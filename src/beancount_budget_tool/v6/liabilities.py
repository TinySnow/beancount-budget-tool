"""Read-only booked liability coordinates for the v6 cutover boundary.

Signed Beancount balances are facts, not obligation allocations.  In
particular, a credit-card payment does not tell us which purchase it settled.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial
from ..refs import _lot
from .facts import FactIndex


def audit_liabilities(root: Path, cutover: date) -> dict:
    """Inventory every liability account with direct booked postings.

    Opening is the sum before cutover; source totals cover all booked entries
    in the snapshot, with each account's latest booked date shown explicitly.
    No sign convention, debt type, owner or due date is inferred.
    """
    require(isinstance(cutover, date) and cutover.day == 1,
            "CUTOVER_INVALID", "cutover 必须是自然月第一天")
    fin = load_financial(root)
    facts = FactIndex(fin)
    liability_root = fin.options["name_liabilities"]
    states: dict[str, dict] = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] != liability_root:
                continue
            meta = posting.meta or {}
            state = states.setdefault(posting.account, {
                "opening": defaultdict(Fraction), "source_total": defaultdict(Fraction),
                "before_cutover_postings": 0, "from_cutover_postings": 0,
                "latest_booked_date": entry.date,
                "first_source": f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}",
                "first_ref": facts.for_posting(posting).as_dict(),
            })
            key = (posting.units.currency, _lot(posting))
            units = exact(posting.units.number)
            state["source_total"][key] += units
            state["latest_booked_date"] = max(state["latest_booked_date"], entry.date)
            if entry.date < cutover:
                state["opening"][key] += units
                state["before_cutover_postings"] += 1
            else:
                state["from_cutover_postings"] += 1

    names = sorted(states)
    rows = []
    for name in names:
        state = states[name]

        def coordinates(balances: dict) -> list[dict]:
            return [{"commodity": commodity, "lot": lot,
                     "signed_units": exact_text(amount)}
                    for (commodity, lot), amount in sorted(balances.items())]

        rows.append({
            "account": name,
            "direct_postings": state["before_cutover_postings"] + state["from_cutover_postings"],
            "before_cutover_postings": state["before_cutover_postings"],
            "from_cutover_postings": state["from_cutover_postings"],
            "has_children_with_direct_postings": any(child.startswith(name + ":") for child in names),
            "opening_coordinates": coordinates(state["opening"]),
            "source_total_coordinates": coordinates(state["source_total"]),
            "latest_booked_date": state["latest_booked_date"].isoformat(),
            "first_source": state["first_source"],
            "first_ref": state["first_ref"],
        })
    return {
        "mode": "v6_liability_audit_only", "ledger_snapshot": fin.digest,
        "loader_version": fin.loader_version, "cutover": cutover.isoformat(),
        "accounts_with_direct_postings": len(rows), "accounts": rows,
        "note": "有符号账面数不是本人责任、到期额、清偿分配或首页可花的认证",
    }

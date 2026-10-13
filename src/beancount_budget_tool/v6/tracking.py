"""Native-currency expense topics; tracking never changes budget ownership."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial
from .facts import FactIndex
from .source import scan_v6_source
from .targets import _month


def _topics(value: object) -> set[str]:
    return set(value.split(",")) if isinstance(value, str) else set()


def audit_monthly_tracking(root: Path, cutover: date, month: str) -> dict:
    """Sum tagged Expenses postings, retaining spent and refund legs separately."""
    month = _month(month)
    first_day = date.fromisoformat(month + "-01")
    require(first_day >= cutover, "MONTH_INVALID", "专题月份不得早于 v6 切换点", month)
    scan_v6_source(root, cutover)
    fin = load_financial(root)
    facts = FactIndex(fin)
    expense_root = fin.options["name_expenses"]
    totals: dict[tuple[str, str], dict[str, Fraction]] = defaultdict(
        lambda: {"spent": Fraction(0), "returned": Fraction(0)})
    events = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date.strftime("%Y-%m") != month:
            continue
        transaction_topics = _topics((entry.meta or {}).get("tracking"))
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] != expense_root:
                continue
            topics = transaction_topics | _topics((posting.meta or {}).get("tracking"))
            if not topics:
                continue
            units = exact(posting.units.number)
            for topic in sorted(topics):
                amounts = totals[(topic, posting.units.currency)]
                if units >= 0:
                    amounts["spent"] += units
                else:
                    amounts["returned"] -= units
                meta = posting.meta or entry.meta
                events.append({"topic": topic, "date": entry.date.isoformat(),
                               "account": posting.account, "commodity": posting.units.currency,
                               "signed_units": exact_text(units),
                               "ref": facts.for_posting(posting).as_dict(),
                               "source": f"{meta['filename']}:{meta['lineno']}"})
    topics = []
    for (topic, commodity), amounts in sorted(totals.items()):
        spent, returned = amounts["spent"], amounts["returned"]
        topics.append({"topic": topic, "commodity": commodity,
                       "spent": exact_text(spent), "returned": exact_text(returned),
                       "net": exact_text(spent - returned)})
    return {"mode": "v6_monthly_tracking", "month": month,
            "ledger_snapshot": fin.digest, "status": "CERTIFIED_NATIVE",
            "topics": topics, "events": events,
            "note": "专题按原币汇总 Expenses；同一支出可属于多个专题，专题间不得相加；不改变预算或用途"}

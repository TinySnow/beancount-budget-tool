"""Separate K book-flow purpose changes from real H inventory.

The sum of subscriptions less redemptions is a bookkeeping coordinate.  A
reviewed event may explain which purposes caused that change, but it cannot
prove fund shares, liquidation value, or pre-cutover purpose composition.
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
from ..yamlstrict import yaml_key_location
from .accounts import _lot, audit_accounts
from .k_events import audit_k_events
from .opening import audit_opening
from .classification import _key
from .review_diagnostics import posting_sources, review_item


def audit_k_book_purposes(root: Path, cutover: date, as_of: date,
                          accounts: Path, opening: Path, ownership: Path,
                          k_events: Path | None = None) -> dict:
    """Reconcile each K coordinate and show only certified post-cutover intent.

    An unreviewed event remains visible as an unassigned signed book change.
    Purpose figures are never merged with H holdings or safe cash.
    """
    require(cutover <= as_of, "TIME_INVALID", "K 账面报告日不得早于 cutover")
    account_audit = audit_accounts(root, cutover, accounts)
    opening_audit = audit_opening(root, cutover, accounts, opening)
    events_audit = audit_k_events(root, cutover, accounts, ownership, k_events)
    fin = load_financial(root)
    require(account_audit["ledger_snapshot"] == opening_audit["ledger_snapshot"] ==
            events_audit["ledger_snapshot"] == fin.digest,
            "K_BOOK_SNAPSHOT_MISMATCH", "K 账面、期初和申赎事件不属于同一账本快照")

    k_accounts = {row["account"]: row for row in account_audit["accounts"]
                  if row.get("semantics") == "K"}
    booked_opening: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    for account, row in k_accounts.items():
        for coordinate in row["opening_coordinates"]:
            booked_opening[(account, coordinate["commodity"], coordinate["lot"])] += (
                Fraction(coordinate["signed_units"]))
        if not row["opening_coordinates"]:
            commodity = next(iter(row["commodities"]))
            booked_opening[(account, commodity, f"{commodity}/no-cost")] += Fraction(0)
    booked_change: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or not cutover <= entry.date <= as_of:
            continue
        for posting in entry.postings:
            if posting.account in k_accounts:
                booked_change[(posting.account, posting.units.currency, _lot(posting))] += (
                    exact(posting.units.number))

    reviewed_change: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    purposes: dict[tuple[str, str, str, str], Fraction] = defaultdict(Fraction)
    pending_count: dict[tuple[str, str, str], int] = defaultdict(int)
    for event in events_audit["events"]:
        if event["date"] > as_of.isoformat():
            continue
        key = (event["k_account"], event["commodity"], event["k_ref"]["lot"])
        if event["status"] != "CERTIFIED":
            pending_count[key] += 1
            continue
        signed = Fraction(event["signed_k_units"])
        reviewed_change[key] += signed
        for part in event["slices"]:
            owner = part["k_owner_id"]
            purposes[(*key, owner)] += Fraction(part["signed_k_units"])

    missing_opening = {(row["account"], row["commodity"], row["lot"])
                       for row in opening_audit["missing_k_openings"]}
    rows = []
    for key in sorted(set(booked_opening) | set(booked_change)):
        account, commodity, lot = key
        opening_units = booked_opening[key]
        change = booked_change[key]
        reviewed = reviewed_change[key]
        unreviewed = change - reviewed
        purpose_rows = [{"owner_id": owner, "signed_change": exact_text(value)}
                        for (a, c, l, owner), value in sorted(purposes.items())
                        if (a, c, l) == key and value]
        require(sum((Fraction(row["signed_change"]) for row in purpose_rows),
                    Fraction(0)) == reviewed,
                "K_BOOK_PURPOSE_MISMATCH", "已审 K 用途切片未加回账面变动", account)
        rows.append({"account": account, "commodity": commodity, "lot": lot,
                     "booked_opening": exact_text(opening_units),
                     "booked_change": exact_text(change),
                     "booked_end": exact_text(opening_units + change),
                     "reviewed_purpose_change": exact_text(reviewed),
                     "unreviewed_change": exact_text(unreviewed),
                     "pending_event_count": pending_count[key],
                     "opening_reviewed": key not in missing_opening,
                     "purpose_changes": purpose_rows,
                     "status": "CERTIFIED_BOOK_FLOW" if key not in missing_opening and
                     not pending_count[key] else "NEEDS_REVIEW"})
    pending = [row for row in rows if row["status"] == "NEEDS_REVIEW"]
    sources = posting_sources(fin)
    review_items = [review_item(
        "K_BOOK_PURPOSE_PENDING", yaml_key_location(opening, "k_openings"),
        account=account, commodity=commodity,
        detail="该 K 账户的期初账面累计数尚未审定。")
        for account, commodity, lot in sorted(missing_opening)]
    for event in events_audit["events"]:
        if event["date"] <= as_of.isoformat() and event["status"] != "CERTIFIED":
            review_items.append(review_item(
                event["reason_codes"][0], sources[_key(event["k_ref"])], ref=event["k_ref"],
                account=event["k_account"], commodity=event["commodity"],
                signed_units=event["signed_k_units"], blocking_reason_code="K_BOOK_PURPOSE_PENDING"))
    zero_records = [event for event in events_audit["events"]
                    if event["shape"] == "zero_record" and event["date"] <= as_of.isoformat()]
    return {"mode": "v6_k_book_purpose_changes", "cutover": cutover.isoformat(),
            "as_of": as_of.isoformat(), "ledger_snapshot": fin.digest,
            "status": "CERTIFIED" if not pending and
            account_audit["review_state"] == "complete" else "NEEDS_REVIEW",
            "rows": rows, "pending_accounts": [row["account"] for row in pending],
            "review_items": review_items,
            **({"zero_value_records": zero_records} if zero_records else {}),
            "note": "逐 K 账户的签约申赎账面数与 cutover 后用途变动；不代表基金份额、市值、可赎回额或真实持有余额，且不与 H 资产合计"}

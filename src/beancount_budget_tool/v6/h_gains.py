"""Prove that a named H ownership gain came from booked Income."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from beancount.core import data

from ..exact import exact, exact_text
from ..financial import load_financial
from .accounts import audit_accounts
from .classification import _key
from .facts import FactIndex
from .ownership import audit_ownership
from .source import scan_v6_source


def audit_h_income_gains(root: Path, cutover: date, accounts: Path,
                         ownership: Path) -> dict:
    """Certify only simple, same-currency Income-to-H purpose gains.

    The H claim is the human purpose decision. Booked Income is independent
    evidence that the increase is a gain, rather than a new allocation G.
    Other transaction shapes remain unsupported for a later protocol.
    """
    scan_v6_source(root, cutover)
    account_audit = audit_accounts(root, cutover, accounts)
    owner_audit = audit_ownership(root, cutover, accounts, ownership)
    fin = load_financial(root)
    facts = FactIndex(fin)
    report_currency = account_audit["report_currency"]
    account_modes = {row["account"]: row.get("semantics")
                     for row in account_audit["accounts"]}
    claims = {_key(row["ref"]): row for row in owner_audit["ownership"]}
    income_root = fin.options["name_income"]
    events = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        for posting in entry.postings:
            if account_modes.get(posting.account) != "H":
                continue
            ref = facts.for_posting(posting).as_dict()
            claim = claims.get(_key(ref))
            if claim is None or not any(part["role"] == "gain" for part in claim["slices"]):
                continue
            other = (next(p for p in entry.postings if p is not posting)
                     if len(entry.postings) == 2 else None)
            units = exact(posting.units.number)
            shape = (len(claim["slices"]) == 1 and
                     claim["slices"][0]["role"] == "gain" and
                     claim["slices"][0]["owner_id"] != "-" and
                     units > 0 and posting.units.currency == report_currency and
                     posting.cost is None and posting.price is None and
                     fin.raw_price(posting)[0] == "none" and
                     other is not None and
                     other.account.split(":", 1)[0] == income_root and
                     other.units.currency == report_currency and
                     exact(other.units.number) == -units and
                     other.cost is None and other.price is None and
                     fin.raw_price(other)[0] == "none")
            events.append({"h_ref": ref,
                           "income_ref": facts.for_posting(other).as_dict()
                           if shape else None,
                           "date": entry.date.isoformat(),
                           "h_account": posting.account,
                           "commodity": posting.units.currency,
                           "signed_h_units": exact_text(units),
                           "owner_id": claim["slices"][0]["owner_id"]
                           if shape else None,
                           "status": "CERTIFIED" if shape else "UNSUPPORTED",
                           "reason_code": None if shape else "H_GAIN_SHAPE_UNSUPPORTED"})
    complete = (account_audit["review_state"] == "complete" and
                all(row["status"] == "CERTIFIED" for row in events))
    return {"mode": "v6_simple_h_income_gain_audit",
            "cutover": cutover.isoformat(), "ledger_snapshot": fin.digest,
            "review_state": "complete" if complete else "pending",
            "certified_events": sum(row["status"] == "CERTIFIED" for row in events),
            "unsupported_events": sum(row["status"] == "UNSUPPORTED" for row in events),
            "events": events,
            "note": "仅证明已审命名 H 损益由简单 booked Income 引起；不构成用途净划拨或完整首页"}

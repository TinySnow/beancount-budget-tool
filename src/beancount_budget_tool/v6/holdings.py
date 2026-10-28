"""Replay reviewed H owner slices against booked native inventory.

This is a scoped holdings audit. It does not price securities, infer budget
allocations, or turn payable holdings into free cash for the v6 home page.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial, shared_audit
from ..yamlstrict import load_yaml
from .accounts import _lot, audit_accounts
from .classification import _key
from .facts import FactIndex
from .opening import audit_opening
from .ownership import _owners, audit_ownership


@shared_audit
def audit_h_holdings(root: Path, cutover: date, as_of: date, accounts: Path,
                     opening: Path, ownership: Path) -> dict:
    """Certify each complete account/commodity/lot at a date end.

    A missing opening slice or H posting claim blocks that coordinate's
    ending owner inventory. Valid coordinates remain separately visible.
    Credit-card same-day bridges may cross zero only on their exact approved
    source refs and must close in the same owner before the date end.
    """
    require(isinstance(as_of, date) and cutover <= as_of,
            "TIME_INVALID", "持有报告日期不得早于 cutover")
    account_audit = audit_accounts(root, cutover, accounts)
    opening_audit = audit_opening(root, cutover, accounts, opening)
    owner_audit = audit_ownership(root, cutover, accounts, ownership, opening)
    fin = load_financial(root)
    facts = FactIndex(fin)
    account_rows = {row["account"]: row for row in account_audit["accounts"]
                    if row.get("semantics") == "H"}
    owner_document = load_yaml(ownership)
    dual_defaults = "defaults" in owner_document
    owner_ids = set(_owners(owner_document["owners"]).values())
    opening_document = load_yaml(opening)
    opening_units: dict[tuple[str, str, str], dict[str, Fraction]] = defaultdict(
        lambda: defaultdict(Fraction))
    for row in opening_document["h_holdings"]:
        owner = row["owner"]
        require(owner == "-" or owner in owner_ids, "OPENING_OWNER_UNKNOWN",
                "H 期初 owner 未登记于归属名册", str(row))
        key = (row["account"], row["commodity"], row["lot"])
        opening_units[key][owner] += Fraction(row["units"])

    expected: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    payment_scope = {}
    for account, row in account_rows.items():
        payment_scope[account] = row["payment_scope"]
        for coordinate in row["opening_coordinates"]:
            key = (account, coordinate["commodity"], coordinate["lot"])
            expected[key] = Fraction(coordinate["signed_units"])

    claims = {_key(row["ref"]): row for row in owner_audit["ownership"]}
    bridges = { _key(row["debit_ref"]): ("debit", row)
                for row in account_audit["same_day_settlements"] }
    bridges.update({_key(row["funding_ref"]): ("funding", row)
                    for row in account_audit["same_day_settlements"]})
    running: dict[tuple[str, str, str], dict[str, Fraction]] = {}
    pending: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    bridge_pending: dict[tuple[str, str, str], tuple[dict, str]] = {}

    for key, booked in expected.items():
        claimed = sum(opening_units[key].values(), Fraction(0))
        if claimed != booked:
            pending[key].append({"code": "OPENING_PENDING",
                                 "unreviewed_units": exact_text(booked - claimed)})
        else:
            running[key] = dict(opening_units[key])

    asset_root = fin.options["name_assets"]
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or not cutover <= entry.date <= as_of:
            continue
        changes: dict[tuple[str, str, str], list[tuple[dict, dict | None, Fraction]]] = defaultdict(list)
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] != asset_root or posting.account not in account_rows:
                continue
            key = (posting.account, posting.units.currency, _lot(posting))
            expected.setdefault(key, Fraction(0))
            if key not in running and key not in pending:
                running[key] = {}
            ref = facts.for_posting(posting).as_dict()
            changes[key].append((ref, claims.get(_key(ref)), exact(posting.units.number)))
        for key, events in changes.items():
            expected[key] += sum((units for _, _, units in events), Fraction(0))
            if key in pending:
                continue
            unclaimed = [ref for ref, claim, _ in events if claim is None]
            if unclaimed:
                pending[key].append({"code": "OWNERSHIP_PENDING", "refs": unclaimed})
                running.pop(key, None)
                bridge_pending.pop(key, None)
                continue
            changes_by_owner: dict[str, Fraction] = defaultdict(Fraction)
            for _, claim, _ in events:
                for part in claim["slices"]:
                    changes_by_owner[part["owner_id"]] += Fraction(part["signed_units"])
            before = running[key]
            after = {owner: before.get(owner, Fraction(0)) + delta
                     for owner, delta in changes_by_owner.items()}
            for owner, amount in before.items():
                after.setdefault(owner, amount)
            worsened_deficits = {owner for owner, amount in after.items()
                                 if amount < 0 and amount < before.get(owner, Fraction(0))}
            bridge_role = bridges.get(_key(events[0][0])) if len(events) == 1 else None
            if key in bridge_pending:
                claim, owner = bridge_pending.pop(key)
                require(bridge_role is not None and bridge_role[0] == "funding" and
                        bridge_role[1] == claim and changes_by_owner == {owner: Fraction(claim["units"])} and
                        before.get(owner, Fraction(0)) == -Fraction(claim["units"]) and
                        after[owner] == 0,
                        "H_OWNER_SETTLEMENT_INVALID", "H 用途结算桥须由同日同用途入账精确清零", str(events[0][0]))
            elif worsened_deficits and dual_defaults and all(owner != "-" for owner in worsened_deficits) and any(
                    posting.account.split(":", 1)[0] in {
                        fin.options["name_expenses"], fin.options["name_income"],
                        fin.options["name_liabilities"]}
                    for posting in entry.postings):
                # Budget shares can cross zero on an actual expense, income
                # reversal or debt payment. The booked H account's real
                # inventory is checked independently by audit_accounts.
                pass
            elif worsened_deficits or (not dual_defaults and any(amount < 0 for amount in after.values())):
                require(bridge_role is not None and bridge_role[0] == "debit" and
                        changes_by_owner and len(changes_by_owner) == 1,
                        "H_OWNER_NEGATIVE", "H 用途份额逐事件变为负数", str(events[0][0]))
                claim = bridge_role[1]
                owner, delta = next(iter(changes_by_owner.items()))
                require(delta == -Fraction(claim["units"]) and
                        before.get(owner, Fraction(0)) == 0 and
                        after[owner] == -Fraction(claim["units"]),
                        "H_OWNER_SETTLEMENT_INVALID", "H 用途结算桥与已审资产扣款不一致", str(events[0][0]))
                bridge_pending[key] = (claim, owner)
            require(sum(after.values(), Fraction(0)) == expected[key],
                    "H_OWNER_CONSERVATION", "H 用途份额与 booked 原生库存不守恒", str(events[0][0]))
            running[key] = after

    require(not bridge_pending, "H_OWNER_SETTLEMENT_INVALID",
            "H 用途结算桥到报告日末仍未清零")
    positions = []
    for key in sorted(expected):
        account, commodity, lot = key
        if expected[key] == 0 and not pending.get(key) and not running.get(key):
            continue
        if pending.get(key):
            positions.append({"account": account, "commodity": commodity, "lot": lot,
                              "status": "NEEDS_REVIEW", "owner_units": None,
                              "reasons": pending[key],
                              "payment_scope": payment_scope[account][commodity]})
            continue
        owners = running.get(key, {})
        require(sum(owners.values(), Fraction(0)) == expected[key] and
                (dual_defaults or all(amount >= 0 for amount in owners.values())),
                "H_OWNER_CONSERVATION", "H 期末用途份额与 booked 库存不守恒", str(key))
        positions.append({"account": account, "commodity": commodity, "lot": lot,
                          "status": "CERTIFIED", "owner_units": [
                              {"owner_id": owner, "units": exact_text(amount)}
                              for owner, amount in sorted(owners.items()) if amount != 0],
                          "reasons": [], "payment_scope": payment_scope[account][commodity]})
    complete = (account_audit["review_state"] == "complete" and
                not opening_audit["missing_h_ownership"] and
                not owner_audit["unknown_account_semantics"] and
                all(row["status"] == "CERTIFIED" for row in positions))
    return {"mode": "v6_known_h_holdings_audit", "cutover": cutover.isoformat(),
            "as_of": as_of.isoformat(), "ledger_snapshot": fin.digest,
            "review_state": "complete" if complete else "pending",
            "certified_coordinates": sum(row["status"] == "CERTIFIED" for row in positions),
            "pending_coordinates": sum(row["status"] != "CERTIFIED" for row in positions),
            "positions": positions,
            "note": "仅为已审 H 原生持有；不含 K 市值、不算用途净划拨或首页可花"}

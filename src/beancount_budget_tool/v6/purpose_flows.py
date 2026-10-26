"""Monthly G from distinct, certified purpose-boundary protocols."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path
import calendar

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial
from ..yamlstrict import load_yaml
from .classification import _key
from .facts import FactIndex
from .h_fx import audit_h_fx
from .h_gains import audit_h_income_gains
from .h_transfers import audit_h_transfers
from .holdings import audit_h_holdings
from .initialization import initial_pad_postings
from .k_events import audit_k_events
from .obligations import audit_simple_obligations
from .native_flows import native_asset_flow
from .ownership import _owners, audit_ownership
from .purpose_targets import _month
from .purpose_pools import audit_purpose_pools


def audit_monthly_purpose_flows(root: Path, cutover: date, month: str,
                                accounts: Path, opening: Path, ownership: Path,
                                h_transfers: Path | None = None,
                                k_events: Path | None = None,
                                h_fx: Path | None = None,
                                purpose_pools: Path | None = None,
                                obligations: Path | None = None, *, include_evidence: bool = False) -> dict:
    """Sign G only when every named H change has a certified event protocol.

    Free H claims alone do not create G. Unknown H claims can conceal a named
    purpose change, so the monthly total stays null until ownership closes.
    Event legs are claimed once across transfer, K, FX, gain and reviewed
    liability-settlement audits. A repayment or third-party reimbursement
    consumes or restores an asset but creates no purpose allocation G.
    """
    _month(month)
    first = date.fromisoformat(month + "-01")
    require(first >= cutover,
            "MONTH_INVALID", "用途净划拨月份不得早于 cutover", month)
    last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
    owner_audit = audit_ownership(root, cutover, accounts, ownership, opening)
    holdings = audit_h_holdings(root, cutover, last, accounts, opening, ownership)
    transfers = audit_h_transfers(root, cutover, accounts, ownership, h_transfers, opening)
    books = audit_k_events(root, cutover, accounts, ownership, k_events)
    exchange = audit_h_fx(root, cutover, accounts, ownership, h_fx)
    gains = audit_h_income_gains(root, cutover, accounts, ownership)
    debt = audit_simple_obligations(root, cutover, obligations, last) if obligations else None
    reports = [owner_audit, holdings, transfers, books, exchange, gains]
    if debt is not None:
        reports.append(debt)
    snapshots = {report["ledger_snapshot"] for report in reports}
    require(len(snapshots) == 1, "PURPOSE_SNAPSHOT_MISMATCH",
            "用途事件证据不属于同一账本快照")
    owner_document = load_yaml(ownership)
    owner_ids = set(_owners(owner_document["owners"]).values())
    totals = {owner: Fraction(0) for owner in owner_ids}
    used_h = set()
    known = []
    pending = []

    def add(kind: str, event: dict, refs: list[dict], allocations: dict[str, str], *, source_refs=None) -> None:
        keys = [_key(ref) for ref in refs]
        require(len(keys) == len(set(keys)) and not (set(keys) & used_h),
                "PURPOSE_EVENT_DOUBLE_CLAIM", "H 资产腿被多个用途边界事件使用", kind)
        used_h.update(keys)
        for owner, amount in allocations.items():
            require(owner in owner_ids, "PURPOSE_OWNER_UNKNOWN",
                    "事件净划拨引用未登记用途", owner)
            totals[owner] += Fraction(amount)
        known.append({"kind": kind, "date": event["date"],
                      "h_refs": refs, "net_allocations": allocations,
                      **({"source_refs": source_refs} if source_refs is not None else {}),
                      **({"protocol": event} if include_evidence else {})})

    for event in transfers["events"]:
        if event["date"][:7] != month or event["status"] != "CERTIFIED":
            continue
        add("h_transfer", event, [event["from_ref"], event["to_ref"]],
            event["net_allocations"])
    for event in books["events"]:
        if event["date"][:7] != month:
            continue
        if event["status"] == "CERTIFIED":
            if event["shape"] == "zero_record":
                add("k_event", event, [], {}, source_refs=[event["k_ref"]])
            else:
                add("k_event", event, [event["cash_ref"]], event["net_allocations"])
        else:
            pending.append({"kind": "k_event", "ref": event["k_ref"],
                            "reason": event["status"]})
    for event in exchange["events"]:
        if event["date"][:7] != month:
            continue
        if event["status"] == "CERTIFIED":
            add("h_fx", event, [event["from_ref"], event["to_ref"]], {})
        else:
            pending.append({"kind": "h_fx", "ref": event["from_ref"],
                            "reason": event["status"]})
    for event in gains["events"]:
        if event["date"][:7] != month:
            continue
        if event["status"] == "CERTIFIED":
            add("h_gain", event, [event["h_ref"]], {})
        else:
            pending.append({"kind": "h_gain", "ref": event["h_ref"],
                            "reason": event["status"]})
    if debt is not None:
        claimed = {_key(row["ref"]): row for row in owner_audit["ownership"]}
        claimed_h = set(claimed)
        debt_by_ref = {_key(row["liability_ref"]): row for row in debt["events"]}
        used_purchase_groups = set()
        used_payment_groups = set()
        used_rule_groups = set()
        for event in debt["events"]:
            if event["date"][:7] != month or event["status"] != "CERTIFIED":
                continue
            if event.get("proof") == "configured_liability_account_v1":
                group = _key(event["transaction_ref"])
                members = [debt_by_ref[_key(ref)] for ref in event["liability_refs"]]
                if group in used_rule_groups or any(row["status"] != "CERTIFIED" for row in members):
                    continue
                used_rule_groups.add(group)
                refs = event["asset_refs"]
                if all(_key(ref) in claimed_h and all(part["role"] == "hold"
                       for part in claimed[_key(ref)]["slices"]) for ref in refs):
                    add("liability_" + event["kind"], event, refs, {},
                        source_refs=event["liability_refs"] if not refs else None)
                continue
            if event["kind"] == "payable_receipt":
                refs = event["asset_refs"]
                if all(_key(ref) in claimed_h and all(part["role"] == "hold"
                       for part in claimed[_key(ref)]["slices"]) for ref in refs):
                    add("liability_payable_receipt", event, refs, {})
                continue
            if event["kind"] == "consumer_purchase":
                # Only the observed cash share changes H holdings. The card
                # share remains a debt, not another asset payment or G.
                refs = event.get("asset_refs", [])
                if event.get("proof") == "native_joint_card_purchase_v1":
                    group = _key(event["purchase_group_ref"])
                    members = [debt_by_ref[_key(ref)] for ref in event["liability_refs"]]
                    if group in used_purchase_groups or any(row["status"] != "CERTIFIED" for row in members):
                        continue
                    used_purchase_groups.add(group)
                    event = {**event, "obligation_ids": [row["obligation_id"] for row in members]}
                if refs and all(_key(ref) in claimed_h and all(
                        part["role"] == "hold" for part in claimed[_key(ref)]["slices"]) for ref in refs):
                    add("liability_consumer_purchase", event, refs, {})
                continue
            if event["kind"] not in ("payment", "advance_outflow", "advance_reimbursement"):
                continue
            if event.get("proof") == "native_group_payment_v1":
                group = _key(event["payment_group_ref"])
                members = [debt_by_ref[_key(ref)] for ref in event["liability_refs"]]
                if group in used_payment_groups or any(row["status"] != "CERTIFIED" for row in members):
                    continue
                used_payment_groups.add(group)
                event = {**event, "obligation_ids": [row["obligation_id"] for row in members]}
            ref = event["counterpart_ref"]
            if ref is not None and _key(ref) in claimed_h:
                add("liability_" + event["kind"], event, [ref], {})
    for event in transfers["events"]:
        if event["date"][:7] != month or event["status"] == "CERTIFIED":
            continue
        if event["status"] == "UNSUPPORTED" and (
                _key(event["from_ref"]) in used_h and
                _key(event["to_ref"]) in used_h):
            continue  # independently certified by the FX protocol
        pending.append({"kind": "h_transfer", "ref": event["from_ref"],
                        "reason": event["status"]})

    # Source refs do not contain a date. Use booked transaction dates for all
    # uncovered legs, including pad-generated postings.
    fin = load_financial(root)
    pool_report = audit_purpose_pools(root, cutover, last, purpose_pools) if purpose_pools is not None else None
    pool_accounts = {name for row in pool_report["pools"] for name in row["accounts"]} if pool_report else set()
    facts = FactIndex(fin)
    initial_pads = initial_pad_postings(fin, cutover)
    by_ref = {_key(row["ref"]): row for row in owner_audit["ownership"]}
    for posting_id, entry in initial_pads.items():
        if not first <= entry.date <= last:
            continue
        posting = next(p for p in entry.postings if id(p) == posting_id)
        ref = facts.for_posting(posting).as_dict()
        key = _key(ref)
        if key not in used_h and key in by_ref and all(
                part["role"] == "hold" for part in by_ref[key]["slices"]):
            add("initial_balance", {"date": entry.date.isoformat()}, [ref], {})
    if "defaults" in owner_document:
        report_currency = load_yaml(accounts)["report_currency"]
        h_accounts = {row["account"] for row in load_yaml(accounts)["accounts"]
                      if row["semantics"] == "H"}
        modes = {name: "H" for name in h_accounts}
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction) or not first <= entry.date <= last:
                continue
            native = native_asset_flow(fin, entry, modes)
            if native is None:
                continue
            refs = [facts.for_posting(p).as_dict() for p in native.assets]
            keys = [_key(ref) for ref in refs]
            if any(key in used_h or key not in by_ref or any(
                    part["role"] != "hold" for part in by_ref[key]["slices"]) for key in keys):
                continue
            allocations = {}
            if native.kind == "income" and native.assets[0].units.currency == report_currency:
                default = owner_document["defaults"]["income_owner_id"]
                for posting, key in zip(native.assets, keys):
                    if posting.account in pool_accounts or by_ref[key].get("origin") != "inline":
                        continue
                    for part in by_ref[key]["slices"]:
                        owner, units = part["owner_id"], Fraction(part["signed_units"])
                        if owner not in (default, "-"):
                            allocations[owner] = allocations.get(owner, Fraction(0)) + units
                            allocations[default] = allocations.get(default, Fraction(0)) - units
            event = {"date": entry.date.isoformat()}
            if allocations:
                event.update(protocol="inline_income_allocation_v1",
                             income_refs=[facts.for_posting(p).as_dict() for p in native.counterparts])
            add("income_asset" if native.kind == "income" else "expense_asset", event, refs,
                {owner: exact_text(value) for owner, value in allocations.items() if value})
    pool_internal_events = 0
    pool_internal_keys: set[str] = set()
    if pool_report is not None:
        require(pool_report["ledger_snapshot"] == fin.digest and
                pool_report["report_currency"] == load_yaml(accounts)["report_currency"],
                "PURPOSE_POOL_MISMATCH", "资金池与月度用途审计的账本或货币不一致")
        claimed = {_key(row["ref"]): row for row in owner_audit["ownership"]}
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction) or not first <= entry.date <= last:
                continue
            inside = [p for p in entry.postings if p.account in pool_accounts]
            if not inside:
                continue
            outside = [p for p in entry.postings if p.account not in pool_accounts]
            if not all(p.account.split(":", 1)[0] in
                       (fin.options["name_income"], fin.options["name_expenses"]) and
                       p.units.currency == pool_report["report_currency"] and
                       p.cost is None for p in outside):
                continue  # a real boundary needs another certified event protocol
            keys = [_key(facts.for_posting(p).as_dict()) for p in inside]
            require(all(not any(part["role"] == "allocation"
                                for part in claimed.get(key, {}).get("slices", []))
                        for key in keys),
                    "POOL_INTERNAL_ALLOCATION_CONFLICT",
                    "证券池内部交易不能同时声明首次用途划拨", entry.date.isoformat())
            used_h.update(keys)
            pool_internal_keys.update(keys)
            pool_internal_events += 1
        pending = [row for row in pending
                   if not (row["kind"] in ("h_fx", "h_transfer") and
                           row.get("ref") is not None and
                           _key(row["ref"]) in pool_internal_keys)]
    # History is replayed above; this projection only needs this month's refs.
    month_sources = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or not first <= entry.date <= last:
            continue
        for posting in entry.postings:
            key = _key(facts.for_posting(posting).as_dict())
            meta = posting.meta or {}
            month_sources[key] = f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"
    for row in owner_audit["ownership"]:
        key = _key(row["ref"])
        if key not in month_sources or key in used_h:
            continue
        if any(part["owner_id"] != "-" or part["role"] != "hold"
               for part in row["slices"]):
            pending.append({"kind": "named_h_without_protocol", "ref": row["ref"],
                            "reason": "NAMED_H_EVENT_UNSUPPORTED"})
    for row in owner_audit["missing_h_ownership"]:
        if _key(row["ref"]) in month_sources and _key(row["ref"]) not in used_h:
            pending.append({"kind": "h_ownership", "ref": row["ref"],
                            "reason": "H_OWNER_PENDING"})
    for row in owner_audit["unknown_account_semantics"]:
        if _key(row["ref"]) in month_sources:
            pending.append({"kind": "account_semantics", "ref": row["ref"],
                            "reason": "ACCOUNT_SEMANTICS_PENDING"})
    # The pool protocol proves booked native lots and nominal coverage without
    # asserting per-lot owner slices. Other H coordinates still require the
    # ordinary exact ownership replay.
    outside_pending = [position for position in holdings["positions"]
                       if position["account"] not in pool_accounts and
                       position["status"] != "CERTIFIED"]
    if holdings["review_state"] != "complete" and outside_pending:
        pending.append({"kind": "h_inventory", "ref": None,
                        "reason": "H_HOLDINGS_PENDING",
                        "pending_coordinates": len(outside_pending)})
    # Any pool posting with an external Assets/Liabilities/Equity counterparty
    # must be covered by a certified transfer or other boundary event.
    if pool_accounts:
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction) or not first <= entry.date <= last:
                continue
            inside = [p for p in entry.postings if p.account in pool_accounts]
            outside = [p for p in entry.postings if p.account not in pool_accounts]
            if not inside or not any(p.account.split(":", 1)[0] not in
                    (fin.options["name_income"], fin.options["name_expenses"])
                    for p in outside):
                continue
            for posting in inside:
                ref = facts.for_posting(posting).as_dict()
                if _key(ref) not in used_h:
                    pending.append({"kind": "pool_external_without_protocol",
                                    "ref": ref, "reason": "POOL_BOUNDARY_PENDING"})
    complete = not pending
    for row in pending:
        if row.get("ref") is not None:
            row["source"] = month_sources.get(_key(row["ref"]))
    evidence = ({"_transaction_evidence": {
        "ownership": [row for row in owner_audit["ownership"] if _key(row["ref"]) in month_sources],
        "events": known, "pool_internal_keys": sorted(pool_internal_keys),
        "liabilities": [row for row in debt["events"] if row["date"][:7] == month] if debt else [],
        "responsibility_owners": {row["obligation_id"]: row["owner_id"] for row in debt["obligations"]} if debt else {},
        "responsibility_names": {row["id"]: row["name"] for row in load_yaml(obligations)["owners"]} if debt else {},
    }} if include_evidence else {})
    return {**evidence, "mode": "v6_monthly_purpose_flow_audit", "cutover": cutover.isoformat(),
            "month": month, "report_currency": load_yaml(accounts)["report_currency"],
            "ledger_snapshot": owner_audit["ledger_snapshot"],
            "status": "CERTIFIED" if complete else "NEEDS_REVIEW",
            "net_allocations": {owner: exact_text(amount) for owner, amount in sorted(totals.items())}
                               if complete else None,
            "known_event_allocations": {owner: exact_text(amount)
                                        for owner, amount in sorted(totals.items()) if amount},
            "known_events": known, "pending_events": pending,
            "pool_internal_events": pool_internal_events,
            "note": "已知事件贡献不等于整月用途净划拨；任一可能改变命名用途的腿待审时总数为空"}

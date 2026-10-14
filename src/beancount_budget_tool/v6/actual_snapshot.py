"""Source-compiled actual snapshot with explicit native card-debt coverage."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial
from ..yamlstrict import load_yaml, yaml_key_location
from .accounts import _lot
from .core import ActualInput, CashResource, Obligation, calculate_actual
from .debt_coverage import audit_debt_coverage
from .obligations import audit_simple_obligations
from .payable_cash import audit_payable_cash
from .classification import _key
from .review_diagnostics import posting_sources, review_item


def audit_actual_snapshot(root: Path, cutover: date, as_of: date,
                          accounts: Path, opening: Path, ownership: Path,
                          obligations: Path, completeness: Path | None = None,
                          debt_coverage: Path | None = None) -> dict:
    """Compile audited cash, simple native card debt and explicit reservations.

    A human statement covers unbooked responsibilities and in-flight payments.
    Every nonzero booked liability must match reviewed obligation IDs exactly.
    Named-purpose coverage is bound to reviewed cash and purchase sources.
    Foreign report-date settlement needs a separate protocol.
    """
    cash = audit_payable_cash(root, cutover, as_of, accounts, opening, ownership)
    debt = audit_simple_obligations(root, cutover, obligations, as_of)
    fin = load_financial(root)
    require(cash["ledger_snapshot"] == debt["ledger_snapshot"] == fin.digest,
            "ACTUAL_SNAPSHOT_MISMATCH", "现金与责任证据不属于同一账本快照")
    liability_root = fin.options["name_liabilities"]
    sources = posting_sources(fin)
    liability_sources = {}
    balances: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date > as_of:
            continue
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] == liability_root:
                coordinate = (posting.account, posting.units.currency, _lot(posting))
                balances[coordinate] += exact(posting.units.number)
                meta = posting.meta or entry.meta
                liability_sources[coordinate] = f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"
    unresolved_liabilities = [
        {"account": account, "commodity": commodity, "lot": lot,
         "signed_booked_units": exact_text(amount)}
        for (account, commodity, lot), amount in sorted(balances.items()) if amount]
    unresolved_obligations = [row for row in debt["obligations"]
                              if Fraction(row["remaining_native"]) != 0]
    unresolved_clearings = [row for row in debt["clearing_advances"]
                            if Fraction(row["remaining_native"]) != 0]

    # A booked liability is not an extra debt on top of its obligation IDs.
    # Match the full signed ledger vector to those IDs before handing them to
    # the pure core; this prevents both omitted and double-counted card debt.
    obligation_by_coordinate: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    booked_debts = []
    for row in unresolved_obligations:
        amount = Fraction(row["remaining_native"])
        obligation_by_coordinate[(row["account"], row["commodity"], row["lot"])] += amount
        booked_debts.append({"obligation_id": row["obligation_id"],
                             "account": row["account"], "commodity": row["commodity"],
                             "owner_id": row["owner_id"], "remaining_native": row["remaining_native"],
                             "purchase_ref": row["purchase_ref"]})
    liability_matches = all(
        amount == -obligation_by_coordinate.get(coordinate, Fraction(0))
        for coordinate, amount in balances.items()) and all(
            -balances.get(coordinate, Fraction(0)) == amount
            for coordinate, amount in obligation_by_coordinate.items())
    coverage_audit = None
    if cash["review_state"] == debt["review_state"] == "complete" and liability_matches:
        coverage_audit = audit_debt_coverage(cash, booked_debts, cutover, as_of, sources, debt_coverage)

    declaration_sha = None
    complete_statement = False
    if completeness is not None:
        document = load_yaml(completeness)
        required = {
            "schema", "cutover", "as_of",
            "unbooked_obligations", "inflight_payments", "reason"}
        require(type(document) is dict and required <= set(document) <=
                required | {"ledger_snapshot", "as_of_booked_fingerprint"},
            "ACTUAL_SCOPE_INVALID", "完整性声明字段无效", f"{completeness.resolve()}:1")
        for key, valid in (
            ("schema", type(document["schema"]) is int and document["schema"] == 6),
            ("cutover", document["cutover"] == cutover.isoformat()),
            ("as_of", document["as_of"] == as_of.isoformat()),
            ("unbooked_obligations", document["unbooked_obligations"] == "none"),
            ("inflight_payments", document["inflight_payments"] == "none"),
            ("reason", type(document["reason"]) is str and bool(document["reason"].strip())),
        ):
            require(valid, "ACTUAL_SCOPE_INVALID", f"完整性声明 {key} 无效",
                    yaml_key_location(completeness, key))
        declaration_sha = sha256(completeness.read_bytes()).hexdigest()
        complete_statement = True

    reasons = []
    if cash["review_state"] != "complete":
        reasons.append("PAYABLE_CASH_PENDING")
    if debt["review_state"] != "complete":
        reasons.append("BOOKED_OBLIGATIONS_PENDING")
    if not liability_matches:
        reasons.append("BOOKED_LIABILITY_UNMATCHED")
    if unresolved_clearings:
        reasons.append("CLEARING_NONZERO")
    if any(row["commodity"] != cash["report_currency"] for row in booked_debts):
        reasons.append("SETTLEMENT_VALUE_MISSING")
    if coverage_audit is not None and coverage_audit["review_state"] != "complete":
        reasons.append("DEBT_COVERAGE_PENDING")
    if not complete_statement:
        reasons.append("EXTERNAL_SCOPE_PENDING")
    review_items = []
    if cash["review_state"] != "complete":
        for row in cash["pending_payable_positions"]:
            refs = [ref for reason in row.get("reasons", []) for ref in reason.get("refs", [])]
            if refs:
                for ref in refs:
                    review_items.append(review_item(
                        "OWNERSHIP_PENDING", sources.get(_key(ref)), ref=ref,
                        account=row["account"], commodity=row["commodity"],
                        blocking_reason_code="PAYABLE_CASH_PENDING"))
            else:
                opening_pending = "OPENING_PENDING" in row["reason_codes"]
                review_items.append(review_item(
                    "OPENING_PENDING" if opening_pending else "PAYABLE_CASH_PENDING",
                    yaml_key_location(opening if opening_pending else accounts,
                                      "h_holdings" if opening_pending else "accounts"),
                    account=row["account"], commodity=row["commodity"],
                    blocking_reason_code="PAYABLE_CASH_PENDING"))
        if not cash["pending_payable_positions"]:
            review_items.append(review_item("PAYABLE_CASH_PENDING", yaml_key_location(accounts, "accounts")))
    if debt["review_state"] != "complete":
        for row in debt["events"]:
            if row["status"] != "CERTIFIED":
                ref = row["liability_ref"]
                review_items.append(review_item(
                    row["reason_codes"][0], sources.get(_key(ref)), ref=ref,
                    account=row["account"], commodity=row["commodity"], signed_units=row["signed_units"],
                    blocking_reason_code="BOOKED_OBLIGATIONS_PENDING"))
        for row in debt["opening_pending"]:
            coordinate = (row["account"], row["commodity"], row["lot"])
            review_items.append(review_item(
                "LIABILITY_OPENING_PENDING", liability_sources.get(coordinate), **row,
                blocking_reason_code="BOOKED_OBLIGATIONS_PENDING"))
    if not liability_matches:
        for coordinate in sorted(set(balances) | set(obligation_by_coordinate)):
            if balances[coordinate] == -obligation_by_coordinate[coordinate]:
                continue
            account, commodity, lot = coordinate
            review_items.append(review_item(
                "BOOKED_LIABILITY_UNMATCHED", liability_sources.get(coordinate), account=account,
                commodity=commodity, lot=lot, signed_booked_units=exact_text(balances[coordinate]),
                reviewed_remaining=exact_text(obligation_by_coordinate[coordinate])))
    for row in unresolved_clearings:
        review_items.append(review_item("CLEARING_NONZERO", sources.get(_key(row["outflow_ref"])),
                                        ref=row["outflow_ref"], account=row["account"],
                                        commodity=row["commodity"], remaining_native=row["remaining_native"]))
    for row in booked_debts:
        if row["commodity"] != cash["report_currency"]:
            review_items.append(review_item("SETTLEMENT_VALUE_MISSING", sources.get(_key(row["purchase_ref"])),
                                            ref=row["purchase_ref"], account=row["account"],
                                            commodity=row["commodity"], owner_id=row["owner_id"],
                                            remaining_native=row["remaining_native"]))
    if coverage_audit is not None:
        review_items.extend(coverage_audit["review_items"])
    if not complete_statement:
        review_items.append(review_item("EXTERNAL_SCOPE_PENDING", None))
    actual = None
    protected_shortfall = None
    purpose_available = None
    uncovered_responsibility = None
    if not reasons:
        # In dual-default mode '-' is explicitly outside the budget system.
        # It remains visible in the cash inventory but cannot become a free
        # resource merely because the legacy core used '-' for free cash.
        outside_owner = "-" if "-" not in cash["spendable_owner_ids"] else None
        resources = tuple(
            CashResource(id=f"payable:{row['owner_id']}",
                         amount=Fraction(row["units"]),
                         currency=cash["report_currency"],
                         owner=row["owner_id"], payable=True,
                         evidence=f"H:{fin.digest}:{as_of.isoformat()}")
            for row in cash["owner_units"] if row["owner_id"] != outside_owner)
        obligations_input = tuple(
            Obligation(id=row["obligation_id"],
                       remaining=Fraction(row["remaining_native"]),
                       currency=row["commodity"], kind="consumer",
                       owner=row["owner_id"], due_on=None,
                       evidence=f"booked:{fin.digest}:{row['obligation_id']}")
            for row in booked_debts)
        state = ActualInput(as_of=as_of, report_currency=cash["report_currency"],
                            resources=resources, obligations=obligations_input,
                            coverage=coverage_audit["coverage"],
                            scope_complete=True,
                            scope_evidence=f"cash:{fin.digest};completeness:{declaration_sha};coverage:{coverage_audit['review_sha256']}",
                            free_owner_ids=tuple(cash["spendable_owner_ids"]))
        report = calculate_actual(state)
        if report.capability.value is None:
            reasons.extend(report.capability.reasons)
        else:
            actual = exact_text(report.capability.value)
            protected_shortfall = exact_text(report.protected_shortfall)
            uncovered_responsibility = exact_text(report.uncovered)
            purpose_available = [{"owner_id": owner, "units": exact_text(amount)}
                                 for owner, amount in report.purpose_available]
    return {"mode": "v6_actual_simple_booked_debt_snapshot",
            "cutover": cutover.isoformat(), "as_of": as_of.isoformat(),
            "ledger_snapshot": fin.digest, "completeness_sha256": declaration_sha,
            "status": "CERTIFIED" if actual is not None else "NEEDS_REVIEW",
            "actual_spendable": actual,
            "protected_shortfall": protected_shortfall,
            "uncovered_responsibility": uncovered_responsibility,
            "purpose_available": purpose_available,
            "debt_coverage_sha256": coverage_audit["review_sha256"] if coverage_audit else None,
            "coverage_allocations": coverage_audit["allocations"] if actual is not None else None,
            "obligation_coverage": coverage_audit["obligations"] if actual is not None else None,
            "outside_budget_cash": cash["outside_budget_cash"],
            "report_currency": cash["report_currency"],
            "reason_codes": reasons,
            "review_items": review_items,
            "payable_cash_state": cash["review_state"],
            "booked_obligation_state": debt["review_state"],
            "unresolved_liabilities": unresolved_liabilities,
            "unresolved_obligations": unresolved_obligations,
            "unresolved_clearings": unresolved_clearings,
            "booked_debts": booked_debts,
            "note": (f"本报告截至 {as_of.isoformat()} 日末，只反映该日及以前已发生的事实；"
                     "此后新发生的消费、负债和付款不倒扣到本报告。"
                     "如用本报告判断当前可花，请检查报告截止日至查看日期间的新交易与负债。"
                     "截至报告日已发生但漏记的责任或在途付款仍须在该日报告中核对。")}

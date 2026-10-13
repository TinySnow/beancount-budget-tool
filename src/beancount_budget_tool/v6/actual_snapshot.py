"""Source-compiled v6 actual snapshot for debt-free or simple CNY card dates."""

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
from ..yamlstrict import load_yaml
from .accounts import _lot
from .core import ActualInput, CashResource, Obligation, calculate_actual
from .obligations import audit_simple_obligations
from .payable_cash import audit_payable_cash


def audit_actual_snapshot(root: Path, cutover: date, as_of: date,
                          accounts: Path, opening: Path, ownership: Path,
                          obligations: Path, completeness: Path | None = None) -> dict:
    """Compile audited cash and simple CNY free-owner card debt into the core.

    A human statement covers unbooked responsibilities and in-flight payments.
    Every nonzero booked liability must match reviewed obligation IDs exactly.
    Foreign settlement and protected-purpose coverage need separate evidence.
    """
    cash = audit_payable_cash(root, cutover, as_of, accounts, opening, ownership)
    debt = audit_simple_obligations(root, cutover, obligations, as_of)
    fin = load_financial(root)
    require(cash["ledger_snapshot"] == debt["ledger_snapshot"] == fin.digest,
            "ACTUAL_SNAPSHOT_MISMATCH", "现金与责任证据不属于同一账本快照")
    liability_root = fin.options["name_liabilities"]
    balances: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date > as_of:
            continue
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] == liability_root:
                balances[(posting.account, posting.units.currency, _lot(posting))] += exact(posting.units.number)
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

    declaration_sha = None
    complete_statement = False
    if completeness is not None:
        document = load_yaml(completeness)
        required = {
            "schema", "cutover", "as_of",
            "unbooked_obligations", "inflight_payments", "reason"}
        require(type(document) is dict and required <= set(document) <=
                required | {"ledger_snapshot", "as_of_booked_fingerprint"},
            "ACTUAL_SCOPE_INVALID", "完整性声明字段无效")
        require(type(document["schema"]) is int and document["schema"] == 6 and
                type(document["cutover"]) is str and document["cutover"] == cutover.isoformat() and
                type(document["as_of"]) is str and document["as_of"] == as_of.isoformat() and
                document["unbooked_obligations"] == "none" and
                document["inflight_payments"] == "none" and
                type(document["reason"]) is str and bool(document["reason"].strip()),
                "ACTUAL_SCOPE_INVALID", "完整性声明时点或事实决定无效")
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
    if any(row["owner_id"] not in cash["spendable_owner_ids"] for row in booked_debts):
        reasons.append("DEBT_COVERAGE_PENDING")
    if not complete_statement:
        reasons.append("EXTERNAL_SCOPE_PENDING")
    actual = None
    protected_shortfall = None
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
                            resources=resources, obligations=obligations_input, coverage=(),
                            scope_complete=True,
                            scope_evidence=f"cash:{fin.digest};completeness:{declaration_sha}",
                            free_owner_ids=tuple(cash["spendable_owner_ids"]))
        report = calculate_actual(state)
        if report.capability.value is None:
            reasons.extend(report.capability.reasons)
        else:
            actual = exact_text(report.capability.value)
            protected_shortfall = exact_text(report.protected_shortfall)
    return {"mode": "v6_actual_simple_booked_debt_snapshot",
            "cutover": cutover.isoformat(), "as_of": as_of.isoformat(),
            "ledger_snapshot": fin.digest, "completeness_sha256": declaration_sha,
            "status": "CERTIFIED" if actual is not None else "NEEDS_REVIEW",
            "actual_spendable": actual,
            "protected_shortfall": protected_shortfall,
            "outside_budget_cash": cash["outside_budget_cash"],
            "report_currency": cash["report_currency"],
            "reason_codes": reasons,
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

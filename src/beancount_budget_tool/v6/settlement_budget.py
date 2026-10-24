"""Actual CNY settlement cost for a fully traced foreign card expense.

This is a budget-comparison basis, never an event-date expense quotation.
Only a complete, source-bound purchase -> card payment -> same-day FX bridge
may supply it. An unsettled or ambiguous expense remains unvalued.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from fractions import Fraction
from pathlib import Path

from ..exact import exact_text
from ..financial import load_financial
from .accounts import audit_accounts
from .classification import _key
from .h_fx import audit_h_fx
from .obligations import audit_simple_obligations
from .valuation import audit_classification_valuation


def audit_settlement_budget_values(root: Path, cutover: date, month: str,
                                   classifications: Path, report_currency: str,
                                   accounts: Path, ownership: Path,
                                   obligations: Path, h_fx: Path, *, through: date | None = None) -> dict:
    """Return only fully proved replacement values for missing expense quotes."""
    valuation = audit_classification_valuation(root, cutover, classifications, report_currency)
    expense_root = load_financial(root).options["name_expenses"]
    account_audit = audit_accounts(root, cutover, accounts)
    debt = audit_simple_obligations(root, cutover, obligations)
    fx = audit_h_fx(root, cutover, accounts, ownership, h_fx)
    if account_audit["report_currency"] != report_currency:
        return {"values": [], "reason_codes": ["REPORT_CURRENCY_MISMATCH"]}
    last_day = through or date(int(month[:4]), int(month[5:]), monthrange(int(month[:4]), int(month[5:]))[1])
    purchases = {}
    payments = {}
    for row in debt["events"]:
        if row["status"] != "CERTIFIED":
            continue
        if row["kind"] == "consumer_purchase":
            purchases.setdefault(row["obligation_id"], []).append(row)
        elif row["kind"] == "payment":
            payments.setdefault(row["obligation_id"], []).append(row)
    bridges = account_audit["same_day_settlements"]
    bridge_by_debit = {_key(row["debit_ref"]): row for row in bridges}
    fx_by_funding = {_key(row["to_ref"]): row for row in fx["events"]
                     if row["status"] == "CERTIFIED"}
    values = []
    used_debits = set()
    used_funding = set()
    for missing in valuation["missing_historical_value"]:
        if missing["month"] != month or missing["account"].split(":", 1)[0] != expense_root:
            continue
        candidates = [row for rows in purchases.values() for row in rows
                      if _key(row["counterpart_ref"]) == _key(missing["ref"])]
        if len(candidates) != 1:
            continue
        purchase = candidates[0]
        matches = payments.get(purchase["obligation_id"], [])
        if len(matches) != 1:
            continue
        payment = matches[0]
        bridge = bridge_by_debit.get(_key(payment["counterpart_ref"]))
        if bridge is None or _key(bridge["liability_ref"]) != _key(payment["liability_ref"]):
            continue
        funding = fx_by_funding.get(_key(bridge["funding_ref"]))
        if funding is None or funding["date"] != bridge["date"] or date.fromisoformat(funding["date"]) > last_day:
            continue
        native = Fraction(missing["signed_units"])
        units = Fraction(bridge["units"])
        if (native <= 0 or native != units or
                Fraction(payment["signed_units"]) != units or
                Fraction(purchase["signed_units"]) != -units or
                Fraction(funding["signed_to_units"]) != units or
                funding["to_commodity"] != missing["commodity"] or
                funding["from_commodity"] != report_currency or
                funding["to_account"] != bridge["asset_account"]):
            continue
        debit_key, funding_key = _key(bridge["debit_ref"]), _key(bridge["funding_ref"])
        if debit_key in used_debits or funding_key in used_funding:
            continue
        used_debits.add(debit_key)
        used_funding.add(funding_key)
        amount = -Fraction(funding["signed_from_units"])
        if amount <= 0 or amount != Fraction(funding["quoted_total"]):
            continue
        slices = [{"id": part["id"], "category_id": part["category_id"],
                   "signed_report_value": exact_text(amount * Fraction(part["signed_units"]) / native)}
                  for part in missing["native_slices"]]
        if sum((Fraction(part["signed_report_value"]) for part in slices), Fraction(0)) != amount:
            continue
        values.append({"ref": missing["ref"], "account": missing["account"],
                       "native_amount": exact_text(native), "native_commodity": missing["commodity"],
                       "budget_comparison_value": exact_text(amount),
                       "report_currency": report_currency,
                       "basis": "actual_card_settlement_cny_cost",
                       "settlement_date": funding["date"],
                       "obligation_id": purchase["obligation_id"],
                       "purchase_liability_ref": purchase["liability_ref"],
                       "payment_liability_ref": payment["liability_ref"],
                       "payment_asset_ref": payment["counterpart_ref"],
                       "fx_cny_ref": funding["from_ref"], "fx_foreign_ref": funding["to_ref"],
                       "historical_report_value": None, "slices": slices})
    return {"mode": "v6_settlement_budget_value_audit", "month": month,
            "ledger_snapshot": valuation["ledger_snapshot"],
            "report_currency": report_currency, "values": values,
            "note": "外币消费保持原币金额；人民币数仅为还款购汇时的实际预算比较成本，不是消费日汇率"}

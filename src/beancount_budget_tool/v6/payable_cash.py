"""Compile complete report-currency directly payable H cash by purpose."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path

from ..exact import exact_text
from ..yamlstrict import load_yaml
from .accounts import audit_accounts
from .holdings import audit_h_holdings


def audit_payable_cash(root: Path, cutover: date, as_of: date, accounts: Path,
                       opening: Path, ownership: Path) -> dict:
    """Sign a cash inventory total only when every payable H coordinate closes.

    This is a source-audited input to the v6 home-page calculation, not safe
    spendable cash: personal liabilities and exclusive coverage are separate.
    Excluded H positions may remain unknown without affecting this scope.
    """
    account_audit = audit_accounts(root, cutover, accounts)
    holdings = audit_h_holdings(root, cutover, as_of, accounts, opening, ownership)
    owner_document = load_yaml(ownership)
    defaults = owner_document.get("defaults")
    # The income default is a staging purpose. It becomes available for
    # spending only after an explicit allocation to the expense purpose.
    spendable_owner_ids = ("-",) if defaults is None else (defaults["expense_owner_id"],)
    currency = account_audit["report_currency"]
    payable_rows = []
    pending = []
    excluded = []
    owner_totals: dict[str, Fraction] = {}
    for row in holdings["positions"]:
        if row["payment_scope"] == "excluded":
            excluded.append({"account": row["account"], "commodity": row["commodity"],
                             "lot": row["lot"], "reason": "payment_scope_excluded"})
            continue
        if (row["payment_scope"] != "payable" or row["commodity"] != currency or
                row["status"] != "CERTIFIED"):
            pending.append({"account": row["account"], "commodity": row["commodity"],
                            "lot": row["lot"], "status": row["status"],
                            "reasons": row["reasons"],
                            "reason_codes": [reason["code"] for reason in row["reasons"]]
                            if row["status"] != "CERTIFIED" else ["PAYABLE_CURRENCY_UNSUPPORTED"]})
            continue
        payable_rows.append({"account": row["account"], "commodity": currency,
                             "lot": row["lot"], "owner_units": row["owner_units"]})
        for part in row["owner_units"]:
            owner = part["owner_id"]
            owner_totals[owner] = owner_totals.get(owner, Fraction(0)) + Fraction(part["units"])
    complete = account_audit["review_state"] == "complete" and not pending
    if complete:
        owner_units = [{"owner_id": owner, "units": exact_text(amount)}
                       for owner, amount in sorted(owner_totals.items()) if amount]
        free = sum((owner_totals.get(owner, Fraction(0))
                    for owner in spendable_owner_ids), Fraction(0))
        outside = (owner_totals.get("-", Fraction(0))
                   if defaults is not None else Fraction(0))
        total = sum(owner_totals.values(), Fraction(0))
    else:
        owner_units = free = outside = total = None
    return {"mode": "v6_payable_cash_inventory_audit",
            "cutover": cutover.isoformat(), "as_of": as_of.isoformat(),
            "report_currency": currency, "ledger_snapshot": holdings["ledger_snapshot"],
            "review_state": "complete" if complete else "pending",
            "payable_coordinates_certified": len(payable_rows),
            "payable_coordinates_pending": len(pending),
            "known_payable_positions": payable_rows,
            "pending_payable_positions": pending,
            "excluded_h_coordinates": excluded,
            "owner_units": owner_units,
            "spendable_owner_ids": list(spendable_owner_ids),
            "free_cash": exact_text(free) if complete else None,
            "outside_budget_cash": exact_text(outside) if complete else None,
            "total_payable_cash": exact_text(total) if complete else None,
            "note": "仅为完整可直接支付范围的原币 H 用途库存；双默认模式仅默认支出用途可用于消费，默认收入用途须先显式划拨，'-' 单列预算外；未减债务、未证清算与覆盖，不是实际可花"}

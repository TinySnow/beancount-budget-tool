"""Invert reviewed account positions into a purpose-by-account report.

Native H quantities, securities pool book costs and K subscription changes
retain their own totals. No exchange rate or liquidation value is inferred.
"""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction

from ..errors import require
from ..exact import exact_text


def summarize_purpose_balances(account_purposes: dict, pools: dict, k_book: dict,
                               owner_document: dict, owner_source: str | None = None) -> dict:
    """Include every registered purpose, even if it currently has no positions.

    Missing attribution prevents totals for the whole view: omitted holdings
    must never look like a zero balance for an otherwise empty purpose.
    """
    reasons = []
    if account_purposes["status"] != "CERTIFIED_H_OUTSIDE_POOLS":
        reasons.append("H_HOLDINGS_PENDING")
    if k_book["status"] != "CERTIFIED":
        reasons.append("K_BOOK_PURPOSE_PENDING")
    if any(pool["status"] != "CERTIFIED_NOMINAL_BOOK_COVERAGE"
           for pool in pools["pools"]):
        reasons.append("PURPOSE_POOL_PENDING")
    if reasons:
        return {"status": "NEEDS_REVIEW", "reason_codes": reasons,
                "purposes": None, "outside_budget": None}

    owners = {row["id"]: row["name"] for row in owner_document["owners"]}
    defaults = owner_document.get("defaults", {})
    h = defaultdict(lambda: defaultdict(Fraction))
    pool_cost = defaultdict(lambda: defaultdict(Fraction))
    k = defaultdict(lambda: defaultdict(Fraction))
    for row in account_purposes["rows"]:
        h[(row["owner_id"], row["commodity"])][row["account"]] += Fraction(row["units"])
    for pool in pools["pools"]:
        for row in pool["account_purposes"]:
            for owner_key, label_key, amount_key in (
                ("protected_owner_id", "protected_label", "protected_nominal"),
                ("remainder_owner_id", "remainder_label", "remainder_book_cost"),
            ):
                key = (pool[owner_key], pools["report_currency"])
                position = (row["account"], pool["id"], pool[label_key])
                pool_cost[key][position] += Fraction(row[amount_key])
    for row in k_book["rows"]:
        for part in row["purpose_changes"]:
            k[(part["owner_id"], row["commodity"])][row["account"]] += Fraction(part["signed_change"])

    used_owners = {owner for owner, _ in set(h) | set(pool_cost) | set(k)}
    unknown = used_owners - set(owners) - {"-"}
    require(not unknown, "MONTH_REPORT_MISMATCH",
            f"用途余额引用未登记用途：{', '.join(sorted(unknown))}", owner_source)

    def group(owner: str, balances: dict, value_key: str, is_pool: bool = False) -> list:
        result = []
        for (ident, commodity), positions in sorted(balances.items()):
            if ident != owner:
                continue
            accounts = []
            for coordinate, amount in sorted(positions.items()):
                if not amount:
                    continue
                item = {"account": coordinate[0] if is_pool else coordinate,
                        value_key: exact_text(amount)}
                if is_pool:
                    item.update(pool_id=coordinate[1], label=coordinate[2])
                accounts.append(item)
            if accounts:
                result.append({"commodity": commodity,
                               value_key: exact_text(sum(positions.values(), Fraction(0))),
                               "accounts": accounts})
        return result

    def purpose(owner: str, name: str) -> dict:
        roles = [role for field, role in (("income_owner_id", "income_default"),
                                         ("expense_owner_id", "expense_default"))
                 if defaults.get(field) == owner]
        native = group(owner, h, "units")
        return {"owner_id": owner, "name": name, "default_roles": roles,
                "h_native_balances": native,
                "pool_book_cost_balances": group(owner, pool_cost, "units", True),
                "k_book_purpose_changes": group(owner, k, "signed_change"),
                "has_negative_h_position": any(
                    Fraction(account["units"]) < 0
                    for currency in native for account in currency["accounts"])}

    return {"status": "CERTIFIED", "reason_codes": [],
            "purposes": [purpose(owner, name) for owner, name in owners.items()],
            "outside_budget": purpose("-", "预算外") if "-" in used_owners else None,
            "note": "H 按原币数量合计；证券池按账面成本单列；K 仅为 cutover 后已审申赎用途净变动。各口径不合并，余额不代表实际可花。"}

"""Read-only projections for purpose attribution, cash, expense and purposes.

Every function consumes an already-audited report or audit fact and copies its
evidence.  No function recalculates cash, expense, K, pool or budget values.
"""

from __future__ import annotations

from copy import deepcopy

from ..errors import BudgetError, require
from .classification import _key


def attribution_rows(ownership: dict, review_items=()) -> list[dict]:
    """Project every H-leg evidence row, merging matching review context."""
    require(isinstance(ownership, dict), "PURPOSE_EXPLAIN_AUDIT_INVALID",
            "归属证据必须是已审计结果", str(type(ownership)))
    reviews = {}
    for item in review_items or ():
        if isinstance(item, dict) and isinstance(item.get("ref"), dict):
            reviews.setdefault(_key(item["ref"]), []).append(item)
    rows = []
    for raw in ownership.get("purpose_evidence", []) or ():
        item = deepcopy(raw)
        matched = reviews.get(_key(item["ref"]), []) if isinstance(item.get("ref"), dict) else []
        if matched:
            item["review_items"] = deepcopy(matched)
            item["review"] = deepcopy(matched[0])
            item["review_source"] = matched[0].get("source")
            item["reason_codes"] = sorted({row.get("reason_code") for row in matched
                                           if row.get("reason_code")})
            if not item.get("source"):
                item["source"] = matched[0].get("source")
            if not item.get("reason_code") and item["reason_codes"]:
                item["reason_code"] = item["reason_codes"][0]
            if not item.get("review_reason"):
                item["review_reason"] = matched[0].get("reason")
        rows.append(item)
    return rows


def _owner(report: dict, owner) -> tuple[str | None, str | None]:
    if owner is None:
        return None, None
    names = report.get("owner_names") or {}
    if owner in names:
        return owner, names[owner]
    for ident, name in names.items():
        if name == owner:
            return ident, name
    raise BudgetError("PURPOSE_EXPLAIN_OWNER_UNKNOWN", "用途未在已审报告中登记", str(owner))


def _filter_positions(rows, owner_id):
    result = []
    for raw in rows or ():
        row = deepcopy(raw)
        if owner_id is not None:
            row["owner_units"] = [part for part in row.get("owner_units", [])
                                  if part.get("owner_id") == owner_id]
            if not row["owner_units"]:
                continue
        result.append(row)
    return result


def _default_owner_or_none(report: dict, cash_inventory: dict):
    outflows = report.get("default_outflows") or {}
    owner = outflows.get("owner_id")
    if isinstance(owner, str) and owner:
        return owner
    candidates = list(dict.fromkeys(cash_inventory.get("spendable_owner_ids") or []))
    return candidates[0] if len(candidates) == 1 else None


def _default_owner(report: dict, cash_inventory: dict) -> str:
    owner = _default_owner_or_none(report, cash_inventory)
    if owner is None:
        raise BudgetError("PURPOSE_EXPLAIN_OWNER_AMBIGUOUS",
                          "未指定用途时无法确定默认支出用途",
                          str(cash_inventory.get("spendable_owner_ids") or []))
    return owner


def explain_cash(report: dict, cash_inventory: dict, owner_id=None) -> dict:
    """Explain audited payable cash; never call inventory maximum spendable."""
    require(isinstance(report, dict) and isinstance(cash_inventory, dict),
            "PURPOSE_EXPLAIN_REPORT_INVALID", "现金解释需要已审报告和 payable inventory")
    default_id = _default_owner_or_none(report, cash_inventory)
    selected = _default_owner(report, cash_inventory) if owner_id is None else owner_id
    ident, name = _owner(report, selected)
    if ident is None:
        raise BudgetError("PURPOSE_EXPLAIN_OWNER_UNKNOWN", "未指定用途时无法解析默认用途", str(selected))
    actual = deepcopy(report.get("actual_spendable") or {})
    available = deepcopy(actual.get("purpose_available") or [])
    selected_parts = [part for part in available if part.get("owner_id") == ident]
    is_default = ident == default_id
    if is_default:
        selected_available = actual.get("actual_spendable")
    elif actual.get("status") == "CERTIFIED":
        # A registered, non-default purpose absent from the payable detail has
        # audited zero payable cash in scope. This does not value K or pool.
        selected_available = selected_parts[0].get("units") if selected_parts else "0"
    else:
        selected_available = None
    other = [part for part in available if part.get("owner_id") != ident]
    inventory = {
        "known_payable_positions": _filter_positions(
            cash_inventory.get("known_payable_positions"), ident),
        "pending_payable_positions": deepcopy(cash_inventory.get("pending_payable_positions") or []),
        "excluded_h_coordinates": deepcopy(cash_inventory.get("excluded_h_coordinates") or []),
        "owner_units": [part for part in deepcopy(cash_inventory.get("owner_units") or [])
                        if part.get("owner_id") == ident],
        "spendable_owner_ids": deepcopy(cash_inventory.get("spendable_owner_ids") or []),
        "free_cash": cash_inventory.get("free_cash"),
        "outside_budget_cash": cash_inventory.get("outside_budget_cash"),
        "total_payable_cash": cash_inventory.get("total_payable_cash"),
    }
    return {
        "selected_owner": {"owner_id": ident, "name": name, "is_default": is_default},
        "selected_available_cash": selected_available,
        "other_purposes": other,
        "report_currency": report.get("report_currency") or cash_inventory.get("report_currency"),
        "inventory": inventory,
        "global_actual_spendable": actual,
        "note": "selected_available_cash 是所选用途的已审值，默认用途取 actual_spendable，其他用途取 purpose_available。global_actual_spendable 只是默认/全局责任上下文，不是其他用途可支付额；库存不是最多能花。",
    }

def _category_scope(report: dict, category):
    categories = deepcopy((report.get("expense_targets") or {}).get("categories") or [])
    if category is None:
        return None, None, categories, {row["id"] for row in categories}
    selected = next((row for row in categories if row.get("id") == category), None)
    if selected is None:
        selected = next((row for row in categories if row.get("name") == category), None)
    if selected is None:
        raise BudgetError("PURPOSE_EXPLAIN_CATEGORY_UNKNOWN", "消费类别未在已审报告中登记", str(category))
    children = {}
    for row in categories:
        children.setdefault(row.get("parent"), []).append(row["id"])
    scope = set()
    stack = [selected["id"]]
    while stack:
        ident = stack.pop()
        if ident in scope:
            continue
        scope.add(ident)
        stack.extend(children.get(ident, []))
    return selected["id"], selected.get("name"), categories, scope


def explain_expense(report: dict, category_id=None, expense_root="Expenses") -> dict:
    """Explain existing expense targets/native rows; filter real Expenses legs."""
    require(isinstance(report, dict), "PURPOSE_EXPLAIN_REPORT_INVALID",
            "支出解释需要已审报告")
    ident, name, categories, scope = _category_scope(report, category_id)
    native = [deepcopy(row) for row in
              ((report.get("multicurrency") or {}).get("native_expenses") or [])
              if row.get("category_id") in scope]
    rows = []
    for raw in ((report.get("transactions") or {}).get("rows") or []):
        row = deepcopy(raw)
        postings = []
        for posting in row.get("postings") or []:
            account = posting.get("account", "")
            if account.split(":", 1)[0] != expense_root:
                continue
            slices = (posting.get("classification") or {}).get("slices") or []
            matched = [part for part in slices if part.get("category_id") in scope]
            if ident is not None and not matched:
                continue
            posting["selected_slices"] = deepcopy(matched)
            postings.append(posting)
        if postings:
            row["postings"] = postings
            rows.append(row)
    return {
        "category_id": ident,
        "category_name": name,
        "categories": categories,
        "scope_category_ids": sorted(scope),
        "native_expenses": native,
        "transactions": rows,
        "expense_root": expense_root,
        "note": "只从已审 expense_targets、multicurrency.native_expenses 和交易详情投影；不混入同名的 Income 科目，也不重算净消费。",
    }


def explain_purpose(report: dict, owner_id) -> dict:
    """Explain one purpose's existing H/K/pool balances without adding them."""
    require(isinstance(report, dict), "PURPOSE_EXPLAIN_REPORT_INVALID",
            "用途解释需要已审报告")
    ident, name = _owner(report, owner_id)
    balances = (report.get("purpose_balances") or {}).get("purposes") or []
    purpose = next((row for row in balances if row.get("owner_id") == ident), None)
    if purpose is None:
        raise BudgetError("PURPOSE_EXPLAIN_OWNER_UNKNOWN", "用途未在已审余额中登记", str(owner_id))
    progress = next((row for row in
                     ((report.get("purpose_progress") or {}).get("owners") or [])
                     if row.get("owner_id") == ident), None)
    return {
        "owner_id": ident,
        "owner_name": name,
        "purpose": deepcopy(purpose),
        "purpose_progress": deepcopy(progress) if progress is not None else None,
        "note": "H 库存、K 账面变动和证券池成本分列，不相互加总，也不代表现金或市值。",
    }

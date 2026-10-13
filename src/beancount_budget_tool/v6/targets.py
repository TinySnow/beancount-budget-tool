"""Strict monthly consumption targets, separate from cash replenishment plans.

Targets are explicit intentions. They never create cash, income, or a future
inflow in the v6 actual/plan scenario engine.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re

from ..errors import require
from ..exact import exact_text
from ..yamlstrict import load_yaml, require_quoted_month_keys
from .classification import _registry
from .spending import _audit_monthly_spending
from .settlement_budget import audit_settlement_budget_values


MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")
DECIMAL = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")
CURRENCY = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def _month(value: object) -> str:
    require(type(value) is str and bool(MONTH.fullmatch(value)),
            "PLAN_AMBIGUOUS", "月份必须是带引号的 YYYY-MM 字符串", str(value))
    try:
        date.fromisoformat(value + "-01")
    except ValueError:
        require(False, "PLAN_AMBIGUOUS", "月份必须是有效的 YYYY-MM", value)
    return value


def _fields(value: object, allowed: set[str], source: str) -> dict:
    require(type(value) is dict and set(value) <= allowed,
            "PLAN_INVALID", "计划字段无效或含未知字段", source)
    return value


def _target_layer(value: object, ids: set[str], source: str) -> dict[str, dict[str, Fraction]]:
    row = _fields(value, {"expense_targets", "expense_subtree_targets"}, source)
    parsed = {}
    for field in ("expense_targets", "expense_subtree_targets"):
        targets = row.get(field, {})
        require(type(targets) is dict, "PLAN_INVALID", f"{field} 必须是 ID 到金额的映射", source)
        result = {}
        for ident, raw in targets.items():
            require(type(ident) is str and ident in ids, "PLAN_CATEGORY_UNKNOWN",
                    "计划引用未注册消费类别", str(ident))
            require(type(raw) is str and bool(DECIMAL.fullmatch(raw)),
                    "PLAN_AMOUNT_INVALID", "目标须是非负十进制字符串", f"{source}/{field}/{ident}")
            result[ident] = Fraction(raw)
        parsed[field] = result
    return parsed


def load_expense_targets(path: Path, month: str, report_currency: str,
                         categories: list[dict]) -> dict:
    """Expand defaults, effective templates and one-month overrides by ID."""
    _month(month)
    require(type(report_currency) is str and bool(CURRENCY.fullmatch(report_currency)),
            "REPORT_CURRENCY_INVALID", "报告货币无效")
    _registry(categories)
    ids = {row["id"] for row in categories}
    require_quoted_month_keys(path)
    document = _fields(load_yaml(path),
                       {"schema", "report_currency", "defaults", "effective_templates", "months"},
                       str(path))
    require(type(document.get("schema")) is int and document["schema"] == 6,
            "SCHEMA_UNSUPPORTED", "消费目标文件 schema 必须是 6", str(path))
    require(document.get("report_currency") == report_currency,
            "REPORT_CURRENCY_INVALID", "消费目标货币须与报告货币一致", str(path))
    defaults = _target_layer(document.get("defaults", {}), ids, "defaults")
    def parse_layers(field: str) -> dict[str, dict[str, dict[str, Fraction]]]:
        values = document.get(field, {})
        require(type(values) is dict, "PLAN_INVALID", f"{field} 必须是月份映射", str(path))
        return {_month(key): _target_layer(value, ids, f"{field}/{key}")
                for key, value in values.items()}
    effective = parse_layers("effective_templates")
    overrides = parse_layers("months")
    targets = {field: dict(defaults[field]) for field in defaults}
    for start in sorted(effective):
        if start <= month:
            for field in targets:
                targets[field].update(effective[start][field])
    override = overrides.get(month)
    if override is not None:
        for field in targets:
            targets[field].update(override[field])
    return {"month": month, "report_currency": report_currency,
            "plan_sha256": sha256(path.read_bytes()).hexdigest(),
            "targets": {ident: exact_text(value) for ident, value in targets["expense_targets"].items()},
            "subtree_targets": {ident: exact_text(value) for ident, value in targets["expense_subtree_targets"].items()}}


def audit_monthly_expense_targets(root: Path, cutover: date, month: str,
                                  classifications: Path, plans: Path,
                                  report_currency: str,
                                  benefit_groups: Path | None = None,
                                  accounts: Path | None = None,
                                  ownership: Path | None = None,
                                  obligations: Path | None = None,
                                  h_fx: Path | None = None) -> dict:
    """Compare certified direct/subtree expense with targets, per category.

    A category with a booked expense slice is active even if refunds make its
    monthly net zero. Missing targets are never silently treated as zero.
    """
    _month(month)
    settlement_paths = (accounts, ownership, obligations, h_fx)
    require(not any(settlement_paths) or all(settlement_paths), "BUDGET_BASIS_INVALID",
            "按实际还款成本比较预算须同时提供账户、归属、责任和购汇审阅来源")
    settlement = (audit_settlement_budget_values(root, cutover, month, classifications,
                                                 report_currency, accounts, ownership,
                                                 obligations, h_fx)
                  if all(settlement_paths) else None)
    spending = _audit_monthly_spending(root, cutover, month, classifications,
                                       report_currency, benefit_groups,
                                       settlement["values"] if settlement else None)
    category_doc = load_yaml(classifications)
    expanded = load_expense_targets(plans, month, report_currency,
                                    category_doc["categories"])
    targets = {key: Fraction(value) for key, value in expanded["targets"].items()}
    subtree_targets = {key: Fraction(value) for key, value in expanded["subtree_targets"].items()}
    expense = spending["metrics"]["net_expense"]
    rows = expense["categories"]
    parents = {row["id"]: row["parent"] for row in rows}
    children = {ident: [] for ident in parents}
    for ident, parent in parents.items():
        if parent is not None:
            children[parent].append(ident)
    def pool_ancestor(ident: str) -> str | None:
        current = ident
        while current is not None:
            if current in subtree_targets:
                return current
            current = parents[current]
        return None

    missing = {row["id"] for row in rows
               if row["active_direct"] and row["id"] not in targets and
               pool_ancestor(row["id"]) is None}
    # An unclassified expense may belong to any category. Its target comparison
    # cannot be certified merely because the visible classified rows have plans.
    unknown_category = expense["missing_classifications"] > 0

    descendants: dict[str, set[str]] = {}
    def subtree(ident: str) -> set[str]:
        if ident not in descendants:
            descendants[ident] = {ident}
            for child in children[ident]:
                descendants[ident].update(subtree(child))
        return descendants[ident]

    effective_targets: dict[str, Fraction] = {}
    def effective_target(ident: str) -> Fraction:
        if ident not in effective_targets:
            effective_targets[ident] = (subtree_targets[ident] if ident in subtree_targets else
                                        targets.get(ident, Fraction(0)) +
                                        sum((effective_target(child) for child in children[ident]), Fraction(0)))
        return effective_targets[ident]

    details = []
    for row in rows:
        ident = row["id"]
        direct_target = targets.get(ident)
        direct_missing = ident in missing or unknown_category
        direct_applicable = direct_target is not None or ident in missing
        direct_ok = direct_applicable and direct_target is not None and not direct_missing and row["direct_status"] == "CERTIFIED"
        members = subtree(ident)
        subtree_missing = bool(members & missing) or unknown_category
        subtree_applicable = (ident in subtree_targets or
                              any(member in targets or member in missing or
                                  member in subtree_targets for member in members))
        subtree_ok = subtree_applicable and not subtree_missing and row["subtree_status"] == "CERTIFIED"
        # An explicit subtree cap covers its own direct expense and every
        # descendant, without adding independent child caps a second time.
        subtree_target = effective_target(ident)
        details.append({"id": ident, "name": row["name"], "parent": row["parent"],
                        "active_direct": row["active_direct"],
                        "direct_target": exact_text(direct_target) if direct_target is not None else None,
                        "direct_spent": row["direct"],
                        "direct_gross_spent": row["direct_gross_spent"],
                        "direct_refunded": row["direct_refunded"],
                        "direct_remaining_status": "CERTIFIED" if direct_ok else
                                                   "NEEDS_REVIEW" if direct_applicable or unknown_category else "NOT_APPLICABLE",
                        "direct_remaining": exact_text(direct_target - Fraction(row["direct"])) if direct_ok else None,
                        "subtree_target": exact_text(subtree_target) if subtree_applicable and
                                          (ident in subtree_targets or not subtree_missing) else None,
                        "subtree_target_kind": ("explicit" if ident in subtree_targets else
                                                "derived" if subtree_applicable else None),
                        "subtree_spent": row["subtree"],
                        "subtree_gross_spent": row["subtree_gross_spent"],
                        "subtree_refunded": row["subtree_refunded"],
                        "subtree_remaining_status": "CERTIFIED" if subtree_ok else
                                                    "NEEDS_REVIEW" if subtree_applicable or unknown_category else "NOT_APPLICABLE",
                        "subtree_remaining": exact_text(subtree_target - Fraction(row["subtree"])) if subtree_ok else None})
    reasons = list(expense["reason_codes"])
    if missing:
        reasons.append("PLAN_MISSING")
    return {"mode": "v6_monthly_expense_target_report", "month": month,
            "cutover": cutover.isoformat(), "ledger_snapshot": spending["ledger_snapshot"],
            "plan_sha256": expanded["plan_sha256"], "report_currency": report_currency,
            "status": "NEEDS_REVIEW" if reasons else "CERTIFIED", "reason_codes": reasons,
            "missing_target_ids": sorted(missing), "categories": details,
            "comparison_basis": "actual_settlement_cost_where_proved" if settlement else
                                "historical_report_value",
            "foreign_settlement_costs": settlement["values"] if settlement else [],
            "note": ("外币消费保留原币；已核实的人民币还款成本只用于预算目标差额，"
                     "不是消费日价格，也不代表现金、用途净划拨或未来生活费补给"
                     if settlement else
                     "消费目标差额不代表现金、用途净划拨或未来生活费补给")}

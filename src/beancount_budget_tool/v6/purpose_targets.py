"""Explicit monthly purpose allocation targets, independent of actual flows."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re

from ..errors import BudgetError, require
from ..exact import exact_text
from ..yamlstrict import load_yaml, require_quoted_month_keys, yaml_key_location, yaml_path_location


MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")
AMOUNT = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")
CURRENCY = re.compile(r"[A-Z][A-Z0-9_]*\Z")
NOT_ASSESSED = "not_assessed"


def _month(value: object) -> str:
    require(type(value) is str and bool(MONTH.fullmatch(value)),
            "PLAN_AMBIGUOUS", "月份必须是带引号的 YYYY-MM 字符串", str(value))
    try:
        date.fromisoformat(value + "-01")
    except ValueError:
        require(False, "PLAN_AMBIGUOUS", "月份必须是有效 YYYY-MM", value)
    return value


def load_purpose_targets(path: Path, month: str, report_currency: str,
                         owner_ids: set[str]) -> dict:
    """Expand Q intentions without inferring a target from an actual transfer.

    An explicit ``not_assessed`` differs from a missing owner and from a
    numeric zero. This function never claims an actual G amount or remaining
    target; those require separately certified event coverage.
    """
    _month(month)
    require(type(report_currency) is str and bool(CURRENCY.fullmatch(report_currency)),
            "REPORT_CURRENCY_INVALID", "报告货币无效")
    require(type(owner_ids) is set and all(type(ident) is str and ident and ident != "-"
                                           for ident in owner_ids),
            "OWNER_REGISTRY_INVALID", "用途 ID 名册无效")
    require_quoted_month_keys(path)
    document = load_yaml(path)
    require(type(document) is dict and set(document) <= {
        "schema", "report_currency", "defaults", "effective_templates", "months"},
        "PLAN_INVALID", "用途目标文件字段无效", f"{path.resolve()}:1")
    require(type(document.get("schema")) is int and document["schema"] == 6,
            "SCHEMA_UNSUPPORTED", "用途目标文件 schema 必须是 6", yaml_key_location(path, "schema"))
    require(document.get("report_currency") == report_currency,
            "REPORT_CURRENCY_INVALID", "用途目标货币须与报告货币一致", yaml_key_location(path, "report_currency"))

    def layer(value: object, keys: tuple[str, ...]) -> dict[str, Fraction | str]:
        require(type(value) is dict and set(value) <= {"allocation_targets"},
                "PLAN_INVALID", "用途目标层字段无效", yaml_path_location(path, keys))
        raw = value.get("allocation_targets", {})
        require(type(raw) is dict, "PLAN_INVALID", "allocation_targets 必须是用途 ID 映射",
                yaml_path_location(path, (*keys, "allocation_targets")))
        parsed = {}
        for ident, amount in raw.items():
            require(type(ident) is str and ident in owner_ids,
                    "PLAN_OWNER_UNKNOWN", "目标引用未登记用途",
                    yaml_path_location(path, (*keys, "allocation_targets", str(ident))))
            require(type(amount) is str and
                    (amount == NOT_ASSESSED or bool(AMOUNT.fullmatch(amount))),
                    "PLAN_AMOUNT_INVALID", "用途目标须是非负十进制字符串或 not_assessed",
                    yaml_path_location(path, (*keys, "allocation_targets", ident)))
            parsed[ident] = amount if amount == NOT_ASSESSED else Fraction(amount)
        return parsed

    def dated_layers(field: str) -> dict[str, dict[str, Fraction | str]]:
        values = document.get(field, {})
        require(type(values) is dict, "PLAN_INVALID", f"{field} 必须是月份映射", yaml_key_location(path, field))
        result = {}
        for key, value in values.items():
            try:
                month_key = _month(key)
            except BudgetError as exc:
                raise BudgetError(exc.code, exc.message, yaml_path_location(path, (field, str(key)))) from exc
            result[month_key] = layer(value, (field, month_key))
        return result

    targets = layer(document.get("defaults", {}), ("defaults",))
    effective = dated_layers("effective_templates")
    overrides = dated_layers("months")
    for start in sorted(effective):
        if start <= month:
            targets.update(effective[start])
    targets.update(overrides.get(month, {}))
    rows = []
    for ident in sorted(owner_ids):
        value = targets.get(ident)
        status = ("MISSING" if value is None else
                  "NOT_ASSESSED" if value == NOT_ASSESSED else "TARGET")
        rows.append({"owner_id": ident, "status": status,
                     "target": exact_text(value) if isinstance(value, Fraction) else None})
    return {"mode": "v6_purpose_target_declarations_only",
            "month": month, "report_currency": report_currency,
            "plan_sha256": sha256(path.read_bytes()).hexdigest(),
            "owners": rows, "missing_target_ids": [row["owner_id"] for row in rows
                                               if row["status"] == "MISSING"],
            "note": "仅展开用途月目标；未编译实际净划拨 G，不把缺失目标或不考核目标当作零"}

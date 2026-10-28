"""Persistent payday targets, independent of calendar-month bookkeeping."""

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

import yaml

from ..errors import BudgetError, require
from ..exact import exact_text
from ..yamlstrict import load_yaml, require_yaml, yaml_path_location


def month_end(month):
    year, number = map(int, month.split("-"))
    return date(year, number, monthrange(year, number)[1])


def read_cycles(plans, source, *, root_key="salary_cycles", default_category=None):
    rows = plans.get(root_key, {})
    require_yaml(type(rows) is dict, "SALARY_PLAN_INVALID", "salary_cycles 须为实际发薪日期到目标的映射", source, (root_key,))
    result = {}
    tree = None
    from .classification import _name
    from .salary_cycle import cycle_bounds
    for start, row in rows.items():
        keys = (root_key, str(start))
        try:
            cycle_bounds(start, (date.fromisoformat(start) + timedelta(days=1)).isoformat())
        except (BudgetError, ValueError, TypeError) as exc:
            raise BudgetError("SALARY_PLAN_INVALID", "发薪日须为加引号的 YYYY-MM-DD", yaml_path_location(source, keys)) from exc
        scalar = type(row) is not dict
        if scalar:
            require_yaml(default_category is not None, "SALARY_PLAN_INVALID",
                         "简写周期额度需要 config.yml 中的消费上限类别", source, keys)
            row = {"expense_targets": {default_category: row}}
        require_yaml(type(row) is dict and set(row) == {"expense_targets"} and type(row["expense_targets"]) is dict,
                     "SALARY_PLAN_INVALID", "每个发薪日须有 expense_targets；下一条发薪日自动结束上一周期", source, keys)
        targets = {}
        for name, value in row["expense_targets"].items():
            location = keys if scalar else (*keys, "expense_targets", name)
            try:
                require(type(name) is str and _name(name) == name, "SALARY_PLAN_INVALID", "消费类别名称无效")
            except BudgetError as exc:
                raise BudgetError(exc.code, exc.message, yaml_path_location(source, location)) from exc
            if value is None or value == "not_assessed":
                targets[name] = value
                continue
            if type(value) is float:
                tree = tree or yaml.compose(source.read_text(encoding="utf-8"))
                node = tree
                for part in location:
                    node = next(v for k, v in node.value if k.value == part)
                value = node.value.replace("_", "")
            try:
                require(type(value) in (int, str), "SALARY_PLAN_INVALID", "周期目标须为非负金额、null 或 not_assessed")
                amount = Decimal(value)
                require(amount.is_finite() and amount >= 0, "SALARY_PLAN_INVALID", "周期目标须为有限非负金额")
                targets[name] = format(amount, "f")
            except (InvalidOperation, ValueError, BudgetError) as exc:
                raise BudgetError("SALARY_PLAN_INVALID", "周期目标须为非负金额、null 或 not_assessed", yaml_path_location(source, location)) from exc
        result[start] = {"expense_targets": targets}
    return dict(sorted(result.items()))


def window(cycles, as_of, start=None):
    starts = list(sorted(cycles))
    if start is None:
        candidates = [key for key in starts if key <= as_of.isoformat()]
        if not candidates:
            return None
        start = candidates[-1]
    require(start in cycles, "SALARY_CYCLE_UNKNOWN", "该发薪日尚未在 budgets.yml 登记", start)
    following = next((key for key in starts if key > start), None)
    last = min(as_of, date.fromisoformat(following) - timedelta(days=1)) if following else as_of
    require(date.fromisoformat(start) <= last, "SALARY_CYCLE_RANGE_INVALID", "查询截止日早于该发薪日")
    return start, following, last


def _location(plan, path, start=None, name=None):
    source = Path(plan.get("source", str(path)))
    if source.is_file():
        keys = ("salary_cycles",) + ((start,) if start else ()) + (("expense_targets", name) if name else ())
        return yaml_path_location(source, keys)
    return yaml_path_location(path, ("cycles",))


def audit_plan(config, path, as_of, *, start=None):
    """Audit consumption through as_of; never consume a future payday's facts."""
    plan = load_yaml(path)
    require(type(plan) is dict and set(plan) == {"schema", "cycles", "category", "source"} and type(plan["schema"]) is int and plan["schema"] == 1,
            "SALARY_PLAN_INVALID", "工资周期侧文件格式无效", str(path))
    for field in ("category", "source"):
        require_yaml(type(plan[field]) is str and bool(plan[field].strip()), "SALARY_PLAN_INVALID", "周期类别和来源须为非空字符串", path, (field,))
    cycles = read_cycles(plan, path, root_key="cycles")
    selected = window(cycles, as_of, start)
    result = {"mode": "v6_salary_cycle_spending", "from": selected[0] if selected else as_of.isoformat(),
              "through": selected[2].isoformat() if selected else as_of.isoformat(),
              "next_payday": selected[1] if selected else None, "configured_cycle": True,
              "status": "NEEDS_REVIEW", "report": None, "review_items": []}
    if selected is None:
        if cycles:
            # The first explicitly configured payday is the start of this
            # target policy. Earlier bookkeeping can be reviewed, but there
            # is no applicable cycle or numeric cap to invent for it.
            result.update({"status": "NOT_STARTED", "first_payday": min(cycles), "from": None})
            return result
        result["review_items"] = [{"reason_code": "SALARY_CYCLE_MISSING", "reason_kind": "data_review",
            "detail": "截至本月末尚无已登记发薪周期；在 budgets.yml 的 salary_cycles 填写真实发薪日和目标。",
            "source": _location(plan, path)}]
        return result
    first, following, last = selected
    if first < load_yaml(config)["cutover"]:
        result["review_items"] = [{"reason_code": "SALARY_CYCLE_BEFORE_CUTOVER", "reason_kind": "data_review",
            "detail": "该发薪周期始于 cutover 之前，不能把缺失的周期消费当零；请补齐更早账务范围后重审。",
            "source": _location(plan, path, first)}]
        return result
    from .salary_cycle import audit_salary_cycle
    # The old auditor's exclusive endpoint is an audit cutoff here, not an
    # assertion that salary was received on the following day.
    audited = audit_salary_cycle(config, first, (last + timedelta(days=1)).isoformat(), category=plan["category"])
    result.update(audited, configured_cycle=True, next_payday=following)
    if not audited["report"]:
        return result
    report = result["report"]
    categories = {row["name"]: row for row in report["categories"]}
    document = load_yaml(config)
    registry = load_yaml((config.parent / document["classifications"]).resolve())
    aliases = {name: row["name"] for row in registry["categories"] for name in [row["id"], row["name"], *row.get("aliases", [])]}
    cap_name = aliases.get(plan["category"], plan["category"])
    targets, declared_names = {}, {}
    for name, target in cycles[first]["expense_targets"].items():
        canonical = aliases.get(name, name)
        require(canonical not in targets, "SALARY_CYCLE_TARGET_DUPLICATE", "同一周期不能用名称及别名重复设置目标", _location(plan, path, first, name))
        targets[canonical], declared_names[canonical] = target, name
    problems, rows = [], []
    for name in set(targets) | {cap_name}:
        target = targets.get(name)
        location = _location(plan, path, first, declared_names.get(name, name))
        require(name in categories, "SALARY_CYCLE_CATEGORY_INVALID", "周期目标类别不在已登记名册中", location)
        category = categories[name]
        state = "NOT_ASSESSED" if target == "not_assessed" else "NEEDS_REVIEW" if target is None else "CERTIFIED"
        rows.append({"category_id": category["id"], "category_name": name, "status": state,
            "target": target if state == "CERTIFIED" else None,
            "gross_spent": category["subtree_gross_spent"], "refunded": category["subtree_refunded"],
            "net_spent": category["subtree"],
            "remaining": exact_text(Fraction(target) - Fraction(category["subtree"])) if state == "CERTIFIED" else None})
        if state == "NEEDS_REVIEW":
            problems.append({"reason_code": "SALARY_CYCLE_TARGET_MISSING", "reason_kind": "data_review",
                "detail": f"发薪周期 {first} 的「{name}」缺少额度；填写金额或明确 not_assessed。", "source": location})
    rows.sort(key=lambda row: row["category_name"])
    report["cycle_targets"] = rows
    report["cycle_budget"] = next(row for row in rows if row["category_name"] == cap_name)
    report["warnings"] = [f"{row['category_name']}：周期消费超过额度。" for row in rows
                          if row["remaining"] is not None and Fraction(row["remaining"]) < 0]
    report["note"] = "额度来自 budgets.yml 的实际发薪周期；含本次发薪日，不含下一发薪日。仅统计至查询截止日，末周期不假定未来发薪日；不代表可支付现金。"
    if problems:
        result.update(status="NEEDS_REVIEW", report=None, review_items=problems)
    return result


def cap(actual, cycle):
    if cycle["status"] == "NOT_STARTED":
        return {"status": "NOT_ASSESSED", "basis": "salary_cycle", "max_spendable": None,
                "first_payday": cycle["first_payday"], "reason_codes": ["SALARY_CYCLE_NOT_STARTED"],
                "note": "首个已配置发薪周期尚未开始；仅核对账务及已审可支付资金，不签发周期剩额或最多能花。"}
    if cycle["report"] is None:
        return {"status": "NEEDS_REVIEW", "basis": "salary_cycle", "max_spendable": None,
                "reason_codes": sorted({row["reason_code"] for row in cycle["review_items"]})}
    row = cycle["report"]["cycle_budget"]
    base = {"basis": "salary_cycle", "category_id": row["category_id"], "category_name": row["category_name"],
            "cycle_start": cycle["from"], "through": cycle["through"], "next_payday": cycle["next_payday"]}
    if row["status"] == "NOT_ASSESSED":
        return {**base, "status": "NOT_ASSESSED", "max_spendable": None, "reason_codes": ["SPENDABLE_CAP_NOT_ASSESSED"]}
    if actual["status"] != "CERTIFIED":
        return {**base, "status": "NEEDS_REVIEW", "max_spendable": None, "reason_codes": actual.get("reason_codes", [])}
    payable, remaining = Fraction(actual["actual_spendable"]), Fraction(row["remaining"])
    return {**base, "status": "CERTIFIED", "payable_after_obligations": exact_text(payable),
            "consumption_target_remaining": exact_text(remaining), "max_spendable": exact_text(min(payable, remaining)),
            "reason_codes": [], "note": "最多能花取已审可支付能力与当前发薪周期剩余额度的较小值；不采用自然月消费目标差。"}


def audit_configured_cycle(config, start=None, as_of=None):
    from .simple_config import _documents, _unchanged, _write, build_runtime
    watched = {}
    doc, budgets, plans = _documents(config, watched)
    cycles = read_cycles(plans, budgets, default_category=doc.get("spendable_cap_category", doc["defaults"]["expense_category"]))
    try:
        cutoff = date.fromisoformat(as_of) if as_of else month_end(doc["month"])
    except (ValueError, TypeError) as exc:
        raise BudgetError("SALARY_CYCLE_DATE_INVALID", "查询截止日须为 YYYY-MM-DD", str(config)) from exc
    if start is not None and as_of is None:
        following = next((key for key in cycles if key > start), None)
        if following:
            cutoff = date.fromisoformat(following) - timedelta(days=1)
    selected = window(cycles, cutoff, start)
    cutoff = selected[2] if selected else cutoff
    runtime = build_runtime(config, cutoff.strftime("%Y-%m"))
    try:
        _unchanged(watched)
        if runtime.pending:
            return {"mode": "v6_salary_cycle_spending", "from": selected[0] if selected else cutoff.isoformat(),
                    "through": cutoff.isoformat(), "next_payday": selected[1] if selected else None,
                    "configured_cycle": True, "status": "NEEDS_REVIEW", "report": None,
                    "review_items": runtime.pending["review_items"]}
        path = runtime.config.parent / "configured_salary_query.yaml"
        _write(path, {"schema": 1, "cycles": cycles, "category": doc.get("spendable_cap_category", doc["defaults"]["expense_category"]), "source": str(budgets)})
        result = audit_plan(runtime.config, path, cutoff, start=start)
        _unchanged(watched)
        runtime.check()
        return runtime.public_sources(result)
    finally:
        runtime.close()

"""Compare certified months and sum flows, never month-end stocks or capacity."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from fractions import Fraction
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys

from ..console import configure_output
from ..errors import BudgetError, require
from ..exact import exact_text, amount_text as _fmt
from ..yamlstrict import load_yaml, require_yaml, yaml_key_location
from .monthly_report import (
    OPTIONAL, REQUIRED, _config_month, audit_month_workflow, render_workflow_text,
)
from .review_diagnostics import IMPLEMENTATION_LIMITS
from .project import DEFAULT_PROJECT, _metadata, load_project
from .targets import _month


SCALARS = {"schema", "cutover", "month", "report_currency"}
CSV_FIELDS = ("scope", "month", "section", "id", "name", "account", "commodity",
              "metric", "value", "numerator", "denominator", "status", "source", "detail")
EXPENSE_METRICS = ("direct_gross_spent", "direct_refunded", "direct_spent",
                   "subtree_gross_spent", "subtree_refunded", "subtree_spent")
OUTFLOW_METRICS = ("expenses_paid", "expense_refunds", "debt_payments", "advances_paid",
                   "advance_reimbursements", "gross_outflow", "gross_inflow", "net_outflow")


def _months(first: str, last: str) -> list[str]:
    first_year, first_month = map(int, first.split("-"))
    last_year, last_month = map(int, last.split("-"))
    return [f"{index // 12:04d}-{index % 12 + 1:02d}"
            for index in range(first_year * 12 + first_month - 1, last_year * 12 + last_month)]


def _inputs(configs: list[Path], since: str | None, through: str | None):
    require(bool(configs), "PERIOD_CONFIG_REQUIRED", "至少提供一份已准备的月报配置")
    require((since is None) == (through is None), "PERIOD_RANGE_INVALID", "--from 和 --through 须同时提供")
    rows, watched = {}, {}
    for config in configs:
        config = config.resolve()
        before = sha256(config.read_bytes()).hexdigest()
        document = load_yaml(config)
        require(sha256(config.read_bytes()).hexdigest() == before, "INPUT_CHANGED",
                "读取区间配置时文件发生变化", str(config))
        require(type(document) is dict and REQUIRED <= set(document) <= REQUIRED | OPTIONAL,
                "MONTH_REPORT_CONFIG_INVALID", "月报配置字段不完整或含未知字段", f"{config}:1")
        require(type(document["schema"]) is int and document["schema"] == 6 and
                all(type(value) is str and value.strip() for key, value in document.items() if key != "schema"),
                "MONTH_REPORT_CONFIG_INVALID", "月报配置版本或字段类型无效", f"{config}:1")
        month = _config_month(document, config)
        if month in rows:
            raise BudgetError("PERIOD_MONTH_DUPLICATE", "同一月份只能提供一份配置", yaml_key_location(config, "month"))
        paths = {key: (config.parent / value).resolve() for key, value in document.items() if key not in SCALARS}
        for path in (config, *paths.values()):
            digest = sha256(path.read_bytes()).hexdigest()
            require(watched.setdefault(path, digest) == digest and (path != config or digest == before),
                    "INPUT_CHANGED", "区间输入在读取期间发生变化", str(path))
        rows[month] = {"config": config, "document": document, "paths": paths}
    ordered = sorted(rows)
    first, last = since or ordered[0], through or ordered[-1]
    # Reuse the exact month grammar, including calendar bounds.
    _month(first)
    _month(last)
    require(first <= last, "PERIOD_MONTHS_INCOMPLETE", "起始月份不能晚于结束月份")
    expected = _months(first, last)
    missing, extra = sorted(set(expected) - set(ordered)), sorted(set(ordered) - set(expected))
    require(ordered == expected, "PERIOD_MONTHS_INCOMPLETE",
            f"区间缺少月份：{', '.join(missing) or '无'}；范围外月份：{', '.join(extra) or '无'}。缺月不能当作零交易。")
    identity = None
    owner_ids, owner_names, category_ids, category_names = {}, {}, {}, {}
    default_identity = None
    for month in ordered:
        row = rows[month]
        document, paths = row["document"], row["paths"]
        current = (paths["ledger"], document["cutover"], document["report_currency"])
        if identity is not None and identity != current:
            key = next(key for key, before, after in zip(("ledger", "cutover", "report_currency"), identity, current)
                       if before != after)
            raise BudgetError("PERIOD_SCOPE_MISMATCH", "区间各月须使用同一根账本、cutover 和报告货币",
                              yaml_key_location(row["config"], key))
        identity = current
        owners, categories = load_yaml(paths["ownership"]), load_yaml(paths["classifications"])
        require(type(owners) is dict and type(categories) is dict and
                type(owners.get("owners")) is list and type(categories.get("categories")) is list,
                "PERIOD_REGISTRY_INVALID", "用途或类别名册缺失", str(row["config"]))
        for field, records, ids, names, source in (
            ("owners", owners["owners"], owner_ids, owner_names, paths["ownership"]),
            ("categories", categories["categories"], category_ids, category_names, paths["classifications"]),
        ):
            for entry in records:
                require(type(entry) is dict and type(entry.get("id")) is str and type(entry.get("name")) is str,
                        "PERIOD_REGISTRY_INVALID", "名册项须有 ID 和名称", str(source))
                ident, name = entry["id"], entry["name"]
                meaning = (name, entry.get("parent"))
                require((ident not in ids or ids[ident] == meaning) and
                        (name not in names or names[name] == ident), "PERIOD_REGISTRY_MISMATCH",
                        "区间内同一用途／类别的 ID、名称及父类别须保持一致；请复核共享名册",
                        yaml_key_location(source, field))
                ids[ident], names[name] = meaning, ident
        defaults = owners.get("defaults")
        if default_identity is not None and default_identity != (defaults,):
            raise BudgetError("PERIOD_DEFAULT_MISMATCH", "区间内默认收支用途须一致，不能把不同用途的默认资金收付混合累计",
                              yaml_key_location(paths["ownership"], "defaults"))
        default_identity = (defaults,)
    return rows, ordered, watched


def _sum_rows(reports, section, list_key, keys, metrics):
    totals = defaultdict(lambda: defaultdict(Fraction))
    for report in reports:
        for row in report[section][list_key]:
            key = tuple(row[field] for field in keys)
            for metric in metrics:
                totals[key][metric] += Fraction(row[metric])
    return [{**dict(zip(keys, key)), **{metric: exact_text(values[metric]) for metric in metrics}}
            for key, values in sorted(totals.items())]


def audit_period(configs: list[Path], since: str | None = None, through: str | None = None) -> dict:
    from .simple_config import is_simple, audit_simple_period
    if len(configs) == 1 and is_simple(load_yaml(configs[0])):
        return audit_simple_period(configs[0], since, through)
    rows, ordered, watched = _inputs(configs, since, through)
    months, reports, reviews, snapshots, warnings = [], [], [], set(), []
    for month in ordered:
        config = rows[month]["config"]
        try:
            outcome = audit_month_workflow(config)
            require(outcome["month"] == month, "INPUT_CHANGED", "区间配置的月份发生变化", str(config))
        except BudgetError as exc:
            if exc.code in {"INPUT_CHANGED", "MONTH_REPORT_MISMATCH"}:
                raise
            implementation = exc.code in IMPLEMENTATION_LIMITS
            outcome = {"status": "IMPLEMENTATION_BLOCKED" if implementation else "INPUT_ERROR",
                       "report": None, "review_items": [{
                "reason_code": exc.code, "reason_kind": "implementation_limit" if implementation else "input_error", "detail": exc.message,
                "source": exc.source or str(config)}]}
        months.append({"month": month, "config": str(config), "status": outcome["status"]})
        reviews.extend({**item, "month": month, "config": str(config)} for item in outcome["review_items"])
        snapshot = (outcome.get("report") or {}).get("ledger_snapshot") or outcome.get("review_queue", {}).get("ledger_snapshot")
        if snapshot:
            snapshots.add(snapshot)
        if outcome["status"] == "CERTIFIED_SCOPED":
            reports.append(outcome["report"])
            warnings.extend({"month": month, "detail": line} for line in render_workflow_text(outcome).splitlines()
                            if "警告" in line)
    require(len(snapshots) <= 1 and all(path.is_file() and sha256(path.read_bytes()).hexdigest() == digest
                                      for path, digest in watched.items()),
            "INPUT_CHANGED", "区间运行期间账本、配置或审阅资料发生变化；请重新运行")
    return _combine_months(ordered, months, reports, reviews, snapshots, warnings)


def audit_project_period(path: Path, since: str | None = None, through: str | None = None) -> dict:
    """Select registered months, then freshly audit the exact complete range."""
    project = load_project(path)
    require((since is None) == (through is None), "PERIOD_RANGE_INVALID",
            "--from 和 --through 须同时提供", str(project.path))
    first = _month(since) if since is not None else min(project.months)
    last = _month(through) if through is not None else max(project.months)
    require(first <= last, "PERIOD_MONTHS_INCOMPLETE", "起始月份不能晚于结束月份", str(project.path))
    ordered = _months(first, last)
    missing = [month for month in ordered if month not in project.months]
    require_yaml(not missing, "PERIOD_MONTHS_INCOMPLETE",
                 f"项目缺少月份：{', '.join(missing)}。请先登记各月配置；缺月不能当作零交易。",
                 project.path, ("months",))
    configs = [project.months[month] for month in ordered]
    before = {config: config.read_bytes() for config in configs}
    for month, config in zip(ordered, configs):
        require(_metadata(config) == (month, project.identity), "INPUT_CHANGED",
                "项目引用的月份或账本范围发生变化；请重新运行", str(config))
    outcome = audit_period(configs, first, last)
    require(project.path.read_bytes() == project.original and
            all(config.read_bytes() == content for config, content in before.items()),
            "INPUT_CHANGED", "区间运行期间项目或月份入口发生变化；请重新运行", str(project.path))
    return outcome


def _single_month_period(outcome: dict, config: Path) -> dict:
    """Format an already audited month with the existing one-month CSV layout.

    Only the caller's fresh, complete workflow is used. No second ledger audit
    or calculation from filtered display text is needed for a monthly export.
    """
    month = outcome["month"]
    months = [{"month": month, "config": str(config.resolve()), "status": outcome["status"]}]
    reviews = [{**item, "month": month, "config": str(config.resolve())}
               for item in outcome["review_items"]]
    reports, snapshots, warnings = [], set(), []
    if outcome["status"] == "CERTIFIED_SCOPED":
        reports = [outcome["report"]]
        snapshots.add(outcome["report"]["ledger_snapshot"])
        warnings = [{"month": month, "detail": line} for line in render_workflow_text(outcome).splitlines()
                    if "警告" in line]
    return _combine_months([month], months, reports, reviews, snapshots, warnings)


def _combine_months(ordered, months, reports, reviews, snapshots, warnings) -> dict:
    """Combine audited results; source and cross-month validation stays upstream."""
    states = {row["status"] for row in months}
    status = ("INPUT_ERROR" if "INPUT_ERROR" in states else
              "IMPLEMENTATION_BLOCKED" if "IMPLEMENTATION_BLOCKED" in states else
              "NEEDS_REVIEW" if states != {"CERTIFIED_SCOPED"} else "CERTIFIED_SCOPED")
    result = {"mode": "v6_period_report", "schema": 6, "from": ordered[0], "through": ordered[-1],
              "status": status, "months": months, "review_items": reviews, "report": None}
    if status != "CERTIFIED_SCOPED":
        return result
    expenses = _sum_rows(reports, "expense_targets", "categories", ("id", "name"), EXPENSE_METRICS)
    expense_total = {metric: exact_text(sum((Fraction(row[metric]) for row in expenses), Fraction(0)))
                     for metric in EXPENSE_METRICS[:3]}
    purpose_rows = _sum_rows(reports, "purpose_progress", "owners", ("owner_id",), ("actual_g",))
    names = {ident: name for report in reports for ident, name in report["owner_names"].items()}
    for row in purpose_rows:
        row["name"] = names[row["owner_id"]]
    last = reports[-1]
    result["report"] = {
        "report_currency": last["report_currency"], "ledger_snapshot": next(iter(snapshots)),
        "monthly_reports": reports, "warnings": warnings,
        "expense_totals": expense_total, "expenses": expenses, "purpose_allocations": purpose_rows,
        "tracking": _sum_rows(reports, "tracking", "topics", ("topic", "commodity"), ("spent", "returned", "net")),
        "default_outflows": (_sum_rows(reports, "default_outflows", "totals", ("commodity",), OUTFLOW_METRICS)
                             if all(row["default_outflows"]["status"] == "CERTIFIED" for row in reports) else None),
        "default_outflows_status": ("CERTIFIED" if all(row["default_outflows"]["status"] == "CERTIFIED" for row in reports)
                                    else "NOT_CONFIGURED"),
        "closing": {key: last[key] for key in ("as_of", "actual_spendable", "max_spendable", "purpose_balances")},
        "note": "区间仅累计流量；消费目标与 Q−G 逐月保留。余额、责任和可花只取期末，不加总；H、证券池账面成本和 K 申赎用途累计分别展示。",
    }
    return result


def render_text(outcome: dict) -> str:
    lines = [f"v6 区间报表 {outcome['from']} 至 {outcome['through']}：{outcome['status']}"]
    if outcome["report"] is None:
        lines.extend(f"{row['month']}：{row['status']} [{row['config']}]" for row in outcome["months"])
        lines.extend(f"  {row['month']} {row['reason_code']}：{row['detail']} [{row.get('source') or row['config']}]"
                     for row in outcome["review_items"])
        lines.append("有月份未通过，未生成区间数字；未提供的月份不能视为零。")
        return "\n".join(lines)
    report = outcome["report"]
    currency = report["report_currency"]
    lines.append(f"月份 | 消费原支出 | 退款 | 净消费 | 月末实际可花 | 月末最多能花（{currency}）")
    for row in report["monthly_reports"]:
        totals = [sum((Fraction(c[key]) for c in row["expense_targets"]["categories"]), Fraction(0))
                  for key in EXPENSE_METRICS[:3]]
        values = [_fmt(exact_text(value)) for value in totals] + [
            _fmt(row["actual_spendable"]["actual_spendable"]),
            _fmt(row["max_spendable"]["max_spendable"]) if row["max_spendable"]["max_spendable"] is not None else
            "不考核" if row["max_spendable"]["status"] == "NOT_ASSESSED" else "未配置"]
        lines.append(f"{row['month']} | " + " | ".join(values))
    totals = report["expense_totals"]
    lines.append(f"区间消费：原支出 {_fmt(totals['direct_gross_spent'])}，退款 {_fmt(totals['direct_refunded'])}，净额 {_fmt(totals['direct_spent'])} {currency}")
    lines.append("区间用途净划拨 G：")
    lines.extend(f"  {row['name']}：{_fmt(row['actual_g'])} {currency}" for row in report["purpose_allocations"])
    lines.append("区间默认支出用途资金收付（原币）：")
    if report["default_outflows"] is None:
        lines.append("  未配置默认用途收付，不能按零累计。")
    for row in report["default_outflows"] or []:
        lines.append(f"  {row['commodity']}：消费实付 {_fmt(row['expenses_paid'])}，退款 {_fmt(row['expense_refunds'])}，"
                     f"还债 {_fmt(row['debt_payments'])}，代垫 {_fmt(row['advances_paid'])}，报销 {_fmt(row['advance_reimbursements'])}，净流出 {_fmt(row['net_outflow'])}")
    lines.append("区间专题（原币；专题可能重叠，不可相加）：")
    lines.extend(f"  {row['topic']}：支出 {_fmt(row['spent'])}，退回 {_fmt(row['returned'])}，净额 {_fmt(row['net'])} {row['commodity']}"
                 for row in report["tracking"])
    lines.append("逐月目标比较（消费按子树，划拨按用途；不累计目标差）：")
    for month in report["monthly_reports"]:
        if month.get("salary_cycle", {}).get("status") == "NOT_STARTED":
            lines.append(f"  {month['month']}：工资周期目标尚未生效；首个已配置发薪日 {month['salary_cycle']['first_payday']}，不计算周期额度或最多能花。")
        if month.get("salary_cycle", {}).get("report"):
            cycle = month["salary_cycle"]
            lines.append(f"  {month['month']} 发薪周期 {cycle['from']}；统计至 {cycle['through']}（额度不按自然月重置）")
            for row in cycle["report"]["cycle_targets"]:
                lines.append(f"    {row['category_name']}：周期额度 {_fmt(row['target']) if row['target'] is not None else '不考核'}，"
                             f"净消费 {_fmt(row['net_spent'])}，剩余 {_fmt(row['remaining']) if row['remaining'] is not None else '不计算'} {currency}")
        for row in month["expense_targets"]["categories"]:
            if row["subtree_target"] is not None:
                lines.append(f"  {month['month']} {row['name']}：目标 {_fmt(row['subtree_target'])}，消费 {_fmt(row['subtree_spent'])}，差额 {_fmt(row['subtree_remaining'])} {currency}")
            elif row["subtree_remaining_status"] == "NOT_ASSESSED":
                lines.append(f"  {month['month']} {row['name']}：不考核目标，消费 {_fmt(row['subtree_spent'])} {currency}；差额不计算")
        for row in month["purpose_progress"]["owners"]:
            target = _fmt(row["target_q"]) if row["target_q"] is not None else "不考核"
            remaining = _fmt(row["remaining_q_minus_g"]) if row["remaining_q_minus_g"] is not None else "不考核"
            lines.append(f"  {month['month']} {month['owner_names'][row['owner_id']]}：Q {target}，G {_fmt(row['actual_g'])}，Q−G {remaining}")
    lines.extend(f"{row['month']} {row['detail']}" for row in report["warnings"])
    lines.append(f"期末用途分布（{report['closing']['as_of']}，各口径分别展示）：")
    balances = report["closing"]["purpose_balances"]
    for purpose in [*balances["purposes"], *([balances["outside_budget"]] if balances["outside_budget"] else [])]:
        if not any(purpose[key] for key in ("h_native_balances", "pool_book_cost_balances", "k_book_purpose_changes")):
            lines.append(f"  {purpose['name']}：无余额或已审用途变动记录")
        for key, label, metric in (("h_native_balances", "H 原币余额", "units"),
                                    ("pool_book_cost_balances", "证券池账面成本", "units"),
                                    ("k_book_purpose_changes", "K 自 cutover 申赎用途净变动", "signed_change")):
            for coordinate in purpose[key]:
                for account in coordinate["accounts"]:
                    lines.append(f"  {purpose['name']} | {account['account']} | {label}：{_fmt(account[metric])} {coordinate['commodity']}")
    for key, label, ident in (("booked_debts", "期末未还卡债", "obligation_id"),
                              ("clearing_receivables", "期末待收单位代垫", "clearing_id")):
        for row in report["closing"]["actual_spendable"][key]:
            lines.append(f"{label} {row[ident]}：{_fmt(row['remaining_native'])} {row['commodity']}，{row['account']}")
    lines.append(report["note"])
    return "\n".join(lines)


def _decimal(value: Fraction) -> str:
    """Only emit an exact finite decimal; never silently round a CSV number."""
    value = _fmt(value)
    return "" if "/" in value else value


def csv_rows(outcome: dict) -> list[dict]:
    rows = []

    def add(scope, month, section, metric, value=None, **fields):
        numeric = Fraction(value) if value is not None else None
        rows.append({"scope": scope, "month": month, "section": section, "metric": metric,
                     "value": _decimal(numeric) if numeric is not None else "",
                     "numerator": str(numeric.numerator) if numeric is not None else "",
                     "denominator": str(numeric.denominator) if numeric is not None else "",
                     "status": "CERTIFIED_SCOPED", **fields})

    if outcome["report"] is None:
        for month in outcome["months"]:
            add("month", month["month"], "audit", "status", status=month["status"], source=month["config"])
        for item in outcome["review_items"]:
            add("review", item["month"], "review", item["reason_code"],
                status=item.get("reason_kind", "data_review"), source=item.get("source") or item["config"], detail=item["detail"])
        return rows
    report, period = outcome["report"], outcome["from"] + ".." + outcome["through"]
    currency = report["report_currency"]
    for month, source in zip(report["monthly_reports"], outcome["months"]):
        label, config = month["month"], source["config"]
        if month.get("salary_cycle", {}).get("status") == "NOT_STARTED":
            add("month_end", label, "salary_cycle", "not_started", None, commodity=currency,
                source=config, status="NOT_STARTED", detail=f"首个已配置发薪日：{month['salary_cycle']['first_payday']}；此前不套用周期目标。")
        if month.get("salary_cycle", {}).get("report"):
            cycle = month["salary_cycle"]
            for row in cycle["report"]["cycle_targets"]:
                for metric in ("target", "gross_spent", "refunded", "net_spent", "remaining"):
                    add("month_end", label, "salary_cycle", metric, row[metric], id=row["category_id"],
                        name=row["category_name"], commodity=currency, source=config, status=row["status"],
                        detail=f"发薪周期 {cycle['from']}；统计至 {cycle['through']}；跨月不重置，不加总周期快照。")
        for metric in EXPENSE_METRICS[:3]:
            total = sum((Fraction(row[metric]) for row in month["expense_targets"]["categories"]), Fraction(0))
            add("month", label, "expense_total", metric, exact_text(total), commodity=currency, source=config)
        for metric in ("actual_spendable", "max_spendable"):
            add("month_end", label, "capacity", metric, month[metric][metric], commodity=currency,
                status=month[metric]["status"], source=config)
        for row in month["expense_targets"]["categories"]:
            for metric in (*EXPENSE_METRICS, "direct_target", "direct_remaining", "subtree_target", "subtree_remaining"):
                unassessed = (metric in {"direct_target", "direct_remaining"} and row["direct_remaining_status"] == "NOT_ASSESSED" or
                              metric in {"subtree_target", "subtree_remaining"} and row["subtree_remaining_status"] == "NOT_ASSESSED")
                add("month", label, "expenses", metric, row[metric], id=row["id"], name=row["name"],
                    commodity=currency, source=config, status=("NOT_ASSESSED" if unassessed else
                        "NOT_DECLARED" if row[metric] is None else "CERTIFIED_SCOPED"))
        for row in month["purpose_progress"]["owners"]:
            for metric in ("target_q", "actual_g", "remaining_q_minus_g"):
                add("month", label, "allocations", metric, row[metric], id=row["owner_id"],
                    name=month["owner_names"][row["owner_id"]], commodity=currency, source=config,
                    status="NOT_ASSESSED" if row[metric] is None else "CERTIFIED_SCOPED")
    for row in report["expenses"]:
        for metric in EXPENSE_METRICS:
            add("period", period, "expenses", metric, row[metric], id=row["id"], name=row["name"], commodity=currency)
    for metric, value in report["expense_totals"].items():
        add("period", period, "expense_total", metric, value, commodity=currency)
    for row in report["purpose_allocations"]:
        add("period", period, "allocations", "actual_g", row["actual_g"], id=row["owner_id"], name=row["name"], commodity=currency)
    # Native flows retain their currencies, both monthly and for the whole range.
    groups = [("month", month["month"], month["tracking"]["topics"], month["default_outflows"]["totals"])
              for month in report["monthly_reports"]]
    groups.append(("period", period, report["tracking"], report["default_outflows"]))
    for scope, label, topics, flows in groups:
        for row in topics:
            for metric in ("spent", "returned", "net"):
                add(scope, label, "tracking", metric, row[metric], name=row["topic"], commodity=row["commodity"])
        if flows is None:
            add(scope, label, "default_outflows", "status", status="NOT_CONFIGURED")
        for row in flows or []:
            for metric in OUTFLOW_METRICS:
                add(scope, label, "default_outflows", metric, row[metric], commodity=row["commodity"])
    last = report["closing"]
    for metric in ("actual_spendable", "max_spendable"):
        add("closing", last["as_of"], "capacity", metric, last[metric][metric], commodity=currency,
            status=last[metric]["status"])
    balances = last["purpose_balances"]
    for purpose in [*balances["purposes"], *([balances["outside_budget"]] if balances["outside_budget"] else [])]:
        if not any(purpose[key] for key in ("h_native_balances", "pool_book_cost_balances", "k_book_purpose_changes")):
            add("closing", last["as_of"], "purpose_balances", "position_status", id=purpose["owner_id"],
                name=purpose["name"], status="NO_RECORDED_POSITIONS")
        for section, metric in (("h_native_balances", "units"), ("pool_book_cost_balances", "units"),
                                ("k_book_purpose_changes", "signed_change")):
            for coordinate in purpose[section]:
                for account in coordinate["accounts"]:
                    add("closing", last["as_of"], section, metric, account[metric],
                        id=purpose["owner_id"], name=purpose["name"], account=account["account"],
                        commodity=coordinate["commodity"], detail=account.get("pool_id", ""))
    for key, section, ident in (("booked_debts", "booked_debt", "obligation_id"),
                               ("clearing_receivables", "clearing_receivable", "clearing_id")):
        for row in last["actual_spendable"][key]:
            add("closing", last["as_of"], section, "remaining_native", row["remaining_native"],
                id=row[ident], account=row["account"], commodity=row["commodity"], source=row.get("source", ""))
    for warning in report["warnings"]:
        add("month", warning["month"], "warning", "warning", status="WARNING", detail=warning["detail"])
    return rows


def render_csv(outcome: dict) -> str:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for row in csv_rows(outcome):
        # Free text is never an executable spreadsheet formula. Numeric columns
        # above are generated solely from exact amounts, including negative ones.
        writer.writerow({key: ("'" + str(value) if key in {"name", "account", "source", "detail"} and
                                str(value).lstrip().startswith(("=", "+", "-", "@")) else value)
                         for key, value in row.items()})
    return stream.getvalue()


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description="重新审计连续月份，比较月报并导出区间流量")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--config", type=Path, action="append", help="每月最新配置，可重复；不能缺月")
    inputs.add_argument("--project", type=Path, help=f"项目入口；未指定输入时读取当前目录的 {DEFAULT_PROJECT}")
    parser.add_argument("--from", dest="since", help="起始月份 YYYY-MM；须与 --through 一起提供")
    parser.add_argument("--through", help="结束月份 YYYY-MM")
    parser.add_argument("--payday", help="工资周期起点 YYYY-MM-DD（含当天）；配合 --next-payday")
    parser.add_argument("--next-payday", help="下次工资到账日 YYYY-MM-DD（归下一周期，本次不包含）")
    parser.add_argument("--cycle-budget", help="本工资周期的消费额度；省略时只统计消费，不套用月目标")
    parser.add_argument("--cycle-category", help="周期额度对应的消费类别名称或 ID；默认使用消费上限类别")
    parser.add_argument("--cycle", help="查询 budgets.yml 已保存的发薪日 YYYY-MM-DD；current 选当前配置月份末的周期")
    parser.add_argument("--as-of", help="已保存周期的统计截止日 YYYY-MM-DD；不含之后的交易")
    parser.add_argument("--format", choices=("text", "json", "csv"), default="text")
    parser.add_argument("--output", type=Path, help="保存到新文件；CSV 使用 UTF-8 BOM，便于 Windows 打开中文")
    args = parser.parse_args(argv)
    if not args.config and not args.project and (Path.cwd() / "config.yml").is_file():
        args.config = [Path.cwd() / "config.yml"]
    salary = any(value is not None for value in (args.payday, args.next_payday, args.cycle_budget, args.cycle_category))
    if args.cycle and (salary or args.since or args.through) or args.as_of and not args.cycle:
        parser.error("--cycle／--as-of 不与手填工资周期或自然月区间混用")
    if salary and (not args.payday or not args.next_payday or args.since or args.through):
        parser.error("工资周期须同时提供 --payday 和 --next-payday，不与自然月 --from／--through 混用")
    try:
        if args.output:
            require(not args.output.exists(), "PERIOD_OUTPUT_EXISTS", "输出文件已存在，请选择新文件", str(args.output))
        text_renderer, csv_renderer = render_text, render_csv
        if args.cycle:
            from .simple_config import is_simple
            from .salary_plan import audit_configured_cycle
            from .salary_cycle import render_text as salary_text, render_csv as salary_csv
            require(args.config and len(args.config) == 1 and is_simple(load_yaml(args.config[0])),
                    "SALARY_PLAN_CONFIG_REQUIRED", "已保存工资周期请提供一份两文件入口 --config config.yml")
            outcome = audit_configured_cycle(args.config[0], None if args.cycle == "current" else args.cycle, args.as_of)
            text_renderer, csv_renderer = salary_text, salary_csv
        elif salary:
            from .salary_cycle import audit_salary_cycle, select_config, render_text as salary_text, render_csv as salary_csv
            project = load_project(args.project or Path.cwd() / DEFAULT_PROJECT) if not args.config else None
            config = select_config(args.config or list(project.months.values()), args.payday, args.next_payday)
            outcome = audit_salary_cycle(config, args.payday, args.next_payday, args.cycle_budget, args.cycle_category,
                                         expected_identity=project.identity if project is not None else None)
            require(project is None or project.path.read_bytes() == project.original,
                    "INPUT_CHANGED", "工资周期查询期间项目入口发生变化")
            text_renderer, csv_renderer = salary_text, salary_csv
        else:
            outcome = (audit_period(args.config, args.since, args.through) if args.config else
                       audit_project_period(args.project or Path.cwd() / DEFAULT_PROJECT, args.since, args.through))
        content = (text_renderer(outcome) if args.format == "text" else csv_renderer(outcome)
                   if args.format == "csv" else json.dumps(outcome, ensure_ascii=False, indent=2))
        if args.output:
            with args.output.open("x", encoding="utf-8-sig" if args.format == "csv" else "utf-8", newline="") as stream:
                stream.write(content)
            print(f"{outcome['status']}：{args.output.resolve()}")
        else:
            print(content, end="" if content.endswith("\n") else "\n")
        return 2 if outcome["status"] == "INPUT_ERROR" else 0
    except BudgetError as exc:
        print(f"{exc.code}：{exc.message} [{exc.source or '区间配置'}]", file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(f"PERIOD_IO_ERROR：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

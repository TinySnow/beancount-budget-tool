"""Expense budgets between explicit paydays; the next payday is exclusive."""

from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
import csv
from io import StringIO
import re

from beancount.core import data

from ..errors import BudgetError, require
from ..exact import exact, exact_text, amount_text
from ..financial import booked_through, frozen_financial
from ..yamlstrict import load_yaml
from .classification import _key, _registry
from .facts import FactIndex, frozen_fact_indices
from .review_diagnostics import annotate_source_items
from .settlement_budget import audit_settlement_budget_values
from .source import collect_v6_source_issues
from .spending import _audit_spending_window
from .valuation import audit_classification_valuation


AMOUNT = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")


def cycle_bounds(payday: str, next_payday: str, budget: str | None = None):
    try:
        require(all(type(value) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                    for value in (payday, next_payday)),
                "SALARY_CYCLE_DATE_INVALID", "工资到账日须为 YYYY-MM-DD")
        first, next_day = date.fromisoformat(payday), date.fromisoformat(next_payday)
    except (ValueError, TypeError) as exc:
        raise BudgetError("SALARY_CYCLE_DATE_INVALID", "工资到账日须为有效 YYYY-MM-DD") from exc
    require(first < next_day, "SALARY_CYCLE_RANGE_INVALID", "下次工资日须晚于本次工资日")
    require(budget is None or type(budget) is str and bool(AMOUNT.fullmatch(budget)),
            "SALARY_CYCLE_BUDGET_INVALID", "周期额度须为非负十进制金额；不填写则只统计消费")
    return first, next_day - timedelta(days=1)


def select_config(configs: list[Path], payday: str, next_payday: str) -> Path:
    """Use the latest supplied review for the cycle's final calendar month."""
    from .period_report import _config_month
    _, last = cycle_bounds(payday, next_payday)
    from .simple_config import is_simple
    if len(configs) == 1 and is_simple(load_yaml(configs[0])):
        return configs[0]
    month = last.strftime("%Y-%m")
    matching = [path for path in configs if _config_month(load_yaml(path), path) == month]
    require(len(matching) == 1, "SALARY_CYCLE_CONFIG_REQUIRED",
            f"请提供周期末所在 {month} 的唯一最新配置；它须包含此前已审分类", month)
    return matching[0]


def audit_salary_cycle(config: Path, payday: str, next_payday: str,
                       budget: str | None = None, category: str | None = None, *,
                       expected_identity: tuple | None = None) -> dict:
    first, last = cycle_bounds(payday, next_payday, budget)
    from .simple_config import is_simple, build_runtime
    if is_simple(load_yaml(config)):
        runtime = build_runtime(config, last.strftime("%Y-%m"))
        try:
            if runtime.pending:
                return {"mode": "v6_salary_cycle_spending", "from": first.isoformat(), "through": last.isoformat(),
                        "next_payday": next_payday, "status": "NEEDS_REVIEW", "report": None,
                        "review_items": runtime.pending["review_items"]}
            result = audit_salary_cycle(runtime.config, payday, next_payday, budget, category, expected_identity=expected_identity)
            runtime.check()
            return runtime.public_sources(result)
        finally:
            runtime.close()
    from .period_report import _inputs
    config = config.resolve()
    rows, ordered, watched = _inputs([config], None, None)
    require(ordered == [last.strftime("%Y-%m")], "SALARY_CYCLE_CONFIG_REQUIRED",
            "使用周期末所在自然月的最新配置，不重置 cutover", str(config))
    document, paths = rows[ordered[0]]["document"], rows[ordered[0]]["paths"]
    try:
        cutover = date.fromisoformat(document["cutover"])
    except ValueError as exc:
        raise BudgetError("TIME_INVALID", "cutover 须为有效日期", str(config)) from exc
    require(expected_identity is None or expected_identity == (paths["ledger"], cutover, document["report_currency"]),
            "PROJECT_CONTEXT_CHANGED", "查询配置的账本／cutover／报告货币与项目入口不一致", str(config))
    require(first >= cutover, "SALARY_CYCLE_BEFORE_CUTOVER", "周期起点不得早于原 cutover", str(config))
    result = {"mode": "v6_salary_cycle_spending", "schema": 6,
              "from": first.isoformat(), "through": last.isoformat(), "next_payday": next_payday,
              "boundary": "payday_inclusive_next_payday_exclusive",
              "scope": "expense_classification_and_cycle_budget_only",
              "config": str(config), "cutover": cutover.isoformat(),
              "status": "NEEDS_REVIEW", "review_items": [], "report": None}
    with booked_through(last), frozen_financial(paths["ledger"]) as fin, frozen_fact_indices():
        issues = [row for row in collect_v6_source_issues(paths["ledger"], cutover, last)
                  if first.isoformat() <= row["date"] <= last.isoformat()]
        if not issues:
            registry = load_yaml(paths["classifications"])
            names = _registry(registry["categories"], source=paths["classifications"])
            ident = (names.get(category, category) if category else
                     load_yaml(paths["expense_plans"]).get("spendable_cap_category_id"))
            require(ident in names.values(), "SALARY_CYCLE_CATEGORY_INVALID",
                    "请用 --cycle-category 指定已登记消费类别，或配置消费上限类别", str(paths["expense_plans"]))
            facts = FactIndex(fin)
            sources = {_key(facts.for_posting(p).as_dict()): (e, p) for e in fin.entries
                       if isinstance(e, data.Transaction) for p in e.postings}
            valuation = audit_classification_valuation(paths["ledger"], cutover, paths["classifications"],
                                                        document["report_currency"])
            missing_months = {row["month"] for row in valuation["missing_historical_value"]
                              if row["account"].split(":", 1)[0] == fin.options["name_expenses"] and
                              first <= sources[_key(row["ref"])][0].date <= last}
            values = []
            # All settlement evidence is bounded by the cycle's last day,
            # including settlements in a later calendar month of this cycle.
            for month in sorted(missing_months):
                values.extend(audit_settlement_budget_values(
                    paths["ledger"], cutover, month, paths["classifications"], document["report_currency"],
                    paths["accounts"], paths["ownership"], paths["obligations"], paths["h_fx"],
                    through=last)["values"])
            spending = _audit_spending_window(paths["ledger"], cutover, first, last,
                paths["classifications"], document["report_currency"], paths.get("benefit_groups"),
                values, include_evidence=True)
            expense = spending["metrics"]["net_expense"]
            issues = expense["review_items"]
            if not issues:
                selected = next(row for row in expense["categories"] if row["id"] == ident)
                target = Fraction(budget) if budget is not None else None
                remaining = target - Fraction(selected["subtree"]) if target is not None else None
                evidence = spending["_transaction_evidence"]
                priced = {_key(row["ref"]): row for row in evidence["valuations"]}
                settled = {_key(row["ref"]): row for row in values}
                events = []
                for claim in evidence["classifications"]:
                    entry, posting = sources[_key(claim["ref"])]
                    if posting.account.split(":", 1)[0] != fin.options["name_expenses"]:
                        continue
                    comparison = settled.get(_key(claim["ref"]))
                    events.append({"date": entry.date.isoformat(), "narration": entry.narration,
                        "account": posting.account, "commodity": posting.units.currency,
                        "signed_units": exact_text(exact(posting.units.number)), "ref": claim["ref"],
                        "source": f"{posting.meta['filename']}:{posting.meta['lineno']}",
                        "classification": claim["slices"],
                        "valuation": priced.get(_key(claim["ref"])),
                        "settlement_budget_value": comparison})
                result.update(status="CERTIFIED_SCOPED", report={
                    "report_currency": document["report_currency"], "ledger_snapshot": fin.digest,
                    "categories": expense["categories"], "native_totals": expense["native_totals"],
                    "total_net_expense": expense["report_total"], "events": events,
                    "cycle_budget": {"category_id": ident, "category_name": selected["name"],
                        "target": exact_text(target) if target is not None else None,
                        "gross_spent": selected["subtree_gross_spent"], "refunded": selected["subtree_refunded"],
                        "net_spent": selected["subtree"],
                        "remaining": exact_text(remaining) if remaining is not None else None},
                    "warnings": ["周期消费超过所填额度。"] if remaining is not None and remaining < 0 else [],
                    "note": "额度由本周期明确指定，不复制、累加或按天折算自然月目标；只签发消费分类及额度差，不签发实际可花、资金库存或周期划拨目标。"})
        result["review_items"] = [{**row, "config": str(config), "month": ordered[0]}
                                  for row in annotate_source_items(issues)]
        if any(row["reason_kind"] == "implementation_limit" for row in result["review_items"]):
            result["status"] = "IMPLEMENTATION_BLOCKED"
    from hashlib import sha256
    require(all(path.is_file() and sha256(path.read_bytes()).hexdigest() == digest
                for path, digest in watched.items()), "INPUT_CHANGED", "周期查询期间配置或审阅资料发生变化")
    return result


def render_text(outcome: dict) -> str:
    boundary = (f"下次工资日 {outcome['next_payday']} 归下一周期；" if outcome.get("next_payday") else "下次工资日尚未登记；仅统计至查询截止日；")
    lines = [f"工资周期 {outcome['from']} 至 {outcome['through']}：{outcome['status']}",
             boundary + "本报告只统计周期消费及额度差。"]
    if outcome["report"] is None:
        lines.extend(f"{row['reason_code']}：{row['detail']} [{row['source']}]" for row in outcome["review_items"])
        return "\n".join([*lines, "本次未签发周期数字，请处理待审或实现缺口后重算。"])
    report, amount = outcome["report"], amount_text
    cycle, currency = report["cycle_budget"], report["report_currency"]
    lines += [f"{cycle['category_name']}：原支出 {amount(cycle['gross_spent'])}，退款 {amount(cycle['refunded'])}，净消费 {amount(cycle['net_spent'])} {currency}"]
    if cycle["target"] is not None:
        lines += [f"本周期额度 {amount(cycle['target'])}；额度剩余 {amount(cycle['remaining'])} {currency}"]
    else:
        lines += ["未指定周期额度，只统计实际消费。"]
    for row in report.get("cycle_targets", []):
        if row["category_id"] == cycle["category_id"]:
            continue
        lines.append(f"{row['category_name']}：周期额度 {amount(row['target']) if row['target'] is not None else '不考核'}；"
                     f"净消费 {amount(row['net_spent'])}；剩余 {amount(row['remaining']) if row['remaining'] is not None else '不计算'} {currency}")
    lines += ["", "类别 | 直接净消费 | 含子类别净消费"]
    lines += [f"{row['name']} | {amount(row['direct'])} | {amount(row['subtree'])} {currency}"
              for row in report["categories"] if row["active_direct"] or row["id"] == cycle["category_id"]]
    for row in report["events"]:
        if row["settlement_budget_value"]:
            value = row["settlement_budget_value"]
            lines.append(f"外币消费 {row['date']} {amount(row['signed_units'])} {row['commodity']}；预算比较采用 {value['settlement_date']} 实际清偿成本 {amount(value['budget_comparison_value'])} {currency} [{row['source']}]")
    lines += [f"警告：{warning}" for warning in report["warnings"]]
    return "\n".join([*lines, report["note"]])


def render_csv(outcome: dict) -> str:
    stream = StringIO(newline="")
    fields = ("from", "through", "next_payday", "status", "category", "metric", "value", "commodity", "source", "detail")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    base = {key: outcome[key] for key in fields[:4]}
    if outcome["report"] is None:
        for row in outcome["review_items"]:
            writer.writerow({**base, "metric": row["reason_code"], "source": row["source"], "detail": row["detail"]})
    else:
        report = outcome["report"]
        for row in report["categories"]:
            for key in ("direct_gross_spent", "direct_refunded", "direct", "subtree"):
                writer.writerow({**base, "category": row["name"], "metric": key,
                                 "value": row[key], "commodity": report["report_currency"]})
        for cycle in report.get("cycle_targets", [report["cycle_budget"]]):
            for key in ("target", "remaining"):
                writer.writerow({**base, "category": cycle["category_name"], "metric": "cycle_" + key,
                    "value": cycle[key], "commodity": report["report_currency"], "detail": report["note"]})
        names = {row["id"]: row["name"] for row in report["categories"]}
        for row in report["native_totals"]:
            writer.writerow({**base, "category": names.get(row["category_id"], row["category_id"]),
                "metric": "native_expense", "value": row["signed_units"], "commodity": row["commodity"],
                "detail": f"{row['month']} 原币分类金额；不同币种不得相加。"})
        for row in report["events"]:
            value = row["settlement_budget_value"]
            if value:
                writer.writerow({**base, "metric": "actual_settlement_budget_comparison",
                    "value": value["budget_comparison_value"], "commodity": report["report_currency"],
                    "source": row["source"], "detail": f"消费 {row['date']}：{amount_text(row['signed_units'])} {row['commodity']}；预算比较采用 {value['settlement_date']} 实际清偿成本，不是消费日汇率。"})
    return stream.getvalue()

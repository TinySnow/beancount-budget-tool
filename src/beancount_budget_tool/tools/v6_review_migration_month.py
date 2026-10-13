"""One read-only entry point for a single month of ledger migration and review.

The structural comparison comes first. Before cutover, it is the whole v6
workflow. From cutover onward, a reviewed month config can continue into the
existing source queue and monthly report without copying any ledger files.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from beancount_budget_tool.errors import BudgetError, require
from beancount_budget_tool.v6.monthly_report import (
    audit_month_workflow, render_workflow_text,
)
from beancount_budget_tool.yamlstrict import load_yaml, yaml_key_location

from .v6_check_migration_month import _month, check_month


def review_month(before: Path, after: Path, month: str, cutover: date,
                 config: Path | None = None,
                 allow_financial_corrections: bool = False) -> dict:
    first, _ = _month(month)
    require(isinstance(cutover, date) and cutover.day == 1,
            "CUTOVER_INVALID", "cutover 必须是自然月第一天")
    structural = check_month(before, after, month, allow_financial_corrections,
                             cutover=cutover, source_issues_as_review=True)
    result = {"mode": "v6_one_month_migration_review", "month": month,
              "cutover": cutover.isoformat(), "structural_check": structural,
              "review": None, "error": None}
    if first < cutover:
        result.update(status="PRE_CUTOVER_CHECKED", stage="pre_cutover")
        return result
    issues = structural["month_source_audit"].get("issues", [])
    if issues:
        result.update(status="NEEDS_REVIEW", stage="source_preflight",
                      review={"items": issues, "note":
                              "先修正这些账本标签并重跑；本次尚未检查用途、计划和月报。"})
        return result
    if config is None:
        result.update(status="REVIEW_CONFIG_REQUIRED", stage="config")
        return result

    try:
        config = config.resolve()
        document = load_yaml(config)
        require(type(document) is dict, "MONTH_REPORT_CONFIG_INVALID",
                "月报配置必须是映射", f"{config}:1")
        require(document.get("month") == month, "MIGRATION_MONTH_CONFIG_MISMATCH",
                "月报配置的 month 与本次复审月份不同",
                yaml_key_location(config, "month"))
        require(document.get("cutover") == cutover.isoformat(),
                "MIGRATION_CUTOVER_CONFIG_MISMATCH",
                "月报配置的 cutover 与本次复审不一致",
                yaml_key_location(config, "cutover"))
        value = document.get("ledger")
        require(type(value) is str and bool(value.strip()),
                "MONTH_REPORT_CONFIG_INVALID", "月报配置缺少根账本路径",
                yaml_key_location(config, "ledger"))
        require((config.parent / value).resolve() == after.resolve(),
                "MIGRATION_LEDGER_CONFIG_MISMATCH",
                "月报配置必须指向本次校验的修改后账本",
                yaml_key_location(config, "ledger"))
        outcome = audit_month_workflow(config)
    except BudgetError as exc:
        result.update(status="REVIEW_INPUT_BLOCKED", stage="review_input",
                      error={"code": exc.code, "message": exc.message,
                             "source": exc.source})
        return result

    result.update(status=outcome["status"], stage=outcome["stage"], review=outcome)
    return result


def render_text(result: dict) -> str:
    check = result["structural_check"]
    lines = [f"单月复审 {result['month']}：{result['status']}",
             "账本改动范围：仅目标月交易块；修改前后均已通过 Beancount 加载"]
    if check["financial_facts_equal"]:
        lines.append("booked 财务事实：未变")
    else:
        lines.append(f"booked 财务事实：{len(check['financial_changes'])} 组差异，须逐项核对")
        for change in check["financial_changes"]:
            lines.append(f"  {change['change']}：")
            for label in ("before", "after"):
                for entry in change[label]:
                    lines.append(f"    {label} {entry['date']} [{entry['source']}] "
                                 f"{entry.get('narration') or ''}")
                    for posting in entry.get("postings", []):
                        lines.append(f"      {posting['account']} {posting['units']}")
    if result["stage"] == "pre_cutover":
        lines.append("本月早于 cutover：v6 不审预算标签、用途或月报。")
    elif result["stage"] == "source_preflight":
        issues = result["review"]["items"]
        lines.append(f"账本标签待审 {len(issues)} 项：")
        for item in issues:
            lines.append(f"  {item['date']} {item['reason_code']}：{item['detail']} "
                         f"[{item['source']}]")
        lines.append(result["review"]["note"])
    elif result["stage"] == "config":
        lines.append("本月已到 cutover：提供指向修改后账本的已审月报配置，继续来源待审与月报。")
    elif result["stage"] == "review_input":
        error = result["error"]
        lines.append(f"复审输入阻断：{error['code']}：{error['message']} "
                     f"[{error['source'] or '未知位置'}]")
    else:
        lines.append(render_workflow_text(result["review"]))
    return "\n".join(lines)


def main(argv=None) -> int:
    from beancount_budget_tool.console import configure_output
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path)
    parser.add_argument("--after", required=True, type=Path)
    parser.add_argument("--month", required=True)
    parser.add_argument("--cutover", required=True, type=date.fromisoformat)
    parser.add_argument("--config", type=Path, help="cutover 当月及之后的已审月报配置")
    parser.add_argument("--allow-financial-corrections", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = review_month(args.before, args.after, args.month, args.cutover,
                              args.config, args.allow_financial_corrections)
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message,
                                    "source": exc.source}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(render_text(result) if args.format == "text" else
          json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

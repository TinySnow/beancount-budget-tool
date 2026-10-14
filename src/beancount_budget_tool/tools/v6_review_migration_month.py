"""One read-only entry point for a single month of ledger migration and review.

The structural comparison comes first. Before cutover, it is the whole v6
workflow. From cutover onward, a reviewed month config can continue into the
existing source queue and monthly report without copying any ledger files.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
import time

from beancount_budget_tool.errors import BudgetError, require
from beancount_budget_tool.financial import booked_through, frozen_financial
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.facts import frozen_fact_indices
from beancount_budget_tool.v6.monthly_report import (
    audit_month_workflow, render_workflow_text,
)
from beancount_budget_tool.yamlstrict import load_yaml, yaml_key_location

from .v6_check_migration_month import _month, check_month
from .v6_classification_proposals import propose as propose_classifications


WATCH_SUFFIXES = frozenset((".bean", ".beancount", ".yaml", ".yml"))
WATCH_IGNORED_DIRS = frozenset((".git", ".venv", "__pycache__", "node_modules"))


def _setup_preview(after: Path, month: str, cutover: date) -> dict:
    """Show the next review inputs without approving any ledger interpretation."""
    _, last = _month(month)
    with booked_through(last), frozen_financial(after), frozen_fact_indices():
        accounts = audit_accounts(after, cutover)
        classifications = propose_classifications(after, cutover, last)
    groups = classifications["groups"]
    return {
        "as_of": last.isoformat(),
        "accounts_to_review": [
            {"account": row["account"], "commodities": row["commodities"],
             "first_source": row["first_source"]}
            for row in accounts["accounts"]
        ],
        "untagged_income_expense_postings": sum(group["count"] for group in groups),
        "classification_groups": [
            {"account": group["account"], "count": group["count"],
             "first_source": group["candidates"][0]["source"]}
            for group in groups
        ],
        "note": "未写行内 budget 的笔数不等于必须逐笔补标签；已审默认分类可覆盖普通收支。账户语义、期初用途、逐腿用途、责任和计划仍须审阅。",
    }


def _watch_snapshot(before: Path, after: Path, config: Path | None) -> dict[Path, tuple[int, int] | None]:
    """Track the ledger trees and review YAML without relying on OS-specific watchers."""
    explicit = {before.resolve(), after.resolve()}
    if config is not None:
        explicit.add(config.resolve())
    files = set(explicit)
    for root in {path.parent for path in explicit}:
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = [name for name in subdirs if name not in WATCH_IGNORED_DIRS]
            files.update(Path(directory) / name for name in names
                         if Path(name).suffix.lower() in WATCH_SUFFIXES)
    snapshot = {}
    for path in files:
        try:
            info = path.stat()
            snapshot[path] = (info.st_mtime_ns, info.st_size)
        except OSError:
            snapshot[path] = None
    return snapshot


def watch_month(before: Path, after: Path, month: str, cutover: date,
                config: Path | None, allow_financial_corrections: bool) -> int:
    """Repeat the same read-only review after relevant files settle on disk."""
    print("自动复审已启动；保存账本或审阅 YAML 后会重跑，按 Ctrl+C 停止。", flush=True)
    snapshot = _watch_snapshot(before, after, config)
    try:
        while True:
            print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S} 复审 {month} ===", flush=True)
            retry_unreadable = False
            try:
                result = review_month(before, after, month, cutover, config,
                                      allow_financial_corrections)
                print(render_text(result), flush=True)
            except BudgetError as exc:
                print(f"复审输入阻断：{exc.code}：{exc.message} "
                      f"[{exc.source or '未知位置'}]", flush=True)
            except (OSError, UnicodeError) as exc:
                print(f"账本文件暂时无法读取：{exc}；保存完成后会自动重试。", flush=True)
                retry_unreadable = True
            current = _watch_snapshot(before, after, config)
            if current != snapshot:
                snapshot = current
                continue
            if retry_unreadable:
                time.sleep(3)
                snapshot = _watch_snapshot(before, after, config)
                continue
            while True:
                time.sleep(0.75)
                current = _watch_snapshot(before, after, config)
                if current != snapshot:
                    time.sleep(0.35)
                    snapshot = _watch_snapshot(before, after, config)
                    break
    except KeyboardInterrupt:
        print("\n自动复审已停止。")
        return 0


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
        try:
            setup = _setup_preview(after, month, cutover)
        except BudgetError as exc:
            result.update(status="REVIEW_INPUT_BLOCKED", stage="review_input",
                          error={"code": exc.code, "message": exc.message,
                                 "source": exc.source})
            return result
        result.update(status="REVIEW_CONFIG_REQUIRED", stage="config", setup=setup)
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
        setup = result["setup"]
        lines.append(f"截至 {setup['as_of']} 的首轮盘点（未作人工批准）：")
        lines.append(f"  账户语义和支付范围待审：{len(setup['accounts_to_review'])} 户")
        for row in setup["accounts_to_review"]:
            lines.append(f"    {row['account']} ({', '.join(row['commodities'])}) "
                         f"[{row['first_source']}]")
        lines.append(f"  本月未写行内 budget 的收支分录："
                     f"{setup['untagged_income_expense_postings']} 条")
        for row in setup["classification_groups"]:
            lines.append(f"    {row['account']}：{row['count']} 条 "
                         f"[首条 {row['first_source']}]")
        lines.append(setup["note"])
        lines.append("下一步：审定账户、期初用途、资产和负债归属、分类默认值与月度计划；"
                     "建立本月月报配置后，用原命令加 --config 配置路径重跑。")
        lines.append("逐笔分类候选：python -m "
                     "beancount_budget_tool.tools.v6_classification_proposals "
                     f"--ledger \"{check['after']}\" --month {result['month']} "
                     "--output review-candidates.json")
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
    parser.add_argument("--watch", action="store_true", help="保存账本或审阅 YAML 后自动重跑文本复审")
    args = parser.parse_args(argv)
    if args.watch:
        if args.format != "text":
            parser.error("--watch 仅支持 --format text；单次 JSON 输出请去掉 --watch")
        return watch_month(args.before, args.after, args.month, args.cutover,
                           args.config, args.allow_financial_corrections)
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

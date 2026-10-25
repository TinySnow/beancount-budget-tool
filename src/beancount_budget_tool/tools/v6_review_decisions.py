"""Review pending categories and H purposes without hand-copying source references."""

import argparse
import json
from pathlib import Path
import sys

from ..console import configure_output
from ..errors import BudgetError
from ..v6.monthly_report import render_workflow_text
from ..v6.input_compiler import (
    MONTH_SCALARS as SCALARS, read_month_config as _config, check_unchanged as _unchanged,
)
from ..v6.review import EDITABLE, ROW_FIELDS, create_review, apply_review


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--config", type=Path, help="导出该月分类与 H 资金用途待审表")
    action.add_argument("--apply", type=Path, help="保存已明确批准的待审表决定并重跑月报")
    parser.add_argument("--project", type=Path, help="使用项目最新月份；保存新配置后自动更新该月入口")
    parser.add_argument("--month", help="项目中的月份 YYYY-MM；省略时使用默认月份，仅用于导出填写表")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if not (args.config or args.apply or args.project):
        parser.error("请提供 --project、--config 或 --apply")
    if args.month and (not args.project or args.apply):
        parser.error("--month 须与 --project 一起用于导出；保存时按填写表引用的月份")
    if not args.apply and args.output is None:
        parser.error("导出待审表须提供 --output 新目录")
    try:
        context = None
        if args.project:
            from ..v6.project_actions import prepare_project_action
            context = prepare_project_action(args.project, config=args.config, worksheet=args.apply, month=args.month)
        config = context.config if context else args.config
        guard = {"expected_inputs": context.inputs} if context else {}
        result = apply_review(args.apply, args.output, **guard) if args.apply else create_review(config, args.output, **guard)
        if context:
            result = context.finish(result)
    except (BudgetError, OSError, UnicodeError) as exc:
        error = {"code": exc.code, "message": exc.message, "source": exc.source} if isinstance(exc, BudgetError) else {
            "code": "REVIEW_IO_ERROR", "message": str(exc), "source": str(args.apply or args.config or args.project)}
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "worksheet" in result:
        print(f"待审填写表：{result['worksheet']}\n可填分类／用途 {result['editable_items']} 项；"
              f"其他待审 {result['other_items']} 项。请查看同目录 START_HERE.md。")
    else:
        print(f"保存 {len(result['accepted'])} 项决定；配置：{result['config'] or '未生成（没有明确批准项）'}")
        if result.get("report_error"):
            error = result["report_error"]
            print(f"决定已保存，月报尚未通过：{error['code']} {error['message']} ({error['source']})")
        else:
            print(render_workflow_text(result["workflow"]))
    if args.format != "json":
        from ..v6.project_actions import print_project_result
        print_project_result(result)
    return 2 if result.get("report_error") or result.get("project_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prepare the next calendar month while retaining the original cutover and reviews."""

import argparse
import json
from pathlib import Path
import sys

from ..console import configure_output
from ..errors import BudgetError
from ..v6.monthly_report import render_workflow_text
from ..v6.month_actions import (
    DATED_INPUTS, FIELDS, next_month as _next, _decimal,
    create_next_month, compile_next_month, confirm_month,
)


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--config", type=Path, help="以此月配置准备紧接的下一月")
    action.add_argument("--apply", type=Path, help="核对并保存 next-month.yaml")
    action.add_argument("--confirm-completeness", type=Path, nargs="?", const=True, metavar="CONFIG",
                        help="确认月末没有未入账责任或在途付款；使用 --project 时可省略 CONFIG")
    parser.add_argument("--project", type=Path, help="使用项目最新月份；保存新配置后自动更新入口")
    parser.add_argument("--month", help="项目中的原月份 YYYY-MM；省略时使用默认月份，保存填写表时不使用")
    parser.add_argument("--reason", help="本月末资料完整性的核对依据，仅用于 --confirm-completeness")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if not (args.config or args.apply or args.confirm_completeness or args.project):
        parser.error("请提供 --project、--config、--apply 或 --confirm-completeness")
    if args.month and (not args.project or args.apply):
        parser.error("--month 须与 --project 一起准备或确认月份；保存时按填写表引用的原月份")
    if args.confirm_completeness is True and not args.project:
        parser.error("省略 --confirm-completeness 的 CONFIG 时须提供 --project")
    if not args.apply and args.output is None:
        parser.error("准备下一月或保存月末声明须提供 --output 新目录")
    if bool(args.confirm_completeness) != bool(args.reason):
        parser.error("--confirm-completeness 必须同时提供 --reason，其他操作不使用 --reason")
    try:
        context = None
        if args.project:
            from ..v6.project_actions import prepare_project_action
            base = args.confirm_completeness if isinstance(args.confirm_completeness, Path) else args.config
            context = prepare_project_action(args.project, config=base, worksheet=args.apply,
                                             month=args.month, successor=not bool(args.confirm_completeness))
        config = context.config if context else args.config or args.confirm_completeness
        guard = {"expected_inputs": context.inputs} if context else {}
        if args.confirm_completeness:
            result = confirm_month(config, args.reason, args.output, **guard)
        else:
            result = compile_next_month(args.apply, args.output, **guard) if args.apply else create_next_month(config, args.output, **guard)
        if context:
            result = context.finish(result)
    except (BudgetError, OSError, UnicodeError) as exc:
        error = {"code": exc.code, "message": exc.message, "source": exc.source} if isinstance(exc, BudgetError) else {
            "code": "NEXT_MONTH_IO_ERROR", "message": str(exc),
            "source": str(args.apply or args.config or args.project or args.confirm_completeness)}
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "worksheet" in result:
        print(f"下一月 {result['month']} 填写表：{result['worksheet']}\n"
              f"待确认账户／币种 {result['changed_accounts']} 户，本月已记交易 {result['booked_transactions_in_month']} 条。")
        if not result["booked_transactions_in_month"]:
            print("本月没有已记交易，请核对 importer 是否已覆盖；生成草稿不代表资料完整。")
    elif result["config"] is None:
        print(f"{result['month']} 尚待确认，未生成配置：")
        for item in result["review_items"]:
            print(f"  {item['source']}：{item['detail']}")
    else:
        print(f"已保存配置：{result['config']}")
        if result.get("report_error"):
            error = result["report_error"]
            print(f"配置已保存，月报尚未通过：{error['code']} {error['message']} ({error['source']})")
        else:
            print(render_workflow_text(result["workflow"]))
    if args.format != "json":
        from ..v6.project_actions import print_project_result
        print_project_result(result)
    return 2 if result.get("report_error") or result.get("project_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

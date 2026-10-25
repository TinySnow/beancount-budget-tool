"""Create one editable first-month worksheet, then compile reviewed inputs."""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from ..console import command, configure_output
from ..errors import BudgetError
from ..v6.input_compiler import (
    SetupDumper as _SetupDumper, relative_reference as _ledger_reference,
    month_inputs as _inputs, publish_inputs as _publish, create_setup, compile_setup,
)


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--ledger", type=Path)
    action.add_argument("--apply", type=Path, help="检查已填写 setup.yaml 并编译月报配置")
    parser.add_argument("--month")
    parser.add_argument("--cutover", type=date.fromisoformat)
    parser.add_argument("--currency")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--income-purpose", default="已收入资金")
    parser.add_argument("--expense-purpose", default="待支出资金")
    parser.add_argument("--expense-category")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if args.ledger and (not args.month or not args.output):
        parser.error("生成草稿须提供 --month 和 --output")
    if args.apply and (args.month or args.cutover or args.currency or args.expense_category or
                       args.income_purpose != "已收入资金" or args.expense_purpose != "待支出资金"):
        parser.error("--apply 时请在 setup.yaml 内修改月份、默认科目和货币")
    try:
        result = compile_setup(args.apply, args.output) if args.apply else create_setup(
            args.ledger, args.month, args.output, cutover=args.cutover, currency=args.currency,
            income_purpose=args.income_purpose, expense_purpose=args.expense_purpose,
            expense_category=args.expense_category)
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message, "source": exc.source}},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(json.dumps({"error": {"code": "SETUP_IO_ERROR", "message": str(exc),
                                    "source": str(args.apply or args.output)}}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["status"])
        for item in result.get("review_items", result.get("source_issues", [])):
            print(f"  {item['detail']} [{item['source']}]")
        if "setup" in result:
            print(f"填写表：{result['setup']}")
            print(f"检查并生成配置：{command('beancount-budget-init')} --apply \"{result['setup']}\"")
        if result.get("config"):
            print(f"查看月报：{command('beancount-budget-tool')} --config \"{result['config']}\"")
        print(result.get("note", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

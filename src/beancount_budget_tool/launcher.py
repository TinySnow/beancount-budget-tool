"""One public launcher for the two-file budget workflow."""

import argparse
from importlib import import_module
import sys

from . import __version__
from .console import configure_output


# Subcommands mirror the installed console scripts.
COMMANDS = {
    "monthly": ("beancount_budget_tool.v6.monthly_report", "查看月报／TUI"),
    "report": ("beancount_budget_tool.v6.period_report", "区间报告／CSV 导出"),
    "configure": ("beancount_budget_tool.v6.simple_config", "创建／迁移 config.yml + budgets.yml 两文件入口"),
    "clean": ("beancount_budget_tool.v6.state_store", "清理缓存并将审阅记录收拢到单个文件"),
    "state": ("beancount_budget_tool.v6.state_store", "清理／导出／导入高级审阅状态"),
    "audit": ("beancount_budget_tool.v6.cli", "分项审计"),
    "history": ("beancount_budget_tool.v6.account_queries", "追查会计科目内预算用途的变动与余额"),
    "snapshot": ("beancount_budget_tool.v6.account_queries", "查看指定日日末的预算用途分布"),
}


def main(argv=None) -> int:
    configure_output()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        from .v6.input_compiler import default_config
        from .errors import BudgetError
        try:
            if default_config() is not None:
                return import_module(COMMANDS['monthly'][0]).main([])
        except BudgetError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if not args or args[0] in ("-h", "--help", "--version"):
        parser = argparse.ArgumentParser(
            description="Beancount budget v6：月报、待审与逐月审核",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="命令：\n" + "\n".join(f"  {key:25} {value[1]}" for key, value in COMMANDS.items()) +
                "\n\n示例（PowerShell）：\n"
                "  .\\beancount-budget-tool.exe configure --help\n"
                "  .\\beancount-budget-tool.exe --config .\\budget\\config.yml --format tui\n"
                "\n各命令加 --help 查看参数；省略命令时按 monthly 处理。")
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
        parser.parse_args(args or ["--help"])
        return 0
    if args[0] in {"init", "next-month", "project", "review", "review-month", "review-refs", "classification-proposals"}:
        import json
        print(json.dumps({"error": {"code": "COMMAND_RETIRED", "message":
            "旧填表／月份登记命令已下线。使用 configure 创建 config.yml + budgets.yml；monthly/TUI 直接切月和审阅；state --check 检查来源引用。"}}, ensure_ascii=False), file=sys.stderr)
        return 2
    command = args.pop(0) if args[0] in COMMANDS else "monthly"
    module = import_module(COMMANDS[command][0])
    entry = getattr(module, command + "_main" if command in {"history", "snapshot"} else "main")
    return entry(args) or 0

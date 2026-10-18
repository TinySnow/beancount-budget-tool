"""Single executable entry point; existing installed commands remain available."""

import argparse
from importlib import import_module
import sys

from . import __version__
from .console import configure_output


# Subcommands mirror the installed console scripts.
COMMANDS = {
    "monthly": ("beancount_budget_tool.v6.monthly_report", "查看月报／TUI"),
    "report": ("beancount_budget_tool.v6.period_report", "区间报告／CSV 导出"),
    "init": ("beancount_budget_tool.tools.v6_init", "生成／编译首月配置"),
    "project": ("beancount_budget_tool.v6.project", "登记月份，生成默认项目入口"),
    "review-month": ("beancount_budget_tool.tools.v6_review_migration_month", "逐月校对／监测改动"),
    "review": ("beancount_budget_tool.tools.v6_review_decisions", "生成／保存交易待审填写表"),
    "review-refs": ("beancount_budget_tool.tools.v6_review_source_refs", "检查审阅来源引用"),
    "next-month": ("beancount_budget_tool.tools.v6_next_month", "准备下一月／确认月末"),
    "audit": ("beancount_budget_tool.v6.cli", "分项审计"),
    "classification-proposals": ("beancount_budget_tool.tools.v6_classification_proposals", "导出分类候选"),
}


def main(argv=None) -> int:
    configure_output()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        from pathlib import Path
        from .v6.project import DEFAULT_PROJECT
        if (Path.cwd() / DEFAULT_PROJECT).is_file():
            return import_module(COMMANDS["monthly"][0]).main([])
    if not args or args[0] in ("-h", "--help", "--version"):
        parser = argparse.ArgumentParser(
            description="Beancount budget v6：月报、待审与逐月审核",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="命令：\n" + "\n".join(f"  {key:25} {value[1]}" for key, value in COMMANDS.items()) +
                "\n\n示例（PowerShell）：\n"
                "  .\\beancount-budget-tool.exe init --help\n"
                "  .\\beancount-budget-tool.exe --config .\\budget\\month.yaml --format tui\n"
                "\n各命令加 --help 查看参数；省略命令时按 monthly 处理。")
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
        parser.parse_args(args or ["--help"])
        return 0
    command = args.pop(0) if args[0] in COMMANDS else "monthly"
    module = import_module(COMMANDS[command][0])
    return module.main(args) or 0

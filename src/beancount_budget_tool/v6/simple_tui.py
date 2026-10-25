"""The existing browser with automatic two-file input preparation."""

import curses
from pathlib import Path
import sys

from ..errors import BudgetError, require
from ..yamlstrict import load_yaml
from .monthly_report import audit_month_workflow, render_workflow_text
from .simple_config import build_runtime, save_snapshot, state_dir
from .tui import ReportBrowser, _report_browser
from .targets import _month


class SimpleReportBrowser(ReportBrowser):
    def __init__(self, config: Path, month: str | None = None):
        self.user_config = config.resolve()
        self.selected_month = _month(month or load_yaml(config)["month"])
        self.runtime = None
        self.range_runtimes = []
        super().__init__([config], audit_month_workflow, render_workflow_text)
        # Transaction details remain available just as in the normal browser.
        self.loader = lambda path: audit_month_workflow(path, include_transactions=True)

    @property
    def display_config(self):
        return self.user_config

    def refresh(self):
        self.clear()
        self.range_runtimes = []
        try:
            self.runtime = build_runtime(self.user_config, self.selected_month)
            if self.runtime.pending:
                self.period = False
                self._accept_outcome(self.runtime.pending, self.renderer)
                return
            self.configs, self.index = [self.runtime.config], 0
            self.review_roots = [state_dir(self.user_config) / "reviews"]
            super().refresh()
            self.runtime.check()
            for runtime in self.range_runtimes:
                runtime.check()
            if self.outcome:
                outcome = self.runtime.public_sources(self.outcome)
                for runtime in self.range_runtimes:
                    outcome = runtime.public_sources(outcome)
                self._accept_outcome(outcome, self._scope_renderer())
        except (BudgetError, OSError, UnicodeError) as exc:
            self.clear()
            self.error = exc if isinstance(exc, BudgetError) else BudgetError("SIMPLE_INPUT_ERROR", str(exc), str(self.user_config))

    def _scope_renderer(self):
        if self.salary_cycle is not None and self.period:
            from .salary_cycle import render_text
            return render_text
        if self.period:
            from .period_report import render_text
            return render_text
        return self.renderer

    def jump_month(self, month):
        month = _month(month)
        require(month >= load_yaml(self.user_config)["cutover"][:7], "MONTH_INVALID", "月份不得早于 cutover", str(self.user_config))
        self.selected_month, self.period = month, False
        self.filter("")
        self.clear()

    def advance_month(self, direction=1):
        year, month = map(int, self.selected_month.split("-"))
        ordinal = year * 12 + month - 1 + direction
        self.jump_month(f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}")

    def switch(self, direction):
        try:
            self.advance_month(direction)
            return True
        except BudgetError:
            return False

    def month_choices(self):
        return [(self.selected_month, self.config)]

    def show_saved_month(self, result):
        save_snapshot(self.user_config, Path(result["config"]), expected=self.runtime.inputs)
        self.period = False
        self.filter("")
        self.refresh()

    def period_configs(self):
        from .period_report import _months
        if self.salary_cycle is not None:
            from .salary_cycle import cycle_bounds
            _, last = cycle_bounds(*self.salary_cycle[:2])
            months = [last.isoformat()[:7]]
        else:
            first, last = self.period_range or (load_yaml(self.user_config)["cutover"][:7], self.selected_month)
            months = _months(first, last)
        self.range_runtimes = [build_runtime(self.user_config, month) for month in months]
        for runtime in self.range_runtimes:
            if runtime.pending:
                item = runtime.pending["review_items"][0]
                raise BudgetError("SIMPLE_CONFIG_REVIEW", f"{runtime.month}：{item['detail']}", item["source"])
        return [runtime.config for runtime in self.range_runtimes]

    def period_label(self):
        if self.period and self.salary_cycle is None and self.period_range is None:
            return f"区间 {load_yaml(self.user_config)['cutover'][:7]} 至 {self.selected_month}"
        return super().period_label()

    def open_review_month(self):
        if not self.period or self.view != "reviews" or self.error or self.help or not self.reviews():
            return False
        item = self.detail or self.reviews()[max(0, min(self.selected, len(self.reviews()) - 1))]
        month = item.get("month") or (item.get("date") or "")[:7]
        if not month:
            return False
        self.jump_month(month)
        return True


def run_simple_tui(config: Path, month: str | None = None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BudgetError("TUI_TERMINAL_REQUIRED", "TUI 需要交互式终端；脚本中请使用 text 或 json")
    browser = SimpleReportBrowser(config, month)
    try:
        curses.wrapper(_report_browser, browser)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        raise BudgetError("TUI_UNAVAILABLE", "当前终端无法打开 TUI") from exc

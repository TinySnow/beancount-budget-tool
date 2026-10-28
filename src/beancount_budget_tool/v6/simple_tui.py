"""The existing browser with automatic two-file input preparation."""

import curses
from pathlib import Path
import sys

from ..errors import BudgetError, require
from ..financial import FinancialSourceCache, financial_session
from ..query import QuerySession
from .facts import frozen_fact_indices
from ..yamlstrict import load_yaml
from .monthly_report import audit_month_workflow, render_workflow_text
from .simple_config import build_runtime, save_snapshot
from .tui import ReportBrowser, _report_browser
from .targets import _month


class SimpleReportBrowser(ReportBrowser):
    def __init__(self, config: Path, month: str | None = None):
        self.user_config = config.resolve()
        self.selected_month = _month(month or load_yaml(config)["month"])
        self.runtime = None
        self.range_runtimes = []
        self.saved_cycle = None
        self.visited_months = {self.selected_month}
        self.source_cache = FinancialSourceCache()
        super().__init__([config], audit_month_workflow, render_workflow_text)
        # Transaction details remain available just as in the normal browser.
        self.loader = lambda path: audit_month_workflow(path, include_transactions=True)

    @property
    def display_config(self):
        return self.user_config

    def close(self):
        if self.runtime is not None:
            self.runtime.close()
        for runtime in self.range_runtimes:
            runtime.close()

    def refresh(self):
        self.close()
        self.clear()
        self.range_runtimes = []
        try:
            with QuerySession(source_cache=self.source_cache):
                if self.period and self.saved_cycle is not None:
                    from .salary_plan import audit_configured_cycle
                    from .salary_cycle import render_text
                    self.configs, self.index = [self.user_config], 0
                    cutoff = self._saved_cutoff(self.saved_cycle)
                    self._accept_outcome(audit_configured_cycle(self.user_config, self.saved_cycle, cutoff.isoformat()), render_text)
                    return
                self.runtime = build_runtime(self.user_config, self.selected_month)
                if self.runtime.pending:
                    self.period = False
                    self._accept_outcome(self.runtime.pending, self.renderer)
                    return
                self.configs, self.index = [self.runtime.config], 0
                self.review_roots = [Path(self.runtime.temporary.name) / "reviews"]
                super().refresh()
                self.runtime.check()
                for runtime in self.range_runtimes:
                    runtime.check()
                if self.outcome:
                    outcome = self.runtime.public_sources(self.outcome)
                    for runtime in self.range_runtimes:
                        runtime.rewrite_sources(outcome)
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
        self.visited_months.add(month)
        self.saved_cycle = None
        self.salary_cycle = None
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
        return [(month, self.user_config) for month in sorted(self.visited_months)]

    def show_saved_month(self, result):
        try:
            save_snapshot(self.user_config, Path(result["config"]), expected=self.runtime.inputs)
        except BaseException as exc:
            # Do not erase a newly approved receipt if persistence failed.
            self.runtime.temporary.keep = True
            if isinstance(exc, (BudgetError, OSError, UnicodeError, TypeError)):
                raise BudgetError("SIMPLE_STATE_SAVE_FAILED",
                    f"审阅状态未覆盖；新决定保留在 {result['config']}。原因：{exc}", str(self.user_config)) from exc
            raise
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
        if self.period and self.saved_cycle is not None:
            return f"已保存发薪周期 {self.saved_cycle}"
        if self.period and self.salary_cycle is None and self.period_range is None:
            return f"区间 {load_yaml(self.user_config)['cutover'][:7]} 至 {self.selected_month}"
        return super().period_label()

    def configured_cycle_choices(self):
        from .simple_config import _documents
        from .salary_plan import read_cycles, month_end, window
        doc, path, plans = _documents(self.user_config)
        cycles = read_cycles(plans, path, default_category=doc.get("spendable_cap_category", doc["defaults"]["expense_category"]))
        if not cycles:
            return [], None
        chosen = window(cycles, month_end(self.selected_month))
        labels = [(start, "，".join(f"{name} {amount if amount is not None else '待定'}"
                                  for name, amount in row["expense_targets"].items()))
                  for start, row in cycles.items()]
        return labels, chosen[0] if chosen else None

    def select_saved_cycle(self, start):
        choices, _ = self.configured_cycle_choices()
        require(start in dict(choices), "SALARY_CYCLE_UNKNOWN", "发薪日尚未在 budgets.yml 登记", str(self.user_config))
        self.saved_cycle = start
        from datetime import timedelta
        cutoff = self._saved_cutoff(start)
        self.salary_cycle = (start, (cutoff + timedelta(days=1)).isoformat(), None, None)
        self.period, self.view = True, "report"
        self.filter("")
        self.clear()

    def _saved_cutoff(self, start):
        from datetime import date, timedelta
        from .simple_config import _documents
        from .salary_plan import read_cycles, month_end
        doc, path, plans = _documents(self.user_config)
        cycles = read_cycles(plans, path, default_category=doc.get("spendable_cap_category", doc["defaults"]["expense_category"]))
        following = next((day for day in cycles if day > start), None)
        return date.fromisoformat(following) - timedelta(days=1) if following else month_end(self.selected_month)

    def select_period(self, *args):
        self.saved_cycle = None
        return super().select_period(*args)

    def select_salary_cycle(self, *args):
        self.saved_cycle = None
        return super().select_salary_cycle(*args)

    def open_review_month(self):
        if not self.period or self.view != "reviews" or self.error or self.help or not self.reviews():
            return False
        item = self.detail or self.reviews()[max(0, min(self.selected, len(self.reviews()) - 1))]
        month = item.get("month") or (item.get("date") or "")[:7]
        if not month:
            return False
        self.jump_month(month)
        return True


def run_simple_tui(config: Path, month: str | None = None, *, watch=False):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BudgetError("TUI_TERMINAL_REQUIRED", "TUI 需要交互式终端；脚本中请使用 text 或 json")
    browser = SimpleReportBrowser(config, month)
    browser.watch = watch
    try:
        curses.wrapper(_report_browser, browser)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        raise BudgetError("TUI_UNAVAILABLE", "当前终端无法打开 TUI") from exc
    finally:
        browser.close()

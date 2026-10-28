"""Interval browsing reuses real audits and returns to pending months safely."""

from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.period_report import audit_period, render_csv
from beancount_budget_tool.v6.tui import ReportBrowser, _draw_browser, _export_report, _report_browser, _select_period
from test_v6_tui import FakeScreen


class IntervalBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.base = Path(temporary.name).resolve()
        example = Path(__file__).resolve().parents[1] / "examples/v6/acceptance/run.py"
        cls.runner = runpy.run_path(str(example))
        cls.demo = cls.runner["build_demo"](cls.base / "demo")
        cls.configs = list(cls.demo["configs"].values())
        cls.originals = {path: path.read_bytes() for path in cls.base.rglob("*") if path.is_file()}

    def tearDown(self):
        for path, content in self.originals.items():
            path.write_bytes(content)

    def browser(self, configs=None):
        return ReportBrowser(configs or self.configs, audit_month_workflow, render_workflow_text)

    def screen(self, browser):
        screen = FakeScreen(size=(100, 180))
        _draw_browser(screen, browser)
        return "\n".join(value for _, _, value in screen.rows)

    def make_pending(self):
        config = self.configs[1]
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        document.pop("completeness")
        self.runner["save"](config, document)

    def test_real_three_month_comparison_preserves_flows_targets_and_closing_stock(self):
        browser = self.browser(list(reversed(self.configs)))
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-12")
        browser.toggle_period()
        browser.refresh()
        self.assertTrue(browser.period)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual([row["month"] for row in browser.outcome["months"]],
                         ["2026-10", "2026-11", "2026-12"])
        report = browser.outcome["report"]
        self.assertEqual(report["expense_totals"]["direct_spent"], "1341")
        self.assertEqual(report["closing"]["max_spendable"]["max_spendable"], "1160")
        text = self.screen(browser)
        self.assertIn("2026-10 至 2026-12", text)
        self.assertIn("净额 1341 CNY", text)
        self.assertIn("2026-11 | 20 | 760 | -740 | 1160 | 1160", text)
        self.assertIn("逐月目标比较", text)
        self.assertIn("不加总", text)
        self.assertFalse(browser.has_transactions())
        browser.toggle_transactions()
        self.assertEqual(browser.view, "report")
        browser.filter("旅游")
        self.assertTrue(all(line in browser.report_lines() for line in browser.lines if "警告" in line))
        browser.toggle_period()
        browser.refresh()
        self.assertFalse(browser.period)
        self.assertEqual(browser.query, "")
        self.assertEqual(browser.outcome["month"], "2026-12")

    def test_any_pending_month_hides_every_interval_number_and_links_to_that_month(self):
        browser = self.browser()
        browser.toggle_period()
        browser.refresh()
        self.assertIn("净额 1341 CNY", "\n".join(browser.lines))
        self.make_pending()
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(browser.outcome["report"])
        self.assertNotIn("1341", "\n".join(browser.lines))
        self.assertEqual(browser.view, "reviews")
        self.assertEqual({item["month"] for item in browser.reviews()}, {"2026-11"})
        self.assertTrue(all(item["source"] for item in browser.reviews()))
        browser.filter("不存在")
        text = self.screen(browser)
        self.assertIn("匹配 0 项", text)
        self.assertNotIn("待审总数 0", text)
        self.assertFalse(browser.open_review_month())
        browser.filter("2026-11")
        browser.open_detail()
        self.assertIn("m 转到该月", self.screen(browser))
        self.assertTrue(browser.open_review_month())
        self.assertFalse(browser.period)
        self.assertEqual(browser.config, self.configs[1])
        self.assertIsNone(browser.outcome)
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-11")
        self.assertIsNone(browser.outcome["report"])

    def test_missing_month_rejects_interval_then_current_month_can_be_reopened(self):
        browser = self.browser([self.configs[0], self.configs[2]])
        browser.refresh()
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.error.code, "PERIOD_MONTHS_INCOMPLETE")
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        text = self.screen(browser)
        self.assertIn("2026-11", text)
        self.assertNotIn("最多能花：880", text)
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-10")
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")

    def test_edited_config_clears_interval_and_recovery_reloads_every_month(self):
        browser = self.browser()
        browser.toggle_period()
        browser.refresh()
        config = self.configs[1]
        config.write_text(config.read_text(encoding="utf-8") + "schema: 6\n", encoding="utf-8")
        browser.refresh()
        self.assertEqual(browser.error.code, "YAML_DUPLICATE_KEY")
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        self.assertNotIn("1341", self.screen(browser))
        config.write_bytes(self.originals[config])
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["report"]["expense_totals"]["direct_spent"], "1341")

    def test_ledger_changes_are_not_replaced_by_cached_monthly_results(self):
        browser = self.browser()
        browser.toggle_period()
        browser.refresh()
        ledger = self.demo["ledger"]
        ledger.write_text(ledger.read_text(encoding="utf-8") +
                          '\n2026-12-10 * "new December food"\n'
                          '  Assets:Bank:Daily -10 CNY\n'
                          '  Expenses:Daily 10 CNY\n', encoding="utf-8")
        # New default category/purpose facts must be recomputed from the ledger.
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        report = browser.outcome["report"]
        self.assertEqual(report["expense_totals"]["direct_spent"], "1351")
        self.assertEqual(report["closing"]["max_spendable"]["max_spendable"], "1150")
        self.assertIn("2026-10 | 2081 | 0 | 2081 | 880 | 880", "\n".join(browser.lines))

    def test_keyboard_recomputes_interval_and_jumps_to_selected_pending_month(self):
        self.make_pending()
        browser = self.browser()
        screen = FakeScreen(keys=("c", "r", "\n", "e", "\n", "m", "q"), size=(20, 160))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"), \
             patch("beancount_budget_tool.v6.review.prepare_review") as prepare:
            _report_browser(screen, browser)
        prepare.assert_not_called()
        self.assertFalse(browser.period)
        self.assertEqual(browser.config, self.configs[1])
        self.assertEqual(browser.outcome["month"], "2026-11")
        self.assertTrue(any("区间待审详情" in value for frame in screen.frames for _, _, value in frame))
        self.assertTrue(any("区间 正在重算" in value for frame in screen.frames for _, _, value in frame))

    def test_switching_config_leaves_interval_and_help_lists_all_latest_paths(self):
        browser = self.browser()
        browser.toggle_period()
        browser.refresh()
        browser.help = True
        text = self.screen(browser)
        self.assertIn("区间使用的最新配置", text)
        for path in self.configs:
            self.assertIn(str(path), text)
        self.assertTrue(browser.switch(1))
        self.assertFalse(browser.period)
        self.assertIsNone(browser.outcome)
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-11")

    def test_interval_uses_replacement_configuration_instead_of_original(self):
        browser = self.browser()
        original = self.configs[1]
        latest = original.with_name("latest-month.yaml")
        latest.write_bytes(original.read_bytes())
        self.addCleanup(latest.unlink)
        browser.configs[1] = latest
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.outcome["months"][1]["config"], str(latest))
        self.assertNotIn("TUI 已保存", self.screen(browser))

    def test_selected_range_audits_only_two_months_and_keeps_closing_stock_once(self):
        browser = self.browser(list(reversed(self.configs)))
        browser.select_period("2026-11", "2026-12")
        with patch("beancount_budget_tool.v6.period_report.audit_month_workflow", wraps=audit_month_workflow) as audit:
            browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual([call.args[0] for call in audit.call_args_list], self.configs[1:])
        self.assertEqual(browser.outcome, audit_period(self.configs[1:], "2026-11", "2026-12"))
        report = browser.outcome["report"]
        self.assertEqual(report["expense_totals"]["direct_spent"], "-740")
        self.assertEqual(report["closing"]["max_spendable"]["max_spendable"], "1160")
        self.assertEqual(set(browser.period_inputs), set(self.configs[1:]))
        self.assertIn("区间 2026-11 至 2026-12", self.screen(browser))
        browser.help = True
        text = self.screen(browser)
        self.assertNotIn(str(self.configs[0]), text)
        self.assertIn(str(self.configs[1]), text)

    def test_one_month_range_and_returning_from_monthly_keep_the_query(self):
        browser = self.browser()
        browser.select_period("2026-10", "2026-10")
        browser.refresh()
        self.assertEqual(browser.outcome["report"]["expense_totals"]["direct_spent"], "2081")
        self.assertEqual(browser.outcome["report"]["closing"]["max_spendable"]["max_spendable"], "880")
        browser.toggle_period()
        browser.refresh()
        self.assertFalse(browser.period)
        self.assertEqual(browser.outcome["month"], "2026-10")
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.period_range, ("2026-10", "2026-10"))
        self.assertEqual(len(browser.outcome["months"]), 1)

    def test_pending_month_outside_range_does_not_block_selected_months(self):
        original = self.configs[0]
        document = yaml.safe_load(original.read_text(encoding="utf-8"))
        document.pop("completeness")
        self.runner["save"](original, document)
        browser = self.browser()
        browser.select_period("2026-11", "2026-12")
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["review_items"], [])
        browser.select_period()
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(browser.outcome["report"])
        self.assertEqual({item["month"] for item in browser.reviews()}, {"2026-10"})

    def test_selected_pending_month_links_to_review_and_returns_to_same_range(self):
        self.make_pending()
        browser = self.browser()
        browser.select_period("2026-11", "2026-12")
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(browser.outcome["report"])
        self.assertTrue(browser.open_review_month())
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-11")
        self.configs[1].write_bytes(self.originals[self.configs[1]])  # Restore only this public fixture's statement.
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.period_range, ("2026-11", "2026-12"))
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["report"]["expense_totals"]["direct_spent"], "-740")

    def test_missing_range_month_fails_then_a_valid_subrange_recovers(self):
        browser = self.browser([self.configs[0], self.configs[2]])
        browser.select_period("2026-10", "2026-12")
        browser.refresh()
        self.assertEqual(browser.error.code, "PERIOD_MONTHS_INCOMPLETE")
        self.assertIn("2026-11", browser.error.message)
        self.assertTrue(browser.error.source.startswith(str(self.configs[0]) + ":"))
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        self.assertIn("区间 2026-10 至 2026-12", self.screen(browser))
        browser.select_period("2026-12", "2026-12")
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["report"]["expense_totals"]["direct_spent"], "0")

    def test_duplicate_month_is_rejected_only_when_inside_the_selected_range(self):
        duplicate = self.configs[1].with_name("duplicate-month.yaml")
        duplicate.write_bytes(self.configs[1].read_bytes())
        self.addCleanup(duplicate.unlink)
        browser = self.browser([*self.configs, duplicate])
        browser.select_period("2026-11", "2026-12")
        browser.refresh()
        self.assertEqual(browser.error.code, "PERIOD_MONTH_DUPLICATE")
        self.assertIsNone(browser.outcome)
        browser.select_period("2026-12", "2026-12")
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")

    def test_cancel_or_invalid_bounds_do_not_change_active_results_or_filter(self):
        browser = self.browser()
        browser.select_period("2026-11", "2026-12")
        browser.refresh()
        browser.filter("旅游")
        original = browser.outcome
        for answers in ((None,), ("2026-10", None), ("invalid", "2026-12"), ("2026-12", "2026-11")):
            with self.subTest(answers=answers), patch("beancount_budget_tool.v6.tui._read_field", side_effect=answers), \
                 patch("beancount_budget_tool.v6.tui._message"), \
                 patch("beancount_budget_tool.v6.period_report.audit_period") as audit:
                _select_period(FakeScreen(), browser)
            audit.assert_not_called()
            self.assertIs(browser.outcome, original)
            self.assertEqual(browser.period_range, ("2026-11", "2026-12"))
            self.assertEqual(browser.query, "旅游")
        with self.assertRaises(BudgetError):
            browser.select_period("2026-11", None)
        self.assertIs(browser.outcome, original)

    def test_real_p_key_selects_range_and_c_preserves_it(self):
        browser = self.browser()
        screen = FakeScreen(keys=("p", "\x15", "2026-11", "\n", "\x15", "2026-12", "\n", "c", "c", "q"), size=(30, 160))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertTrue(browser.period)
        self.assertEqual(browser.period_range, ("2026-11", "2026-12"))
        self.assertEqual(browser.outcome["report"]["expense_totals"]["direct_spent"], "-740")
        self.assertTrue(any("p 选择范围" in value for frame in screen.frames for _, _, value in frame))

    def test_all_option_restores_full_range(self):
        browser = self.browser()
        browser.select_period("2026-11", "2026-12")
        browser.refresh()
        with patch("beancount_budget_tool.v6.tui._read_field", return_value=" ALL ") as prompt:
            _select_period(FakeScreen(), browser)
        self.assertEqual(prompt.call_count, 1)
        self.assertIsNone(browser.period_range)
        self.assertEqual(len(browser.outcome["months"]), 3)
        self.assertEqual(browser.outcome["report"]["expense_totals"]["direct_spent"], "1341")

    def test_export_csv_uses_selected_range_ignoring_display_filter(self):
        browser = self.browser()
        browser.select_period("2026-11", "2026-12")
        browser.refresh()
        browser.filter("no matching row")
        exported = self.base / "selected-range.csv"
        self.addCleanup(lambda: exported.unlink(missing_ok=True))
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["csv", str(exported)]), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _export_report(FakeScreen(), browser)
        self.assertEqual(message.call_args.args[1], "已导出")
        self.assertEqual(exported.read_bytes(), render_csv(audit_period(self.configs[1:], "2026-11", "2026-12")).encode("utf-8-sig"))
        self.assertEqual(browser.query, "no matching row")



if __name__ == "__main__":
    unittest.main()

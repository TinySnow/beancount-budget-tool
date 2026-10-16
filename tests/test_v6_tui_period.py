"""Interval browsing reuses real audits and returns to pending months safely."""

from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.tui import ReportBrowser, _draw_browser, _report_browser
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
        screen = FakeScreen(keys=("c", "r", "\n", "e", "m", "q"), size=(20, 160))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"), \
             patch("beancount_budget_tool.v6.tui_review.prepare_review") as prepare:
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
        browser.saved.add(1)
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.outcome["months"][1]["config"], str(latest))
        self.assertIn("包含 TUI 已保存的新配置", self.screen(browser))


if __name__ == "__main__":
    unittest.main()

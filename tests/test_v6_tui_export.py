"""TUI exports fresh full audits, with the same exact public CLI formats."""

import codecs
import csv
from functools import partial
from io import StringIO
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.period_report import audit_period, render_csv
from beancount_budget_tool.v6.tui import ReportBrowser, _export_report, _report_browser
from beancount_budget_tool.v6.tui_export import write_export
from test_v6_tui import FakeScreen


class ReportExportTests(unittest.TestCase):
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

    def browser(self, *, period=False):
        browser = ReportBrowser(self.configs, partial(audit_month_workflow, include_transactions=True),
                                render_workflow_text)
        if period:
            browser.toggle_period()
        browser.refresh()
        self.assertIsNone(browser.error)
        return browser

    def destination(self, extension):
        return self.base / (self._testMethodName + extension)

    def export(self, browser, path, format):
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=[format, str(path)]), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _export_report(FakeScreen(size=(20, 120)), browser)
        return message.call_args.args[1:]

    def test_monthly_json_contains_full_unfiltered_audited_sources(self):
        browser = self.browser()
        browser.toggle_transactions()
        browser.filter("no matching transaction")
        path = self.destination(".json")
        title, _ = self.export(browser, path, " JSON ")
        self.assertEqual(title, "已导出")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, browser.outcome)
        self.assertGreater(saved["report"]["transactions"]["count"], 0)
        self.assertEqual(browser.query, "no matching transaction")
        self.assertEqual(browser.view, "transactions")

    def test_monthly_csv_is_identical_to_one_month_cli_csv_and_has_utf8_bom(self):
        browser = self.browser()
        browser.filter("旅游")
        path = self.destination(".csv")
        self.assertEqual(self.export(browser, path, "csv")[0], "已导出")
        expected = render_csv(audit_period([browser.config])).encode("utf-8-sig")
        self.assertEqual(path.read_bytes(), expected)
        self.assertTrue(path.read_bytes().startswith(codecs.BOM_UTF8))
        self.assertIn("生活费", path.read_text(encoding="utf-8-sig"))

    def test_interval_csv_exports_all_months_and_preserves_closing_stock(self):
        browser = self.browser(period=True)
        browser.filter("no matching row")
        path = self.destination(".csv")
        self.assertEqual(self.export(browser, path, "csv")[0], "已导出")
        self.assertEqual(path.read_bytes(), render_csv(audit_period(self.configs)).encode("utf-8-sig"))
        rows = list(csv.DictReader(StringIO(path.read_text(encoding="utf-8-sig"))))
        closing = next(row for row in rows if row["scope"] == "closing" and row["metric"] == "max_spendable")
        self.assertEqual(closing["value"], "1160")
        self.assertEqual({row["month"] for row in rows if row["scope"] == "month_end"},
                         {"2026-10", "2026-11", "2026-12"})

    def test_pending_month_exports_only_review_results_in_every_format(self):
        browser = self.browser()
        config = browser.config
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        document.pop("completeness")
        self.runner["save"](config, document)
        for format in ("text", "json", "csv"):
            with self.subTest(format=format):
                path = self.destination("." + format)
                title, lines = self.export(browser, path, format)
                self.assertEqual(title, "已导出")
                self.assertIn("不含局部预算数字", "\n".join(lines))
                self.assertIsNone(browser.outcome["report"])
                content = path.read_text(encoding="utf-8-sig")
                self.assertIn("EXTERNAL_SCOPE_PENDING", content)
                self.assertNotIn("最多能花：880", content)
                if format == "json":
                    self.assertIsNone(json.loads(content)["report"])
                elif format == "csv":
                    self.assertTrue(all(row["section"] in {"audit", "review"}
                                        for row in csv.DictReader(StringIO(content))))

    def test_pending_interval_never_exports_passing_month_metrics(self):
        browser = self.browser(period=True)
        config = self.configs[1]
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        document.pop("completeness")
        self.runner["save"](config, document)
        path = self.destination(".csv")
        self.assertEqual(self.export(browser, path, "csv")[0], "已导出")
        self.assertIsNone(browser.outcome["report"])
        rows = list(csv.DictReader(StringIO(path.read_text(encoding="utf-8-sig"))))
        self.assertTrue(all(row["section"] in {"audit", "review"} for row in rows))
        self.assertEqual({row["month"] for row in rows if row["section"] == "review"}, {"2026-11"})

    def test_ledger_saved_during_export_form_is_reaudited_once_before_write(self):
        browser = self.browser()
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "880")
        path = self.destination(".json")
        def form(_screen, title, *_args):
            if title == "导出报表":
                return "json"
            ledger = self.demo["ledger"]
            ledger.write_text(ledger.read_text(encoding="utf-8") +
                              '\n2026-10-20 * "new food saved during form"\n'
                              '  Assets:Bank:Daily -10 CNY\n  Expenses:Daily 10 CNY\n', encoding="utf-8")
            return str(path)
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=form), \
             patch("beancount_budget_tool.v6.tui._message"), \
             patch.object(browser, "loader", wraps=browser.loader) as load:
            _export_report(FakeScreen(size=(20, 120)), browser)
        self.assertEqual(load.call_count, 1)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["report"]["max_spendable"]["max_spendable"], "870")
        self.assertEqual(saved, browser.outcome)

    def test_cancelling_at_either_field_does_not_write_or_recalculate(self):
        browser = self.browser()
        original = browser.outcome
        for fields in ([None], ["text", None]):
            with self.subTest(fields=fields), \
                 patch("beancount_budget_tool.v6.tui._read_field", side_effect=fields), \
                 patch.object(browser, "refresh") as refresh, \
                 patch("beancount_budget_tool.v6.tui_export.write_export") as write:
                _export_report(FakeScreen(), browser)
                refresh.assert_not_called()
                write.assert_not_called()
                self.assertIs(browser.outcome, original)

    def test_existing_files_including_input_ledger_are_not_overwritten(self):
        browser = self.browser()
        for path in (browser.config, self.demo["ledger"]):
            with self.subTest(path=path):
                original = path.read_bytes()
                with patch.object(browser, "refresh") as refresh:
                    title, lines = self.export(browser, path, "json")
                self.assertEqual(title, "导出失败")
                self.assertIn("TUI_EXPORT_EXISTS", "\n".join(lines))
                self.assertEqual(path.read_bytes(), original)
                refresh.assert_not_called()

    def test_late_collision_after_audit_is_rejected_by_exclusive_create(self):
        browser = self.browser()
        path = self.destination(".json")
        refresh = browser.refresh
        def race():
            refresh()
            path.write_text("another export", encoding="utf-8")
        with patch.object(browser, "refresh", side_effect=race):
            title, lines = self.export(browser, path, "json")
        self.assertEqual(title, "导出失败")
        self.assertIn("TUI_EXPORT_EXISTS", "\n".join(lines))
        self.assertEqual(path.read_text(encoding="utf-8"), "another export")

    def test_invalid_config_clears_old_numbers_and_does_not_create_export(self):
        browser = self.browser()
        browser.config.write_text(browser.config.read_text(encoding="utf-8") + "schema: 6\n", encoding="utf-8")
        path = self.destination(".json")
        title, lines = self.export(browser, path, "json")
        self.assertEqual(title, "导出失败")
        self.assertIn("YAML_DUPLICATE_KEY", "\n".join(lines))
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        self.assertFalse(path.exists())

    def test_bad_format_or_unwritable_parent_returns_to_browser(self):
        browser = self.browser()
        with patch("beancount_budget_tool.v6.tui._read_field", return_value="unknown"), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _export_report(FakeScreen(), browser)
        self.assertIn("TUI_EXPORT_FORMAT_INVALID", "\n".join(message.call_args.args[2]))
        path = self.base / "missing-folder" / "report.csv"
        self.assertEqual(self.export(browser, path, "csv")[0], "导出失败")
        self.assertFalse(path.exists())
        self.assertIsNotNone(browser.outcome)
        with self.assertRaises(BudgetError):
            write_export(browser.outcome, browser.config, self.destination(".invalid"), "unknown", period=False)

    def test_native_key_flow_can_save_chinese_path_and_continue_browsing(self):
        browser = self.browser()
        path = self.destination("-月报.csv")
        screen = FakeScreen(keys=("x", "\x15", *"csv", "\n", "\x15", *str(path), "\n", "\n", "a", "q"),
                            size=(24, 140))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertTrue(path.is_file())
        self.assertEqual(browser.view, "transactions")
        self.assertTrue(any("已导出" in value for frame in screen.frames for _, _, value in frame))


if __name__ == "__main__":
    unittest.main()

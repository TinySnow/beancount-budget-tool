"""Next-month preparation and worksheet loading stay inside one TUI session."""

from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_init import _SetupDumper, compile_setup
from beancount_budget_tool.tools.v6_next_month import compile_next_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.tui import ReportBrowser, _next_month, _report_browser
from beancount_budget_tool.v6.tui_next_month import load_next_month, month_workspace, prepare_next_month
from test_v6_tui import FakeScreen


class NextMonthTuiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        example = Path(__file__).resolve().parents[1] / "examples/v6"
        self.ledger = self.base / "账本 空格.bean"
        self.ledger.write_text((example / "first-month.bean").read_text(encoding="utf-8") +
                               '\n2026-10-05 * "下一月餐饮"\n'
                               '  Expenses:Food 20 CNY\n  Assets:Cash -20 CNY\n', encoding="utf-8")
        setup = yaml.safe_load((example / "first-month-setup.yaml").read_text(encoding="utf-8"))
        setup["ledger"] = self.ledger.name
        self.write(self.base / "setup.yaml", setup)
        self.config = Path(compile_setup(self.base / "setup.yaml")["config"])
        self.originals = {path: path.read_bytes() for path in [self.ledger, *self.config.parent.glob("*.yaml")]}

    def write(self, path, document):
        path.write_text(yaml.dump(document, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def browser(self, configs=None):
        browser = ReportBrowser(configs or [self.config], partial(audit_month_workflow, include_transactions=True),
                                render_workflow_text)
        browser.refresh()
        return browser

    def operate(self, browser, fields):
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=fields), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _next_month(FakeScreen(size=(25, 160)), browser)
        return message.call_args.args[1:]

    def draft(self, output=None):
        result = prepare_next_month(self.config, [self.config], output or self.base / "十月 草稿")
        return Path(result["worksheet"])

    def fill(self, worksheet, complete=False):
        sheet = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        sheet["targets_confirmed"] = True
        if complete:
            sheet.update(completeness_confirmed=True, completeness_reason="仅确认公开虚构十月账单已全部导入")
        self.write(worksheet, sheet)

    def assert_originals(self):
        self.assertTrue(all(path.read_bytes() == original for path, original in self.originals.items()))

    def test_prepare_draft_uses_latest_config_and_requires_all_new_confirmations(self):
        browser = self.browser()
        original_outcome = browser.outcome
        output = self.base / "十月 草稿"
        title, lines = self.operate(browser, ["1", str(output)])
        self.assertEqual(title, "下一月填写表已准备")
        worksheet = browser.next_worksheets[self.config]
        sheet = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        self.assertEqual(sheet["month"], "2026-10")
        self.assertEqual(sheet["cutover"], "2026-09-01")
        self.assertEqual((worksheet.parent / sheet["config"]).resolve(), self.config)
        self.assertFalse(sheet["targets_confirmed"])
        self.assertFalse(sheet["completeness_confirmed"])
        self.assertIn("生成填写表不等于", "\n".join(lines))
        self.assertIs(browser.outcome, original_outcome)
        self.assertFalse((output / "compiled").exists())
        self.assert_originals()

    def test_unfilled_worksheet_lists_locations_and_does_not_add_a_month(self):
        worksheet = self.draft()
        browser = self.browser()
        title, lines = self.operate(browser, ["2", str(worksheet)])
        self.assertEqual(title, "下一月填写仍待确认")
        self.assertIn("核对本月目标后填 true", "\n".join(lines))
        self.assertIn(str(worksheet) + ":", "\n".join(lines))
        self.assertEqual(browser.configs, [self.config])
        self.assertFalse((worksheet.parent / "compiled").exists())
        self.assert_originals()

    def test_load_then_confirm_month_end_then_compare_without_restarting(self):
        browser = self.browser()
        workspace = month_workspace(browser.config, browser.review_roots[0])
        worksheet = self.draft(workspace / "2026-10")
        self.fill(worksheet)
        with patch.object(browser, "loader", wraps=browser.loader) as load, \
             patch("beancount_budget_tool.tools.v6_next_month.audit_month_workflow", wraps=audit_month_workflow) as audit:
            title, _ = self.operate(browser, ["2", str(worksheet)])
        self.assertEqual(title, "下一月已载入")
        self.assertEqual(load.call_count, 0)
        audit.assert_called_once_with(worksheet.parent / "compiled/month.yaml", include_transactions=True)
        october = browser.config
        self.assertEqual(browser.configs, [self.config, october])
        self.assertEqual(browser.index, 1)
        self.assertIsNone(browser.outcome["report"])
        self.assertIn("EXTERNAL_SCOPE_PENDING", {row["reason_code"] for row in browser.reviews()})
        self.assertNotIn("completeness", yaml.safe_load(october.read_text(encoding="utf-8")))
        keys = ["f", "核过虚构十月全部账单", "\n", "y", "\n", "\n", "a", "q"]
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(FakeScreen(keys=keys, size=(30, 160)), browser)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "170")
        self.assertEqual(browser.outcome["report"]["transactions"]["count"], 1)
        self.assertEqual(browser.view, "transactions")
        self.assertEqual(month_workspace(browser.config, browser.review_roots[1]), workspace)
        reopened = self.browser([browser.config])
        self.assertEqual(month_workspace(reopened.config, reopened.review_roots[0]), workspace)
        self.assertTrue(browser.switch(-1))
        browser.refresh()
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "190")
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual((browser.outcome["from"], browser.outcome["through"]), ("2026-09", "2026-10"))
        self.assert_originals()

    def test_explicitly_completed_worksheet_can_immediately_browse_transactions(self):
        worksheet = self.draft()
        self.fill(worksheet, complete=True)
        browser = self.browser()
        self.assertEqual(self.operate(browser, ["2", str(worksheet)])[0], "下一月已载入")
        self.assertTrue(browser.has_transactions())
        self.assertEqual(browser.outcome["report"]["transactions"]["count"], 1)
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "170")

    def test_next_month_insertion_retains_later_paths_and_saved_indices(self):
        future = self.config.parent / "november.yaml"
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["month"] = "2026-11"
        document.pop("completeness")
        self.write(future, document)
        browser = self.browser([self.config, future])
        browser.saved.add(1)
        worksheet = self.draft()
        self.fill(worksheet)
        self.operate(browser, ["2", str(worksheet)])
        self.assertEqual(browser.index, 1)
        self.assertEqual(browser.configs[0], self.config)
        self.assertEqual(browser.configs[2], future)
        self.assertEqual(browser.saved, {1, 2})
        self.assertEqual(len(browser.review_roots), 3)

    def test_cancellation_at_either_field_never_publishes_files_or_recalculates(self):
        browser = self.browser()
        for fields in ([None], ["1", None], ["2", None]):
            before = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
            with self.subTest(fields=fields), \
                 patch("beancount_budget_tool.v6.tui._read_field", side_effect=fields), \
                 patch("beancount_budget_tool.v6.tui._message"), patch.object(browser, "refresh") as refresh:
                _next_month(FakeScreen(), browser)
            refresh.assert_not_called()
            self.assertEqual(before, {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()})

    def test_existing_output_and_existing_next_month_do_not_replace_decisions(self):
        worksheet = self.draft()
        contents = worksheet.read_bytes()
        browser = self.browser()
        title, lines = self.operate(browser, ["1", str(worksheet.parent)])
        self.assertEqual(title, "下一月操作未完成")
        self.assertIn("SETUP_OUTPUT_EXISTS", "\n".join(lines))
        self.assertEqual(worksheet.read_bytes(), contents)
        self.fill(worksheet, complete=True)
        october = Path(load_next_month(self.config, [self.config], worksheet)["config"])
        browser = self.browser([self.config, october])
        title, lines = self.operate(browser, [])
        self.assertEqual(title, "下一月操作未完成")
        self.assertIn("TUI_NEXT_MONTH_EXISTS", "\n".join(lines))
        self.assertEqual(browser.configs, [self.config, october])

    def test_saved_config_is_retained_if_the_following_audit_has_io_errors(self):
        for index, error in enumerate((OSError("read failed"), UnicodeError("bad encoding"))):
            worksheet = self.draft(self.base / f"error-{index}")
            self.fill(worksheet, complete=True)
            browser = self.browser()
            with patch("beancount_budget_tool.tools.v6_next_month.audit_month_workflow", side_effect=error):
                title, lines = self.operate(browser, ["2", str(worksheet)])
            self.assertEqual(title, "下一月已载入")
            self.assertTrue(browser.config.is_file())
            self.assertEqual(browser.error.code, "NEXT_MONTH_IO_ERROR")
            self.assertIsNone(browser.outcome)
            self.assertEqual(browser.lines, [])
            self.assertIn(str(browser.config), "\n".join(lines))
            browser.refresh()
            self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")

    def test_worksheet_must_use_the_current_latest_config(self):
        worksheet = self.draft()
        self.fill(worksheet)
        other = self.config.parent / "updated-month.yaml"
        other.write_bytes(self.config.read_bytes())
        browser = self.browser([other])
        title, lines = self.operate(browser, ["2", str(worksheet)])
        self.assertEqual(title, "下一月操作未完成")
        self.assertIn("TUI_NEXT_MONTH_BASE_CHANGED", "\n".join(lines))
        self.assertEqual(browser.config, other)
        self.assertFalse((worksheet.parent / "compiled").exists())

    def test_changes_immediately_before_compiling_are_rejected_before_publication(self):
        worksheet = self.draft()
        self.fill(worksheet)
        original = self.config.read_bytes()
        def race(*args, **kwargs):
            self.config.write_bytes(original + b"\n# changed at compile boundary\n")
            return compile_next_month(*args, **kwargs)
        try:
            with patch("beancount_budget_tool.v6.tui_next_month.compile_next_month", side_effect=race), \
                 self.assertRaises(BudgetError) as caught:
                load_next_month(self.config, [self.config], worksheet)
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")
            self.assertFalse((worksheet.parent / "compiled").exists())
        finally:
            self.config.write_bytes(original)

    def test_input_change_after_publication_retains_new_path_and_suppresses_numbers(self):
        worksheet = self.draft()
        self.fill(worksheet, complete=True)
        browser = self.browser()
        original = self.config.read_bytes()
        def changed_after_audit(*args, **kwargs):
            result = audit_month_workflow(*args, **kwargs)
            self.config.write_bytes(original + b"\n# changed after audit\n")
            return result
        try:
            with patch("beancount_budget_tool.tools.v6_next_month.audit_month_workflow", side_effect=changed_after_audit):
                title, _ = self.operate(browser, ["2", str(worksheet)])
            self.assertEqual(title, "下一月已载入")
            self.assertEqual(browser.error.code, "INPUT_CHANGED")
            self.assertIsNone(browser.outcome)
            self.assertTrue(browser.config.is_file())
            self.assertEqual(browser.lines, [])
        finally:
            self.config.write_bytes(original)

    def test_empty_next_month_draft_warns_without_approving_completeness(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").split('\n2026-10-05')[0], encoding="utf-8")
        browser = self.browser()
        title, lines = self.operate(browser, ["1", str(self.base / "empty")])
        self.assertEqual(title, "下一月填写表已准备")
        self.assertIn("下一月没有已记交易", "\n".join(lines))
        sheet = yaml.safe_load(browser.next_worksheets[self.config].read_text(encoding="utf-8"))
        self.assertFalse(sheet["completeness_confirmed"])

    def test_n_key_prepares_draft_and_remembers_it_for_loading(self):
        browser = self.browser()
        screen = FakeScreen(keys=["n", "\n", "\n", "\n", "q"], size=(30, 160))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertTrue(browser.next_worksheets[self.config].is_file())
        text = "\n".join(value for frame in screen.frames for _, _, value in frame)
        self.assertIn("n 下一月", text)
        self.assertIn("下一月填写表已准备", text)
        self.assertEqual(browser.config, self.config)

    def test_interval_and_help_do_not_start_next_month_operations(self):
        browser = self.browser()
        for attribute in ("period", "help"):
            setattr(browser, attribute, True)
            with patch("beancount_budget_tool.v6.tui._read_field") as form:
                _next_month(FakeScreen(), browser)
            form.assert_not_called()
            setattr(browser, attribute, False)


if __name__ == "__main__":
    unittest.main()

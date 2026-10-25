"""Month-end facts require explicit approval and survive publication failures."""

from datetime import date
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_init import _SetupDumper, compile_setup
from beancount_budget_tool.tools.v6_next_month import create_next_month, compile_next_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.tui import ReportBrowser, _confirm_month_end, _report_browser
from beancount_budget_tool.v6.month_actions import prepare_month_end, save_month_end
from test_v6_tui import FakeScreen


class MonthEndTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        example = Path(__file__).resolve().parents[1] / "examples/v6"
        self.ledger = self.base / "账本.bean"
        self.ledger.write_text((example / "first-month.bean").read_text(encoding="utf-8") +
                               '\ninclude "extra*.bean"\n', encoding="utf-8")
        self.extra = self.base / "extra-one.bean"
        self.extra.write_text("; included evidence\n", encoding="utf-8")
        setup = yaml.safe_load((example / "first-month-setup.yaml").read_text(encoding="utf-8"))
        setup["ledger"] = self.ledger.name
        self.write(self.base / "setup.yaml", setup)
        self.config = Path(compile_setup(self.base / "setup.yaml")["config"])
        self.full_config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document = dict(self.full_config)
        document.pop("completeness")
        self.write(self.config, document)
        self.originals = {path: path.read_bytes() for path in [self.ledger, self.extra,
                                                             *self.config.parent.glob("*.yaml")]}

    def write(self, path, document):
        path.write_text(yaml.dump(document, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def browser(self, configs=None):
        browser = ReportBrowser(configs or [self.config], partial(audit_month_workflow, include_transactions=True),
                                render_workflow_text)
        browser.refresh()
        return browser

    def run_keys(self, browser, keys):
        screen = FakeScreen(keys=keys, size=(30, 150))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        return screen

    def assert_originals(self):
        self.assertTrue(all(path.read_bytes() == original for path, original in self.originals.items()))

    def test_preparation_is_read_only_and_uses_the_historical_cutoff(self):
        self.extra.write_text('2026-10-02 * "future income"\n'
                              '  Assets:Cash 10 CNY\n  Income:Salary -10 CNY\n', encoding="utf-8")
        before = {path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}
        prepared = prepare_month_end(self.config, "2026-09")
        self.assertEqual((prepared.month, prepared.as_of, prepared.transactions),
                         ("2026-09", date(2026, 9, 30), 3))
        self.assertEqual(prepared.latest_transaction, date(2026, 9, 3))
        self.assertEqual({path: path.read_bytes() for path in self.base.rglob("*") if path.is_file()}, before)

    def test_save_requires_literal_approval_and_nonempty_reason(self):
        prepared = prepare_month_end(self.config, "2026-09")
        output = self.base / "unapproved"
        for approved in (False, None, 1, "y"):
            with self.subTest(approved=approved), self.assertRaises(BudgetError) as caught:
                save_month_end(prepared, "核对虚拟账单", output, approved=approved)
            self.assertEqual(caught.exception.code, "REVIEW_APPROVAL_REQUIRED")
        with self.assertRaises(BudgetError) as caught:
            save_month_end(prepared, "  ", output, approved=True)
        self.assertEqual(caught.exception.code, "NEXT_MONTH_REASON_REQUIRED")
        self.assertFalse(output.exists())
        self.assert_originals()

    def test_saved_statement_signs_only_the_current_month_and_preserves_inputs(self):
        prepared = prepare_month_end(self.config, "2026-09")
        output = self.base / "closed"
        result = save_month_end(prepared, "  已核对虚拟九月导入，无漏记责任和在途付款  ", output, approved=True)
        self.assertEqual(result["status"], "COMPLETENESS_SAVED")
        workflow = result["workflow"]
        self.assertEqual(workflow["status"], "CERTIFIED_SCOPED")
        self.assertEqual(workflow["report"]["max_spendable"]["max_spendable"], "190")
        self.assertEqual(workflow["report"]["transactions"]["count"], 3)
        statement = yaml.safe_load((output / "completeness.yaml").read_text(encoding="utf-8"))
        self.assertEqual(statement["as_of"], "2026-09-30")
        self.assertEqual(statement["reason"], "已核对虚拟九月导入，无漏记责任和在途付款")
        self.assertEqual(statement["unbooked_obligations"], "none")
        self.assert_originals()
        with self.assertRaises(BudgetError) as caught:
            save_month_end(prepared, "不覆盖", output, approved=True)
        self.assertEqual(caught.exception.code, "SETUP_OUTPUT_EXISTS")
        draft = create_next_month(Path(result["config"]), self.base / "next")
        self.assertEqual(draft["month"], "2026-10")
        proposal = yaml.safe_load(Path(draft["worksheet"]).read_text(encoding="utf-8"))
        self.assertFalse(proposal["completeness_confirmed"])

    def test_form_changes_in_ledger_include_or_sidecar_require_fresh_confirmation(self):
        prepared = prepare_month_end(self.config, "2026-09")
        for index, path in enumerate((self.ledger, self.extra, self.config.parent / "ownership.yaml")):
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"\n; changed during form\n" if path.suffix == ".bean" else
                                 original + b"\n# changed during form\n")
                with self.subTest(path=path), self.assertRaises(BudgetError) as caught:
                    save_month_end(prepared, "旧依据", self.base / f"changed-{index}", approved=True)
                self.assertEqual(caught.exception.code, "INPUT_CHANGED")
                self.assertFalse((self.base / f"changed-{index}").exists())
            finally:
                path.write_bytes(original)
        additional = self.base / "extra-two.bean"
        additional.write_text("; newly matched include\n", encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            save_month_end(prepared, "旧依据", self.base / "new-include", approved=True)
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")
        self.assertFalse((self.base / "new-include").exists())

    def test_changed_month_cannot_reuse_the_displayed_month(self):
        document = dict(self.full_config)
        document["month"] = "2026-10"
        self.write(self.config, document)
        with self.assertRaises(BudgetError) as caught:
            prepare_month_end(self.config, "2026-09")
        self.assertEqual(caught.exception.code, "TUI_MONTH_CONTEXT_CHANGED")

    def test_context_changed_while_preparing_or_immediately_before_publish_is_rejected(self):
        from beancount_budget_tool.tools.v6_review_decisions import _config
        from beancount_budget_tool.tools.v6_next_month import confirm_month
        original = self.config.read_bytes()
        def changed_config(path):
            parsed = _config(path)
            path.write_bytes(original + b"\n# changed while preparing\n")
            return parsed
        with patch("beancount_budget_tool.v6.month_actions.read_month_config", side_effect=changed_config):
            with self.assertRaises(BudgetError) as caught:
                prepare_month_end(self.config, "2026-09")
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")
        self.config.write_bytes(original)
        prepared = prepare_month_end(self.config, "2026-09")
        def before_publish(*args, **kwargs):
            self.config.write_bytes(original + b"\n# changed before backend capture\n")
            return confirm_month(*args, **kwargs)
        with patch("beancount_budget_tool.v6.month_actions.confirm_month", side_effect=before_publish):
            with self.assertRaises(BudgetError) as caught:
                save_month_end(prepared, "旧依据", self.base / "raced", approved=True)
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")
        self.assertFalse((self.base / "raced").exists())

    def test_confirmation_does_not_approve_other_pending_classifications(self):
        path = self.config.parent / "classifications.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document.pop("defaults")
        self.write(path, document)
        result = save_month_end(prepare_month_end(self.config, "2026-09"), "虚拟来源已完整导入",
                                self.base / "still-pending", approved=True)
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["workflow"]["report"])
        self.assertTrue(any(item["reason_code"] == "CLASSIFICATION_UNREVIEWED"
                            for item in result["workflow"]["review_items"]))

    def test_post_publication_io_error_keeps_saved_path_without_old_numbers(self):
        browser = self.browser()
        prepared = prepare_month_end(self.config, "2026-09")
        output = self.base / "published"
        with patch("beancount_budget_tool.v6.month_actions.audit_month_workflow", side_effect=OSError("temporary IO failure")):
            result = save_month_end(prepared, "已核对虚拟九月", output, approved=True)
        self.assertEqual(result["report_error"]["code"], "NEXT_MONTH_IO_ERROR")
        self.assertIsNone(result["workflow"])
        browser.lines = ["old number 999999"]
        browser.show_saved_month(result)
        self.assertEqual(browser.config, output / "month.yaml")
        self.assertTrue(browser.config.is_file())
        self.assertEqual(browser.error.code, "NEXT_MONTH_IO_ERROR")
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assert_originals()

    def test_error_after_backend_publication_is_reported_as_saved_not_lost(self):
        from beancount_budget_tool.tools.v6_next_month import confirm_month
        prepared = prepare_month_end(self.config, "2026-09")
        def publish_then_fail(*args, **kwargs):
            confirm_month(*args, **kwargs)
            raise OSError("after publication")
        with patch("beancount_budget_tool.v6.month_actions.confirm_month", side_effect=publish_then_fail):
            result = save_month_end(prepared, "虚拟账单核对", self.base / "late-error", approved=True)
        self.assertEqual(result["status"], "COMPLETENESS_SAVED")
        self.assertTrue(Path(result["config"]).is_file())
        self.assertIsNone(result["workflow"])

    def test_keyboard_can_confirm_then_browse_sources_without_repeating_the_audit(self):
        browser = self.browser([self.config, self.base / "another-month.yaml"])
        self.assertIsNone(browser.outcome["report"])
        with patch.object(browser, "loader", wraps=browser.loader) as load, \
             patch("beancount_budget_tool.v6.month_actions.audit_month_workflow", wraps=audit_month_workflow) as audit:
            screen = self.run_keys(browser, ["f", "\n", "\n", "核过虚拟九月账单", "\n", "y", "\n", "\n", "a", "q"])
        self.assertEqual(load.call_count, 1)  # Initial browser load, no duplicate load after saving.
        self.assertEqual(audit.call_count, 1)
        self.assertNotEqual(browser.config, self.config)
        self.assertEqual(browser.configs[1], self.base / "another-month.yaml")
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.view, "transactions")
        self.assertEqual(browser.outcome["report"]["transactions"]["count"], 3)
        text = "\n".join(value for frame in screen.frames for _, _, value in frame)
        self.assertIn("尚未保存", text)
        self.assertIn("不要求预测", text)
        self.assertIn("已保存月末核对", text)
        reopened = self.browser([browser.config])
        self.assertEqual(reopened.review_roots[0], browser.review_roots[0])
        self.assert_originals()

    def test_cancel_at_any_stage_leaves_original_config_and_no_statement(self):
        for inputs in (["\x1b"], ["依据", "\n", "\x1b"], ["依据", "\n", "Y", "\n"], ["依据", "\n", "\n"]):
            with self.subTest(inputs=inputs):
                browser = self.browser()
                self.run_keys(browser, ["f", *inputs, "q"])
                self.assertEqual(browser.config, self.config)
                self.assertFalse(browser.review_roots[0].exists())
                self.assertIsNone(browser.outcome["report"])
                self.assert_originals()

    def test_changed_include_during_preview_cannot_be_approved(self):
        browser = self.browser()
        def fields(_screen, title, *_args):
            if title == "核对本月末资料":
                return "旧核对依据"
            self.extra.write_text("; changed before approval\n", encoding="utf-8")
            return "y"
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=fields), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _confirm_month_end(FakeScreen(size=(30, 150)), browser)
        self.assertEqual(message.call_args.args[1], "未保存月末核对")
        self.assertIn("INPUT_CHANGED", "\n".join(message.call_args.args[2]))
        self.assertEqual(browser.config, self.config)
        self.assertFalse(browser.review_roots[0].exists())

    def test_already_reviewed_and_interval_views_do_not_create_declarations(self):
        self.write(self.config, self.full_config)
        browser = self.browser()
        with patch("beancount_budget_tool.v6.month_actions.prepare_month_end") as prepare, \
             patch("beancount_budget_tool.v6.tui._message"):
            _confirm_month_end(FakeScreen(), browser)
            browser.toggle_period()
            browser.refresh()
            _confirm_month_end(FakeScreen(), browser)
        prepare.assert_not_called()
        self.assertFalse(browser.review_roots[0].exists())

    def test_empty_next_month_requires_explicit_confirmation_and_keeps_warning(self):
        draft = create_next_month(self.config, self.base / "october")
        worksheet = Path(draft["worksheet"])
        document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        document["targets_confirmed"] = True
        self.write(worksheet, document)
        october = Path(compile_next_month(worksheet)["config"])
        browser = self.browser([october])
        screen = self.run_keys(browser, ["f", "已核对虚拟十月确无交易", "\n", "y", "\n", "\n", "q"])
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["month"], "2026-10")
        self.assertIn("查询月份没有已记交易", "\n".join(browser.lines))
        text = "\n".join(value for frame in screen.frames for _, _, value in frame)
        self.assertIn("零笔不能证明资料完整", text)
        self.assertIn("2026-10-31", text)


if __name__ == "__main__":
    unittest.main()

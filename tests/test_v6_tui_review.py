"""Interactive review uses explicit approval and the formal worksheet checks."""

from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from fixture_inputs import SetupDumper as _SetupDumper, create_setup, compile_setup
from beancount_budget_tool.v6.review import apply_review
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.tui import ReportBrowser, _draw_browser, _read_field, _report_browser, _message
from beancount_budget_tool.v6.review import prepare_review, save_review, validate_decision
from test_v6_tui import FakeScreen


class InteractiveReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.ledger = self.base / "账本.bean"
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Equity:Opening\n2026-08-01 open Income:Salary\n'
            '2026-08-01 open Expenses:Food\n2026-08-01 open Liabilities:Card\n'
            '2026-08-31 * "opening"\n  Assets:Cash 100 CNY\n  Equity:Opening -100 CNY\n'
            '2026-09-02 * "银行" "用途待定的搬家"\n  Assets:Cash -20 CNY\n  Assets:Bank 20 CNY\n'
            '2026-09-03 * "餐饮"\n  Assets:Cash -10 CNY\n  Expenses:Food 10 CNY\n'
            '2026-09-04 * "工资"\n  Assets:Cash 5 CNY\n  Income:Salary -5 CNY\n', encoding="utf-8")
        setup = Path(create_setup(self.ledger, "2026-09", self.base / "setup",
                                  income_purpose="自由资金", expense_purpose="生活费",
                                  expense_category="生活开销")["setup"])
        document = yaml.safe_load(setup.read_text(encoding="utf-8"))
        document["defaults"]["confirmed"] = document["completeness_confirmed"] = True
        document["expense_targets"] = {"生活开销": "200"}
        document["allocation_targets"] = {name: "not_assessed" for name in document["allocation_targets"]}
        for account in document["accounts"]:
            account.update(semantics="H", reason="核过虚拟银行流水", payment_scope={"CNY": "payable"})
            for opening in account["opening"]:
                opening["owners"] = {"自由资金": "50", "生活费": "50"}
        self.write(setup, document)
        self.config = Path(compile_setup(setup)["config"])
        categories = self.config.parent / "classifications.yaml"
        document = yaml.safe_load(categories.read_text(encoding="utf-8"))
        del document["defaults"]
        self.write(categories, document)
        self.originals = {path: path.read_bytes() for path in [self.ledger, *self.config.parent.glob("*.yaml")]}

    def write(self, path, document):
        path.write_text(yaml.dump(document, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def item(self, kind="classification", account="Expenses:Food", config=None):
        return next(row for row in audit_month_workflow(config or self.config)["review_items"]
                    if row["kind"] == kind and row.get("account") == account)

    def browser(self, configs=None):
        browser = ReportBrowser(configs or [self.config],
                                lambda path: audit_month_workflow(path, include_transactions=True), render_workflow_text)
        browser.refresh()
        return browser

    def run_keys(self, browser, keys):
        screen = FakeScreen(keys=keys, size=(18, 120))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        return screen

    def assert_originals(self):
        self.assertTrue(all(path.read_bytes() == content for path, content in self.originals.items()))

    def test_prepare_and_cancel_do_not_publish_or_approve_anything(self):
        prepared = prepare_review(self.config, self.item())
        self.assertEqual(prepared.row["narration"], "餐饮")
        self.assertEqual(prepared.row["signed_units"], "10")
        self.assertIn("生活开销", prepared.names)
        self.assertTrue(all(row["approved"] is False for row in prepared.document["rows"]))
        self.assertFalse((self.config.parent / "tui-reviews").exists())
        self.assert_originals()

    def test_saving_requires_literal_explicit_approval_and_registered_exact_amounts(self):
        prepared = prepare_review(self.config, self.item())
        for approved in (False, None, "y", 1):
            with self.subTest(approved=approved), self.assertRaises(BudgetError) as caught:
                save_review(prepared, "生活开销", "核对过餐饮流水", self.base / "saved", approved=approved)
            self.assertEqual(caught.exception.code, "REVIEW_APPROVAL_REQUIRED")
        for decision, reason, code in (("生活开销", " ", "REVIEW_REASON_REQUIRED"),
                                       ("未登记", "理由", "REVIEW_DECISION_INVALID"),
                                       ("生活开销6，自由资金3", "理由", "REVIEW_DECISION_INVALID")):
            with self.subTest(decision=decision, reason=reason), self.assertRaises(BudgetError) as caught:
                save_review(prepared, decision, reason, self.base / "saved", approved=True)
            self.assertEqual(caught.exception.code, code)
        self.assertFalse((self.base / "saved").exists())
        self.assert_originals()

    def test_single_decision_saved_new_configuration_other_rows_stay_unapproved(self):
        prepared = prepare_review(self.config, self.item())
        result = save_review(prepared, "生活开销", "本人核对餐饮流水", self.base / "saved", approved=True)
        self.assertEqual(result["status"], "DECISIONS_SAVED")
        self.assertEqual(len(result["accepted"]), 1)
        sheet = yaml.safe_load((self.base / "saved" / "decisions.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sum(row["approved"] for row in sheet["rows"]), 1)
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["workflow"]["report"])
        self.assert_originals()
        with self.assertRaises(BudgetError) as caught:
            save_review(prepared, "生活开销", "不覆盖记录", self.base / "saved", approved=True)
        self.assertEqual(caught.exception.code, "SETUP_OUTPUT_EXISTS")

    def test_purpose_split_keeps_native_negative_sign_and_checks_ownership(self):
        prepared = prepare_review(self.config, self.item("asset_ownership", "Assets:Cash"))
        self.assertEqual(validate_decision(prepared, "自由资金10，生活费10", "混合资金"),
                         [("自由资金", Fraction(-10)), ("生活费", Fraction(-10))])
        result = save_review(prepared, "自由资金10，生活费10", "混合资金", self.base / "split", approved=True)
        owners = yaml.safe_load((Path(result["config"]).parent / "ownership.yaml").read_text(encoding="utf-8"))
        self.assertEqual(sum(Fraction(part["signed_units"]) for part in owners["claims"][-1]["slices"]), -20)
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")

    def test_voluntary_overdraft_cannot_publish_configuration(self):
        path = self.config.parent / "opening.yaml"
        opening = yaml.safe_load(path.read_text(encoding="utf-8"))
        owners = yaml.safe_load((self.config.parent / "ownership.yaml").read_text(encoding="utf-8"))
        free = next(row["id"] for row in owners["owners"] if row["name"] == "自由资金")
        for row in opening["h_holdings"]:
            row["units"] = "10" if row["owner"] == free else "90"
        self.write(path, opening)
        prepared = prepare_review(self.config, self.item("asset_ownership", "Assets:Cash"))
        with self.assertRaises(BudgetError) as caught:
            save_review(prepared, "自由资金", "划转", self.base / "overdraft", approved=True)
        self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")
        self.assertFalse((self.base / "overdraft" / "reviewed").exists())

    def test_changed_source_cannot_use_displayed_review_before_or_after_preparation(self):
        item = self.item()
        prepared = prepare_review(self.config, item)
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace('"餐饮"', '"改过的餐饮"'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            prepare_review(self.config, item)
        self.assertEqual(caught.exception.code, "REVIEW_SOURCE_CHANGED")
        with self.assertRaises(BudgetError) as caught:
            save_review(prepared, "生活开销", "旧理由", self.base / "stale", approved=True)
        self.assertEqual(caught.exception.code, "REVIEW_SOURCE_CHANGED")
        self.assertFalse((self.base / "stale" / "reviewed").exists())

    def test_changed_configuration_during_form_is_rejected_before_any_write(self):
        prepared = prepare_review(self.config, self.item())
        path = self.config.parent / "classifications.yaml"
        path.write_text(path.read_text(encoding="utf-8") + "\n# another reviewer changed this file\n", encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            save_review(prepared, "生活开销", "已核对", self.base / "changed", approved=True)
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")
        self.assertFalse((self.base / "changed").exists())

    def test_other_pending_types_are_not_approved_through_category_form(self):
        item = next(row for row in audit_month_workflow(self.config)["review_items"] if row["kind"] == "h_transfer")
        with self.assertRaises(BudgetError) as caught:
            prepare_review(self.config, item)
        self.assertEqual(caught.exception.code, "TUI_REVIEW_UNSUPPORTED")

    def test_keyboard_approve_switches_only_current_month_and_refreshes_pending(self):
        other = self.base / "other-month.yaml"
        browser = self.browser([self.config, other])
        screen = self.run_keys(browser, ["/", *"Expenses:Food", "\n", "e", *"生活开销", "\n",
                                         *"核过原流水", "\n", "y", "\n", "\n", "q"])
        self.assertNotEqual(browser.config, self.config)
        self.assertTrue(browser.config.is_file())
        self.assertEqual(browser.configs[1], other)
        self.assertEqual(browser.query, "")
        self.assertEqual(browser.saved, {0})
        self.assertFalse(any(row.get("account") == "Expenses:Food" for row in browser.reviews()))
        self.assertTrue(any("已保存审阅决定" in value for frame in screen.frames for _, _, value in frame))
        self.assert_originals()

    def test_keyboard_cancel_at_every_stage_leaves_no_persistent_files(self):
        for keys in (["\x1b"], [*"生活开销", "\n", "\x1b"],
                     [*"生活开销", "\n", "理由", "\n", "\x1b"],
                     [*"生活开销", "\n", "理由", "\n", "\n"],
                     [*"生活开销", "\n", "理由", "\n", "Y", "\n"]):
            with self.subTest(keys=keys):
                browser = self.browser()
                self.run_keys(browser, ["/", *"Expenses:Food", "\n", "e", *keys, "q"])
                self.assertEqual(browser.config, self.config)
                self.assertFalse(browser.review_roots[0].exists())
                self.assert_originals()

    def test_keyboard_continues_decisions_to_certified_report_and_details(self):
        browser = self.browser()
        root = browser.review_roots[0]
        for kind, account, decision in (("classification", "Expenses:Food", "生活开销"),
                                        ("classification", "Income:Salary", "自由资金"),
                                        ("asset_ownership", "Assets:Cash", "自由资金")):
            self.run_keys(browser, ["/", *account, "\n", "e", *decision, "\n", "核对逐笔", "\n",
                                     "y", "\n", "\n", "q"])
            self.assertEqual(browser.review_roots[0], root)
            self.assertEqual(browser.config.parent.parent.parent, root)
        self.assertEqual(len(list(root.iterdir())), 3)  # The exact matching inbound owner can then be inferred.
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "40")
        self.assertTrue(browser.has_transactions())
        self.assertEqual(browser.outcome["report"]["transactions"]["count"], 3)
        self.run_keys(browser, ["c", "q"])
        self.assertTrue(browser.period)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["months"][0]["config"], str(browser.config))
        self.assertEqual(browser.outcome["report"]["closing"]["max_spendable"]["max_spendable"], "40")
        self.run_keys(browser, ["c", "a", "q"])
        self.assertFalse(browser.period)
        self.assertEqual(browser.view, "transactions")
        self.assertEqual(browser.outcome["report"]["transactions"]["count"], 3)
        output = self.base / "reviewed-month.csv"
        self.run_keys(browser, ["x", "\x15", *"csv", "\n", "\x15", *str(output), "\n", "\n", "q"])
        self.assertIn(str(browser.config), output.read_text(encoding="utf-8-sig"))
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "40")
        self.assert_originals()

    def test_keyboard_invalid_amount_can_be_corrected_before_approval(self):
        browser = self.browser()
        screen = self.run_keys(browser, ["/", *"Expenses:Food", "\n", "e", *"生活开销6，自由资金3", "\n",
                                         "核对", "\n", "\n", "\x15", *"生活开销", "\n", "\n",
                                         "y", "\n", "\n", "q"])
        self.assertIn(0, browser.saved)
        self.assertTrue(any("尚未保存" in text for frame in screen.frames for _, _, text in frame))
        self.assertEqual(len(list(browser.review_roots[0].iterdir())), 1)
        self.assert_originals()

    def test_reopening_saved_config_reuses_record_root_without_nested_paths(self):
        browser = self.browser()
        root = browser.review_roots[0]
        self.run_keys(browser, ["/", *"Expenses:Food", "\n", "e", *"生活开销", "\n",
                                 "核对", "\n", "y", "\n", "\n", "q"])
        reopened = self.browser([browser.config])
        self.assertEqual(reopened.review_roots[0], root)
        self.run_keys(reopened, ["/", *"Income:Salary", "\n", "e", *"自由资金", "\n",
                                  "核对", "\n", "y", "\n", "\n", "q"])
        self.assertEqual(reopened.config.parent.parent.parent, root)
        self.assertEqual(len(list(root.iterdir())), 2)
        self.assert_originals()

    def test_keyboard_overdraft_keeps_old_configuration_and_remaining_reviews(self):
        path = self.config.parent / "opening.yaml"
        opening = yaml.safe_load(path.read_text(encoding="utf-8"))
        owners = yaml.safe_load((self.config.parent / "ownership.yaml").read_text(encoding="utf-8"))
        free = next(row["id"] for row in owners["owners"] if row["name"] == "自由资金")
        for row in opening["h_holdings"]:
            row["units"] = "10" if row["owner"] == free else "90"
        self.write(path, opening)
        browser = self.browser()
        screen = self.run_keys(browser, ["/", *"Assets:Cash", "\n", "e", *"自由资金", "\n",
                                         "主动划转", "\n", "y", "\n", "\n", "q"])
        self.assertEqual(browser.config, self.config)
        self.assertEqual(browser.saved, set())
        self.assertIsNone(browser.outcome["report"])
        self.assertTrue(browser.reviews())
        self.assertFalse(list(browser.review_roots[0].glob("*/reviewed/month.yaml")))
        text = "\n".join(value for frame in screen.frames for _, _, value in frame)
        self.assertIn("H_OWNER_NEGATIVE", text)
        self.assertIn("填写记录保留在", text)

    def test_io_failure_after_publication_still_returns_saved_configuration(self):
        prepared = prepare_review(self.config, self.item())
        def publish_then_fail(worksheet):
            apply_review(worksheet)
            raise OSError("next report input was temporarily unavailable")
        with patch("beancount_budget_tool.v6.review.apply_review", side_effect=publish_then_fail):
            result = save_review(prepared, "生活开销", "核对过", self.base / "io-error", approved=True)
        self.assertEqual(result["status"], "DECISIONS_SAVED")
        self.assertTrue(Path(result["config"]).is_file())
        self.assertEqual(result["report_error"]["code"], "REVIEW_IO_ERROR")
        self.assertIsNone(result["workflow"])
        self.assertEqual(len(result["accepted"]), 1)
        self.assert_originals()

    def test_post_save_report_error_preserves_new_path_and_clears_old_values(self):
        path = self.config.parent / "expense_plans.yaml"
        plan = yaml.safe_load(path.read_text(encoding="utf-8"))
        plan["months"]["2026-09"]["expense_subtree_targets"]["category_2"] = "bad"
        self.write(path, plan)
        browser = self.browser()
        # Approve the last item only after the other sources have been saved.
        for kind, account, decision in (("classification", "Income:Salary", "自由资金"),
                                        ("asset_ownership", "Assets:Cash", "自由资金")):
            prepared = prepare_review(browser.config, self.item(kind, account, browser.config))
            result = save_review(prepared, decision, "已核对", self.base / account.replace(":", "-"), approved=True)
            browser.configs[0] = Path(result["config"])
        screen = self.run_keys(browser, ["/", *"Expenses:Food", "\n", "e", *"生活开销", "\n",
                                         "核对", "\n", "y", "\n", "\n", "q"])
        self.assertIn(0, browser.saved)
        self.assertTrue(browser.config.is_file())
        self.assertIsNotNone(browser.error)
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        text = "\n".join(value for frame in screen.frames for _, _, value in frame)
        self.assertIn("决定已保存，但月报计算仍有错误", text)
        browser.help = True
        help_screen = FakeScreen(size=(100, 40))
        _draw_browser(help_screen, browser)
        self.assertIn(str(browser.config), "".join(value for row, _, value in help_screen.rows if row >= 3))


class ReviewFormTests(unittest.TestCase):
    def test_notice_has_no_discarded_text_field_and_both_keys_return(self):
        for key in ('\n', '\x1b'):
            screen = FakeScreen(keys=['误以为能在这里填写的文字', key], size=(8, 90))
            _message(screen, '填写有误；尚未保存', ['判断依据不能为空'])
            text = '\n'.join(value for frame in screen.frames for _, _, value in frame)
            self.assertIn('Enter 或 Esc 返回', text)
            self.assertNotIn('Enter 继续', text)
            self.assertNotIn('Ctrl+U', text)
            self.assertNotIn('> ', text)
            self.assertNotIn('误以为能在这里填写的文字', text)

    def test_notice_scrolls_and_can_close_on_small_screens(self):
        import curses
        screen = FakeScreen(keys=[curses.KEY_NPAGE, curses.KEY_PPAGE, '\n'], size=(5, 40))
        _message(screen, '通知', [f'内容 {index}' for index in range(20)])
        self.assertTrue(any('内容 3' in value for _, _, value in screen.frames[1]))
        for size in ((1, 8), (3, 6)):
            _message(FakeScreen(keys=['\x1b'], size=size), '通知', ['长内容'])

    def test_unicode_backspace_clear_scroll_and_control_character_escaping(self):
        context = ["/账本/很长的路径/" + "x" * 30 + ":42", "原文\x1b[2J", *[str(i) for i in range(20)]]
        screen = FakeScreen(keys=[*"旅游x", "\x7f", 338, 339, "\x15", *"生活费", "\n"], size=(8, 24))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            self.assertEqual(_read_field(screen, "审阅", context, "决定："), "生活费")
        self.assertTrue(all("\x1b" not in text for frame in screen.frames for _, _, text in frame))

    def test_small_terminal_cannot_accidentally_approve(self):
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            self.assertIsNone(_read_field(FakeScreen(keys=["y", "\n", "\x1b"], size=(3, 6)),
                                          "批准", ["preview"], "批准："))

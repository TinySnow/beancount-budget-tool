"""The terminal browser shares report text and handles narrow screens."""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import redirect_stderr
from io import StringIO
import curses
from tempfile import TemporaryDirectory
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.tui import (
    ReportBrowser, _browse, _clip, _draw, _draw_browser, _read_filter,
    _report_browser, _wrap, run_tui, run_report_tui,
)
from beancount_budget_tool.v6.monthly_report import main, render_workflow_text
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.tools.v6_init import _SetupDumper, compile_setup
from beancount_budget_tool.tools.v6_next_month import create_next_month, compile_next_month


class FakeScreen:
    def __init__(self, keys=(), size=(5, 24)):
        self.keys = iter(keys)
        self.size = size
        self.rows = []
        self.frames = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows = []

    def addstr(self, row, col, value, *_style):
        self.rows.append((row, col, value))

    def refresh(self):
        self.frames.append(list(self.rows))

    def keypad(self, _enabled):
        pass

    def getch(self):
        return next(self.keys)

    def get_wch(self):
        return next(self.keys)


class TuiTests(unittest.TestCase):
    def test_noninteractive_call_fails_before_curses(self):
        with patch("beancount_budget_tool.v6.tui.sys.stdin.isatty", return_value=False):
            with self.assertRaises(BudgetError) as caught:
                run_tui("月报")
        self.assertEqual(caught.exception.code, "TUI_TERMINAL_REQUIRED")

    def test_cjk_clipping_and_scroll(self):
        self.assertEqual(_clip("生活费123", 5), "生活")
        screen = FakeScreen(size=(4, 14))
        offset = _draw(screen, ["甲", "乙", "丙", "丁"], 99)
        self.assertEqual(offset, 2)
        self.assertTrue(any(value == "丙" for _, _, value in screen.rows))
        _draw(screen, ["v6 月报待审：2026-10", "原因"], 0)
        self.assertTrue(any(row == 0 and value.startswith("v6 月报待审")
                            for row, _, value in screen.rows))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _browse(FakeScreen(keys=(ord("j"), ord("q"))), ["一", "二", "三", "四"])


def pending(month="2026-09"):
    items = [
        {"kind": "classification", "date": month + "-03", "account": "Expenses:Food",
         "signed_units": "10", "commodity": "CNY", "reason_code": "CLASSIFICATION_UNREVIEWED",
         "reason_kind": "data_review", "detail": "餐饮类别尚未确定", "source": "/ledger/生活账本.bean:42",
         "ref": {"path": "生活账本.bean", "posting_ordinal": 2}},
        {"kind": "asset_ownership", "date": month + "-04", "account": "Assets:Travel",
         "signed_units": "-30", "commodity": "CNY", "reason_code": "FUND_MIXED_SOURCE",
         "reason_kind": "data_review", "detail": "旅游账户混合用途，需要确认", "source": "/ledger/旅行.bean:57",
         "ref": {"path": "旅行.bean", "posting_ordinal": 1}},
        {"kind": "h_transfer", "date": month + "-05", "account": "Assets:Broker",
         "reason_code": "H_TRANSFER_SHAPE_UNSUPPORTED", "reason_kind": "implementation_limit",
         "detail": "当前复合转账需要实现", "source": "/ledger/broker.bean:63"}]
    queue = {"since": month + "-01", "through": month + "-30", "items": items,
             "suppressed_pool_internal_h_postings": 0, "booked_transactions_in_range": 3,
             "note": "待审来源不签发预算数字"}
    return {"month": month, "status": "IMPLEMENTATION_BLOCKED", "stage": "source",
            "review_items": items, "review_queue": queue, "report": None}


class ReportBrowserTests(unittest.TestCase):
    def browser(self, outcome=None, loader=None):
        browser = ReportBrowser([Path("september/month.yaml"), Path("october/month.yaml")],
                                loader or Mock(return_value=outcome or pending()), render_workflow_text)
        browser.refresh()
        return browser

    def test_pending_list_filters_by_chinese_account_reason_and_file(self):
        browser = self.browser()
        self.assertEqual(browser.view, "reviews")
        for query, account in (("餐饮", "Expenses:Food"), ("assets:travel", "Assets:Travel"),
                               ("旅行.bean:57", "Assets:Travel"), ("SHAPE_UNSUPPORTED", "Assets:Broker")):
            with self.subTest(query=query):
                browser.filter(query)
                self.assertEqual([row["account"] for row in browser.reviews()], [account])
        browser.filter("不存在")
        screen = FakeScreen(size=(9, 100))
        _draw_browser(screen, browser)
        text = "\n".join(value for _, _, value in screen.rows)
        self.assertIn("待审总数 3", text)
        self.assertIn("匹配 0 项", text)
        self.assertIn("IMPLEMENTATION_BLOCKED", text)
        self.assertNotIn("已审月报", text)

    def test_selected_detail_is_the_filtered_item_and_preserves_full_reference(self):
        browser = self.browser()
        browser.filter("旅游")
        browser.open_detail()
        self.assertEqual(browser.detail["source"], "/ledger/旅行.bean:57")
        self.assertEqual(browser.detail["ref"], {"path": "旅行.bean", "posting_ordinal": 1})
        screen = FakeScreen(size=(100, 32))
        _draw_browser(screen, browser)
        contents = "".join(value for row, _, value in screen.rows if 3 <= row < 99)
        self.assertIn("/ledger/旅行.bean:57", contents)
        self.assertIn("posting_ordinal", contents)
        self.assertIn("需要确认", contents)

    def test_refresh_failure_erases_previous_certified_numbers(self):
        certified = {"month": "2026-09", "status": "CERTIFIED_SCOPED", "stage": "report",
                     "review_items": [], "report": {"value": "987654.32"}}
        load = Mock(side_effect=[certified, BudgetError("YAML_INVALID", "invalid edited config", "/config.yaml:8"),
                                 FileNotFoundError(2, "file moved during edit", "/moved.yaml"), pending()])
        browser = ReportBrowser([Path("month.yaml")], load,
                                lambda row: "v6 已审月报\n最多能花：987654.32" if row["report"] else "仅待审")
        browser.refresh()
        self.assertIn("987654.32", "\n".join(browser.lines))
        browser.refresh()
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        screen = FakeScreen(size=(15, 100))
        _draw_browser(screen, browser)
        text = "\n".join(value for _, _, value in screen.rows)
        self.assertNotIn("987654.32", text)
        self.assertIn("计算失败", text)
        self.assertIn("/config.yaml:8", text)
        browser.refresh()
        self.assertEqual(browser.error.code, "TUI_INPUT_ERROR")
        self.assertEqual(browser.error.source, "/moved.yaml")
        self.assertEqual(browser.lines, [])
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.view, "reviews")

    def test_clear_happens_before_loader_and_config_switch_does_not_reuse_data(self):
        calls = []
        def loader(path):
            self.assertIsNone(browser.outcome)
            self.assertEqual(browser.lines, [])
            calls.append(path)
            return pending("2026-09" if len(calls) == 1 else "2026-10")
        browser = ReportBrowser([Path("september.yaml"), Path("october.yaml")], loader, render_workflow_text)
        browser.refresh()
        self.assertTrue(browser.switch(1))
        self.assertIsNone(browser.outcome)
        browser.refresh()
        self.assertIn("2026-10", browser.title())
        self.assertEqual(calls, [Path("september.yaml").resolve(), Path("october.yaml").resolve()])
        self.assertFalse(browser.switch(1))

    def test_report_filter_keeps_headline_and_negative_balance_warning(self):
        browser = ReportBrowser([Path("month.yaml")], Mock(), Mock())
        browser.lines = ["v6 已审月报", "最多能花：-20 CNY", "旅游 100 CNY", "生活费 30 CNY",
                         "警告：投资用途余额为负 -50 CNY"]
        browser.filter("旅游")
        self.assertEqual(browser.report_lines(), ["v6 已审月报", "最多能花：-20 CNY", "旅游 100 CNY",
                                                   "警告：投资用途余额为负 -50 CNY"])

    def test_unicode_search_input_backspace_cancel_and_clear(self):
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            self.assertEqual(_read_filter(FakeScreen(keys=("旅", "游", "x", curses.KEY_BACKSPACE, "\n")), ""), "旅游")
            self.assertEqual(_read_filter(FakeScreen(keys=("x", "\x1b")), "餐饮"), "餐饮")
            self.assertEqual(_read_filter(FakeScreen(keys=("\x15", "\n")), "餐饮"), "")

    def test_keyboard_filter_detail_help_scroll_reload_and_switch(self):
        load = Mock(side_effect=[pending(), pending(), pending("2026-10")])
        browser = ReportBrowser([Path("september.yaml"), Path("october.yaml")], load, render_workflow_text)
        screen = FakeScreen(keys=("/", "旅", "游", "\n", "\n", "G", "\x1b", "?", "\n",
                                  "t", " ", "g", "r", "]", "q"), size=(12, 100))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertEqual(load.call_count, 3)
        self.assertEqual(browser.outcome["month"], "2026-10")
        self.assertTrue(any("待审详情" in value for frame in screen.frames for _, _, value in frame))
        self.assertTrue(any("正在重算" in value for frame in screen.frames for _, _, value in frame))

    def test_pending_preflight_stays_explicit_and_tiny_windows_do_not_crash(self):
        outcome = pending()
        outcome["review_queue"]["preflight_only"] = True
        browser = self.browser(outcome)
        screen = FakeScreen(size=(12, 140))
        _draw_browser(screen, browser)
        self.assertTrue(any("仅当前预检阻断层" in value for _, _, value in screen.rows))
        for size in ((1, 1), (2, 3), (4, 7)):
            _draw_browser(FakeScreen(size=size), browser)

    def test_loading_never_looks_like_zero_pending_and_initial_error_can_be_retried(self):
        loader = Mock(side_effect=[BudgetError("YAML_INVALID", "repair this", "/month.yaml:3"), pending()])
        browser = ReportBrowser([Path("month.yaml")], loader, render_workflow_text)
        screen = FakeScreen(keys=("r", "q"), size=(12, 100))
        _draw_browser(screen, browser)
        initial = "\n".join(value for _, _, value in screen.rows)
        self.assertIn("尚不提供待审总数", initial)
        self.assertNotIn("待审总数 0", initial)
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertTrue(any("计算失败" in value for frame in screen.frames for _, _, value in frame))

    def test_wrap_preserves_long_sources_and_combining_characters(self):
        source = "/very/long/中文账本目录/银行账户/月度账单.bean:12345"
        self.assertEqual("".join(_wrap([source], 9)), source)
        self.assertEqual(_clip("e\u0301甲乙", 3), "e\u0301甲")
        self.assertIn("\\x1b", "".join(_wrap(["payee\x1b[2J"], 20)))

    def test_nonterminal_tui_does_not_even_run_the_auditor(self):
        loader = Mock()
        with patch("beancount_budget_tool.v6.tui.sys.stdin.isatty", return_value=False):
            with self.assertRaises(BudgetError):
                run_report_tui([Path("month.yaml")], loader, render_workflow_text)
        loader.assert_not_called()

    def test_cli_routes_tui_to_config_loader_and_rejects_tui_options_for_json(self):
        with patch("beancount_budget_tool.v6.tui.run_report_tui") as tui, \
             patch("beancount_budget_tool.v6.monthly_report.audit_month_workflow") as audit:
            self.assertEqual(main(["--config", "september.yaml", "--format", "tui", "--tui-config", "october.yaml"]), 0)
            self.assertEqual(tui.call_args.args[0], [Path("september.yaml"), Path("october.yaml")])
            self.assertIs(tui.call_args.args[1].func, audit)
            self.assertEqual(tui.call_args.args[1].keywords, {"include_transactions": True})
            audit.assert_not_called()
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main(["--config", "september.yaml", "--format", "json", "--tui-config", "october.yaml"])


class LiveReportBrowserTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        examples = Path(__file__).resolve().parents[1] / "examples" / "v6"
        self.ledger = self.base / "main.bean"
        self.ledger.write_bytes((examples / "first-month.bean").read_bytes())
        setup = yaml.safe_load((examples / "first-month-setup.yaml").read_text())
        setup["ledger"] = "main.bean"
        self.save(self.base / "setup.yaml", setup)
        self.config = Path(compile_setup(self.base / "setup.yaml")["config"])

    def save(self, path, value):
        path.write_text(yaml.dump(value, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def test_saved_ledger_changes_recompute_then_config_error_clears_numbers_and_recovers(self):
        browser = ReportBrowser([self.config], audit_month_workflow, render_workflow_text)
        browser.refresh()
        self.assertIn("最多能花：190 CNY", "\n".join(browser.lines))
        self.ledger.write_text(self.ledger.read_text().replace("Expenses:Food 10 CNY", "Expenses:Food 15 CNY").replace(
            "Assets:Cash -10 CNY", "Assets:Cash -15 CNY"), encoding="utf-8")
        browser.refresh()
        self.assertIn("最多能花：185 CNY", "\n".join(browser.lines))
        self.assertNotIn("最多能花：190 CNY", "\n".join(browser.lines))
        original = self.config.read_text()
        self.config.write_text(original + "schema: 6\n", encoding="utf-8")
        browser.refresh()
        self.assertEqual(browser.error.code, "YAML_DUPLICATE_KEY")
        self.assertEqual(browser.lines, [])
        document = yaml.safe_load(original)
        del document["completeness"]
        self.save(self.config, document)
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(browser.outcome["report"])
        browser.open_detail()
        self.assertEqual(browser.detail["reason_code"], "EXTERNAL_SCOPE_PENDING")
        self.assertIn(str(self.config), browser.detail["source"])

    def test_switch_prepared_month_configs_uses_each_cutoff_and_evidence(self):
        self.ledger.write_text(self.ledger.read_text() + '2026-10-03 * "October food"\n'
                              '  Assets:Cash -20 CNY\n  Expenses:Food 20 CNY\n', encoding="utf-8")
        draft = create_next_month(self.config, self.base / "october")
        sheet = Path(draft["worksheet"])
        document = yaml.safe_load(sheet.read_text())
        document.update(targets_confirmed=True, completeness_confirmed=True,
                        completeness_reason="virtual October import complete")
        self.save(sheet, document)
        october = Path(compile_next_month(sheet)["config"])
        browser = ReportBrowser([self.config, october], audit_month_workflow, render_workflow_text)
        browser.refresh()
        self.assertIn("最多能花：190 CNY", "\n".join(browser.lines))
        browser.switch(1)
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-10")
        self.assertIn("最多能花：170 CNY", "\n".join(browser.lines))
        browser.switch(-1)
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-09")
        self.assertIn("最多能花：190 CNY", "\n".join(browser.lines))


if __name__ == "__main__":
    unittest.main()

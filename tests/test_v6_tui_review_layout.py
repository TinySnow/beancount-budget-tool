"""Review cards keep source positions readable on Windows and narrow terminals."""

import copy
import curses
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.monthly_report import render_workflow_text
from beancount_budget_tool.v6.tui import (
    ReportBrowser, _clip, _draw, _draw_browser, _put, _read_field, _report_browser, _review_layout,
    _review_location, _review_page_target, _review_reason, _width,
)
from test_v6_tui import FakeScreen, pending


class PhysicalScreen(FakeScreen):
    """Model an out-of-sync terminal that retains cells without invalidation."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.physical = {}
        self.force_clear = False
        self.cursor = (0, 0)

    def clearok(self, enabled):
        self.force_clear = enabled

    def move(self, row, col):
        self.cursor = (row, col)

    def clrtoeol(self):
        row, col = self.cursor
        self.physical[row] = self.physical.get(row, "")[:col]

    def refresh(self):
        if self.force_clear:
            self.physical = {}
            self.force_clear = False
        for row, col, value in self.rows:
            old = self.physical.get(row, "")
            self.physical[row] = old[:col] + value + old[col + len(value):]
        super().refresh()


class StyledTailScreen(FakeScreen):
    """Model the reported terminal: an attribute write loses its text tail."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.styles = []

    def addstr(self, row, col, value, style=0):
        self.styles.append(style)
        super().addstr(row, col, _clip(value, 20) if style else value, style)


def inventory_item():
    return {
        "kind": "inventory", "date": "2023-07-29", "month": "2023-07",
        "account": "Assets:Digital-电子钱包:支付宝:余额",
        "signed_units": "-9/2", "commodity": "CNY",
        "reason_code": "H_NEGATIVE_INVENTORY", "reason_kind": "data_review",
        "detail": "真实库存变为负数：CNY CNY/no-cost -151/50",
        "source": r"C:\Users\Example\Documents\账本\2023\07\支付宝.bean:123",
        "ref": {"path": "2023/07/支付宝.bean", "posting_ordinal": 2},
    }


class ReviewLayoutTests(unittest.TestCase):
    def browser(self, items=None):
        outcome = pending("2023-07")
        outcome["review_items"] = items if items is not None else [inventory_item()]
        if items is None:
            outcome["status"] = "NEEDS_REVIEW"
        browser = ReportBrowser([Path("month.yaml")], Mock(return_value=outcome), render_workflow_text)
        browser.refresh()
        return browser

    def body(self, screen):
        return "\n".join(value for row, _, value in screen.rows if 3 <= row < screen.size[0] - 1)

    def test_windows_source_and_decimal_amount_are_visible_without_opening_detail(self):
        browser = self.browser()
        before = copy.deepcopy(browser.outcome)
        screen = FakeScreen(size=(24, 80))
        _draw_browser(screen, browser)
        text = self.body(screen)
        self.assertIn("2023-07-29  本笔 -4.5 CNY", text)
        self.assertIn("位置：第 123 行 · 支付宝.bean", text)
        self.assertIn("账户真实余额为 -3.02 CNY", text)
        self.assertNotIn("-9/2", text)
        self.assertNotIn("-151/50", text)
        self.assertIn("H_NEGATIVE_INVENTORY", text)
        self.assertEqual(browser.outcome, before)  # Display never changes exact audit evidence.
        self.assertIn("Enter 详情", screen.rows[-1][2])
        self.assertTrue(all(sum(map(_width, value)) < 80 for _, _, value in screen.rows))

    def test_narrow_cards_wrap_and_keep_position_before_long_account(self):
        browser = self.browser()
        screen = FakeScreen(size=(24, 32))
        _draw_browser(screen, browser)
        body = self.body(screen)
        self.assertIn("第 123 行", body)
        self.assertLess(body.index("第 123 行"), body.index("账户："))
        self.assertIn(browser.items()[0]["account"], body.replace("\n", ""))
        self.assertTrue(all(sum(map(_width, value)) < 32 for _, _, value in screen.rows))

    def test_detail_preserves_full_windows_path_and_exact_evidence_after_plain_explanation(self):
        browser = self.browser()
        browser.open_detail()
        screen = FakeScreen(size=(100, 38))
        _draw_browser(screen, browser)
        text = self.body(screen).replace("\n", "")
        self.assertIn(inventory_item()["source"], text)
        self.assertIn("本笔金额：-4.5 CNY", text)
        self.assertIn("余额为 -3.02 CNY", text)
        self.assertIn('"signed_units": "-9/2"', text)
        self.assertLess(text.index("完整路径"), text.index("原始审计资料"))
        self.assertLess(text.index("第 123 行"), text.index("原始审计资料"))

    def test_short_location_handles_windows_drive_posix_and_missing_line(self):
        self.assertEqual(_review_location(r"C:\账本\月账.bean:42"), "位置：第 42 行 · 月账.bean")
        self.assertEqual(_review_location(r"C:\账本\month.yaml"), "位置：month.yaml")
        self.assertEqual(_review_location("/ledger/month.yaml:8"), "位置：第 8 行 · month.yaml")
        self.assertEqual(_review_location(None), "位置：未提供")

    def test_multiline_error_keeps_source_position_and_amounts_readable(self):
        error = BudgetError("H_OWNER_NEGATIVE", "主动资产用途划拨不能透支交易前余额\n"
                            "账户：Assets:Bank-银行:中国:工商银行\n"
                            "资金用途：额外储蓄\n"
                            "划拨前余额：0 CNY（可划出 0 CNY）\n"
                            "本次净划出：1200 CNY\n本次缺口：1200 CNY\n"
                            "划拨后余额：-1200 CNY",
                            r"C:\Users\Example\Documents\账本\2023\10\icbc.bean:51")
        browser = ReportBrowser([Path("month.yaml")], Mock(side_effect=error), render_workflow_text)
        browser.refresh()
        for width in (40, 80):
            with self.subTest(width=width):
                screen = FakeScreen(size=(40, width))
                _draw_browser(screen, browser)
                body = self.body(screen)
                text = body.replace("\n", "")
                self.assertIn("位置：第 51 行 · icbc.bean", text)
                self.assertLess(text.index("第 51 行"), text.index("账户："))
                self.assertIn("资金用途：额外储蓄", text)
                self.assertIn("划拨前余额：0 CNY（可划出 0 CNY）", text)
                self.assertIn("本次净划出：1200 CNY", text)
                self.assertIn("本次缺口：1200 CNY", text)
                self.assertIn(error.source, text)
                self.assertNotIn(r"\n", body)
                self.assertTrue(all(sum(map(_width, value)) < width for _, _, value in screen.rows))

    def test_config_header_is_short_and_full_path_remains_in_help(self):
        browser = self.browser()
        browser.configs = [Path("/very/long/project/root/budget/2023-07/compiled/month.yaml")]
        screen = FakeScreen(size=(100, 80))
        _draw_browser(screen, browser)
        self.assertIn("2023-07/compiled/month.yaml", screen.rows[1][2])
        self.assertNotIn("/very/long", screen.rows[1][2])
        browser.help = True
        _draw_browser(screen, browser)
        self.assertIn(str(browser.config), self.body(screen).replace("\n", ""))

    def test_cost_lot_negative_is_not_mislabelled_as_whole_account_balance(self):
        item = inventory_item()
        item["detail"] = "真实库存变为负数：FUND FUND/100CNY -1/2"
        self.assertIn("批次 FUND/100CNY真实余额为 -0.5 FUND", _review_reason(item))
        self.assertNotIn("账户真实余额", _review_reason(item))

    def test_selecting_wrapped_items_keeps_their_header_and_position_visible(self):
        browser = self.browser(pending()["review_items"] * 4)
        screen = FakeScreen(size=(14, 64))
        for selected in (0, 1, 8, 11, 3, 0):
            browser.selected = selected
            _draw_browser(screen, browser)
            self.assertIn(f"> {selected + 1}. ", self.body(screen))
            self.assertIn(_review_location(browser.items()[selected]["source"]), self.body(screen))

    def test_page_movement_uses_card_heights_and_end_opens_last_item(self):
        items = pending()["review_items"] * 5
        browser = self.browser(items)
        _, spans = _review_layout(items, 0, 79, False)
        step = _review_page_target(browser, 20, 79, True)
        self.assertGreater(step, 0)
        self.assertLess(step, len(items) - 1)
        self.assertGreaterEqual(spans[step][0], 20)
        browser.selected = step
        self.assertEqual(_review_page_target(browser, 20, 79, False), 0)
        keys = (curses.KEY_NPAGE, curses.KEY_PPAGE, "G", "\n", "G", "\x1b", "g", "q")
        screen = FakeScreen(keys=keys, size=(24, 80))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        frames = ["\n".join(value for _, _, value in frame) for frame in screen.frames]
        self.assertTrue(any(f"> {len(items)}. " in frame for frame in frames))
        self.assertTrue(any("待审详情" in frame for frame in frames))
        self.assertEqual(browser.selected, 0)
        self.assertEqual(browser.offset, 0)

    def test_decimal_filter_matches_displayed_amount_and_balance(self):
        browser = self.browser()
        for query in ("-4.5", "-3.02", "第 123 行", "-9/2"):
            browser.filter(query)
            self.assertEqual(len(browser.reviews()), 1)
        browser.filter("unmatched")
        screen = FakeScreen(size=(12, 80))
        _draw_browser(screen, browser)
        self.assertIn("没有匹配待审项", self.body(screen))
        self.assertIn("待审总数 1", screen.rows[2][2])

    def test_refresh_replaces_stale_physical_cells_between_long_and_short_views(self):
        browser = self.browser()
        screen = PhysicalScreen(size=(30, 80))
        _draw_browser(screen, browser)
        screen.physical[20] = "old wide-character message outside curses' virtual contents"
        browser.filter("no matches")
        _draw_browser(screen, browser)
        self.assertNotIn(20, screen.physical)
        self.assertIn("没有匹配待审项", "\n".join(screen.physical.values()))
        self.assertNotIn("H_NEGATIVE_INVENTORY", "\n".join(screen.physical.values()))

    def test_short_inline_status_clears_the_rest_of_the_previous_row(self):
        screen = PhysicalScreen(size=(10, 80))
        _put(screen, 9, "很长的核对消息以及以前残留的文件路径和账户名")
        screen.refresh()
        screen.erase()
        _put(screen, 9, "完成")
        screen.refresh()
        self.assertEqual(screen.physical[9], "完成")

    def test_field_dialog_clears_rows_left_by_previous_larger_screen(self):
        screen = PhysicalScreen(size=(10, 80), keys=("\n",))
        screen.physical[7] = "obsolete error tail"
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _read_field(screen, "填写", ["简短说明"], "输入：")
        self.assertNotIn("obsolete error tail", "\n".join(screen.physical.values()))

    def test_windows_selection_keeps_complete_account_reason_and_code_after_moving(self):
        first = inventory_item()
        first["reason_code"] = "FUND_SOURCE_INSUFFICIENT"
        first["detail"] = "已审用途库存不足以覆盖本次转出；核对金额及此前来源，不会自动挪用其他用途。"
        second = {**first, "account": "Assets:Digital-电子钱包:支付宝:余额",
                  "reason_code": "FUND_SOURCE_UNRESOLVED",
                  "detail": "转入用途依赖尚未审定的转出资金；先处理转出分录，再重新计算。"}
        browser = self.browser([first, second])
        screen = StyledTailScreen(size=(30, 140))
        with patch("beancount_budget_tool.v6.tui.sys.platform", "win32"):
            for selected in (0, 1, 0):
                browser.selected = selected
                _draw_browser(screen, browser)
                body = self.body(screen)
                self.assertIn(f"> {selected + 1}. [选中]", body)
                self.assertEqual(body.count("[选中]"), 1)
                for item in (first, second):
                    self.assertIn(item["account"], body)
                    self.assertIn(item["detail"], body)
                    self.assertIn(item["reason_code"], body)
                self.assertIn("匹配 2 项", screen.rows[2][2])
                self.assertIn("q 退出", screen.rows[-1][2])
        self.assertTrue(all(style == 0 for style in screen.styles))

    def test_windows_dialog_and_text_browser_use_the_same_plain_rendering(self):
        screen = StyledTailScreen(size=(20, 80), keys=("\n",))
        with patch("beancount_budget_tool.v6.tui.sys.platform", "win32"), patch(
                "beancount_budget_tool.v6.tui.curses.curs_set"):
            _read_field(screen, "完整的中文填写标题不能因高亮而截断", ["上下文"], "填写：")
            self.assertIn("完整的中文填写标题不能因高亮而截断", screen.rows[0][2])
            _draw(screen, ["v6 完整的中文报告标题不能因高亮而截断", "内容"], 0)
            self.assertIn("完整的中文报告标题不能因高亮而截断", screen.rows[0][2])
        self.assertTrue(all(style == 0 for style in screen.styles))

    def test_review_selection_does_not_depend_on_attributes_on_other_platforms(self):
        browser = self.browser()
        screen = StyledTailScreen(size=(24, 100))
        with patch("beancount_budget_tool.v6.tui.sys.platform", "darwin"):
            _draw_browser(screen, browser)
        self.assertIn(inventory_item()["account"], self.body(screen))
        self.assertIn("H_NEGATIVE_INVENTORY", self.body(screen))
        self.assertIn("> 1. [选中]", self.body(screen))


if __name__ == "__main__":
    unittest.main()

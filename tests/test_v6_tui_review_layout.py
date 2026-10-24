"""Review cards keep source positions readable on Windows and narrow terminals."""

import copy
import curses
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from beancount_budget_tool.v6.monthly_report import render_workflow_text
from beancount_budget_tool.v6.tui import (
    ReportBrowser, _draw_browser, _report_browser, _review_layout,
    _review_location, _review_page_target, _review_reason, _width,
)
from test_v6_tui import FakeScreen, pending


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


if __name__ == "__main__":
    unittest.main()

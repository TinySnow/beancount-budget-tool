"""Sorting is a display preference; paging never changes financial evidence."""

from copy import deepcopy
import curses
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from beancount_budget_tool.v6 import tui
from beancount_budget_tool.v6.account_queries import render_text
from beancount_budget_tool.v6.diagnostic_history import render_text as render_diagnostic
from test_v6_tui import FakeScreen


class HistoryNavigationTests(unittest.TestCase):
    def history(self, diagnostic=False):
        history = {
            "account": "Assets:Bank", "purpose": "生活费", "from": "2026-09-01",
            "through": "2026-09-30", "opening_positions": [], "closing_positions": [],
            "note": "余额按交易整体更新",
            "events": [{"date": "2026-09-01", "purpose": "生活费", "commodity": "CNY",
                        "delta": "10", "balance": str(10 * index), "source_label": f"账本.bean:{index}",
                        "narration": f"交易{index:03d}", "reason": "原始依据"} for index in range(1, 31)],
        }
        result = {"report": history, "as_of": "2026-09-30", "approval_month": "2026-09", "note": "只读查询"}
        if diagnostic:
            result.update(report=None, history=history, status="DIAGNOSTIC_ONLY", before_source="账本.bean:31")
        return result

    def test_history_toggle_reverses_whole_same_day_events_without_changing_balances_or_envelope(self):
        for diagnostic in (False, True):
            with self.subTest(diagnostic=diagnostic):
                result = self.history(diagnostic)
                original = deepcopy(result)
                renderer = render_diagnostic if diagnostic else render_text
                rendered = []

                def record(view):
                    rendered.append(deepcopy(view))
                    return renderer(view)

                screen = FakeScreen(keys=(" ", "o", "O", "\x1b"), size=(24, 100))
                browser = SimpleNamespace(newest_first=True)
                tui._history_message(screen, browser, "用途流水", result, record)
                key = "history" if diagnostic else "report"
                events = original[key]["events"]
                self.assertEqual([row[key]["events"] for row in rendered], [list(reversed(events)), events, list(reversed(events))])
                self.assertEqual(result, original)
                self.assertTrue(browser.newest_first)
                if diagnostic:
                    self.assertTrue(all(row["report"] is None and row["status"] == "DIAGNOSTIC_ONLY" for row in rendered))
                # Sorting resets paging: both the title and the start of the
                # freshly ordered event list appear immediately after toggling.
                for frame, label, first in ((screen.frames[2], "正序", "交易001"),
                                            (screen.frames[3], "倒序", "交易030")):
                    text = "\n".join(line for _, _, line in frame)
                    self.assertIn(label, text)
                    self.assertIn(first, text)

    def test_message_page_shortcuts_and_windows_key_codes(self):
        # Each page has six rows. Integer ASCII values exercise the getch
        # fallback used by terminals without a working Unicode key reader.
        keys = (ord(" "), ord("b"), curses.KEY_NPAGE, curses.KEY_PPAGE,
                curses.KEY_END, curses.KEY_HOME, ord("G"), ord("g"), ord("j"), ord("k"), 27)
        screen = FakeScreen(keys=keys, size=(8, 80))
        with patch.object(tui.sys, "platform", "win32"):
            tui._message(screen, "长流水", [f"流水行{index:03d}" for index in range(40)])
        first_rows = [next(text for row, _, text in frame if row == 1) for frame in screen.frames]
        self.assertEqual(first_rows, [f"流水行{index:03d}" for index in (0, 6, 0, 6, 0, 34, 0, 34, 0, 1, 0)])
        self.assertTrue(all("b/PgUp" in frame[-1][2] for frame in screen.frames))

    def test_empty_history_and_tiny_windows_can_toggle_and_return(self):
        result = self.history()
        result["report"]["events"] = []
        for size in ((1, 1), (2, 5), (24, 80)):
            with self.subTest(size=size):
                screen = FakeScreen(keys=("O", curses.KEY_END, "b", "\x1b"), size=size)
                browser = SimpleNamespace(newest_first=True)
                tui._history_message(screen, browser, "用途流水", result, render_text)
                self.assertFalse(browser.newest_first)
                self.assertEqual(result["report"]["events"], [])

    def test_transaction_list_order_paging_filter_and_refresh_are_display_only(self):
        rows = [{"date": "2026-09-01", "narration": f"交易{index:03d}", "source": f"账本.bean:{index}",
                 "postings": []} for index in range(1, 31)]
        outcome = {"month": "2026-09", "status": "CERTIFIED_SCOPED", "review_items": [],
                   "report": {"transactions": {"rows": rows, "count": 30}, "owner_names": {}}}
        original = deepcopy(outcome)
        browser = tui.ReportBrowser([Path("config.yml")], Mock(return_value=outcome), lambda _: "月报")
        browser.refresh()
        browser.toggle_transactions()
        self.assertEqual(browser.transactions(), list(reversed(rows)))
        with patch("beancount_budget_tool.v6.transaction_details.render_transaction",
                   side_effect=lambda row, *args, **kwargs: [row["narration"]]):
            browser.filter("交易00")
            self.assertEqual(browser.transactions(), list(reversed(rows[:9])))
            browser.selected = 5
            browser.offset = 4
            browser.toggle_order()
            self.assertEqual(browser.transactions(), rows[:9])
            self.assertEqual((browser.selected, browser.offset), (0, 0))
            browser.refresh()
            self.assertFalse(browser.newest_first)
            self.assertEqual(browser.transactions(), rows[:9])
        browser.filter("")
        browser.view = "report"
        # The outer browser covers budget/category-filtered transaction lists,
        # not only h history popups. A page moves six entries on a 10-row screen.
        screen = FakeScreen(keys=("a", " ", "b", curses.KEY_END, curses.KEY_HOME, "o", "O", "q"), size=(10, 120))
        with patch.object(tui.curses, "curs_set"), patch.object(tui, "_init_colors"):
            tui._report_browser(screen, browser)
        listing_frames = [frame for frame in screen.frames if any("[选中]" in text for _, _, text in frame)]
        selected = [next(text for _, _, text in frame if "[选中]" in text) for frame in listing_frames]
        self.assertIn("交易001", selected[0])
        self.assertIn("交易007", selected[1])
        self.assertIn("交易001", selected[2])
        self.assertIn("交易030", selected[3])
        self.assertIn("交易001", selected[4])
        self.assertIn("交易030", selected[5])
        self.assertIn("交易001", selected[6])
        self.assertEqual(outcome, original)


if __name__ == "__main__":
    unittest.main()

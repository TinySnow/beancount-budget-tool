"""The terminal browser shares report text and handles narrow screens."""

import unittest
from unittest.mock import patch

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.tui import _browse, _clip, _draw, run_tui


class FakeScreen:
    def __init__(self, keys=(), size=(5, 24)):
        self.keys = iter(keys)
        self.size = size
        self.rows = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows = []

    def addstr(self, row, col, value, *_style):
        self.rows.append((row, col, value))

    def refresh(self):
        pass

    def keypad(self, _enabled):
        pass

    def getch(self):
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


if __name__ == "__main__":
    unittest.main()

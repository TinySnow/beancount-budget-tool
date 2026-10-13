"""Read-only terminal viewer for the already audited v6 monthly report."""

from __future__ import annotations

import curses
import sys
import unicodedata

from ..errors import BudgetError


def _width(character: str) -> int:
    return 2 if unicodedata.east_asian_width(character) in "WF" else 1


def _clip(line: str, columns: int) -> str:
    if columns <= 0:
        return ""
    result = []
    used = 0
    for character in line:
        width = _width(character)
        if used + width > columns:
            break
        result.append(character)
        used += width
    return "".join(result)


def _draw(stdscr, lines: list[str], offset: int) -> int:
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    if height < 3 or width < 4:
        stdscr.refresh()
        return offset
    page = height - 2
    offset = max(0, min(offset, max(0, len(lines) - page)))
    header = lines[0] if lines and lines[0].startswith("v6 ") else "v6 月报"
    footer = f"↑↓ 滚动  PgUp/PgDn 翻页  g/G 首尾  q 退出  {offset + 1}-{min(offset + page, len(lines))}/{len(lines)}"
    for row, content, style in (
            (0, header, curses.A_REVERSE | curses.A_BOLD),
            (height - 1, footer, curses.A_REVERSE)):
        try:
            stdscr.addstr(row, 0, _clip(content, width - 1), style)
        except curses.error:
            pass
    for row in range(page):
        index = offset + row
        if index >= len(lines):
            break
        try:
            stdscr.addstr(row + 1, 0, _clip(lines[index], width - 1))
        except curses.error:
            pass
    stdscr.refresh()
    return offset


def _browse(stdscr, lines: list[str]) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    offset = 0
    while True:
        offset = _draw(stdscr, lines, offset)
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key in (curses.KEY_UP, ord("k")):
            offset -= 1
        elif key in (curses.KEY_DOWN, ord("j")):
            offset += 1
        elif key in (curses.KEY_PPAGE,):
            offset -= max(1, stdscr.getmaxyx()[0] - 2)
        elif key in (curses.KEY_NPAGE, ord(" ")):
            offset += max(1, stdscr.getmaxyx()[0] - 2)
        elif key in (curses.KEY_HOME, ord("g")):
            offset = 0
        elif key in (curses.KEY_END, ord("G")):
            offset = len(lines)


def run_tui(text: str) -> None:
    """Browse the same text returned by monthly_report.render_text."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BudgetError("TUI_TERMINAL_REQUIRED", "TUI 需要交互式终端；脚本中请使用 text 或 json")
    try:
        curses.wrapper(_browse, text.splitlines())
    except KeyboardInterrupt:
        return
    except curses.error as exc:
        raise BudgetError("TUI_UNAVAILABLE", "当前终端无法打开 TUI") from exc

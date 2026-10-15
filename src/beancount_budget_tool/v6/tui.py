"""Read-only terminal browsing and refresh over audited v6 monthly workflows."""

from __future__ import annotations

import curses
import json
from pathlib import Path
import sys
import unicodedata
from collections.abc import Callable

from ..errors import BudgetError


def _width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
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


def _clean(line: str) -> str:
    return "".join(character if character.isprintable() else
                   "    " if character == "\t" else character.encode("unicode_escape").decode("ascii")
                   for character in line)


def _wrap(lines: list[str], columns: int) -> list[str]:
    """Wrap complete source paths and reasons using terminal cell widths."""
    columns = max(2, columns)
    wrapped = []
    for line in lines:
        current, used = "", 0
        for character in _clean(line):
            width = _width(character)
            if used + width > columns:
                wrapped.append(current)
                current, used = "", 0
            current += character
            used += width
        wrapped.append(current)
    return wrapped


class ReportBrowser:
    """Display-only state over fresh complete workflow results, with no number cache."""

    def __init__(self, configs: list[Path], loader: Callable, renderer: Callable):
        self.configs = list(dict.fromkeys(path.resolve() for path in configs))
        if not self.configs:
            raise BudgetError("TUI_CONFIG_REQUIRED", "至少提供一份月报配置")
        self.loader, self.renderer = loader, renderer
        self.index = 0
        self.query = ""
        self.view = "report"
        self.help = False
        self.selected = 0
        self.offset = 0
        self.clear()

    @property
    def config(self) -> Path:
        return self.configs[self.index]

    def clear(self):
        self.outcome = None
        self.error = None
        self.lines = []
        self.detail = None
        self.selected = self.offset = 0
        self.help = False

    def refresh(self):
        self.clear()
        try:
            outcome = self.loader(self.config)
            # Blocked workflows may never expose an earlier or partial report.
            if outcome["status"] != "CERTIFIED_SCOPED":
                outcome = {**outcome, "report": None}
            rendered = self.renderer(outcome)
            self.outcome, self.lines = outcome, rendered.splitlines()
            self.view = "reviews" if outcome.get("review_items") else "report"
        except BudgetError as exc:
            self.error = exc
        except (OSError, UnicodeError) as exc:
            self.error = BudgetError("TUI_INPUT_ERROR", str(exc), str(getattr(exc, "filename", None) or self.config))

    def switch(self, direction: int) -> bool:
        target = max(0, min(len(self.configs) - 1, self.index + direction))
        if target == self.index:
            return False
        self.index = target
        self.clear()
        return True

    def filter(self, query: str):
        self.query = query
        self.selected = self.offset = 0
        self.detail = None
        self.help = False

    def reviews(self) -> list[dict]:
        items = self.outcome.get("review_items", []) if self.outcome else []
        query = self.query.casefold()
        return [item for item in items if not query or query in json.dumps(
            item, ensure_ascii=False, sort_keys=True).casefold()]

    def report_lines(self) -> list[str]:
        if not self.query:
            return self.lines
        query = self.query.casefold()
        # A filtered report must not hide deficit warnings or its headline.
        return [line for index, line in enumerate(self.lines)
                if index < 2 or "警告" in line or query in line.casefold()] or ["没有匹配行"]

    def open_detail(self):
        items = self.reviews()
        if self.view == "reviews" and items:
            self.selected = max(0, min(self.selected, len(items) - 1))
            self.detail = items[self.selected]
            self.offset = 0

    def title(self) -> str:
        if self.error:
            state = "计算失败（无可用月报）"
        elif self.outcome:
            state = f"{self.outcome['month']} {self.outcome['status']}"
        else:
            state = "正在重算（旧结果已清除）"
        return f"v6 {state} [{self.index + 1}/{len(self.configs)}]"


HELP = [
    "按键帮助", "r：读取当前账本与配置，重新运行全部月报审计；计算期间旧结果清除。",
    "[ / ]：按命令提供的顺序切换配置，每次重新审计，不复用其他月份的声明或数字。",
    "/：输入关键词筛选；支持中文、账户、用途、原因码和来源路径。空查询清除筛选。",
    "t：切换完整报告文本和待审列表。待审状态只显示未通过项目，不显示局部正式数字。",
    "↑↓ 或 j/k：滚动文本；在待审列表选择项目。PgUp/PgDn 或空格翻页，g/G 到首尾。",
    "Enter：打开选中待审的完整详情，包含原因、文件行号和全部来源引用。",
    "Esc：关闭详情／帮助，或清除筛选；无这些状态时退出。q 退出。? 打开帮助。",
    "此界面只读；类别／用途决定用 beancount-budget-review 保存后，按 r 重算。",
    "筛选只改变显示，不改变计算、认证状态或待审总数。警告不会随报告筛选隐藏。",
]


def _put(stdscr, row: int, text: str, style=0):
    try:
        stdscr.addstr(row, 0, _clip(_clean(text), max(0, stdscr.getmaxyx()[1] - 1)), style)
    except curses.error:
        pass


def _draw_browser(stdscr, browser: ReportBrowser):
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    _put(stdscr, 0, browser.title(), curses.A_REVERSE | curses.A_BOLD)
    if height < 5 or width < 8:
        if height > 1:
            _put(stdscr, 1, "窗口过小；放大终端，q 退出")
        stdscr.refresh()
        return
    _put(stdscr, 1, f"配置：{browser.config}")
    page = height - 4
    total_reviews = len(browser.outcome.get("review_items", [])) if browser.outcome else 0
    items = browser.reviews()
    subtitle = f"{'待审列表' if browser.view == 'reviews' else '报告文本'}；待审总数 {total_reviews}"
    if browser.query:
        subtitle += f"；筛选：{browser.query}"
    listing = browser.outcome is not None and browser.view == "reviews" and not (
        browser.error or browser.detail or browser.help)
    if browser.error:
        lines = ["本次计算失败；旧月报已清除。修正文件后按 r 重算。",
                 f"{browser.error.code}：{browser.error.message}", f"来源：{browser.error.source or '未提供'}"]
        subtitle = "计算失败；不显示旧数字或局部月报"
    elif browser.outcome is None:
        lines = ["正在读取当前账本与配置并运行审计。旧结果已清除，请等待。"]
        subtitle = "审计进行中；尚不提供待审总数或预算数字"
    elif browser.help:
        lines = [*HELP, f"当前配置完整路径：{browser.config}"]
        subtitle = "帮助；Esc 返回"
    elif browser.detail:
        item = browser.detail
        lines = ["待审详情（当前审计快照；只读）",
                 f"原因：{item.get('reason_code', '')} {item.get('detail', '')}",
                 f"位置：{item.get('source') or '未提供'}", "",
                 *json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True).splitlines()]
        subtitle = "待审详情；Esc 返回列表"
    elif listing:
        subtitle += f"；匹配 {len(items)} 项"
        if (browser.outcome or {}).get("review_queue", {}).get("preflight_only"):
            subtitle += "；仅当前预检阻断层，其余尚未检查"
        browser.selected = max(0, min(browser.selected, max(0, len(items) - 1)))
        browser.offset = min(browser.offset, browser.selected)
        browser.offset = max(browser.offset, browser.selected - page + 1)
        browser.offset = max(0, min(browser.offset, max(0, len(items) - page)))
        lines = []
        for index, item in enumerate(items):
            label = "程序实现缺口" if item.get("reason_kind") == "implementation_limit" else "待审"
            lines.append(f"{'>' if index == browser.selected else ' '} {index + 1}. {label} "
                         f"{item.get('date', '')} {item.get('account', '')} "
                         f"{item.get('signed_units', '')} {item.get('commodity', '')} "
                         f"{item.get('reason_code', '')}：{item.get('detail', '')} [{item.get('source', '')}]")
        if not lines:
            lines = ["没有匹配待审项；筛选不改变上方待审总数与认证状态。"]
    else:
        lines = browser.report_lines()
    _put(stdscr, 2, subtitle, curses.A_BOLD)
    if not listing:
        lines = _wrap(lines, width - 1)
        browser.offset = max(0, min(browser.offset, max(0, len(lines) - page)))
    for row, line in enumerate(lines[browser.offset:browser.offset + page], 3):
        selected = listing and row - 3 + browser.offset == browser.selected and bool(items)
        _put(stdscr, row, line, curses.A_REVERSE if selected else 0)
    footer = "r 重算  / 筛选  t 视图  Enter 详情  [ ] 切配置  ? 帮助  q 退出"
    _put(stdscr, height - 1, footer, curses.A_REVERSE)
    stdscr.refresh()


def _read_key(stdscr):
    value = stdscr.get_wch() if hasattr(stdscr, "get_wch") else stdscr.getch()
    return chr(value) if type(value) is int and 0 <= value < 256 else value


def _read_filter(stdscr, original: str) -> str:
    value = original
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            row = max(0, stdscr.getmaxyx()[0] - 1)
            _put(stdscr, row, " " * max(0, stdscr.getmaxyx()[1] - 1))
            _put(stdscr, row, "/" + value)
            stdscr.refresh()
            key = _read_key(stdscr)
            if key in ("\n", "\r", curses.KEY_ENTER):
                return value
            if key == "\x1b":
                return original
            if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                value = value[:-1]
            elif key == "\x15":
                value = ""
            elif isinstance(key, str) and key.isprintable() and len(value) < 512:
                value += key
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _report_browser(stdscr, browser: ReportBrowser):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)

    def reload():
        browser.clear()
        _draw_browser(stdscr, browser)
        browser.refresh()

    reload()
    while True:
        _draw_browser(stdscr, browser)
        key = _read_key(stdscr)
        page = max(1, stdscr.getmaxyx()[0] - 4)
        listing = browser.view == "reviews" and not (browser.detail or browser.help or browser.error)
        if key == "q":
            return
        if key == "\x1b":
            if browser.detail or browser.help:
                browser.detail, browser.help, browser.offset = None, False, 0
            elif browser.query:
                browser.filter("")
            else:
                return
        elif key == "r":
            reload()
        elif key in ("[", "]"):
            if browser.switch(-1 if key == "[" else 1):
                reload()
        elif key == "/":
            browser.filter(_read_filter(stdscr, browser.query))
        elif key == "?":
            browser.help, browser.offset = not browser.help, 0
        elif key == "t":
            browser.view = "reviews" if browser.view == "report" else "report"
            browser.detail, browser.help, browser.offset = None, False, 0
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if browser.help:
                browser.help, browser.offset = False, 0
            else:
                browser.open_detail()
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j", curses.KEY_PPAGE,
                     curses.KEY_NPAGE, " ", curses.KEY_HOME, "g", curses.KEY_END, "G"):
            delta = -1 if key in (curses.KEY_UP, "k") else 1
            if key == curses.KEY_PPAGE:
                delta = -page
            elif key in (curses.KEY_NPAGE, " "):
                delta = page
            attribute = "selected" if listing else "offset"
            value = getattr(browser, attribute) + delta
            if key in (curses.KEY_HOME, "g"):
                value = 0
            elif key in (curses.KEY_END, "G"):
                value = len(browser.reviews()) - 1 if listing else sys.maxsize
            setattr(browser, attribute, max(0, value))


def run_report_tui(configs: list[Path], loader: Callable, renderer: Callable) -> None:
    """Run foreground audits on demand; never carry numbers across config files."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BudgetError("TUI_TERMINAL_REQUIRED", "TUI 需要交互式终端；脚本中请使用 text 或 json")
    browser = ReportBrowser(configs, loader, renderer)
    try:
        curses.wrapper(_report_browser, browser)
    except KeyboardInterrupt:
        return
    except curses.error as exc:
        raise BudgetError("TUI_UNAVAILABLE", "当前终端无法打开 TUI") from exc

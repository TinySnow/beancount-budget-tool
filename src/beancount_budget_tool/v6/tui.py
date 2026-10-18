"""Terminal monthly browsing, refresh and explicitly approved source decisions."""

from __future__ import annotations

import curses
from datetime import datetime
import json
from pathlib import Path
import sys
import unicodedata
from uuid import uuid4
from collections.abc import Callable

from ..errors import BudgetError, require


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
    """Fresh workflow results and the current configuration for each supplied month."""

    def __init__(self, configs: list[Path], loader: Callable, renderer: Callable, *, project=None,
                 initial_config: Path | None = None):
        self.configs = list(dict.fromkeys(path.resolve() for path in configs))
        if not self.configs:
            raise BudgetError("TUI_CONFIG_REQUIRED", "至少提供一份月报配置")
        self.loader, self.renderer = loader, renderer
        self.project = project
        self.project_error = None
        # Reopening a saved configuration continues in the same record root;
        # repeated sessions must not grow paths indefinitely on Windows.
        self.review_roots = [path.parent.parent.parent if
                             path.name == "month.yaml" and path.parent.name == "reviewed" and
                             path.parent.parent.parent.name == "tui-reviews" else
                             path.parent / "tui-reviews" for path in self.configs]
        self.saved = set()
        self.next_worksheets: dict[Path, Path] = {}
        self.index = self.configs.index(initial_config.resolve()) if initial_config is not None else 0
        self.period = False
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
            if self.period:
                from .period_report import audit_period, render_text
                outcome = audit_period(list(self.configs))
                renderer = render_text
            else:
                outcome = self.loader(self.config)
                renderer = self.renderer
            self._accept_outcome(outcome, renderer)
        except BudgetError as exc:
            self.error = exc
        except (OSError, UnicodeError) as exc:
            self.error = BudgetError("TUI_INPUT_ERROR", str(exc), str(getattr(exc, "filename", None) or self.config))

    def _accept_outcome(self, outcome: dict, renderer: Callable):
        """Display a fresh audit result, including one returned after publication."""
        previous_view = self.view
        if outcome["status"] != "CERTIFIED_SCOPED":
            outcome = {**outcome, "report": None}
        rendered = renderer(outcome)
        self.outcome, self.lines = outcome, rendered.splitlines()
        self.view = "reviews" if outcome.get("review_items") else "report"
        if previous_view == "transactions" and self.has_transactions():
            self.view = "transactions"

    def show_saved_month(self, result: dict):
        """Keep the published path even if its following audit could not finish."""
        previous = self.config
        self.configs[self.index] = Path(result["config"]).resolve()
        self.remember_current_config(previous)
        self.saved.add(self.index)
        self.period = False
        self.filter("")
        self.clear()
        if result.get("report_error"):
            error = result["report_error"]
            self.error = BudgetError(error["code"], error["message"], error.get("source"))
            return
        try:
            self._accept_outcome(result["workflow"], self.renderer)
        except BudgetError as exc:
            self.error = exc
        except (OSError, UnicodeError) as exc:
            self.error = BudgetError("TUI_INPUT_ERROR", str(exc), str(self.config))

    def remember_current_config(self, previous: Path):
        if self.project is None:
            return
        try:
            self.project.remember(self.config, previous)
            self.project_error = None
        except (BudgetError, OSError, UnicodeError) as exc:
            self.project_error = exc

    def project_messages(self) -> list[str]:
        if self.project_error is None:
            return []
        return ["新配置已保存，但项目入口尚未更新；重启前请核对项目文件或显式使用新配置路径。",
                f"项目入口：{self.project.path}", str(self.project_error)]

    def month_choices(self) -> list[tuple[str, Path]]:
        from .project import _metadata
        return [(_metadata(path)[0], path) for path in self.configs]

    def jump_month(self, month: str):
        from .targets import _month
        month = _month(month)
        matches = [index for index, (label, _) in enumerate(self.month_choices()) if label == month]
        require(bool(matches), "TUI_MONTH_MISSING", f"没有载入 {month}；请先准备该月配置或登记到项目入口")
        require(len(matches) == 1, "TUI_MONTH_DUPLICATE", f"{month} 有多份配置；请保留该月最新入口")
        self.index = matches[0]
        self.period = False
        self.filter("")
        self.clear()

    def show_next_month(self, result: dict):
        """Retain the current month's latest path, then select its successor."""
        target = self.index + 1
        path = Path(result["config"]).resolve()
        self.configs.insert(target, path)
        self.review_roots.insert(target, path.parent / "tui-reviews")
        self.saved = {index + (index >= target) for index in self.saved}
        self.index = target
        self.show_saved_month(result)

    def switch(self, direction: int) -> bool:
        target = max(0, min(len(self.configs) - 1, self.index + direction))
        if target == self.index:
            return False
        self.index = target
        self.period = False
        self.clear()
        return True

    def toggle_period(self):
        self.period = not self.period
        self.view = "report"
        self.filter("")
        self.clear()

    def open_review_month(self) -> bool:
        """Leave the interval for the latest supplied config of a pending month."""
        if not self.period or self.view != "reviews" or self.error or self.help:
            return False
        items = self.reviews()
        if not items:
            return False
        item = self.detail or items[max(0, min(self.selected, len(items) - 1))]
        target = Path(item["config"]).resolve()
        if target not in self.configs:
            return False
        self.index = self.configs.index(target)
        self.period = False
        self.filter("")
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

    def has_transactions(self) -> bool:
        return bool(not self.period and self.outcome and self.outcome["status"] == "CERTIFIED_SCOPED" and
                    self.outcome.get("report") and self.outcome["report"].get("transactions") is not None)

    def transactions(self) -> list[dict]:
        items = self.outcome["report"]["transactions"]["rows"] if self.has_transactions() else []
        query = self.query.casefold()
        if not query:
            return items
        from .transaction_details import render_transaction
        names = self.outcome["report"]["owner_names"] if self.has_transactions() else {}
        # Search what the user can read, including decimal display amounts.
        # Short queries must not accidentally match opaque source hashes.
        return [item for item in items if query in "\n".join(render_transaction(item, names)).casefold()]

    def items(self) -> list[dict]:
        return self.transactions() if self.view == "transactions" else self.reviews()

    def toggle_transactions(self):
        if not self.has_transactions():
            return
        self.view = "report" if self.view == "transactions" else "transactions"
        self.selected = self.offset = 0
        self.detail, self.help = None, False

    def report_lines(self) -> list[str]:
        if not self.query:
            return self.lines
        query = self.query.casefold()
        # A filtered report must not hide deficit warnings or its headline.
        return [line for index, line in enumerate(self.lines)
                if index < 2 or "警告" in line or query in line.casefold()] or ["没有匹配行"]

    def open_detail(self):
        items = self.items()
        if self.view in {"reviews", "transactions"} and items:
            self.selected = max(0, min(self.selected, len(items) - 1))
            self.detail = items[self.selected]
            self.offset = 0

    def title(self) -> str:
        scope = "区间 " if self.period else ""
        if self.error:
            state = "计算失败（无可用月报）"
        elif self.outcome:
            label = (f"{self.outcome['from']} 至 {self.outcome['through']}" if self.period else
                     self.outcome["month"])
            state = f"{label} {self.outcome['status']}"
        else:
            state = "正在重算（旧结果已清除）"
        return f"v6 {scope}{state} [{self.index + 1}/{len(self.configs)}]"


HELP = [
    "按键帮助", "r：读取当前账本与配置，重新运行全部月报审计；计算期间旧结果清除。",
    "[ / ]：按命令提供的顺序切换配置，每次重新审计，不复用其他月份的声明或数字。",
    "m：月报中输入 YYYY-MM 直接跳到已载入的月份；每次重新审计。区间待审中仍转到所选待审月份。",
    "项目模式会在保存审阅／月末声明或载入下一月后更新项目入口，默认进入上次保存的月份；登记路径不代表签发。",
    "n：准备下一月填写表；用编辑器填好后，再按 n 选择载入。未确认的目标／账户继续列待审，不自动沿用月末声明。",
    "下一月保存后自动加入当前配置清单，可用 [／] 返回前月；重启时仍须提供已保存配置路径。",
    "c：切换当前月报与连续月份比较；使用命令提供的全部最新配置，区间内不能缺月。",
    "区间重新审计每个月：只累计流量，目标差逐月比较，余额与可花取期末，任何月待审均不显示区间数字。",
    "m：在区间待审列表／详情中转到所选月份，重新审计后可按 e 填写；保存的新配置会用于下次区间比较。",
    "x：导出当前月份／区间的完整 text、json 或 csv；填写格式与新文件路径，Esc 取消。",
    "导出前重新审计，使用最新已保存配置，忽略显示筛选；待审时只导出待审清单。CSV 使用 UTF-8 BOM。",
    "/：输入关键词筛选；支持中文、账户、用途、原因码和来源路径。空查询清除筛选。",
    "t：切换完整报告文本和待审列表。待审状态只显示未通过项目，不显示局部正式数字。",
    "a：已签发后切换交易来源列表；可按类别、资金用途、责任 ID、摘要或账户筛选。",
    "↑↓ 或 j/k：滚动文本；在待审列表选择项目。PgUp/PgDn 或空格翻页，g/G 到首尾。",
    "Enter：打开所选待审／交易详情；交易显示逐腿原币、分类、用途、责任及原始账本内容。",
    "e：填写选中的分类／H 资产用途，写理由、核对预览，明确输入 y 并回车才保存。",
    "f：本月出现 EXTERNAL_SCOPE_PENDING 时，核对截至月末已发生的漏记责任／在途付款，填写依据并明确输入 y 才保存。",
    "月末核对只声明截止日及以前的事实，不保证未来没有负债；其他待审仍须处理。存在漏记责任／在途付款时请取消。",
    "保存生成新的 tui-reviews/本次记录/reviewed/month.yaml，当前月份自动使用新配置并重审。",
    "项目模式保存后会同步月份入口；非项目模式重启时须显式提供新配置。取消填写不保存。其他待审按对应配置处理。",
    "Esc：关闭详情／帮助，或清除筛选；无这些状态时退出。q 退出。? 打开帮助。",
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
    _put(stdscr, 1, f"区间使用全部 {len(browser.configs)} 份配置；? 查看清单，c 返回月报" if browser.period else
         f"配置：{browser.config}")
    page = height - 4
    total_reviews = len(browser.outcome.get("review_items", [])) if browser.outcome else 0
    items = browser.items()
    title = {"reviews": "待审列表", "transactions": "已签发月报的交易来源", "report": "报告文本"}[browser.view]
    subtitle = f"{title}；待审总数 {total_reviews}"
    if browser.saved and browser.period:
        subtitle = "包含 TUI 已保存的新配置；" + subtitle
    elif browser.index in browser.saved:
        subtitle = "已保存，正使用新配置（? 看完整路径）；" + subtitle
    if browser.project_error is not None:
        subtitle = "项目入口未更新（? 看详情）；" + subtitle
    if browser.query:
        subtitle += f"；筛选：{browser.query}"
    listing = browser.outcome is not None and browser.view in {"reviews", "transactions"} and not (
        browser.error or browser.detail or browser.help)
    if browser.help:
        lines = [*HELP, f"当前配置完整路径：{browser.config}"]
        if browser.project is not None:
            lines += [f"项目入口：{browser.project.path}", *browser.project_messages()]
        if browser.period:
            lines += ["区间使用的最新配置：", *map(str, browser.configs)]
        subtitle = "帮助；Esc 返回"
    elif browser.error:
        lines = ["本次计算失败；旧月报已清除。修正文件后按 r 重算。",
                 f"{browser.error.code}：{browser.error.message}", f"来源：{browser.error.source or '未提供'}"]
        subtitle = "计算失败；不显示旧数字或局部月报"
    elif browser.outcome is None:
        lines = ["正在读取当前账本与配置并运行审计。旧结果已清除，请等待。"]
        subtitle = "审计进行中；尚不提供待审总数或预算数字"
    elif browser.detail:
        item = browser.detail
        if browser.view == "transactions":
            from .transaction_details import render_transaction
            lines = ["交易来源详情（当前月报快照；只读）", *render_transaction(item, browser.outcome["report"]["owner_names"])]
            subtitle = "交易来源详情；Esc 返回列表"
        else:
            lines = [("区间待审详情；按 m 转到该月审阅" if browser.period else
                      "待审详情（当前审计快照；分类／H 用途可按 e 填写）"),
                 f"原因：{item.get('reason_code', '')} {item.get('detail', '')}",
                 f"位置：{item.get('source') or '未提供'}", "",
                 *json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True).splitlines()]
            subtitle = "待审详情；Esc 返回列表" + ("，m 转到该月" if browser.period else "")
    elif listing:
        subtitle += f"；匹配 {len(items)} 项"
        if browser.view == "transactions":
            count = browser.outcome["report"]["transactions"]["count"] if browser.has_transactions() else 0
            subtitle += f"；交易总数 {count}"
        if (browser.outcome or {}).get("review_queue", {}).get("preflight_only"):
            subtitle += "；仅当前预检阻断层，其余尚未检查"
        browser.selected = max(0, min(browser.selected, max(0, len(items) - 1)))
        browser.offset = min(browser.offset, browser.selected)
        browser.offset = max(browser.offset, browser.selected - page + 1)
        browser.offset = max(0, min(browser.offset, max(0, len(items) - page)))
        lines = []
        for index, item in enumerate(items):
            if browser.view == "transactions":
                amounts = " / ".join(f"{part['signed_units']} {part['commodity']}" for part in item["postings"][:2])
                lines.append(f"{'>' if index == browser.selected else ' '} {index + 1}. {item['date']} "
                             f"{item.get('payee') or ''} {item['narration']} | {amounts} [{item['source']}]")
                continue
            label = "程序实现缺口" if item.get("reason_kind") == "implementation_limit" else "待审"
            month = f"{item['month']} " if browser.period else ""
            lines.append(f"{'>' if index == browser.selected else ' '} {index + 1}. {label} "
                         f"{month}"
                         f"{item.get('date', '')} {item.get('account', '')} "
                         f"{item.get('signed_units', '')} {item.get('commodity', '')} "
                         f"{item.get('reason_code', '')}：{item.get('detail', '')} [{item.get('source', '')}]")
        if not lines:
            lines = (["没有匹配交易；筛选不改变上方交易总数和预算数字。"] if browser.view == "transactions" else
                     ["没有匹配待审项；筛选不改变上方待审总数与认证状态。"])
    else:
        lines = browser.report_lines()
    if browser.has_transactions() and browser.view == "transactions" and not browser.help:
        warning_count = sum(line.lstrip().startswith("警告：") for line in browser.lines)
        if warning_count:
            subtitle = f"月报警告 {warning_count} 条（t 查看）；" + subtitle
    _put(stdscr, 2, subtitle, curses.A_BOLD)
    if not listing:
        lines = _wrap(lines, width - 1)
        browser.offset = max(0, min(browser.offset, max(0, len(lines) - page)))
    for row, line in enumerate(lines[browser.offset:browser.offset + page], 3):
        selected = listing and row - 3 + browser.offset == browser.selected and bool(items)
        _put(stdscr, row, line, curses.A_REVERSE if selected else 0)
    footer = ("x 导出  c 月报  m 转到该月  r 重算区间  / 筛选  Enter 详情  ? 帮助  q 退出" if browser.period else
              "m 跳月 n 下一月 f 月末 x 导出 c 比较 r 重算 / 筛选 e 审阅 ? 帮助 q 退出")
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


def _read_field(stdscr, title: str, context: list[str], prompt: str,
                original: str = "") -> str | None:
    """A cancellable Unicode field with scrollable full context and visible input tail."""
    value, offset = original, 0
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        while True:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            _put(stdscr, 0, title, curses.A_REVERSE | curses.A_BOLD)
            page = max(1, height - 4)
            lines = _wrap(context, width - 1)
            offset = max(0, min(offset, max(0, len(lines) - page)))
            if height >= 5 and width >= 8:
                for row, line in enumerate(lines[offset:offset + page], 1):
                    _put(stdscr, row, line)
                _put(stdscr, height - 3, prompt)
                _put(stdscr, height - 2, "PgUp/PgDn 查看全文；Enter 继续，Esc 取消；Ctrl+U 清空")
                tail = value
                while tail and sum(_width(c) for c in tail) > max(1, width - 3):
                    tail = tail[1:]
                _put(stdscr, height - 1, "> " + tail, curses.A_REVERSE)
            elif height > 1:
                _put(stdscr, 1, "窗口过小；放大终端或 Esc 取消")
            stdscr.refresh()
            key = _read_key(stdscr)
            if key == "\x1b":
                return None
            if height < 5 or width < 8:
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                return value
            if key in (curses.KEY_PPAGE, curses.KEY_UP):
                offset -= page if key == curses.KEY_PPAGE else 1
            elif key in (curses.KEY_NPAGE, curses.KEY_DOWN):
                offset += page if key == curses.KEY_NPAGE else 1
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                value = value[:-1]
            elif key == "\x15":
                value = ""
            elif isinstance(key, str) and key.isprintable() and len(value) < 4096:
                value += key
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _message(stdscr, title: str, lines: list[str]):
    _read_field(stdscr, title, lines, "Enter 或 Esc 返回")


def _edit_review(stdscr, browser: ReportBrowser):
    from .tui_review import prepare_review, save_review, validate_decision
    from ..exact import amount_text

    if browser.period or browser.view != "reviews" or browser.error or browser.help or not browser.reviews():
        return
    items = browser.reviews()
    item = browser.detail or items[max(0, min(browser.selected, len(items) - 1))]
    output = browser.review_roots[browser.index] / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:12])
    try:
        _put(stdscr, max(0, stdscr.getmaxyx()[0] - 1), "正在核对所选来源及可选名称……")
        stdscr.refresh()
        prepared = prepare_review(browser.config, item)
        row = prepared.row
        label = "预算类别（budget）" if row["kind"] == "classification" else "资产资金用途（fund）"
        context = [f"日期：{row['date']}  {row['payee'] or ''}  {row['narration']}",
                   f"账户：{row['account']}", f"原币分录：{row['signed_units']} {row['commodity']}",
                   f"来源：{row['source']}", f"填写：{label}",
                   "可选名称／别名（- 表示预算外）：", *prepared.names,
                   "单一名称覆盖整笔；多项如：保险200，旅游900。合计须等于分录绝对金额，不填负金额。"]
        decision, reason = "", ""
        while True:
            decision = _read_field(stdscr, "填写一项审阅决定", context, f"{label}：", decision)
            if decision is None:
                return
            reason = _read_field(stdscr, "填写审阅理由", [*context[:5], f"决定：{decision}"], "判断依据：", reason)
            if reason is None:
                return
            try:
                parts = validate_decision(prepared, decision, reason)
                break
            except BudgetError as exc:
                _message(stdscr, "填写有误；尚未保存", [str(exc)])
        preview = [*context[:5], "本次只批准这一项：",
                   *[f"{name}：{amount_text(amount)} {row['commodity']}" for name, amount in parts],
                   f"理由：{reason}", "保存后自动复审；其他待审不会被批准。",
                   f"新配置：{output / 'reviewed' / 'month.yaml'}",
                   "重启工具、导出报表或准备下一月时，使用上面的新配置。"]
        approved = _read_field(stdscr, "核对后明确批准", preview, "输入 y 并回车保存；其他输入或 Esc 取消：")
        if approved != "y":
            return
        browser.clear()
        _draw_browser(stdscr, browser)
        result = save_review(prepared, decision, reason, output, approved=True)
        previous = browser.config
        browser.configs[browser.index] = Path(result["config"]).resolve()
        browser.remember_current_config(previous)
        browser.saved.add(browser.index)
        browser.filter("")
        browser.refresh()
        lines = ["本项决定已保存；当前月份已切换到新配置并重新审计。",
                 f"以后请使用：{browser.config}", f"审阅记录：{output}"]
        if browser.error:
            lines += ["决定已保存，但月报计算仍有错误：", str(browser.error)]
        else:
            lines += [f"本月状态：{browser.outcome['status']}；待审 {len(browser.outcome.get('review_items', []))} 项。"]
        _message(stdscr, "已保存审阅决定", [*lines, *browser.project_messages()])
    except (BudgetError, OSError, UnicodeError) as exc:
        # Refresh after a stale source or rejected ownership decision. No old
        # report/detail should survive while the user repairs the input.
        browser.refresh()
        lines = [str(exc), "本次未发布新配置；请核对原因后重新选择。"]
        if output.exists():
            lines += [f"填写记录保留在：{output}", "原账本与原配置没有改写。"]
        _message(stdscr, "未保存审阅决定", lines)


def _export_report(stdscr, browser: ReportBrowser):
    from .tui_export import FORMATS, write_export

    scope = "区间报表" if browser.period else "月报"
    context = [f"导出：{scope}（完整结果，忽略显示筛选）",
               "重新读取账本与最新配置后导出；待审时只保存待审清单。",
               "格式：text 文本、json 完整结构、csv 表格。已有文件不会覆盖。",
               *[str(path) for path in (browser.configs if browser.period else [browser.config])]]
    try:
        format = _read_field(stdscr, "导出报表", context, "格式（text / json / csv）：", "text")
        if format is None:
            return
        format = format.strip().casefold()
        require(format in FORMATS, "TUI_EXPORT_FORMAT_INVALID", "导出格式须为 text、json 或 csv")
        label = ("period" if browser.period else (browser.outcome or {}).get("month", "month"))
        suggested = Path.cwd() / f"budget-{label}-{datetime.now():%Y%m%d-%H%M%S}{FORMATS[format]}"
        destination = _read_field(stdscr, "保存到新文件", context,
                                  "输出文件完整路径：", str(suggested))
        if destination is None:
            return
        require(bool(destination.strip()), "TUI_EXPORT_PATH_INVALID", "请填写输出文件路径")
        path = Path(destination.strip()).expanduser().absolute()
        require(not path.exists() and not path.is_symlink(), "TUI_EXPORT_EXISTS",
                "输出文件已存在，请选择新文件", str(path))
        browser.clear()
        _draw_browser(stdscr, browser)
        browser.refresh()
        if browser.error:
            raise browser.error
        write_export(browser.outcome, browser.config, path, format, period=browser.period)
        state = browser.outcome["status"]
        exported_scope = (f"{browser.outcome['from']} 至 {browser.outcome['through']}" if browser.period else
                          browser.outcome["month"])
        _message(stdscr, "已导出", [f"文件：{path}", f"范围：{exported_scope}", f"状态：{state}",
                                    "完整报表已保存。" if state == "CERTIFIED_SCOPED" else
                                    "当前未通过审计，已保存完整待审清单；文件不含局部预算数字。"])
    except (BudgetError, OSError, UnicodeError, ValueError) as exc:
        _message(stdscr, "导出失败", [str(exc), "本次导出未完成。可核对路径或输入文件后重新按 x 导出。"])


def _next_month(stdscr, browser: ReportBrowser):
    from .tui_next_month import load_next_month, month_workspace, next_month_context, prepare_next_month

    if browser.period or browser.error or browser.help or not browser.outcome:
        return
    try:
        month, _ = next_month_context(browser.config, browser.configs)
        remembered = browser.next_worksheets.get(browser.config)
        context = [f"当前最新配置：{browser.config}", f"下一月：{month}",
                   "1：准备填写表。目标只是提议，账户变化及月末完整性都不自动批准。",
                   "2：载入已填好的 next-month.yaml。沿原 cutover 重放，原月份配置保留。",
                   "请先用编辑器填写该表；未填完时只列出字段及行号，不生成新月配置。"]
        default_action = "2" if remembered else "1"
        action = _read_field(stdscr, "准备／载入下一月", context, f"选择 1 或 2（Enter 采用 {default_action}）：")
        if action is None:
            return
        action = action.strip() or default_action
        require(action in {"1", "2"}, "TUI_NEXT_MONTH_ACTION_INVALID", "请输入 1（准备）或 2（载入）")
        if action == "1":
            suggested = month_workspace(browser.config, browser.review_roots[browser.index]) / (
                month + datetime.now().strftime("-%Y%m%d-%H%M%S-") + uuid4().hex[:8])
            destination = _read_field(stdscr, "保存下一月填写表", context, "新的输出目录：", str(suggested))
            if destination is None:
                return
            require(bool(destination.strip()), "TUI_NEXT_MONTH_PATH_INVALID", "请填写新的输出目录")
            output = Path(destination.strip()).expanduser().resolve()
            result = prepare_next_month(browser.config, browser.configs, output)
            browser.next_worksheets[browser.config] = Path(result["worksheet"])
            lines = [f"填写表：{result['worksheet']}", f"说明：{output / 'START_HERE.md'}",
                     f"待确认账户／币种 {result['changed_accounts']} 户；本月已记交易 {result['booked_transactions_in_month']} 条。",
                     "用编辑器填好并保存后，回到这里按 n，选择 2 载入；当前月报仍保留。",
                     "生成填写表不等于批准目标、月末完整性或签发下一月月报。"]
            if not result["booked_transactions_in_month"]:
                lines.append("下一月没有已记交易：请核对账单是否确已导入；零笔不能证明资料完整。")
            _message(stdscr, "下一月填写表已准备", lines)
        else:
            filename = _read_field(stdscr, "载入已填写的下一月表", context, "next-month.yaml 完整路径：", str(remembered or ""))
            if filename is None:
                return
            require(bool(filename.strip()), "TUI_NEXT_MONTH_PATH_INVALID", "请填写 next-month.yaml 路径")
            worksheet = Path(filename.strip()).expanduser().resolve()
            result = load_next_month(browser.config, browser.configs, worksheet)
            browser.next_worksheets[browser.config] = worksheet
            if not result.get("config"):
                _message(stdscr, "下一月填写仍待确认", [f"填写表：{worksheet}",
                    *[f"{item['detail']} [{item['source']}]" for item in result["review_items"]],
                    "尚未生成下一月配置；填好保存后再次按 n 选择 2。"])
                return
            browser.show_next_month(result)
            lines = [f"下一月配置已保存并载入：{browser.config}",
                     "按 [ 可返回原月份；重启时请使用这里的新配置路径。"]
            if browser.error:
                lines += ["配置已保存，月报计算仍有错误；修正后按 r 重算：", str(browser.error)]
            else:
                lines.append(f"{result['month']} 状态：{browser.outcome['status']}；待审 {len(browser.reviews())} 项。")
                if any(item.get("reason_code") == "EXTERNAL_SCOPE_PENDING" for item in browser.reviews()):
                    lines.append("月末资料尚未确认；核对该截止日资料后可按 f。")
            _message(stdscr, "下一月已载入", [*lines, *browser.project_messages()])
    except (BudgetError, OSError, UnicodeError, ValueError) as exc:
        browser.refresh()
        _message(stdscr, "下一月操作未完成", [str(exc), "核对填写表、配置与输出目录后可再次按 n。"])


def _confirm_month_end(stdscr, browser: ReportBrowser):
    from .tui_month_end import prepare_month_end, save_month_end

    if browser.period or browser.error or browser.help or not browser.outcome:
        return
    if not any(item.get("reason_code") == "EXTERNAL_SCOPE_PENDING"
               for item in browser.outcome.get("review_items", [])):
        _message(stdscr, "本轮无需补充月末核对", [
            "当前清单没有 EXTERNAL_SCOPE_PENDING；如还有待审，请先按清单处理。未发现此项不代表月报已签发。"])
        return
    output = browser.review_roots[browser.index] / (
        datetime.now().strftime("%Y%m%d-%H%M%S-month-end-") + uuid4().hex[:12]) / "reviewed"
    try:
        _put(stdscr, max(0, stdscr.getmaxyx()[0] - 1), "正在读取本月截止日和账本来源……")
        stdscr.refresh()
        prepared = prepare_month_end(browser.config, browser.outcome["month"])
        context = [f"核对月份：{prepared.month}；截止日：{prepared.as_of} 日末。",
                   f"原 cutover：{prepared.cutover}；本月已记交易 {prepared.transactions} 笔。",
                   f"截至该日最近已记交易：{prepared.latest_transaction or '无'}。",
                   "请核对 importer 已覆盖本月，并确认截至截止日：",
                   "没有已发生但漏记的本人责任；没有已发起而尚未结算的付款。",
                   "存在这些事项时请取消，先补账或处理相关事实。",
                   "这不要求预测截止日之后新发生的消费、负债或付款。",
                   "本项不会批准其他交易、目标或责任；只有其余审计也通过才签发月报。"]
        if not prepared.transactions:
            context.append("本月没有已记交易：请特别核对账单是否真的已导入；零笔不能证明资料完整。")
        reason = ""
        while True:
            reason = _read_field(stdscr, "核对本月末资料", context, "核对依据：", reason)
            if reason is None:
                return
            if reason.strip():
                break
            _message(stdscr, "尚未保存", ["请填写实际核对的依据；不能留空。"])
        preview = [*context, f"核对依据：{reason.strip()}", f"新配置：{output / 'month.yaml'}",
                   "保存后当前月份使用新配置；重启、导出或准备下一月时也使用此路径。"]
        approved = _read_field(stdscr, "明确批准本月末声明", preview,
                               "输入 y 并回车保存；其他输入或 Esc 取消：")
        if approved != "y":
            return
        browser.clear()
        _draw_browser(stdscr, browser)
        result = save_month_end(prepared, reason, output, approved=True)
        browser.show_saved_month(result)
        lines = [f"本月末核对声明已保存：{browser.config}", "原账本与原配置未改写。"]
        if browser.error:
            lines += ["新配置已保存，月报仍未通过：", str(browser.error)]
        else:
            lines += [f"本月状态：{browser.outcome['status']}；待审 {len(browser.reviews())} 项。"]
        _message(stdscr, "已保存月末核对", [*lines, *browser.project_messages()])
    except (BudgetError, OSError, UnicodeError) as exc:
        browser.refresh()
        _message(stdscr, "未保存月末核对", [str(exc), "请重算并重新核对；本次没有批准月末声明。"])


def _jump_month(stdscr, browser: ReportBrowser):
    if browser.help:
        return
    try:
        choices = browser.month_choices()
        value = _read_field(stdscr, "跳到已载入月份", ["可选月份：", *[f"{month}：{path}" for month, path in choices],
                           "只切换已有配置并重算；要准备新月请按 n。"], "月份 YYYY-MM（空白／Esc 返回）：")
        if value is None or not value.strip():
            return
        browser.jump_month(value.strip())
        _draw_browser(stdscr, browser)
        browser.refresh()
    except (BudgetError, OSError, UnicodeError) as exc:
        _message(stdscr, "无法跳到该月", [str(exc)])


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
        listing = browser.view in {"reviews", "transactions"} and not (browser.detail or browser.help or browser.error)
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
        elif key == "c":
            browser.toggle_period()
            reload()
        elif key == "m":
            if browser.period:
                if browser.open_review_month():
                    reload()
            else:
                _jump_month(stdscr, browser)
        elif key in ("[", "]"):
            if browser.switch(-1 if key == "[" else 1):
                reload()
        elif key == "/":
            browser.filter(_read_filter(stdscr, browser.query))
        elif key == "?":
            browser.help, browser.offset = not browser.help, 0
        elif key == "a":
            browser.toggle_transactions()
        elif key == "e":
            _edit_review(stdscr, browser)
        elif key == "x":
            _export_report(stdscr, browser)
        elif key == "f":
            _confirm_month_end(stdscr, browser)
        elif key == "n":
            _next_month(stdscr, browser)
        elif key == "t":
            browser.view = "reviews" if browser.view == "report" else "report"
            browser.detail, browser.help, browser.offset, browser.selected = None, False, 0, 0
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
                value = len(browser.items()) - 1 if listing else sys.maxsize
            setattr(browser, attribute, max(0, value))


def run_report_tui(configs: list[Path], loader: Callable, renderer: Callable, *, project=None,
                   initial_config: Path | None = None) -> None:
    """Run foreground audits on demand; never carry numbers across config files."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BudgetError("TUI_TERMINAL_REQUIRED", "TUI 需要交互式终端；脚本中请使用 text 或 json")
    browser = ReportBrowser(configs, loader, renderer, project=project, initial_config=initial_config)
    try:
        curses.wrapper(_report_browser, browser)
    except KeyboardInterrupt:
        return
    except curses.error as exc:
        raise BudgetError("TUI_UNAVAILABLE", "当前终端无法打开 TUI") from exc

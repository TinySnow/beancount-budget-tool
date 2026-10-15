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
    """Fresh workflow results and the current configuration for each supplied month."""

    def __init__(self, configs: list[Path], loader: Callable, renderer: Callable):
        self.configs = list(dict.fromkeys(path.resolve() for path in configs))
        if not self.configs:
            raise BudgetError("TUI_CONFIG_REQUIRED", "至少提供一份月报配置")
        self.loader, self.renderer = loader, renderer
        # Reopening a saved configuration continues in the same record root;
        # repeated sessions must not grow paths indefinitely on Windows.
        self.review_roots = [path.parent.parent.parent if
                             path.name == "month.yaml" and path.parent.name == "reviewed" and
                             path.parent.parent.parent.name == "tui-reviews" else
                             path.parent / "tui-reviews" for path in self.configs]
        self.saved = set()
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
        previous_view = self.view
        self.clear()
        try:
            outcome = self.loader(self.config)
            # Blocked workflows may never expose an earlier or partial report.
            if outcome["status"] != "CERTIFIED_SCOPED":
                outcome = {**outcome, "report": None}
            rendered = self.renderer(outcome)
            self.outcome, self.lines = outcome, rendered.splitlines()
            self.view = "reviews" if outcome.get("review_items") else "report"
            if previous_view == "transactions" and self.has_transactions():
                self.view = "transactions"
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

    def has_transactions(self) -> bool:
        return bool(self.outcome and self.outcome["status"] == "CERTIFIED_SCOPED" and
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
    "a：已签发后切换交易来源列表；可按类别、资金用途、责任 ID、摘要或账户筛选。",
    "↑↓ 或 j/k：滚动文本；在待审列表选择项目。PgUp/PgDn 或空格翻页，g/G 到首尾。",
    "Enter：打开所选待审／交易详情；交易显示逐腿原币、分类、用途、责任及原始账本内容。",
    "e：填写选中的分类／H 资产用途，写理由、核对预览，明确输入 y 并回车才保存。",
    "保存生成新的 tui-reviews/本次记录/reviewed/month.yaml，当前月份自动使用新配置并重审。",
    "重启或准备下一月时使用新配置；原配置不改写。取消填写不保存。其他待审按对应配置处理。",
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
    _put(stdscr, 1, f"配置：{browser.config}")
    page = height - 4
    total_reviews = len(browser.outcome.get("review_items", [])) if browser.outcome else 0
    items = browser.items()
    title = {"reviews": "待审列表", "transactions": "已签发月报的交易来源", "report": "报告文本"}[browser.view]
    subtitle = f"{title}；待审总数 {total_reviews}"
    if browser.index in browser.saved:
        subtitle = "已保存，正使用新配置（? 看完整路径）；" + subtitle
    if browser.query:
        subtitle += f"；筛选：{browser.query}"
    listing = browser.outcome is not None and browser.view in {"reviews", "transactions"} and not (
        browser.error or browser.detail or browser.help)
    if browser.help:
        lines = [*HELP, f"当前配置完整路径：{browser.config}"]
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
            lines = ["待审详情（当前审计快照；分类／H 用途可按 e 填写）",
                 f"原因：{item.get('reason_code', '')} {item.get('detail', '')}",
                 f"位置：{item.get('source') or '未提供'}", "",
                 *json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True).splitlines()]
            subtitle = "待审详情；Esc 返回列表"
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
            lines.append(f"{'>' if index == browser.selected else ' '} {index + 1}. {label} "
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
    footer = "r 重算  / 筛选  e 填写  a 交易  t 报告/待审  Enter 详情  [ ] 切配置  ? 帮助  q 退出"
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

    if browser.view != "reviews" or browser.error or browser.help or not browser.reviews():
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
        browser.configs[browser.index] = Path(result["config"]).resolve()
        browser.saved.add(browser.index)
        browser.filter("")
        browser.refresh()
        lines = ["本项决定已保存；当前月份已切换到新配置并重新审计。",
                 f"以后请使用：{browser.config}", f"审阅记录：{output}"]
        if browser.error:
            lines += ["决定已保存，但月报计算仍有错误：", str(browser.error)]
        else:
            lines += [f"本月状态：{browser.outcome['status']}；待审 {len(browser.outcome.get('review_items', []))} 项。"]
        _message(stdscr, "已保存审阅决定", lines)
    except (BudgetError, OSError, UnicodeError) as exc:
        # Refresh after a stale source or rejected ownership decision. No old
        # report/detail should survive while the user repairs the input.
        browser.refresh()
        lines = [str(exc), "本次未发布新配置；请核对原因后重新选择。"]
        if output.exists():
            lines += [f"填写记录保留在：{output}", "原账本与原配置没有改写。"]
        _message(stdscr, "未保存审阅决定", lines)


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

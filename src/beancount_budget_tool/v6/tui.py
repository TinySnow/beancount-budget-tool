"""Read-only terminal browsing over explicit ledger and YAML decisions."""

from __future__ import annotations

import curses
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from uuid import uuid4
from collections.abc import Callable

from ..errors import BudgetError, require
from ..exact import amount_text


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


def _erase_screen(stdscr):
    stdscr.erase()
    # erase() clears only curses' virtual buffer. Windows/VS Code can retain
    # cells after wide-character text changes, so invalidate the physical
    # screen too; refresh() will repaint one complete frame.
    if hasattr(stdscr, "clearok"):
        stdscr.clearok(True)


def _draw(stdscr, lines: list[str], offset: int) -> int:
    height, width = stdscr.getmaxyx()
    _erase_screen(stdscr)
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
        _put(stdscr, row, content, style)
    for row in range(page):
        index = offset + row
        if index >= len(lines):
            break
        _put(stdscr, row + 1, lines[index])
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


def _review_location(source: str | None) -> str:
    if not source:
        return "位置：未提供"
    path, separator, line = source.rpartition(":")
    # A Windows drive colon is part of the path, not a line number.
    if not separator or not line.isdigit():
        path, line = source, ""
    name = re.split(r"[\\/]", path)[-1]
    return f"位置：第 {line} 行 · {name}" if line else f"位置：{name}"


def _review_reason(item: dict) -> str:
    detail = item.get("detail") or "尚未提供详细说明；请查看原因码和来源资料。"
    if item.get("reason_code") == "H_NEGATIVE_INVENTORY":
        match = re.fullmatch(r"真实库存变为负数：(\S+) (\S+) (-?\d+(?:/\d+)?)(?:。(.*))?", detail)
        if match:
            currency, lot, balance, hint = match.groups()
            scope = "账户" if lot == f"{currency}/no-cost" else f"批次 {lot}"
            return f"这笔交易后，{scope}真实余额为 {amount_text(balance)} {currency}（低于 0）。" + (hint or "")
    return detail


def _review_card(item: dict, index: int, selected: int, columns: int, period: bool) -> list[str]:
    label = "程序实现缺口" if item.get("reason_kind") == "implementation_limit" else "待审"
    date = item.get("date") or (item.get("month", "") if period else "")
    month = f"[{item['month']}] " if period and item.get("month") else ""
    units = item.get("signed_units")
    amount = f"本笔 {amount_text(units)} {item.get('commodity', '')}" if units is not None else ""
    selection = "[选中] " if index == selected else ""
    return _wrap([
        f"{'>' if index == selected else ' '} {index + 1}. {selection}{label} {month}{date}  {amount}".rstrip(),
        f"  {_review_location(item.get('source'))}",
        f"  账户：{item['account']}" if item.get("account") else "  账户：不适用（配置或范围问题）",
        f"  原因：{_review_reason(item)}",
        f"  原因码：{item.get('reason_code', '未提供')}", "",
    ], columns)


def _review_layout(items: list[dict], selected: int, columns: int, period: bool
                   ) -> tuple[list[str], list[tuple[int, int]]]:
    lines, spans = [], []
    for index, item in enumerate(items):
        start = len(lines)
        lines.extend(_review_card(item, index, selected, columns, period))
        spans.append((start, len(lines)))
    return lines, spans


def _review_page_target(browser: ReportBrowser, page: int, columns: int, forward: bool) -> int:
    items = browser.items()
    if not items:
        return 0
    selected = min(browser.selected, len(items) - 1)
    _, spans = _review_layout(items, selected, columns, browser.period)
    target = spans[selected][0] + (page if forward else -page)
    if forward:
        return next((index for index, (start, _) in enumerate(spans) if start >= target), len(items) - 1)
    return next((index for index in reversed(range(len(spans))) if spans[index][0] <= target), 0)


class ReportBrowser:
    """Fresh workflow results and the current configuration for each supplied month."""

    def __init__(self, configs: list[Path], loader: Callable, renderer: Callable, *,
                 initial_config: Path | None = None):
        self.configs = list(dict.fromkeys(path.resolve() for path in configs))
        if not self.configs:
            raise BudgetError("TUI_CONFIG_REQUIRED", "至少提供一份月报配置")
        self.loader, self.renderer = loader, renderer
        self.index = self.configs.index(initial_config.resolve()) if initial_config is not None else 0
        self.period = False
        self.period_range: tuple[str, str] | None = None
        self.salary_cycle: tuple[str, str, str | None, str | None] | None = None
        self.query = ""
        self.view = "report"
        self.help = False
        self.selected = 0
        self.offset = 0
        self.newest_first = True
        self.clear()

    @property
    def config(self) -> Path:
        return self.configs[self.index]

    def clear(self):
        self.outcome = None
        self.error = None
        self.lines = []
        self.detail = None
        self.period_inputs: list[Path] = []
        self.selected = self.offset = 0
        self.help = False

    def refresh(self):
        self.clear()
        try:
            if self.period:
                from .period_report import audit_period, render_text
                self.period_inputs = self.period_configs()
                if self.salary_cycle is not None:
                    from .salary_cycle import audit_salary_cycle, render_text as salary_text
                    outcome = audit_salary_cycle(self.period_inputs[0], *self.salary_cycle)
                    renderer = salary_text
                else:
                    outcome = (audit_period(self.period_inputs, *self.period_range) if self.period_range is not None else
                               audit_period(self.period_inputs))
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

    def month_choices(self) -> list[tuple[str, Path]]:
        from .input_compiler import config_metadata as _metadata
        return [(_metadata(path)[0], path) for path in self.configs]

    def jump_month(self, month: str):
        from .targets import _month
        month = _month(month)
        matches = [index for index, (label, _) in enumerate(self.month_choices()) if label == month]
        require(bool(matches), "TUI_MONTH_MISSING", f"没有载入 {month}；请使用 config.yml 切月，或载入该月导出的快照")
        require(len(matches) == 1, "TUI_MONTH_DUPLICATE", f"{month} 有多份配置；请保留该月最新入口")
        self.index = matches[0]
        self.period = False
        self.filter("")
        self.clear()

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

    def select_period(self, since: str | None = None, through: str | None = None):
        """Remember only query bounds; configuration paths are selected afresh."""
        from .targets import _month
        require((since is None) == (through is None), "PERIOD_RANGE_INVALID", "起始与结束月份须同时填写")
        bounds = None
        if since is not None:
            bounds = (_month(since), _month(through))
            require(bounds[0] <= bounds[1], "PERIOD_RANGE_INVALID", "起始月份不能晚于结束月份")
        self.period_range, self.period, self.view = bounds, True, "report"
        self.salary_cycle = None
        self.filter("")
        self.clear()

    def select_salary_cycle(self, payday: str, next_payday: str,
                            budget: str | None = None, category: str | None = None):
        from .salary_cycle import cycle_bounds
        cycle_bounds(payday, next_payday, budget)
        self.salary_cycle = (payday, next_payday, budget, category)
        self.period, self.view = True, "report"
        self.filter("")
        self.clear()

    def period_configs(self) -> list[Path]:
        if self.salary_cycle is not None:
            from .salary_cycle import select_config
            return [select_config([path for _, path in self.month_choices()], *self.salary_cycle[:2])]
        if self.period_range is None:
            return list(self.configs)
        from .period_report import _months
        from ..yamlstrict import require_yaml
        since, through = self.period_range
        rows = [(month, path) for month, path in self.month_choices() if since <= month <= through]
        missing = sorted(set(_months(since, through)) - {month for month, _ in rows})
        require_yaml(not missing, "PERIOD_MONTHS_INCOMPLETE",
                     f"所选区间缺少月份：{', '.join(missing)}。请先载入各月配置；缺月不能当作零交易。",
                     self.config, ("month",))
        return [path for _, path in rows]

    def period_label(self) -> str:
        if self.salary_cycle is not None:
            return f"工资周期 {self.salary_cycle[0]} 至下次工资日 {self.salary_cycle[1]}（不含）"
        return (f"区间 {self.period_range[0]} 至 {self.period_range[1]}" if self.period_range is not None else
                f"区间使用全部 {len(self.configs)} 份配置")

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
        return [item for item in items if not query or query in (json.dumps(
            item, ensure_ascii=False, sort_keys=True) + "\n" + "\n".join(
                _review_card(item, 0, -1, 100000, self.period))).casefold()]

    def has_transactions(self) -> bool:
        return bool(not self.period and self.outcome and self.outcome["status"] == "CERTIFIED_SCOPED" and
                    self.outcome.get("report") and self.outcome["report"].get("transactions") is not None)

    def transactions(self) -> list[dict]:
        items = self.outcome["report"]["transactions"]["rows"] if self.has_transactions() else []
        # Replay and exported reports keep their canonical chronological order.
        # Only this browser's presentation is reversed, including same-day ties.
        items = list(reversed(items)) if self.newest_first else list(items)
        query = self.query.casefold()
        if not query:
            return items
        from .transaction_details import render_transaction
        names = self.outcome["report"]["owner_names"] if self.has_transactions() else {}
        # Search transaction text, file/line and decimal display amounts;
        # machine-specific parent paths and opaque IDs are not keywords.
        return [item for item in items if query in "\n".join(render_transaction(item, names, for_search=True)).casefold()]

    def items(self) -> list[dict]:
        return self.transactions() if self.view == "transactions" else self.reviews()

    def toggle_transactions(self):
        if not self.has_transactions():
            return
        self.view = "report" if self.view == "transactions" else "transactions"
        self.selected = self.offset = 0
        self.detail, self.help = None, False

    def toggle_order(self):
        if self.view != "transactions" or self.detail or self.help or self.error:
            return
        self.newest_first = not self.newest_first
        self.selected = self.offset = 0

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
        scope = ("工资周期 " if self.salary_cycle is not None else "区间 ") if self.period else ""
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
    "n：切换下一月；直接使用两份 YAML 中该月的配置，缺少目标或完整性声明仍待审。",
    "p：输入起始与结束月份，查看所选连续区间；起始填 all 恢复全部已载入月份。Esc 取消，不改变当前查询。",
    "w：输入本次／下次真实工资日及可选周期额度；含本次工资日，不含下次工资日。只统计周期消费及额度差，不套用自然月目标或签发实际可花。",
    "查询区间不改变 cutover；各月余额仍按原历史重放，所需的历史归属及责任资料须齐全。",
    "c：切换当前月报与上次选择的区间；未选择时使用全部已载入最新配置。范围仅在本次 TUI 会话保留。",
    "区间重新审计每个月：只累计流量，目标差逐月比较，余额与可花取期末，任何月待审均不显示区间数字。",
    "m：在区间待审列表／详情中转到所选月份，按 e 查看修改指引，编辑账本或两份 YAML 后重算。",
    "x：导出当前月份／区间的完整 text、json 或 csv；填写格式与新文件路径，Esc 取消。",
    "导出前重新审计，使用最新已保存配置，忽略显示筛选；待审时只导出待审清单。CSV 使用 UTF-8 BOM。",
    "/：输入关键词筛选；支持中文、账户、用途、原因码和来源路径。空查询清除筛选。",
    "t：切换完整报告文本和待审列表。待审状态只显示未通过项目，不显示局部正式数字。",
    "a：已签发后切换交易来源列表；可按类别、资金用途、责任 ID、摘要或账户筛选。",
    "o / O：交易来源列表和 h 流水页切换正序／倒序；默认倒序，最新在上。同日顺序也随之反转，逐笔余额仍是该笔交易后的余额。",
    "h：追查完整会计科目内某预算用途的历史，显示每笔增减、交易后余额、原因和原行；可留空预算看全部。",
    "用途透支报错时也可按 h：仅查失败交易前的 H 账户诊断流水；不要求签出整月，不放行失败交易。",
    "d：输入 YYYY-MM-DD，查看该日日末的用途分布；两种查询都要求该日所在月份当前能签出月报。",
    "查询不使用截止日之后的交易／还款，不签发当天最多能花；弹窗是本次静态查询，Esc 返回后再次打开会重算。",
    "↑↓ 或 j/k：滚动文本；在待审列表选择项目。空格／PgDn 下翻整页，b／PgUp 上翻整页，g/G 或 Home/End 到首尾。",
    "Enter：打开所选待审／交易详情；交易显示逐腿原币、分类、用途、责任及原始账本内容。",
    "待审项分行显示：日期与本笔金额、文件名与行号、账户、原因。Enter 查看完整路径；详情可用 ↑↓／PgUp／PgDn 滚动。",
    "e：查看所选待审项的来源和修改指引；请在账本或两份 YAML 中明确配置后重算。",
    "f：显示本月 budgets.yml 月末完整性配置指引；程序不代写、不批准、不保存隐藏状态。",
    "月末核对只声明截止日及以前的事实，不保证未来没有负债；其他待审仍须处理。存在漏记责任／在途付款时请取消。",
    "所有决定来自账本和两份 YAML；缺少明确依据时保持待审，不创建独立批准记录。",
    "Esc：关闭详情／帮助，或清除筛选；无这些状态时退出。q 退出。? 打开帮助。",
    "筛选只改变显示，不改变计算、认证状态或待审总数。警告不会随报告筛选隐藏。",
]


def _put(stdscr, row: int, text: str, style=0):
    try:
        if hasattr(stdscr, "move") and hasattr(stdscr, "clrtoeol"):
            stdscr.move(row, 0)
            stdscr.clrtoeol()
        visible = _clip(_clean(text), max(0, stdscr.getmaxyx()[1] - 1))
        stdscr.addstr(row, 0, visible, 0 if sys.platform == "win32" else style)
        if style and hasattr(stdscr, "chgat"):
            stdscr.chgat(row, 0, max(1, sum(_width(char) for char in visible)), style)
    except curses.error:
        pass


_COLORS = {}


def _init_colors():
    _COLORS.clear()
    try:
        if "NO_COLOR" in os.environ or not curses.has_colors():
            return
        curses.start_color()
        background = curses.COLOR_BLACK
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            pass
        for index, name in enumerate(("cyan", "green", "yellow", "magenta", "red"), 1):
            curses.init_pair(index, getattr(curses, "COLOR_" + name.upper()), background)
            _COLORS[name] = curses.color_pair(index)
    except curses.error:
        _COLORS.clear()


def _line_style(line, current=0):
    if any(word in line for word in ("警告", "计算失败", "H_OWNER_NEGATIVE", "程序实现缺口")):
        return _COLORS.get("red", 0) | curses.A_BOLD
    if line and not line[0].isspace():
        for heading, color in (("用途划拨", "green"), ("其他 H", "cyan"),
                               ("消费目标", "yellow"), ("默认支出", "yellow"),
                               ("预算资金", "green"), ("专题", "magenta"),
                               ("K 账面", "magenta"), ("证券资金池", "magenta")):
            if line.startswith(heading):
                return _COLORS.get(color, 0) | curses.A_BOLD
    return current


def _draw_browser(stdscr, browser: ReportBrowser):
    height, width = stdscr.getmaxyx()
    _erase_screen(stdscr)
    _put(stdscr, 0, browser.title(), curses.A_REVERSE | curses.A_BOLD)
    if height < 5 or width < 8:
        if height > 1:
            _put(stdscr, 1, "窗口过小；放大终端，q 退出")
        stdscr.refresh()
        return
    display_config = getattr(browser, "display_config", browser.config)
    config_label = "/".join(display_config.parts[-3:])
    _put(stdscr, 1, f"{browser.period_label()}；p 选择范围，c 返回月报" if browser.period else
         f"配置：{config_label}（? 查看完整路径）")
    page = height - 4
    total_reviews = len(browser.outcome.get("review_items", [])) if browser.outcome else 0
    items = browser.items()
    title = {"reviews": "待审列表", "transactions": "已签发月报的交易来源", "report": "报告文本"}[browser.view]
    subtitle = f"{title}；待审总数 {total_reviews}"
    if browser.query:
        subtitle += f"；筛选：{browser.query}"
    listing = browser.outcome is not None and browser.view in {"reviews", "transactions"} and not (
        browser.error or browser.detail or browser.help)
    if browser.help:
        lines = [f"当前配置完整路径：{browser.config}", *HELP]
        if hasattr(browser, "user_config"):
            lines = ["两文件模式：只维护 config.yml、budgets.yml 和账本；所有决定来自账本与这两份 YAML，不保存独立审阅记录。",
                     "m 跳到任意 cutover 后月份；n 直接进入下一月；r 重读两份配置与账本，不用手工编译。",
                     "[ / ] 上一月／下一月；月份切换保留原 cutover，不重置资金余额。",
                     "w 输入两次工资日及可选额度；p 选择区间；c 返回月报或上次查询。",
                     "↑↓ 选项目；Enter 看完整来源和原因；Esc 返回；/ 筛选；q 退出。",
                     "e 查看修改指引；分类与用途请在账本写 budget／fund，或修改两份 YAML 后按 r。",
                     "f 查看月末填写指引；在 budgets.yml 对应月份明确配置 completeness_confirmed，不保存内部批准。",
                     "x 导出；t 报告／待审；a 已审交易明细；? 关闭帮助。",
                     "流水默认倒序（最新在上）；o/O 正倒序切换。空格/PgDn 下页，b/PgUp 上页，g/G 或 Home/End 到首尾。",
                     "未写目标表示不考核；写 null 表示尚未决定，仍待审；0 是真实零目标。",
                     f"用户配置：{browser.user_config}"]
            if browser.runtime is not None:
                lines.append(f"预算目标：{browser.runtime.budgets}")
        if browser.period:
            lines += [browser.period_label(), "区间使用的最新配置：", *map(str, browser.period_inputs)]
        subtitle = "帮助；Esc 返回"
    elif browser.error:
        lines = ["本次计算失败；旧月报已清除。修正文件后按 r 重算。",
                 _review_location(browser.error.source),
                 *f"{browser.error.code}：{browser.error.message}".splitlines(),
                 f"来源：{browser.error.source or '未提供'}"]
        if browser.error.diagnostic:
            lines.append('按 h 查该账户在失败交易之前的用途流水；可筛选自由资金等用途。')
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
                 _review_location(item.get('source')),
                 f"日期：{item.get('date') or item.get('month') or '不适用'}",
                 f"本笔金额：{amount_text(item['signed_units'])} {item.get('commodity', '')}"
                 if item.get('signed_units') is not None else "本笔金额：不适用",
                 f"原因：{_review_reason(item)}",
                 f"账户：{item.get('account') or '不适用'}",
                 f"完整路径：{item.get('source') or '未提供'}",
                 f"原因码：{item.get('reason_code', '')}", "", "原始审计资料（精确数值）：",
                 *json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True).splitlines()]
            subtitle = "待审详情；Esc 返回列表" + ("，m 转到该月" if browser.period else "")
    elif listing:
        subtitle += f"；匹配 {len(items)} 项"
        if browser.view == "transactions":
            count = browser.outcome["report"]["transactions"]["count"] if browser.has_transactions() else 0
            subtitle = f"{_order_label(browser.newest_first)}；" + subtitle + f"；交易总数 {count}"
        if (browser.outcome or {}).get("review_queue", {}).get("preflight_only"):
            subtitle += "；仅当前预检阻断层，其余尚未检查"
        browser.selected = max(0, min(browser.selected, max(0, len(items) - 1)))
        lines = []
        if browser.view == "transactions":
            browser.offset = min(browser.offset, browser.selected)
            browser.offset = max(browser.offset, browser.selected - page + 1)
            browser.offset = max(0, min(browser.offset, max(0, len(items) - page)))
            for index, item in enumerate(items):
                amounts = " / ".join(f"{amount_text(part['signed_units'])} {part['commodity']}" for part in item["postings"][:2])
                selection = "[选中] " if index == browser.selected else ""
                lines.append(f"{'>' if index == browser.selected else ' '} {index + 1}. {selection}{item['date']} "
                             f"{item.get('payee') or ''} {item['narration']} | {amounts} [{item['source']}]")
        else:
            lines, spans = _review_layout(items, browser.selected, width - 1, browser.period)
            if items:
                start, end = spans[browser.selected]
                if start < browser.offset or end - start > page:
                    browser.offset = start
                elif end > browser.offset + page:
                    browser.offset = end - page
                browser.offset = max(0, min(browser.offset, max(0, len(lines) - page)))
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
    styles, current_style = [], 0
    for line in lines:
        style = _line_style(line, current_style)
        if line and not line[0].isspace() and "警告" not in line:
            current_style = style
        styles.append((_COLORS.get("red", 0) if browser.error else style))
    for row, line in enumerate(lines[browser.offset:browser.offset + page], 3):
        _put(stdscr, row, line, styles[browser.offset + row - 3])
    footer = ("w 工资周期 p 范围 x 导出 c 月报 m 转该月 r 重算 / 筛选 Enter 详情 ? 帮助 q 退出" if browser.period else
              "h 科目流水 d 日末分布 w 工资周期 p 区间 m 跳月 n 下一月 f 月末配置 x 导出 r 重算 ? 帮助 q 退出")
    if listing and browser.view == "reviews":
        footer = "↑↓ 选项 Enter 详情 e 修改指引 [ ] 上/下月 m 跳月 r 重算 / 筛选 ? 帮助 q 退出"
    elif listing and browser.view == "transactions":
        footer = "o 正/倒序 空格/PgDn 下页 b/PgUp 上页 g/G 首尾 / 筛选 Enter 详情 t 报告"
    elif browser.detail:
        footer = "空格/PgDn 下页 b/PgUp 上页 g/G 首尾 ↑↓ 滚动 Esc 返回"
    if browser.error:
        footer = ("h 报错前流水 " if browser.error.diagnostic else "") + "[ ] 上/下月 m 跳月 r 重算 ↑↓ 滚动 ? 帮助 q 退出"
    if getattr(browser, "watch", False):
        footer = "自动监测保存 · " + footer
    _put(stdscr, height - 1, footer, curses.A_REVERSE)
    stdscr.refresh()


def _read_key(stdscr):
    try:
        value = stdscr.get_wch() if hasattr(stdscr, "get_wch") else stdscr.getch()
    except curses.error:
        return -1  # get_wch raises on a timed poll, including inside dialogs.
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
            _erase_screen(stdscr)
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


def _order_label(newest_first: bool) -> str:
    return "倒序（最新在上）" if newest_first else "正序（最早在上）"


def _message(stdscr, title: str, lines: list[str], *, reorder: Callable | None = None):
    """A read-only notice; never present an input box that discards text."""
    offset = 0
    while True:
        height, width = stdscr.getmaxyx()
        _erase_screen(stdscr)
        _put(stdscr, 0, title, curses.A_REVERSE | curses.A_BOLD)
        page = max(1, height - 2)
        wrapped = _wrap(lines, width - 1)
        offset = max(0, min(offset, max(0, len(wrapped) - page)))
        if height >= 3:
            for row, line in enumerate(wrapped[offset:offset + page], 1):
                _put(stdscr, row, line)
        footer = ("o 正/倒序 " if reorder else "") + "空格/PgDn 下页 b/PgUp 上页 g/G 首尾 Enter 或 Esc 返回"
        _put(stdscr, max(0, height - 1), footer)
        stdscr.refresh()
        key = _read_key(stdscr)
        if key in ("\n", "\r", curses.KEY_ENTER, "\x1b"):
            return
        if reorder and key in ("o", "O"):
            title, lines = reorder()
            offset = 0
        elif key in (curses.KEY_PPAGE, "b"):
            offset -= page
        elif key in (curses.KEY_NPAGE, " "):
            offset += page
        elif key in (curses.KEY_UP, "k"):
            offset -= 1
        elif key in (curses.KEY_DOWN, "j"):
            offset += 1
        elif key in (curses.KEY_HOME, "g"):
            offset = 0
        elif key in (curses.KEY_END, "G"):
            offset = len(wrapped)


def _history_message(stdscr, browser, title: str, result: dict, renderer: Callable):
    """Reorder complete events, preserving each event's source and audited balance."""
    def display():
        key = "history" if "history" in result else "report"
        report = result[key]
        newest_first = getattr(browser, "newest_first", True)
        view = {**result, key: {**report, "events": list(reversed(report["events"]))
                              if newest_first else list(report["events"])}}
        return f"{title} · {_order_label(newest_first)}", renderer(view).splitlines()

    def reorder():
        browser.newest_first = not getattr(browser, "newest_first", True)
        browser.selected = browser.offset = 0
        return display()

    label, lines = display()
    _message(stdscr, label, lines, reorder=reorder)


def _edit_review(stdscr, browser: ReportBrowser):
    """Explain which explicit input to edit; never approve or persist a choice."""
    if browser.error or browser.help or not browser.reviews():
        return
    item = browser.detail or browser.reviews()[max(0, min(browser.selected, len(browser.reviews()) - 1))]
    config = getattr(browser, 'user_config', browser.config)
    lines = [f"原因：{item.get('reason_code', '待审')}",
             f"来源：{item.get('source') or config}", item.get('detail') or '尚无明确依据。',
             '本程序不保存独立审阅决定，也不会替你批准或猜测。',
             '分类请修改该分录的 budget；资产用途请修改 fund；专题使用 tracking。',
             '账户性质、默认规则、负债和复杂明确协议请修改 config.yml；目标和月末声明请修改 budgets.yml。',
             '有多种合理解释时仍保留待审；修改后按 r 重算，或等待 watch 检测变更。']
    _message(stdscr, '修改账本／两份 YAML 后重算', lines)


def _export_report(stdscr, browser: ReportBrowser):
    from .tui_export import FORMATS, write_export

    scope = browser.period_label() if browser.period else "月报"
    context = [f"导出：{scope}（完整结果，忽略显示筛选）",
               "重新读取账本与最新配置后导出；待审时只保存待审清单。",
               "格式：text 文本、json 完整结构、csv 表格。已有文件不会覆盖。",
               *[str(path) for path in (browser.period_inputs if browser.period else [browser.config])]]
    try:
        format = _read_field(stdscr, "导出报表", context, "格式（text / json / csv）：", "text")
        if format is None:
            return
        format = format.strip().casefold()
        require(format in FORMATS, "TUI_EXPORT_FORMAT_INVALID", "导出格式须为 text、json 或 csv")
        label = (("salary-" + "-to-".join(browser.salary_cycle[:2]) if browser.salary_cycle is not None else
                  "period-" + "-to-".join(browser.period_range) if browser.period_range else "period")
                 if browser.period else (browser.outcome or {}).get("month", "month"))
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
        write_export(browser.outcome, getattr(browser, "display_config", browser.config), path, format, period=browser.period)
        state = browser.outcome["status"]
        exported_scope = (f"{browser.outcome['from']} 至 {browser.outcome['through']}" if browser.period else
                          browser.outcome["month"])
        _message(stdscr, "已导出", [f"文件：{path}", f"范围：{exported_scope}", f"状态：{state}",
                                    "完整报表已保存。" if state == "CERTIFIED_SCOPED" else
                                    "当前未通过审计，已保存完整待审清单；文件不含局部预算数字。"])
    except (BudgetError, OSError, UnicodeError, ValueError) as exc:
        _message(stdscr, "导出失败", [str(exc), "本次导出未完成。可核对路径或输入文件后重新按 x 导出。"])


def _next_month(stdscr, browser: ReportBrowser):
    if browser.period or browser.help:
        return
    if hasattr(browser, 'advance_month'):
        browser.advance_month()
        _draw_browser(stdscr, browser)
        browser.refresh()
    else:
        _message(stdscr, '使用两文件配置切月', ['请使用 config.yml；按 m／[／] 直接切月，不再创建下一月填写表。'])


def _confirm_month_end(stdscr, browser: ReportBrowser):
    """Locate the public monthly declaration without writing hidden state."""
    if browser.period or browser.error or browser.help or not browser.outcome:
        return
    month = browser.outcome.get('month', getattr(browser, 'selected_month', 'YYYY-MM'))
    runtime = getattr(browser, 'runtime', None)
    target = str(runtime.budgets) if runtime is not None and runtime.budgets else 'budgets.yml'
    _message(stdscr, '月末声明请写入 budgets.yml', [
        f'月份：{month}；配置：{target}',
        '先核对截至该月末没有已发生但漏记的本人责任或在途付款。',
        '确认后在 months 的该月份填写 completeness_confirmed: true；未确认填 false 或留空。',
        '这只声明该月截止日及以前的完整性，不批准其他歧义或预测未来事实。',
        '程序不会代写此声明或创建审阅文件；请用编辑器保存 YAML 后按 r 重算。'])


def _jump_month(stdscr, browser: ReportBrowser):
    if browser.help:
        return
    try:
        choices = browser.month_choices()
        context = ["已浏览月份：" if hasattr(browser, "user_config") else "可选月份：",
                   *[f"{month}：{path}" for month, path in choices],
                   "可输入任意 cutover 之后的月份，不要求先签发；当前失败也能返回前月。"
                   if hasattr(browser, "user_config") else "只切换已有配置并重算；要准备新月请按 n。"]
        value = _read_field(stdscr, "跳到月份", context, "月份 YYYY-MM（空白／Esc 返回）：")
        if value is None or not value.strip():
            return
        browser.jump_month(value.strip())
        _draw_browser(stdscr, browser)
        browser.refresh()
    except (BudgetError, OSError, UnicodeError) as exc:
        _message(stdscr, "无法跳到该月", [str(exc)])


def _select_period(stdscr, browser: ReportBrowser):
    if browser.help:
        return
    try:
        months = [month for month, _ in browser.month_choices()]
        first, last = browser.period_range or (min(months), max(months))
        context = ["只查询起止月份内的连续月报；区间内不能缺月。",
                   "只累计流量，余额与可花取期末；所选月份待审时只显示待审清单。",
                   "区间不改变 cutover，余额仍按原历史重放；历史归属及责任资料仍须齐全。",
                   "起始月份输入 all 恢复全部已载入月份；Esc 取消。",
                   "当前载入月份：" + "、".join(sorted(months))]
        since = _read_field(stdscr, "选择报表区间", context, "起始月份 YYYY-MM（或 all）：", first)
        if since is None:
            return
        if since.strip().casefold() == "all":
            browser.select_period()
        else:
            through = _read_field(stdscr, "选择报表区间", context, "结束月份 YYYY-MM：", last)
            if through is None:
                return
            browser.select_period(since.strip(), through.strip())
        _draw_browser(stdscr, browser)
        browser.refresh()
    except (BudgetError, OSError, UnicodeError) as exc:
        _message(stdscr, "未切换查询范围", [str(exc), "请核对起止月份后再次按 p；当前结果不代表新范围。"])


def _inspect_account(stdscr, browser: ReportBrowser, *, history=False):
    if browser.help:
        return
    if getattr(browser, 'error', None):
        if history:
            _inspect_failed_history(stdscr, browser)
        return
    from .account_queries import query, render_text
    from .input_compiler import config_metadata as _metadata
    from .salary_plan import month_end
    try:
        config = getattr(browser, 'user_config', browser.config)
        month = getattr(browser, 'selected_month', None) or _metadata(browser.config)[0]
        context = ['查询该日日末；所在月份必须能签发月报。',
                   '只显示原币用途；K 是申赎账面流量，证券池是名义分配，不代表当天最多能花。']
        day = _read_field(stdscr, '科目用途历史' if history else '日末用途分布', context,
                          '截止日 YYYY-MM-DD：', month_end(month).isoformat())
        if day is None:
            return
        snapshot = query(config, day.strip())
        if snapshot['report'] is None or not history:
            _message(stdscr, '日末用途查询', render_text(snapshot).splitlines())
            return
        accounts = snapshot['report']['accounts']
        value = _read_field(stdscr, '选择会计科目',
            ['只查这个完整会计科目的直接记账；不自动合并子科目。',
             *[f'{i+1}. {name}' for i,name in enumerate(accounts)]], '科目编号或完整名称：')
        if value is None:
            return
        value = value.strip()
        if value.isdigit():
            require(1 <= int(value) <= len(accounts), 'QUERY_ACCOUNT_UNKNOWN', '科目编号超出范围')
            value = accounts[int(value)-1]
        purposes = [row['purpose'] for row in snapshot['report']['positions'] if row['account'] == value]
        budget = _read_field(stdscr, '选择预算用途',
            [f'会计科目：{value}', '当前非零份额：'+('、'.join(dict.fromkeys(purposes)) or '无'),
             '资产查 fund，负债查责任用途，收支查 budget；空白显示全部。'], '预算名称（可留空）：')
        if budget is None:
            return
        result = query(config, day.strip(), account=value, purpose=budget.strip() or None)
        if result['report'] is not None:
            _history_message(stdscr, browser, '科目用途历史', result, render_text)
        else:
            _message(stdscr, '科目用途历史', render_text(result).splitlines())
    except (BudgetError, OSError, UnicodeError) as exc:
        _message(stdscr, '未生成用途查询', [str(exc)])


def _inspect_failed_history(stdscr, browser):
    from .diagnostic_history import query, render_text
    from .input_compiler import config_metadata
    error = browser.error
    if not error.diagnostic:
        _message(stdscr, '无法生成诊断流水', ['该错误没有可重放的用途交易位置；请先处理所示错误。'])
        return
    try:
        config = getattr(browser, 'user_config', browser.config)
        month = getattr(browser, 'selected_month', None) or config_metadata(config)[0]
        purpose = _read_field(stdscr, '报错前用途流水',
            [f"账户：{error.diagnostic['account']}", f'失败位置：{error.source}',
             '严格停在失败交易之前，不含该笔及后续补款；不签发月报。',
             '输入自由资金、生活费等用途；留空查看该账户全部用途。'], '用途（可留空）：')
        if purpose is None:
            return
        result = query(config, month, error.source, purpose=purpose.strip() or None, anchor=error.diagnostic)
        _history_message(stdscr, browser, '报错前用途流水（诊断，未签发）', result, render_text)
    except (BudgetError, OSError, UnicodeError) as exc:
        _message(stdscr, '未生成诊断流水', [str(exc)])


def _select_salary_cycle(stdscr, browser: ReportBrowser):
    if browser.help:
        return
    try:
        choices, current = browser.configured_cycle_choices() if hasattr(browser, "configured_cycle_choices") else ([], None)
        if choices:
            if current is None:
                _message(stdscr, "工资周期目标尚未生效", [f"首个已配置发薪日：{choices[0][0]}。",
                         "当前月份末尚无适用周期；可继续核对账务。到首个发薪月份后再按 w 查看。"])
                return
            value = _read_field(stdscr, "查看已保存发薪周期",
                                ["日期和额度来自 budgets.yml，无需每次重填。",
                                 *[f"{start}：{label}" for start, label in choices],
                                 "含本次发薪日，不含下一发薪日；末周期统计至 config.yml 默认月份末。"],
                                "发薪日 YYYY-MM-DD：", current)
            if value is not None:
                browser.select_saved_cycle(value.strip())
                browser.refresh()
            return
        from .input_compiler import config_metadata as _metadata
        month, _ = _metadata(browser.config)
        year, number = map(int, month.split("-"))
        following = f"{year + (number == 12):04d}-{number % 12 + 1:02d}"
        previous = browser.salary_cycle or (month + "-01", following + "-01", None, None)
        context = ["本周期包含本次工资到账日，排除下次工资到账日；当天所有交易均归下一周期。",
                   "请输入真实日期，不按账户名称或固定几号猜工资。",
                   "周期额度单独填写，不复制、累加或按天折算自然月目标；留空只统计消费。",
                   "只签发消费及额度差，不表示实际可花现金；Esc 取消。"]
        values = []
        for prompt, initial in zip(("本次工资日 YYYY-MM-DD：", "下次工资日 YYYY-MM-DD：",
                                    "本周期消费额度（可留空）：", "消费类别（留空沿用消费上限类别）："), previous):
            value = _read_field(stdscr, "工资周期消费统计", context, prompt, initial or "")
            if value is None:
                return
            values.append(value.strip())
        browser.select_salary_cycle(values[0], values[1], values[2] or None, values[3] or None)
        _draw_browser(stdscr, browser)
        browser.refresh()
    except (BudgetError, OSError, UnicodeError) as exc:
        _message(stdscr, "未切换工资周期", [str(exc), "请核对真实工资日、额度及周期末月份配置。"])


def _report_browser(stdscr, browser: ReportBrowser):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    _init_colors()
    watcher = None
    if getattr(browser, "watch", False):
        from .input_watch import InputWatch
        watcher = InputWatch([browser.user_config] if hasattr(browser, "user_config") else browser.configs)
        stdscr.timeout(300)

    def reload():
        if watcher:
            watcher.configs = ([browser.user_config] if hasattr(browser, "user_config") else list(browser.configs))
            watcher.accept()
        browser.clear()
        _draw_browser(stdscr, browser)
        browser.refresh()

    reload()
    redraw = True
    while True:
        if redraw:
            _draw_browser(stdscr, browser)
            redraw = False
        key = _read_key(stdscr)
        if key == -1:
            if watcher and watcher.changed():
                reload()
                redraw = True
            continue
        redraw = True
        if watcher and watcher.changed():
            reload()
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
        elif key == "p":
            _select_period(stdscr, browser)
        elif key == "w":
            _select_salary_cycle(stdscr, browser)
        elif key == "h":
            _inspect_account(stdscr, browser, history=True)
        elif key == "d":
            _inspect_account(stdscr, browser)
        elif key == "m":
            if browser.period:
                if browser.open_review_month():
                    reload()
            else:
                _jump_month(stdscr, browser)
        elif key in ("[", "]", curses.KEY_LEFT, curses.KEY_RIGHT):
            if browser.switch(-1 if key in ("[", curses.KEY_LEFT) else 1):
                reload()
        elif key == "/":
            browser.filter(_read_filter(stdscr, browser.query))
        elif key == "?":
            browser.help, browser.offset = not browser.help, 0
        elif key == "a":
            browser.toggle_transactions()
        elif key in ("o", "O"):
            browser.toggle_order()
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
                     curses.KEY_NPAGE, " ", "b", curses.KEY_HOME, "g", curses.KEY_END, "G"):
            delta = -1 if key in (curses.KEY_UP, "k") else 1
            if key in (curses.KEY_PPAGE, "b"):
                delta = -page
            elif key in (curses.KEY_NPAGE, " "):
                delta = page
            attribute = "selected" if listing else "offset"
            value = getattr(browser, attribute) + delta
            if listing and browser.view == "reviews" and key in (curses.KEY_PPAGE, "b", curses.KEY_NPAGE, " "):
                value = _review_page_target(browser, page, stdscr.getmaxyx()[1] - 1,
                                            key not in (curses.KEY_PPAGE, "b"))
            if key in (curses.KEY_HOME, "g"):
                value = 0
            elif key in (curses.KEY_END, "G"):
                value = len(browser.items()) - 1 if listing else sys.maxsize
            setattr(browser, attribute, max(0, value))


def run_report_tui(configs: list[Path], loader: Callable, renderer: Callable, *,
                   initial_config: Path | None = None, watch: bool = False) -> None:
    """Run foreground audits on demand; never carry numbers across config files."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BudgetError("TUI_TERMINAL_REQUIRED", "TUI 需要交互式终端；脚本中请使用 text 或 json")
    browser = ReportBrowser(configs, loader, renderer, initial_config=initial_config)
    browser.watch = watch
    try:
        curses.wrapper(_report_browser, browser)
    except KeyboardInterrupt:
        return
    except curses.error as exc:
        raise BudgetError("TUI_UNAVAILABLE", "当前终端无法打开 TUI") from exc

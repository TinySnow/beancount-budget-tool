"""Terminal monthly browsing, refresh and explicitly approved source decisions."""

from __future__ import annotations

import curses
from datetime import datetime
import json
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
        match = re.fullmatch(r"真实库存变为负数：(\S+) (\S+) (-?\d+(?:/\d+)?)", detail)
        if match:
            currency, lot, balance = match.groups()
            scope = "账户" if lot == f"{currency}/no-cost" else f"批次 {lot}"
            return f"这笔交易后，{scope}真实余额为 {amount_text(balance)} {currency}（低于 0）。"
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


def _review_root(path: Path) -> Path:
    return (path.parent.parent.parent if
            path.name == "month.yaml" and path.parent.name == "reviewed" and
            path.parent.parent.parent.name == "tui-reviews" else path.parent / "tui-reviews")


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
        self.review_roots = [_review_root(path) for path in self.configs]
        self.saved = set()
        self.next_worksheets: dict[Path, Path] = {}
        self.index = self.configs.index(initial_config.resolve()) if initial_config is not None else 0
        self.period = False
        self.period_range: tuple[str, str] | None = None
        self.salary_cycle: tuple[str, str, str | None, str | None] | None = None
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
        self.period_inputs: list[Path] = []
        self.selected = self.offset = 0
        self.help = False

    def refresh(self):
        self.clear()
        try:
            self.sync_project()
            project_before = self.project.original if self.project is not None and self.project_error is None else None
            if self.period:
                from .period_report import audit_period, render_text
                self.period_inputs = self.period_configs()
                if self.salary_cycle is not None:
                    from .salary_cycle import audit_salary_cycle, render_text as salary_text
                    outcome = audit_salary_cycle(self.period_inputs[0], *self.salary_cycle,
                        expected_identity=self.project.identity if self.project is not None else None)
                    renderer = salary_text
                else:
                    outcome = (audit_period(self.period_inputs, *self.period_range) if self.period_range is not None else
                               audit_period(self.period_inputs))
                    renderer = render_text
            else:
                outcome = self.loader(self.config)
                renderer = self.renderer
            if project_before is not None:
                require(self.project.path.read_bytes() == project_before, "INPUT_CHANGED",
                        "计算期间项目入口发生变化；请重新重算", str(self.project.path))
            self._accept_outcome(outcome, renderer)
        except BudgetError as exc:
            self.error = exc
        except (OSError, UnicodeError) as exc:
            self.error = BudgetError("TUI_INPUT_ERROR", str(exc), str(getattr(exc, "filename", None) or self.config))

    def sync_project(self):
        """Follow external CLI saves, preserving the selected month and local work."""
        if self.project is None:
            return
        from .project import _metadata, load_project
        latest = load_project(self.project.path)
        require(latest.identity == self.project.identity, "PROJECT_CONTEXT_CHANGED",
                "项目的账本／cutover／报告货币已更换；请退出后重新打开项目", str(latest.path))
        registered = set(self.project.months.values())
        local = [self.configs[index] for index in self.saved if self.configs[index] not in registered]
        for path in local:
            month, _ = _metadata(path)
            if latest.months.get(month) != path:
                # A failed project save must not discard an already published
                # financial config, even when a CLI session changes the manifest.
                self.project_error = self.project_error or BudgetError(
                    "PROJECT_LOCAL_SAVE_PENDING", "本次保存的配置尚未登记；请先登记新路径或显式打开它", str(path))
                return
        month = next((month for month, path in self.project.months.items() if path == self.config), None)
        if month is None:
            month, _ = _metadata(self.config)
        require(month in latest.months, "PROJECT_MONTH_MISSING",
                f"当前查看的 {month} 已从项目移除；请退出后重新选择月份", str(latest.path))
        roots = dict(zip(self.configs, self.review_roots))
        saved = {self.configs[index] for index in self.saved}
        configs = list(latest.months.values())
        self.review_roots = [roots.get(path, _review_root(path)) for path in configs]
        self.saved = {index for index, path in enumerate(configs) if path in saved}
        self.next_worksheets = {path: worksheet for path, worksheet in self.next_worksheets.items() if path in configs}
        self.index = configs.index(latest.months[month])
        self.configs, self.project, self.project_error = configs, latest, None

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
                     self.project.path if self.project is not None else self.config,
                     ("months",) if self.project is not None else ("month",))
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
    "项目模式会在保存审阅／月末声明或载入下一月后更新项目入口，默认进入上次保存的月份；登记路径不代表签发。",
    "项目模式按 r 重读已登记路径，跟进其他命令行会话的保存；保持当前查看月份，不随外部默认月份跳转。导出也先重读。",
    "项目更新失败但配置已保存时，保留本次新路径及警告；先登记该路径或显式打开，避免刷新丢失审阅结果。",
    "n：准备下一月填写表；用编辑器填好后，再按 n 选择载入。未确认的目标／账户继续列待审，不自动沿用月末声明。",
    "下一月保存后自动加入当前配置清单，可用 [／] 返回前月；重启时仍须提供已保存配置路径。",
    "p：输入起始与结束月份，查看所选连续区间；起始填 all 恢复全部已载入月份。Esc 取消，不改变当前查询。",
    "w：输入本次／下次真实工资日及可选周期额度；含本次工资日，不含下次工资日。只统计周期消费及额度差，不套用自然月目标或签发实际可花。",
    "查询区间不改变 cutover；各月余额仍按原历史重放，所需的历史归属及责任资料须齐全。",
    "c：切换当前月报与上次选择的区间；未选择时使用全部已载入最新配置。范围仅在本次 TUI 会话保留。",
    "区间重新审计每个月：只累计流量，目标差逐月比较，余额与可花取期末，任何月待审均不显示区间数字。",
    "m：在区间待审列表／详情中转到所选月份，重新审计后可按 e 填写；保存的新配置会用于下次区间比较。",
    "x：导出当前月份／区间的完整 text、json 或 csv；填写格式与新文件路径，Esc 取消。",
    "导出前重新审计，使用最新已保存配置，忽略显示筛选；待审时只导出待审清单。CSV 使用 UTF-8 BOM。",
    "/：输入关键词筛选；支持中文、账户、用途、原因码和来源路径。空查询清除筛选。",
    "t：切换完整报告文本和待审列表。待审状态只显示未通过项目，不显示局部正式数字。",
    "a：已签发后切换交易来源列表；可按类别、资金用途、责任 ID、摘要或账户筛选。",
    "↑↓ 或 j/k：滚动文本；在待审列表选择项目。PgUp/PgDn 或空格翻页，g/G 到首尾。",
    "Enter：打开所选待审／交易详情；交易显示逐腿原币、分类、用途、责任及原始账本内容。",
    "待审项分行显示：日期与本笔金额、文件名与行号、账户、原因。Enter 查看完整路径；详情可用 ↑↓／PgUp／PgDn 滚动。",
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
        if hasattr(stdscr, "move") and hasattr(stdscr, "clrtoeol"):
            stdscr.move(row, 0)
            stdscr.clrtoeol()
        # Native Windows curses can lose the tail of styled CJK text in VS
        # Code's terminal. Keep content plain; selection has a textual marker.
        stdscr.addstr(row, 0, _clip(_clean(text), max(0, stdscr.getmaxyx()[1] - 1)),
                      0 if sys.platform == "win32" else style)
    except curses.error:
        pass


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
        if hasattr(browser, "user_config"):
            lines = ["两文件模式：只维护 config.yml、budgets.yml 和账本；内部审阅记录自动保存。",
                     "m 跳到任意 cutover 后月份；n 直接进入下一月；r 重读两份配置与账本，不用手工编译。",
                     "[ / ] 上一月／下一月；月份切换保留原 cutover，不重置资金余额。",
                     "w 输入两次工资日及可选额度；p 选择区间；c 返回月报或上次查询。",
                     "↑↓ 选项目；Enter 看完整来源和原因；Esc 返回；/ 筛选；q 退出。",
                     "e 保存分类／资金用途决定；也可直接在账本写 budget／fund 后按 r。同一腿只认领一次。",
                     "f 核对截至月末的账单完整性；确认后保存内部记录，不会自动沿用到下一月。",
                     "x 导出；t 报告／待审；a 已审交易明细；? 关闭帮助。",
                     "未写目标表示不考核；写 null 表示尚未决定，仍待审；0 是真实零目标。",
                     f"用户配置：{browser.user_config}"]
            if browser.runtime is not None:
                lines.append(f"预算目标：{browser.runtime.budgets}")
        if browser.project is not None:
            lines += [f"项目入口：{browser.project.path}", *browser.project_messages()]
        if browser.period:
            lines += [browser.period_label(), "区间使用的最新配置：", *map(str, browser.period_inputs)]
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
            subtitle += f"；交易总数 {count}"
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
    for row, line in enumerate(lines[browser.offset:browser.offset + page], 3):
        _put(stdscr, row, line)
    footer = ("w 工资周期 p 范围 x 导出 c 月报 m 转该月 r 重算 / 筛选 Enter 详情 ? 帮助 q 退出" if browser.period else
              "w 工资周期 p 区间 m 跳月 n 下一月 f 月末 x 导出 c 比较 r 重算 / 筛选 e 审阅 ? 帮助 q 退出")
    if listing and browser.view == "reviews":
        footer = "↑↓ 选项 Enter 详情 r 重算 / 筛选 ? 更多按键 q 退出"
    elif browser.detail:
        footer = "↑↓ 滚动 PgUp/PgDn 翻页 Esc 返回 ? 帮助 q 退出"
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
        if hasattr(browser, "user_config"):
            preview = [*context[:5], "本次只批准这一项：",
                       *[f"{name}：{amount_text(amount)} {row['commodity']}" for name, amount in parts],
                       f"理由：{reason}", "审阅记录自动保存；以后仍使用同一份 config.yml。"]
        approved = _read_field(stdscr, "核对后明确批准", preview, "输入 y 并回车保存；其他输入或 Esc 取消：")
        if approved != "y":
            return
        browser.clear()
        _draw_browser(stdscr, browser)
        result = save_review(prepared, decision, reason, output, approved=True)
        if hasattr(browser, "user_config"):
            browser.show_saved_month(result)
        else:
            previous = browser.config
            browser.configs[browser.index] = Path(result["config"]).resolve()
            browser.remember_current_config(previous)
            browser.saved.add(browser.index)
            browser.filter("")
            browser.refresh()
        lines = ["本项决定已保存；当前月份已切换到新配置并重新审计。",
                 f"以后请使用：{browser.config}", f"审阅记录：{output}"]
        if hasattr(browser, "user_config"):
            lines = ["本项决定已保存并重新审计；以后仍使用同一份配置。", f"用户入口：{browser.user_config}"]
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
    from .tui_next_month import load_next_month, month_workspace, next_month_context, prepare_next_month

    if browser.period or browser.error or browser.help or not browser.outcome:
        return
    if hasattr(browser, "advance_month"):
        browser.advance_month()
        _draw_browser(stdscr, browser)
        browser.refresh()
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
        if hasattr(browser, "user_config"):
            preview = [*context, f"核对依据：{reason.strip()}", "声明自动保存到本月的内部记录；以后仍使用同一份 config.yml。"]
        approved = _read_field(stdscr, "明确批准本月末声明", preview,
                               "输入 y 并回车保存；其他输入或 Esc 取消：")
        if approved != "y":
            return
        browser.clear()
        _draw_browser(stdscr, browser)
        result = save_month_end(prepared, reason, output, approved=True)
        browser.show_saved_month(result)
        lines = [f"本月末核对声明已保存：{browser.config}", "原账本与原配置未改写。"]
        if hasattr(browser, "user_config"):
            lines = ["本月末核对声明已自动保存；以后仍使用同一份配置。", f"用户入口：{browser.user_config}"]
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


def _select_salary_cycle(stdscr, browser: ReportBrowser):
    if browser.help:
        return
    try:
        from .project import _metadata
        from ..tools.v6_next_month import _next
        month, _ = _metadata(browser.config)
        previous = browser.salary_cycle or (month + "-01", _next(month) + "-01", None, None)
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
        elif key == "p":
            _select_period(stdscr, browser)
        elif key == "w":
            _select_salary_cycle(stdscr, browser)
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
            if listing and browser.view == "reviews" and key in (curses.KEY_PPAGE, curses.KEY_NPAGE, " "):
                value = _review_page_target(browser, page, stdscr.getmaxyx()[1] - 1,
                                            key != curses.KEY_PPAGE)
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

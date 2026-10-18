"""Bind command-line worksheets and saved results to a project's latest month."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sys

from ..console import command
from ..errors import BudgetError, require
from ..yamlstrict import load_yaml, require_yaml
from .project import Project, _metadata, load_project
from .targets import _month


@dataclass
class ProjectAction:
    project: Project
    config: Path
    month: str
    inputs: dict[Path, bytes]

    def check(self) -> None:
        for path, content in self.inputs.items():
            require(path.is_file() and path.read_bytes() == content,
                    "INPUT_CHANGED", "操作期间项目、原配置或填写表发生变化；请重新核对", str(path))

    def finish(self, result: dict) -> dict:
        """A saved financial config survives any later navigation failure."""
        result = {**result, "project": str(self.project.path), "project_status": "UNCHANGED"}
        try:
            self.check()
            if result.get("worksheet"):
                attach_project_instructions(result, self)
            elif result.get("config"):
                config = Path(result["config"]).resolve()
                require(_metadata(config) == (self.month, self.project.identity), "PROJECT_MONTH_MISMATCH",
                        "保存结果的月份或账本范围与本次项目操作不同", str(config))
                self.project.remember(config, self.config)
                result["project_status"] = "UPDATED"
        except (BudgetError, OSError, UnicodeError) as exc:
            result.update(project_status="UPDATE_FAILED", project_error={
                "code": exc.code if isinstance(exc, BudgetError) else "PROJECT_IO_ERROR",
                "message": exc.message if isinstance(exc, BudgetError) else str(exc),
                "source": exc.source if isinstance(exc, BudgetError) else str(self.project.path)})
        return result


def prepare_project_action(path: Path, *, config: Path | None = None, worksheet: Path | None = None,
                           month: str | None = None, successor: bool = False) -> ProjectAction:
    project = load_project(path)
    before = {project.path: project.original}
    source = project.path
    keys = ("months",)
    if worksheet is not None:
        worksheet = worksheet.resolve()
        before[worksheet] = worksheet.read_bytes()
        document = load_yaml(worksheet)
        require_yaml(type(document) is dict and type(document.get("config")) is str and
                     bool(document["config"].strip()), "PROJECT_WORKSHEET_INVALID",
                     "填写表须引用原月份配置", worksheet, ("config",))
        config = (worksheet.parent / document["config"]).resolve()
        source, keys = worksheet, ("config",)
    config = config.resolve() if config is not None else project.select(month)
    before[config] = config.read_bytes()
    base_month, identity = _metadata(config)
    require_yaml(identity == project.identity, "PROJECT_CONTEXT_MISMATCH",
                 "原配置不属于该项目的账本／cutover／报告货币", source, keys)
    require_yaml(project.months.get(base_month) == config, "PROJECT_MONTH_CHANGED",
                 "原配置不是项目中该月的最新入口；请从最新配置重新生成填写表", source, keys)
    if month is not None:
        require(base_month == _month(month), "PROJECT_MONTH_MISMATCH", "指定月份与原配置不同", str(config))
    target = base_month
    if successor:
        first = date.fromisoformat(base_month + "-01")
        try:
            target = date(first.year + (first.month == 12), first.month % 12 + 1, 1).isoformat()[:7]
        except ValueError as exc:
            raise BudgetError("TIME_INVALID", "无法准备超出日期范围的下一月", str(config)) from exc
        require_yaml(target not in project.months, "PROJECT_NEXT_MONTH_EXISTS",
                     f"{target} 已登记，请切到该月继续审核，不再从上一月生成配置", project.path, ("months", target))
    action = ProjectAction(project, config, target, before)
    action.check()
    return action


def attach_project_instructions(result: dict, action: ProjectAction) -> None:
    """Make the generated worksheet's apply command keep the project context."""
    if not result.get("worksheet"):
        return
    instructions = Path(result["worksheet"]).parent / "START_HERE.md"
    # Absolute paths work when the user runs the instructions from the worksheet
    # directory. Both command rendering and spaces support Windows PowerShell.
    project = str(action.project.path)
    if sys.platform == "win32":
        quoted = "'" + project.replace("'", "''") + "'"
    else:
        from shlex import quote
        quoted = quote(project)
    text = instructions.read_text(encoding="utf-8")
    for name in ("beancount-budget-review", "beancount-budget-next-month"):
        text = text.replace(command(name) + " --apply ", command(name) + f" --project {quoted} --apply ")
    instructions.write_text(text, encoding="utf-8")


def print_project_result(result: dict) -> None:
    if result.get("project_error"):
        error = result["project_error"]
        print(f"项目入口未更新：{error['code']} {error['message']} ({error['source']})")
    elif result.get("project_status") == "UPDATED":
        print(f"项目入口已更新：{result['project']}")

"""A navigation-only registry of monthly configurations; never an approval."""

import argparse
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import yaml

from ..console import configure_output
from ..errors import BudgetError, require
from ..yamlstrict import load_yaml, require_yaml, yaml_path_location
from .targets import _month


DEFAULT_PROJECT = "budget-project.yaml"


def _metadata(path: Path) -> tuple[str, tuple]:
    document = load_yaml(path)
    require_yaml(type(document) is dict and document.get("schema") == 6 and type(document.get("schema")) is int,
                 "PROJECT_MONTH_INVALID", "项目须引用 schema 6 月报配置", path)
    try:
        month = _month(document.get("month"))
    except BudgetError as exc:
        raise BudgetError(exc.code, exc.message, yaml_path_location(path, ("month",))) from exc
    for key in ("ledger", "cutover", "report_currency"):
        require_yaml(type(document.get(key)) is str and bool(document[key].strip()),
                     "PROJECT_MONTH_INVALID", "月份配置缺少账本、cutover 或报告货币", path, (key,))
    try:
        cutover = date.fromisoformat(document["cutover"])
    except ValueError as exc:
        raise BudgetError("CUTOVER_INVALID", "cutover 须为自然月第一天", yaml_path_location(path, ("cutover",))) from exc
    require_yaml(cutover.day == 1 and cutover <= date.fromisoformat(month + "-01"),
                 "CUTOVER_INVALID", "cutover 须为不晚于报告月的自然月第一天", path, ("cutover",))
    ledger = (path.parent / document["ledger"]).resolve()
    require_yaml(ledger.is_file(), "PROJECT_MONTH_INVALID", "月份配置引用的账本不存在", path, ("ledger",))
    return month, (ledger, cutover, document["report_currency"])


def _validate(months: dict[str, Path], source: Path) -> tuple:
    identity = None
    for month, path in months.items():
        require_yaml(path.is_file(), "PROJECT_MONTH_MISSING", "项目引用的月份配置不存在", source, ("months", month))
        actual, context = _metadata(path)
        require_yaml(actual == month, "PROJECT_MONTH_MISMATCH", f"登记月份 {month} 与配置月份 {actual} 不同",
                     source, ("months", month))
        require_yaml(identity is None or identity == context, "PROJECT_CONTEXT_MISMATCH",
                     "同一项目的账本、cutover 和报告货币必须相同", source, ("months", month))
        identity = context
    return identity


def _content(path: Path, months: dict[str, Path], default: str) -> bytes:
    def relative(config):
        try:
            return os.path.relpath(config, path.parent)
        except ValueError:  # Different Windows drives.
            return str(config)
    return yaml.safe_dump({"schema": 1, "default_month": default,
                           "months": {month: relative(config) for month, config in sorted(months.items())}},
                          allow_unicode=True, sort_keys=False).encode("utf-8")


@dataclass
class Project:
    path: Path
    months: dict[str, Path]
    default_month: str
    original: bytes
    identity: tuple

    def select(self, month: str | None = None) -> Path:
        month = self.default_month if month is None else _month(month)
        require_yaml(month in self.months, "PROJECT_MONTH_MISSING", f"项目尚未登记 {month}；请先准备或登记该月配置",
                     self.path, ("months",))
        return self.months[month]

    def remember(self, config: Path, previous: Path) -> None:
        """Publish only navigation paths, refusing concurrent external edits."""
        config = config.resolve()
        original = config.read_bytes()
        month, _ = _metadata(config)
        require(month not in self.months or self.months[month] == previous.resolve(),
                "PROJECT_MONTH_CHANGED", "该月份的入口与当前保存来源不同；请核对项目文件", str(self.path))
        self.register([config], default_month=month, expected_inputs={config: original})

    def register(self, configs: list[Path], default_month: str | None = None, *,
                 expected_inputs: dict[Path, bytes] | None = None) -> None:
        """Explicitly add/replace months as one atomic navigation update."""
        months, replacements = dict(self.months), {}
        before = dict(expected_inputs or {})
        for config in configs:
            config = config.resolve()
            require(config.is_file(), "PROJECT_MONTH_MISSING", "指定的月份配置不存在", str(config))
            if config not in before:
                before[config] = config.read_bytes()
            month, identity = _metadata(config)
            require(identity == self.identity, "PROJECT_CONTEXT_MISMATCH",
                    "新配置不属于当前账本／cutover／报告货币项目", str(config))
            require(month not in replacements, "PROJECT_MONTH_DUPLICATE",
                    f"{month} 登记了不止一个配置，请选择最新配置", str(config))
            replacements[month] = config
        months.update(replacements)
        default = self.default_month if default_month is None else _month(default_month)
        require(default in months, "PROJECT_DEFAULT_INVALID", "默认月份须已登记", str(self.path))
        # Keep the pre-read bytes of replacement headers; never bless a header
        # edited between its identity check and publication.
        for path in months.values():
            if path not in before:
                before[path] = path.read_bytes()
        require(_validate(months, self.path) == self.identity, "PROJECT_CONTEXT_MISMATCH",
                "项目月份的账本／cutover／报告货币发生变化；请重新打开项目", str(self.path))
        content = _content(self.path, months, default)
        with TemporaryDirectory(prefix=".budget-project-", dir=self.path.parent) as folder:
            staging = Path(folder) / "project.yaml"
            staging.write_bytes(content)
            require(self.path.read_bytes() == self.original and
                    all(path.read_bytes() == data for path, data in before.items()),
                    "PROJECT_CHANGED", "项目或月份入口在会话中发生变化；本次不覆盖，请核对后重新打开项目", str(self.path))
            os.replace(staging, self.path)
        self.months, self.default_month, self.original = dict(sorted(months.items())), default, content


def load_project(path: Path) -> Project:
    path = path.resolve()
    original = path.read_bytes()
    document = load_yaml(path)
    require_yaml(type(document) is dict, "PROJECT_INVALID", "项目文件须为映射", path)
    fields = {"schema", "default_month", "months"}
    invalid = sorted(set(document) - fields, key=str) or sorted(fields - set(document))
    require_yaml(not invalid,
                 "PROJECT_INVALID", "项目文件须含 schema: 1、default_month 和 months", path,
                 (invalid[0],) if invalid else ())
    require_yaml(type(document["schema"]) is int and document["schema"] == 1,
                 "PROJECT_INVALID", "项目入口须使用 schema: 1", path, ("schema",))
    require_yaml(type(document["months"]) is dict and bool(document["months"]),
                 "PROJECT_INVALID", "months 须登记至少一个月份配置", path, ("months",))
    months = {}
    for month, value in document["months"].items():
        try:
            _month(month)
        except BudgetError as exc:
            raise BudgetError(exc.code, exc.message, yaml_path_location(path, ("months", month))) from exc
        require_yaml(type(value) is str and bool(value.strip()), "PROJECT_INVALID", "月份配置路径须为非空字符串",
                     path, ("months", month))
        months[month] = (path.parent / value).resolve()
    require_yaml(type(document["default_month"]) is str and document["default_month"] in months,
                 "PROJECT_DEFAULT_INVALID", "default_month 须选择已登记的月份", path, ("default_month",))
    identity = _validate(months, path)
    require(path.read_bytes() == original, "PROJECT_CHANGED", "读取期间项目入口发生变化，请重新打开", str(path))
    return Project(path, dict(sorted(months.items())), document["default_month"], original, identity)


def create_project(configs: list[Path], output: Path, default_month: str | None = None) -> Project:
    output = output.resolve()
    require(not output.exists(), "PROJECT_EXISTS", "项目文件已存在；不会覆盖已有月份入口", str(output))
    months = {}
    before = {}
    for path in configs:
        path = path.resolve()
        require(path.is_file(), "PROJECT_MONTH_MISSING", "指定的月份配置不存在", str(path))
        before[path] = path.read_bytes()
        month, _ = _metadata(path)
        require(month not in months, "PROJECT_MONTH_DUPLICATE", f"{month} 登记了不止一个配置，请选择最新配置", str(path))
        months[month] = path
    require(bool(months), "PROJECT_INVALID", "请提供至少一个已有月份配置", str(output))
    default = default_month if default_month is not None else max(months)
    require(default in months, "PROJECT_DEFAULT_INVALID", "默认月份须已登记", str(output))
    # _validate only resolves source locations on a failed check. During
    # creation there is no project file yet, so validate context here instead.
    identities = {_metadata(path)[1] for path in months.values()}
    require(len(identities) == 1, "PROJECT_CONTEXT_MISMATCH", "不能混合不同账本、cutover 或报告货币", str(output))
    require(all(path.read_bytes() == data for path, data in before.items()), "PROJECT_CHANGED", "登记期间月份配置发生变化", str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    content = _content(output, months, default)
    try:
        with output.open("xb") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise BudgetError("PROJECT_EXISTS", "项目文件已存在；不会覆盖", str(output)) from exc
    return Project(output, dict(sorted(months.items())), default, content, identities.pop())


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description="登记已有月报配置，创建或更新项目入口；不批准任何预算事实")
    parser.add_argument("--config", type=Path, action="append", default=[], help="新增或替换月份的最新配置，可重复")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", type=Path, help=f"新建入口；默认 {DEFAULT_PROJECT}，不覆盖已有文件")
    mode.add_argument("--update", type=Path, help="更新已有项目；显式替换所提供月份的入口，保留其他月份")
    parser.add_argument("--default-month", help="默认启动月份；新建时默认最近一月，更新时默认保留原选择")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if not args.config and (not args.update or not args.default_month):
        parser.error("请提供 --config；仅更新默认月份时可用 --update 项目 --default-month YYYY-MM")
    path = args.update or args.output or Path(DEFAULT_PROJECT)
    try:
        if args.update:
            project = load_project(path)
            project.register(args.config, args.default_month)
        else:
            project = create_project(args.config, path, args.default_month)
    except (BudgetError, OSError, UnicodeError) as exc:
        error = {"code": exc.code, "message": exc.message, "source": exc.source} if isinstance(exc, BudgetError) else {
            "code": "PROJECT_IO_ERROR", "message": str(exc), "source": str(getattr(exc, "filename", None) or path)}
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    result = {"status": "PROJECT_UPDATED" if args.update else "PROJECT_CREATED",
              "project": str(project.path), "default_month": project.default_month,
              "months": sorted(project.months), "note": "只登记已有配置；每次查看仍重新审计，不代表月份已经签发。"}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.format == "json" else
          f"项目入口：{project.path}\n默认月份：{project.default_month}\n{result['note']}")
    return 0

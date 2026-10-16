"""Save complete audited workflows using the existing public report formats."""

import json
from pathlib import Path

from ..errors import BudgetError, require
from .monthly_report import render_workflow_text
from .period_report import _single_month_period, render_csv, render_text


FORMATS = {"text": ".txt", "json": ".json", "csv": ".csv"}


def write_export(outcome: dict, config: Path, path: Path, format: str, *, period: bool) -> None:
    """Publish a fresh full result; exclusive create also rejects late collisions."""
    require(format in FORMATS, "TUI_EXPORT_FORMAT_INVALID", "导出格式须为 text、json 或 csv")
    if format == "json":
        content = json.dumps(outcome, ensure_ascii=False, indent=2) + "\n"
    elif format == "csv":
        content = render_csv(outcome if period else _single_month_period(outcome, config))
    else:
        content = (render_text(outcome) if period else render_workflow_text(outcome)) + "\n"
    try:
        with path.open("x", encoding="utf-8-sig" if format == "csv" else "utf-8", newline="") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise BudgetError("TUI_EXPORT_EXISTS", "输出文件已存在，请选择新文件", str(path.absolute())) from exc

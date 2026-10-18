"""Prepare/compile next-month worksheets using the browser's latest config."""

from pathlib import Path

from ..errors import require
from ..tools.v6_next_month import _next, compile_next_month, create_next_month
from ..tools.v6_review_decisions import _config, _unchanged
from ..yamlstrict import load_yaml, yaml_key_location
from .monthly_report import _config_month


def next_month_context(config: Path, configs: list[Path]) -> tuple[str, dict[Path, bytes]]:
    """Avoid adding a second config for a month already supplied to the TUI."""
    config = config.resolve()
    before = {path: path.read_bytes() for path in {config, *(path.resolve() for path in configs)}}
    document, paths, _ = _config(config)
    month = _next(document["month"])
    for other in configs:
        other_document = load_yaml(other)
        require(type(other_document) is dict, "MONTH_REPORT_CONFIG_INVALID", "月报配置必须是映射", f"{other}:1")
        require(_config_month(other_document, other) != month, "TUI_NEXT_MONTH_EXISTS",
                f"{month} 已在当前配置清单中；请用 [／] 切到该月继续审核", str(other))
    before.update({path: path.read_bytes() for path in paths.values() if path != paths["ledger"]})
    _unchanged(before)
    return month, before


def prepare_next_month(config: Path, configs: list[Path], output: Path) -> dict:
    month, before = next_month_context(config, configs)
    result = create_next_month(config, output, expected_inputs=before)
    _unchanged(before)
    require(result["month"] == month, "TUI_MONTH_CONTEXT_CHANGED", "准备期间月份配置发生变化；请重新核对", str(config))
    return result


def load_next_month(config: Path, configs: list[Path], worksheet: Path) -> dict:
    """Only apply an explicitly filled sheet based on this exact latest config."""
    month, before = next_month_context(config, configs)
    worksheet = worksheet.resolve()
    before[worksheet] = worksheet.read_bytes()
    document = load_yaml(worksheet)
    require(type(document) is dict and type(document.get("config")) is str and bool(document["config"].strip()),
            "NEXT_MONTH_INVALID", "请选择已填写的 next-month.yaml", f"{worksheet}:1")
    require((worksheet.parent / document["config"]).resolve() == config.resolve(),
            "TUI_NEXT_MONTH_BASE_CHANGED", "填写表引用的不是当前最新配置；请从最新配置重新准备下一月",
            yaml_key_location(worksheet, "config"))
    require(document.get("month") == month, "TUI_MONTH_CONTEXT_CHANGED",
            "填写表不是当前配置的紧接下一月", yaml_key_location(worksheet, "month"))
    _unchanged(before)
    return compile_next_month(worksheet, include_transactions=True, expected_inputs=before)


def month_workspace(config: Path, review_root: Path) -> Path:
    """Keep subsequent drafts as siblings, including after restarting the TUI."""
    return next((parent for parent in config.resolve().parents if parent.name == "tui-months"),
                review_root.parent / "tui-months")

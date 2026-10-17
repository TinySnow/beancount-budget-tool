"""Explicit month-end confirmation, bound to the inputs shown in its form."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from beancount.core import data

from ..errors import BudgetError, require
from ..tools.v6_init import _inputs
from ..tools.v6_next_month import confirm_month
from ..tools.v6_review_decisions import _config, _unchanged


@dataclass(frozen=True)
class PreparedMonthEnd:
    config: Path
    ledger: Path
    month: str
    cutover: date
    as_of: date
    snapshot: str
    transactions: int
    latest_transaction: date | None
    inputs: dict[Path, bytes]


def prepare_month_end(config: Path, displayed_month: str) -> PreparedMonthEnd:
    config = config.resolve()
    original = config.read_bytes()
    document, paths, cutover = _config(config)
    require(document["month"] == displayed_month, "TUI_MONTH_CONTEXT_CHANGED",
            "月份配置已改变，请返回并重算后核对", str(config))
    before = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    require(before[config] == original, "INPUT_CHANGED", "读取期间月份配置发生变化，请重算后重新核对", str(config))
    with _inputs(paths["ledger"], displayed_month, cutover) as (fin, last):
        transactions = [entry for entry in fin.entries if isinstance(entry, data.Transaction)]
        count = sum(entry.date.isoformat().startswith(displayed_month) for entry in transactions)
        latest = max((entry.date for entry in transactions), default=None)
        prepared = PreparedMonthEnd(config, paths["ledger"], displayed_month, cutover,
                                    last, fin.digest, count, latest, before)
    _unchanged(before)
    return prepared


def save_month_end(prepared: PreparedMonthEnd, reason: str, output: Path, *, approved: bool = False) -> dict:
    require(approved is True, "REVIEW_APPROVAL_REQUIRED", "须明确批准本月末核对声明后才能保存")
    require(type(reason) is str and bool(reason.strip()), "NEXT_MONTH_REASON_REQUIRED",
            "请写明截至该月末资料、责任及在途付款完整性的核对依据", str(prepared.config))
    output = output.resolve()
    require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已存在，请选择新目录", str(output))
    _unchanged(prepared.inputs)
    try:
        with _inputs(prepared.ledger, prepared.month, prepared.cutover) as (fin, _):
            require(fin.digest == prepared.snapshot, "INPUT_CHANGED",
                    "填写期间账本或 include 来源发生变化，请返回重算后重新核对", str(prepared.ledger))
            _unchanged(prepared.inputs)
            result = confirm_month(prepared.config, reason.strip(), output, include_transactions=True,
                                   expected_inputs=prepared.inputs)
            _unchanged(prepared.inputs)
            return result
    except (BudgetError, OSError, UnicodeError) as exc:
        # A failure after atomic publication must retain the newly saved path,
        # while withholding all formal numbers from the browser.
        config = output / "month.yaml"
        if config.is_file():
            return {"status": "COMPLETENESS_SAVED", "month": prepared.month, "config": str(config),
                    "review_items": [], "workflow": None, "report_error": {
                        "code": exc.code if isinstance(exc, BudgetError) else "NEXT_MONTH_IO_ERROR",
                        "message": exc.message if isinstance(exc, BudgetError) else str(exc),
                        "source": exc.source if isinstance(exc, BudgetError) else str(config)}}
        raise

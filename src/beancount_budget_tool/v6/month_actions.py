"""Atomic month-end confirmation used by the current TUI."""
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from beancount.core import data
from ..errors import BudgetError, require
from ..financial import month_bounds
from .monthly_report import audit_month_workflow
from .input_compiler import (read_month_config, check_unchanged, month_inputs,
    relative_reference, publish_inputs, report_saved_inputs, saved_report_failure)


def confirm_month(config: Path, reason: str, output: Path, *, include_transactions: bool = False,
                  expected_inputs: dict[Path, bytes] | None = None) -> dict:
    """Attach a new month-end assertion to the latest reviewed config."""
    config, output = config.resolve(), output.resolve()
    check_unchanged(expected_inputs)
    doc, paths, _ = read_month_config(config)
    require(type(reason) is str and bool(reason.strip()), "NEXT_MONTH_REASON_REQUIRED",
            "请写明截至该月末资料、责任及在途付款完整性的核对依据", str(config))
    _, last = month_bounds(doc["month"])
    before = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    candidate = {**doc, **{key: relative_reference(path, output) for key, path in paths.items()},
                 "completeness": "completeness.yaml"}
    statement = {"schema": 6, "cutover": doc["cutover"], "as_of": last.isoformat(),
                 "unbooked_obligations": "none", "inflight_payments": "none", "reason": reason}
    check_unchanged(before)
    check_unchanged(expected_inputs)
    publish_inputs(output, {"month.yaml": candidate, "completeness.yaml": statement})
    result = {"status": "COMPLETENESS_SAVED", "month": doc["month"],
              "config": str(output / "month.yaml"), "review_items": []}
    return report_saved_inputs(result, audit_month_workflow, before, expected_inputs,
                               io_error="NEXT_MONTH_IO_ERROR", include_transactions=include_transactions)


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
    document, paths, cutover = read_month_config(config)
    require(document["month"] == displayed_month, "TUI_MONTH_CONTEXT_CHANGED",
            "月份配置已改变，请返回并重算后核对", str(config))
    before = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    require(before[config] == original, "INPUT_CHANGED", "读取期间月份配置发生变化，请重算后重新核对", str(config))
    with month_inputs(paths["ledger"], displayed_month, cutover) as (fin, last):
        transactions = [entry for entry in fin.entries if isinstance(entry, data.Transaction)]
        count = sum(entry.date.isoformat().startswith(displayed_month) for entry in transactions)
        latest = max((entry.date for entry in transactions), default=None)
        prepared = PreparedMonthEnd(config, paths["ledger"], displayed_month, cutover,
                                    last, fin.digest, count, latest, before)
    check_unchanged(before)
    return prepared


def save_month_end(prepared: PreparedMonthEnd, reason: str, output: Path, *, approved: bool = False) -> dict:
    require(approved is True, "REVIEW_APPROVAL_REQUIRED", "须明确批准本月末核对声明后才能保存")
    require(type(reason) is str and bool(reason.strip()), "NEXT_MONTH_REASON_REQUIRED",
            "请写明截至该月末资料、责任及在途付款完整性的核对依据", str(prepared.config))
    output = output.resolve()
    require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已存在，请选择新目录", str(output))
    check_unchanged(prepared.inputs)
    try:
        with month_inputs(prepared.ledger, prepared.month, prepared.cutover) as (fin, _):
            require(fin.digest == prepared.snapshot, "INPUT_CHANGED",
                    "填写期间账本或 include 来源发生变化，请返回重算后重新核对", str(prepared.ledger))
            check_unchanged(prepared.inputs)
            result = confirm_month(prepared.config, reason.strip(), output, include_transactions=True,
                                   expected_inputs=prepared.inputs)
            check_unchanged(prepared.inputs)
            return result
    except (BudgetError, OSError, UnicodeError) as exc:
        # A failure after atomic publication must retain the newly saved path,
        # while withholding all formal numbers from the browser.
        config = output / "month.yaml"
        if config.is_file():
            return saved_report_failure({"status": "COMPLETENESS_SAVED", "month": prepared.month,
                                         "config": str(config), "review_items": []}, exc, "NEXT_MONTH_IO_ERROR")
        raise

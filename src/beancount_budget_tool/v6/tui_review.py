"""Explicit single-item terminal decisions using the existing review worksheet."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from ..errors import BudgetError, require
from ..yamlstrict import load_yaml
from ..tools.v6_init import _ledger_reference, _publish
from ..tools.v6_review_decisions import (
    EDITABLE, _config, _unchanged, apply_review, create_review,
)
from .classification import _key
from .inline_amounts import parse_inline_amounts


@dataclass
class PreparedReview:
    config: Path
    document: dict
    choices: dict
    other_items: dict
    instructions: str
    selected: int
    inputs: dict[Path, bytes]

    @property
    def row(self) -> dict:
        return self.document["rows"][self.selected]

    @property
    def names(self) -> list[str]:
        return self.choices["budget" if self.row["kind"] == "classification" else "fund"]


def prepare_review(config: Path, item: dict) -> PreparedReview:
    """Refresh the selected source and choices without writing persistent files."""
    require(item.get("kind") in EDITABLE and item.get("reason_kind") != "implementation_limit",
            "TUI_REVIEW_UNSUPPORTED", "此项需要相应配置或程序支持；TUI 只填写分类与 H 资产用途",
            item.get("source"))
    config = config.resolve()
    _, paths, _ = _config(config)
    inputs = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    with TemporaryDirectory(prefix="budget-tui-review-") as directory:
        folder = Path(directory) / "draft"
        result = create_review(config, folder)
        document = load_yaml(Path(result["worksheet"]))
        selected = next((index for index, row in enumerate(document["rows"])
                         if row["kind"] == item["kind"] and _key(row["ref"]) == _key(item["ref"]) and
                         all(row[field] == item[field] for field in
                             ("date", "account", "commodity", "signed_units"))), None)
        require(selected is not None, "REVIEW_SOURCE_CHANGED",
                "选中来源已变或已被认领；请返回当前清单重新选择", item.get("source"))
        prepared = PreparedReview(config, document, load_yaml(folder / "choices.yaml"),
                                  load_yaml(folder / "other-items.yaml"),
                                  (folder / "START_HERE.md").read_text(encoding="utf-8"), selected, inputs)
    _unchanged(inputs)
    return prepared


def validate_decision(prepared: PreparedReview, decision: str, reason: str):
    require(isinstance(reason, str) and bool(reason.strip()), "REVIEW_REASON_REQUIRED",
            "请填写判断依据", prepared.row["source"])
    parts = parse_inline_amounts(decision, Fraction(prepared.row["signed_units"]), prepared.row["source"],
                                known_names=set(prepared.names), code="REVIEW_DECISION_INVALID")
    require(all(name in prepared.names for name, _ in parts), "REVIEW_DECISION_INVALID",
            "名称未登记；请使用界面列出的名称或别名", prepared.row["source"])
    return parts


def save_review(prepared: PreparedReview, decision: str, reason: str, output: Path,
                *, approved: bool = False) -> dict:
    """Publish a new worksheet and configuration only after explicit approval."""
    require(approved is True, "REVIEW_APPROVAL_REQUIRED", "须明确批准本项后才能保存")
    validate_decision(prepared, decision, reason)
    _unchanged(prepared.inputs)
    output = output.resolve()
    document = deepcopy(prepared.document)
    document["config"] = _ledger_reference(prepared.config, output)
    document["rows"][prepared.selected].update(decision=decision, reason=reason.strip(), approved=True)
    instructions = ("# TUI 审阅记录\n\n"
                    "本次只批准 decisions.yaml 中的一项。若 reviewed/month.yaml 已生成，"
                    "以后请使用该配置继续；原配置不会自动改写。\n\n" + prepared.instructions)
    _publish(output, {"decisions.yaml": document, "choices.yaml": prepared.choices,
                      "other-items.yaml": prepared.other_items, "START_HERE.md": instructions})
    worksheet = output / "decisions.yaml"
    try:
        _unchanged(prepared.inputs)
        return apply_review(worksheet)
    except (BudgetError, OSError, UnicodeError) as exc:
        # Publication is atomic. A failure in the following report must not
        # conceal decisions that were already successfully published.
        config = output / "reviewed" / "month.yaml"
        if config.is_file():
            return {"status": "DECISIONS_SAVED", "config": str(config), "accepted": [
                {"kind": prepared.row["kind"], "source": prepared.row["source"], "decision": decision}],
                "workflow": None, "report_error": {
                    "code": exc.code if isinstance(exc, BudgetError) else "REVIEW_IO_ERROR",
                    "message": exc.message if isinstance(exc, BudgetError) else str(exc),
                    "source": exc.source if isinstance(exc, BudgetError) else str(config)}}
        raise

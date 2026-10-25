"""Shared category and H-purpose review, from worksheets to terminal decisions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from beancount.core import data
import yaml

from ..console import command
from ..errors import BudgetError, require
from ..exact import exact_text
from ..yamlstrict import load_yaml, yaml_path_location
from .classification import _key, _registry, audit_classification
from .facts import FactIndex
from .inline_amounts import parse_inline_amounts
from .monthly_report import audit_month_workflow
from .ownership import _owners, audit_ownership
from .input_compiler import (
    read_month_config, check_unchanged, month_inputs, relative_reference, publish_inputs,
    report_saved_inputs, saved_report_failure,
)


EDITABLE = {"classification": ("classifications", "classifications", "category_id"),
            "asset_ownership": ("ownership", "claims", "owner_id")}
ROW_FIELDS = {"kind", "ref", "date", "account", "commodity", "signed_units", "source",
              "payee", "narration", "decision", "reason", "approved"}


def create_review(config: Path, output: Path, *, expected_inputs: dict[Path, bytes] | None = None) -> dict:
    config, output = config.resolve(), output.resolve()
    check_unchanged(expected_inputs)
    doc, paths, cutover = read_month_config(config)
    before = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    with month_inputs(paths["ledger"], doc["month"], cutover) as (fin, _):
        workflow = audit_month_workflow(config)
        facts = FactIndex(fin)
        descriptions = {_key(facts.for_posting(p).as_dict()): entry
                        for entry in fin.entries if isinstance(entry, data.Transaction)
                        for p in entry.postings}
        rows, other = [], []
        for item in workflow["review_items"]:
            if item.get("kind") not in EDITABLE:
                other.append(item)
                continue
            entry = descriptions[_key(item["ref"])]
            rows.append({"kind": item["kind"], "date": item["date"],
                         "payee": entry.payee, "narration": entry.narration,
                         **{key: item[key] for key in ("account", "commodity", "signed_units", "source")},
                         "decision": None, "reason": None, "approved": False,
                         "ref": item["ref"]})
        worksheet = {"schema": 6, "config": relative_reference(config, output),
                     "month": doc["month"], "cutover": doc["cutover"], "rows": rows}
        categories = load_yaml(paths["classifications"])["categories"]
        owners = load_yaml(paths["ownership"])["owners"]
        choices = {"budget": [*sorted(_registry(categories)), "-"],
                   "fund": [*sorted(_owners(owners)), "-"]}
        instructions = (
            "# 交易待审：只填写 decisions.yaml\n\n"
            "每项已带入日期、摘要、原币金额和账本行号。kind=classification 填预算类别；"
            "kind=asset_ownership 填资产资金用途。可用名称见 choices.yaml。\n\n"
            "1. decision 填已登记名称，如 餐饮 或 储蓄；拆分写 '保险200，旅游900'，合计须等于分录绝对金额。"
            "符号由原分录决定，不填负金额。'-' 表示预算外。\n"
            "2. reason 写这次判断的依据。确定后把 approved 改为 true。不确定就保持 false；可以只批准部分。\n"
            "3. 不要修改 ref、kind、日期、账户、币种和分录金额；无需手工复制来源引用。"
            "改了原交易、加了行内标签或已有别人认领时，重新导出待审表。\n\n"
            f"```\n{command('beancount-budget-review')} --apply decisions.yaml\n```\n\n"
            "保存到新的 reviewed 目录，并自动重跑该月月报；原账本及原审阅文件只读。"
            "已有 reviewed 时用 --output 另选目录。后续以新目录的 month.yaml 继续审核。\n\n"
            "other-items.yaml 保留本轮不能通过这张表决定的项目，例如账户、期初、负债、转账协议、计价和目标。"
            "它们不会被批准或隐藏；本表只覆盖当前已发现的分类和 H 用途待审。"
            "保存后可能出现下一层待审，待审清零也须通过月报指标检查才能签发。\n")
    check_unchanged(before)
    check_unchanged(expected_inputs)
    publish_inputs(output, {"decisions.yaml": worksheet, "choices.yaml": choices,
                      "other-items.yaml": {"status": workflow["status"], "items": other},
                      "START_HERE.md": instructions})
    return {"status": "DRAFT_CREATED", "worksheet": str(output / "decisions.yaml"),
            "editable_items": len(rows), "other_items": len(other),
            "workflow_status": workflow["status"]}


def apply_review(worksheet: Path, output: Path | None = None, *,
                 expected_inputs: dict[Path, bytes] | None = None) -> dict:
    worksheet = worksheet.resolve()
    output = (output or worksheet.parent / "reviewed").resolve()
    check_unchanged(expected_inputs)
    document = load_yaml(worksheet)
    require(type(document) is dict and set(document) == {"schema", "config", "month", "cutover", "rows"} and
            type(document["schema"]) is int and document["schema"] == 6 and
            type(document["config"]) is str and bool(document["config"].strip()) and
            type(document["rows"]) is list,
            "REVIEW_WORKSHEET_INVALID", "待审表字段或版本无效", f"{worksheet}:1")
    config = (worksheet.parent / document["config"]).resolve()
    doc, paths, cutover = read_month_config(config)
    require(document["month"] == doc["month"] and document["cutover"] == doc["cutover"],
            "REVIEW_CONTEXT_CHANGED", "原配置月份或 cutover 已变；请重新导出待审表", str(worksheet))
    before = {path: path.read_bytes() for path in {worksheet, config, *paths.values()} if path != paths["ledger"]}
    classes, owners = load_yaml(paths["classifications"]), load_yaml(paths["ownership"])
    registries = {"classification": _registry(classes["categories"]),
                  "asset_ownership": _owners(owners["owners"])}
    changed = {"classifications": classes, "ownership": owners}
    accepted = []
    with month_inputs(paths["ledger"], doc["month"], cutover) as (fin, _):
        current = audit_month_workflow(config)
        facts = FactIndex(fin)
        descriptions = {_key(facts.for_posting(p).as_dict()): entry
                        for entry in fin.entries if isinstance(entry, data.Transaction)
                        for p in entry.postings}
        pending = {(item["kind"], _key(item["ref"])): item for item in current["review_items"]
                   if item.get("kind") in EDITABLE}
        seen = set()
        for index, row in enumerate(document["rows"]):
            location = yaml_path_location(worksheet, ("rows", index))
            require(type(row) is dict and set(row) == ROW_FIELDS and type(row["approved"]) is bool,
                    "REVIEW_WORKSHEET_INVALID", "待审项字段无效；approved 须为 true 或 false", location)
            require(type(row["kind"]) is str and row["kind"] in EDITABLE and type(row["ref"]) is dict,
                    "REVIEW_WORKSHEET_INVALID", "本表只接受分类或 H 资金用途来源", location)
            try:
                key = (row["kind"], _key(row["ref"]))
            except (TypeError, ValueError) as exc:
                raise BudgetError("REVIEW_WORKSHEET_INVALID", "来源引用格式被改动；请重新导出",
                                  yaml_path_location(worksheet, ("rows", index, "ref"))) from exc
            require(key not in seen, "REVIEW_DUPLICATE", "待审表包含重复来源", location)
            seen.add(key)
            if not row["approved"]:
                continue
            require(key in pending, "REVIEW_SOURCE_CHANGED",
                    "来源已变、更换了标签或已被认领；请重新导出当前待审项", location)
            item = pending[key]
            require(all(row[field] == item[field] for field in (
                "date", "account", "commodity", "signed_units")), "REVIEW_SOURCE_CHANGED",
                "待审项的日期、账户、币种或金额被改动；请重新导出", location)
            entry = descriptions[_key(item["ref"])]
            require(row["payee"] == entry.payee and row["narration"] == entry.narration,
                    "REVIEW_SOURCE_CHANGED", "待审项的交易摘要被改动；请重新导出", location)
            require(type(row["reason"]) is str and bool(row["reason"].strip()),
                    "REVIEW_REASON_REQUIRED", "批准项须填写审阅理由", yaml_path_location(worksheet, ("rows", index, "reason")))
            registry = registries[row["kind"]]
            decision_location = yaml_path_location(worksheet, ("rows", index, "decision"))
            parts = parse_inline_amounts(row["decision"], Fraction(item["signed_units"]), decision_location,
                                         known_names=set(registry), code="REVIEW_DECISION_INVALID")
            require(all(name == "-" or name in registry for name, _ in parts),
                    "REVIEW_DECISION_INVALID", "名称未登记；请使用 choices.yaml 的名称或别名", decision_location)
            target, field, owner_field = EDITABLE[row["kind"]]
            stem = "review_" + sha256((row["kind"] + _key(row["ref"])).encode()).hexdigest()
            slices = [{"id": f"{stem}_{part_index}", owner_field: registry.get(name, "-"),
                       "signed_units": exact_text(amount),
                       **({"role": "hold"} if target == "ownership" else {})}
                      for part_index, (name, amount) in enumerate(parts)]
            changed[target][field].append({"claim": item["ref"], "reason": row["reason"], "slices": slices})
            accepted.append({"kind": row["kind"], "source": item["source"], "decision": row["decision"]})
        if not accepted:
            check_unchanged(before)
            check_unchanged(expected_inputs)
            return {"status": "NO_APPROVED_DECISIONS", "config": None, "accepted": [], "workflow": current}
        require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已存在；请选择新目录", str(output))
        # Validate both claim domains before publishing any review decisions.
        with TemporaryDirectory(prefix="budget-review-") as folder:
            staging = Path(folder)
            candidate = {**doc, **{key: str(value) for key, value in paths.items()}}
            for key, value in changed.items():
                path = staging / (key + ".yaml")
                path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
                candidate[key] = str(path)
            audit_classification(paths["ledger"], cutover, Path(candidate["classifications"]))
            audit_ownership(paths["ledger"], cutover, paths["accounts"],
                            Path(candidate["ownership"]), paths["opening"])
        published_config = {**doc, **{key: relative_reference(path, output) for key, path in paths.items()},
                            **{key: key + ".yaml" for key in changed}}
    check_unchanged(before)
    check_unchanged(expected_inputs)
    publish_inputs(output, {"month.yaml": published_config,
                      **{key + ".yaml": value for key, value in changed.items()}})
    # Render from final paths, so any remaining diagnostics open real files.
    result = {"status": "DECISIONS_SAVED", "config": str(output / "month.yaml"), "accepted": accepted}
    return report_saved_inputs(result, audit_month_workflow, before, expected_inputs,
                               io_error="REVIEW_IO_ERROR")


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
    _, paths, _ = read_month_config(config)
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
    check_unchanged(inputs)
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
    check_unchanged(prepared.inputs)
    output = output.resolve()
    document = deepcopy(prepared.document)
    document["config"] = relative_reference(prepared.config, output)
    document["rows"][prepared.selected].update(decision=decision, reason=reason.strip(), approved=True)
    instructions = ("# TUI 审阅记录\n\n"
                    "本次只批准 decisions.yaml 中的一项。若 reviewed/month.yaml 已生成，"
                    "以后请使用该配置继续；原配置不会自动改写。\n\n" + prepared.instructions)
    publish_inputs(output, {"decisions.yaml": document, "choices.yaml": prepared.choices,
                      "other-items.yaml": prepared.other_items, "START_HERE.md": instructions})
    worksheet = output / "decisions.yaml"
    try:
        check_unchanged(prepared.inputs)
        return apply_review(worksheet)
    except (BudgetError, OSError, UnicodeError) as exc:
        # Publication is atomic. A failure in the following report must not
        # conceal decisions that were already successfully published.
        config = output / "reviewed" / "month.yaml"
        if config.is_file():
            return saved_report_failure({"status": "DECISIONS_SAVED", "config": str(config), "accepted": [
                {"kind": prepared.row["kind"], "source": prepared.row["source"], "decision": decision}]},
                exc, "REVIEW_IO_ERROR")
        raise

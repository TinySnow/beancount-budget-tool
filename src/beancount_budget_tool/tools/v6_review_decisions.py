"""Review pending categories and H purposes without hand-copying source references."""

from __future__ import annotations

import argparse
from datetime import date
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from beancount.core import data
import yaml

from ..console import configure_output
from ..errors import BudgetError, require
from ..exact import exact_text
from ..yamlstrict import load_yaml, yaml_path_location
from ..v6.classification import _key, _registry, audit_classification
from ..v6.facts import FactIndex
from ..v6.inline_amounts import parse_inline_amounts
from ..v6.monthly_report import REQUIRED, OPTIONAL, audit_month_workflow, render_workflow_text
from ..v6.ownership import _owners, audit_ownership
from .v6_check_migration_month import _month
from .v6_init import _inputs, _ledger_reference, _publish


EDITABLE = {"classification": ("classifications", "classifications", "category_id"),
            "asset_ownership": ("ownership", "claims", "owner_id")}
SCALARS = {"schema", "cutover", "month", "report_currency"}
ROW_FIELDS = {"kind", "ref", "date", "account", "commodity", "signed_units", "source",
              "payee", "narration", "decision", "reason", "approved"}


def _config(config: Path):
    doc = load_yaml(config)
    require(type(doc) is dict and REQUIRED <= set(doc) <= REQUIRED | OPTIONAL and
            type(doc["schema"]) is int and doc["schema"] == 6,
            "REVIEW_CONFIG_INVALID", "请使用完整的 v6 月报配置（可由首月初始化生成）", f"{config}:1")
    for key in set(doc) - {"schema"}:
        require(type(doc[key]) is str and bool(doc[key].strip()), "REVIEW_CONFIG_INVALID",
                "配置月份、日期和路径须为非空字符串", yaml_path_location(config, (key,)))
    try:
        _month(doc["month"])
        cutover = date.fromisoformat(doc["cutover"])
    except (BudgetError, ValueError) as exc:
        raise BudgetError("TIME_INVALID", "月份或 cutover 无效", str(config)) from exc
    paths = {key: (config.parent / value).resolve() for key, value in doc.items() if key not in SCALARS}
    for key, path in paths.items():
        require(path.is_file(), "REVIEW_CONFIG_INVALID", "配置引用的文件不存在",
                yaml_path_location(config, (key,)))
    return doc, paths, cutover


def _unchanged(before: dict[Path, bytes]):
    require(all(path.is_file() and path.read_bytes() == content for path, content in before.items()),
            "INPUT_CHANGED", "审阅期间配置或侧文件发生变化；请重跑")


def create_review(config: Path, output: Path) -> dict:
    config, output = config.resolve(), output.resolve()
    doc, paths, cutover = _config(config)
    before = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    with _inputs(paths["ledger"], doc["month"], cutover) as (fin, _):
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
        worksheet = {"schema": 6, "config": _ledger_reference(config, output),
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
            "```\nbeancount-budget-review --apply decisions.yaml\n```\n\n"
            "保存到新的 reviewed 目录，并自动重跑该月月报；原账本及原审阅文件只读。"
            "已有 reviewed 时用 --output 另选目录。后续以新目录的 month.yaml 继续审核。\n\n"
            "other-items.yaml 保留本轮不能通过这张表决定的项目，例如账户、期初、负债、转账协议、计价和目标。"
            "它们不会被批准或隐藏；本表只覆盖当前已发现的分类和 H 用途待审。"
            "保存后可能出现下一层待审，待审清零也须通过月报指标检查才能签发。\n")
    _unchanged(before)
    _publish(output, {"decisions.yaml": worksheet, "choices.yaml": choices,
                      "other-items.yaml": {"status": workflow["status"], "items": other},
                      "START_HERE.md": instructions})
    return {"status": "DRAFT_CREATED", "worksheet": str(output / "decisions.yaml"),
            "editable_items": len(rows), "other_items": len(other),
            "workflow_status": workflow["status"]}


def apply_review(worksheet: Path, output: Path | None = None) -> dict:
    worksheet = worksheet.resolve()
    output = (output or worksheet.parent / "reviewed").resolve()
    document = load_yaml(worksheet)
    require(type(document) is dict and set(document) == {"schema", "config", "month", "cutover", "rows"} and
            type(document["schema"]) is int and document["schema"] == 6 and
            type(document["config"]) is str and bool(document["config"].strip()) and
            type(document["rows"]) is list,
            "REVIEW_WORKSHEET_INVALID", "待审表字段或版本无效", f"{worksheet}:1")
    config = (worksheet.parent / document["config"]).resolve()
    doc, paths, cutover = _config(config)
    require(document["month"] == doc["month"] and document["cutover"] == doc["cutover"],
            "REVIEW_CONTEXT_CHANGED", "原配置月份或 cutover 已变；请重新导出待审表", str(worksheet))
    before = {path: path.read_bytes() for path in {worksheet, config, *paths.values()} if path != paths["ledger"]}
    classes, owners = load_yaml(paths["classifications"]), load_yaml(paths["ownership"])
    registries = {"classification": _registry(classes["categories"]),
                  "asset_ownership": _owners(owners["owners"])}
    changed = {"classifications": classes, "ownership": owners}
    accepted = []
    with _inputs(paths["ledger"], doc["month"], cutover) as (fin, _):
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
        published_config = {**doc, **{key: _ledger_reference(path, output) for key, path in paths.items()},
                            **{key: key + ".yaml" for key in changed}}
    _unchanged(before)
    _publish(output, {"month.yaml": published_config,
                      **{key + ".yaml": value for key, value in changed.items()}})
    # Render from final paths, so any remaining diagnostics open real files.
    result = {"status": "DECISIONS_SAVED", "config": str(output / "month.yaml"), "accepted": accepted}
    try:
        return {**result, "workflow": audit_month_workflow(output / "month.yaml")}
    except BudgetError as exc:
        # Valid source decisions remain available if the next report stage
        # uncovers a separate plan/configuration error.
        return {**result, "workflow": None,
                "report_error": {"code": exc.code, "message": exc.message, "source": exc.source}}


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--config", type=Path, help="导出该月分类与 H 资金用途待审表")
    action.add_argument("--apply", type=Path, help="保存已明确批准的待审表决定并重跑月报")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if args.config and args.output is None:
        parser.error("导出待审表须提供 --output 新目录")
    try:
        result = apply_review(args.apply, args.output) if args.apply else create_review(args.config, args.output)
    except (BudgetError, OSError, UnicodeError) as exc:
        error = {"code": exc.code, "message": exc.message, "source": exc.source} if isinstance(exc, BudgetError) else {
            "code": "REVIEW_IO_ERROR", "message": str(exc), "source": str(args.apply or args.config)}
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "worksheet" in result:
        print(f"待审填写表：{result['worksheet']}\n可填分类／用途 {result['editable_items']} 项；"
              f"其他待审 {result['other_items']} 项。请查看同目录 START_HERE.md。")
    else:
        print(f"保存 {len(result['accepted'])} 项决定；配置：{result['config'] or '未生成（没有明确批准项）'}")
        if result.get("report_error"):
            error = result["report_error"]
            print(f"决定已保存，月报尚未通过：{error['code']} {error['message']} ({error['source']})")
        else:
            print(render_workflow_text(result["workflow"]))
    return 2 if result.get("report_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

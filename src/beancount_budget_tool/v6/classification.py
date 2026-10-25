"""Exact, source-bound v6 classification audit for Expenses/Income postings.

This is a claim compiler, not a net-expense report.  Historical valuation,
economic-benefit deduplication and budget targets remain separate proofs.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

from beancount.core import data

from ..errors import BudgetError, require
from ..exact import exact, exact_text
from ..financial import load_financial, shared_audit
from ..yamlstrict import load_yaml, yaml_path_location, require_yaml
from .facts import FactIndex, future_posting_refs
from .inline_amounts import parse_inline_amounts
from .source import _source, scan_v6_source


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")


def _key(ref: dict) -> str:
    return json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _name(value: object) -> str:
    require(type(value) is str, "CATEGORY_NAME_INVALID", "类别名称必须是字符串")
    name = unicodedata.normalize("NFC", value).strip()
    require(bool(name) and name != "-" and
            all(part and part == part.strip() for part in name.split(".")) and
            not any(char in "|｜,，;；" or unicodedata.category(char).startswith("C") for char in name),
            "CATEGORY_NAME_INVALID", "类别名称或层级段非法", value)
    return name


def _registry(records: object, source: Path | None = None) -> dict[str, str]:
    def check(condition, code, message, keys=(), fallback=None):
        require_yaml(condition, code, message, source, ("categories", *keys), fallback)

    check(type(records) is list, "CATEGORY_REGISTRY_INVALID", "categories 必须是列表")
    names: dict[str, str] = {}
    ids: set[str] = set()
    parents: dict[str, str | None] = {}
    locations = {}
    for index, row in enumerate(records):
        check(type(row) is dict and set(row) == {"id", "name", "parent", "aliases"},
                "CATEGORY_REGISTRY_INVALID", "类别需包含 id/name/parent/aliases", (index,))
        ident, parent = row["id"], row["parent"]
        check(type(ident) is str and bool(ID.fullmatch(ident)) and ident not in ids,
                "CATEGORY_ID_INVALID", "类别 ID 无效或重复", (index, "id"), str(ident))
        check(parent is None or type(parent) is str, "CATEGORY_REGISTRY_INVALID",
                "类别 parent 须为空或稳定 ID", (index, "parent"), ident)
        check(type(row["aliases"]) is list, "CATEGORY_REGISTRY_INVALID",
                "aliases 必须是列表", (index, "aliases"), ident)
        ids.add(ident)
        parents[ident] = parent
        locations[ident] = index
        for alias_index, raw in enumerate([row["name"], *row["aliases"]]):
            keys = (index, "name") if alias_index == 0 else (index, "aliases", alias_index - 1)
            try:
                name = _name(raw)
            except BudgetError as exc:
                check(False, exc.code, exc.message, keys, exc.source)
            check(name not in names, "CATEGORY_NAME_DUPLICATE", "类别名称或别名重复", keys, name)
            names[name] = ident
    for ident in ids:
        seen = {ident}
        child = ident
        parent = parents[ident]
        while parent is not None:
            check(parent in ids, "CATEGORY_PARENT_INVALID", "类别父节点不存在",
                  (locations[child], "parent"), child)
            check(parent not in seen, "CATEGORY_PARENT_INVALID", "类别层级成环",
                  (locations[child], "parent"), child)
            seen.add(parent)
            child = parent
            parent = parents[parent]
    return names


def _units(value: object, source: str) -> Fraction:
    require(type(value) is str and bool(NUMBER.fullmatch(value)),
            "CLASSIFICATION_UNITS_INVALID", "切片数量须为精确数字字符串", source)
    return Fraction(value)


@shared_audit
def audit_classification(root: Path, cutover: date, declarations: Path | None = None) -> dict:
    """Validate complete per-posting classification or report exact gaps.

    Inline ``budget`` and external slices are mutually exclusive even when
    they happen to express the same category and amount.  External claims use
    full booked source identities, so unrelated ledger insertions do not
    invalidate their references.
    """
    scan_v6_source(root, cutover)
    fin = load_financial(root)
    facts = FactIndex(fin)
    expense_root = fin.options["name_expenses"]
    income_root = fin.options["name_income"]
    future_keys = {_key(ref) for posting, ref in future_posting_refs(fin, cutover)
                   if posting.account.split(":", 1)[0] in (expense_root, income_root)}
    candidates: dict[str, tuple[data.Posting, dict]] = {}
    order: list[str] = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        for posting in entry.postings:
            kind = posting.account.split(":", 1)[0]
            if kind not in (expense_root, income_root):
                continue
            ref = facts.for_posting(posting).as_dict()
            key = _key(ref)
            require(key not in candidates, "SOURCE_REF_AMBIGUOUS",
                    "多个费用／收入 posting 具有相同来源身份")
            candidates[key] = (posting, ref)
            order.append(key)

    registry: dict[str, str] = {}
    claims: dict[str, dict] = {}
    defaults = None
    if declarations is not None:
        document = load_yaml(declarations)
        required = {"schema", "cutover", "loader_version", "categories", "classifications"}
        unknown = set(document) - required - {"defaults"} if type(document) is dict else set()
        require_yaml(type(document) is dict and required <= set(document) and not unknown,
            "CLASSIFICATION_DECLARATION_INVALID", "分类声明字段不完整或含未知字段", declarations,
            (sorted(unknown, key=str)[0],) if unknown else ())
        for field, valid in (
            ("schema", type(document["schema"]) is int and document["schema"] == 6),
            ("cutover", type(document["cutover"]) is str and document["cutover"] == cutover.isoformat()),
            ("loader_version", document["loader_version"] == fin.loader_version),
        ):
            require_yaml(valid, "CLASSIFICATION_DECLARATION_INVALID", "分类声明版本、cutover 或 loader 版本不匹配",
                         declarations, (field,))
        registry = _registry(document["categories"], source=declarations)
        if "defaults" in document:
            defaults = document["defaults"]
            require(type(defaults) is dict and set(defaults) == {
                "income_category_id", "expense_category_id"} and
                all(type(category) is str and category in registry.values()
                    for category in defaults.values()),
                "CLASSIFICATION_DEFAULT_INVALID", "默认收入／支出类别须引用已登记类别",
                yaml_path_location(declarations, ("defaults",)))
        rows = document["classifications"]
        require_yaml(type(rows) is list, "CLASSIFICATION_DECLARATION_INVALID",
                     "classifications 必须是列表", declarations, ("classifications",))
        seen_ids: set[str] = set()
        for row_index, row in enumerate(rows):
            require_yaml(type(row) is dict and set(row) == {"claim", "slices", "reason"},
                    "CLASSIFICATION_CLAIM_INVALID", "分类认领须包含 claim/slices/reason", declarations,
                    ("classifications", row_index))
            require_yaml(type(row["claim"]) is dict, "CLAIM_REF_INVALID", "claim 必须是完整 booked 引用",
                         declarations, ("classifications", row_index, "claim"))
            key = _key(row["claim"])
            if key in future_keys:
                continue  # Retain the decision for its month, not this report.
            require(key in candidates, "CLAIM_REF_INVALID", "分类引用不存在、过期或不属费用／收入",
                    yaml_path_location(declarations, ("classifications", row_index, "claim")))
            require(key not in claims, "CLASSIFICATION_DOUBLE_CLAIM", "同一 posting 重复分类", key)
            posting, ref = candidates[key]
            require("budget" not in (posting.meta or {}), "CLASSIFICATION_DOUBLE_CLAIM",
                    "行内 budget 与外部分类不能同时认领", key)
            require(type(row["reason"]) is str and bool(row["reason"].strip()),
                    "CLASSIFICATION_REASON_MISSING", "外部分类须有审阅理由", key)
            parts = row["slices"]
            require(type(parts) is list and bool(parts), "CLASSIFICATION_CLAIM_INVALID",
                    "分类须有至少一个切片", key)
            parsed = []
            total = Fraction(0)
            original = exact(posting.units.number)
            for part in parts:
                require(type(part) is dict and set(part) == {"id", "category_id", "signed_units"},
                        "CLASSIFICATION_CLAIM_INVALID", "切片须含 id/category_id/signed_units", key)
                ident, category = part["id"], part["category_id"]
                require(type(ident) is str and bool(ID.fullmatch(ident)) and ident not in seen_ids,
                        "CLASSIFICATION_SLICE_ID_INVALID", "切片 ID 为空或重复", key)
                require(type(category) is str and (category == "-" or category in registry.values()),
                        "CATEGORY_UNKNOWN", "切片类别未登记", str(category))
                seen_ids.add(ident)
                amount = _units(part["signed_units"], key)
                require((amount == 0 if original == 0 else amount * original > 0),
                        "CLASSIFICATION_UNITS_MISMATCH", "切片与原 posting 符号不一致", key)
                total += amount
                parsed.append({"id": ident, "category_id": category,
                               "signed_units": exact_text(amount)})
            require(total == original, "CLASSIFICATION_UNITS_MISMATCH",
                    "分类切片数量没有精确加回原 posting", key)
            claims[key] = {"ref": ref, "account": posting.account, "commodity": posting.units.currency,
                           "origin": "external", "review_reason": row["reason"], "slices": parsed}

    missing = []
    compiled = []
    for key in order:
        posting, ref = candidates[key]
        if key in claims:
            compiled.append(claims[key])
            continue
        inline = (posting.meta or {}).get("budget")
        if inline is None:
            kind = posting.account.split(":", 1)[0]
            field = ("income_category_id" if kind == income_root else
                     "expense_category_id")
            if defaults is not None:
                compiled.append({"ref": ref, "account": posting.account,
                                 "commodity": posting.units.currency, "origin": "default",
                                 "slices": [{"id": "default:" + sha256(key.encode()).hexdigest(),
                                             "category_id": defaults[field],
                                             "signed_units": exact_text(exact(posting.units.number))}]})
                continue
            missing.append({"ref": ref, "account": posting.account,
                            "signed_units": exact_text(exact(posting.units.number)),
                            "commodity": posting.units.currency})
            continue
        source = _source(posting.meta or {})
        parts = parse_inline_amounts(inline, exact(posting.units.number), source,
                                     known_names=set(registry), code="CLASSIFICATION_UNITS_MISMATCH")
        slice_id = "inline:" + sha256(key.encode()).hexdigest()
        slices = []
        for index, (name, amount) in enumerate(parts):
            category = "-" if name == "-" else registry.get(_name(name))
            require(category is not None, "CATEGORY_UNKNOWN", f"行内 budget 类别未登记：{name}", source)
            slices.append({"id": slice_id if len(parts) == 1 else f"{slice_id}:{index}",
                           "category_id": category, "signed_units": exact_text(amount)})
        compiled.append({"ref": ref, "account": posting.account,
                         "commodity": posting.units.currency, "origin": "inline",
                         "slices": slices})
    return {"mode": "v6_classification_audit_only", "cutover": cutover.isoformat(),
            "ledger_snapshot": fin.digest, "loader_version": fin.loader_version,
            "review_sha256": sha256(declarations.read_bytes()).hexdigest() if declarations else None,
            "review_state": "complete" if not missing else "pending",
            "postings": len(order), "claimed_postings": len(compiled),
            "missing_classifications": missing, "classifications": compiled,
            "note": "分类守恒不证明历史计价、经济利益去重或已认证净费用"}

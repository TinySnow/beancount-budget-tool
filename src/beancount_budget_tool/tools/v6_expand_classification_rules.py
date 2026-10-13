"""Expand approved private account rules into source-bound v6 classifications.

The public expander contains no account-specific defaults.  A rule file must
name exact accounts or explicit expense/income defaults, date ranges and
registered categories; every generated claim is then checked by the normal
v6 classification auditor.
"""

from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import yaml
from beancount.core import data

from beancount_budget_tool.errors import BudgetError, require
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.classification import _key, _registry, audit_classification
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.yamlstrict import load_yaml


def _day(value: object, field: str) -> date:
    require(type(value) is str, "PRIVATE_RULE_INVALID", f"{field} 须为带引号的 ISO 日期")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BudgetError("PRIVATE_RULE_INVALID", f"{field} 不是有效 ISO 日期：{value}") from exc


def expand(root: Path, cutover: date, rules_path: Path,
           existing: Path | None = None) -> tuple[dict, dict]:
    fin = load_financial(root)
    document = load_yaml(rules_path)
    required = {"schema", "cutover", "ledger_snapshot", "loader_version", "categories", "rules"}
    require(type(document) is dict and required <= set(document) and
            set(document) <= required | {"expense_default", "income_default"},
        "PRIVATE_RULE_INVALID", "私人分类规则字段不完整或含未知字段", str(rules_path))
    require(type(document["schema"]) is int and document["schema"] == 6 and
            document["cutover"] == cutover.isoformat() and
            type(document["ledger_snapshot"]) is str and
            bool(re.fullmatch(r"[0-9a-f]{64}", document["ledger_snapshot"])) and
            document["loader_version"] == fin.loader_version,
            "PRIVATE_RULE_STALE", "私人规则的原审快照格式、cutover 或 loader 版本不匹配", str(rules_path))
    names = _registry(document["categories"])
    valid_categories = set(names.values())
    raw_rules = document["rules"]
    require(type(raw_rules) is list and (bool(raw_rules) or
            "expense_default" in document or "income_default" in document),
            "PRIVATE_RULE_INVALID", "rules 须是列表；无规则时须有显式默认", str(rules_path))
    rules = []
    ids = set()
    for row in raw_rules:
        require(type(row) is dict and set(row) == {
            "id", "account", "first_date", "last_date", "category_id", "reason"},
            "PRIVATE_RULE_INVALID", "规则须含 id/account/日期区间/category_id/reason", str(rules_path))
        ident, account, category = row["id"], row["account"], row["category_id"]
        require(type(ident) is str and bool(ident.strip()) and ident not in ids and
                type(account) is str and bool(account.strip()) and
                type(category) is str and (category == "-" or category in valid_categories) and
                type(row["reason"]) is str and bool(row["reason"].strip()),
                "PRIVATE_RULE_INVALID", "规则 ID、账户、类别或审阅理由无效", str(row))
        ids.add(ident)
        first, last = _day(row["first_date"], "first_date"), _day(row["last_date"], "last_date")
        require(cutover <= first <= last, "PRIVATE_RULE_INVALID",
                "规则日期区间须在 cutover 之后且非空", ident)
        rules.append((row, first, last))

    defaults = {}
    for field in ("expense_default", "income_default"):
        if field not in document:
            continue
        default = document[field]
        required_default = {"category_id", "first_date", "last_date", "reason"}
        require(type(default) is dict and required_default <= set(default) <=
                required_default | {"exclude_accounts", "exclude_account_prefixes"},
            "PRIVATE_RULE_INVALID", f"{field} 须含类别、有效日期和理由", str(rules_path))
        require(type(default["category_id"]) is str and
                default["category_id"] in valid_categories and
                type(default["reason"]) is str and bool(default["reason"].strip()),
                "PRIVATE_RULE_INVALID", f"{field} 类别或理由无效", str(rules_path))
        first = _day(default["first_date"], f"{field}.first_date")
        last = _day(default["last_date"], f"{field}.last_date")
        require(cutover <= first <= last,
                "PRIVATE_RULE_INVALID", f"{field} 有效期须在 cutover 之后", str(rules_path))
        excluded = default.get("exclude_accounts", [])
        excluded_prefixes = default.get("exclude_account_prefixes", [])
        for label, values in (("exclude_accounts", excluded),
                              ("exclude_account_prefixes", excluded_prefixes)):
            require(type(values) is list and
                    all(type(account) is str and bool(account.strip()) and
                        account == account.strip() for account in values) and
                    len(values) == len(set(values)),
                    "PRIVATE_RULE_INVALID", f"{field}.{label} 须为不重复的账户名列表",
                    str(rules_path))
        defaults[field] = (default, first, last, set(excluded), tuple(excluded_prefixes))

    preserved = []
    if existing is not None:
        reviewed = load_yaml(existing)
        require(type(reviewed) is dict and reviewed.get("categories") == document["categories"],
                "PRIVATE_RULE_CATEGORIES_CHANGED", "现有分类声明与私人规则的类别名册不一致", str(existing))
        audit = audit_classification(root, cutover, existing)
        preserved = reviewed["classifications"]
    else:
        with TemporaryDirectory() as folder:
            registry_path = Path(folder) / "categories.yaml"
            registry_path.write_text(yaml.safe_dump({
                "schema": 6, "cutover": cutover.isoformat(),
                "loader_version": fin.loader_version,
                "categories": document["categories"], "classifications": []},
                allow_unicode=True, sort_keys=False), encoding="utf-8")
            audit = audit_classification(root, cutover, registry_path)
    options = fin.options
    expense_root, income_root = options["name_expenses"], options["name_income"]
    missing_by_key = {_key(item["ref"]): item for item in audit["missing_classifications"]}
    facts = FactIndex(fin)
    candidates = [{**missing_by_key[key], "date": entry.date.isoformat()}
                  for entry in fin.entries if isinstance(entry, data.Transaction)
                  for posting in entry.postings
                  if (key := _key(facts.for_posting(posting).as_dict())) in missing_by_key]
    preserved_keys = {_key(row["claim"]) for row in preserved}
    claims = list(preserved)
    matches = {row["id"]: 0 for row, _, _ in rules}
    default_matches = {"expense_default": 0, "income_default": 0}
    for item in candidates:
        if _key(item["ref"]) in preserved_keys:
            continue
        day = date.fromisoformat(item["date"])
        eligible = [row for row, first, last in rules
                    if row["account"] == item["account"] and first <= day <= last]
        require(len(eligible) <= 1, "PRIVATE_RULE_OVERLAP",
                "同一 booked posting 被多条私人规则覆盖", _key(item["ref"]))
        if eligible:
            rule = eligible[0]
            matches[rule["id"]] += 1
            category = rule["category_id"]
            reason = f"私人规则 {rule['id']}：{rule['reason']}"
        else:
            root_kind = item["account"].split(":", 1)[0]
            field = ("expense_default" if root_kind == expense_root else
                     "income_default" if root_kind == income_root else None)
            selected = defaults.get(field)
            if selected is None:
                continue
            default, first, last, excluded, excluded_prefixes = selected
            if (not first <= day <= last or item["account"] in excluded or
                    any(item["account"] == prefix or
                        item["account"].startswith(prefix + ":")
                        for prefix in excluded_prefixes)):
                continue
            default_matches[field] += 1
            category = default["category_id"]
            label = "费用" if field == "expense_default" else "收入"
            reason = f"私人{label}默认规则：{default['reason']}"
        digest = sha256(_key(item["ref"]).encode()).hexdigest()
        claims.append({
            "claim": item["ref"],
            "reason": reason,
            "slices": [{"id": f"rule_{digest}", "category_id": category,
                        "signed_units": item["signed_units"]}],
        })
    output = {"schema": 6, "cutover": cutover.isoformat(),
              "loader_version": audit["loader_version"],
              "categories": document["categories"], "classifications": claims}
    return output, {"matched_rules": matches,
                    "expense_default_matches": default_matches["expense_default"],
                    "income_default_matches": default_matches["income_default"],
                    "preserved_claims": len(preserved),
                    "generated_claims": len(claims) - len(preserved),
                    "still_unreviewed": len(candidates) - (len(claims) - len(preserved)),
                    "ledger_snapshot": audit["ledger_snapshot"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--cutover", required=True, type=date.fromisoformat)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--existing", type=Path,
                        help="已审分类侧文件；逐条重验旧来源，仅为新增待审来源展开规则")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"输出文件已存在，拒绝覆盖：{args.output}")
    output, summary = expand(args.ledger, args.cutover, args.rules, args.existing)
    args.output.write_text(yaml.safe_dump(output, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    try:
        checked = audit_classification(args.ledger, args.cutover, args.output)
        require(checked["claimed_postings"] >=
                summary["preserved_claims"] + summary["generated_claims"] and
                len(checked["missing_classifications"]) == summary["still_unreviewed"],
                "PRIVATE_RULE_INVALID", "展开后分类审计数量不一致", str(args.output))
    except Exception:
        args.output.unlink()
        raise
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

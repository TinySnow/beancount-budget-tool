"""Two editable files in front of the existing evidence-based audit engine.

Every decision comes from the ledger and the two public YAML files.
Generated documents are temporary adapters, never persisted review state.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory, mkdtemp, gettempdir
from uuid import uuid4

import yaml
from beancount.core import data

from ..runtime_inputs import input_bytes, input_text
from ..errors import BudgetError, require
from ..financial import financial_session, load_financial
from ..runtime_inputs import register_documents
from .facts import frozen_fact_indices
from ..console import configure_output
from ..yamlstrict import config_path, load_yaml, require_yaml, yaml_path_location
from .input_compiler import (
    draft_inputs, compile_inputs, SetupDumper, relative_reference,
)
from .targets import _month


GOALS = {"expense_targets", "expense_direct_targets", "allocation_targets"}
SCALARS = {"schema", "ledger", "cutover", "month", "report_currency"}


def is_simple(document: object) -> bool:
    return type(document) is dict and type(document.get("schema")) is int and document["schema"] == 1 and "budgets" in document


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(value, Dumper=SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _unchanged(inputs: dict):
    require(all(not path.exists() if content is None else path.is_file() and input_bytes(path) == content
                for path, content in inputs.items()),
            "INPUT_CHANGED", "配置、预算或审阅记录在计算期间改变；请重算")


def _grouped_accounts(doc: dict, config: Path, commodities: dict[str, set[str]]) -> tuple[dict, dict]:
    """Expand explicit account lists; never classify by account name or prefix."""
    groups = doc.get("account_groups", {})
    require_yaml(type(groups) is dict and set(groups) <= {"H", "K"},
                 "SIMPLE_ACCOUNT_INVALID", "account_groups 只接受 H 和 K", config, ("account_groups",))
    h = groups.get("H", {})
    require_yaml(type(h) is dict and set(h) <= {"payable", "excluded"},
                 "SIMPLE_ACCOUNT_INVALID", "H 按 payable／excluded 列出完整账户名", config, ("account_groups", "H"))
    accounts, locations = {}, {}
    for semantics, scope, names, keys in [
        ("H", scope, names, ("account_groups", "H", scope)) for scope, names in h.items()
    ] + [("K", "excluded", groups.get("K", []), ("account_groups", "K"))]:
        require_yaml(type(names) is list, "SIMPLE_ACCOUNT_INVALID", "账户组须为完整账户名列表", config, keys)
        for index, name in enumerate(names):
            require_yaml(type(name) is str and bool(name) and name == name.strip(),
                         "SIMPLE_ACCOUNT_INVALID", "账户名须为非空字符串", config, (*keys, index))
            require_yaml(name not in accounts, "SIMPLE_ACCOUNT_DUPLICATE", "账户不能重复列入多个组", config, (*keys, index))
            actual = commodities.get(name, set())
            # Excluding an entire H account covers its actual holdings,
            # including new securities, without guessing from commodity names.
            # A payable group approves only existing report-currency cash.
            payment = ({commodity: "excluded" for commodity in sorted(actual)}
                       if semantics == "H" and scope == "excluded" else
                       {doc["report_currency"]: scope} if doc["report_currency"] in actual else {})
            accounts[name] = {"semantics": semantics, "payment_scope": payment}
            locations[name] = (*keys, index)
    for name, settings in doc.get("accounts", {}).items():
        require_yaml(type(settings) is dict, "SIMPLE_ACCOUNT_INVALID", "账户设置须为映射", config, ("accounts", name))
        # Explicit per-account settings are exceptions, including opening owners.
        require_yaml(type(settings.get("payment_scope", {})) is dict, "SIMPLE_ACCOUNT_INVALID",
                     "payment_scope 须为币种映射", config, ("accounts", name, "payment_scope"))
        base = accounts.get(name, {})
        accounts[name] = {**base, **settings,
                          "payment_scope": {**base.get("payment_scope", {}), **settings.get("payment_scope", {})}}
        locations[name] = ("accounts", name)
    return accounts, locations


def _documents(config: Path, inputs: dict | None = None):
    inputs = {} if inputs is None else inputs
    inputs.setdefault(config, input_bytes(config))
    doc = load_yaml(config)
    fields = {"schema", "ledger", "budgets", "cutover", "month", "report_currency", "defaults"}
    require_yaml(is_simple(doc) and fields <= set(doc) <= fields | {"accounts", "account_groups", "account_defaults", "spendable_cap_category", "classification_defaults", "expense_budget_basis", "liabilities", "liability_budget_mode"},
                 "SIMPLE_CONFIG_INVALID", "config.yml 须含 schema: 1、ledger、budgets、cutover、month、report_currency、defaults，以及 accounts 或 account_groups", config)
    for key in ("ledger", "budgets", "cutover", "month", "report_currency"):
        require_yaml(type(doc[key]) is str and bool(doc[key].strip()), "SIMPLE_CONFIG_INVALID",
                     f"{key} 须为非空字符串", config, (key,))
    require_yaml(type(doc.get("accounts", {})) is dict, "SIMPLE_CONFIG_INVALID", "accounts 须为账户名映射", config, ("accounts",))
    require_yaml("accounts" in doc or "account_groups" in doc, "SIMPLE_CONFIG_INVALID",
                 "请用 account_groups 分组，或用 accounts 列出账户", config)
    defaults = doc["defaults"]
    require_yaml(doc.get("liability_budget_mode", "purpose") == "purpose",
                 "LIABILITY_MODE_REMOVED", "只保留逐笔 budget 责任用途模式；请移除旧 classification 设置并迁移旧标签", config, ("liability_budget_mode",))
    require_yaml(type(defaults) is dict and {"income_purpose", "expense_purpose", "expense_category"} <= set(defaults) <=
                 {"income_purpose", "expense_purpose", "expense_category", "income_category"},
                 "SIMPLE_CONFIG_INVALID", "defaults 须指定默认收入用途、支出用途和费用类别", config, ("defaults",))
    require_yaml("classification_defaults" not in doc or type(doc["classification_defaults"]) is bool,
                 "SIMPLE_CONFIG_INVALID", "classification_defaults 须为 true 或 false", config, ("classification_defaults",))
    require_yaml(doc.get("expense_budget_basis", "calendar_month") in ("calendar_month", "salary_cycle"),
                 "SIMPLE_CONFIG_INVALID", "expense_budget_basis 须为 calendar_month 或 salary_cycle", config, ("expense_budget_basis",))
    require_yaml(doc.get("expense_budget_basis") != "salary_cycle" or doc.get("spendable_cap_category", defaults["expense_category"]) is not None,
                 "SIMPLE_CONFIG_INVALID", "工资周期上限须指定消费类别，不能同时关闭 spendable_cap_category", config, ("spendable_cap_category",))
    from .classification import _name
    from .ownership import _owners
    for key, value in defaults.items():
        try:
            if key.endswith("category"):
                require(value == _name(value), "CATEGORY_NAME_INVALID", "默认类别名称须规范且无首尾空白")
            else:
                _owners([{"id": "default", "name": value, "aliases": []}])
        except BudgetError as exc:
            raise BudgetError(exc.code, exc.message, yaml_path_location(config, ("defaults", key))) from exc
    budgets = config_path(config, doc["budgets"], "budgets")
    inputs.setdefault(budgets, input_bytes(budgets))
    plans = load_yaml(budgets)
    require_yaml(type(plans) is dict and type(plans.get("schema")) is int and plans["schema"] == 1 and
                 set(plans) <= {"schema", "purposes", "defaults", "effective_templates", "months", "salary_cycles"},
                 "SIMPLE_BUDGETS_INVALID", "budgets.yml 须为 schema: 1 的目标配置", budgets)
    require_yaml(type(plans.get("purposes", [])) is list and all(type(name) is str and name.strip() for name in plans.get("purposes", [])),
                 "SIMPLE_BUDGETS_INVALID", "purposes 须为用途名称列表（也可从 fund 自动发现）", budgets, ("purposes",))
    _unchanged(inputs)
    return doc, budgets, plans


def _layer(plans: dict, month: str, source: Path):
    merged = {key: {} for key in GOALS}
    locations = {}
    raw_tree = None
    def decimal_value(keys):
        # PyYAML normally converts an unquoted decimal to a binary float.
        # Read its scalar spelling instead, preserving the user's exact goal.
        nonlocal raw_tree
        if raw_tree is None:
            raw_tree = yaml.compose(input_text(source))
        node = raw_tree
        for key in keys:
            node = next(value for name, value in node.value if name.value == str(key))
        try:
            return format(Decimal(node.value.replace("_", "")), "f")
        except InvalidOperation:
            return node.value  # The ordinary amount validator will reject it.
    def apply(value, keys, *, monthly=False):
        require_yaml(type(value) is dict and set(value) <= GOALS | ({"completeness_confirmed"} if monthly else set()),
                     "SIMPLE_BUDGETS_INVALID", "预算层字段无效", source, keys)
        for key in GOALS & set(value):
            require_yaml(type(value[key]) is dict, "SIMPLE_BUDGETS_INVALID", "目标须为中文名称到金额的映射", source, (*keys, key))
            require_yaml(all(type(name) is str and name.strip() for name in value[key]),
                         "SIMPLE_BUDGETS_INVALID", "目标名称须为非空中文名称或别名", source, (*keys, key))
            merged[key].update({name: decimal_value((*keys, key, name)) if type(amount) is float else amount
                                for name, amount in value[key].items()})
            for name in value[key]:
                locations[(key, name)] = (*keys, key, name)
        if "completeness_confirmed" in value:
            require_yaml(type(value["completeness_confirmed"]) is bool, "SIMPLE_BUDGETS_INVALID",
                         "completeness_confirmed 须为 true 或 false", source, (*keys, "completeness_confirmed"))
            merged["completeness_confirmed"] = value["completeness_confirmed"]
    apply(plans.get("defaults", {}), ("defaults",))
    for field in ("effective_templates", "months"):
        rows = plans.get(field, {})
        require_yaml(type(rows) is dict, "SIMPLE_BUDGETS_INVALID", f"{field} 须为月份映射", source, (field,))
        for key in rows:
            try:
                _month(key)
            except BudgetError as exc:
                raise BudgetError(exc.code,
                    f"{field} 只接受自然月键，如 '2023-07'；完整发薪日期 YYYY-MM-DD 请放在顶层 salary_cycles 中。"
                    f"当前键：{key!s}", yaml_path_location(source, (field, str(key)))) from exc
        for key in sorted(rows):
            if field == "effective_templates" and key <= month or field == "months" and key == month:
                apply(rows[key], (field, key), monthly=field == "months")
    return merged, locations


def _registry(current, prefix, *, categories=False):
    rows, names, used = [], {}, set()
    mapping = {}
    for row in current:
        name = row["name"]
        if name not in names:
            ident = prefix + "_" + sha256(name.encode("utf-8")).hexdigest()[:20]
            require(ident not in used, "SIMPLE_REGISTRY_CONFLICT", "自动用途／类别 ID 冲突")
            names[name] = ident
            used.add(ident)
            rows.append({**row, "id": ident})
        mapping[row["id"]] = names[name]
    if categories:
        for row in rows:
            row["parent"] = mapping.get(row.get("parent"))
    return rows, mapping, names


@dataclass
class RuntimeFiles:
    name: str = field(default_factory=lambda: mkdtemp(prefix="budget-runtime-"))
    keep: bool = False

    def cleanup(self):
        if not self.keep:
            shutil.rmtree(self.name, ignore_errors=True)


@dataclass
class Runtime:
    source: Path
    month: str
    inputs: dict
    config: Path | None = None
    pending: dict | None = None
    budgets: Path | None = None
    temporary: RuntimeFiles | None = field(default=None, repr=False)

    def close(self):
        if self.temporary is not None:
            temporary, self.temporary = self.temporary, None
            temporary.cleanup()

    def __del__(self):
        self.close()

    def check(self):
        _unchanged(self.inputs)

    def public_sources(self, outcome):
        """Internal file locations should lead to the two editable inputs."""
        result = deepcopy(outcome)
        return self.rewrite_sources(result)

    def rewrite_sources(self, result):
        """Apply this runtime's public names to an already owned result tree."""
        if self.config:
            for row in result.get("months", []):
                if (row.get("config") or "").startswith(str(self.config.parent)):
                    row["config"] = str(self.source)
            for item in result.get("review_items", []):
                if (item.get("config") or "").startswith(str(self.config.parent)):
                    item["config"] = str(self.source)
                if item.get("reason_code") == "EXTERNAL_SCOPE_PENDING":
                    item["detail"] = "核对本月末账单后，在 budgets.yml 对应月份填写 completeness_confirmed: true 并重算；下一月须单独配置。"
                    item["source"] = yaml_path_location(self.budgets, ("months", item.get("month", result.get("month", self.month)), "completeness_confirmed"))
                    continue
                source = item.get("source") or ""
                if source.startswith(str(self.config.parent)):
                    target = self.budgets if any(key in source for key in ("expense_plans", "purpose_plans", "salary_cycle")) else self.source
                    item["source"] = yaml_path_location(target, ())
        return result


def build_runtime(config: Path, month: str | None = None, *, materialize: bool = True) -> Runtime:
    config = config.resolve()
    inputs = {config: input_bytes(config)}
    doc, budgets, plans = _documents(config, inputs)
    month = _month(month or doc["month"])
    goals, locations = _layer(plans, month, budgets)
    from .salary_plan import read_cycles
    cycles = read_cycles(plans, budgets, default_category=doc.get("spendable_cap_category", doc["defaults"]["expense_category"]))
    # Never inspect or load .budget-state: identical ledger + YAML inputs must
    # give identical decisions even if a legacy review file exists nearby.
    runtime = Runtime(config, month, inputs, budgets=budgets)
    try:
        cutover = date.fromisoformat(doc["cutover"])
    except ValueError as exc:
        raise BudgetError("CUTOVER_INVALID", "cutover 须为 YYYY-MM-DD", yaml_path_location(config, ("cutover",))) from exc
    root = config_path(config, doc["ledger"], "ledger")
    unresolved_opening = set()
    filled, _ = draft_inputs(root, month, cutover=cutover, currency=doc["report_currency"],
                              **{key: doc["defaults"][key] for key in
                                 ("income_purpose", "expense_purpose", "expense_category")})
    filled["defaults"]["confirmed"] = True
    # This flag only lets the input compiler run. The final completeness
    # declaration is removed unless the user actually confirmed this month.
    filled["completeness_confirmed"] = True
    from .liability_rules import validate_rules, liability_budget
    liability_rules = deepcopy(doc.get("liabilities", {}))
    # Validate before reading names out of an arbitrary YAML value.
    require_yaml(type(liability_rules) is dict and all(type(row) is dict for row in liability_rules.values()),
                 "LIABILITY_RULE_INVALID", "liabilities 须为负债账户到规则的映射", config, ("liabilities",))
    fin = load_financial(root)
    budget_purposes = set()
    for entry in fin.entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                if posting.account.split(":", 1)[0] == fin.options["name_liabilities"]:
                    liability_rules.setdefault(posting.account, {"role": "payable"})
                    if entry.date < cutover:
                        continue
                    try:
                        labels = liability_budget(posting)
                    except BudgetError:
                        # Name discovery is not approval: source preflight
                        # reports malformed labels with their original line.
                        labels = []
                    budget_purposes.update(part["name"] for part in labels or [] if part["name"] != "-")
    validate_rules(liability_rules, set(), config, public=True, liability_root=fin.options['name_liabilities'])
    owners = set(filled["allocation_targets"]) | set(plans.get("purposes", [])) | budget_purposes
    filled["allocation_targets"] = {name: goals["allocation_targets"].get(name, "not_assessed")
                                     for name in owners | set(goals["allocation_targets"])}
    declared_expenses = set(goals["expense_targets"]) | set(goals["expense_direct_targets"])
    expense = {name: "not_assessed" for name in filled["expense_targets"]
               if not any(child.startswith(name + ".") for child in declared_expenses)}
    expense.update(goals["expense_direct_targets"])
    expense.update(goals["expense_targets"])
    # Assembly only needs a complete set of registry names; actual goal
    # scopes are written below, without adding a parent cap over child goals.
    filled["expense_targets"] = {name: "not_assessed" for name in filled["expense_targets"]}
    filled["expense_targets"].update(expense)
    for cycle in cycles.values():
        for name in cycle["expense_targets"]:
            filled["expense_targets"].setdefault(name, "not_assessed")
    if "income_category" in doc["defaults"]:
        filled["expense_targets"].setdefault(doc["defaults"]["income_category"], "not_assessed")
    current_commodities = {row["account"]: set(row["payment_scope"]) for row in filled["accounts"]}
    known_commodities = {}
    for entry in fin.entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                known_commodities.setdefault(posting.account, set()).add(posting.units.currency)
    declared_accounts, account_locations = _grouped_accounts(doc, config, current_commodities)
    for row in filled["accounts"]:
        name = row["account"]
        declared = declared_accounts.get(name, doc.get("account_defaults", {}))
        require_yaml(type(declared) is dict and set(declared) <= {"semantics", "payment_scope", "opening"},
                     "SIMPLE_ACCOUNT_INVALID", "账户只需 semantics、payment_scope 和必要的 opening", config, ("accounts", name))
        row["semantics"] = declared.get("semantics")
        require_yaml(type(declared.get("payment_scope", {})) is dict, "SIMPLE_ACCOUNT_INVALID",
                     "payment_scope 须为币种到 payable／excluded／pending 的映射", config, ("accounts", name, "payment_scope"))
        # A single public config serves older months too. Declarations for
        # actual later postings must not add those currencies to past cash.
        # Unknown currencies remain visible to the compiler's strict check.
        row["payment_scope"].update({commodity: scope for commodity, scope in declared.get("payment_scope", {}).items()
            if commodity in current_commodities[name] or commodity not in known_commodities.get(name, set())})
        row["reason"] = f"使用者在 {config.name} 中声明账户属性"
        opening = declared.get("opening", {})
        require_yaml(type(opening) in (dict, list), "SIMPLE_ACCOUNT_INVALID", "opening 须为币种用途映射或完整坐标列表", config, ("accounts", name, "opening"))
        require_yaml(not isinstance(opening, list) or all(type(part) is dict for part in opening),
                     "SIMPLE_ACCOUNT_INVALID", "opening 坐标须为映射", config, ("accounts", name, "opening"))
        for coordinate in row["opening"]:
            if isinstance(opening, list):
                matches = [part for part in opening if part.get("commodity") == coordinate["commodity"] and part.get("lot") == coordinate["lot"]]
                coordinate["owners"] = matches[0].get("owners", {}) if len(matches) == 1 else {}
            elif coordinate["lot"] == coordinate["commodity"] + "/no-cost":
                coordinate["owners"] = opening.get(coordinate["commodity"], {})
            if type(coordinate["owners"]) is dict:
                for owner in coordinate["owners"]:
                    if owner != "-":
                        filled["allocation_targets"].setdefault(owner, goals["allocation_targets"].get(owner, "not_assessed"))
            if row["semantics"] == "H" and not coordinate["owners"] and Fraction(coordinate["signed_units"]) > 0:
                unresolved_opening.add((name, coordinate["commodity"], coordinate["lot"]))
                # Input assembly must not require ordinary owner slices
                # for a reviewed securities pool. Remove these temporary
                # compiler-only rows before any audit sees the document;
                # missing ordinary openings still become real review items.
                coordinate["owners"] = {"-": coordinate["signed_units"]}

    def input_location(keys):
        target, public_keys = config, keys
        if len(keys) > 1 and keys[0] == "accounts" and type(keys[1]) is int:
            name = filled["accounts"][keys[1]]["account"]
            public_keys = (*account_locations.get(name, ("accounts", name)), *keys[2:])
        elif len(keys) > 1 and keys[0] in ("expense_targets", "allocation_targets"):
            field = keys[0] if keys[0] != "expense_targets" or keys[1] not in goals["expense_direct_targets"] else "expense_direct_targets"
            public_keys = locations.get((field, keys[1]), ("months", month))
            target = budgets
        return yaml_path_location(target, public_keys)

    if materialize:
        runtime.temporary = RuntimeFiles()
        destination = Path(runtime.temporary.name).resolve() / "compiled"
    else:
        destination = (Path(gettempdir()) / ('budget-runtime-' + uuid4().hex) / 'compiled').resolve()
    documents, issues = compile_inputs(filled, config, destination, location=input_location)
    if issues:
        items = [{**item, "kind": "configuration", "metrics": ["配置"], "reason_kind": "data_review"}
                 for item in issues]
        runtime.pending = {"month": month, "status": "NEEDS_REVIEW", "stage": "metrics", "report": None,
                           "review_items": items, "pending_metrics": [], "configuration_only": True,
                           "review_queue": {"preflight_only": True}}
        runtime.check()
        runtime.close()
        return runtime
    month_config = documents.pop("month.yaml")
    generated = {name.removesuffix(".yaml"): document for name, document in documents.items()}
    owner_rows, owner_map, owner_names = _registry(generated["ownership"]["owners"], "fund")
    categories, category_map, category_names = _registry(generated["classifications"]["categories"], "category", categories=True)
    ownership = generated["ownership"]
    ownership["owners"] = owner_rows
    ownership["defaults"] = {key: owner_map[value] for key, value in ownership["defaults"].items()}
    classifications = generated["classifications"]
    classifications["categories"] = categories
    classifications["defaults"] = {key: category_map[value] for key, value in classifications["defaults"].items()}
    if "income_category" in doc["defaults"]:
        classifications["defaults"]["income_category_id"] = category_names[doc["defaults"]["income_category"]]
    if doc.get("classification_defaults") is False:
        classifications.pop("defaults", None)
    generated["opening"]["h_holdings"] = [row for row in generated["opening"]["h_holdings"]
        if (row["account"], row["commodity"], row["lot"]) not in unresolved_opening]
    for row in generated["opening"]["h_holdings"]:
        if row["owner"] != "-":
            row["owner"] = owner_map[row["owner"]]
    generated["obligations"]["owners"] = owner_rows
    # These settings are authoritative in config.yml, never sticky approvals
    # recovered from .budget-state after a user removes a rule.
    for key in ("account_rules", "account_rule_assets", "account_rule_defaults", "budget_categories", "budget_owners"):
        generated["obligations"].pop(key, None)
    if liability_rules:
        compiled_rules = {account: {"role": rule["role"], "owner_id": owner_names[doc["defaults"]["expense_purpose"]],
                                    **({"due_on": rule["due_on"]} if "due_on" in rule else {})}
                          for account, rule in liability_rules.items()}
        cash_accounts = [row["account"] for row in generated["accounts"]["accounts"] if row["semantics"] == "H"]
        for document in (generated["obligations"], ownership):
            document["account_rules"] = compiled_rules
            document["account_rule_assets"] = cash_accounts
        generated["obligations"]["account_rule_defaults"] = deepcopy(ownership["defaults"])
    for document in (generated["obligations"], ownership):
        document["budget_owners"] = dict(owner_names)
    # Liability labels have their own projection, never consumption totals.
    if liability_rules or any(isinstance(entry, data.Transaction) and entry.date >= cutover and
            any(posting.account.split(":", 1)[0] == fin.options["name_liabilities"] and
                "budget" in (posting.meta or {}) for posting in entry.postings) for entry in fin.entries):
        generated["obligations"]["budget_categories"] = sorted(category_names)
    allocations = {row["id"]: "not_assessed" for row in owner_rows}
    declared_allocations = set()
    for name, value in goals["allocation_targets"].items():
        ident = owner_names[name]
        require_yaml(ident not in declared_allocations, "SIMPLE_TARGET_DUPLICATE",
                     "同一用途的名称和别名不能重复设置目标", budgets, locations[("allocation_targets", name)])
        declared_allocations.add(ident)
        allocations[ident] = str(value)
    generated["purpose_plans"]["months"][month]["allocation_targets"] = allocations
    def target_values(field):
        return {category_names[name]: str(value) for name, value in goals[field].items()}
    subtree = {category_names[name]: str(value) for name, value in expense.items() if name not in goals["expense_direct_targets"]}
    subtree.update(target_values("expense_targets"))
    cap = doc.get("spendable_cap_category", doc["defaults"]["expense_category"])
    if cap is None:
        generated["expense_plans"].pop("spendable_cap_category_id", None)
    else:
        require_yaml(type(cap) is str and cap in category_names, "SIMPLE_CAP_INVALID",
                     "消费上限类别须是已使用或已配置目标的类别名称", config, ("spendable_cap_category",))
        generated["expense_plans"]["spendable_cap_category_id"] = category_names[cap]
    generated["expense_plans"]["months"][month] = {"expense_subtree_targets": subtree, "expense_targets": target_values("expense_direct_targets")}
    if doc.get("expense_budget_basis") == "salary_cycle":
        generated["salary_cycle"] = {"schema": 1, "cycles": cycles, "category": cap, "source": str(budgets)}
    complete = goals.get("completeness_confirmed")
    if complete is True:
        generated["completeness"]["reason"] = "使用者在 budgets.yml 确认该月截止日账单完整"
    else:
        generated.pop("completeness", None)
        month_config.pop("completeness", None)
    for key, document in generated.items():
        month_config[key] = key + ".yaml"
    documents = {key + '.yaml': value for key, value in generated.items()} | {'month.yaml': month_config}
    if materialize:
        for name, document in documents.items():
            _write(destination / name, document)
    else:
        register_documents(destination, documents, SetupDumper)
    runtime.config = destination / "month.yaml"
    runtime.check()
    return runtime


def audit_simple(config: Path, month: str | None = None, *, include_transactions=False):
    from ..query import QuerySession
    with QuerySession() as query:
        return query.month(config, month, include_transactions=include_transactions)


def audit_simple_period(config: Path, since=None, through=None):
    from ..query import QuerySession
    with QuerySession() as query:
        return query.period(config, since, through)


def _check_import_supported(documents, source, context):
    """Reject old evidence that the existing public fields cannot express."""
    unsupported = []
    for name, fields in {
            'accounts': ('credit_card_whitelist', 'same_day_settlements'),
            'ownership': ('claims', 'transfer_orders'),
            'classifications': ('classifications',),
            'obligations': ('events', 'opening'), 'benefit_groups': ('approvals',),
            'purpose_pools': ('pools',), 'h_transfers': ('events',),
            'h_fx': ('events',), 'k_events': ('events',)
    }.items():
        for key in fields:
            if documents.get(name, {}).get(key):
                unsupported.append(f'{name}.{key}')
    unsupported.extend(name for name in ('debt_coverage', 'debt_settlements') if name in documents)
    debt = documents.get('obligations', {})
    if debt.get('account_rules') and 'budget_owners' not in debt:
        unsupported.append('obligations.account_rules')
    defaults = documents.get('ownership', {}).get('defaults', {})
    cash = {row['account'] for row in documents.get('accounts', {}).get('accounts', [])
            if row.get('semantics') == 'H'}
    for name in ('ownership', 'obligations'):
        doc = documents.get(name, {})
        if doc.get('account_rules') and (
                set(doc.get('account_rule_assets', [])) != cash or
                doc.get('defaults' if name == 'ownership' else 'account_rule_defaults') != defaults or
                any(rule.get('owner_id') != defaults.get('expense_owner_id')
                    for rule in doc['account_rules'].values())):
            unsupported.append(name + '.account_rules')
    for name, rows in (('ownership', 'owners'), ('classifications', 'categories')):
        if any(row.get('aliases') for row in documents.get(name, {}).get(rows, [])):
            unsupported.append(name + '.aliases')
    categories = documents.get('classifications', {}).get('categories', [])
    names = {row['id']: row['name'] for row in categories}
    if any(row.get('parent') is not None and row['parent'] not in names or names.get(row.get('parent')) !=
           (row['name'].rsplit('.', 1)[0] if '.' in row['name'] else None) for row in categories):
        unsupported.append('classifications.parent')
    statement = documents.get('completeness', {})
    if any(statement.get(key, 'none') != 'none' for key in ('unbooked_obligations', 'inflight_payments')):
        unsupported.append('completeness')
    if statement:
        from .salary_plan import month_end
        fields = {'schema', 'cutover', 'as_of', 'unbooked_obligations', 'inflight_payments', 'reason'}
        if (not fields <= set(statement) <= fields | {'ledger_snapshot', 'as_of_booked_fingerprint'} or
                type(statement.get('schema')) is not int or statement['schema'] != 6 or
                statement.get('cutover') != context['cutover'] or
                statement.get('as_of') != month_end(context['month']).isoformat() or
                type(statement.get('reason')) is not str or not statement['reason'].strip()):
            unsupported.append('completeness.context')
    require(not unsupported, 'SIMPLE_IMPORT_UNSUPPORTED',
            '旧配置含现有两文件字段无法表达的外部证据：' + '、'.join(unsupported) +
            '；不迁入、不丢弃后签发。请明确改账本/现有配置；无法表达的协议暂不支持。', str(source))


def create_files(output: Path, *, ledger: Path | None = None, month: str | None = None, old_config: Path | None = None):
    """Create two explicit YAML files; never create hidden approval state."""
    from .monthly_report import REQUIRED, OPTIONAL
    output = output.resolve()
    require(not output.exists(), "SIMPLE_OUTPUT_EXISTS", "输出目录已存在；请选择新目录，原配置不会覆盖", str(output))
    watched = {}
    state = {}
    if old_config:
        old_config = old_config.resolve()
        old = load_yaml(old_config)
        require(type(old) is dict and REQUIRED <= set(old) <= REQUIRED | OPTIONAL,
                "SIMPLE_IMPORT_INVALID", "迁移来源须是现有 month.yaml", str(old_config))
        watched[old_config] = input_bytes(old_config)
        ledger, month = config_path(old_config, old["ledger"], "ledger"), old["month"]
        for key in set(old) - SCALARS:
            path = config_path(old_config, old[key], key)
            watched[path] = input_bytes(path)
            state[key] = load_yaml(path)
        _check_import_supported(state, old_config, old)
        defaults = state["ownership"].get("defaults")
        require(type(defaults) is dict, "SIMPLE_IMPORT_DEFAULTS",
                "旧配置没有默认收入／支出用途；请先明确这两个默认名称再迁移", str(old_config))
        owner_names = {row["id"]: row["name"] for row in state["ownership"]["owners"]}
        category_names = {row["id"]: row["name"] for row in state["classifications"]["categories"]}
        classification_defaults = state["classifications"].get("defaults", {})
        expense_category = classification_defaults.get("expense_category_id") or state["expense_plans"].get("spendable_cap_category_id")
        names = {"income_purpose": owner_names[defaults["income_owner_id"]], "expense_purpose": owner_names[defaults["expense_owner_id"]],
                 "expense_category": category_names[expense_category] if expense_category else owner_names[defaults["expense_owner_id"]]}
        income_category = classification_defaults.get("income_category_id")
        if income_category and category_names[income_category] != names["income_purpose"]:
            names["income_category"] = category_names[income_category]
        cutover, currency = old["cutover"], old["report_currency"]
    else:
        require(ledger is not None and month is not None, "SIMPLE_CONFIG_REQUIRED", "新建须提供 --ledger 和 --month")
        names = {"income_purpose": "自由资金", "expense_purpose": "生活费", "expense_category": "生活开销"}
        cutover, currency = month + "-01", "CNY"
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".budget-config-", dir=output.parent) as temporary:
        staging = Path(temporary) / "output"
        staging.mkdir()
        setup, _ = draft_inputs(ledger.resolve(), month, cutover=date.fromisoformat(cutover), currency=currency,
                                **{key: names[key] for key in ("income_purpose", "expense_purpose", "expense_category")})
        accounts = {}
        declared = {row["account"]: row for row in state.get("accounts", {}).get("accounts", [])}
        for row in setup["accounts"]:
            reviewed = declared.get(row["account"], {})
            account = {"semantics": reviewed.get("semantics"), "payment_scope": reviewed.get("payment_scope", row["payment_scope"])}
            opening = []
            for coordinate in row["opening"]:
                slices = [part for part in state.get("opening", {}).get("h_holdings", []) if
                          (part["account"], part["commodity"], part["lot"]) == (row["account"], coordinate["commodity"], coordinate["lot"])]
                opening.append({**coordinate, "owners": {owner_names.get(part["owner"], part["owner"]): part["units"] for part in slices}})
            if opening:
                if old_config and reviewed.get("semantics") == "H":
                    owned = [coordinate for coordinate in opening if coordinate["owners"]]
                    if owned and all(coordinate["lot"] == coordinate["commodity"] + "/no-cost" for coordinate in owned):
                        account["opening"] = {coordinate["commodity"]: coordinate["owners"] for coordinate in owned}
                    elif owned:
                        account["opening"] = owned
                elif not old_config:
                    account["opening"] = opening
            accounts[row["account"]] = account
        public = {"schema": 1, "ledger": relative_reference(ledger.resolve(), output), "budgets": "budgets.yml",
                  "cutover": cutover, "month": month, "report_currency": currency, "defaults": names, "accounts": accounts}
        roles = state.get('obligations', {}).get('account_rules', {})
        metadata = {account: {key: rule[key] for key in ('role','due_on') if key in rule}
                    for account,rule in roles.items() if rule['role']!='payable'}
        if metadata:
            public['liabilities'] = metadata
        if old_config:
            if "defaults" not in state["classifications"]:
                public["classification_defaults"] = False
            cap = state["expense_plans"].get("spendable_cap_category_id")
            if cap is None or category_names[cap] != names["expense_category"]:
                public["spendable_cap_category"] = category_names.get(cap)
        budgets = {"schema": 1, "defaults": {}, "months": {month: {"expense_targets": {names["expense_category"]: "not_assessed"}}}}
        if old_config:
            budgets = {"schema": 1}
            if state.get("completeness"):
                budgets["months"] = {month: {"completeness_confirmed": True}}
            if "salary_cycle" in state:
                public["expense_budget_basis"] = "salary_cycle"
                budgets["salary_cycles"] = deepcopy(state["salary_cycle"]["cycles"])
            for key, doc in (("expense_plans", state["expense_plans"]), ("purpose_plans", state["purpose_plans"])):
                for field in ("defaults", "effective_templates", "months"):
                    if field not in doc:
                        continue
                    layers = {None: doc[field]} if field == "defaults" else doc[field]
                    for start, layer in layers.items():
                        target = budgets.setdefault(field, {}) if start is None else budgets.setdefault(field, {}).setdefault(start, {})
                        for old_key, new_key, registry in (("expense_subtree_targets", "expense_targets", category_names),
                                                          ("expense_targets", "expense_direct_targets", category_names),
                                                          ("allocation_targets", "allocation_targets", owner_names)):
                            if old_key in layer:
                                target[new_key] = {registry[ident]: value for ident, value in layer[old_key].items()}
            # Missing old targets were unresolved, not an explicit decision to
            # stop assessing them. Preserve that distinction during migration.
            from .purpose_targets import load_purpose_targets
            purpose_path = config_path(old_config, old["purpose_plans"], "purpose_plans")
            expanded = load_purpose_targets(purpose_path, month, currency, set(owner_names))
            for ident in expanded["missing_target_ids"]:
                budgets.setdefault("months", {}).setdefault(month, {}).setdefault("allocation_targets", {})[owner_names[ident]] = None
            effective, _ = _layer(budgets, month, old_config)
            configured = set(effective["expense_targets"]) | set(effective["expense_direct_targets"])
            for root_name in setup["expense_targets"]:
                if not any(name == root_name or name.startswith(root_name + ".") for name in configured):
                    budgets.setdefault("months", {}).setdefault(month, {}).setdefault("expense_targets", {})[root_name] = None
        _write(staging / "config.yml", public)
        _write(staging / "budgets.yml", budgets)
        _unchanged(watched)
        os.replace(staging, output)
    return {"config": str(output / "config.yml"), "budgets": str(output / "budgets.yml"),
            "note": "以后只维护这两份文件；运行时自动准备内部输入。原账本和旧配置保持不变。"}


def main(argv=None):
    configure_output()
    parser = argparse.ArgumentParser(description="创建／迁移 config.yml + budgets.yml 两文件入口")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-config", type=Path, help="迁移旧 month.yaml 中现有字段能表达的配置；外部批准不迁入")
    source.add_argument("--ledger", type=Path, help="从账本生成两文件模板")
    parser.add_argument("--month")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.from_config and args.month:
        parser.error("迁移使用来源 month.yaml 的月份；以后运行时用 --month 切换")
    try:
        result = create_files(args.output, ledger=args.ledger, month=args.month, old_config=args.from_config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BudgetError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Compile shared monthly inputs for simple configs, review, and CLI adapters.

Temporary publication, source guards, and exact input loading live here so
daily use never depends on migration command modules.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from fractions import Fraction
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from beancount.core import data
import yaml

from ..runtime_inputs import input_bytes, input_is_file
from ..errors import BudgetError, require
from ..exact import exact, exact_text
from ..financial import booked_through, frozen_financial, load_financial, month_bounds, shared_audit
from ..yamlstrict import config_path, load_yaml, yaml_path_location, require_yaml
from .targets import _month
from .accounts import audit_accounts
from .classification import _name, _registry
from .facts import frozen_fact_indices
from .inline_amounts import parse_inline_amounts
from .ownership import _owners
from .source import collect_v6_source_issues


MONTH_SCALARS = {"schema", "cutover", "month", "report_currency"}

MONEY = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")
NATIVE = re.compile(r"(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")


class SetupDumper(yaml.SafeDumper):
    pass


def _string(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value,
                                   style='"' if re.fullmatch(r"\d{4}-\d{2}", value) else None)


SetupDumper.add_representer(str, _string)


def relative_reference(ledger: Path, directory: Path) -> str:
    try:
        return Path(os.path.relpath(ledger, directory)).as_posix()
    except ValueError:  # Windows paths on different drives cannot be relative.
        return ledger.as_posix()


@contextmanager
def month_inputs(ledger: Path, month: str, cutover: date):
    first, last = month_bounds(month)
    require(cutover.day == 1 and cutover <= first, "CUTOVER_INVALID",
            "cutover 须为不晚于报告月的自然月第一天")
    with booked_through(last), frozen_financial(ledger) as fin, frozen_fact_indices():
        yield fin, last


@shared_audit
def _inspect(root: Path, cutover: date, income: str, expense: str, category: str):
    fin = load_financial(root)
    accounts = audit_accounts(fin.root, cutover)["accounts"]
    purposes = {income, expense}
    categories = {income, category}
    expense_names = {category}
    issues = list(collect_v6_source_issues(fin.root, cutover))
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        for posting in entry.postings:
            root = posting.account.split(":", 1)[0]
            key = "fund" if root == fin.options["name_assets"] else "budget"
            if key == "budget" and root not in (fin.options["name_income"], fin.options["name_expenses"], fin.options["name_liabilities"]):
                continue
            raw = (posting.meta or {}).get(key)
            if not isinstance(raw, str):
                continue
            source = f"{posting.meta.get('filename')}:{posting.meta.get('lineno')}"
            try:
                parts = parse_inline_amounts(raw, exact(posting.units.number), source)
                names = {name for name, _ in parts if name != "-"}
                if key == "fund":
                    purposes.update(names)
                else:
                    names = {_name(name) for name in names}
                    categories.update(names)
                    if root == fin.options["name_expenses"]:
                        expense_names.update(names)
            except BudgetError as exc:
                issues.append({"reason_code": exc.code, "detail": exc.message, "source": source})
    return accounts, purposes, categories, expense_names, issues


def _registry_rows(names: set[str], prefix: str, categories: bool = False):
    if categories:
        names = {".".join(parts[:index]) for name in names
                 for parts in [name.split(".")] for index in range(1, len(parts) + 1)}
    ids = {name: f"{prefix}_{index}" for index, name in enumerate(sorted(names), 1)}
    rows = [{"id": ids[name], "name": name, "aliases": [],
             **({"parent": ids.get(name.rpartition(".")[0])} if categories else {})}
            for name in sorted(names)]
    return rows, ids


def publish_inputs(output: Path, files: dict[str, object]):
    """Publish a new directory only; never overwrite someone's review decisions."""
    output = output.resolve()
    require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已存在；请选择新目录，避免覆盖审阅决定", str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".budget-setup-", dir=output.parent) as folder:
        staging = Path(folder) / "output"
        staging.mkdir()
        for name, document in files.items():
            content = document if isinstance(document, str) else yaml.dump(
                document, Dumper=SetupDumper, allow_unicode=True, sort_keys=False)
            (staging / name).write_text(content, encoding="utf-8")
        require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已经存在", str(output))
        staging.rename(output)


def draft_inputs(ledger: Path, month: str, *, cutover: date | None = None,
                 currency: str | None = None, income_purpose: str = "已收入资金",
                 expense_purpose: str = "待支出资金", expense_category: str | None = None) -> tuple[dict, list]:
    """Inspect source facts into an unapproved document without creating files."""
    first, _ = month_bounds(month)
    cutover = cutover or first
    category = expense_category or expense_purpose
    _owners([{"id": "income", "name": income_purpose, "aliases": []}])
    _owners([{"id": "expense", "name": expense_purpose, "aliases": []}])
    _name(category)
    with month_inputs(ledger.resolve(), month, cutover) as (fin, last):
        currencies = fin.options.get("operating_currency", [])
        require(currency is not None or len(currencies) == 1, "REPORT_CURRENCY_REQUIRED",
                "账本有多个或没有 operating_currency，请显式传 --currency")
        currency = currency or currencies[0]
        require(currency in currencies, "REPORT_CURRENCY_INVALID", "报告货币须来自 operating_currency")
        rows, purposes, _, expense_names, issues = _inspect(fin.root, cutover, income_purpose, expense_purpose, category)
        worksheet = {
            "schema": 6, "ledger": str(fin.root), "cutover": cutover.isoformat(),
            "month": month, "report_currency": currency,
            "defaults": {"income_purpose": income_purpose, "expense_purpose": expense_purpose,
                         "expense_category": category, "confirmed": False},
            "accounts": [{"account": row["account"], "semantics": None,
                          "payment_scope": {commodity: None for commodity in row["commodities"]},
                          "opening": [{**coordinate, "owners": {}}
                                      for coordinate in row["opening_coordinates"]
                                      if Fraction(coordinate["signed_units"]) != 0],
                          "reason": None} for row in rows],
            "expense_targets": {name: None for name in sorted({n.split(".")[0] for n in expense_names})},
            "allocation_targets": {name: None for name in sorted(purposes)},
            "completeness_confirmed": False,
        }
        return worksheet, issues


def compile_inputs(doc: dict, source: Path, output: Path, *, location=None) -> tuple[dict, list]:
    """Build input documents in memory; callers choose publication and locations."""
    source, output = source.resolve(), output.resolve()
    loc = location or (lambda keys: yaml_path_location(source, keys))
    fields = {"schema", "ledger", "cutover", "month", "report_currency", "defaults", "accounts",
              "expense_targets", "allocation_targets", "completeness_confirmed"}
    require(type(doc) is dict and set(doc) == fields and type(doc["schema"]) is int and doc["schema"] == 6,
            "SETUP_INVALID", "填写表字段不完整、含未知字段或版本无效", loc(()))
    require(all(type(doc[key]) is str and doc[key] for key in ("ledger", "month", "cutover", "report_currency")),
            "SETUP_INVALID", "账本、月份、切换点和货币须为字符串", loc(()))
    try:
        cutover = date.fromisoformat(doc["cutover"])
    except ValueError as exc:
        raise BudgetError("CUTOVER_INVALID", "cutover 须为 YYYY-MM-DD", loc(("cutover",))) from exc
    defaults = doc["defaults"]
    require(type(defaults) is dict and set(defaults) == {
        "income_purpose", "expense_purpose", "expense_category", "confirmed"},
        "SETUP_INVALID", "defaults 字段无效", loc(("defaults",)))
    require(all(type(defaults[key]) is str and defaults[key].strip() for key in
                ("income_purpose", "expense_purpose", "expense_category")),
            "SETUP_INVALID", "默认用途和类别须为非空名称", loc(("defaults",)))
    for key in ("income_purpose", "expense_purpose", "expense_category"):
        try:
            if key == "expense_category":
                _name(defaults[key])
            else:
                _owners([{"id": "default", "name": defaults[key], "aliases": []}])
        except BudgetError as exc:
            raise BudgetError(exc.code, exc.message, loc(("defaults", key))) from exc
    issues = []

    def pending(keys, detail):
        issues.append({"reason_code": "SETUP_DECISION_REQUIRED", "detail": detail,
                       "source": loc(keys)})

    def money(raw, keys, allow_not_assessed=False, native=False):
        if allow_not_assessed and raw == "not_assessed":
            return raw
        if type(raw) is int and raw >= 0:
            return str(raw)
        if type(raw) is str and (NATIVE if native else MONEY).fullmatch(raw):
            return raw
        pending(keys, "填写非负金额（例如 '2000'）" + ("，或 not_assessed" if allow_not_assessed else ""))
        return None

    with month_inputs((source.parent / doc["ledger"]).resolve(), doc["month"], cutover) as (fin, last):
        require(doc["report_currency"] in fin.options.get("operating_currency", []),
                "REPORT_CURRENCY_INVALID", "报告货币须来自 operating_currency", loc(("report_currency",)))
        rows, purposes, category_names, expense_names, source_issues = _inspect(
            fin.root, cutover, defaults["income_purpose"], defaults["expense_purpose"], defaults["expense_category"])
        purposes, category_names = set(purposes), set(category_names)
        issues.extend(source_issues)
        if defaults["confirmed"] is not True:
            pending(("defaults", "confirmed"), "确认未标标签时使用这些默认用途和类别后填 true")
        if doc["completeness_confirmed"] is not True:
            pending(("completeness_confirmed",), f"确认截至 {last} 的账本资料及责任完整后填 true")
        require(type(doc["accounts"]) is list, "SETUP_INVALID", "accounts 须为列表", loc(("accounts",)))
        by_account = {row["account"]: row for row in rows}
        seen = set()
        account_claims, h_openings, k_openings = [], [], []
        for index, row in enumerate(doc["accounts"]):
            keys = ("accounts", index)
            require(type(row) is dict and set(row) == {"account", "semantics", "payment_scope", "opening", "reason"},
                    "SETUP_INVALID", "账户字段无效", loc(keys))
            account = row["account"]
            require(type(account) is str and account in by_account and account not in seen,
                    "SETUP_ACCOUNT_INVALID", "账户不存在或重复，请重新盘点源账本", loc((*keys, "account")))
            seen.add(account)
            original = by_account[account]
            mode = row["semantics"]
            if mode not in ("H", "K"):
                pending((*keys, "semantics"), f"{account}：选择 H（真实余额）或 K（累计申赎）")
            scope = row["payment_scope"]
            require(type(scope) is dict and set(scope) == set(original["commodities"]),
                    "SETUP_ACCOUNT_INVALID", f"{account}：支付范围的币种须与当前账本一致（实际：{', '.join(original['commodities'])}）", loc((*keys, "payment_scope")))
            for commodity, value in scope.items():
                if value not in ("payable", "excluded", "pending") or mode == "K" and value != "excluded":
                    choices = ("excluded／pending（外币不计入首页直接可支付）" if commodity != doc["report_currency"]
                               else "payable／excluded／pending；K 只能 excluded")
                    pending((*keys, "payment_scope", commodity),
                            f"{account} / {commodity}：在 accounts 的 payment_scope 中选择 {choices}")
            if type(row["reason"]) is not str or not row["reason"].strip():
                pending((*keys, "reason"), "填写账户语义、支付范围及期初判断依据")
            expected = {(c["commodity"], c["lot"]): c["signed_units"] for c in original["opening_coordinates"]
                        if Fraction(c["signed_units"]) != 0}
            require(type(row["opening"]) is list, "SETUP_INVALID", "opening 须为列表", loc((*keys, "opening")))
            visited = set()
            for number, coordinate in enumerate(row["opening"]):
                ck = (*keys, "opening", number)
                require(type(coordinate) is dict and set(coordinate) == {"commodity", "lot", "signed_units", "owners"},
                        "SETUP_INVALID", "期初坐标字段无效", loc(ck))
                require(all(type(coordinate[key]) is str for key in ("commodity", "lot", "signed_units")),
                        "SETUP_INVALID", "期初币种、lot 和数量须保留原字符串", loc(ck))
                pair = (coordinate["commodity"], coordinate["lot"])
                require(pair in expected and pair not in visited and coordinate["signed_units"] == expected[pair],
                        "SETUP_OPENING_CHANGED", "期初坐标或金额与账本不符，请重新盘点", loc(ck))
                visited.add(pair)
                owners = coordinate["owners"]
                require(type(owners) is dict, "SETUP_INVALID", "owners 须为用途到数量的映射", loc((*ck, "owners")))
                if mode == "H":
                    total = Fraction(0)
                    if not owners:
                        pending((*ck, "owners"), "按用途拆分这份真实期初余额")
                    for owner, raw in owners.items():
                        require(type(owner) is str and owner.strip(), "SETUP_INVALID", "用途名称无效", loc((*ck, "owners")))
                        if owner != "-":
                            try:
                                _owners([{"id": "opening", "name": owner, "aliases": []}])
                            except BudgetError as exc:
                                raise BudgetError(exc.code, exc.message,
                                                  loc((*ck, "owners", owner))) from exc
                        units = money(raw, (*ck, "owners", owner), native=True)
                        if units is not None and Fraction(units) > 0:
                            total += Fraction(units)
                            if owner != "-":
                                purposes.add(owner)
                            h_openings.append({"account": account, "commodity": pair[0], "lot": pair[1],
                                "owner": owner, "units": units, "reason": row["reason"]})
                        elif units is not None:
                            pending((*ck, "owners", owner), "每份 H 期初用途数量须为正数；零份额请删除")
                    if total != Fraction(coordinate["signed_units"]) or total <= 0:
                        pending((*ck, "owners"), "H 期初正数用途切片之和须精确等于账本余额")
                elif mode == "K":
                    if owners:
                        pending((*ck, "owners"), "K 期初只确认累计数，owners 留空；不生成真实库存")
                    k_openings.append({"account": account, "commodity": pair[0], "lot": pair[1],
                                       "signed_units": coordinate["signed_units"], "reason": row["reason"]})
            require(visited == set(expected), "SETUP_OPENING_CHANGED", "填写表遗漏了账本期初坐标", loc((*keys, "opening")))
            if mode == "K" and not expected:
                commodity = original["commodities"][0]
                k_openings.append({"account": account, "commodity": commodity, "lot": f"{commodity}/no-cost",
                                   "signed_units": "0", "reason": row["reason"]})
            account_claims.append({"account": account, "semantics": mode,
                "balance_semantics": "real_inventory" if mode == "H" else "signed_subscription_flow",
                "payment_scope": scope, "reason": row["reason"]})
        if seen != set(by_account):
            pending(("accounts",), "账本出现新账户或填写表缺账户，请重新生成草稿并保留已有决定")
        require(type(doc["expense_targets"]) is dict and type(doc["allocation_targets"]) is dict,
                "SETUP_INVALID", "目标须为名称到金额的映射", loc(()))
        require(all(type(name) is str and bool(name.strip()) for name in
                    list(doc["expense_targets"]) + list(doc["allocation_targets"])),
                "SETUP_INVALID", "目标名称须为非空字符串", loc(()))
        for name in doc["expense_targets"]:
            try:
                category_names.add(_name(name))
            except BudgetError as exc:
                raise BudgetError(exc.code, exc.message, loc(("expense_targets", name))) from exc
        for name in doc["allocation_targets"]:
            try:
                _owners([{"id": "allocation", "name": name, "aliases": []}])
            except BudgetError as exc:
                raise BudgetError(exc.code, exc.message, loc(("allocation_targets", name))) from exc
        purposes.update(doc["allocation_targets"])
        category_rows, category_ids = _registry_rows(category_names, "category", True)
        owner_rows, owner_ids = _registry_rows(purposes, "owner")
        _registry(category_rows)
        _owners(owner_rows)
        expense_targets = {}
        for name, raw in doc["expense_targets"].items():
            value = money(raw, ("expense_targets", name), allow_not_assessed=True)
            if value is not None:
                expense_targets[category_ids[_name(name)]] = value
        for name in expense_names:
            if not any(name == parent or name.startswith(parent + ".") for parent in doc["expense_targets"]):
                pending(("expense_targets",), f"为 {name} 或其父类别填写消费上限")
        allocations = {}
        for name in owner_ids:
            value = money(doc["allocation_targets"].get(name), ("allocation_targets", name), True)
            if value is not None:
                allocations[owner_ids[name]] = value
        if issues:
            return {}, issues
        for row in h_openings:
            if row["owner"] != "-":
                row["owner"] = owner_ids[row["owner"]]
        header = {"schema": 6, "cutover": cutover.isoformat(), "ledger_snapshot": fin.digest,
                  "loader_version": fin.loader_version}
        files = {
            "accounts.yaml": {k: v for k, v in header.items() if k != "loader_version"} |
                {"report_currency": doc["report_currency"], "accounts": account_claims},
            "opening.yaml": {k: v for k, v in header.items() if k != "loader_version"} |
                {"h_holdings": h_openings, "k_openings": k_openings},
            "ownership.yaml": {**header, "owners": owner_rows, "claims": [], "defaults": {
                "income_owner_id": owner_ids[defaults["income_purpose"]],
                "expense_owner_id": owner_ids[defaults["expense_purpose"]]}},
            "classifications.yaml": {"schema": 6, "cutover": cutover.isoformat(), "loader_version": fin.loader_version,
                "categories": category_rows, "classifications": [], "defaults": {
                    "income_category_id": category_ids[defaults["income_purpose"]],
                    "expense_category_id": category_ids[defaults["expense_category"]]}},
            "obligations.yaml": {**header, "owners": owner_rows, "events": []},
            "benefit_groups.yaml": {**header, "report_currency": doc["report_currency"], "approvals": []},
            "purpose_pools.yaml": {"schema": 6, "cutover": cutover.isoformat(), "report_currency": doc["report_currency"], "pools": []},
            "expense_plans.yaml": {"schema": 6, "report_currency": doc["report_currency"],
                "spendable_cap_category_id": category_ids[defaults["expense_category"]],
                "months": {doc["month"]: {"expense_subtree_targets": expense_targets}}},
            "purpose_plans.yaml": {"schema": 6, "report_currency": doc["report_currency"],
                "months": {doc["month"]: {"allocation_targets": allocations}}},
            "completeness.yaml": {"schema": 6, "cutover": cutover.isoformat(), "as_of": last.isoformat(),
                "ledger_snapshot": fin.digest, "unbooked_obligations": "none", "inflight_payments": "none",
                "reason": "使用者在首月填写表中确认该截止日资料完整"},
            **{key + ".yaml": {**header, "events": []} for key in ("h_transfers", "h_fx", "k_events")},
        }
        files["month.yaml"] = {"schema": 6, "ledger": relative_reference(fin.root, output), "cutover": cutover.isoformat(),
            "month": doc["month"], "report_currency": doc["report_currency"],
            **{name.removesuffix(".yaml"): name for name in files}}
    return files, []


def read_month_config(config: Path):
    from .monthly_report import REQUIRED, OPTIONAL

    doc = load_yaml(config)
    require(type(doc) is dict and REQUIRED <= set(doc) <= REQUIRED | OPTIONAL and
            type(doc["schema"]) is int and doc["schema"] == 6,
            "REVIEW_CONFIG_INVALID", "请使用完整的 v6 月报配置（可由首月初始化生成）", f"{config}:1")
    for key in set(doc) - {"schema"}:
        require(type(doc[key]) is str and bool(doc[key].strip()), "REVIEW_CONFIG_INVALID",
                "配置月份、日期和路径须为非空字符串", yaml_path_location(config, (key,)))
    try:
        month_bounds(doc["month"])
        cutover = date.fromisoformat(doc["cutover"])
    except (BudgetError, ValueError) as exc:
        raise BudgetError("TIME_INVALID", "月份或 cutover 无效", str(config)) from exc
    paths = {key: (config.parent / value).resolve() for key, value in doc.items() if key not in MONTH_SCALARS}
    for key, path in paths.items():
        require(input_is_file(path), "REVIEW_CONFIG_INVALID", "配置引用的文件不存在",
                yaml_path_location(config, (key,)))
    return doc, paths, cutover


def check_unchanged(before: dict[Path, bytes] | None):
    for path, content in (before or {}).items():
        require(input_is_file(path) and input_bytes(path) == content,
                "INPUT_CHANGED", "审阅期间配置或侧文件发生变化；请重跑", str(path))


def saved_report_failure(result: dict, exc: Exception, io_error: str) -> dict:
    """Keep an already published config visible without exposing an old report."""
    return {**result, "workflow": None, "report_error": {
        "code": exc.code if isinstance(exc, BudgetError) else io_error,
        "message": exc.message if isinstance(exc, BudgetError) else str(exc),
        "source": exc.source if isinstance(exc, BudgetError) else str(result["config"])}}


def report_saved_inputs(result: dict, audit, before: dict[Path, bytes],
                        expected_inputs: dict[Path, bytes] | None, *,
                        io_error: str, include_transactions: bool = False) -> dict:
    """Audit final paths after publication; a report failure must retain the save."""
    try:
        config = Path(result["config"])
        workflow = audit(config, include_transactions=True) if include_transactions else audit(config)
        check_unchanged(before)
        check_unchanged(expected_inputs)
        return {**result, "workflow": workflow}
    except (BudgetError, OSError, UnicodeError) as exc:
        return saved_report_failure(result, exc, io_error)


def config_metadata(path: Path) -> tuple[str, tuple]:
    document = load_yaml(path)
    require_yaml(type(document) is dict and document.get("schema") == 6 and type(document.get("schema")) is int,
                 "MONTH_REPORT_CONFIG_INVALID", "内部月份输入须为 schema 6", path)
    try:
        month = _month(document.get("month"))
    except BudgetError as exc:
        raise BudgetError(exc.code, exc.message, yaml_path_location(path, ("month",))) from exc
    for key in ("ledger", "cutover", "report_currency"):
        require_yaml(type(document.get(key)) is str and bool(document[key].strip()),
                     "MONTH_REPORT_CONFIG_INVALID", "月份配置缺少账本、cutover 或报告货币", path, (key,))
    try:
        cutover = date.fromisoformat(document["cutover"])
    except ValueError as exc:
        raise BudgetError("CUTOVER_INVALID", "cutover 须为自然月第一天", yaml_path_location(path, ("cutover",))) from exc
    require_yaml(cutover.day == 1 and cutover <= date.fromisoformat(month + "-01"),
                 "CUTOVER_INVALID", "cutover 须为不晚于报告月的自然月第一天", path, ("cutover",))
    ledger = config_path(path, document["ledger"], "ledger")
    require_yaml(ledger.is_file(), "MONTH_REPORT_CONFIG_INVALID", "月份配置引用的账本不存在", path, ("ledger",))
    return month, (ledger, cutover, document["report_currency"])


def default_config():
    """Use the current two-file entry; no month-registration manifest."""
    candidates = [Path.cwd()/name for name in ("config.yml", "budget/simple/config.yml", "budget/config.yml")]
    found = [path.resolve() for path in candidates if path.is_file()]
    require(len(found)<=1, "CONFIG_AMBIGUOUS", "找到多份 config.yml，请用 --config 指定："+"、".join(map(str,found)))
    return found[0] if found else None

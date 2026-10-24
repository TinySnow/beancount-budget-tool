"""Create one editable first-month worksheet, then compile reviewed inputs."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from beancount.core import data
import yaml

from ..console import command, configure_output
from ..errors import BudgetError, require
from ..exact import exact, exact_text
from ..financial import booked_through, frozen_financial
from ..yamlstrict import load_yaml, yaml_path_location
from ..v6.accounts import audit_accounts
from ..v6.classification import _name, _registry
from ..v6.facts import frozen_fact_indices
from ..v6.inline_amounts import parse_inline_amounts
from ..v6.ownership import _owners
from ..v6.source import collect_v6_source_issues
from .v6_check_migration_month import _month


MONEY = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")
NATIVE = re.compile(r"(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")


class _SetupDumper(yaml.SafeDumper):
    pass


def _string(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value,
                                   style='"' if re.fullmatch(r"\d{4}-\d{2}", value) else None)


_SetupDumper.add_representer(str, _string)


def _ledger_reference(ledger: Path, directory: Path) -> str:
    try:
        return os.path.relpath(ledger, directory)
    except ValueError:  # Windows paths on different drives cannot be relative.
        return str(ledger)


@contextmanager
def _inputs(ledger: Path, month: str, cutover: date):
    first, last = _month(month)
    require(cutover.day == 1 and cutover <= first, "CUTOVER_INVALID",
            "cutover 须为不晚于报告月的自然月第一天")
    with booked_through(last), frozen_financial(ledger) as fin, frozen_fact_indices():
        yield fin, last


def _inspect(fin, cutover: date, income: str, expense: str, category: str):
    accounts = audit_accounts(fin.root, cutover)["accounts"]
    purposes = {income, expense}
    categories = {income, category}
    expense_names = {category}
    issues = collect_v6_source_issues(fin.root, cutover)
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        for posting in entry.postings:
            root = posting.account.split(":", 1)[0]
            key = "fund" if root == fin.options["name_assets"] else "budget"
            if key == "budget" and root not in (fin.options["name_income"], fin.options["name_expenses"]):
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


def _publish(output: Path, files: dict[str, object]):
    """Publish a new directory only; never overwrite someone's review decisions."""
    output = output.resolve()
    require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已存在；请选择新目录，避免覆盖审阅决定", str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".budget-setup-", dir=output.parent) as folder:
        staging = Path(folder) / "output"
        staging.mkdir()
        for name, document in files.items():
            content = document if isinstance(document, str) else yaml.dump(
                document, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False)
            (staging / name).write_text(content, encoding="utf-8")
        require(not output.exists(), "SETUP_OUTPUT_EXISTS", "输出目录已经存在", str(output))
        staging.rename(output)


def create_setup(ledger: Path, month: str, output: Path, *, cutover: date | None = None,
                 currency: str | None = None, income_purpose: str = "已收入资金",
                 expense_purpose: str = "待支出资金", expense_category: str | None = None) -> dict:
    first, _ = _month(month)
    cutover = cutover or first
    category = expense_category or expense_purpose
    _owners([{"id": "income", "name": income_purpose, "aliases": []}])
    _owners([{"id": "expense", "name": expense_purpose, "aliases": []}])
    _name(category)
    with _inputs(ledger.resolve(), month, cutover) as (fin, last):
        currencies = fin.options.get("operating_currency", [])
        require(currency is not None or len(currencies) == 1, "REPORT_CURRENCY_REQUIRED",
                "账本有多个或没有 operating_currency，请显式传 --currency")
        currency = currency or currencies[0]
        require(currency in currencies, "REPORT_CURRENCY_INVALID", "报告货币须来自 operating_currency")
        rows, purposes, _, expense_names, issues = _inspect(fin, cutover, income_purpose, expense_purpose, category)
        worksheet = {
            "schema": 6, "ledger": _ledger_reference(fin.root, output.resolve()), "cutover": cutover.isoformat(),
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
        files = {"setup.yaml": worksheet, "START_HERE.md": (
            "# 首月配置：只填写 setup.yaml\n\n"
            "1. defaults 是未标标签时的收入用途、支出用途和费用类别；确认后把 confirmed 改为 true。\n"
            "2. accounts 的 semantics 填 H（真实余额）或 K（只有累计申赎额）。payment_scope 每个币种填 payable、excluded 或 pending；K 必须全填 excluded。reason 写判断依据。\n"
            "3. H 的非零期初在 owners 中按用途拆分，例如 `{自由资金: '100', 储蓄: '200'}`，合计必须等于 signed_units。K 的 owners 留空，reason 表示确认账面累计额。不要修改提取的账户、币种、lot 和期初数。\n"
            "4. expense_targets 是本月各类别及其子类别的合计消费上限，金额填带引号的非负数字；历史目标无法确认或不考核时填 not_assessed，不计算目标差。allocation_targets 填划拨目标，或明确填 not_assessed。null 仍表示待决定；0 表示真实零额度。\n"
            f"5. 确认导入资料覆盖到 {last}，无该截止日尚未入账的责任或付款后，completeness_confirmed 改为 true；报告不代表截止日之后的现实余额。\n\n"
            "填不出来就保留 null 或 false。在本目录运行下列命令会列出待确认字段和行号，不会自动批准。\n\n"
            f"```\n{command('beancount-budget-init')} --apply setup.yaml\n```\n\n"
            f"全部首轮决定明确后生成 compiled/month.yaml，再运行 `{command('beancount-budget-tool')} --config compiled/month.yaml`。复杂转账、负债或缺价仍可能出现在后续待审清单中。\n"
            "生成目录不会覆盖已有文件；如已生成 compiled，请用 --output 指定新的目录。账本始终只读。\n")}
    _publish(output, files)
    return {"status": "DRAFT_CREATED", "setup": str((output / "setup.yaml").resolve()),
            "accounts": len(rows), "source_issues": issues,
            "note": "只生成填写表；没有批准账户、期初用途、目标或完整性。"}


def compile_setup(setup: Path, output: Path | None = None) -> dict:
    setup = setup.resolve()
    output = (output or setup.parent / "compiled").resolve()
    doc = load_yaml(setup)
    fields = {"schema", "ledger", "cutover", "month", "report_currency", "defaults", "accounts",
              "expense_targets", "allocation_targets", "completeness_confirmed"}
    require(type(doc) is dict and set(doc) == fields and type(doc["schema"]) is int and doc["schema"] == 6,
            "SETUP_INVALID", "填写表字段不完整、含未知字段或版本无效", f"{setup}:1")
    require(all(type(doc[key]) is str and doc[key] for key in ("ledger", "month", "cutover", "report_currency")),
            "SETUP_INVALID", "账本、月份、切换点和货币须为字符串", f"{setup}:1")
    try:
        cutover = date.fromisoformat(doc["cutover"])
    except ValueError as exc:
        raise BudgetError("CUTOVER_INVALID", "cutover 须为 YYYY-MM-DD", yaml_path_location(setup, ("cutover",))) from exc
    defaults = doc["defaults"]
    require(type(defaults) is dict and set(defaults) == {
        "income_purpose", "expense_purpose", "expense_category", "confirmed"},
        "SETUP_INVALID", "defaults 字段无效", yaml_path_location(setup, ("defaults",)))
    require(all(type(defaults[key]) is str and defaults[key].strip() for key in
                ("income_purpose", "expense_purpose", "expense_category")),
            "SETUP_INVALID", "默认用途和类别须为非空名称", yaml_path_location(setup, ("defaults",)))
    for key in ("income_purpose", "expense_purpose", "expense_category"):
        try:
            if key == "expense_category":
                _name(defaults[key])
            else:
                _owners([{"id": "default", "name": defaults[key], "aliases": []}])
        except BudgetError as exc:
            raise BudgetError(exc.code, exc.message, yaml_path_location(setup, ("defaults", key))) from exc
    issues = []

    def pending(keys, detail):
        issues.append({"reason_code": "SETUP_DECISION_REQUIRED", "detail": detail,
                       "source": yaml_path_location(setup, keys)})

    def money(raw, keys, allow_not_assessed=False, native=False):
        if allow_not_assessed and raw == "not_assessed":
            return raw
        if type(raw) is int and raw >= 0:
            return str(raw)
        if type(raw) is str and (NATIVE if native else MONEY).fullmatch(raw):
            return raw
        pending(keys, "填写非负金额（例如 '2000'）" + ("，或 not_assessed" if allow_not_assessed else ""))
        return None

    with _inputs((setup.parent / doc["ledger"]).resolve(), doc["month"], cutover) as (fin, last):
        require(doc["report_currency"] in fin.options.get("operating_currency", []),
                "REPORT_CURRENCY_INVALID", "报告货币须来自 operating_currency", yaml_path_location(setup, ("report_currency",)))
        rows, purposes, category_names, expense_names, source_issues = _inspect(
            fin, cutover, defaults["income_purpose"], defaults["expense_purpose"], defaults["expense_category"])
        issues.extend(source_issues)
        if defaults["confirmed"] is not True:
            pending(("defaults", "confirmed"), "确认未标标签时使用这些默认用途和类别后填 true")
        if doc["completeness_confirmed"] is not True:
            pending(("completeness_confirmed",), f"确认截至 {last} 的账本资料及责任完整后填 true")
        require(type(doc["accounts"]) is list, "SETUP_INVALID", "accounts 须为列表", yaml_path_location(setup, ("accounts",)))
        by_account = {row["account"]: row for row in rows}
        seen = set()
        account_claims, h_openings, k_openings = [], [], []
        for index, row in enumerate(doc["accounts"]):
            keys = ("accounts", index)
            require(type(row) is dict and set(row) == {"account", "semantics", "payment_scope", "opening", "reason"},
                    "SETUP_INVALID", "账户字段无效", yaml_path_location(setup, keys))
            account = row["account"]
            require(type(account) is str and account in by_account and account not in seen,
                    "SETUP_ACCOUNT_INVALID", "账户不存在或重复，请重新盘点源账本", yaml_path_location(setup, (*keys, "account")))
            seen.add(account)
            original = by_account[account]
            mode = row["semantics"]
            if mode not in ("H", "K"):
                pending((*keys, "semantics"), f"{account}：选择 H（真实余额）或 K（累计申赎）")
            scope = row["payment_scope"]
            require(type(scope) is dict and set(scope) == set(original["commodities"]),
                    "SETUP_ACCOUNT_INVALID", "支付范围的币种须与当前账本一致", yaml_path_location(setup, (*keys, "payment_scope")))
            for commodity, value in scope.items():
                if value not in ("payable", "excluded", "pending") or mode == "K" and value != "excluded":
                    pending((*keys, "payment_scope", commodity), "选择 payable／excluded／pending；K 只能 excluded")
            if type(row["reason"]) is not str or not row["reason"].strip():
                pending((*keys, "reason"), "填写账户语义、支付范围及期初判断依据")
            expected = {(c["commodity"], c["lot"]): c["signed_units"] for c in original["opening_coordinates"]
                        if Fraction(c["signed_units"]) != 0}
            require(type(row["opening"]) is list, "SETUP_INVALID", "opening 须为列表", yaml_path_location(setup, (*keys, "opening")))
            visited = set()
            for number, coordinate in enumerate(row["opening"]):
                ck = (*keys, "opening", number)
                require(type(coordinate) is dict and set(coordinate) == {"commodity", "lot", "signed_units", "owners"},
                        "SETUP_INVALID", "期初坐标字段无效", yaml_path_location(setup, ck))
                require(all(type(coordinate[key]) is str for key in ("commodity", "lot", "signed_units")),
                        "SETUP_INVALID", "期初币种、lot 和数量须保留原字符串", yaml_path_location(setup, ck))
                pair = (coordinate["commodity"], coordinate["lot"])
                require(pair in expected and pair not in visited and coordinate["signed_units"] == expected[pair],
                        "SETUP_OPENING_CHANGED", "期初坐标或金额与账本不符，请重新盘点", yaml_path_location(setup, ck))
                visited.add(pair)
                owners = coordinate["owners"]
                require(type(owners) is dict, "SETUP_INVALID", "owners 须为用途到数量的映射", yaml_path_location(setup, (*ck, "owners")))
                if mode == "H":
                    total = Fraction(0)
                    if not owners:
                        pending((*ck, "owners"), "按用途拆分这份真实期初余额")
                    for owner, raw in owners.items():
                        require(type(owner) is str and owner.strip(), "SETUP_INVALID", "用途名称无效", yaml_path_location(setup, (*ck, "owners")))
                        if owner != "-":
                            try:
                                _owners([{"id": "opening", "name": owner, "aliases": []}])
                            except BudgetError as exc:
                                raise BudgetError(exc.code, exc.message,
                                                  yaml_path_location(setup, (*ck, "owners", owner))) from exc
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
            require(visited == set(expected), "SETUP_OPENING_CHANGED", "填写表遗漏了账本期初坐标", yaml_path_location(setup, (*keys, "opening")))
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
                "SETUP_INVALID", "目标须为名称到金额的映射", f"{setup}:1")
        require(all(type(name) is str and bool(name.strip()) for name in
                    list(doc["expense_targets"]) + list(doc["allocation_targets"])),
                "SETUP_INVALID", "目标名称须为非空字符串", f"{setup}:1")
        for name in doc["expense_targets"]:
            try:
                category_names.add(_name(name))
            except BudgetError as exc:
                raise BudgetError(exc.code, exc.message, yaml_path_location(setup, ("expense_targets", name))) from exc
        for name in doc["allocation_targets"]:
            try:
                _owners([{"id": "allocation", "name": name, "aliases": []}])
            except BudgetError as exc:
                raise BudgetError(exc.code, exc.message, yaml_path_location(setup, ("allocation_targets", name))) from exc
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
            return {"status": "NEEDS_REVIEW", "setup": str(setup), "review_items": issues, "config": None}
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
        files["month.yaml"] = {"schema": 6, "ledger": _ledger_reference(fin.root, output), "cutover": cutover.isoformat(),
            "month": doc["month"], "report_currency": doc["report_currency"],
            **{name.removesuffix(".yaml"): name for name in files}}
    _publish(output, files)
    return {"status": "CONFIG_CREATED", "config": str(output / "month.yaml"),
            "review_items": [], "note": "只编译已填写的首轮决定；运行月报后仍可能出现交易、责任或计价待审。"}


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--ledger", type=Path)
    action.add_argument("--apply", type=Path, help="检查已填写 setup.yaml 并编译月报配置")
    parser.add_argument("--month")
    parser.add_argument("--cutover", type=date.fromisoformat)
    parser.add_argument("--currency")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--income-purpose", default="已收入资金")
    parser.add_argument("--expense-purpose", default="待支出资金")
    parser.add_argument("--expense-category")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if args.ledger and (not args.month or not args.output):
        parser.error("生成草稿须提供 --month 和 --output")
    if args.apply and (args.month or args.cutover or args.currency or args.expense_category or
                       args.income_purpose != "已收入资金" or args.expense_purpose != "待支出资金"):
        parser.error("--apply 时请在 setup.yaml 内修改月份、默认科目和货币")
    try:
        result = compile_setup(args.apply, args.output) if args.apply else create_setup(
            args.ledger, args.month, args.output, cutover=args.cutover, currency=args.currency,
            income_purpose=args.income_purpose, expense_purpose=args.expense_purpose,
            expense_category=args.expense_category)
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message, "source": exc.source}},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as exc:
        print(json.dumps({"error": {"code": "SETUP_IO_ERROR", "message": str(exc),
                                    "source": str(args.apply or args.output)}}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["status"])
        for item in result.get("review_items", result.get("source_issues", [])):
            print(f"  {item['detail']} [{item['source']}]")
        if "setup" in result:
            print(f"填写表：{result['setup']}")
            print(f"检查并生成配置：{command('beancount-budget-init')} --apply \"{result['setup']}\"")
        if result.get("config"):
            print(f"查看月报：{command('beancount-budget-tool')} --config \"{result['config']}\"")
        print(result.get("note", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

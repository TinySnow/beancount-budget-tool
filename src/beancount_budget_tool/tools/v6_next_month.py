"""Prepare the next calendar month while retaining the original cutover and reviews."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
from fractions import Fraction
import json
from pathlib import Path
import sys

from beancount.core import data

from ..console import command, configure_output
from ..errors import BudgetError, require
from ..yamlstrict import load_yaml, yaml_path_location
from ..v6.accounts import audit_accounts
from ..v6.classification import _name, _registry
from ..v6.monthly_report import audit_month_workflow, render_workflow_text
from ..v6.ownership import _owners
from ..v6.purpose_targets import load_purpose_targets
from ..v6.targets import DECIMAL, load_expense_targets
from .v6_check_migration_month import _month
from .v6_init import _inputs, _ledger_reference, _publish
from .v6_review_decisions import SCALARS, _config, _unchanged


DATED_INPUTS = {"completeness", "debt_coverage", "debt_settlements"}
FIELDS = {"schema", "config", "month", "cutover", "report_currency", "account_changes",
          "expense_targets", "expense_subtree_targets", "allocation_targets", "targets_confirmed",
          "completeness_confirmed", "completeness_reason"}


def _next(month: str) -> str:
    first, _ = _month(month)
    try:
        return date(first.year + (first.month == 12), first.month % 12 + 1, 1).isoformat()[:7]
    except ValueError as exc:
        raise BudgetError("TIME_INVALID", "无法准备超出日期范围的下一月") from exc


def _decimal(raw):
    """Plans accept decimal strings; never round their exact expanded fractions."""
    if raw is None or raw == "not_assessed":
        return raw
    value = Fraction(raw)
    denominator = value.denominator
    counts = []
    for factor in (2, 5):
        count = 0
        while denominator % factor == 0:
            denominator //= factor
            count += 1
        counts.append(count)
    require(denominator == 1 and value >= 0, "PLAN_AMOUNT_INVALID", "目标不能转换为精确非负十进制")
    scale = max(counts)
    units = value.numerator * 2 ** (scale - counts[0]) * 5 ** (scale - counts[1])
    if not scale:
        return str(units)
    digits = str(units).zfill(scale + 1)
    return (digits[:-scale] + "." + digits[-scale:]).rstrip("0").rstrip(".")


def _account_changes(fin, cutover: date, original: dict) -> list[dict]:
    rows = audit_accounts(fin.root, cutover)["accounts"]
    known = {row["account"]: row for row in original["accounts"]}
    sources = {}
    for entry in fin.entries:
        if isinstance(entry, data.Transaction):
            for posting in entry.postings:
                meta = posting.meta or entry.meta
                sources.setdefault((posting.account, posting.units.currency),
                                   f"{meta.get('filename')}:{meta.get('lineno')}")
    changes = []
    for row in rows:
        previous = known.get(row["account"])
        scope = previous["payment_scope"] if previous else {}
        missing = [commodity for commodity in row["commodities"]
                   if commodity not in scope or scope[commodity] == "pending"]
        if previous and not missing:
            continue
        changes.append({"account": row["account"],
                        "first_source": sources[(row["account"], missing[0])],
                        "semantics": previous["semantics"] if previous else None,
                        "payment_scope": {commodity: None for commodity in missing}, "reason": None})
    return changes


def _proposals(paths, doc, month):
    categories = load_yaml(paths["classifications"])["categories"]
    owners = load_yaml(paths["ownership"])["owners"]
    _registry(categories)
    _owners(owners)
    category_names = {row["id"]: _name(row["name"]) for row in categories}
    owner_names = {row["id"]: row["name"] for row in owners}
    expense = load_expense_targets(paths["expense_plans"], month, doc["report_currency"], categories)
    expense_from = month
    if not expense["targets"] and not expense["subtree_targets"]:
        expense = load_expense_targets(paths["expense_plans"], doc["month"], doc["report_currency"], categories)
        expense_from = doc["month"]
    direct = {category_names[key]: _decimal(value) for key, value in expense["targets"].items()}
    subtree = {category_names[key]: _decimal(value) for key, value in expense["subtree_targets"].items()}
    if expense["spendable_cap_category_id"]:
        subtree.setdefault(category_names[expense["spendable_cap_category_id"]], None)
    purpose = load_purpose_targets(paths["purpose_plans"], month, doc["report_currency"], set(owner_names))
    previous = load_purpose_targets(paths["purpose_plans"], doc["month"], doc["report_currency"], set(owner_names))
    old = {row["owner_id"]: row for row in previous["owners"]}
    allocations, sources = {}, {}
    for row in purpose["owners"]:
        ident = row["owner_id"]
        sources[owner_names[ident]] = month if row["status"] != "MISSING" else doc["month"]
        chosen = row if row["status"] != "MISSING" else old[ident]
        allocations[owner_names[ident]] = ("not_assessed" if chosen["status"] == "NOT_ASSESSED"
                                          else _decimal(chosen["target"]))
    return direct, subtree, allocations, {"expense_proposal_from": expense_from,
                                          "allocation_proposal_from": sources}


def create_next_month(config: Path, output: Path, *, expected_inputs: dict[Path, bytes] | None = None) -> dict:
    config, output = config.resolve(), output.resolve()
    if expected_inputs is not None:
        _unchanged(expected_inputs)
    doc, paths, cutover = _config(config)
    month = _next(doc["month"])
    before = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    with _inputs(paths["ledger"], doc["month"], cutover):
        audit_accounts(paths["ledger"], cutover, paths["accounts"])
    with _inputs(paths["ledger"], month, cutover) as (fin, last):
        changes = _account_changes(fin, cutover, load_yaml(paths["accounts"]))
        direct, subtree, allocations, proposal_sources = _proposals(paths, doc, month)
        worksheet = {"schema": 6, "config": _ledger_reference(config, output),
                     "month": month, "cutover": doc["cutover"], "report_currency": doc["report_currency"],
                     "account_changes": changes, "expense_targets": direct,
                     "expense_subtree_targets": subtree, "allocation_targets": allocations,
                     "targets_confirmed": False, "completeness_confirmed": False, "completeness_reason": None}
        transactions = sum(isinstance(entry, data.Transaction) and entry.date >= _month(month)[0]
                           for entry in fin.entries)
        notes = {
            "previous_month": doc["month"], "month": month, "cutover": doc["cutover"],
            "booked_transactions_in_month": transactions, **proposal_sources,
            "not_reused": {key: str(paths[key]) for key in sorted(DATED_INPUTS & paths.keys())},
            "note": "目标仅为待确认提议；期初与用途沿原 cutover 重放。上月的报告日声明、债务覆盖及外币报价不作新月证据。"}
        instructions = (
            f"# 准备 {month}：只填写 next-month.yaml\n\n"
            f"原 cutover 仍为 {doc['cutover']}，不会把账户余额重置或新增一笔期初资金。\n\n"
            "1. 核对 expense_targets（直接费用目标）、expense_subtree_targets（含子类别的总上限）"
            "和 allocation_targets（用途划拨目标），再把 targets_confirmed 改为 true。"
            "金额填带引号的非负数字；消费及用途目标都可填 not_assessed 表示本月明确不考核，不能把缺目标当作零。"
            "优先提取新月已写的模板／目标；缺失处参考上月作提议，来源月份见 preparation.yaml。\n"
            "2. account_changes 只列新账户、新币种或原来未确定的支付范围。"
            "semantics 选 H（真实库存）或 K（累计申赎）；每个币种选 payable 或 excluded，K 必须 excluded；reason 写判断依据。"
            "已审账户语义不能在这张表中改为另一种。\n"
            f"3. 确认 importer 已覆盖至 {last}，截至该日没有未入账责任或在途付款后，"
            "completeness_confirmed 填 true，并在 completeness_reason 写核对依据。"
            "尚未核对时保持 false，也可先生成配置审交易；月报会继续提示月末声明待审。\n\n"
            f"```\n{command('beancount-budget-next-month')} --apply next-month.yaml\n```\n\n"
            f"生成 compiled/month.yaml 并自动运行本月检查。之后用该配置运行月报或导出 {command('beancount-budget-review')} 待审表。"
            "原账本和原审阅文件只读，输出不覆盖；已有 compiled 时用 --output 指定新目录。\n\n"
            "如果先审交易、后来才确认月末，请以交易审阅后最新的 month.yaml 运行下列命令，"
            "保留本月已有决定，不必重新编译原填写表：\n\n"
            f"```\n{command('beancount-budget-next-month')} --confirm-completeness path/to/latest/month.yaml "
            "--reason '已核对截至本月末全部账单，无漏记责任或在途付款' --output closed\n```\n\n"
            "上月完整性声明、专项卡债覆盖和外币清偿报价不自动沿用。"
            "未清债务、其他责任及新月交易仍需按月报提示审阅；原有交易认领和 cutover 期初继续复用。"
            "本月零交易时，请检查账单是否真的导入；生成配置不是资料完整或月报签发。\n")
    _unchanged(before)
    if expected_inputs is not None:
        _unchanged(expected_inputs)
    _publish(output, {"next-month.yaml": worksheet, "preparation.yaml": notes, "START_HERE.md": instructions})
    return {"status": "DRAFT_CREATED", "month": month, "worksheet": str(output / "next-month.yaml"),
            "changed_accounts": len(changes), "booked_transactions_in_month": transactions}


def compile_next_month(worksheet: Path, output: Path | None = None, *, include_transactions: bool = False,
                       expected_inputs: dict[Path, bytes] | None = None) -> dict:
    worksheet = worksheet.resolve()
    output = (output or worksheet.parent / "compiled").resolve()
    if expected_inputs is not None:
        _unchanged(expected_inputs)
    proposal = load_yaml(worksheet)
    require(type(proposal) is dict and set(proposal) == FIELDS and
            type(proposal["schema"]) is int and proposal["schema"] == 6 and
            type(proposal["config"]) is str and bool(proposal["config"].strip()),
            "NEXT_MONTH_INVALID", "新月填写表字段或版本无效", f"{worksheet}:1")
    config = (worksheet.parent / proposal["config"]).resolve()
    doc, paths, cutover = _config(config)
    month = _next(doc["month"])
    require(proposal["month"] == month and proposal["cutover"] == doc["cutover"] and
            proposal["report_currency"] == doc["report_currency"], "NEXT_MONTH_CONTEXT_CHANGED",
            "月份、cutover 或报告货币与原配置不符；请重新准备下一月", str(worksheet))
    before = {path: path.read_bytes() for path in {worksheet, config, *paths.values()} if path != paths["ledger"]}
    issues = []

    def pending(keys, detail):
        issues.append({"reason_code": "NEXT_MONTH_DECISION_REQUIRED", "detail": detail,
                       "source": yaml_path_location(worksheet, keys)})

    with _inputs(paths["ledger"], doc["month"], cutover):
        audit_accounts(paths["ledger"], cutover, paths["accounts"])
    with _inputs(paths["ledger"], month, cutover) as (fin, last):
        account_doc = deepcopy(load_yaml(paths["accounts"]))
        expected = {row["account"]: row for row in _account_changes(fin, cutover, account_doc)}
        known = {row["account"]: row for row in account_doc["accounts"]}
        require(type(proposal["account_changes"]) is list, "NEXT_MONTH_INVALID", "account_changes 须为列表",
                yaml_path_location(worksheet, ("account_changes",)))
        seen = set()
        for index, row in enumerate(proposal["account_changes"]):
            keys = ("account_changes", index)
            require(type(row) is dict and set(row) == {
                "account", "first_source", "semantics", "payment_scope", "reason"} and type(row["account"]) is str,
                "NEXT_MONTH_INVALID", "账户变更项字段无效", yaml_path_location(worksheet, keys))
            name = row["account"]
            require(name in expected and name not in seen, "NEXT_MONTH_ACCOUNTS_CHANGED",
                    "账户变更与当前来源不符或重复；请重新准备", yaml_path_location(worksheet, keys))
            seen.add(name)
            mode = row["semantics"]
            if name in known:
                require(mode == known[name]["semantics"], "NEXT_MONTH_SEMANTICS_CHANGED",
                        "不能在月度准备中改变已审账户语义", yaml_path_location(worksheet, (*keys, "semantics")))
            if mode not in ("H", "K"):
                pending((*keys, "semantics"), "确认该账户是 H 真实库存还是 K 累计申赎")
            scope = row["payment_scope"]
            require(type(scope) is dict and set(scope) == set(expected[name]["payment_scope"]),
                    "NEXT_MONTH_ACCOUNTS_CHANGED", "待审币种与当前来源不符；请重新准备",
                    yaml_path_location(worksheet, (*keys, "payment_scope")))
            for currency, value in scope.items():
                if value not in ("payable", "excluded") or mode == "K" and value != "excluded":
                    pending((*keys, "payment_scope", currency), "确认 payable 或 excluded；K 必须 excluded")
            if type(row["reason"]) is not str or not row["reason"].strip():
                pending((*keys, "reason"), "填写新账户／币种支付范围的核对依据")
            if name not in known:
                account_doc["accounts"].append({"account": name, "semantics": mode,
                    "balance_semantics": "real_inventory" if mode == "H" else "signed_subscription_flow",
                    "payment_scope": scope, "reason": row["reason"]})
            else:
                known[name]["payment_scope"].update(scope)
                if type(row["reason"]) is str:
                    known[name]["reason"] += "；新月补审：" + row["reason"]
        if seen != set(expected):
            pending(("account_changes",), "账本出现新的账户／币种；请重新准备并核对变化")
        categories = load_yaml(paths["classifications"])["categories"]
        category_ids = _registry(categories)
        owner_ids = _owners(load_yaml(paths["ownership"])["owners"])

        def amounts(field, registry, allow_not_assessed=False):
            values = proposal[field]
            require(type(values) is dict, "NEXT_MONTH_INVALID", "目标须为名称到金额的映射",
                    yaml_path_location(worksheet, (field,)))
            result = {}
            for name, raw in values.items():
                require(type(name) is str and name in registry, "NEXT_MONTH_NAME_UNKNOWN",
                        "目标名称未登记于原配置", yaml_path_location(worksheet, (field, str(name))))
                ident = registry[name]
                require(ident not in result, "NEXT_MONTH_TARGET_DUPLICATE", "同一目标不能通过名称和别名重复填写",
                        yaml_path_location(worksheet, (field, name)))
                if type(raw) is int and raw >= 0:
                    raw = str(raw)
                if type(raw) is str and (DECIMAL.fullmatch(raw) or allow_not_assessed and raw == "not_assessed"):
                    result[ident] = raw
                else:
                    pending((field, name), "填写非负十进制目标" + ("，或 not_assessed" if allow_not_assessed else ""))
            return result

        direct = amounts("expense_targets", category_ids, True)
        subtree = amounts("expense_subtree_targets", category_ids, True)
        allocations = amounts("allocation_targets", owner_ids, True)
        for owner in set(owner_ids.values()) - set(allocations):
            pending(("allocation_targets",), f"用途 {owner} 缺少本月目标或 not_assessed")
        expense_doc = deepcopy(load_yaml(paths["expense_plans"]))
        purpose_doc = deepcopy(load_yaml(paths["purpose_plans"]))
        cap = expense_doc.get("spendable_cap_category_id")
        if cap and cap not in subtree:
            pending(("expense_subtree_targets",), "最多能花的上限类别须有明确子树消费目标，或填 not_assessed 表示本月不考核")
        expanded = load_expense_targets(paths["expense_plans"], month, doc["report_currency"], categories)
        for field, declared, selected in (("expense_targets", expanded["targets"], direct),
                                           ("expense_subtree_targets", expanded["subtree_targets"], subtree)):
            if set(declared) - set(selected):
                pending((field,), "原计划已为本月声明这些目标，不能仅删除提议项；如需撤销，请先调整原计划再重新准备")
        if proposal["targets_confirmed"] is not True:
            pending(("targets_confirmed",), "核对本月目标后填 true；提议不会自动成为本月预算")
        require(type(proposal["completeness_confirmed"]) is bool, "NEXT_MONTH_INVALID",
                "completeness_confirmed 须为 true 或 false", yaml_path_location(worksheet, ("completeness_confirmed",)))
        if proposal["completeness_confirmed"] and (
                type(proposal["completeness_reason"]) is not str or not proposal["completeness_reason"].strip()):
            pending(("completeness_reason",), "填写截至本月末账单、责任及在途付款完整性的核对依据")
        if issues:
            _unchanged(before)
            if expected_inputs is not None:
                _unchanged(expected_inputs)
            return {"status": "NEEDS_REVIEW", "config": None, "month": month, "review_items": issues}
        # Validate all preserved template layers, not just the entered amounts.
        load_purpose_targets(paths["purpose_plans"], month, doc["report_currency"], set(owner_ids.values()))
        expense_doc.setdefault("months", {})[month] = {"expense_targets": direct, "expense_subtree_targets": subtree}
        purpose_doc.setdefault("months", {})[month] = {"allocation_targets": allocations}
        files = {"accounts.yaml": account_doc, "expense_plans.yaml": expense_doc, "purpose_plans.yaml": purpose_doc}
        # A newly active, reviewed K account may have a provable zero at the
        # original cutover. Acknowledge that book-flow zero, never new H money.
        opening_doc = deepcopy(load_yaml(paths["opening"]))
        reviewed_k = {(row["account"], row["commodity"], row["lot"]) for row in opening_doc["k_openings"]}
        account_rows = {row["account"]: row for row in account_doc["accounts"]}
        for row in audit_accounts(fin.root, cutover)["accounts"]:
            approved = account_rows.get(row["account"], {})
            if (approved.get("semantics") != "K" or row["commodities"] != [doc["report_currency"]]
                    or row["has_cost"] or row["has_price"] or row["source_unavailable"]):
                continue
            coordinates = row["opening_coordinates"] or [{"commodity": doc["report_currency"],
                "lot": f"{doc['report_currency']}/no-cost", "signed_units": "0"}]
            for coordinate in coordinates:
                key = (row["account"], coordinate["commodity"], coordinate["lot"])
                if key not in reviewed_k and Fraction(coordinate["signed_units"]) == 0:
                    opening_doc["k_openings"].append({"account": row["account"], **coordinate,
                        "reason": "已审 K 账户在原 cutover 的账面累计为零；" + approved["reason"]})
                    reviewed_k.add(key)
        if opening_doc != load_yaml(paths["opening"]):
            files["opening.yaml"] = opening_doc
        reused = {key: path for key, path in paths.items() if key not in DATED_INPUTS}
        candidate = {**{key: value for key, value in doc.items() if key in SCALARS}, "month": month,
                     **{key: _ledger_reference(path, output) for key, path in reused.items()},
                     **{name.removesuffix(".yaml"): name for name in files}}
        if proposal["completeness_confirmed"]:
            files["completeness.yaml"] = {"schema": 6, "cutover": doc["cutover"], "as_of": last.isoformat(),
                "unbooked_obligations": "none", "inflight_payments": "none", "reason": proposal["completeness_reason"]}
            candidate["completeness"] = "completeness.yaml"
        files["month.yaml"] = candidate
    _unchanged(before)
    if expected_inputs is not None:
        _unchanged(expected_inputs)
    _publish(output, files)
    result = {"status": "MONTH_PREPARED", "month": month, "config": str(output / "month.yaml"), "review_items": []}
    try:
        workflow = (audit_month_workflow(output / "month.yaml", include_transactions=True) if include_transactions else
                    audit_month_workflow(output / "month.yaml"))
        _unchanged(before)
        if expected_inputs is not None:
            _unchanged(expected_inputs)
        return {**result, "workflow": workflow}
    except (BudgetError, OSError, UnicodeError) as exc:
        return {**result, "workflow": None,
                "report_error": {"code": exc.code if isinstance(exc, BudgetError) else "NEXT_MONTH_IO_ERROR",
                                 "message": exc.message if isinstance(exc, BudgetError) else str(exc),
                                 "source": exc.source if isinstance(exc, BudgetError) else str(output / "month.yaml")}}


def confirm_month(config: Path, reason: str, output: Path, *, include_transactions: bool = False,
                  expected_inputs: dict[Path, bytes] | None = None) -> dict:
    """Attach a new month-end assertion to the latest reviewed config."""
    config, output = config.resolve(), output.resolve()
    if expected_inputs is not None:
        _unchanged(expected_inputs)
    doc, paths, _ = _config(config)
    require(type(reason) is str and bool(reason.strip()), "NEXT_MONTH_REASON_REQUIRED",
            "请写明截至该月末资料、责任及在途付款完整性的核对依据", str(config))
    _, last = _month(doc["month"])
    before = {path: path.read_bytes() for path in {config, *paths.values()} if path != paths["ledger"]}
    candidate = {**doc, **{key: _ledger_reference(path, output) for key, path in paths.items()},
                 "completeness": "completeness.yaml"}
    statement = {"schema": 6, "cutover": doc["cutover"], "as_of": last.isoformat(),
                 "unbooked_obligations": "none", "inflight_payments": "none", "reason": reason}
    _unchanged(before)
    if expected_inputs is not None:
        _unchanged(expected_inputs)
    _publish(output, {"month.yaml": candidate, "completeness.yaml": statement})
    result = {"status": "COMPLETENESS_SAVED", "month": doc["month"],
              "config": str(output / "month.yaml"), "review_items": []}
    try:
        workflow = (audit_month_workflow(output / "month.yaml", include_transactions=True) if include_transactions else
                    audit_month_workflow(output / "month.yaml"))
        _unchanged(before)
        if expected_inputs is not None:
            _unchanged(expected_inputs)
        return {**result, "workflow": workflow}
    except (BudgetError, OSError, UnicodeError) as exc:
        return {**result, "workflow": None,
                "report_error": {"code": exc.code if isinstance(exc, BudgetError) else "NEXT_MONTH_IO_ERROR",
                                 "message": exc.message if isinstance(exc, BudgetError) else str(exc),
                                 "source": exc.source if isinstance(exc, BudgetError) else str(output / "month.yaml")}}


def main(argv=None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--config", type=Path, help="以此月配置准备紧接的下一月")
    action.add_argument("--apply", type=Path, help="核对并保存 next-month.yaml")
    action.add_argument("--confirm-completeness", type=Path, nargs="?", const=True, metavar="CONFIG",
                        help="确认月末没有未入账责任或在途付款；使用 --project 时可省略 CONFIG")
    parser.add_argument("--project", type=Path, help="使用项目最新月份；保存新配置后自动更新入口")
    parser.add_argument("--month", help="项目中的原月份 YYYY-MM；省略时使用默认月份，保存填写表时不使用")
    parser.add_argument("--reason", help="本月末资料完整性的核对依据，仅用于 --confirm-completeness")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    if not (args.config or args.apply or args.confirm_completeness or args.project):
        parser.error("请提供 --project、--config、--apply 或 --confirm-completeness")
    if args.month and (not args.project or args.apply):
        parser.error("--month 须与 --project 一起准备或确认月份；保存时按填写表引用的原月份")
    if args.confirm_completeness is True and not args.project:
        parser.error("省略 --confirm-completeness 的 CONFIG 时须提供 --project")
    if not args.apply and args.output is None:
        parser.error("准备下一月或保存月末声明须提供 --output 新目录")
    if bool(args.confirm_completeness) != bool(args.reason):
        parser.error("--confirm-completeness 必须同时提供 --reason，其他操作不使用 --reason")
    try:
        context = None
        if args.project:
            from ..v6.project_actions import prepare_project_action
            base = args.confirm_completeness if isinstance(args.confirm_completeness, Path) else args.config
            context = prepare_project_action(args.project, config=base, worksheet=args.apply,
                                             month=args.month, successor=not bool(args.confirm_completeness))
        config = context.config if context else args.config or args.confirm_completeness
        guard = {"expected_inputs": context.inputs} if context else {}
        if args.confirm_completeness:
            result = confirm_month(config, args.reason, args.output, **guard)
        else:
            result = compile_next_month(args.apply, args.output, **guard) if args.apply else create_next_month(config, args.output, **guard)
        if context:
            result = context.finish(result)
    except (BudgetError, OSError, UnicodeError) as exc:
        error = {"code": exc.code, "message": exc.message, "source": exc.source} if isinstance(exc, BudgetError) else {
            "code": "NEXT_MONTH_IO_ERROR", "message": str(exc),
            "source": str(args.apply or args.config or args.project or args.confirm_completeness)}
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif "worksheet" in result:
        print(f"下一月 {result['month']} 填写表：{result['worksheet']}\n"
              f"待确认账户／币种 {result['changed_accounts']} 户，本月已记交易 {result['booked_transactions_in_month']} 条。")
        if not result["booked_transactions_in_month"]:
            print("本月没有已记交易，请核对 importer 是否已覆盖；生成草稿不代表资料完整。")
    elif result["config"] is None:
        print(f"{result['month']} 尚待确认，未生成配置：")
        for item in result["review_items"]:
            print(f"  {item['source']}：{item['detail']}")
    else:
        print(f"已保存配置：{result['config']}")
        if result.get("report_error"):
            error = result["report_error"]
            print(f"配置已保存，月报尚未通过：{error['code']} {error['message']} ({error['source']})")
        else:
            print(render_workflow_text(result["workflow"]))
    if args.format != "json":
        from ..v6.project_actions import print_project_result
        print_project_result(result)
    return 2 if result.get("report_error") or result.get("project_error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

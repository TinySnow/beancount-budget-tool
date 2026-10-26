"""One-command, explicitly scoped report over independently certified v6 audits."""

from __future__ import annotations

import argparse
import calendar
from datetime import date
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import sys

from ..errors import BudgetError, require
from ..exact import exact_text, amount_text
from ..financial import booked_through, frozen_financial, load_financial, financial_session
from ..yamlstrict import load_yaml, yaml_key_location
from .actual_snapshot import audit_actual_snapshot
from .default_outflows import audit_default_outflows
from .classification import _key
from .facts import frozen_fact_indices
from .holdings import audit_h_holdings
from .k_book_purposes import audit_k_book_purposes
from .metric_review import enrich_metric_reviews
from .purpose_balances import summarize_purpose_balances
from .purpose_pools import audit_purpose_pools
from .purpose_progress import audit_monthly_purpose_progress
from .review_queue import audit_review_queue, render_text as render_review_queue
from .review_diagnostics import posting_sources, annotate_source_items
from .targets import _month, audit_monthly_expense_targets
from .tracking import audit_monthly_tracking


REQUIRED = {"schema", "ledger", "cutover", "month", "report_currency",
            "accounts", "opening", "ownership", "obligations", "classifications",
            "expense_plans", "purpose_plans", "h_transfers", "k_events", "h_fx",
            "purpose_pools"}
OPTIONAL = {"completeness", "benefit_groups", "debt_coverage", "debt_settlements", "salary_cycle"}


def _config_month(document: dict, config: Path) -> str:
    try:
        return _month(document.get("month"))
    except BudgetError as exc:
        raise BudgetError(exc.code, exc.message, yaml_key_location(config, "month")) from exc


def summarize_account_purposes(holdings: dict, pools: dict) -> dict:
    """Show reviewed H purpose units by account; pool accounts use their overlay."""
    require(holdings["ledger_snapshot"] == pools["ledger_snapshot"] and
            holdings["as_of"] == pools["as_of"],
            "MONTH_REPORT_MISMATCH", "资产用途和证券池不属于同一账本时点")
    pool_accounts = {account for pool in pools["pools"] for account in pool["accounts"]}
    totals = {}
    pending = []
    for position in holdings["positions"]:
        if position["account"] in pool_accounts:
            continue
        if position["status"] != "CERTIFIED":
            pending.append({"account": position["account"],
                            "commodity": position["commodity"], "lot": position["lot"],
                            "reasons": position["reasons"]})
            continue
        for slice_ in position["owner_units"]:
            key = (position["account"], position["commodity"], slice_["owner_id"])
            totals[key] = totals.get(key, Fraction(0)) + Fraction(slice_["units"])
    rows = [{"account": account, "commodity": commodity, "owner_id": owner,
             "units": exact_text(units)}
            for (account, commodity, owner), units in sorted(totals.items()) if units]
    negative_positions = [row for row in rows
                          if row["owner_id"] != "-" and Fraction(row["units"]) < 0]
    return {"status": "NEEDS_REVIEW" if pending else "CERTIFIED_H_OUTSIDE_POOLS",
            "rows": rows, "pending": pending,
            "negative_purpose_positions": negative_positions,
            "note": "仅为 H 账户原币用途份额；证券池单独按账面成本展示，K 与市值不在此合计"}


def summarize_max_spendable(actual: dict, expense: dict) -> dict:
    """Cap certified payable living funds by a declared consumption subtree goal."""
    category_id = expense.get("spendable_cap_category_id")
    if category_id is None:
        return {"status": "NOT_CONFIGURED", "category_id": None,
                "max_spendable": None, "reason_codes": ["SPENDABLE_CAP_NOT_CONFIGURED"]}
    category = next((row for row in expense["categories"] if row["id"] == category_id), None)
    require(category is not None, "MONTH_REPORT_MISMATCH", "最多能花的上限类别不在消费报表中")
    if category["subtree_remaining_status"] == "NOT_ASSESSED":
        return {"status": "NOT_ASSESSED", "category_id": category_id,
                "category_name": category["name"], "max_spendable": None,
                "reason_codes": ["SPENDABLE_CAP_NOT_ASSESSED"],
                "note": "本月明确不考核消费上限，不计算最多能花；用途现金可支付另行审计。"}
    if category["subtree_target_kind"] != "explicit":
        return {"status": "NEEDS_REVIEW", "category_id": category_id,
                "max_spendable": None, "reason_codes": ["SPENDABLE_CAP_TARGET_MISSING"]}
    if (actual["status"] != "CERTIFIED" or expense["status"] != "CERTIFIED" or
            category["subtree_remaining_status"] != "CERTIFIED"):
        return {"status": "NEEDS_REVIEW", "category_id": category_id,
                "max_spendable": None, "reason_codes": sorted(set(
                    actual.get("reason_codes", []) + expense.get("reason_codes", []) +
                    (["SPENDABLE_CAP_PENDING"] if category["subtree_remaining_status"] != "CERTIFIED" else [])))}
    payable = Fraction(actual["actual_spendable"])
    remaining = Fraction(category["subtree_remaining"])
    return {"status": "CERTIFIED", "category_id": category_id,
            "category_name": category["name"],
            "payable_after_obligations": exact_text(payable),
            "consumption_target_remaining": exact_text(remaining),
            "max_spendable": exact_text(min(payable, remaining)),
            "reason_codes": [],
            "note": "最多能花取已审可支付能力与指定消费目标子树差额的较小值；负数表示超支或资金缺口"}


def audit_reviewed_month(config: Path, *, include_transactions: bool = False) -> dict:
    """Run existing audits without promoting a partial metric to a complete home."""
    document = load_yaml(config)
    require(type(document) is dict, "MONTH_REPORT_CONFIG_INVALID",
            "月报配置必须是映射", f"{config.resolve()}:1")
    missing = REQUIRED - set(document)
    unknown = set(document) - REQUIRED - OPTIONAL
    require(not missing and not unknown, "MONTH_REPORT_CONFIG_INVALID",
            f"月报配置缺少 {', '.join(sorted(missing))} 或含未知字段 {', '.join(map(str, sorted(unknown, key=str)))}",
            yaml_key_location(config, str(sorted(unknown, key=str)[0])) if unknown
            else f"{config.resolve()}:1")
    require(type(document["schema"]) is int and document["schema"] == 6,
            "MONTH_REPORT_CONFIG_INVALID", "月报配置版本无效",
            yaml_key_location(config, "schema"))
    for key in set(document) - {"schema"}:
        require(type(document[key]) is str and bool(document[key].strip()),
                "MONTH_REPORT_CONFIG_INVALID", "月报配置路径、月份或货币须为非空字符串",
                yaml_key_location(config, key))
    month = _config_month(document, config)
    try:
        cutover = date.fromisoformat(document["cutover"])
    except ValueError as exc:
        raise BudgetError("TIME_INVALID", "cutover 必须是有效日期",
                          yaml_key_location(config, "cutover")) from exc
    last_day = calendar.monthrange(*map(int, month.split("-")))[1]
    as_of = date.fromisoformat(f"{month}-{last_day:02d}")
    require(cutover.day == 1 and cutover <= as_of,
            "TIME_INVALID", "月报月份须不早于自然月切换点",
            yaml_key_location(config, "cutover"))
    currency = document["report_currency"]

    def path(key: str) -> Path | None:
        value = document.get(key)
        return (config.parent / value).resolve() if value is not None else None

    root = path("ledger")
    require(root.is_file(), "MONTH_REPORT_CONFIG_INVALID",
            "月报配置引用了不存在的根账本", yaml_key_location(config, "ledger"))
    sidecar_keys = (REQUIRED | OPTIONAL) - {"schema", "ledger", "cutover", "month", "report_currency"}
    watched = [config, *(path(key) for key in sidecar_keys if key in document)]
    for key in sidecar_keys & set(document):
        require(path(key).is_file(), "MONTH_REPORT_CONFIG_INVALID",
                "月报配置引用了不存在的审阅文件", yaml_key_location(config, key))
    before = {item: sha256(item.read_bytes()).hexdigest() for item in watched}
    accounts, opening, ownership = path("accounts"), path("opening"), path("ownership")
    obligations, h_fx = path("obligations"), path("h_fx")
    with frozen_financial(root), frozen_fact_indices():
        actual = audit_actual_snapshot(root, cutover, as_of, accounts, opening,
                                       ownership, obligations, path("completeness"), path("debt_coverage"), path("debt_settlements"))
        expense = audit_monthly_expense_targets(
            root, cutover, month, path("classifications"), path("expense_plans"),
            currency, path("benefit_groups"), accounts, ownership, obligations, h_fx, include_evidence=include_transactions)
        purpose = audit_monthly_purpose_progress(
            root, cutover, month, accounts, opening, ownership, path("purpose_plans"),
            currency, path("h_transfers"), path("k_events"), h_fx, path("purpose_pools"),
            obligations, include_evidence=include_transactions)
        pool = audit_purpose_pools(root, cutover, as_of, path("purpose_pools"))
        k_book = audit_k_book_purposes(root, cutover, as_of, accounts, opening,
                                       ownership, path("k_events"))
        holdings = audit_h_holdings(root, cutover, as_of, accounts, opening, ownership)
        account_purposes = summarize_account_purposes(holdings, pool)
        locations = posting_sources(load_financial(root))
        for position in account_purposes["pending"]:
            position["reasons"] = [
                {**reason, "sources": [locations[_key(ref)]
                                       for ref in reason.get("refs", [])] or
                 [yaml_key_location(opening, "h_holdings")]}
                for reason in position["reasons"]]
        tracking = audit_monthly_tracking(root, cutover, month)
        outflows = audit_default_outflows(root, cutover, month, accounts, ownership, opening,
                                         obligations, path("purpose_pools"))
        salary = None
        if path("salary_cycle"):
            from .salary_plan import audit_plan
            salary = audit_plan(config, path("salary_cycle"), as_of)
        owner_document = load_yaml(ownership)
        owner_names = {row["id"]: row["name"] for row in owner_document["owners"]}
        purpose_balances = summarize_purpose_balances(account_purposes, pool, k_book,
                                                      owner_document,
                                                      yaml_key_location(ownership, "owners"))
        details = {}
        if include_transactions:
            from .transaction_details import project_transactions
            details["transactions"] = project_transactions(load_financial(root), month, expense, purpose, pool,
                tracking, outflows, owner_names, load_yaml(accounts))
        require(all(item.is_file() and sha256(item.read_bytes()).hexdigest() == digest
                    for item, digest in before.items()),
                "INPUT_CHANGED", "月报运行期间配置或审阅文件发生变化", str(config))
    require(len({actual["ledger_snapshot"], expense["ledger_snapshot"],
                 purpose["ledger_snapshot"], pool["ledger_snapshot"],
                 holdings["ledger_snapshot"], k_book["ledger_snapshot"],
                 tracking["ledger_snapshot"], outflows["ledger_snapshot"]}) == 1 and
            all(row["report_currency"] == currency for row in (actual, expense, purpose, pool)),
            "MONTH_REPORT_MISMATCH", "月报各项审计的来源快照或报告货币不一致")
    if salary is not None:
        from .salary_plan import cap
        require(salary["report"] is None or salary["report"]["ledger_snapshot"] == actual["ledger_snapshot"],
                "MONTH_REPORT_MISMATCH", "工资周期与月报来源快照不同")
        max_spendable = cap(actual, salary)
        details["salary_cycle"] = salary
    else:
        max_spendable = summarize_max_spendable(actual, expense)
    return {**details, "mode": "v6_reviewed_month", "month": month, "as_of": as_of.isoformat(),
            "report_currency": currency, "ledger_snapshot": actual["ledger_snapshot"],
            "owner_names": owner_names,
            "actual_spendable": actual,
            "max_spendable": max_spendable,
            "expense_targets": expense,
            "purpose_progress": purpose,
            "default_outflows": outflows,
            "purpose_pools": pool,
            "k_book_purposes": k_book,
            "account_purposes": account_purposes,
            "purpose_balances": purpose_balances,
            "tracking": tracking,
            "note": "各项独立认证；此报表不签发完整首页或证券市值。历史日末数字不代表今天余额。"}


def audit_month_workflow(config: Path, *, include_transactions: bool = False, month: str | None = None) -> dict:
    """Give a new month one entry point, without presenting partial audits as complete."""
    with financial_session(), frozen_fact_indices():
        document = load_yaml(config)
        from .simple_config import is_simple, audit_simple
        if is_simple(document):
            return audit_simple(config, month, include_transactions=include_transactions)
        require(month is None, "MONTH_REPORT_CONFIG_INVALID", "旧月报配置已固定月份；--month 用于两文件入口")
        require(type(document) is dict, "MONTH_REPORT_CONFIG_INVALID",
                "月报配置必须是映射", f"{config.resolve()}:1")
        month = _config_month(document, config)
        first_day = date.fromisoformat(f"{month}-01")
        last_day = calendar.monthrange(first_day.year, first_day.month)[1]
        as_of = date(first_day.year, first_day.month, last_day)
        with booked_through(as_of):
            return _audit_month_workflow_bounded(config, document, month, first_day, as_of,
                                                include_transactions=include_transactions)


def _audit_month_workflow_bounded(config: Path, document: dict, month: str,
                                  first_day: date, as_of: date, *, include_transactions: bool = False) -> dict:
    review = audit_review_queue(config, first_day, as_of)
    if review["items"]:
        review["items"] = annotate_source_items(review["items"])
        status = ("IMPLEMENTATION_BLOCKED" if any(item["reason_kind"] == "implementation_limit"
                                                  for item in review["items"]) else "NEEDS_REVIEW")
        return {"status": status, "stage": "source",
                "month": month, "review_queue": review, "report": None,
                "review_items": review["items"],
                "pending_metrics": []}

    report = (audit_reviewed_month(config, include_transactions=True) if include_transactions else audit_reviewed_month(config))
    require(report["ledger_snapshot"] == review["ledger_snapshot"],
            "MONTH_REPORT_MISMATCH", "待审清单与月报不属于同一账本快照")
    pending = []
    for key, label, source_key in (
        ("actual_spendable", "实际可花", "completeness"),
        ("expense_targets", "消费目标", "expense_plans"),
        ("purpose_progress", "用途划拨", "purpose_plans"),
    ):
        result = report[key]
        if result["status"] != "CERTIFIED":
            pending.append({"metric": label, "status": result["status"],
                            "reason_codes": result.get("reason_codes", []),
                            "missing_target_ids": result.get("missing_target_ids", []),
                            "pending_events": result.get("pending_actual_events", []),
                            "review_items": result.get("review_items", []),
                            "source": str((config.parent / document[source_key]).resolve())
                            if source_key in document else str(config)})
    capped = report.get("max_spendable", {"status": "NOT_CONFIGURED"})
    if report.get("salary_cycle", {}).get("status") in ("NEEDS_REVIEW", "IMPLEMENTATION_BLOCKED"):
        cycle = report["salary_cycle"]
        pending.append({"metric": "工资周期", "status": cycle["status"],
                        "reason_codes": sorted({row["reason_code"] for row in cycle["review_items"]}),
                        "review_items": cycle["review_items"]})
    if capped["status"] == "NEEDS_REVIEW":
        pending.append({"metric": "最多能花", "status": "NEEDS_REVIEW",
                        "reason_codes": capped["reason_codes"],
                        "source": str((config.parent / document["expense_plans"]).resolve())})
    if report["account_purposes"]["status"] == "NEEDS_REVIEW":
        pending.append({"metric": "资产账户用途", "status": "NEEDS_REVIEW",
                        "reason_codes": ["H_HOLDINGS_PENDING"],
                        "pending_positions": report["account_purposes"]["pending"],
                        "source": str((config.parent / document["ownership"]).resolve())})
    if report.get("k_book_purposes", {"status": "CERTIFIED"})["status"] != "CERTIFIED":
        pending.append({"metric": "K 账面申赎用途", "status": "NEEDS_REVIEW",
                        "reason_codes": ["K_BOOK_PURPOSE_PENDING"],
                        "review_items": report["k_book_purposes"].get("review_items", []),
                        "pending_accounts": report["k_book_purposes"]["pending_accounts"],
                        "source": str((config.parent / document["k_events"]).resolve())})
    balance_view = report.get("purpose_balances", {"status": "CERTIFIED"})
    outflows = report.get("default_outflows", {"status": "NOT_CONFIGURED"})
    if outflows["status"] == "NEEDS_REVIEW":
        pending.append({"metric": "默认支出资金收付", "status": "NEEDS_REVIEW",
                        "reason_codes": outflows["reason_codes"],
                        "review_items": outflows["review_items"],
                        "source": str((config.parent / document["ownership"]).resolve())})
    if balance_view["status"] != "CERTIFIED":
        covered = {code for item in pending for code in item["reason_codes"]}
        uncovered = [code for code in balance_view["reason_codes"] if code not in covered]
        if uncovered:
            pending.append({"metric": "预算用途余额", "status": "NEEDS_REVIEW",
                            "reason_codes": uncovered,
                            "source": yaml_key_location(config, "purpose_pools")})
    items = enrich_metric_reviews(config, document, report, pending)
    blocked = any(item["reason_kind"] == "implementation_limit" for item in items)
    return {"status": "IMPLEMENTATION_BLOCKED" if blocked else
            "NEEDS_REVIEW" if pending else "CERTIFIED_SCOPED",
            "stage": "metrics" if pending else "report", "month": month,
            "review_queue": review, "report": None if pending else report,
            "review_items": items,
            "pending_metrics": pending}


def render_workflow_text(outcome: dict) -> str:
    if outcome.get("configuration_only"):
        return "\n".join([f"v6 {outcome['month']} 配置待审：NEEDS_REVIEW",
            *[f"{item['detail']} [{item['source']}]" for item in outcome["review_items"]],
            "本轮只检查配置；修正后重算，程序会继续核对交易与月报。"])
    if outcome["stage"] == "source":
        text = render_review_queue(outcome["review_queue"])
        if outcome.get("status") == "IMPLEMENTATION_BLOCKED":
            text += "\n其中含当前程序实现缺口；人工改标签不能解决这些计算限制。"
        return text
    if outcome["stage"] == "metrics":
        if "review_items" in outcome:
            state = "月报阻断（存在程序实现缺口）" if outcome["status"] == "IMPLEMENTATION_BLOCKED" else "月报待审"
            lines = [f"v6 {outcome['month']} {state}：{outcome['status']}"]
            for item in outcome["review_items"]:
                label = "程序实现缺口" if item["reason_kind"] == "implementation_limit" else "待审"
                lines.append(f"  {'、'.join(item['metrics'])}／{label}（{item['reason_code']}）："
                             f"{item['detail']} [{item['source']}]")
                context = item.get("account", "")
                if item.get("obligation_id"):
                    context = f"责任 {item['obligation_id']}，" + context
                if "remaining_native" in item:
                    context += f" 未结金额 {item['remaining_native']} {item.get('commodity', '')}"
                    if item.get("settlement_currency") != item.get("commodity") and "remaining_report" in item:
                        context += f"；清偿金额 {item['remaining_report']} {item['settlement_currency']}"
                elif "signed_booked_units" in item:
                    context += (f" 账本余额 {item['signed_booked_units']} {item.get('commodity', '')}；"
                                f"已审责任未偿额 {item['reviewed_remaining']}")
                    if "reviewed_receivable" in item:
                        context += (f"，代垫应收 {item['reviewed_receivable']}；"
                                    f"应有账本余额 {item['signed_reviewed_units']}")
                elif "signed_units" in item:
                    context += f" {item['signed_units']} {item.get('commodity', '')}"
                if context:
                    lines.append(f"    {context}")
            lines.append("本次不签发完整月报；" + (
                "处理上述程序实现缺口与待审事实后重跑。" if outcome["status"] == "IMPLEMENTATION_BLOCKED"
                else "处理上述待审事实后重跑。"))
            return "\n".join(lines)
        lines = [f"v6 {outcome['month']} 月报待审：以下指标尚未认证"]
        for item in outcome["pending_metrics"]:
            reasons = ", ".join(item["reason_codes"]) or item["status"]
            lines.append(f"  {item['metric']}：{reasons} [{item['source']}]")
            if item.get("missing_target_ids"):
                lines.append(f"    缺少目标：{', '.join(item['missing_target_ids'])}")
            for event in item.get("pending_events", []):
                source = event.get("source") or str(event.get("ref", item["source"]))
                lines.append(f"    {event['kind']}：{event['reason']} [{source}]")
            for position in item.get("pending_positions", []):
                codes = ", ".join(reason["code"] for reason in position["reasons"])
                lines.append(f"    {position['account']} {position['commodity']} "
                             f"{position['lot']}：{codes}")
            for account in item.get("pending_accounts", []):
                lines.append(f"    {account}：K 期初或申赎用途待审")
        lines.append("本次不签发完整月报；修正上述问题后重跑。")
        return "\n".join(lines)
    rendered = render_text(outcome["report"])
    review = outcome["review_queue"]
    if not review["booked_transactions_in_range"]:
        latest = review["latest_booked_transaction_date"] or "无"
        rendered += (f"\n查询月份没有已记交易；最近已记交易日 {latest}。"
                     "请核对 importer 是否已覆盖本月。")
    return rendered


def render_text(report: dict) -> str:
    """Show only rows carrying an independently certified value."""
    amount = amount_text

    currency = report["report_currency"]
    actual = report["actual_spendable"]
    capped = report.get("max_spendable", {"status": "NOT_CONFIGURED"})
    expense = report["expense_targets"]
    purpose = report["purpose_progress"]
    owner_names = report.get("owner_names", {})
    if capped["status"] == "CERTIFIED":
        headline = (f"最多能花：{amount(capped['max_spendable'])} {currency}"
                    f"（{capped['category_name']}{'发薪周期剩余额度' if capped.get('basis') == 'salary_cycle' else '消费目标差'} {amount(capped['consumption_target_remaining'])}；"
                    f"用途现金可支付 {amount(capped['payable_after_obligations'])}）")
    elif capped["status"] == "NEEDS_REVIEW":
        headline = f"最多能花：待审（{', '.join(capped['reason_codes'])}）"
    elif actual["status"] == "CERTIFIED":
        headline = (f"用途现金可支付：{amount(actual['actual_spendable'])} {currency}；"
                    + (("本周期消费上限不考核，不计算『最多能花』" if capped.get("basis") == "salary_cycle" else "本月消费上限不考核，不计算『最多能花』") if capped["status"] == "NOT_ASSESSED" else
                       "未配置消费目标上限，不签发『最多能花』"))
        if "SALARY_CYCLE_NOT_STARTED" in capped.get("reason_codes", []):
            headline = f"用途现金可支付：{amount(actual['actual_spendable'])} {currency}；工资周期目标尚未生效，不计算『最多能花』"
    else:
        headline = f"实际可花：待审（{', '.join(actual['reason_codes'])}）"
    lines = [f"v6 已审月报：{report['month']}（截至 {report['as_of']} 日末）",
             headline, "", ("自然月消费目标（与发薪周期额度分别显示）" if "salary_cycle" in report else "消费目标（目标 − 实际；负数为超支）")]
    if actual.get("same_day_clearings"):
        lines[2:2] = [f"本月已审同日责任清算 {len(actual['same_day_clearings'])} 条：已确认付款与责任来源对应，"
                      "未确认实际日内先后；保留原顺序，按日末余额签发。", ""]
    if report.get("salary_cycle", {}).get("status") == "NOT_STARTED":
        lines[2:2] = [f"首个已配置发薪日：{report['salary_cycle']['first_payday']}；此前仅核对账务，不套用工资周期额度。", ""]
    if report.get("salary_cycle", {}).get("report"):
        cycle = report["salary_cycle"]
        rows = [f"当前发薪周期：{cycle['from']} 起；本次统计至 {cycle['through']}。"
                + (f"下次发薪日 {cycle['next_payday']} 归下一周期。" if cycle["next_payday"] else "下次发薪日尚未登记。")]
        for row in cycle["report"]["cycle_targets"]:
            rows.append(f"  {row['category_name']}：周期额度 {amount(row['target']) if row['target'] is not None else '不考核'}，"
                        f"净消费 {amount(row['net_spent'])}，剩余 {amount(row['remaining']) if row['remaining'] is not None else '不计算'} {currency}")
        lines[2:2] = [*rows, ""]
    if actual["status"] == "CERTIFIED" and actual.get("clearing_receivables"):
        lines[2:2] = ["待报销代垫（应收原币；未到账，不计入可花现金）", *(
            f"  {row['clearing_id']}：待收 {amount(row['remaining_native'])} "
            f"{row['commodity']}，{row['account']}" +
            ("（期初结转）" if row.get("origin") == "opening" else "") + f" [{row['source']}]"
            for row in actual["clearing_receivables"]), ""]
    if actual["status"] == "CERTIFIED" and actual.get("booked_debts"):
        coverage = {row["obligation_id"]: row for row in actual.get("obligation_coverage") or []}
        settlements = {row["obligation_id"]: row for row in actual.get("settlement_allocations") or []}
        lines[2:2] = ["未清已记消费责任（未覆盖部分占用实际可花）", *(
            f"  {row['obligation_id']}：{amount(row['remaining_native'])} "
            f"{row['commodity']}，{row['account']}" +
            ("（期初结转）" if row.get("origin") == "opening" else "") +
            (f"；锁定清偿 {amount(settlements[row['obligation_id']]['report_amount'])} {currency}"
             f"（账单 {settlements[row['obligation_id']]['statement_id']}）"
             if row["obligation_id"] in settlements else "") +
            (f"；专用覆盖 {amount(coverage[row['obligation_id']]['covered'])}，"
             f"未覆盖 {amount(coverage[row['obligation_id']]['uncovered'])}"
             + (f" {currency}（覆盖与未覆盖均按锁定清偿金额）" if row["commodity"] != currency else "")
             if row["obligation_id"] in coverage else "")
            for row in actual["booked_debts"]), ""]
        if actual.get("purpose_available"):
            lines[2:2] = ["其他用途可动用现金（支付范围内；已扣债务预留与未覆盖责任）", *(
                f"  {owner_names.get(row['owner_id'], row['owner_id'])}："
                f"{amount(row['units'])} {currency}" +
                ("（警告：可动用现金为负）" if Fraction(row["units"]) < 0 else "")
                for row in actual["purpose_available"]), ""]
    shown = 0
    for row in expense["categories"]:
        if row.get("subtree_remaining_status") == "NOT_ASSESSED":
            lines.append(f"  {row['name']}（含子类）：不考核消费上限，实际 {amount(row['subtree_spent'])} {currency}；不计算目标差额")
            shown += 1
            if row["direct_target"] is None:
                continue
        elif row.get("direct_remaining_status") == "NOT_ASSESSED" and row["subtree_target_kind"] != "explicit":
            lines.append(f"  {row['name']}：直接费用目标不考核，实际 {amount(row['direct_spent'])} {currency}；不计算直接目标差额")
            shown += 1
            continue
        if row["subtree_target_kind"] != "explicit" and row["direct_target"] is None:
            continue
        subtree = row["subtree_target_kind"] == "explicit"
        status_key = "subtree_remaining_status" if subtree else "direct_remaining_status"
        amount_key = "subtree_spent" if subtree else "direct_spent"
        gross_key = "subtree_gross_spent" if subtree else "direct_gross_spent"
        refund_key = "subtree_refunded" if subtree else "direct_refunded"
        target_key = "subtree_target" if subtree else "direct_target"
        remain_key = "subtree_remaining" if subtree else "direct_remaining"
        label = row["name"] + ("（含子类）" if subtree else "")
        if row[status_key] == "CERTIFIED":
            detail = (f"（支出 {amount(row[gross_key])}，"
                      f"退款／冲回 {amount(row[refund_key])}）") if row.get(gross_key) is not None else ""
            lines.append(f"  {label}：目标 {amount(row[target_key])}，实际 {amount(row[amount_key])}，"
                         f"差额 {amount(row[remain_key])} {currency}{detail}")
        else:
            lines.append(f"  {label}：待审（目标 {row[target_key]}）")
        shown += 1
    if not shown:
        lines.append("  无已声明的消费目标")
    outflows = report.get("default_outflows", {})
    if outflows.get("status") == "CERTIFIED":
        name = owner_names.get(outflows["owner_id"], outflows["owner_id"])
        lines += ["", f"默认支出资金收付：{name}（实记原币）"]
        for row in outflows["totals"]:
            lines.append(f"  {row['commodity']}：净流出 {amount(row['net_outflow'])}"
                         f"（流出 {amount(row['gross_outflow'])}，流入 {amount(row['gross_inflow'])}）")
            lines.append(f"    消费实付 {amount(row['expenses_paid'])}，"
                         f"消费退款 {amount(row['expense_refunds'])}，还债 {amount(row['debt_payments'])}")
            lines.append(f"    代垫 {amount(row['advances_paid'])}，报销收回 {amount(row['advance_reimbursements'])}")
            for position in row["accounts"]:
                lines.append(f"    {position['account']}：净流出 {amount(position['net_outflow'])} {row['commodity']}")
        lines.append("  刷卡在实际还款时计入；资产搬移、购汇和申赎不计入。此处汇总资金流向，余额和预算差额各按原口径计算。")
    lines += ["", "用途划拨（目标 Q − 实际 G；负数为超额划拨）"]
    for row in purpose["owners"]:
        name = owner_names.get(row["owner_id"], row["owner_id"])
        if row["status"] == "CERTIFIED":
            completion = "已达成" if Fraction(row["remaining_q_minus_g"]) <= 0 else "未达成"
            lines.append(f"  {name}：目标 {amount(row['target_q'])}，实际 {amount(row['actual_g'])}，差额 {amount(row['remaining_q_minus_g'])} {currency}；{completion}")
        elif row["status"] == "NOT_ASSESSED":
            value = amount(row["actual_g"]) if row["actual_g"] is not None else "待审"
            lines.append(f"  {name}：实际净划拨 {value} {currency}；本月不考核划拨目标")
        elif row["status"] == "TARGET_MISSING":
            lines.append(f"  {name}：实际 {amount(row['actual_g']) if row['actual_g'] is not None else '待审'}，无目标")
        else:
            lines.append(f"  {name}：{row['status']}")
    lines += ["", "证券资金池（账面成本口径）"]
    for pool in report["purpose_pools"]["pools"]:
        lines.append(f"  {pool['id']}：{pool['protected_label']} {amount(pool['protected_nominal_principal'])}，"
                     f"{pool['remainder_label']} {amount(pool['remainder_book_cost'])} {currency}；市值未知")
        if Fraction(pool.get("principal_book_shortfall", "0")) > 0:
            lines.append(f"    警告：{pool['remainder_label']} 为负，名义本金账面缺口 "
                         f"{amount(pool['principal_book_shortfall'])} {currency}；不代表本金已保全")
        elif Fraction(pool.get("maximum_principal_book_shortfall", "0")) > 0:
            lines.append(f"    警告：期间曾有名义本金账面缺口 "
                         f"{amount(pool['maximum_principal_book_shortfall'])} {currency}，日末已补足")
        for account in pool["account_purposes"]:
            lines.append(f"    {account['account']}：{pool['protected_label']} {amount(account['protected_nominal'])}，"
                         f"{pool['remainder_label']} {amount(account['remainder_book_cost'])} {currency}")
    if "account_purposes" in report:
        lines += ["", "其他 H 资产账户的用途份额（原币）"]
        for row in report["account_purposes"].get("negative_purpose_positions", []):
            name = owner_names.get(row["owner_id"], row["owner_id"])
            lines.append(f"  警告：{row['account']} 的 {name} 用途份额为 "
                         f"{amount(row['units'])} {row['commodity']}")
        for row in report["account_purposes"]["rows"]:
            name = owner_names.get(row["owner_id"], row["owner_id"])
            lines.append(f"  {row['account']}：{name} {amount(row['units'])} {row['commodity']}")
        if not report["account_purposes"]["rows"]:
            lines.append("  无已审的非证券池 H 余额")
    if report.get("k_book_purposes", {}).get("rows"):
        lines += ["", "K 账面申赎及用途变动（非真实持仓；不可与 H 余额相加）"]
        for row in report["k_book_purposes"]["rows"]:
            lines.append(f"  {row['account']}：账面期初 {amount(row['booked_opening'])}，"
                         f"本期变动 {amount(row['booked_change'])}，"
                         f"期末 {amount(row['booked_end'])} {row['commodity']}；"
                         "真实份额与可赎回额未知")
            for part in row["purpose_changes"]:
                name = owner_names.get(part["owner_id"], part["owner_id"])
                lines.append(f"    cutover 后已审 {name} 申赎净变动 "
                             f"{amount(part['signed_change'])} {row['commodity']}")
        zero_records = report["k_book_purposes"].get("zero_value_records", [])
        if zero_records:
            lines.append(f"  截至报告日保留 {len(zero_records)} 条 K 零金额记录；不改变预算、现金或 G，"
                         "实际份额与价值未记录，不能把 0 元当作价值为零。")
    if report.get("purpose_balances", {}).get("status") == "CERTIFIED":
        lines += ["", "预算资金按用途查看（各币种与账面口径分别列示）"]
        balances = report["purpose_balances"]
        rows = balances["purposes"] + ([balances["outside_budget"]]
                                       if balances["outside_budget"] else [])
        roles = {"income_default": "默认收入用途", "expense_default": "默认支出用途"}
        for row in rows:
            suffix = f"（{'、'.join(roles[role] for role in row['default_roles'])}）" if row["default_roles"] else ""
            lines.append(f"  {row['name']}{suffix}：")
            if row["has_negative_h_position"]:
                lines.append("    警告：部分账户的该用途余额为负，请核对账户分布")
            if row.get("has_negative_pool_position"):
                lines.append("    警告：部分证券池账户的该用途账面余额为负，名义本金不代表已保全")
            if not row["h_native_balances"]:
                lines.append("    H 用途余额：0（无余额）")
            for key, label, value_key in (
                ("h_native_balances", "H 用途余额", "units"),
                ("pool_book_cost_balances", "证券池账面成本", "units"),
                ("k_book_purpose_changes", "K 已审申赎净变动（cutover 后）", "signed_change"),
            ):
                for total in row[key]:
                    lines.append(f"    {label}：{amount(total[value_key])} {total['commodity']}")
                    for position in total["accounts"]:
                        pool_label = f"（{position['pool_id']}／{position['label']}）" if "pool_id" in position else ""
                        lines.append(f"      {position['account']}{pool_label}："
                                     f"{amount(position[value_key])} {total['commodity']}")
    if report.get("tracking", {}).get("topics"):
        lines += ["", "专题支出（原币；支出 − 退款／冲回 = 净额）"]
        for row in report["tracking"]["topics"]:
            lines.append(f"  {row['topic']}：支出 {amount(row['spent'])}，"
                         f"退款／冲回 {amount(row['returned'])}，"
                         f"净额 {amount(row['net'])} {row['commodity']}")
    lines += ["", actual["note"], report["note"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    from ..console import configure_output
    configure_output()
    parser = argparse.ArgumentParser(description="从 config.yml + budgets.yml 生成月报；兼容旧月报配置")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path, help="config.yml 两文件入口，或兼容旧 schema 6 月报配置")
    source.add_argument("--project", type=Path, help="项目入口；省略输入时读取当前目录 budget-project.yaml")
    parser.add_argument("--month", help="两文件入口或项目中选择 YYYY-MM；省略时使用配置默认月份")
    parser.add_argument("--format", choices=("text", "json", "tui"), default="text")
    parser.add_argument("--watch", action="store_true", help="保存账本、include 或配置后自动重算；支持 text 和 tui")
    parser.add_argument("--transactions", action="store_true", help="已签发时附逐笔来源详情；TUI 自动启用，可按 a 浏览")
    parser.add_argument("--tui-config", type=Path, action="append", default=[],
                        help="TUI 中可切换的其他已准备月报配置，可重复指定；按所给顺序浏览")
    args = parser.parse_args(argv)
    if args.watch and args.format == "json":
        parser.error("--watch 支持 text／tui；单次 JSON 请去掉 --watch")
    if args.tui_config and args.format != "tui":
        parser.error("--tui-config 仅用于 --format tui")
    if not args.config and not args.project and (Path.cwd() / "config.yml").is_file():
        args.config = Path.cwd() / "config.yml"
    simple = False
    if args.config and args.config.is_file():
        try:
            from .simple_config import is_simple
            simple = is_simple(load_yaml(args.config))
        except BudgetError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.month is not None and args.config and not simple:
        parser.error("--month 用于项目或两文件入口；旧 --config 已经指定月份")
    if simple and args.tui_config:
        parser.error("两文件入口用 m／--month 切换月份，不需要 --tui-config")
    from .project import DEFAULT_PROJECT, load_project
    project = None
    if not args.config:
        project_path = args.project or Path.cwd() / DEFAULT_PROJECT
        if not args.project and not project_path.is_file():
            parser.error("请提供 --config 或 --project，或先用 project 命令生成当前目录的 budget-project.yaml")
        if args.tui_config:
            parser.error("项目模式使用已登记的月份清单，不同时使用 --tui-config")
        try:
            project = load_project(project_path)
            args.config = project.select(args.month)
        except (BudgetError, OSError, UnicodeError) as exc:
            print(json.dumps({"error": {"code": exc.code if isinstance(exc, BudgetError) else "PROJECT_IO_ERROR",
                "message": exc.message if isinstance(exc, BudgetError) else str(exc),
                "source": exc.source if isinstance(exc, BudgetError) else str(project_path)}}, ensure_ascii=False), file=sys.stderr)
            return 2
    if args.format == "tui":
        from functools import partial
        from .tui import run_report_tui
        try:
            if simple:
                from .simple_tui import run_simple_tui
                run_simple_tui(args.config, args.month, **({"watch": True} if args.watch else {}))
            elif project:
                run_report_tui(list(project.months.values()), partial(audit_month_workflow, include_transactions=True),
                               render_workflow_text, project=project, initial_config=args.config, **({"watch": True} if args.watch else {}))
            else:
                run_report_tui([args.config, *args.tui_config],
                               partial(audit_month_workflow, include_transactions=True), render_workflow_text, **({"watch": True} if args.watch else {}))
        except BudgetError as exc:
            print(json.dumps({"error": {"code": exc.code, "message": exc.message,
                                        "source": exc.source}}, ensure_ascii=False), file=sys.stderr)
            return 2
        return 0
    if args.watch:
        from .input_watch import InputWatch
        import time
        watcher = InputWatch([args.config])
        rerun = ["--config", str(args.config), "--format", "text"]
        if args.month:
            rerun.extend(["--month", args.month])
        if args.transactions:
            rerun.append("--transactions")
        try:
            while True:
                watcher.accept()
                main(rerun)
                print("\n正在监测账本／配置保存；Ctrl+C 退出。", flush=True)
                while not watcher.changed():
                    time.sleep(0.3)
        except KeyboardInterrupt:
            return 0
    try:
        outcome = (audit_month_workflow(args.config.resolve(), month=args.month, include_transactions=args.transactions) if simple else
                   audit_month_workflow(args.config.resolve(), include_transactions=True) if args.transactions
                   else audit_month_workflow(args.config.resolve()))
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message,
                                    "source": exc.source}}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.format == "text":
        text = render_workflow_text(outcome)
        if args.transactions and outcome["report"]:
            from .transaction_details import render_transaction
            report = outcome["report"]
            text += "\n\n已签发月报的逐笔来源：\n" + "\n\n".join(
                "\n".join(render_transaction(row, report["owner_names"])) for row in report["transactions"]["rows"])
        print(text)
    else:
        print(json.dumps(outcome, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

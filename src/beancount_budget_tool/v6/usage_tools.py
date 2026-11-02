"""Read-only onboarding and monthly trend helpers.

These are projections over existing audits.  They do not create configuration,
approve postings, fill pending months with zero, or add financial conversions.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import shlex

from beancount.core import data

from ..errors import BudgetError, require
from ..exact import exact_text
from ..financial import financial_session, frozen_financial, load_financial, month_bounds
from ..runtime_inputs import input_bytes
from ..yamlstrict import config_path, load_yaml
from ..query import QuerySession
from .accounts import _lot
from .purpose_workspace import _category_scope
from .targets import _month


def _address(config=None, ledger=None):
    require((config is None) != (ledger is None), "ONBOARDING_INPUT_INVALID",
            "config 与 ledger 必须二选一")
    return (Path(config).resolve() if config is not None else None,
            Path(ledger).resolve() if ledger is not None else None)


def _asset_rows(fin, *, cutover=None, through=None):
    roots = {fin.options["name_assets"]: "asset", fin.options["name_liabilities"]: "liability",
             fin.options["name_expenses"]: "expense", fin.options["name_income"]: "income"}
    opened = {}
    def collect(entries):
        balances = defaultdict(Fraction)
        for entry in entries:
            if isinstance(entry, data.Open):
                kind = roots.get(entry.account.split(":", 1)[0])
                if kind:
                    opened.setdefault(entry.account, {"account": entry.account, "kind": kind,
                                                      "opens": entry.date.isoformat(),
                                                      "commodities": []})
                    opened[entry.account]["commodities"].extend(
                        cur for cur in (entry.currencies or []) if cur not in opened[entry.account]["commodities"])
            elif isinstance(entry, data.Transaction):
                for posting in entry.postings:
                    kind = roots.get(posting.account.split(":", 1)[0])
                    if kind:
                        balances[(posting.account, posting.units.currency, _lot(posting))] += Fraction(posting.units.number)
        rows = []
        for key, amount in sorted(balances.items()):
            if amount:
                account, commodity, lot = key
                rows.append({"account": account, "commodity": commodity, "lot": lot,
                             "units": exact_text(amount)})
        return rows
    opening = collect(entry for entry in fin.entries
                      if cutover is not None and entry.date < cutover)
    observed = collect(entry for entry in fin.entries
                       if through is None or entry.date <= through)
    by_account = defaultdict(list)
    for row in observed:
        by_account[row["account"]].append(row)
    for account, row in opened.items():
        row["coordinates"] = by_account.get(account, [])
        row["opening_coordinates"] = [p for p in opening if p["account"] == account]
    latest = max((entry.date for entry in fin.entries
                  if through is None or entry.date <= through), default=None)
    return sorted(opened.values(), key=lambda row: row["account"]), opening, observed, latest, fin.options.get("operating_currency")




def _declarations(doc):
    """List declared intent; explicit empty K is a declaration, not a missing one."""
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    groups = doc.get("account_groups") if isinstance(doc.get("account_groups"), dict) else {}
    h, k, payment, opening = [], [], [], []
    def add_payment(account, scope):
        for currency, value in (scope or {}).items():
            payment.append({"account": account, "commodity": currency, "scope": value})
    for account, settings in accounts.items():
        settings = settings if isinstance(settings, dict) else {}
        if settings.get("semantics") == "H":
            h.append(account)
        elif settings.get("semantics") == "K":
            k.append(account)
        add_payment(account, settings.get("payment_scope"))
        if settings.get("opening") is not None:
            opening.append({"account": account, "opening": deepcopy(settings["opening"])})
    def group(name):
        value = groups.get(name)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            rows = []
            for key in ("payable", "excluded", "pending"):
                rows.extend(value.get(key) or [])
            return rows
        return []
    h = list(dict.fromkeys(h + group("H")))
    k = list(dict.fromkeys(k + group("K")))
    return {
        "h_accounts": h, "k_accounts": k, "payment_scope": payment, "opening": opening,
        "declared": {
            "accounts": "accounts" in doc, "h": "H" in groups or any(
                isinstance(row, dict) and row.get("semantics") == "H" for row in accounts.values()),
            "k": "K" in groups or any(
                isinstance(row, dict) and row.get("semantics") == "K" for row in accounts.values()),
            "payment": bool(payment), "opening": bool(opening),
            "k_explicit_empty": "K" in groups and groups.get("K") == [],
        },
    }


def _configured_onboarding(config: Path, month, currency):
    from .diagnostics import doctor
    inputs = None
    try:
        inputs = _trend_inputs(config)
    except (BudgetError, OSError, UnicodeError):
        pass
    with QuerySession() as session:
        checked = doctor(config, month=month)
        static_state = checked["checks"]["static"]["state"]
        load_error = None
        try:
            document = load_yaml(config)
            declarations = _declarations(document)
        except (BudgetError, OSError, UnicodeError) as exc:
            document = None
            declarations = {"h_accounts": [], "k_accounts": [], "payment_scope": [], "opening": [],
                            "declared": {"accounts": False, "h": False, "k": False,
                                         "payment": False, "opening": False, "k_explicit_empty": False},
                            "load_error": str(exc)}
            load_error = exc
        cutover_month = (document or {}).get("cutover")
        cutover_month = cutover_month[:7] if isinstance(cutover_month, str) and len(cutover_month) >= 7 else None
        preview_month = month or cutover_month or checked.get("month")
        preview, preview_error = None, None
        if preview_month:
            try:
                preview = session.month(config, preview_month, include_transactions=False)
            except (BudgetError, OSError, UnicodeError) as exc:
                preview_error = {"code": getattr(exc, "code", "PREVIEW_ERROR"),
                                 "message": str(exc)}
        preview_state = ("not_run" if preview_month is None else
                         "error" if preview_error is not None else
                         "passed" if preview and preview.get("report") is not None else
                         "pending" if preview else "not_run")
        actions = [{"action": "review", "reason": row.get("reason_code"),
                    "source": row.get("source")}
                   for row in checked["checks"]["static"]["issues"] + checked["checks"]["replay"]["issues"]]
        if load_error is not None:
            actions.append({"action": "fix_config_yaml", "detail": str(load_error)})
        actions.append({"action": "preview_first_month",
                        "command": "beancount-budget-tool monthly --config "
                        f"{shlex.quote(str(config))} --month {preview_month} --format text",
                        "detail": "先只读预览该月；是否还有声明缺失以 doctor 结果为准。"})
        steps = [
            {"name": "load_config", "state": "error" if load_error is not None or static_state == "error"
             else "passed" if static_state == "passed" else "pending"},
            {"name": "static", "state": static_state},
            {"name": "replay", "state": checked["checks"]["replay"]["state"]},
            {"name": "declarations", "state": "error" if load_error is not None
             else "passed" if static_state == "passed" else "pending"},
            {"name": "first_month_preview", "state": preview_state},
        ]
        if inputs is not None:
            require(_trend_inputs(config) == inputs, "INPUT_CHANGED",
                    "首次使用检查期间输入发生变化，请重算", str(config))
        return {"mode": "v6_onboarding", "status": "DIAGNOSTIC_ONLY", "report": None,
                "config": str(config), "ledger": None, "month": checked["month"],
                "preview_month": preview_month, "currency": currency,
                "checks": checked["checks"], "declarations": declarations,
                "preview": preview, "preview_error": preview_error,
                "preview_state": preview_state,
                "steps": steps, "next_actions": actions, "source": str(config),
                "note": "只读预检，不创建文件、不自动选择 H/K、支付范围或用途；显式空 K 视为已声明。"}


def onboarding(*, config=None, ledger=None, month=None, currency=None) -> dict:
    """Inventory a ledger or inspect an existing configuration without writing."""
    config, ledger = _address(config, ledger)
    if config is not None:
        return _configured_onboarding(config, month, currency)
    with financial_session(), frozen_financial(ledger):
        fin = load_financial(ledger)
        configured_month = (_month(month) if month is not None else
                            max((entry.date for entry in fin.entries), default=None).strftime("%Y-%m")
                            if any(True for _ in fin.entries) else None)
        if configured_month:
            first, last = month_bounds(configured_month)
        else:
            first = last = None
        accounts, opening, observed, latest, operating = _asset_rows(fin, cutover=first, through=last)
    return {"mode": "v6_onboarding", "status": "DIAGNOSTIC_ONLY", "report": None,
            "config": None, "ledger": str(ledger), "month": configured_month,
            "currency": currency or operating, "cutover": first.isoformat() if first else None,
            "accounts": accounts, "opening_coordinates": opening,
            "observed_coordinates": observed, "as_of": last.isoformat() if last else
            (latest.isoformat() if latest else None),
            "steps": [
                {"name": "load_ledger", "state": "passed"},
                {"name": "inventory_accounts", "state": "passed"},
                {"name": "choose_h_k", "state": "pending"},
                {"name": "choose_payment_scope", "state": "pending"},
                {"name": "declare_opening_and_purposes", "state": "pending"},
                {"name": "generate_two_file_config", "state": "pending"},
            ],
            "next_actions": [
                {"action": "identify_h_k_and_payment_scope", "detail": "请人工指定 H/K、可直接支付账户与币种。"},
                {"action": "confirm_opening_purposes", "detail": "按 cutover 前 opening 坐标确认用途；未知不能填零。"},
                {"action": "configure", "command": "beancount-budget-tool configure "
                 f"--ledger {shlex.quote(str(ledger))}" +
                 (f" --month {configured_month}" if configured_month else "") +
                 " --output <新配置目录>"},
            ],
            "source": str(ledger),
            "note": "只读盘点；opening_coordinates 是所选首月 cutover 前，observed_coordinates 是该月末观察值；均非 H/K 声明或已确认期初。"}


def render_onboarding(result: dict) -> str:
    lines = ["只读首次使用盘点（不创建文件、不签发月报）"]
    lines.append(f"来源：{result.get('config') or result.get('ledger')}")
    if result.get("config"):
        static = result["checks"]["static"]["state"]
        replay = result["checks"]["replay"]["state"]
        lines.append(f"静态检查：{static}；重放审计：{replay}")
        declared = result.get("declarations") or {}
        flags = declared.get("declared") or {}
        lines.append("声明状态：H=" + ("已声明" if flags.get("h") else "未声明")
                     + "；K=" + ("显式空" if flags.get("k_explicit_empty") else
                                 "已声明" if flags.get("k") else "未声明")
                     + "；支付范围=" + ("已声明" if flags.get("payment") else "未声明")
                     + "；期初=" + ("已声明" if flags.get("opening") else "未声明"))
        if declarations := declared.get("h_accounts"):
            lines.append("  H：" + ", ".join(declarations))
        if declarations := declared.get("k_accounts"):
            lines.append("  K：" + ", ".join(declarations))
        if declared.get("load_error"):
            lines.append("配置加载错误：" + declared["load_error"])
        if result.get("preview_month"):
            lines.append(f"首月预览 {result['preview_month']}：{result.get('preview_state')}")
            if result.get("preview_error"):
                error = result["preview_error"]
                lines.append("  预览错误：" + error.get("code", "PREVIEW_ERROR") + "：" + error.get("message", ""))
            elif result.get("preview"):
                outcome = result["preview"]
                if outcome.get("report") is None:
                    codes = sorted({row.get("reason_code") for row in outcome.get("review_items", [])
                                    if row.get("reason_code")})
                    lines.append("  未签发；原因：" + ("、".join(codes) or "见 doctor"))
                else:
                    from .monthly_report import render_workflow_text
                    lines.append("  已审原文本：")
                    lines.extend("  " + line for line in render_workflow_text(outcome).splitlines())
    else:
        lines.append(f"cutover：{result.get('cutover') or '未指定'}；截至：{result.get('as_of') or '未知'}；币种：{result.get('currency') or '未知'}")
        for account in result.get("accounts", []):
            observed = "；".join(f"{p['units']} {p['commodity']} {p['lot']}" for p in account['coordinates']) or "无"
            opening = "；".join(f"{p['units']} {p['commodity']} {p['lot']}" for p in account['opening_coordinates']) or "无"
            lines.append(f"  {account['account']} · {account['kind']} · 月末 {observed} · 期初 {opening}")
    lines.append("仍需人工确定：H/K、支付范围、期初用途；未知不能当零。")
    lines.append("下一步：")
    for action in result.get("next_actions", []):
        lines.append("  " + (action.get("command") or action.get("detail") or action.get("reason") or action.get("action")))
    return "\n".join(lines)


def _watch_files(config: Path) -> list[Path]:
    doc = load_yaml(config)
    paths = [config]
    if isinstance(doc, dict):
        for key in ("ledger", "budgets", "opening", "ownership", "obligations", "purpose_pools"):
            if isinstance(doc.get(key), str):
                paths.append(config_path(config, doc[key], key))
    return list(dict.fromkeys(paths))


def _trend_inputs(config: Path) -> dict[Path, bytes]:
    return {path: input_bytes(path) for path in _watch_files(config)}


def _owner_id(report, value):
    names = report.get("owner_names") or {}
    if value in names:
        return value, names[value]
    for ident, name in names.items():
        if name == value:
            return ident, name
    raise BudgetError("TREND_OWNER_UNKNOWN", "趋势用途未在已审报告中登记", str(value))


def _category(report, value):
    ident, name, _, scope = _category_scope(report, value)
    return ident, name, scope


def _cash_value(report, owner_id):
    actual = report.get("actual_spendable") or {}
    available = {row["owner_id"]: row["units"] for row in actual.get("purpose_available") or []}
    default = (report.get("default_outflows") or {}).get("owner_id")
    if default is not None:
        available[default] = actual.get("actual_spendable")
    if actual.get("status") != "CERTIFIED" or owner_id == "-":
        return None
    return available.get(owner_id, "0")


def _purpose_row(report, owner_id):
    rows = (report.get("purpose_balances") or {}).get("purposes") or []
    row = next((item for item in rows if item.get("owner_id") == owner_id), None)
    if row is None:
        raise BudgetError("TREND_OWNER_UNKNOWN", "趋势用途未在已审余额中登记", str(owner_id))
    return {"owner_id": owner_id, "name": row.get("name"),
            "default_roles": deepcopy(row.get("default_roles") or []),
            "h_native_balances": deepcopy(row.get("h_native_balances") or []),
            "k_book_purpose_changes": deepcopy(row.get("k_book_purpose_changes") or []),
            "pool_book_cost_balances": deepcopy(row.get("pool_book_cost_balances") or []),
            "available_cash": _cash_value(report, owner_id)}


def _native_rows(report, scope):
    return [deepcopy(row) for row in ((report.get("multicurrency") or {}).get("native_expenses") or [])
            if row.get("category_id") in scope]


def trend(config: Path, since: str, through: str, *, owner=None, category=None) -> dict:
    """Monthly read-only trend; pending months never become zero."""
    require(owner is None or category is None, "TREND_FILTER_INVALID",
            "owner 与 category 不能同时使用")
    config = Path(config).resolve()
    first, last = _month(since), _month(through)
    require(first <= last, "TREND_RANGE_INVALID", "起始月份不能晚于结束月份")
    before = _trend_inputs(config)
    outcomes = []
    with QuerySession() as query:
        month = first
        while month <= last:
            outcomes.append((month, query.month(config, month, include_transactions=False)))
            year, num = map(int, month.split("-"))
            ordinal = year * 12 + num - 1 + 1
            month = f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"
    after = _trend_inputs(config)
    require(before == after, "TREND_INPUT_CHANGED", "趋势查询期间输入发生变化，请重算", str(config))
    snapshots = {row["report"].get("ledger_snapshot") for _, row in outcomes
                 if row.get("report") is not None}
    require(len(snapshots) <= 1, "TREND_SNAPSHOT_CHANGED", "趋势各月不属于同一账本快照", str(config))
    representative = next((row["report"] for _, row in outcomes if row.get("report") is not None), None)
    owner_id = name = None
    scope = None
    filter_validation = "passed"
    if representative is not None:
        if owner is not None:
            owner_id, name = _owner_id(representative, owner)
        if category is not None:
            _, name, scope = _category(representative, category)
    else:
        # No signed month exists to validate a filter against. Keep the request
        # visible and return every pending month; never invent a report or use
        # a later month to validate an earlier filter.
        filter_validation = "not_run"
        name = owner if owner is not None else category
    rows = []
    for month, outcome in outcomes:
        report = outcome.get("report")
        item = {"month": month, "status": outcome.get("status"),
                "review_items": deepcopy(outcome.get("review_items") or []),
                "ledger_snapshot": (report or {}).get("ledger_snapshot"),
                "report": deepcopy(report)}
        if report is None:
            item.update({"purpose": None, "expense": None})
        elif owner is not None:
            item["purpose"] = _purpose_row(report, owner_id)
        elif category is not None:
            item["expense"] = {"category_id": representative and _category(representative, category)[0],
                               "category_name": name,
                               "native_expenses": _native_rows(report, scope)}
        else:
            item["purpose"] = [_purpose_row(report, row["owner_id"])
                               for row in (report.get("purpose_balances") or {}).get("purposes") or []]
            item["expense"] = {"native_expenses": _native_rows(
                report, {row["id"] for row in (report.get("expense_targets") or {}).get("categories") or []})}
        rows.append(item)
    comparisons = _comparisons(rows, owner_id=owner_id, category_id=(
        _category(representative, category)[0] if category is not None else None))
    all_signed = bool(rows) and all(str(row.get("status", "")).startswith("CERTIFIED") for row in rows)
    return {"mode": "v6_usage_trend", "read_only": True, "report": None,
            "status": "CERTIFIED_SCOPED" if all_signed else "NEEDS_REVIEW", "config": str(config),
            "since": first, "through": last, "report_currency": (representative or {}).get("report_currency"),
            "snapshot": next(iter(snapshots), None), "months": rows, "comparisons": comparisons,
            "filter": ({"owner_id": owner_id, "name": name, "validation": filter_validation}
                       if owner is not None else
                       {"category_id": (_category(representative, category)[0]
                                        if representative is not None else None),
                        "name": name, "validation": filter_validation}
                       if category is not None else None),
            "note": "逐月原值；待审月金额为 None，不填零。累计用途只取期末库存，不累加月末库存；不给同比百分比、不做汇率折算。"}


def _comparison_value(row, metric):
    return row.get(metric)


def _h_map(row, field="units"):
    result = {}
    for group in row.get("h_native_balances") or []:
        commodity = group.get("commodity")
        accounts = group.get("accounts") or [{"account": None, field: group.get(field, "0")}]
        for account in accounts:
            result[(commodity, account.get("account"))] = Fraction(account.get(field, "0"))
    return result


def _book_map(row, key, field):
    result = {}
    for group in row.get(key) or []:
        commodity = group.get("commodity")
        accounts = group.get("accounts") or [{"account": None, field: group.get(field, "0")}]
        for account in accounts:
            ident = (commodity, account.get("account"), account.get("pool_id"), account.get("label"))
            result[ident] = Fraction(account.get(field, "0"))
    return result


def _delta_map(left, right, fields):
    rows = []
    for key in sorted(set(left) | set(right), key=lambda value: tuple("" if p is None else str(p) for p in value)):
        delta = right.get(key, Fraction(0)) - left.get(key, Fraction(0))
        row = dict(zip(fields, key))
        row["change"] = exact_text(delta)
        rows.append(row)
    return rows


def _comparisons(rows, *, owner_id=None, category_id=None):
    result = []
    for left, right in zip(rows, rows[1:]):
        if not (str(left.get("status", "")).startswith("CERTIFIED") and
                str(right.get("status", "")).startswith("CERTIFIED")):
            continue
        if owner_id is not None:
            l, r = left.get("purpose"), right.get("purpose")
            if not l or not r:
                continue
            result.append({
                "from": left["month"], "to": right["month"], "owner_id": owner_id,
                "h_changes": _delta_map(_h_map(l), _h_map(r), ("commodity", "account")),
                "k_book_changes": _delta_map(
                    _book_map(l, "k_book_purpose_changes", "signed_change"),
                    _book_map(r, "k_book_purpose_changes", "signed_change"),
                    ("commodity", "account", "pool_id", "label")),
                "pool_book_changes": _delta_map(
                    _book_map(l, "pool_book_cost_balances", "units"),
                    _book_map(r, "pool_book_cost_balances", "units"),
                    ("commodity", "account", "pool_id", "label")),
            })
        elif category_id is not None:
            l = {(p["currency"], p["category_id"]): p for p in
                 ((left.get("expense") or {}).get("native_expenses") or [])}
            r = {(p["currency"], p["category_id"]): p for p in
                 ((right.get("expense") or {}).get("native_expenses") or [])}
            deltas = []
            for key in sorted(set(l) | set(r)):
                a = Fraction(l.get(key, {}).get("net", "0"))
                b = Fraction(r.get(key, {}).get("net", "0"))
                deltas.append({"currency": key[0], "category_id": key[1], "net_change": exact_text(b - a)})
            result.append({"from": left["month"], "to": right["month"],
                           "category_id": category_id, "expense_changes": deltas})
    return result


def _purpose_lines(purpose):
    lines = []
    currency = None
    fields = (("h_native_balances", "H 原币库存", "units"),
              ("k_book_purpose_changes", "K 账面申赎", "signed_change"),
              ("pool_book_cost_balances", "证券池账面成本", "units"))
    for field, label, metric in fields:
        groups = purpose.get(field) or []
        if not groups:
            lines.append(f"    {label}：无")
            continue
        for group in groups:
            currency = group.get("commodity") or currency
            lines.append(f"    {label}：{group.get(metric)} {group.get('commodity')}")
            for account in group.get("accounts") or []:
                extra = ""
                if account.get("pool_id"):
                    extra = f" · {account.get('pool_id')}" + (f" {account.get('label')}" if account.get('label') else "")
                lines.append(f"      {account.get('account')}：{account.get(metric)} {group.get('commodity')}{extra}")
    return lines


def render_trend(result: dict) -> str:
    lines = [f"只读月度趋势 {result['since']} 至 {result['through']}（不折算、不填零）"]
    names = {}
    for row in result.get("months", []):
        report = row.get("report")
        if report:
            names = {item.get("id"): item.get("name") for item in
                     ((report.get("expense_targets") or {}).get("categories") or [])}
            break
    for row in result.get("months", []):
        lines.append(f"{row['month']} {row['status']}")
        if row.get("report") is None:
            lines.append("  未签发：不显示金额")
            continue
        if row.get("purpose"):
            purposes = row["purpose"] if isinstance(row["purpose"], list) else [row["purpose"]]
            for p in purposes:
                cash = p.get("available_cash")
                lines.append(f"  {p.get('name')} · 可支付 {cash if cash is not None else '未知'}")
                lines.extend(_purpose_lines(p))
        if row.get("expense"):
            for e in row["expense"].get("native_expenses") or []:
                label = names.get(e.get("category_id"), e.get("category_id"))
                lines.append(f"  消费 · {label} · {e.get('net')} {e.get('currency')}")
    if result.get("comparisons"):
        lines.append("相邻已签月派生变化（独立于月报字段）：")
        for c in result["comparisons"]:
            lines.append(f"  {c['from']} -> {c['to']}")
            for item in c.get("h_changes", []):
                lines.append(f"    H · {item.get('commodity')} · {item.get('account') or '未分账户'} · 变化 {item.get('change')}")
            for item in c.get("k_book_changes", []):
                lines.append(f"    K · {item.get('commodity')} · {item.get('account') or '未分账户'} · 变化 {item.get('change')}")
            for item in c.get("pool_book_changes", []):
                lines.append(f"    池 · {item.get('commodity')} · {item.get('account') or '未分账户'} · 变化 {item.get('change')}")
            for item in c.get("expense_changes", []):
                label = names.get(item.get("category_id"), item.get("category_id"))
                lines.append(f"    消费 · {label} · {item.get('currency')} · 净变化 {item.get('net_change')}")
    return "\n".join(lines)

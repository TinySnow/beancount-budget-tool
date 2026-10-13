"""v6 第二阶段：资产账户语义与支付范围的独立、只读审阅。

账户是否为真实库存 H，与真实库存能否用于首页支付，是两个决定。
这里只读取官方 booking 后的事实，既不按名称/余额猜决定，也不把
账户名册或声明变成已经签发的预算报告。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path
import json

from beancount.core import data

from ..errors import BudgetError, require
from ..exact import exact, exact_text
from ..financial import load_financial
from ..yamlstrict import load_yaml
from .facts import FactIndex, GeneratedPostingRef
from .provenance import asset_opening_fingerprint
from .settlements import validate_same_day_settlements


def _lot(posting: data.Posting) -> str:
    """完整成本坐标；不导入 v4 的归属或组语义。"""
    cost = posting.cost
    if cost is None:
        return f"{posting.units.currency}/no-cost"
    return json.dumps([posting.units.currency, str(cost.number), cost.currency,
                       cost.date.isoformat() if cost.date else None, cost.label],
                      ensure_ascii=False)


def _where(posting: data.Posting) -> str:
    meta = posting.meta or {}
    return f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"


def audit_accounts(root: Path, cutover: date, declarations: Path | None = None) -> dict:
    """列出全部有直接 posting 的资产账户，并可校验人工声明。

    旧交易级标签不会阻断这个迁移盘点；只有正式 v6 源门槛会拒绝它们。
    `review_state=complete` 仅表示本阶段账户声明闭合，不是 v6 快照签发。
    """
    require(isinstance(cutover, date) and cutover.day == 1,
            "CUTOVER_INVALID", "cutover 必须是自然月第一天")
    fin = load_financial(root)
    facts = FactIndex(fin)
    asset_root = fin.options["name_assets"]
    states: dict[str, dict] = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] != asset_root:
                continue
            state = states.setdefault(posting.account, {
                "commodities": set(), "opening": defaultdict(Fraction),
                "direct_postings": 0, "before_cutover": 0, "from_cutover": 0,
                "has_cost": False, "has_price": False, "source_unavailable": False,
                "commodity_shape": defaultdict(lambda: {
                    "has_cost": False, "has_price": False, "source_unavailable": False}),
                "first_source": _where(posting), "postings": [],
            })
            commodity = posting.units.currency
            shape = state["commodity_shape"][commodity]
            state["commodities"].add(commodity)
            state["direct_postings"] += 1
            state["has_cost"] |= posting.cost is not None
            state["has_price"] |= posting.price is not None
            shape["has_cost"] |= posting.cost is not None
            shape["has_price"] |= posting.price is not None
            if not isinstance(facts.for_posting(posting), GeneratedPostingRef):
                try:
                    has_raw_price = fin.raw_price(posting)[0] != "none"
                    state["has_price"] |= has_raw_price
                    shape["has_price"] |= has_raw_price
                except BudgetError as exc:
                    if exc.code != "SOURCE_MISSING":
                        raise
                    state["source_unavailable"] = True
                    shape["source_unavailable"] = True
            units = exact(posting.units.number)
            if entry.date < cutover:
                state["before_cutover"] += 1
                state["opening"][(posting.units.currency, _lot(posting))] += units
            else:
                state["from_cutover"] += 1
            state["postings"].append((id(entry), entry.date, posting, units))

    names = sorted(states)
    rows = []
    for name in names:
        state = states[name]
        rows.append({
            "account": name,
            "direct_postings": state["direct_postings"],
            "before_cutover_postings": state["before_cutover"],
            "from_cutover_postings": state["from_cutover"],
            "has_children_with_direct_postings": any(
                child.startswith(name + ":") for child in names),
            "commodities": sorted(state["commodities"]),
            "has_cost": state["has_cost"],
            "has_price": state["has_price"],
            "source_unavailable": state["source_unavailable"],
            "opening_coordinates": [
                {"commodity": commodity, "lot": lot, "signed_units": exact_text(units)}
                for (commodity, lot), units in sorted(state["opening"].items())
            ],
            "first_source": state["first_source"],
        })

    opening_fingerprint = asset_opening_fingerprint(fin, cutover)
    result = {"mode": "v6_account_audit_only", "ledger_snapshot": fin.digest,
              "opening_fingerprint": opening_fingerprint,
              "loader_version": fin.loader_version, "cutover": cutover.isoformat(),
              "review_state": "pending", "accounts_with_direct_postings": len(rows),
              "missing_declarations": names, "pending_payment_scope": [], "accounts": rows,
              "same_day_settlements": [],
              "note": "账户审阅不证明逐腿归属、责任或完整实际可花"}
    if declarations is None:
        return result
    document = load_yaml(declarations)
    base_fields = {"schema", "cutover", "report_currency", "accounts"}
    settlement_fields = {"credit_card_whitelist", "same_day_settlements"}
    optional_fields = {"ledger_snapshot", "opening_fingerprint"}
    require(isinstance(document, dict) and base_fields <= set(document) and
            set(document) <= base_fields | optional_fields | settlement_fields and
            set(document) - base_fields - optional_fields in (set(), settlement_fields),
        "ACCOUNT_DECLARATION_INVALID", "账户声明必须含 schema/cutover/report_currency/accounts",
        str(declarations))
    require(type(document["schema"]) is int and document["schema"] == 6,
            "ACCOUNT_DECLARATION_INVALID", "账户声明 schema 必须为整数 6", str(declarations))
    require(type(document["cutover"]) is str and document["cutover"] == cutover.isoformat(),
            "ACCOUNT_DECLARATION_INVALID", "cutover 必须是与审计一致的带引号日期", str(declarations))
    currency = document["report_currency"]
    require(type(currency) is str and bool(currency) and currency in fin.options.get("operating_currency", []),
            "REPORT_CURRENCY_INVALID", "报告货币须明确选自账本 operating_currency", str(declarations))
    records = document["accounts"]
    require(isinstance(records, list), "ACCOUNT_DECLARATION_INVALID", "accounts 必须是列表", str(declarations))
    settlements, settlement_roles = validate_same_day_settlements(
        fin, facts, document.get("credit_card_whitelist", []),
        document.get("same_day_settlements", []), cutover, currency)
    approved = {}
    for item in records:
        require(isinstance(item, dict) and set(item) == {
            "account", "semantics", "balance_semantics", "payment_scope", "reason"},
            "ACCOUNT_DECLARATION_INVALID", "每个账户须明确语义、余额语义、逐币种支付范围和理由",
            str(declarations))
        account = item["account"]
        require(type(account) is str and account in states,
                "ACCOUNT_UNKNOWN", "声明了没有直接资产 posting 的账户", str(account))
        require(account not in approved, "ACCOUNT_DUPLICATE", "账户重复声明", account)
        require(type(item["reason"]) is str and bool(item["reason"].strip()),
                "ACCOUNT_REASON_MISSING", "账户语义及支付范围须留下人工审阅理由", account)
        semantics = item["semantics"]
        require(semantics in ("H", "K"), "ACCOUNT_SEMANTICS_INVALID", "semantics 只能为 H 或 K", account)
        expected_balance = "real_inventory" if semantics == "H" else "signed_subscription_flow"
        require(item["balance_semantics"] == expected_balance,
                "ACCOUNT_SEMANTICS_INVALID", f"{semantics} 必须使用 {expected_balance} 余额语义", account)
        scope = item["payment_scope"]
        require(isinstance(scope, dict) and set(scope) == states[account]["commodities"] and
                all(type(value) is str and value in ("payable", "excluded", "pending") for value in scope.values()),
                "PAYMENT_SCOPE_INVALID", "每个出现过的商品均须填写 payable、excluded 或 pending", account)
        state = states[account]
        if semantics == "K":
            require(state["commodities"] == {currency} and not state["has_cost"] and
                    not state["has_price"] and not state["source_unavailable"],
                    "K_SHAPE_INVALID", "K 仅接受无成本、无价格、来源完整的报告货币累计腿", account)
            require(all(value == "excluded" for value in scope.values()),
                    "PAYMENT_SCOPE_INVALID", "K 不能作为首页可直接支付现金", account)
        else:
            for (commodity, lot), amount in state["opening"].items():
                require(amount >= 0, "H_NEGATIVE_OPENING",
                        f"真实库存期初为负：{commodity} {lot} {exact_text(amount)}", account)
            running = dict(state["opening"])
            # 同一交易可在同账户内先出后入；先按整笔交易净额结算，再
            # 检查真实库存。旧历史不逐笔重算，只核对 cutover 期初。
            current_tx = None
            current_day = None
            current_postings: list[data.Posting] = []
            changes: dict[tuple[str, str], Fraction] = defaultdict(Fraction)
            pending_settlements = {}
            used_settlements: set[int] = set()
            source = ""

            def flush() -> None:
                for key, delta in changes.items():
                    before = running.get(key, Fraction(0))
                    after = before + delta
                    relevant = [p for p in current_postings
                                if (p.units.currency, _lot(p)) == key]
                    if key in pending_settlements:
                        claim = pending_settlements.pop(key)
                        require(current_day == claim.day and len(relevant) == 1 and
                                id(relevant[0]) == claim.funding_posting_id and
                                before == -claim.units and after == 0,
                                "SETTLEMENT_INTERRUPTED",
                                "临时负库存须由同日下一条同坐标购汇入账精确清零", source)
                        used_settlements.add(claim.funding_posting_id)
                    elif after < 0:
                        role = settlement_roles.get(id(relevant[0])) if len(relevant) == 1 else None
                        require(role is not None and role[0] == "debit" and
                                role[1].asset_account == account and
                                role[1].commodity == key[0] and
                                before == 0 and after == -role[1].units,
                                "H_NEGATIVE_INVENTORY",
                                f"真实库存变为负数：{key[0]} {key[1]} {exact_text(after)}", source)
                        pending_settlements[key] = role[1]
                        used_settlements.add(role[1].debit_posting_id)
                    else:
                        require(not any(settlement_roles.get(id(p), (None,))[0] == "funding"
                                        for p in relevant),
                                "SETTLEMENT_OUT_OF_ORDER", "结算入账没有对应的临时负库存", source)
                    running[key] = after

            for tx, day, posting, units in state["postings"]:
                if day < cutover:
                    continue
                if current_tx is not None and tx != current_tx:
                    flush()
                    changes.clear()
                    current_postings.clear()
                current_tx = tx
                current_day = day
                source = _where(posting)
                changes[(posting.units.currency, _lot(posting))] += units
                current_postings.append(posting)
            if current_tx is not None:
                flush()
            require(not pending_settlements, "SETTLEMENT_UNFINISHED",
                    "同日临时结算未在本账本内精确清零", account)
            expected_settlement_ids = {ident for claim in settlements if claim.asset_account == account
                                       for ident in (claim.debit_posting_id, claim.funding_posting_id)}
            require(used_settlements == expected_settlement_ids,
                    "SETTLEMENT_UNUSED", "结算声明没有被真实库存重放完整使用", account)
            if "payable" in scope.values():
                cash_shape = state["commodity_shape"].get(currency, {})
                require({commodity for commodity, value in scope.items() if value == "payable"} == {currency} and
                        not cash_shape.get("has_cost") and not cash_shape.get("has_price") and
                        not cash_shape.get("source_unavailable"),
                        "PAYMENT_SCOPE_INVALID", "首版只认证无成本、无报价的报告货币真实现金", account)
        approved[account] = item
    require(all(claim.asset_account in approved and
                approved[claim.asset_account]["semantics"] == "H" for claim in settlements),
            "SETTLEMENT_ACCOUNT_INVALID", "临时结算只能用于已审真实库存账户")
    missing = sorted(set(states) - set(approved))
    pending_scope = sorted(f"{account}/{commodity}" for account, item in approved.items()
                           for commodity, value in item["payment_scope"].items()
                           if value == "pending")
    result["report_currency"] = currency
    result["missing_declarations"] = missing
    result["pending_payment_scope"] = pending_scope
    result["same_day_settlements"] = [claim.as_dict() for claim in settlements]
    result["review_state"] = "complete" if not missing and not pending_scope else "pending"
    for row in rows:
        item = approved.get(row["account"])
        if item:
            row["semantics"] = item["semantics"]
            row["balance_semantics"] = item["balance_semantics"]
            row["payment_scope"] = item["payment_scope"]
            row["review_reason"] = item["reason"]
    return result

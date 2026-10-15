"""Read-only provenance of a signed month, joined by complete booked identities."""

from collections import defaultdict
from fractions import Fraction

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text, amount_text
from .classification import _key
from .facts import FactIndex


ORIGINS = {"inline": "行内标签", "external": "审阅决定", "default": "默认规则", "inferred": "默认或已审库存继承"}
FLOW_NAMES = {"expenses_paid": "消费实付", "expense_refunds": "消费退款", "debt_payments": "还债",
              "advances_paid": "单位代垫", "advance_reimbursements": "报销收回"}
DEBT_NAMES = {"consumer_purchase": "刷卡消费", "payment": "还款", "advance_outflow": "单位代垫",
              "advance_reimbursement": "单位报销"}
VALUE_NAMES = {"native_report_currency": "原币即报告货币", "source_price_@": "原分录单价", "source_price_@@": "原分录总价"}


def project_transactions(fin, month: str, expense: dict, purpose: dict, pools: dict,
                         tracking: dict, outflows: dict, owner_names: dict, account_document: dict) -> dict:
    """Use existing audit evidence; do not classify, value, or replay any event."""
    spending = expense.pop("_transaction_evidence")
    funds = purpose.pop("_transaction_evidence")
    require(len({fin.digest, expense["ledger_snapshot"], purpose["ledger_snapshot"], pools["ledger_snapshot"]}) == 1,
            "TRANSACTION_DETAILS_MISMATCH", "交易详情与月报不属于同一来源快照")
    classified = {_key(row["ref"]): row for row in spending["classifications"]}
    owned = {_key(row["ref"]): row for row in funds["ownership"]}
    valued = {_key(row["ref"]): row for row in spending["valuations"]}
    benefits = {_key(row["ref"]): row for row in spending["benefits"]}
    settlements = {_key(row["ref"]): row for row in expense["foreign_settlement_costs"]}
    categories = {row["id"]: row["name"] for row in expense["categories"]} | {"-": "预算外"}
    names = owner_names | {"-": "预算外"}
    semantics = {row["account"]: row["semantics"] for row in account_document["accounts"]}
    internal_keys = set(funds["pool_internal_keys"])
    pool_accounts = {account: pool for pool in pools["pools"] for account in pool["accounts"]}
    topic_by_ref = defaultdict(set)
    for row in tracking["events"]:
        topic_by_ref[_key(row["ref"])].add(row["topic"])
    flow_by_ref = defaultdict(list)
    for row in outflows["events"]:
        flow_by_ref[_key(row["asset_ref"])].append(row)
    facts = FactIndex(fin)
    rows, by_posting = [], {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date.strftime("%Y-%m") != month:
            continue
        reference = facts.for_transaction(entry).as_dict()
        meta = entry.meta
        source = f"{meta['filename']}:{meta['lineno']}"
        generated = "generated_kind" in reference
        record = {"date": entry.date.isoformat(), "payee": entry.payee, "narration": entry.narration,
                  "report_currency": expense["report_currency"],
                  "flag": entry.flag, "source": source, "ref": reference, "generated": generated,
                  "source_text": fin.line(meta["filename"], meta["lineno"]) if generated else
                                 fin.transaction_text(meta["filename"], meta["lineno"]),
                  "postings": [], "purpose_events": [], "net_allocations": {}, "liabilities": [],
                  "pool_internal": False}
        for posting in entry.postings:
            ref = facts.for_posting(posting).as_dict()
            key = _key(ref)
            by_posting[key] = record
            posting_meta = posting.meta or entry.meta
            labels = {field: (posting.meta or {})[field] for field in ("budget", "fund", "tracking") if field in (posting.meta or {})}
            part = {"account": posting.account, "signed_units": exact_text(exact(posting.units.number)),
                    "commodity": posting.units.currency, "lot": ref["lot"], "ref": ref,
                    "source": f"{posting_meta['filename']}:{posting_meta['lineno']}", "labels": labels,
                    "classification": None, "h_purpose": None, "k_purpose": None, "pool": None,
                    "historical_valuation": valued.get(key), "settlement_comparison": settlements.get(key),
                    "benefit_source": benefits.get(key), "tracking": sorted(topic_by_ref[key]),
                    "default_outflows": flow_by_ref[key]}
            if not generated:
                notation, number, currency = fin.raw_price(posting)
                part["source_price"] = ({"notation": notation, "unsigned_amount": exact_text(number), "commodity": currency}
                                         if notation != "none" else None)
            if key in classified:
                claim = classified[key]
                part["classification"] = {**claim, "slices": [
                    {**slice_, "name": categories[slice_["category_id"]]} for slice_ in claim["slices"]]}
            if posting.account in pool_accounts:
                pool = pool_accounts[posting.account]
                part["pool"] = {"id": pool["id"], "protected_name": names[pool["protected_owner_id"]],
                                "remainder_name": names[pool["remainder_owner_id"]],
                                "protected_label": pool["protected_label"], "remainder_label": pool["remainder_label"],
                                "basis": "pool_level_only_no_lot_attribution"}
            elif key in owned:
                claim = owned[key]
                part["h_purpose"] = {**claim, "slices": [
                    {**slice_, "name": names[slice_["owner_id"]]} for slice_ in claim["slices"]]}
            if semantics.get(posting.account) == "K":
                part["basis"] = "K_book_flow_not_holdings_or_market_value"
            if posting.cost is not None:
                cost = posting.cost
                part["booked_cost"] = {"unit_cost": exact_text(exact(cost.number)), "commodity": cost.currency,
                                       "date": cost.date.isoformat() if cost.date else None, "label": cost.label}
            if key in internal_keys:
                record["pool_internal"] = True
            record["postings"].append(part)
        rows.append(record)
    for event in funds["events"]:
        matched = [by_posting[_key(ref)] for ref in event["h_refs"]]
        require(bool(matched) and all(row is matched[0] for row in matched),
                "TRANSACTION_DETAILS_MISMATCH", "用途事件不能跨交易拼接到详情")
        record = matched[0]
        record["purpose_events"].append(event)
        for ident, amount in event["net_allocations"].items():
            record["net_allocations"][ident] = exact_text(Fraction(record["net_allocations"].get(ident, "0")) + Fraction(amount))
        if event["kind"] == "k_event":
            protocol = event["protocol"]
            leg = next(part for part in record["postings"] if part["ref"] == protocol["k_ref"])
            leg["k_purpose"] = {"basis": "K_book_flow_not_holdings_or_market_value", "slices": [
                {"owner_id": slice_["k_owner_id"], "name": names[slice_["k_owner_id"]],
                 "signed_units": slice_["signed_k_units"]} for slice_ in protocol["slices"]]}
    for event in funds["liabilities"]:
        record = by_posting[_key(event["liability_ref"])]
        owner = event["owner_id"] or funds["responsibility_owners"].get(event["obligation_id"])
        record["liabilities"].append({**event, "responsibility_owner_id": owner,
                                      "responsibility_owner_name": funds["responsibility_names"][owner] if owner else None})
    return {"status": "SOURCE_OF_MONTH_REPORT", "month": month, "ledger_snapshot": fin.digest,
            "rows": rows, "count": len(rows),
            "note": "已签发月报内的 booked 来源与各项审计证据；不另行签发逐笔净收益或市值。分录保留原币和正负号，资金用途与债务责任分别显示。"}


def render_transaction(record: dict, owner_names: dict) -> list[str]:
    """Readable first; complete reference dictionaries remain in JSON."""
    lines = [f"{record['date']} {record.get('payee') or ''} {record['narration']}",
             f"账本位置：{record['source']}"]
    if record["generated"]:
        lines.append("由 Beancount pad 生成；以下位置是原始 pad 指令。")
    for posting in record["postings"]:
        lines.extend(["", f"{posting['account']}  {amount_text(posting['signed_units'])} {posting['commodity']}",
                      f"  位置：{posting['source']}"])
        for key, label in (("classification", "预算类别"), ("h_purpose", "资产分录用途"), ("k_purpose", "K 账面申赎用途")):
            claim = posting[key]
            if claim:
                origin = ORIGINS.get(claim.get("origin"), "已审事件")
                lines.append(f"  {label}（{origin}）：" + "，".join(
                    f"{part['name']} {amount_text(part['signed_units'])} {posting['commodity']}" for part in claim["slices"]))
                if claim.get("review_reason"):
                    lines.append(f"  依据：{claim['review_reason']}")
        if posting["pool"]:
            pool = posting["pool"]
            lines.append(f"  证券池 {pool['id']}：{pool['protected_label']}／{pool['remainder_label']}，仅池级用途，不逐批次分配。")
        if posting.get("basis"):
            lines.append("  K 仅为账面申赎，不是持仓、市值或可赎回金额。")
        if posting.get("booked_cost"):
            cost = posting["booked_cost"]
            lines.append(f"  已记单位成本：{amount_text(cost['unit_cost'])} {cost['commodity']}，批次日期 {cost['date'] or '未指定'}")
        if posting["tracking"]:
            lines.append("  专题：" + "，".join(posting["tracking"]))
        value = posting["historical_valuation"]
        settlement = posting["settlement_comparison"]
        if value:
            lines.append(f"  原分录计价：{amount_text(value['historical_report_value'])} {value['report_currency']}（{VALUE_NAMES.get(value['proof'], value['proof'])}）")
        if settlement:
            lines.append(f"  预算比较采用 {settlement['settlement_date']} 实际还款／购汇成本 "
                         f"{amount_text(settlement['budget_comparison_value'])} {settlement['report_currency']}；不是消费日价格。")
        if posting["classification"] and not value and not settlement:
            lines.append("  本项仅显示原币分类；本月报未给出该分录的报告货币计价值。")
        for flow in posting["default_outflows"]:
            lines.append(f"  默认支出资金收付：{FLOW_NAMES.get(flow['kind'], flow['kind'])} {amount_text(flow['amount'])} {flow['commodity']}")
    for event in record["liabilities"]:
        label = event["obligation_id"] or event["clearing_id"]
        owner = event["responsibility_owner_name"] or "单位代垫清算，不作本人消费责任"
        lines.append(f"责任事件：{DEBT_NAMES.get(event['kind'], event['kind'])}，ID {label}，责任用途：{owner}")
    allocations = record["net_allocations"]
    lines.append("本笔正式净划拨 G：" + ("，".join(
        f"{owner_names[ident]} {amount_text(value)} {record['report_currency']}" for ident, value in allocations.items()) if allocations else "0（按已审用途事件）"))
    lines.extend(["", "原始账本内容：", *record["source_text"].splitlines()])
    return lines

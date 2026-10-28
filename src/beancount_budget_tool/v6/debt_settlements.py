"""Report-date foreign debt values from explicitly reviewed locked statements."""

from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re

from ..runtime_inputs import input_bytes
from ..errors import require
from ..exact import exact_text
from ..yamlstrict import load_yaml, yaml_key_location, yaml_path_location
from .classification import _key
from .review_diagnostics import review_item


AMOUNT = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")
ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def audit_debt_settlements(debts: list[dict], cutover: date, as_of: date,
                           report_currency: str, sources: dict[str, str],
                           declarations: Path | None = None) -> dict:
    """A statement allocates its total once across whole outstanding debt IDs.

    Original quantities stay native. Valued copies carry separate report money
    for the cash core and coverage audit; no historical purchase rate is reused.
    An explicit unlocked declaration exposes the remaining implementation gap.
    """
    outstanding = {row["obligation_id"]: row for row in debts}
    values, records, claimed, statements, evidence_used = {}, [], set(), set(), set()
    review_items = []
    review_sha = None
    if declarations is not None:
        document = load_yaml(declarations)
        require(type(document) is dict and set(document) == {
            "schema", "cutover", "as_of", "report_currency", "statements"},
            "DEBT_SETTLEMENT_INVALID", "清偿文件须含 schema/cutover/as_of/report_currency/statements",
            f"{declarations.resolve()}:1")
        for key, valid in (
            ("schema", type(document["schema"]) is int and document["schema"] == 6),
            ("cutover", document["cutover"] == cutover.isoformat()),
            ("as_of", document["as_of"] == as_of.isoformat()),
            ("report_currency", document["report_currency"] == report_currency),
            ("statements", type(document["statements"]) is list),
        ):
            require(valid, "DEBT_SETTLEMENT_INVALID", f"清偿文件 {key} 与报告不符或格式无效",
                    yaml_key_location(declarations, key))
        review_sha = sha256(input_bytes(declarations)).hexdigest()
        for index, statement in enumerate(document["statements"]):
            keys = ("statements", index)
            where = yaml_path_location(declarations, keys)
            common = {"id", "kind", "evidence", "reason", "allocations"}
            require(type(statement) is dict and set(statement) in (
                common, common | {"locked_on", "valid_until", "report_amount"}),
                "DEBT_SETTLEMENT_INVALID", "清偿账单字段无效", where)
            kind = statement["kind"]
            require(type(kind) is str and kind in ("locked_bill", "unlocked") and
                    set(statement) == (common | {"locked_on", "valid_until", "report_amount"}
                                       if kind == "locked_bill" else common),
                    "DEBT_SETTLEMENT_INVALID", "kind 须为 locked_bill 或 unlocked，且字段须匹配", where)
            ident = statement["id"]
            require(type(ident) is str and ID.fullmatch(ident) is not None and ident not in statements,
                    "DEBT_SETTLEMENT_DUPLICATE", "账单 ID 非法或重复", where)
            statements.add(ident)
            for field in ("evidence", "reason"):
                require(type(statement[field]) is str and bool(statement[field].strip()),
                        "DEBT_SETTLEMENT_INVALID", f"账单 {field} 须记录可复核证据及审阅理由",
                        yaml_path_location(declarations, (*keys, field)))
            require(statement["evidence"].strip() not in evidence_used, "DEBT_SETTLEMENT_DUPLICATE",
                    "同一账单证据须合并为一组分摊，不能重复计价", where)
            evidence_used.add(statement["evidence"].strip())
            total = None
            if kind == "locked_bill":
                dates = {}
                for field in ("locked_on", "valid_until"):
                    raw = statement[field]
                    require(type(raw) is str, "DEBT_SETTLEMENT_TIME_INVALID", "锁定与有效日期须为带引号的 ISO 日期",
                            yaml_path_location(declarations, (*keys, field)))
                    try:
                        dates[field] = date.fromisoformat(raw)
                    except ValueError:
                        require(False, "DEBT_SETTLEMENT_TIME_INVALID", "锁定与有效日期无效",
                                yaml_path_location(declarations, (*keys, field)))
                require(dates["locked_on"] <= as_of <= dates["valid_until"], "DEBT_SETTLEMENT_TIME_INVALID",
                        "账单尚未锁定或在报告日已失效；未来还款价不能倒填历史报告", where)
                raw = statement["report_amount"]
                require(type(raw) is str and AMOUNT.fullmatch(raw) is not None,
                        "DEBT_SETTLEMENT_AMOUNT_INVALID", "账单总额须为非负精确十进制字符串",
                        yaml_path_location(declarations, (*keys, "report_amount")))
                total = Fraction(raw)
            allocations = statement["allocations"]
            require(type(allocations) is list and bool(allocations), "DEBT_SETTLEMENT_INVALID",
                    "账单须分摊到至少一份当前未清外币责任", where)
            allocated = Fraction(0)
            has_foreign = False
            for allocation_index, allocation in enumerate(allocations):
                allocation_keys = (*keys, "allocations", allocation_index)
                source = yaml_path_location(declarations, allocation_keys)
                fields = {"obligation_id", "purchase_ref", "remaining_native"}
                require(type(allocation) is dict and set(allocation) == (
                    fields | {"report_amount"} if kind == "locked_bill" else fields),
                    "DEBT_SETTLEMENT_INVALID", "清偿分摊字段无效", source)
                debt_id = allocation["obligation_id"]
                require(type(debt_id) is str and debt_id in outstanding, "DEBT_SETTLEMENT_OBLIGATION_UNKNOWN",
                        "清偿决定须指向报告日仍未结清的已审责任", source)
                require(debt_id not in claimed, "DEBT_SETTLEMENT_DUPLICATE", "同一责任重复计价", source)
                claimed.add(debt_id)
                row = outstanding[debt_id]
                ref = allocation["purchase_ref"]
                require(type(ref) is dict and set(ref) == set(row["purchase_ref"]) and
                        all(type(value) is type(row["purchase_ref"][field]) for field, value in ref.items()) and
                        _key(ref) == _key(row["purchase_ref"]), "DEBT_SETTLEMENT_REF_INVALID",
                        "清偿决定的消费来源不符或已过期", source)
                foreign = row["commodity"] != report_currency
                has_foreign |= foreign
                require(foreign or kind == "locked_bill", "DEBT_SETTLEMENT_CURRENCY_INVALID",
                        "本币责任不需要未锁定外汇路径", source)
                if kind == "locked_bill" and row.get("purchase_date") is not None:
                    require(dates["locked_on"] >= date.fromisoformat(row["purchase_date"]),
                            "DEBT_SETTLEMENT_TIME_INVALID", "清偿金额锁定日不能早于这笔实际消费", source)
                raw = allocation["remaining_native"]
                require(type(raw) is str and AMOUNT.fullmatch(raw) is not None and Fraction(raw) > 0 and
                        Fraction(raw) == Fraction(row["remaining_native"]), "DEBT_SETTLEMENT_NATIVE_MISMATCH",
                        f"清偿决定须覆盖当前全部原币未偿额 {row['remaining_native']} {row['commodity']}；还款后重新审阅", source)
                if kind == "unlocked":
                    review_items.append(review_item(
                        "SETTLEMENT_PATH_UNSUPPORTED", source, ref=row["purchase_ref"],
                        obligation_id=debt_id, account=row["account"], commodity=row["commodity"],
                        remaining_native=row["remaining_native"], evidence=statement["evidence"]))
                    continue
                raw = allocation["report_amount"]
                require(type(raw) is str and AMOUNT.fullmatch(raw) is not None,
                        "DEBT_SETTLEMENT_AMOUNT_INVALID", "清偿分摊额须为非负精确十进制字符串", source)
                amount = Fraction(raw)
                require(foreign or amount == Fraction(row["remaining_native"]), "DEBT_SETTLEMENT_CURRENCY_INVALID",
                        "混合账单里的本币责任必须按原未偿额分摊，不能改价", source)
                allocated += amount
                values[debt_id] = exact_text(amount)
                records.append({"obligation_id": debt_id, "statement_id": ident,
                                "remaining_native": row["remaining_native"], "commodity": row["commodity"],
                                "report_amount": exact_text(amount), "report_currency": report_currency,
                                "locked_on": statement["locked_on"], "valid_until": statement["valid_until"],
                                "evidence": statement["evidence"], "source": source})
            if kind == "locked_bill":
                require(has_foreign, "DEBT_SETTLEMENT_CURRENCY_INVALID", "锁定清偿账单须至少含一笔外币责任", where)
                require(allocated == total, "DEBT_SETTLEMENT_ALLOCATION_MISMATCH",
                        f"分摊合计 {exact_text(allocated)} 不等于锁定账单总额 {exact_text(total)}；含费用总额只分摊一次", where)
    for row in debts:
        if row["commodity"] != report_currency and row["obligation_id"] not in claimed:
            review_items.append(review_item(
                "SETTLEMENT_VALUE_MISSING", sources.get(_key(row["purchase_ref"])), ref=row["purchase_ref"],
                obligation_id=row["obligation_id"], account=row["account"], commodity=row["commodity"],
                owner_id=row["owner_id"], remaining_native=row["remaining_native"]))
    valued = [{**row, "settlement_currency": report_currency,
               "remaining_report": row["remaining_native"] if row["commodity"] == report_currency else values.get(row["obligation_id"])}
              for row in debts]
    return {"review_state": "pending" if review_items else "complete", "review_sha256": review_sha,
            "valued_debts": valued, "allocations": records, "review_items": review_items,
            "reason_codes": list(dict.fromkeys(item["reason_code"] for item in review_items))}

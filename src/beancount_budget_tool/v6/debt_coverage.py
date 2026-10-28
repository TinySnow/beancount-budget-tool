"""Compile explicit report-date reservations from reviewed payable H cash."""

from __future__ import annotations

from collections import defaultdict
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
from .core import Coverage
from .review_diagnostics import review_item


AMOUNT = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")


def _currency(row: dict) -> str:
    return row.get("settlement_currency", row["commodity"])


def _remaining(row: dict) -> Fraction:
    return Fraction(row.get("remaining_report", row["remaining_native"]))


def audit_debt_coverage(cash: dict, debts: list[dict], cutover: date, as_of: date,
                        sources: dict[str, str], declarations: Path | None = None) -> dict:
    """A decision may reserve several cash slices or explicitly reserve none.

    No same-purpose matching, FIFO allocation or capacity clamping happens.
    Every named-purpose debt with a proved report value needs a decision; daily-purpose debt retains
    the existing full-uncovered behavior unless a reservation is declared.
    """
    currency = cash["report_currency"]
    outstanding = {row["obligation_id"]: row for row in debts}
    owner_capacity = {row["owner_id"]: Fraction(row["units"]) for row in cash["owner_units"]}
    outside = "-" if "-" not in cash["spendable_owner_ids"] else None
    slices = {}
    physical = {}
    for position in cash["known_payable_positions"]:
        coordinate = (position["account"], position["commodity"], position["lot"])
        physical[coordinate] = sum((Fraction(part["units"]) for part in position["owner_units"]),
                                   Fraction(0))
        for part in position["owner_units"]:
            if part["owner_id"] != outside:
                slices[(*coordinate, part["owner_id"])] = Fraction(part["units"])

    required_ids = {row["obligation_id"] for row in debts
                    if _currency(row) == currency and row["owner_id"] not in cash["spendable_owner_ids"]}
    decisions = set()
    covered = defaultdict(Fraction)
    coordinate_used = defaultdict(Fraction)
    slice_used = defaultdict(Fraction)
    owner_used = defaultdict(Fraction)
    allocations = []
    records = []
    review_sha = None
    if declarations is not None:
        document = load_yaml(declarations)
        require(type(document) is dict and set(document) == {
            "schema", "cutover", "as_of", "report_currency", "decisions"},
            "DEBT_COVERAGE_INVALID", "覆盖文件须含 schema/cutover/as_of/report_currency/decisions",
            f"{declarations.resolve()}:1")
        for key, valid in (
            ("schema", type(document["schema"]) is int and document["schema"] == 6),
            ("cutover", document["cutover"] == cutover.isoformat()),
            ("as_of", document["as_of"] == as_of.isoformat()),
            ("report_currency", document["report_currency"] == currency),
            ("decisions", type(document["decisions"]) is list),
        ):
            require(valid, "DEBT_COVERAGE_INVALID", f"覆盖文件 {key} 与本报告不符或格式无效",
                    yaml_key_location(declarations, key))
        review_sha = sha256(input_bytes(declarations)).hexdigest()
        for index, decision in enumerate(document["decisions"]):
            keys = ("decisions", index)
            where = yaml_path_location(declarations, keys)
            require(type(decision) is dict and set(decision) == {
                "obligation_id", "purchase_ref", "resources", "reason"},
                "DEBT_COVERAGE_INVALID", "每份覆盖决定须含 obligation_id/purchase_ref/resources/reason", where)
            ident = decision["obligation_id"]
            require(type(ident) is str and ident in outstanding, "DEBT_COVERAGE_OBLIGATION_UNKNOWN",
                    "覆盖决定须指向报告日仍未结清的已审责任", yaml_path_location(declarations, (*keys, "obligation_id")))
            require(ident not in decisions, "DEBT_COVERAGE_DUPLICATE",
                    "同一责任重复声明覆盖决定", where)
            row = outstanding[ident]
            require(type(decision["purchase_ref"]) is dict and
                    set(decision["purchase_ref"]) == set(row["purchase_ref"]) and
                    all(type(value) is type(row["purchase_ref"][field])
                        for field, value in decision["purchase_ref"].items()) and
                    _key(decision["purchase_ref"]) == _key(row["purchase_ref"]),
                    "DEBT_COVERAGE_REF_INVALID", "覆盖决定的消费来源与已审责任不符或已过期",
                    yaml_path_location(declarations, (*keys, "purchase_ref")))
            require(_currency(row) == currency, "DEBT_COVERAGE_CURRENCY_UNSUPPORTED",
                    "覆盖须使用报告货币责任金额或已审外币清偿金额", where)
            require(type(decision["reason"]) is str and bool(decision["reason"].strip()),
                    "DEBT_COVERAGE_INVALID", "覆盖或明确不覆盖须留下审阅理由",
                    yaml_path_location(declarations, (*keys, "reason")))
            require(type(decision["resources"]) is list, "DEBT_COVERAGE_INVALID",
                    "resources 须为列表；明确无专用覆盖时填写空列表",
                    yaml_path_location(declarations, (*keys, "resources")))
            decisions.add(ident)
            require(row["owner_id"] in set(cash["registered_owner_ids"]) | set(cash["spendable_owner_ids"]),
                    "DEBT_COVERAGE_OWNER_UNKNOWN", "责任用途未登记在当前资产用途名册中", where)
            used_in_decision = set()
            for resource_index, resource in enumerate(decision["resources"]):
                resource_keys = (*keys, "resources", resource_index)
                source = yaml_path_location(declarations, resource_keys)
                require(type(resource) is dict and set(resource) == {
                    "account", "commodity", "lot", "owner_id", "amount"},
                    "DEBT_COVERAGE_RESOURCE_INVALID", "覆盖资源须含完整账户/币种/lot/用途与金额", source)
                require(all(type(resource[field]) is str for field in resource),
                        "DEBT_COVERAGE_RESOURCE_INVALID", "覆盖资源字段须为字符串", source)
                coordinate = (resource["account"], resource["commodity"], resource["lot"])
                owner = resource["owner_id"]
                slice_key = (*coordinate, owner)
                require(slice_key in slices and slices[slice_key] > 0,
                        "DEBT_COVERAGE_RESOURCE_INVALID", "覆盖资源不是当前已审、可直接支付的正数用途切片", source)
                require(slice_key not in used_in_decision, "DEBT_COVERAGE_DUPLICATE",
                        "同一责任内重复列出同一现金切片", source)
                used_in_decision.add(slice_key)
                require(AMOUNT.fullmatch(resource["amount"]) is not None and Fraction(resource["amount"]) > 0,
                        "DEBT_COVERAGE_AMOUNT_INVALID", "覆盖金额须为正的十进制字符串",
                        yaml_path_location(declarations, (*resource_keys, "amount")))
                amount = Fraction(resource["amount"])
                covered[ident] += amount
                coordinate_used[coordinate] += amount
                slice_used[slice_key] += amount
                owner_used[owner] += amount
                require(covered[ident] <= _remaining(row),
                        "DEBT_COVERAGE_OBLIGATION_EXCEEDED",
                        f"总覆盖 {exact_text(covered[ident])} 超过责任清偿额 {exact_text(_remaining(row))} {currency}", source)
                require(slice_used[slice_key] <= slices[slice_key],
                        "DEBT_COVERAGE_RESOURCE_EXCEEDED",
                        f"同一现金切片总覆盖 {exact_text(slice_used[slice_key])} 超过当前份额 {exact_text(slices[slice_key])}", source)
                require(coordinate_used[coordinate] <= physical[coordinate],
                        "DEBT_COVERAGE_INVENTORY_EXCEEDED",
                        f"本账户币种/lot 总覆盖 {exact_text(coordinate_used[coordinate])} 超过真实现金余额 {exact_text(physical[coordinate])}", source)
                require(owner_used[owner] <= max(owner_capacity.get(owner, Fraction(0)), Fraction(0)),
                        "DEBT_COVERAGE_OWNER_EXCEEDED",
                        f"用途总覆盖 {exact_text(owner_used[owner])} 超过可覆盖容量 "
                        f"{exact_text(max(owner_capacity.get(owner, Fraction(0)), Fraction(0)))}"
                        f"（跨账户净余额 {exact_text(owner_capacity.get(owner, Fraction(0)))}）", source)
                allocations.append(Coverage(
                    id=f"coverage:{ident}:{resource_index}", resource_id=f"payable:{owner}",
                    obligation_id=ident, amount=amount,
                    evidence=f"{cash['ledger_snapshot']}:{as_of}:{review_sha}:{source}"))
                records.append({"obligation_id": ident, "account": coordinate[0],
                                "commodity": coordinate[1], "lot": coordinate[2], "owner_id": owner,
                                "amount": exact_text(amount), "source": source})

    missing = sorted(required_ids - decisions)
    review_items = [review_item(
        "DEBT_COVERAGE_PENDING", sources[_key(outstanding[ident]["purchase_ref"])],
        obligation_id=ident, account=outstanding[ident]["account"],
        commodity=outstanding[ident]["commodity"], owner_id=outstanding[ident]["owner_id"],
        remaining_native=outstanding[ident]["remaining_native"],
        remaining_report=exact_text(_remaining(outstanding[ident])), settlement_currency=currency,
        ref=outstanding[ident]["purchase_ref"])
        for ident in missing]
    return {"review_state": "pending" if missing else "complete", "review_sha256": review_sha,
            "coverage": tuple(allocations), "allocations": records,
            "missing_decision_ids": missing, "review_items": review_items,
            "obligations": [{"obligation_id": ident, "covered": exact_text(covered[ident]),
                             "uncovered": exact_text(_remaining(row) - covered[ident])}
                            for ident, row in sorted(outstanding.items()) if _currency(row) == currency]}

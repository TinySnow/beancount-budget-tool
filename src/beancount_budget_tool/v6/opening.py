"""Validate cutover H ownership and K book-flow openings without inventing assets."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path
import re

from ..errors import require
from ..exact import exact_text
from ..yamlstrict import load_yaml
from .accounts import audit_accounts


NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")


def _units(value: object, source: str) -> Fraction:
    require(type(value) is str and bool(NUMBER.fullmatch(value)),
            "OPENING_NUMBER_INVALID", "数量须是精确十进制或有理数字符串", source)
    return Fraction(value)


def _owner(value: object, source: str) -> str:
    require(type(value) is str and bool(value) and value == value.strip() and
            value != "?" and not any(c in value for c in "|,，"),
            "OPENING_OWNER_INVALID", "H 归属须是明确用途 ID 或 '-'，未知部分请留待审", source)
    return value


def audit_opening(root: Path, cutover: date, accounts: Path,
                  declarations: Path | None = None) -> dict:
    """Check exact cutover quantities for reviewed H/K accounts.

    H claims partition booked native units by full account/commodity/lot;
    incomplete partitions stay pending.  K claims acknowledge signed book-flow
    coordinates only: they are never reinterpreted as real holdings or owner
    balances.  Account semantics and payment scope remain separate reviews.
    """
    account_audit = audit_accounts(root, cutover, accounts)
    rows = {row["account"]: row for row in account_audit["accounts"]}
    expected_h: dict[tuple[str, str, str], Fraction] = {}
    expected_k: dict[tuple[str, str, str], Fraction] = {}
    for row in rows.values():
        semantics = row.get("semantics")
        for coordinate in row["opening_coordinates"]:
            key = (row["account"], coordinate["commodity"], coordinate["lot"])
            amount = _units(coordinate["signed_units"], row["account"])
            if semantics == "H" and amount != 0:
                expected_h[key] = amount
            elif semantics == "K":
                expected_k[key] = amount
        if semantics == "K" and not row["opening_coordinates"]:
            commodity = next(iter(row["commodities"]))
            expected_k[(row["account"], commodity, f"{commodity}/no-cost")] = Fraction(0)

    h_claimed: dict[tuple[str, str, str], Fraction] = defaultdict(Fraction)
    k_claimed: set[tuple[str, str, str]] = set()
    if declarations is not None:
        document = load_yaml(declarations)
        required = {"schema", "cutover", "h_holdings", "k_openings"}
        require(isinstance(document, dict) and required <= set(document) and
                set(document) <= required | {"ledger_snapshot", "opening_fingerprint"},
            "OPENING_DECLARATION_INVALID", "期初声明字段不完整或含未知字段", str(declarations))
        require(type(document["schema"]) is int and document["schema"] == 6 and
                type(document["cutover"]) is str and document["cutover"] == cutover.isoformat(),
                "OPENING_DECLARATION_INVALID", "期初声明的 schema/cutover 不匹配", str(declarations))
        require(isinstance(document["h_holdings"], list) and isinstance(document["k_openings"], list),
                "OPENING_DECLARATION_INVALID", "h_holdings 与 k_openings 必须是列表", str(declarations))
        seen_h = set()
        for item in document["h_holdings"]:
            require(isinstance(item, dict) and set(item) == {
                "account", "commodity", "lot", "owner", "units", "reason"},
                "OPENING_CLAIM_INVALID", "H 期初切片字段不完整或含未知字段", str(declarations))
            require(all(type(item[field]) is str and bool(item[field])
                        for field in ("account", "commodity", "lot")),
                    "OPENING_CLAIM_INVALID", "H 坐标字段必须是非空字符串", str(declarations))
            key = (item["account"], item["commodity"], item["lot"])
            source = str(key)
            require(key in expected_h, "OPENING_COORDINATE_INVALID",
                    "H 期初切片未对应真实、非零的 booked 坐标", source)
            owner = _owner(item["owner"], source)
            require(type(item["reason"]) is str and bool(item["reason"].strip()),
                    "OPENING_REASON_MISSING", "期初归属须有人工作出理由", source)
            amount = _units(item["units"], source)
            require(amount > 0, "OPENING_CLAIM_INVALID", "H 份额必须为正", source)
            claim_key = (*key, owner)
            require(claim_key not in seen_h, "OPENING_DUPLICATE_CLAIM",
                    "同一 H 坐标及用途被重复认领", source)
            seen_h.add(claim_key)
            h_claimed[key] += amount
            require(h_claimed[key] <= expected_h[key], "OPENING_OVERALLOCATED",
                    "H 期初用途份额超过 booked 数量", source)
        for item in document["k_openings"]:
            require(isinstance(item, dict) and set(item) == {
                "account", "commodity", "lot", "signed_units", "reason"},
                "OPENING_CLAIM_INVALID", "K 期初确认字段不完整或含未知字段", str(declarations))
            require(all(type(item[field]) is str and bool(item[field])
                        for field in ("account", "commodity", "lot")),
                    "OPENING_CLAIM_INVALID", "K 坐标字段必须是非空字符串", str(declarations))
            key = (item["account"], item["commodity"], item["lot"])
            source = str(key)
            require(key in expected_k, "OPENING_COORDINATE_INVALID",
                    "K 期初确认未对应账面累计坐标", source)
            require(key not in k_claimed, "OPENING_DUPLICATE_CLAIM",
                    "K 期初坐标重复确认", source)
            require(type(item["reason"]) is str and bool(item["reason"].strip()),
                    "OPENING_REASON_MISSING", "K 期初账面累计须有人工作出依据", source)
            require(_units(item["signed_units"], source) == expected_k[key],
                    "K_OPENING_MISMATCH", "K 期初确认数与 booked 累计不一致", source)
            k_claimed.add(key)

    missing_h = [{"account": a, "commodity": c, "lot": lot,
                  "unreviewed_units": exact_text(amount - h_claimed[(a, c, lot)])}
                 for (a, c, lot), amount in sorted(expected_h.items())
                 if h_claimed[(a, c, lot)] < amount]
    missing_k = [{"account": a, "commodity": c, "lot": lot,
                  "signed_units": exact_text(expected_k[(a, c, lot)])}
                 for a, c, lot in sorted(expected_k) if (a, c, lot) not in k_claimed]
    return {"mode": "v6_opening_audit_only", "cutover": cutover.isoformat(),
            "ledger_snapshot": account_audit["ledger_snapshot"],
            "opening_fingerprint": account_audit["opening_fingerprint"],
            "review_state": ("complete" if account_audit["review_state"] == "complete" and
                             not missing_h and not missing_k else "pending"),
            "missing_account_declarations": account_audit["missing_declarations"],
            "pending_payment_scope": account_audit["pending_payment_scope"],
            "missing_h_ownership": missing_h, "missing_k_openings": missing_k,
            "note": "期初审阅不证明 cutover 后逐腿归属、责任或首页可花"}

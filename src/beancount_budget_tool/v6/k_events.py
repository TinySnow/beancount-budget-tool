"""Reviewed simple cash subscriptions and redemptions for K book-flow accounts.

K units remain signed bookkeeping flow. Event purposes can establish a cash
boundary, but never create fund shares, market value, or an owner inventory
inside K.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial
from ..yamlstrict import load_yaml, yaml_path_location
from .accounts import audit_accounts
from .classification import _key
from .facts import FactIndex, future_transaction_refs
from .inline_amounts import parse_inline_amounts
from .ownership import _owners, audit_ownership
from .provenance import valid_digest
from .source import scan_v6_source


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")


def _number(value: object, source: str) -> Fraction:
    require(type(value) is str and bool(NUMBER.fullmatch(value)),
            "K_EVENT_UNITS_INVALID", "K 事件切片须使用精确数字字符串", source)
    return Fraction(value)


def audit_k_events(root: Path, cutover: date, accounts: Path,
                   ownership: Path, declarations: Path | None = None) -> dict:
    """Bind each approved K intent to exactly one same-transaction H cash leg.

    A reviewed event remains pending until the H cash leg has matching owner
    slices. This prevents an approved K intention from silently rewriting
    actual cash ownership or counting a budget allocation twice.
    """
    scan_v6_source(root, cutover)
    account_audit = audit_accounts(root, cutover, accounts)
    owner_audit = audit_ownership(root, cutover, accounts, ownership)
    fin = load_financial(root)
    facts = FactIndex(fin)
    report_currency = account_audit["report_currency"]
    modes = {row["account"]: row.get("semantics") for row in account_audit["accounts"]}
    owner_document = load_yaml(ownership)
    names = _owners(owner_document["owners"])
    owner_ids = set(names.values())
    cash_claims = {_key(row["ref"]): row for row in owner_audit["ownership"]}
    future_pairs = {}
    for entry, refs in future_transaction_refs(fin, cutover):
        if len(entry.postings) != 2:
            continue
        modes_future = [account_audit["declared_semantics"].get(p.account) for p in entry.postings]
        if set(modes_future) == {"H", "K"}:
            index = modes_future.index("K")
            future_pairs[_key(refs[index])] = refs[1 - index]
    candidates = {}
    inline_funds = {}
    order = []
    asset_root = fin.options["name_assets"]
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        k_postings = [p for p in entry.postings if modes.get(p.account) == "K"]
        for posting in k_postings:
            k_ref = facts.for_posting(posting).as_dict()
            key = _key(k_ref)
            require(key not in candidates, "SOURCE_REF_AMBIGUOUS",
                    "K booked 来源重复", key)
            amount = exact(posting.units.number)
            reason = None
            cash = None
            if len(entry.postings) != 2 or len(k_postings) != 1:
                reason = "K_EVENT_SHAPE_UNSUPPORTED"
            else:
                cash = next(p for p in entry.postings if p is not posting)
                if (cash.account.split(":", 1)[0] != asset_root or
                        modes.get(cash.account) != "H" or
                        posting.units.currency != report_currency or
                        cash.units.currency != report_currency or
                        amount == 0 or exact(cash.units.number) != -amount or
                        posting.cost is not None or posting.price is not None or
                        cash.cost is not None or cash.price is not None):
                    reason = "K_EVENT_SHAPE_UNSUPPORTED"
                else:
                    for leg in (posting, cash):
                        notation = fin.raw_price(leg)[0]
                        if notation != "none":
                            reason = "K_EVENT_SHAPE_UNSUPPORTED"
            candidate = {"k_ref": k_ref, "k_account": posting.account,
                         "date": entry.date.isoformat(),
                         "signed_k_units": exact_text(amount),
                         "commodity": posting.units.currency,
                         "kind": "subscription" if amount > 0 else "redemption" if amount < 0 else None,
                         "cash_ref": facts.for_posting(cash).as_dict() if cash is not None and
                                     reason is None else None,
                         "cash_account": cash.account if cash is not None and reason is None else None,
                         "shape": "simple_cash" if reason is None else "unsupported",
                         "reason_code": reason}
            candidates[key] = candidate
            raw_fund = (posting.meta or {}).get("fund")
            if raw_fund is not None:
                inline_funds[key] = raw_fund
            order.append(key)

    claims = {}
    review_sha = None
    if declarations is not None:
        document = load_yaml(declarations)
        require(type(document) is dict and set(document) == {
            "schema", "cutover", "ledger_snapshot", "loader_version", "events"},
            "K_EVENT_DECLARATION_INVALID", "K 事件侧文件字段不完整或含未知字段", str(declarations))
        require(type(document["schema"]) is int and document["schema"] == 6 and
                type(document["cutover"]) is str and document["cutover"] == cutover.isoformat() and
                valid_digest(document["ledger_snapshot"]) and
                document["loader_version"] == fin.loader_version and
                type(document["events"]) is list,
                "K_EVENT_DECLARATION_INVALID", "K 事件版本或原审快照格式不匹配", str(declarations))
        review_sha = sha256(declarations.read_bytes()).hexdigest()
        used_cash = set()
        used_slices = set()
        for row_index, row in enumerate(document["events"]):
            require(type(row) is dict and set(row) == {
                "k_ref", "cash_ref", "kind", "reason", "slices"},
                "K_EVENT_CLAIM_INVALID", "K 事件须声明两侧来源、类型、理由及切片")
            require(type(row["k_ref"]) is dict and type(row["cash_ref"]) is dict,
                    "K_EVENT_CLAIM_INVALID", "K 事件必须绑定完整 booked 引用")
            key = _key(row["k_ref"])
            location = yaml_path_location(declarations, ("events", row_index))
            if key in future_pairs:
                require(row["cash_ref"] == future_pairs[key], "K_EVENT_CASH_REF_INVALID",
                        "未来 K 现金来源须为同一交易的完整 H 对手", location)
                continue
            require(key in candidates, "K_EVENT_REF_INVALID", "K 来源不存在或过期", location)
            require(key not in claims, "K_EVENT_DOUBLE_CLAIM", "K 事件重复认领", key)
            require(key not in inline_funds, "K_EVENT_DOUBLE_CLAIM",
                    "K 行内 fund 与外部申赎事件不能同时认领", key)
            candidate = candidates[key]
            require(candidate["shape"] == "simple_cash", "K_EVENT_UNSUPPORTED",
                    "本 K 事件不符合简单现金申赎协议", key)
            cash_key = _key(row["cash_ref"])
            require(cash_key == _key(candidate["cash_ref"]) and cash_key not in used_cash,
                    "K_EVENT_CASH_REF_INVALID", "K 现金对手不存在、过期或被重复使用", cash_key)
            used_cash.add(cash_key)
            require(row["kind"] == candidate["kind"] and
                    type(row["reason"]) is str and bool(row["reason"].strip()) and
                    type(row["slices"]) is list and bool(row["slices"]),
                    "K_EVENT_CLAIM_INVALID", "K 事件类型、理由或切片无效", key)
            k_total = Fraction(0)
            cash_total = Fraction(0)
            parsed = []
            allocations = {}
            for part in row["slices"]:
                require(type(part) is dict and set(part) == {
                    "id", "k_owner_id", "cash_owner_id", "signed_k_units",
                    "signed_cash_units", "boundary_role"},
                    "K_EVENT_SLICE_INVALID", "K 切片须逐份声明双方用途、数量和边界角色", key)
                ident, k_owner, cash_owner = (part[field] for field in
                                              ("id", "k_owner_id", "cash_owner_id"))
                require(type(ident) is str and bool(ID.fullmatch(ident)) and
                        ident not in used_slices,
                        "K_EVENT_SLICE_INVALID", "K 切片 ID 非法或重复", key)
                used_slices.add(ident)
                require(type(k_owner) is str and (k_owner == "-" or k_owner in owner_ids) and
                        type(cash_owner) is str and (cash_owner == "-" or cash_owner in owner_ids),
                        "K_EVENT_OWNER_INVALID", "K 切片引用未登记用途", key)
                k_units = _number(part["signed_k_units"], key)
                cash_units = _number(part["signed_cash_units"], key)
                require(k_units != 0 and k_units * amount > 0 and cash_units == -k_units,
                        "K_EVENT_UNITS_MISMATCH", "K 与现金切片须同币种精确反向", key)
                role = part["boundary_role"]
                valid = ((role == "preserve" and k_owner == cash_owner) or
                         (role == "allocate" and amount > 0 and k_owner != cash_owner) or
                         (role == "release" and amount < 0 and k_owner != cash_owner))
                require(valid, "K_EVENT_ROLE_INVALID", "K 用途边界与申赎方向不一致", key)
                k_total += k_units
                cash_total += cash_units
                if k_owner != "-":
                    allocations[k_owner] = allocations.get(k_owner, Fraction(0)) + k_units
                if cash_owner != "-":
                    allocations[cash_owner] = allocations.get(cash_owner, Fraction(0)) + cash_units
                parsed.append({"id": ident, "k_owner_id": k_owner,
                               "cash_owner_id": cash_owner,
                               "signed_k_units": exact_text(k_units),
                               "signed_cash_units": exact_text(cash_units),
                               "boundary_role": role})
            require(k_total == Fraction(candidate["signed_k_units"]) and cash_total == -k_total,
                    "K_EVENT_UNITS_MISMATCH", "K 与现金切片未精确加回 booked 分录", key)
            claims[key] = {"reason": row["reason"], "slices": parsed,
                           "net_allocations": {owner: exact_text(value) for owner, value in sorted(allocations.items()) if value}}

    def inline_claim(key: str, cash_claim: dict) -> dict:
        candidate = candidates[key]
        amount = Fraction(candidate["signed_k_units"])
        parsed = parse_inline_amounts(inline_funds[key], amount, str(candidate["k_ref"]),
                                      known_names=set(names), code="K_EVENT_UNITS_MISMATCH")
        k_remaining: dict[str, Fraction] = {}
        for name, units in parsed:
            owner = "-" if name == "-" else names[name]
            k_remaining[owner] = k_remaining.get(owner, Fraction(0)) + abs(units)
        cash_remaining: dict[str, Fraction] = {}
        for part in cash_claim["slices"]:
            require(part["role"] == "hold", "K_EVENT_CASH_ROLE_INVALID",
                    "K 申赎现金腿的用途边界由 K 事件独占，H 腿须为 hold", key)
            units = Fraction(part["signed_units"])
            require(units * amount < 0, "K_EVENT_CASH_OWNER_MISMATCH",
                    "K 现金用途切片须与 K 申赎方向相反", key)
            owner = part["owner_id"]
            cash_remaining[owner] = cash_remaining.get(owner, Fraction(0)) + abs(units)
        require(sum(cash_remaining.values(), Fraction(0)) == abs(amount),
                "K_EVENT_CASH_OWNER_MISMATCH", "K 现金用途未精确加回申赎金额", key)
        slices = []
        allocations: dict[str, Fraction] = {}

        def pair(k_owner: str, cash_owner: str, units: Fraction) -> None:
            signed_k = units if amount > 0 else -units
            signed_cash = -signed_k
            role = ("preserve" if k_owner == cash_owner else
                    "allocate" if amount > 0 else "release")
            slices.append({"id": "inline_" + sha256(key.encode()).hexdigest()[:24] +
                           f"_{len(slices)}", "k_owner_id": k_owner,
                           "cash_owner_id": cash_owner,
                           "signed_k_units": exact_text(signed_k),
                           "signed_cash_units": exact_text(signed_cash),
                           "boundary_role": role})
            if k_owner != "-":
                allocations[k_owner] = allocations.get(k_owner, Fraction(0)) + signed_k
            if cash_owner != "-":
                allocations[cash_owner] = allocations.get(cash_owner, Fraction(0)) + signed_cash
            k_remaining[k_owner] -= units
            cash_remaining[cash_owner] -= units

        # Match preserved purposes first. The remaining positive vectors may
        # be paired in stable order; their net purpose changes are invariant.
        for owner in sorted(k_remaining.keys() & cash_remaining.keys()):
            units = min(k_remaining[owner], cash_remaining[owner])
            if units:
                pair(owner, owner, units)
        for k_owner in sorted(k_remaining):
            for cash_owner in sorted(cash_remaining):
                units = min(k_remaining[k_owner], cash_remaining[cash_owner])
                if units:
                    pair(k_owner, cash_owner, units)
        require(not any(k_remaining.values()) and not any(cash_remaining.values()),
                "K_EVENT_UNITS_MISMATCH", "K 行内 fund 与现金用途金额不匹配", key)
        return {"reason": "K 分录行内 fund 与 H 现金用途逐笔匹配",
                "slices": slices,
                "net_allocations": {owner: exact_text(value)
                                    for owner, value in sorted(allocations.items()) if value}}

    events = []
    for key in order:
        candidate = candidates[key]
        claim = claims.get(key)
        if claim is None and key in inline_funds and candidate["shape"] == "simple_cash":
            cash_claim = cash_claims.get(_key(candidate["cash_ref"]))
            if cash_claim is not None:
                claim = inline_claim(key, cash_claim)
        if candidate["shape"] != "simple_cash":
            state, codes = "UNSUPPORTED", ["K_EVENT_SHAPE_UNSUPPORTED"]
        elif claim is None:
            state, codes = "NEEDS_REVIEW", ["K_CASH_OWNER_PENDING" if key in inline_funds
                                               else "K_EVENT_PENDING"]
        else:
            cash_claim = cash_claims.get(_key(candidate["cash_ref"]))
            if cash_claim is None:
                state, codes = "NEEDS_REVIEW", ["K_CASH_OWNER_PENDING"]
            else:
                expected_cash = {}
                for part in claim["slices"]:
                    owner = part["cash_owner_id"]
                    expected_cash[owner] = expected_cash.get(owner, Fraction(0)) + Fraction(part["signed_cash_units"])
                actual_cash = {}
                for part in cash_claim["slices"]:
                    require(part["role"] == "hold", "K_EVENT_CASH_ROLE_INVALID",
                            "K 申赎现金腿的用途边界由 K 事件独占，H 腿须为 hold", key)
                    owner = part["owner_id"]
                    actual_cash[owner] = actual_cash.get(owner, Fraction(0)) + Fraction(part["signed_units"])
                require(actual_cash == expected_cash, "K_EVENT_CASH_OWNER_MISMATCH",
                        "K 用途切片与真实现金 H 归属不一致", key)
                state, codes = "CERTIFIED", []
        events.append({**candidate, "status": state, "reason_codes": codes,
                       "review_reason": claim["reason"] if claim else None,
                       "slices": claim["slices"] if claim else None,
                       "net_allocations": claim["net_allocations"] if state == "CERTIFIED" else None})
    return {"mode": "v6_k_simple_event_audit", "cutover": cutover.isoformat(),
            "ledger_snapshot": fin.digest, "review_sha256": review_sha,
            "review_state": "complete" if all(row["status"] == "CERTIFIED" for row in events) and
                            account_audit["review_state"] == "complete" else "pending",
            "certified_events": sum(row["status"] == "CERTIFIED" for row in events),
            "pending_events": sum(row["status"] == "NEEDS_REVIEW" for row in events),
            "unsupported_events": sum(row["status"] == "UNSUPPORTED" for row in events),
            "events": events,
            "note": "仅认证当次 K 申赎与真实现金对手；K 账面累计不代表基金份额、市值或用途库存"}

"""Exact-total, single-purpose H cash purchases of foreign currency."""

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
from ..yamlstrict import load_yaml, require_yaml
from .accounts import audit_accounts
from .classification import _key
from .facts import FactIndex, future_transaction_refs
from .ownership import _owners, audit_ownership
from .provenance import validate_event_declaration
from .source import scan_v6_source


NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")


def _number(value: object, path: Path, keys: tuple) -> Fraction:
    require_yaml(type(value) is str and bool(NUMBER.fullmatch(value)),
        "H_FX_UNITS_INVALID", "H 外汇原生数量须为精确数字字符串",
        path, keys)
    return Fraction(value)


def audit_h_fx(root: Path, cutover: date, accounts: Path, ownership: Path,
               declarations: Path | None = None) -> dict:
    """Prove a single-purpose FX cash move without changing its purpose.

    An original ``@@`` report-currency total must exactly equal the booked
    report-currency debit. This is not a historical expense quote or a
    current liquidation quote for home-page valuation.
    """
    scan_v6_source(root, cutover)
    account_audit = audit_accounts(root, cutover, accounts)
    owner_audit = audit_ownership(root, cutover, accounts, ownership)
    fin = load_financial(root)
    facts = FactIndex(fin)
    report_currency = account_audit["report_currency"]
    modes = {row["account"]: row.get("semantics") for row in account_audit["accounts"]}
    owner_ids = set(_owners(load_yaml(ownership)["owners"]).values())
    h_claims = {_key(row["ref"]): row for row in owner_audit["ownership"]}
    future_pairs = None
    candidates = {}
    order = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover or len(entry.postings) != 2:
            continue
        if not all(modes.get(p.account) == "H" for p in entry.postings):
            continue
        if len({p.units.currency for p in entry.postings}) != 2:
            continue
        report_legs = [p for p in entry.postings if p.units.currency == report_currency]
        if len(report_legs) != 1:
            continue
        debit = report_legs[0]
        acquired = next(p for p in entry.postings if p is not debit)
        notation, total, price_currency = fin.raw_price(acquired)
        shape = ("simple_buy" if exact(debit.units.number) < 0 and
                 exact(acquired.units.number) > 0 and
                 notation == "@@" and price_currency == report_currency and
                 total == -exact(debit.units.number) and
                 fin.raw_price(debit)[0] == "none" and
                 debit.cost is None and debit.price is None and
                 acquired.cost is None else "unsupported")
        from_ref = facts.for_posting(debit).as_dict()
        key = _key(from_ref)
        require(key not in candidates, "SOURCE_REF_AMBIGUOUS", "H 外汇来源重复", key)
        candidates[key] = {"from_ref": from_ref,
                           "to_ref": facts.for_posting(acquired).as_dict(),
                           "date": entry.date.isoformat(),
                           "from_account": debit.account,
                           "to_account": acquired.account,
                           "from_commodity": debit.units.currency,
                           "to_commodity": acquired.units.currency,
                           "signed_from_units": exact_text(exact(debit.units.number)),
                           "signed_to_units": exact_text(exact(acquired.units.number)),
                           "quoted_total": exact_text(total) if notation == "@@" else None,
                           "shape": shape}
        order.append(key)

    claims = {}
    claim_locations = {}
    review_sha = None
    if declarations is not None:
        document = load_yaml(declarations)
        validate_event_declaration(document, declarations, cutover, fin.loader_version,
                                   "H_FX_DECLARATION_INVALID")
        review_sha = sha256(declarations.read_bytes()).hexdigest()
        used_to = set()
        for row_index, row in enumerate(document["events"]):
            location = ("events", row_index)
            require_yaml(type(row) is dict and set(row) == {
                "from_ref", "to_ref", "owner_id", "signed_from_units",
                "signed_to_units", "boundary_role", "reason"} and
                type(row["from_ref"]) is dict and type(row["to_ref"]) is dict,
                "H_FX_CLAIM_INVALID", "H 外汇须有两侧完整 booked 引用、单一用途及人工理由",
                declarations, location)
            key = _key(row["from_ref"])
            if key not in candidates:
                if future_pairs is None:
                    future_pairs = {}
                    for entry, refs in future_transaction_refs(fin, cutover):
                        if len(entry.postings) != 2 or not all(
                                account_audit["declared_semantics"].get(p.account) == "H" for p in entry.postings):
                            continue
                        if len({p.units.currency for p in entry.postings}) != 2:
                            continue
                        report_legs = [index for index, p in enumerate(entry.postings)
                                       if p.units.currency == report_currency]
                        if len(report_legs) == 1:
                            index = report_legs[0]
                            future_pairs[_key(refs[index])] = refs[1 - index]
                if key in future_pairs:
                    require_yaml(row["to_ref"] == future_pairs[key],
                        "H_FX_TO_REF_INVALID", "未来 H 外汇转入来源须为同一交易的完整对手",
                        declarations, (*location, "to_ref"))
                    continue
            require_yaml(key in candidates,
                "H_FX_REF_INVALID", "H 外汇转出来源不存在或过期",
                declarations, (*location, "from_ref"))
            require_yaml(key not in claims,
                "H_FX_DOUBLE_CLAIM", "H 外汇来源重复认领",
                declarations, (*location, "from_ref"))
            candidate = candidates[key]
            require_yaml(candidate["shape"] == "simple_buy",
                "H_FX_UNSUPPORTED", "交易不符合单用途报告币种购汇协议",
                declarations, location)
            to_key = _key(row["to_ref"])
            require_yaml(to_key == _key(candidate["to_ref"]) and to_key not in used_to,
                "H_FX_TO_REF_INVALID", "H 外汇转入来源不存在、过期或重复",
                declarations, (*location, "to_ref"))
            used_to.add(to_key)
            owner = row["owner_id"]
            require_yaml(type(owner) is str and (owner == "-" or owner in owner_ids) and
                    row["boundary_role"] == "preserve" and
                    type(row["reason"]) is str and bool(row["reason"].strip()),
                "H_FX_PURPOSE_INVALID", "简单购汇只能保留已审的同一用途",
                declarations, location)
            require_yaml(_number(row["signed_from_units"], declarations, (*location, "signed_from_units")) == Fraction(candidate["signed_from_units"]) and
                    _number(row["signed_to_units"], declarations, (*location, "signed_to_units")) == Fraction(candidate["signed_to_units"]),
                "H_FX_UNITS_MISMATCH", "H 外汇两侧原生数量不等于 booked 数量",
                declarations, location)
            claim_locations[key] = location
            claims[key] = {"owner_id": owner, "reason": row["reason"]}

    events = []
    for key in order:
        candidate = candidates[key]
        claim = claims.get(key)
        if candidate["shape"] != "simple_buy":
            status, code = "UNSUPPORTED", "H_FX_SHAPE_UNSUPPORTED"
        elif claim is None:
            status, code = "NEEDS_REVIEW", "H_FX_PENDING"
        else:
            from_h = h_claims.get(_key(candidate["from_ref"]))
            to_h = h_claims.get(_key(candidate["to_ref"]))
            if from_h is None or to_h is None:
                status, code = "NEEDS_REVIEW", "H_FX_OWNER_PENDING"
            else:
                for h_claim, units in ((from_h, candidate["signed_from_units"]),
                                       (to_h, candidate["signed_to_units"])):
                    require_yaml(len(h_claim["slices"]) == 1 and
                            h_claim["slices"][0]["owner_id"] == claim["owner_id"] and
                            h_claim["slices"][0]["role"] == "hold" and
                            Fraction(h_claim["slices"][0]["signed_units"]) == Fraction(units),
                        "H_FX_OWNER_MISMATCH", "购汇两侧 H 份额须精确保留同一用途",
                        declarations, claim_locations[key])
                status, code = "CERTIFIED", None
        events.append({**candidate, "status": status, "reason_code": code,
                       "owner_id": claim["owner_id"] if status == "CERTIFIED" else None,
                       "review_reason": claim["reason"] if claim else None,
                       "net_allocations": {} if status == "CERTIFIED" else None})
    return {"mode": "v6_simple_h_fx_audit", "cutover": cutover.isoformat(),
            "ledger_snapshot": fin.digest, "review_sha256": review_sha,
            "review_state": "complete" if account_audit["review_state"] == "complete" and
                            all(row["status"] == "CERTIFIED" for row in events) else "pending",
            "certified_events": sum(row["status"] == "CERTIFIED" for row in events),
            "pending_events": sum(row["status"] == "NEEDS_REVIEW" for row in events),
            "unsupported_events": sum(row["status"] == "UNSUPPORTED" for row in events),
            "events": events,
            "note": "仅认证单一用途且有原文 @@ 总价的购汇搬移；不替代历史消费计价、当前清偿报价或首页"}

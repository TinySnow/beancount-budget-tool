"""Source-bound simple H-to-H transfers and their purpose boundaries."""

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
from .ownership import _owners, audit_ownership
from .provenance import valid_digest
from .source import scan_v6_source


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")


def _number(value: object, key: str) -> Fraction:
    require(type(value) is str and bool(NUMBER.fullmatch(value)),
            "H_TRANSFER_UNITS_INVALID", "H 转账切片须使用精确数字字符串", key)
    return Fraction(value)


def audit_h_transfers(root: Path, cutover: date, accounts: Path,
                      ownership: Path, declarations: Path | None = None,
                      opening: Path | None = None) -> dict:
    """Audit exact two-H transfers; no sale, FX or composite event guessing."""
    scan_v6_source(root, cutover)
    account_audit = audit_accounts(root, cutover, accounts)
    owner_audit = audit_ownership(root, cutover, accounts, ownership, opening)
    fin = load_financial(root)
    facts = FactIndex(fin)
    report_currency = account_audit["report_currency"]
    modes = {row["account"]: row.get("semantics") for row in account_audit["accounts"]}
    owner_document = load_yaml(ownership)
    owner_ids = set(_owners(owner_document["owners"]).values())
    h_claims = {_key(row["ref"]): row for row in owner_audit["ownership"]}
    future_pairs = {}
    for entry, refs in future_transaction_refs(fin, cutover):
        if len(entry.postings) != 2 or not all(
                account_audit["declared_semantics"].get(p.account) == "H" for p in entry.postings):
            continue
        negatives = [index for index, p in enumerate(entry.postings) if exact(p.units.number) < 0]
        positives = [index for index, p in enumerate(entry.postings) if exact(p.units.number) > 0]
        if len(negatives) == len(positives) == 1:
            future_pairs[_key(refs[negatives[0]])] = refs[positives[0]]
    candidates = {}
    order = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover or len(entry.postings) != 2:
            continue
        if not all(modes.get(p.account) == "H" for p in entry.postings):
            continue
        positives = [p for p in entry.postings if exact(p.units.number) > 0]
        negatives = [p for p in entry.postings if exact(p.units.number) < 0]
        # Without a unique direction, expose an unsupported source rather than
        # choosing a destination from posting order.
        if len(positives) == len(negatives) == 1:
            to_leg, from_leg = positives[0], negatives[0]
        else:
            from_leg, to_leg = entry.postings
        amount = exact(from_leg.units.number)
        shape = ("simple_cash" if len(positives) == len(negatives) == 1 and
                 from_leg.units.currency == to_leg.units.currency == report_currency and
                 amount == -exact(to_leg.units.number) and
                 all(p.cost is None and p.price is None and
                     fin.raw_price(p)[0] == "none" for p in entry.postings)
                 else "unsupported")
        from_ref = facts.for_posting(from_leg).as_dict()
        key = _key(from_ref)
        require(key not in candidates, "SOURCE_REF_AMBIGUOUS", "H 转账来源重复", key)
        candidates[key] = {"from_ref": from_ref,
                           "to_ref": facts.for_posting(to_leg).as_dict(),
                           "from_account": from_leg.account,
                           "to_account": to_leg.account,
                           "date": entry.date.isoformat(),
                           "commodity": from_leg.units.currency,
                           "signed_from_units": exact_text(amount),
                           "signed_to_units": exact_text(exact(to_leg.units.number)),
                           "shape": shape}
        order.append(key)

    claims = {}
    review_sha = None
    if declarations is not None:
        document = load_yaml(declarations)
        require(type(document) is dict and set(document) == {
            "schema", "cutover", "ledger_snapshot", "loader_version", "events"},
            "H_TRANSFER_DECLARATION_INVALID", "H 转账侧文件字段不完整或含未知字段")
        require(type(document["schema"]) is int and document["schema"] == 6 and
                type(document["cutover"]) is str and document["cutover"] == cutover.isoformat() and
                valid_digest(document["ledger_snapshot"]) and
                document["loader_version"] == fin.loader_version and
                type(document["events"]) is list,
                "H_TRANSFER_DECLARATION_INVALID", "H 转账版本或原审快照格式不匹配")
        review_sha = sha256(declarations.read_bytes()).hexdigest()
        used_to = set()
        used_slices = set()
        for row_index, row in enumerate(document["events"]):
            require(type(row) is dict and set(row) == {"from_ref", "to_ref", "reason", "slices"} and
                    type(row["from_ref"]) is dict and type(row["to_ref"]) is dict,
                    "H_TRANSFER_CLAIM_INVALID", "H 转账须有双方完整 booked 来源、理由和切片")
            key = _key(row["from_ref"])
            location = yaml_path_location(declarations, ("events", row_index))
            if key in future_pairs:
                require(row["to_ref"] == future_pairs[key], "H_TRANSFER_TO_REF_INVALID",
                        "未来 H 转入来源须为同一交易的完整对手", location)
                continue
            require(key in candidates, "H_TRANSFER_REF_INVALID", "H 转出来源不存在或过期", location)
            require(key not in claims, "H_TRANSFER_DOUBLE_CLAIM", "H 转出来源重复认领", key)
            candidate = candidates[key]
            require(candidate["shape"] == "simple_cash", "H_TRANSFER_UNSUPPORTED",
                    "本事件不符合简单同币种 H 转账协议", key)
            to_key = _key(row["to_ref"])
            require(to_key == _key(candidate["to_ref"]) and to_key not in used_to,
                    "H_TRANSFER_TO_REF_INVALID", "H 转入来源不存在、过期或重复使用", to_key)
            used_to.add(to_key)
            require(type(row["reason"]) is str and bool(row["reason"].strip()) and
                    type(row["slices"]) is list and bool(row["slices"]),
                    "H_TRANSFER_CLAIM_INVALID", "H 转账须有非空人工理由和切片", key)
            from_total = Fraction(0)
            to_total = Fraction(0)
            parsed = []
            net_allocations = {}
            for part in row["slices"]:
                require(type(part) is dict and set(part) == {
                    "id", "from_owner_id", "to_owner_id", "signed_from_units",
                    "signed_to_units", "boundary_role"},
                    "H_TRANSFER_SLICE_INVALID", "H 转账切片字段无效", key)
                ident, source, target = (part[field] for field in
                                         ("id", "from_owner_id", "to_owner_id"))
                require(type(ident) is str and bool(ID.fullmatch(ident)) and
                        ident not in used_slices,
                        "H_TRANSFER_SLICE_INVALID", "H 转账切片 ID 非法或重复", key)
                used_slices.add(ident)
                require(type(source) is str and (source == "-" or source in owner_ids) and
                        type(target) is str and (target == "-" or target in owner_ids),
                        "H_TRANSFER_OWNER_INVALID", "H 转账用途未登记", key)
                from_units = _number(part["signed_from_units"], key)
                to_units = _number(part["signed_to_units"], key)
                require(from_units < 0 and to_units == -from_units,
                        "H_TRANSFER_UNITS_MISMATCH", "双方 H 切片须精确反向", key)
                role = part["boundary_role"]
                valid = ((role == "preserve" and source == target) or
                         (role == "allocate" and source == "-" and target != "-") or
                         (role == "release" and source != "-" and target == "-") or
                         (role == "reallocate" and source != target and
                          source in owner_ids and target in owner_ids))
                require(valid, "H_TRANSFER_ROLE_INVALID", "H 转账边界角色与双方用途不一致", key)
                from_total += from_units
                to_total += to_units
                if role == "allocate":
                    net_allocations[target] = net_allocations.get(target, Fraction(0)) + to_units
                elif role == "release":
                    net_allocations[source] = net_allocations.get(source, Fraction(0)) + from_units
                elif role == "reallocate":
                    net_allocations[source] = net_allocations.get(source, Fraction(0)) + from_units
                    net_allocations[target] = net_allocations.get(target, Fraction(0)) + to_units
                parsed.append({"id": ident, "from_owner_id": source,
                               "to_owner_id": target,
                               "signed_from_units": exact_text(from_units),
                               "signed_to_units": exact_text(to_units),
                               "boundary_role": role})
            require(from_total == Fraction(candidate["signed_from_units"]) and
                    to_total == Fraction(candidate["signed_to_units"]),
                    "H_TRANSFER_UNITS_MISMATCH", "切片未精确加回双侧 H posting", key)
            claims[key] = {"reason": row["reason"], "slices": parsed,
                           "net_allocations": {owner: exact_text(value)
                                               for owner, value in sorted(net_allocations.items())}}

    events = []
    for key in order:
        candidate = candidates[key]
        claim = claims.get(key)
        auto_reason = None
        if (candidate["shape"] == "simple_cash" and claim is None and
                "defaults" in owner_document):
            from_claim = h_claims.get(_key(candidate["from_ref"]))
            to_claim = h_claims.get(_key(candidate["to_ref"]))
            if from_claim is not None and to_claim is not None:
                def owner_units(row: dict) -> dict[str, Fraction]:
                    result: dict[str, Fraction] = {}
                    for part in row["slices"]:
                        require(part["role"] == "hold", "H_TRANSFER_OWNER_ROLE_INVALID",
                                "行内自动转账只接受 hold 用途腿", key)
                        owner = part["owner_id"]
                        result[owner] = result.get(owner, Fraction(0)) + Fraction(part["signed_units"])
                    return {owner: amount for owner, amount in result.items() if amount}

                source_units = owner_units(from_claim)
                target_units = owner_units(to_claim)
                require(sum(source_units.values(), Fraction(0)) ==
                        Fraction(candidate["signed_from_units"]) and
                        sum(target_units.values(), Fraction(0)) ==
                        Fraction(candidate["signed_to_units"]),
                        "H_TRANSFER_OWNER_MISMATCH", "自动转账双方用途金额未加回 booked 分录", key)
                source_positive = {owner: -amount for owner, amount in source_units.items()}
                # Two different mixed splits on different accounts need an
                # explicit decision; same-account virtual reallocations are
                # already fully evidenced by their two fund vectors.
                if (candidate["from_account"] != candidate["to_account"] and
                        len(source_units) > 1 and
                        source_positive != target_units):
                    auto_reason = "H_TRANSFER_MIXED_REMAP_PENDING"
                else:
                    net = {owner: source_units.get(owner, Fraction(0)) +
                           target_units.get(owner, Fraction(0))
                           for owner in set(source_units) | set(target_units)
                           if owner != "-"}
                    claim = {"origin": "inferred", "reason": "双 H booked 腿与逐腿用途直接推导",
                             "slices": None,
                             "from_owner_units": {owner: exact_text(amount)
                                                  for owner, amount in sorted(source_units.items())},
                             "to_owner_units": {owner: exact_text(amount)
                                                for owner, amount in sorted(target_units.items())},
                             "net_allocations": {owner: exact_text(amount)
                                                 for owner, amount in sorted(net.items()) if amount}}
        if candidate["shape"] != "simple_cash":
            state, codes = "UNSUPPORTED", ["H_TRANSFER_SHAPE_UNSUPPORTED"]
        elif claim is None:
            state, codes = "NEEDS_REVIEW", [auto_reason or "H_TRANSFER_PENDING"]
        elif claim.get("origin") == "inferred":
            state, codes = "CERTIFIED", []
        else:
            from_claim = h_claims.get(_key(candidate["from_ref"]))
            to_claim = h_claims.get(_key(candidate["to_ref"]))
            if from_claim is None or to_claim is None:
                state, codes = "NEEDS_REVIEW", ["H_TRANSFER_OWNER_PENDING"]
            else:
                expected_from = {}
                expected_to = {}
                for part in claim["slices"]:
                    owner = part["from_owner_id"]
                    expected_from[owner] = expected_from.get(owner, Fraction(0)) + Fraction(part["signed_from_units"])
                    owner = part["to_owner_id"]
                    expected_to[owner] = expected_to.get(owner, Fraction(0)) + Fraction(part["signed_to_units"])
                if candidate["from_account"] != candidate["to_account"]:
                    source_mix = {owner: -units for owner, units in expected_from.items() if units}
                    target_mix = {owner: units for owner, units in expected_to.items() if units}
                    require(len(source_mix) == 1 or source_mix == target_mix,
                            "H_TRANSFER_MIXED_REMAP_INVALID",
                            "跨账户混合用途转账不能顺带改变多项用途；须单独记同账户虚拟划拨", key)
                for h_claim, expected in ((from_claim, expected_from), (to_claim, expected_to)):
                    actual = {}
                    for part in h_claim["slices"]:
                        require(part["role"] == "hold", "H_TRANSFER_OWNER_ROLE_INVALID",
                                "H 转账用途边界由事件独占，H 双侧须为 hold", key)
                        owner = part["owner_id"]
                        actual[owner] = actual.get(owner, Fraction(0)) + Fraction(part["signed_units"])
                    require(actual == expected, "H_TRANSFER_OWNER_MISMATCH",
                            "H 转账切片与真实两侧归属不一致", key)
                state, codes = "CERTIFIED", []
        events.append({**candidate, "status": state, "reason_codes": codes,
                       "origin": claim.get("origin", "external") if claim else None,
                       "review_reason": claim["reason"] if claim else None,
                       "slices": claim["slices"] if claim else None,
                       "from_owner_units": claim.get("from_owner_units") if claim else None,
                       "to_owner_units": claim.get("to_owner_units") if claim else None,
                       "net_allocations": claim["net_allocations"] if state == "CERTIFIED" else None})
    return {"mode": "v6_simple_h_transfer_audit", "cutover": cutover.isoformat(),
            "ledger_snapshot": fin.digest, "review_sha256": review_sha,
            "review_state": "complete" if account_audit["review_state"] == "complete" and
                            all(row["status"] == "CERTIFIED" for row in events) else "pending",
            "certified_events": sum(row["status"] == "CERTIFIED" for row in events),
            "pending_events": sum(row["status"] == "NEEDS_REVIEW" for row in events),
            "unsupported_events": sum(row["status"] == "UNSUPPORTED" for row in events),
            "events": events,
            "note": "仅认证简单同币种 H 转账的来源及用途边界；不生成新资产、不替代 H 库存或首页"}

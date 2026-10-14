"""Source-bound replay of simple booked obligations and clearing advances.

This contract covers a single expense/card purchase and a later single
asset/card payment. A separately reviewed asset-outflow / asset-inflow pair
can settle a third-party advance recorded in a liability account without
creating a personal consumption obligation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial, reference_financial
from ..refs import _lot
from ..yamlstrict import load_yaml
from .classification import _key
from .facts import FactIndex
from .ownership import _owners
from .provenance import valid_digest
from .source import scan_v6_source


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def audit_simple_obligations(root: Path, cutover: date,
                             declarations: Path | None = None,
                             as_of: date | None = None) -> dict:
    """Replay reviewed purchases and payments, keeping all other debt pending.

    A certified event is a source and responsibility match, not a complete
    actual-spendable calculation. Nonzero cutover liability coordinates need
    a separate opening protocol and therefore keep this audit pending.
    """
    scan_v6_source(root, cutover)
    require(as_of is None or (type(as_of) is date and as_of >= cutover),
            "TIME_INVALID", "责任审计日末须不早于 cutover")
    fin = load_financial(root)
    facts = FactIndex(fin)
    liability_root = fin.options["name_liabilities"]
    reference_fin = reference_financial(fin)
    visible = {id(entry) for entry in fin.entries}
    future_postings = [posting
        for entry in reference_fin.entries if isinstance(entry, data.Transaction) and
        (id(entry) not in visible or as_of is not None and entry.date > as_of)
        for posting in entry.postings if posting.account.split(":", 1)[0] == liability_root]
    future_keys = set()
    if future_postings:
        reference_facts = facts if reference_fin is fin else FactIndex(reference_fin)
        future_keys = {_key(reference_facts.for_posting(posting).as_dict()) for posting in future_postings}
    asset_root = fin.options["name_assets"]
    expense_root = fin.options["name_expenses"]
    opening = defaultdict(Fraction)
    candidates = {}
    order = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] != liability_root:
                continue
            coord = (posting.account, posting.units.currency, _lot(posting))
            amount = exact(posting.units.number)
            if entry.date < cutover:
                opening[coord] += amount
                continue
            ref = facts.for_posting(posting).as_dict()
            key = _key(ref)
            require(key not in candidates, "SOURCE_REF_AMBIGUOUS",
                    "负债 booked 来源重复", key)
            counterpart = next((p for p in entry.postings if p is not posting), None) if len(entry.postings) == 2 else None
            shape = None
            if counterpart is not None and amount != 0 and (
                    posting.cost is None and posting.price is None and
                    counterpart.cost is None and counterpart.price is None and
                    counterpart.units.currency == posting.units.currency and
                    exact(counterpart.units.number) == -amount and
                    fin.raw_price(posting)[0] == "none" and
                    fin.raw_price(counterpart)[0] == "none"):
                other_root = counterpart.account.split(":", 1)[0]
                if amount < 0 and other_root == expense_root:
                    shape = "consumer_purchase"
                elif amount > 0 and other_root == asset_root:
                    shape = "asset_outflow"
                elif amount < 0 and other_root == asset_root:
                    shape = "asset_inflow"
            candidate = {"liability_ref": ref, "counterpart_ref":
                         facts.for_posting(counterpart).as_dict() if shape else None,
                         "account": posting.account, "commodity": posting.units.currency,
                         "lot": coord[2], "date": entry.date.isoformat(),
                         "signed_units": exact_text(amount), "shape": shape or "unsupported"}
            candidates[key] = candidate
            order.append(key)

    claims = {}
    review_sha = None
    if declarations is not None:
        document = load_yaml(declarations)
        require(type(document) is dict and set(document) == {
            "schema", "cutover", "ledger_snapshot", "loader_version", "owners", "events"},
            "OBLIGATION_DECLARATION_INVALID", "责任侧文件字段不完整或含未知字段")
        require(type(document["schema"]) is int and document["schema"] == 6 and
                type(document["cutover"]) is str and document["cutover"] == cutover.isoformat() and
                valid_digest(document["ledger_snapshot"]) and
                document["loader_version"] == fin.loader_version and
                type(document["events"]) is list,
                "OBLIGATION_DECLARATION_INVALID", "责任侧文件版本或原审快照格式不匹配")
        owners = set(_owners(document["owners"]).values())
        review_sha = sha256(declarations.read_bytes()).hexdigest()
        for row in document["events"]:
            require(type(row) is dict and set(row) in ({
                "liability_ref", "counterpart_ref", "kind", "obligation_id",
                "owner_id", "reason"}, {
                "liability_ref", "counterpart_ref", "kind", "obligation_id", "reason"}, {
                "liability_ref", "counterpart_ref", "kind", "clearing_id", "reason"}),
                "OBLIGATION_CLAIM_INVALID", "责任事件字段无效")
            require(type(row["liability_ref"]) is dict and type(row["counterpart_ref"]) is dict,
                    "OBLIGATION_CLAIM_INVALID", "责任事件必须绑定两侧完整 booked 来源")
            key = _key(row["liability_ref"])
            if key in future_keys:
                continue  # Existing source after the report date; do not apply its claim.
            require(key in candidates, "OBLIGATION_REF_INVALID", "负债来源不存在或过期", key)
            require(key not in claims, "DUPLICATE_OBLIGATION_CLAIM", "负债腿被重复认领", key)
            candidate = candidates[key]
            require(candidate["shape"] != "unsupported", "OBLIGATION_EVENT_UNSUPPORTED",
                    "本事件不符合简单消费刷卡／付款协议", key)
            require(_key(row["counterpart_ref"]) == _key(candidate["counterpart_ref"]),
                    "OBLIGATION_COUNTERPART_INVALID", "负债对手来源错误或过期", key)
            kind = row["kind"]
            allowed = {"consumer_purchase": {"consumer_purchase"},
                       "asset_outflow": {"payment", "advance_outflow"},
                       "asset_inflow": {"advance_reimbursement"}}
            require(type(kind) is str and kind in allowed[candidate["shape"]] and
                    type(row["reason"]) is str and bool(row["reason"].strip()),
                    "OBLIGATION_CLAIM_INVALID", "责任 ID、类型或理由无效", key)
            ident = row.get("clearing_id") if kind.startswith("advance_") else row.get("obligation_id")
            require(type(ident) is str and bool(ID.fullmatch(ident)),
                    "OBLIGATION_CLAIM_INVALID", "责任或清算 ID 非法", key)
            if kind == "consumer_purchase":
                require("obligation_id" in row and "clearing_id" not in row,
                        "OBLIGATION_CLAIM_INVALID", "消费须使用责任 ID", key)
                require("owner_id" in row and type(row["owner_id"]) is str and
                        row["owner_id"] in owners,
                        "OBLIGATION_OWNER_INVALID", "消费责任须声明已登记用途", key)
            else:
                require("owner_id" not in row, "OBLIGATION_CLAIM_INVALID",
                        "还款沿用原责任用途，不得重写", key)
                require((kind == "payment" and "obligation_id" in row and "clearing_id" not in row) or
                        (kind.startswith("advance_") and "clearing_id" in row and "obligation_id" not in row),
                        "OBLIGATION_CLAIM_INVALID", "责任与代垫清算须使用各自 ID", key)
            claims[key] = row

    balances = {}
    advances = {}
    events = []
    for key in order:
        candidate = candidates[key]
        if as_of is not None and date.fromisoformat(candidate["date"]) > as_of:
            continue
        claim = claims.get(key)
        state, codes = "NEEDS_REVIEW", ["OBLIGATION_UNCLAIMED"]
        if candidate["shape"] == "unsupported":
            state, codes = "UNSUPPORTED", ["OBLIGATION_EVENT_UNSUPPORTED"]
        elif claim is not None:
            kind = claim["kind"]
            ident = claim.get("clearing_id") if kind.startswith("advance_") else claim["obligation_id"]
            amount = Fraction(candidate["signed_units"])
            coord = (candidate["account"], candidate["commodity"], candidate["lot"])
            if kind == "consumer_purchase":
                require(ident not in balances, "DUPLICATE_OBLIGATION_CLAIM",
                        "稳定责任 ID 重复用于新消费", key)
                balances[ident] = {"account": coord, "owner_id": claim["owner_id"],
                                   "remaining": -amount, "purchase_ref": candidate["liability_ref"]}
            elif kind == "payment":
                require(ident in balances, "OBLIGATION_SETTLEMENT_UNKNOWN",
                        "还款未指向本审计中较早发生的消费责任", key)
                obligation = balances[ident]
                require(obligation["account"] == coord and
                        amount <= obligation["remaining"],
                        "OBLIGATION_SETTLEMENT_EXCESS",
                        "还款币种／账户不符或超过所指责任未偿额", key)
                obligation["remaining"] -= amount
            elif kind == "advance_outflow":
                require(ident not in advances, "CLEARING_DOUBLE_CLAIM",
                        "代垫清算 ID 重复", key)
                advances[ident] = {"account": coord, "remaining": amount,
                                   "outflow_ref": candidate["liability_ref"]}
            else:
                require(ident in advances, "CLEARING_SOURCE_UNKNOWN",
                        "报销未指向较早发生的代垫", key)
                advance = advances[ident]
                require(advance["account"] == coord and
                        -amount <= advance["remaining"],
                        "CLEARING_REIMBURSEMENT_EXCESS",
                        "报销币种／账户不符或超过代垫未结额", key)
                advance["remaining"] += amount
            state, codes = "CERTIFIED", []
        events.append({**candidate, "status": state, "reason_codes": codes,
                       "kind": claim["kind"] if claim else None,
                       "obligation_id": claim.get("obligation_id") if claim else None,
                       "clearing_id": claim.get("clearing_id") if claim else None,
                       "owner_id": (claim.get("owner_id") if claim else None),
                       "review_reason": claim["reason"] if claim else None})

    opening_pending = [{"account": account, "commodity": commodity, "lot": lot,
                        "signed_units": exact_text(value)}
                       for (account, commodity, lot), value in sorted(opening.items()) if value]
    remaining = [{"obligation_id": ident, "account": item["account"][0],
                  "commodity": item["account"][1], "lot": item["account"][2],
                  "owner_id": item["owner_id"],
                  "remaining_native": exact_text(item["remaining"]),
                  "purchase_ref": item["purchase_ref"]}
                 for ident, item in sorted(balances.items())]
    pending_advances = [{"clearing_id": ident, "account": item["account"][0],
                         "commodity": item["account"][1], "lot": item["account"][2],
                         "remaining_native": exact_text(item["remaining"]),
                         "outflow_ref": item["outflow_ref"]}
                        for ident, item in sorted(advances.items())]
    return {"mode": "v6_simple_booked_obligation_audit", "cutover": cutover.isoformat(),
            "as_of": as_of.isoformat() if as_of is not None else None,
            "ledger_snapshot": fin.digest, "review_sha256": review_sha,
            "review_state": "complete" if declarations is not None and not opening_pending and all(
                row["status"] == "CERTIFIED" for row in events) else "pending",
            "opening_pending": opening_pending,
            "certified_events": sum(row["status"] == "CERTIFIED" for row in events),
            "pending_events": sum(row["status"] == "NEEDS_REVIEW" for row in events),
            "unsupported_events": sum(row["status"] == "UNSUPPORTED" for row in events),
            "events": events, "obligations": remaining,
            "clearing_advances": pending_advances,
            "note": "仅认证简单已 booked 消费责任／还款和另经批准的代垫清算；未处理账单、期初、到期、覆盖、外币清偿价或首页"}

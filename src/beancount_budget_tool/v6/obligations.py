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
from ..yamlstrict import load_yaml, yaml_path_location, require_yaml
from .classification import _key
from .facts import FactIndex, reference_fact_index
from .obligation_opening import compile_obligation_opening, opening_source, paired_price_value
from .ownership import _owners
from .provenance import valid_digest
from .source import scan_v6_source


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def audit_simple_obligations(root: Path, cutover: date,
                             declarations: Path | None = None,
                             as_of: date | None = None) -> dict:
    """Replay reviewed purchases and payments, keeping all other debt pending.

    A certified event is a source and responsibility match, not a complete
    actual-spendable calculation. Reviewed opening residuals seed stable IDs;
    unmatched cutover liability coordinates remain pending.
    """
    scan_v6_source(root, cutover)
    require(as_of is None or (type(as_of) is date and as_of >= cutover),
            "TIME_INVALID", "责任审计日末须不早于 cutover")
    fin = load_financial(root)
    facts = FactIndex(fin)
    liability_root = fin.options["name_liabilities"]
    reference_fin = reference_financial(fin)
    visible = {id(entry) for entry in fin.entries}
    future_entries = [entry
        for entry in reference_fin.entries if isinstance(entry, data.Transaction) and
        (id(entry) not in visible or as_of is not None and entry.date > as_of)]
    future_pairs = {}
    if any(p.account.split(":", 1)[0] == liability_root for e in future_entries for p in e.postings):
        reference_facts = reference_fact_index(fin)
        for entry in future_entries:
            refs = [_key(reference_facts.for_posting(p).as_dict()) for p in entry.postings]
            for index, posting in enumerate(entry.postings):
                if posting.account.split(":", 1)[0] == liability_root:
                    future_pairs[refs[index]] = set(refs[:index] + refs[index + 1:])
    asset_root = fin.options["name_assets"]
    expense_root = fin.options["name_expenses"]
    opening = defaultdict(Fraction)
    historical_sources = {}
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
                origin = opening_source(entry, posting, fin, facts)
                historical_sources[_key(origin["liability_ref"])] = origin
                continue
            ref = facts.for_posting(posting).as_dict()
            key = _key(ref)
            require(key not in candidates, "SOURCE_REF_AMBIGUOUS",
                    "负债 booked 来源重复", key)
            counterpart = next((p for p in entry.postings if p is not posting), None) if len(entry.postings) == 2 else None
            shape = None
            if counterpart is not None and amount != 0 and (
                    posting.cost is None and counterpart.cost is None and
                    counterpart.units.currency == posting.units.currency and
                    exact(counterpart.units.number) == -amount):
                other_root = counterpart.account.split(":", 1)[0]
                if amount < 0 and other_root == expense_root:
                    meta = posting.meta or entry.meta
                    require(paired_price_value(fin, posting, counterpart), "OBLIGATION_PRICE_MISMATCH",
                            "消费与负债两腿的原文价格权重须精确相等；不能用 loader 容差掩盖计价残差",
                            f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}")
                    shape = "consumer_purchase"
                elif (posting.price is None and counterpart.price is None and
                      fin.raw_price(posting)[0] == fin.raw_price(counterpart)[0] == "none" and
                      other_root == asset_root):
                    shape = "asset_outflow" if amount > 0 else "asset_inflow"
            candidate = {"liability_ref": ref, "counterpart_ref":
                         facts.for_posting(counterpart).as_dict() if shape else None,
                         "account": posting.account, "commodity": posting.units.currency,
                         "lot": coord[2], "date": entry.date.isoformat(),
                         "signed_units": exact_text(amount), "shape": shape or "unsupported"}
            candidates[key] = candidate
            order.append(key)

    claims, claim_locations = {}, {}

    def check_event(condition, code, message, location):
        # Resolve YAML positions only on failure: a shared event file may contain
        # years of later settlements, and location lookup parses the source.
        if not condition:
            require(False, code, message, yaml_path_location(declarations, location))

    review_sha = None
    opening_rows, owners = [], set()
    if declarations is not None:
        document = load_yaml(declarations)
        required = {"schema", "cutover", "ledger_snapshot", "loader_version", "owners", "events"}
        unknown = set(document) - required - {"opening"} if type(document) is dict else set()
        require_yaml(type(document) is dict and required <= set(document) and not unknown,
            "OBLIGATION_DECLARATION_INVALID", "责任侧文件字段不完整或含未知字段", declarations,
            (sorted(unknown, key=str)[0],) if unknown else ())
        for field, valid in (
            ("schema", type(document["schema"]) is int and document["schema"] == 6),
            ("cutover", type(document["cutover"]) is str and document["cutover"] == cutover.isoformat()),
            ("ledger_snapshot", valid_digest(document["ledger_snapshot"])),
            ("loader_version", document["loader_version"] == fin.loader_version),
            ("events", type(document["events"]) is list),
        ):
            require_yaml(valid, "OBLIGATION_DECLARATION_INVALID", "责任侧文件版本或原审快照格式不匹配",
                         declarations, (field,))
        owners = set(_owners(document["owners"], source=declarations).values())
        opening_rows = document.get("opening", [])
        review_sha = sha256(declarations.read_bytes()).hexdigest()
        for row_index, row in enumerate(document["events"]):
            location = ("events", row_index)
            check_event(type(row) is dict and set(row) in ({
                "liability_ref", "counterpart_ref", "kind", "obligation_id",
                "owner_id", "reason"}, {
                "liability_ref", "counterpart_ref", "kind", "obligation_id", "reason"}, {
                "liability_ref", "counterpart_ref", "kind", "clearing_id", "reason"}),
                "OBLIGATION_CLAIM_INVALID", "责任事件字段无效", location)
            check_event(type(row["liability_ref"]) is dict and type(row["counterpart_ref"]) is dict,
                    "OBLIGATION_CLAIM_INVALID", "责任事件必须绑定两侧完整 booked 来源", location)
            key = _key(row["liability_ref"])
            if key in future_pairs:
                check_event(_key(row["counterpart_ref"]) in future_pairs[key],
                        "OBLIGATION_COUNTERPART_INVALID", "未来负债对手须为同一交易的完整来源",
                        (*location, "counterpart_ref"))
                continue  # Existing source after the report date; do not apply its claim.
            check_event(key in candidates, "OBLIGATION_REF_INVALID", "负债来源不存在或过期",
                        (*location, "liability_ref"))
            check_event(key not in claims, "DUPLICATE_OBLIGATION_CLAIM", "负债腿被重复认领", location)
            candidate = candidates[key]
            check_event(candidate["shape"] != "unsupported", "OBLIGATION_EVENT_UNSUPPORTED",
                    "本事件不符合简单消费刷卡／付款协议", location)
            check_event(_key(row["counterpart_ref"]) == _key(candidate["counterpart_ref"]),
                    "OBLIGATION_COUNTERPART_INVALID", "负债对手来源错误或过期",
                    (*location, "counterpart_ref"))
            kind = row["kind"]
            allowed = {"consumer_purchase": {"consumer_purchase"},
                       "asset_outflow": {"payment", "advance_outflow"},
                       "asset_inflow": {"advance_reimbursement"}}
            check_event(type(kind) is str and kind in allowed[candidate["shape"]] and
                    type(row["reason"]) is str and bool(row["reason"].strip()),
                    "OBLIGATION_CLAIM_INVALID", "责任 ID、类型或理由无效", location)
            ident = row.get("clearing_id") if kind.startswith("advance_") else row.get("obligation_id")
            check_event(type(ident) is str and bool(ID.fullmatch(ident)),
                    "OBLIGATION_CLAIM_INVALID", "责任或清算 ID 非法", location)
            if kind == "consumer_purchase":
                check_event("obligation_id" in row and "clearing_id" not in row,
                        "OBLIGATION_CLAIM_INVALID", "消费须使用责任 ID", location)
                check_event("owner_id" in row and type(row["owner_id"]) is str and
                        row["owner_id"] in owners,
                        "OBLIGATION_OWNER_INVALID", "消费责任须声明已登记用途", location)
            else:
                check_event("owner_id" not in row, "OBLIGATION_CLAIM_INVALID",
                        "还款沿用原责任用途，不得重写", location)
                check_event((kind == "payment" and "obligation_id" in row and "clearing_id" not in row) or
                        (kind.startswith("advance_") and "clearing_id" in row and "obligation_id" not in row),
                        "OBLIGATION_CLAIM_INVALID", "责任与代垫清算须使用各自 ID", location)
            claims[key] = row
            claim_locations[key] = location

    opening_audit = compile_obligation_opening(opening, historical_sources, opening_rows, owners, declarations)
    balances = opening_audit["debts"]
    advances = opening_audit["advances"]
    opening_pending = opening_audit["pending"]
    pending_coordinates = {(row["account"], row["commodity"], row["lot"]) for row in opening_pending}
    events = []
    for key in order:
        candidate = candidates[key]
        if as_of is not None and date.fromisoformat(candidate["date"]) > as_of:
            continue
        claim = claims.get(key)
        kind = claim["kind"] if claim else None
        ident = (claim.get("clearing_id") if kind.startswith("advance_") else claim["obligation_id"]) if claim else None
        coord = (candidate["account"], candidate["commodity"], candidate["lot"])
        opening_required = coord in pending_coordinates and (
            kind == "payment" and ident not in balances or
            kind == "advance_reimbursement" and ident not in advances)
        state, codes = "NEEDS_REVIEW", ["OBLIGATION_UNCLAIMED"]
        if candidate["shape"] == "unsupported":
            state, codes = "UNSUPPORTED", ["OBLIGATION_EVENT_UNSUPPORTED"]
        elif opening_required:
            state, codes = "NEEDS_REVIEW", ["OBLIGATION_OPENING_REQUIRED"]
        elif claim is not None:
            location = claim_locations[key]
            amount = Fraction(candidate["signed_units"])
            if kind == "consumer_purchase":
                check_event(ident not in balances, "DUPLICATE_OBLIGATION_CLAIM",
                        "稳定责任 ID 重复用于新消费", location)
                balances[ident] = {"account": coord, "owner_id": claim["owner_id"],
                                   "remaining": -amount, "purchase_ref": candidate["liability_ref"],
                                   "purchase_date": candidate["date"]}
            elif kind == "payment":
                check_event(ident in balances, "OBLIGATION_SETTLEMENT_UNKNOWN",
                        "还款未指向本审计中较早发生的消费责任", location)
                obligation = balances[ident]
                check_event(obligation["account"] == coord and
                        amount <= obligation["remaining"],
                        "OBLIGATION_SETTLEMENT_EXCESS",
                        "还款币种／账户不符或超过所指责任未偿额", location)
                obligation["remaining"] -= amount
            elif kind == "advance_outflow":
                check_event(ident not in advances, "CLEARING_DOUBLE_CLAIM",
                        "代垫清算 ID 重复", location)
                advances[ident] = {"account": coord, "remaining": amount,
                                   "outflow_ref": candidate["liability_ref"]}
            else:
                check_event(ident in advances, "CLEARING_SOURCE_UNKNOWN",
                        "报销未指向较早发生的代垫", location)
                advance = advances[ident]
                check_event(advance["account"] == coord and
                        -amount <= advance["remaining"],
                        "CLEARING_REIMBURSEMENT_EXCESS",
                        "报销币种／账户不符或超过代垫未结额", location)
                advance["remaining"] += amount
            state, codes = "CERTIFIED", []
        events.append({**candidate, "status": state, "reason_codes": codes,
                       "kind": claim["kind"] if claim else None,
                       "obligation_id": claim.get("obligation_id") if claim else None,
                       "clearing_id": claim.get("clearing_id") if claim else None,
                       "owner_id": (claim.get("owner_id") if claim else None),
                       "review_reason": claim["reason"] if claim else None})

    remaining = [{"obligation_id": ident, "account": item["account"][0],
                  "commodity": item["account"][1], "lot": item["account"][2],
                  "owner_id": item["owner_id"],
                  "origin": item.get("origin", "current_period"),
                  "purchase_date": item.get("purchase_date"),
                  "remaining_native": exact_text(item["remaining"]),
                  "purchase_ref": item["purchase_ref"]}
                 for ident, item in sorted(balances.items())]
    pending_advances = [{"clearing_id": ident, "account": item["account"][0],
                         "commodity": item["account"][1], "lot": item["account"][2],
                         "remaining_native": exact_text(item["remaining"]),
                         "origin": item.get("origin", "current_period"),
                         "outflow_ref": item["outflow_ref"]}
                        for ident, item in sorted(advances.items())]
    return {"mode": "v6_simple_booked_obligation_audit", "cutover": cutover.isoformat(),
            "as_of": as_of.isoformat() if as_of is not None else None,
            "ledger_snapshot": fin.digest, "review_sha256": review_sha,
            "review_state": "complete" if declarations is not None and not opening_pending and all(
                row["status"] == "CERTIFIED" for row in events) else "pending",
            "opening_pending": opening_pending,
            "opening_declarations": opening_audit["declarations"],
            "certified_events": sum(row["status"] == "CERTIFIED" for row in events),
            "pending_events": sum(row["status"] == "NEEDS_REVIEW" for row in events),
            "unsupported_events": sum(row["status"] == "UNSUPPORTED" for row in events),
            "events": events, "obligations": remaining,
            "clearing_advances": pending_advances,
            "note": "认证简单已 booked 消费责任／还款、代垫清算及已审期初未结 ID；不重算 cutover 前费用；账单、到期、外币清偿及完整首页仍另审"}

"""Source-bound replay of simple booked obligations and clearing advances.

This contract covers a card purchase (including native expenses jointly paid
by cash and one or several cards), explicit payable receipts and native
single-cash payments settling one or several liability legs.
A separately reviewed asset-outflow / asset-inflow pair
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
from ..financial import load_financial, reference_financial, shared_audit
from ..refs import _lot
from ..yamlstrict import load_yaml, yaml_path_location, require_yaml
from .classification import _key
from .facts import FactIndex, reference_fact_index
from .obligation_opening import compile_obligation_opening, opening_source, paired_price_value
from .ownership import _owners
from .native_flows import native_card_purchase, native_payable_receipt, native_group_payment
from .inline_amounts import parse_inline_amounts
from .provenance import valid_digest
from .source import _source, scan_v6_source


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


@shared_audit
def audit_simple_obligations(root: Path, cutover: date,
                             declarations: Path | None = None,
                             as_of: date | None = None, *, before_ref: str | None = None) -> dict:
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
    future_pairs = None
    future_info = {}
    asset_root = fin.options["name_assets"]
    expense_root = fin.options["name_expenses"]
    opening = defaultdict(Fraction)
    historical_sources = {}
    candidates = {}
    candidate_transactions = {}
    payable_receipts = {}
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
            group = native_card_purchase(fin, entry) if len(entry.postings) > 2 else None
            if group is not None:
                shape = "consumer_purchase"
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
            receipt = native_payable_receipt(fin, entry)
            if receipt is not None and receipt.liability is posting:
                shape = "income_payable"
                payable_receipts[key] = receipt
            candidate = {"liability_ref": ref, "counterpart_ref":
                         facts.for_posting(counterpart).as_dict() if shape and counterpart is not None else None,
                         "account": posting.account, "commodity": posting.units.currency,
                         "lot": coord[2], "date": entry.date.isoformat(),
                         "signed_units": exact_text(amount), "shape": shape or "unsupported"}
            if group is not None:
                candidate.update(
                    counterpart_refs=[facts.for_posting(p).as_dict() for p in entry.postings if p is not posting],
                    expense_refs=[facts.for_posting(p).as_dict() for p in group.expenses],
                    asset_refs=[facts.for_posting(p).as_dict() for p in group.assets])
                if len(group.liabilities) > 1:
                    candidate.update(
                        proof="native_joint_card_purchase_v1",
                        purchase_group_ref=facts.for_transaction(entry).as_dict(),
                        liability_refs=[facts.for_posting(p).as_dict() for p in group.liabilities],
                        expense_total=exact_text(sum((exact(p.units.number) for p in group.expenses), Fraction(0))),
                        cash_paid=exact_text(-sum((exact(p.units.number) for p in group.assets), Fraction(0))),
                        debt_total=exact_text(-sum((exact(p.units.number) for p in group.liabilities), Fraction(0))))
            if receipt is not None and receipt.liability is posting:
                candidate.update(counterpart_refs=[facts.for_posting(p).as_dict()
                                                   for p in entry.postings if p is not posting],
                                 asset_refs=[facts.for_posting(receipt.cash).as_dict()],
                                 income_refs=[facts.for_posting(p).as_dict() for p in receipt.incomes],
                                 cash_received=exact_text(receipt.received),
                                 income_received=exact_text(receipt.income),
                                 payable_received=exact_text(receipt.payable),
                                 proof="native_income_payable_v1")
            payment = native_group_payment(fin, entry)
            if payment is not None:
                candidate.update(shape="asset_outflow", proof="native_group_payment_v1",
                    counterpart_ref=facts.for_posting(payment.assets[0]).as_dict(),
                    counterpart_refs=[facts.for_posting(p).as_dict() for p in entry.postings if p is not posting],
                    asset_refs=[facts.for_posting(payment.assets[0]).as_dict()],
                    liability_refs=[facts.for_posting(p).as_dict() for p in payment.counterparts],
                    payment_group_ref=facts.for_transaction(entry).as_dict(), cash_paid=exact_text(-payment.asset_total))
            candidates[key] = candidate
            if before_ref is not None:
                candidate_transactions[key] = id(entry)
            order.append(key)

    claims, claim_locations = {}, {}

    def check_event(condition, code, message, location):
        # Resolve YAML positions only on failure: a shared event file may contain
        # years of later settlements, and location lookup parses the source.
        if not condition:
            require(False, code, message, yaml_path_location(declarations, location))

    review_sha = None
    opening_rows, owners = [], set()
    owner_names = {}
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
        owner_names = _owners(document["owners"], source=declarations)
        owners = set(owner_names.values())
        opening_rows = document.get("opening", [])
        review_sha = sha256(declarations.read_bytes()).hexdigest()
        for row_index, row in enumerate(document["events"]):
            location = ("events", row_index)
            check_event(type(row) is dict and (set(row) - ({"same_day_source_ref"} if row.get("kind") == "payment" else set())) in ({
                "liability_ref", "counterpart_ref", "kind", "obligation_id",
                "owner_id", "reason"}, {
                "liability_ref", "counterpart_ref", "kind", "obligation_id", "reason"}, {
                "liability_ref", "counterpart_ref", "kind", "clearing_id", "reason"}, {
                "liability_ref", "counterpart_refs", "kind", "obligation_id", "owner_id", "reason"}, {
                "liability_ref", "counterpart_refs", "kind", "obligation_id", "reason"}),
                "OBLIGATION_CLAIM_INVALID", "责任事件字段无效", location)
            grouped = "counterpart_refs" in row
            ref_field = "counterpart_refs" if grouped else "counterpart_ref"
            refs = row[ref_field] if grouped else [row[ref_field]]
            check_event(type(row["liability_ref"]) is dict and type(refs) is list and bool(refs) and
                        all(type(ref) is dict for ref in refs),
                    "OBLIGATION_CLAIM_INVALID", "责任事件必须绑定全部对手的完整 booked 来源", location)
            if "same_day_source_ref" in row:
                check_event(type(row.get("obligation_id")) is str and bool(ID.fullmatch(row["obligation_id"])) and
                            "owner_id" not in row and "clearing_id" not in row,
                            "SAME_DAY_CLEARING_INVALID", "同日清算只可绑定既有责任 ID，不能重写用途或冒充代垫", location)
            try:
                ref_keys = [_key(ref) for ref in refs]
                key = _key(row["liability_ref"])
            except (TypeError, ValueError):
                check_event(False, "OBLIGATION_CLAIM_INVALID", "责任来源须为可序列化的完整引用", location)
            check_event(len(set(ref_keys)) == len(ref_keys), "OBLIGATION_COUNTERPART_INVALID",
                        "负债对手来源不得重复", (*location, ref_field))
            if key not in candidates or as_of is not None and candidates[key]["date"] > as_of.isoformat():
                if future_pairs is None:
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
                                    future_info[refs[index]] = (entry.date, posting.account,
                                                               posting.units.currency, _lot(posting))
                if key in future_pairs:
                    check_event(set(ref_keys) == future_pairs[key] if grouped else ref_keys[0] in future_pairs[key],
                            "OBLIGATION_COUNTERPART_INVALID", "未来负债对手须为同一交易的完整来源组",
                            (*location, ref_field))
                    if "same_day_source_ref" in row:
                        source = row["same_day_source_ref"]
                        check_event(type(source) is dict and all(type(value) in (str, int) for value in source.values()),
                                    "SAME_DAY_CLEARING_INVALID", "同日清算须绑定完整来源", (*location, "same_day_source_ref"))
                        source_key = _key(source)
                        check_event(source_key in future_info and future_info[source_key] == future_info[key] and
                                    any(type(other) is dict and other.get("liability_ref") == source and
                                        other.get("kind") == "consumer_purchase" and
                                        other.get("obligation_id") == row["obligation_id"] for other in document["events"]),
                                    "SAME_DAY_CLEARING_INVALID", "未来清算须绑定同日、同坐标及相同 ID 的已审消费来源",
                                    (*location, "same_day_source_ref"))
                    continue  # Existing source after the report date; do not apply its claim.
            check_event(key in candidates, "OBLIGATION_REF_INVALID", "负债来源不存在或过期",
                        (*location, "liability_ref"))
            check_event(key not in claims, "DUPLICATE_OBLIGATION_CLAIM", "负债腿被重复认领", location)
            candidate = candidates[key]
            check_event(candidate["shape"] != "unsupported", "OBLIGATION_EVENT_UNSUPPORTED",
                    "本事件不符合简单消费刷卡／付款协议", location)
            expected = candidate.get("counterpart_refs", [candidate["counterpart_ref"]])
            check_event(grouped == ("counterpart_refs" in candidate) and
                        set(ref_keys) == {_key(ref) for ref in expected},
                    "OBLIGATION_COUNTERPART_INVALID", "负债对手来源错误或过期",
                    (*location, ref_field))
            kind = row["kind"]
            allowed = {"consumer_purchase": {"consumer_purchase"},
                       "asset_outflow": {"payment", "advance_outflow"},
                       "asset_inflow": {"advance_reimbursement", "payable_receipt"},
                       "income_payable": {"payable_receipt"}}
            if candidate.get("proof") == "native_group_payment_v1":
                allowed["asset_outflow"] = {"payment"}
            check_event(type(kind) is str and kind in allowed[candidate["shape"]] and
                    type(row["reason"]) is str and bool(row["reason"].strip()),
                    "OBLIGATION_CLAIM_INVALID", "责任 ID、类型或理由无效", location)
            ident = row.get("clearing_id") if kind.startswith("advance_") else row.get("obligation_id")
            check_event(type(ident) is str and bool(ID.fullmatch(ident)),
                    "OBLIGATION_CLAIM_INVALID", "责任或清算 ID 非法", location)
            if kind in ("consumer_purchase", "payable_receipt"):
                check_event("obligation_id" in row and "clearing_id" not in row,
                        "OBLIGATION_CLAIM_INVALID", "新增责任须使用责任 ID", location)
                check_event("owner_id" in row and type(row["owner_id"]) is str and
                        row["owner_id"] in owners,
                        "OBLIGATION_OWNER_INVALID", "新增责任须声明已登记用途", location)
            else:
                check_event("owner_id" not in row, "OBLIGATION_CLAIM_INVALID",
                        "还款沿用原责任用途，不得重写", location)
                check_event((kind == "payment" and "obligation_id" in row and "clearing_id" not in row) or
                        (kind.startswith("advance_") and "clearing_id" in row and "obligation_id" not in row),
                        "OBLIGATION_CLAIM_INVALID", "责任与代垫清算须使用各自 ID", location)
            claims[key] = row
            claim_locations[key] = location

    # A user may identify two same-date sources as one clearing without
    # asserting which happened first. Bind the approved purchase explicitly;
    # never discover it by amount or use a later date to clear today's cash.
    same_day_targets = {}
    for key, claim in claims.items():
        if "same_day_source_ref" not in claim:
            continue
        location = (*claim_locations[key], "same_day_source_ref")
        ref = claim["same_day_source_ref"]
        check_event(type(ref) is dict and all(type(value) in (str, int) for value in ref.values()),
                    "SAME_DAY_CLEARING_INVALID", "同日清算须绑定完整的消费责任来源", location)
        source_key = _key(ref)
        target = candidates.get(source_key)
        approved = claims.get(source_key)
        payment = candidates[key]
        check_event(target is not None and target["liability_ref"] == ref and
                    target["shape"] == "consumer_purchase" and target["date"] == payment["date"] and
                    all(target[field] == payment[field] for field in ("account", "commodity", "lot")) and
                    approved is not None and approved["kind"] == "consumer_purchase" and
                    approved["obligation_id"] == claim["obligation_id"],
                    "SAME_DAY_CLEARING_INVALID", "清算对象须为同日、同账户／币种／批次的已审消费责任，且 ID 精确一致", location)
        same_day_targets[key] = target

    opening_audit = compile_obligation_opening(opening, historical_sources, opening_rows, owners, declarations)
    require(before_ref is None or type(before_ref) is str and before_ref in candidates and
            (as_of is None or candidates[before_ref]["date"] <= as_of.isoformat()),
            "OBLIGATION_REF_INVALID", "责任重放截止来源不存在或不在当前范围", str(root))
    balances = opening_audit["debts"]
    advances = opening_audit["advances"]
    opening_pending = opening_audit["pending"]
    pending_coordinates = {(row["account"], row["commodity"], row["lot"]) for row in opening_pending}
    prepaid = defaultdict(Fraction)
    events = []
    for key in order:
        if before_ref is not None and candidate_transactions[key] == candidate_transactions[before_ref]:
            break
        candidate = candidates[key]
        if as_of is not None and date.fromisoformat(candidate["date"]) > as_of:
            continue
        claim = claims.get(key)
        kind = claim["kind"] if claim else None
        ident = (claim.get("clearing_id") if kind.startswith("advance_") else claim["obligation_id"]) if claim else None
        coord = (candidate["account"], candidate["commodity"], candidate["lot"])
        derived = False
        if claim is None and candidate["shape"] == "income_payable":
            receipt = payable_receipts[key]
            raw = (receipt.cash.meta or {}).get("fund")
            if raw is not None:
                parts = parse_inline_amounts(raw, receipt.received,
                                             _source(receipt.cash.meta or {}),
                                             known_names=set(owner_names), code="OWNERSHIP_UNITS_MISMATCH")
                if len(parts) == 1 and parts[0][0] in owner_names:
                    ident = "payable_" + sha256(key.encode()).hexdigest()
                    kind, derived = "payable_receipt", True
                    claim = {"kind": kind, "obligation_id": ident, "owner_id": owner_names[parts[0][0]],
                             "reason": "同笔原币精确等式：到账＝已记收入＋应付款；责任沿唯一显式资产用途"}
        if (claim is None and candidate["shape"] == "asset_outflow" and coord not in pending_coordinates and
                candidate.get("proof") != "native_group_payment_v1"):
            active = [(name, item) for name, item in balances.items()
                      if item["account"] == coord and item["remaining"] > 0]
            if (len(active) == 1 and active[0][1].get("origin") == "payable_receipt" and
                    Fraction(candidate["signed_units"]) <= active[0][1]["remaining"] and
                    not any(item["account"] == coord and item["remaining"] for item in advances.values())):
                ident, kind, derived = active[0][0], "payment", True
                claim = {"kind": kind, "obligation_id": ident,
                         "reason": "支付同账户同币种唯一未结的已记应付款；金额不超过剩余责任"}
        opening_required = coord in pending_coordinates and (
            kind == "payment" and ident not in balances and key not in same_day_targets or
            kind == "advance_reimbursement" and ident not in advances)
        state, codes = "NEEDS_REVIEW", ["OBLIGATION_UNCLAIMED"]
        if candidate["shape"] == "unsupported":
            state, codes = "UNSUPPORTED", ["OBLIGATION_EVENT_UNSUPPORTED"]
        elif opening_required:
            state, codes = "NEEDS_REVIEW", ["OBLIGATION_OPENING_REQUIRED"]
        elif claim is not None:
            location = claim_locations.get(key)
            amount = Fraction(candidate["signed_units"])
            if kind in ("consumer_purchase", "payable_receipt"):
                check_event(ident not in balances, "DUPLICATE_OBLIGATION_CLAIM",
                        "稳定责任 ID 重复用于新消费", location)
                balances[ident] = {"account": coord, "owner_id": claim["owner_id"],
                                   "remaining": -amount - prepaid.pop(ident, Fraction(0)), "purchase_ref": candidate["liability_ref"],
                                   "purchase_date": candidate["date"],
                                   **({"origin": "payable_receipt"} if kind == "payable_receipt" else {})}
            elif kind == "payment":
                if ident not in balances and key in same_day_targets:
                    prepaid[ident] += amount
                    check_event(prepaid[ident] <= -Fraction(same_day_targets[key]["signed_units"]),
                                "OBLIGATION_SETTLEMENT_EXCESS", "同日预付合计超过明确关联的消费责任金额", location)
                else:
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
        if kind == "payable_receipt" and candidate["shape"] == "asset_inflow":
            candidate = {**candidate, "asset_refs": [candidate["counterpart_ref"]],
                         "proof": "native_payable_inflow_v1", "cash_received": exact_text(-amount),
                         "income_received": "0", "payable_received": exact_text(-amount)}
        events.append({**candidate, "status": state, "reason_codes": codes,
                       "kind": claim["kind"] if claim else None,
                       "obligation_id": claim.get("obligation_id") if claim else None,
                       "clearing_id": claim.get("clearing_id") if claim else None,
                       "owner_id": (claim.get("owner_id") if claim else None),
                       "review_reason": claim["reason"] if claim else None,
                       **({"clearing_protocol": "same_day_obligation_clearing_v1",
                           "same_day_source_ref": claim["same_day_source_ref"],
                           "intraday_order": "not_asserted"} if key in same_day_targets else {}),
                       **({"claim_origin": "booked_equation"} if derived else {})})

    require(before_ref is not None or not prepaid, "SAME_DAY_CLEARING_INCOMPLETE",
            "同日预付未由明确来源全部承接，不能签发日末责任", str(declarations or root))
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
            **({"before_liability_ref": candidates[before_ref]["liability_ref"],
                **({"pending_same_day_prepayments": {ident: exact_text(value) for ident, value in prepaid.items()}}
                   if prepaid else {})} if before_ref is not None else {}),
            "as_of": as_of.isoformat() if as_of is not None else None,
            "ledger_snapshot": fin.digest, "review_sha256": review_sha,
            "review_state": "complete" if declarations is not None and not opening_pending and not prepaid and all(
                row["status"] == "CERTIFIED" for row in events) else "pending",
            "opening_pending": opening_pending,
            "opening_declarations": opening_audit["declarations"],
            "certified_events": sum(row["status"] == "CERTIFIED" for row in events),
            "pending_events": sum(row["status"] == "NEEDS_REVIEW" for row in events),
            "unsupported_events": sum(row["status"] == "UNSUPPORTED" for row in events),
            "events": events, "obligations": remaining,
            "clearing_advances": pending_advances,
            "note": "认证简单已 booked 消费责任／还款、代垫清算及已审期初未结 ID；不重算 cutover 前费用；账单、到期、外币清偿及完整首页仍另审"}

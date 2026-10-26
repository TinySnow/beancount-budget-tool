"""Conservative source audit for refunds and signed supplemental income.

Two-posting transactions can prove a unique bookkeeping counterpart. Native
one-direction asset flows with only expenses or only incomes can instead
prove a joint source group, without inventing pairwise evidence allocations.
Asset semantics and personal liability remain separate reviews. Other
Costed sales prove their complete lot/fee equation, including losses. Other
complex events require explicit evidence, never matching dates or totals.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import BudgetError, require
from ..exact import exact, exact_text
from ..financial import load_financial, reference_financial
from ..yamlstrict import load_yaml, require_yaml, yaml_path_location
from .classification import _key, audit_classification
from .provenance import valid_digest
from .facts import FactIndex, future_transaction_refs, reference_fact_index
from .native_flows import native_asset_flow, native_book_redemption, native_payable_receipt


def costed_sale_candidate(entry: data.Transaction, income: data.Posting,
                          fin, facts: FactIndex, report_currency: str) -> dict | None:
    """Describe the complete cost-lot/receipt/income/fee group, if unique."""
    asset_root = fin.options["name_assets"]
    expense_root = fin.options["name_expenses"]
    income_root = fin.options["name_income"]
    if (not any(p is income for p in entry.postings) or
            income.account.split(":", 1)[0] != income_root or
            income.units.currency != report_currency or income.cost is not None or
            income.price is not None or fin.raw_price(income)[0] != "none"):
        return None
    cash = [p for p in entry.postings if p.account.split(":", 1)[0] == asset_root
            and p.units.currency == report_currency and exact(p.units.number) > 0
            and p.cost is None and p.price is None and fin.raw_price(p)[0] == "none"]
    sold = [p for p in entry.postings if p.account.split(":", 1)[0] == asset_root
            and exact(p.units.number) < 0 and p.cost is not None
            and p.cost.currency == report_currency and p.cost.number is not None
            and exact(p.cost.number) > 0
            and (p.price is None or p.price.currency == report_currency)]
    fees = [p for p in entry.postings if p.account.split(":", 1)[0] == expense_root
            and p.units.currency == report_currency and exact(p.units.number) >= 0
            and p.cost is None and p.price is None and fin.raw_price(p)[0] == "none"]
    if (len(cash) != 1 or not sold or
            len(entry.postings) != len(sold) + len(fees) + 2):
        return None
    principal = sum((-exact(p.units.number) * exact(p.cost.number) for p in sold), Fraction(0))
    receipt = exact(cash[0].units.number)
    gain = -exact(income.units.number)
    fee = sum((exact(p.units.number) for p in fees), Fraction(0))
    if principal <= 0 or receipt != principal + gain - fee:
        return None
    inventory = ({"inventory_ref": facts.for_posting(sold[0]).as_dict()} if len(sold) == 1 else {
        "inventory_refs": [facts.for_posting(p).as_dict() for p in sold],
        "inventory_components": [
            {"ref": facts.for_posting(p).as_dict(), "signed_units": exact_text(exact(p.units.number)),
             "unit_cost": exact_text(exact(p.cost.number)), "cost_currency": p.cost.currency,
             "principal_report_value": exact_text(-exact(p.units.number) * exact(p.cost.number))}
            for p in sold]})
    return {"status": "proposed", "kind": "costed_sale_income",
            "date": entry.date.isoformat(), "narration": entry.narration,
            "income_ref": facts.for_posting(income).as_dict(),
            "cash_ref": facts.for_posting(cash[0]).as_dict(),
            **inventory,
            "fee_refs": [facts.for_posting(p).as_dict() for p in fees],
            "principal_report_value": exact_text(principal),
            "income_report_value": exact_text(gain),
            "fee_report_value": exact_text(fee),
            "cash_receipt_report_value": exact_text(receipt),
            "equation": "cash_receipt = principal_at_booked_cost + income - fee"}


def _costed_proof(record: dict, candidate: dict, reason: str | None = None) -> dict:
    inventory = ({"inventory_evidence_ref": candidate["inventory_ref"]} if "inventory_ref" in candidate else {
        "inventory_evidence_refs": candidate["inventory_refs"],
        "inventory_components": candidate["inventory_components"]})
    return {**record, **inventory, "cash_evidence_ref": candidate["cash_ref"],
            "fee_evidence_refs": candidate["fee_refs"],
            **{key: candidate[key] for key in ("principal_report_value", "income_report_value",
                                              "fee_report_value", "cash_receipt_report_value")},
            "proof": "exact_costed_sale_group_v1" if reason is None else "approved_costed_sale_equation",
            **({"review_reason": reason} if reason is not None else {})}


def audit_economic_benefits(root: Path, cutover: date,
                            classifications: Path | None = None,
                            approved_groups: Path | None = None) -> dict:
    """Find uniquely sourced refunds, income receipts and costed-sale losses.

    This does not certify net expense or cash arrival: asset semantics,
    liability ownership, valuation and classification remain separate.
    """
    classification = audit_classification(root, cutover, classifications)
    fin = load_financial(root)
    facts = FactIndex(fin)
    claimed = {_key(row["ref"]): row for row in classification["classifications"]}
    expense_root = fin.options["name_expenses"]
    income_root = fin.options["name_income"]
    proved = []
    pending = []
    complex_context = {}
    complex_records = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        native, native_checked, native_proof = None, False, None
        for posting in entry.postings:
            kind = posting.account.split(":", 1)[0]
            costed_income = kind == income_root and any(
                p.account.split(":", 1)[0] == fin.options["name_assets"] and
                p.cost is not None and exact(p.units.number) < 0 for p in entry.postings)
            if kind not in (expense_root, income_root) or (exact(posting.units.number) >= 0 and not costed_income):
                continue
            ref = facts.for_posting(posting).as_dict()
            source = f"{(posting.meta or {}).get('filename', '?')}:{(posting.meta or {}).get('lineno', '?')}"
            record = {"ref": ref, "account": posting.account,
                      "signed_units": exact_text(exact(posting.units.number)),
                      "commodity": posting.units.currency, "source": source,
                      "role": "refund" if kind == expense_root else "income_supplement"}
            if _key(ref) not in claimed:
                pending.append({**record, "reason_code": "BENEFIT_CLASSIFICATION_PENDING"})
                continue
            if len(entry.postings) != 2:
                if not native_checked:
                    native = native_asset_flow(fin, entry)
                    native_checked = True
                if native is not None and native.asset_total > 0:
                    # Every beneficiary belongs to one projection domain and
                    # the entire joint evidence equals their joint amount.
                    # Do not reuse a full receipt as a per-posting allocation.
                    if native_proof is None:
                        native_proof = {
                            "transaction_ref": facts.for_transaction(entry).as_dict(),
                            "asset_refs": [facts.for_posting(p).as_dict() for p in native.assets],
                            "benefit_refs": [facts.for_posting(p).as_dict() for p in native.counterparts],
                            "asset_total": exact_text(native.asset_total),
                            "benefit_total": exact_text(-native.asset_total),
                            "commodity": posting.units.currency,
                            "basis": "joint_native_totals_no_pairwise_allocation"}
                    proved.append({**record, "proof": "same_transaction_native_asset_group",
                                   "evidence_group": native_proof})
                    continue
                receipt = native_payable_receipt(fin, entry)
                if receipt is not None and any(p is posting for p in receipt.incomes):
                    proved.append({**record, "proof": "native_income_payable_v1",
                                   "evidence_refs": [facts.for_posting(p).as_dict() for p in entry.postings
                                                     if p is not posting],
                                   "cash_receipt_native": exact_text(receipt.received),
                                   "income_total_native": exact_text(receipt.income),
                                   "payable_native": exact_text(receipt.payable),
                                   "equation": "cash_receipt = income_total + payable"})
                    continue
                redemption = native_book_redemption(fin, entry)
                if redemption is not None and redemption.income is posting:
                    proved.append({**record, "proof": "same_transaction_book_redemption_equation",
                                   "cash_evidence_ref": facts.for_posting(redemption.cash).as_dict(),
                                   "book_evidence_ref": facts.for_posting(redemption.book).as_dict(),
                                   "principal_book_value": exact_text(redemption.principal),
                                   "income_report_value": exact_text(redemption.gain),
                                   "cash_receipt_report_value": exact_text(redemption.receipt),
                                   "principal_basis": "book_flow_not_cost_or_market_value",
                                   "equation": "cash_receipt = book_principal + income"})
                    continue
                candidate = costed_sale_candidate(entry, posting, fin, facts, posting.units.currency)
                complex_context[_key(ref)] = (entry, posting)
                complex_records[_key(ref)] = record
                if candidate is not None:
                    proved.append(_costed_proof(record, candidate))
                else:
                    pending.append({**record, "reason_code": "BENEFIT_GROUP_SHAPE_UNSUPPORTED"})
                continue
            counterpart = entry.postings[1] if entry.postings[0] is posting else entry.postings[0]
            counterpart_kind = counterpart.account.split(":", 1)[0]
            if (counterpart_kind not in (fin.options["name_assets"], fin.options["name_liabilities"])
                    or counterpart.units.currency != posting.units.currency
                    or exact(counterpart.units.number) != -exact(posting.units.number)):
                pending.append({**record, "reason_code": "BENEFIT_SOURCE_UNPROVED"})
                continue
            proved.append({**record, "evidence_ref": facts.for_posting(counterpart).as_dict(),
                          "proof": "unique_same_transaction_booked_counterpart"})
    if approved_groups is not None:
        future_groups = None
        document = load_yaml(approved_groups)
        fields = {
            "schema", "cutover", "ledger_snapshot", "loader_version",
            "report_currency", "approvals"}
        invalid = sorted(set(document) - fields, key=str) if type(document) is dict else []
        require_yaml(type(document) is dict and set(document) == fields,
                     "BENEFIT_GROUP_INVALID", "经济组批准文件字段不完整或含未知字段", approved_groups,
                     (invalid[0],) if invalid else ())
        currency = document["report_currency"]
        for field, valid in (
            ("schema", type(document["schema"]) is int and document["schema"] == 6),
            ("cutover", type(document["cutover"]) is str and document["cutover"] == cutover.isoformat()),
            ("ledger_snapshot", valid_digest(document["ledger_snapshot"])),
            ("loader_version", document["loader_version"] == fin.loader_version),
            ("report_currency", type(currency) is str and currency in fin.options.get("operating_currency", [])),
            ("approvals", type(document["approvals"]) is list),
        ):
            require_yaml(valid, "BENEFIT_GROUP_INVALID", "经济组版本、原审快照格式或报告货币不匹配",
                         approved_groups, (field,))
        seen = set()
        approved = set()
        for row_index, row in enumerate(document["approvals"]):
            keys = ("approvals", row_index)
            invalid = sorted(set(row) - {"income_ref", "reason"}, key=str) if type(row) is dict else []
            require_yaml(type(row) is dict and set(row) == {"income_ref", "reason"},
                         "BENEFIT_GROUP_INVALID", "经济组批准须有完整收入引用与理由", approved_groups,
                         (*keys, invalid[0]) if invalid else keys)
            require_yaml(type(row["income_ref"]) is dict, "BENEFIT_GROUP_INVALID",
                         "income_ref 须为完整收入引用", approved_groups, (*keys, "income_ref"))
            require_yaml(type(row["reason"]) is str and bool(row["reason"].strip()),
                         "BENEFIT_GROUP_INVALID", "经济组批准须填写理由", approved_groups, (*keys, "reason"))
            location = yaml_path_location(approved_groups, (*keys, "income_ref"))
            try:
                key = _key(row["income_ref"])
            except (TypeError, ValueError) as exc:
                raise BudgetError("BENEFIT_GROUP_REF_INVALID", "收入引用格式无效；请重新导出完整引用", location) from exc
            require(key not in seen, "BENEFIT_GROUP_DUPLICATE", "同一收入重复认领经济组", location)
            seen.add(key)
            if key not in complex_context:
                if future_groups is None:
                    future_groups = {_key(ref): (entry, posting)
                                     for entry, refs in future_transaction_refs(fin, cutover)
                                     if len(entry.postings) > 2
                                     for posting, ref in zip(entry.postings, refs)
                                     if posting.account.split(":", 1)[0] == income_root}
                    reference_fin = reference_financial(fin)
                    reference_facts = reference_fact_index(fin) if future_groups else facts
                if key in future_groups:
                    entry, posting = future_groups[key]
                    require(costed_sale_candidate(entry, posting, reference_fin, reference_facts, currency)
                            is not None, "BENEFIT_GROUP_UNPROVED",
                            "未来证券成本、到账、收益与费用未构成唯一守恒组", location)
                    continue
            require(key in complex_context, "BENEFIT_GROUP_REF_INVALID",
                    "经济组收入引用不存在、过期或无需复杂分摊", location)
            entry, posting = complex_context[key]
            candidate = costed_sale_candidate(entry, posting, fin, facts, currency)
            require(candidate is not None and candidate["income_ref"] == row["income_ref"],
                    "BENEFIT_GROUP_UNPROVED", "证券成本、到账、收益与费用未构成唯一守恒组", location)
            approved.add(key)
            proved = [item for item in proved if _key(item["ref"]) != key]
            proved.append(_costed_proof(complex_records[key], candidate, row["reason"]))
        pending = [item for item in pending if _key(item["ref"]) not in approved]
    return {"mode": "v6_economic_benefit_audit_only",
            "ledger_snapshot": fin.digest, "cutover": cutover.isoformat(),
            "review_state": "complete" if not pending else "pending",
            "proved_benefits": len(proved), "pending_benefits": pending,
            "proved": proved,
            "note": "唯一 booked 对手不证明资产为真实现金或负债属本人；也不证明历史计价和分类闭合，不签发净费用"}

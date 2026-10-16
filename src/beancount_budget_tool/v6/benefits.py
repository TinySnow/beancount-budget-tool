"""Conservative source audit for refunds and supplemental income.

Two-posting transactions can prove a unique bookkeeping counterpart. Native
one-direction asset flows with only expenses or only incomes can instead
prove a joint source group, without inventing pairwise evidence allocations.
Asset semantics and personal liability remain separate reviews. Other
complex events require explicit evidence, never matching dates or totals.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial, reference_financial
from ..yamlstrict import load_yaml, yaml_path_location
from .classification import _key, audit_classification
from .provenance import valid_digest
from .facts import FactIndex, future_transaction_refs, reference_fact_index
from .native_flows import native_asset_flow


def costed_sale_candidate(entry: data.Transaction, income: data.Posting,
                          fin, facts: FactIndex, report_currency: str) -> dict | None:
    """Describe one exact booked cost/receipt/income equation, if unique."""
    asset_root = fin.options["name_assets"]
    expense_root = fin.options["name_expenses"]
    income_root = fin.options["name_income"]
    if (income.account.split(":", 1)[0] != income_root or
            income.units.currency != report_currency or exact(income.units.number) >= 0 or
            len(entry.postings) not in (3, 4)):
        return None
    cash = [p for p in entry.postings if p.account.split(":", 1)[0] == asset_root
            and p.units.currency == report_currency and exact(p.units.number) > 0
            and p.cost is None]
    sold = [p for p in entry.postings if p.account.split(":", 1)[0] == asset_root
            and exact(p.units.number) < 0 and p.cost is not None
            and p.cost.currency == report_currency]
    fees = [p for p in entry.postings if p.account.split(":", 1)[0] == expense_root
            and p.units.currency == report_currency and exact(p.units.number) > 0]
    if (len(cash) != 1 or len(sold) != 1 or len(fees) > 1 or
            len(entry.postings) != 3 + len(fees)):
        return None
    principal = -exact(sold[0].units.number) * exact(sold[0].cost.number)
    receipt = exact(cash[0].units.number)
    gain = -exact(income.units.number)
    fee = sum((exact(p.units.number) for p in fees), Fraction(0))
    if principal <= 0 or gain <= 0 or receipt != principal + gain - fee:
        return None
    return {"status": "proposed", "kind": "costed_sale_income",
            "date": entry.date.isoformat(), "narration": entry.narration,
            "income_ref": facts.for_posting(income).as_dict(),
            "cash_ref": facts.for_posting(cash[0]).as_dict(),
            "inventory_ref": facts.for_posting(sold[0]).as_dict(),
            "fee_refs": [facts.for_posting(p).as_dict() for p in fees],
            "principal_report_value": exact_text(principal),
            "income_report_value": exact_text(gain),
            "fee_report_value": exact_text(fee),
            "cash_receipt_report_value": exact_text(receipt),
            "equation": "cash_receipt = principal_at_booked_cost + income - fee"}


def audit_economic_benefits(root: Path, cutover: date,
                            classifications: Path | None = None,
                            approved_groups: Path | None = None) -> dict:
    """Find uniquely sourced negative expenses and income receipts.

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
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        native, native_checked, native_proof = None, False, None
        for posting in entry.postings:
            kind = posting.account.split(":", 1)[0]
            if kind not in (expense_root, income_root) or exact(posting.units.number) >= 0:
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
                pending.append({**record, "reason_code": "BENEFIT_GROUP_REQUIRED"})
                complex_context[_key(ref)] = (entry, posting)
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
        future_groups = {_key(ref): (entry, posting)
                         for entry, refs in future_transaction_refs(fin, cutover)
                         if len(entry.postings) > 2
                         for posting, ref in zip(entry.postings, refs)
                         if posting.account.split(":", 1)[0] == income_root
                         and exact(posting.units.number) < 0}
        reference_fin = reference_financial(fin)
        reference_facts = reference_fact_index(fin) if future_groups else facts
        document = load_yaml(approved_groups)
        require(type(document) is dict and set(document) == {
            "schema", "cutover", "ledger_snapshot", "loader_version",
            "report_currency", "approvals"},
            "BENEFIT_GROUP_INVALID", "经济组批准文件字段不完整或含未知字段")
        currency = document["report_currency"]
        require(type(document["schema"]) is int and document["schema"] == 6 and
                document["cutover"] == cutover.isoformat() and
                valid_digest(document["ledger_snapshot"]) and
                document["loader_version"] == fin.loader_version and
                type(currency) is str and currency in fin.options.get("operating_currency", []) and
                type(document["approvals"]) is list,
                "BENEFIT_GROUP_INVALID", "经济组版本、原审快照格式或报告货币不匹配")
        seen = set()
        approved = set()
        for row_index, row in enumerate(document["approvals"]):
            require(type(row) is dict and set(row) == {"income_ref", "reason"} and
                    type(row["income_ref"]) is dict and type(row["reason"]) is str and
                    bool(row["reason"].strip()),
                    "BENEFIT_GROUP_INVALID", "经济组批准须有完整收入引用与理由")
            key = _key(row["income_ref"])
            require(key not in seen, "BENEFIT_GROUP_DUPLICATE", "同一收入重复认领经济组", key)
            seen.add(key)
            location = yaml_path_location(approved_groups, ("approvals", row_index))
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
                    "BENEFIT_GROUP_UNPROVED", "证券成本、到账、收益与费用未构成唯一守恒组", key)
            approved.add(key)
            source = next(item for item in pending if _key(item["ref"]) == key)
            proved.append({k: v for k, v in source.items() if k != "reason_code"} |
                          {"cash_evidence_ref": candidate["cash_ref"],
                           "inventory_evidence_ref": candidate["inventory_ref"],
                           "fee_evidence_refs": candidate["fee_refs"],
                           "principal_report_value": candidate["principal_report_value"],
                           "income_report_value": candidate["income_report_value"],
                           "fee_report_value": candidate["fee_report_value"],
                           "cash_receipt_report_value": candidate["cash_receipt_report_value"],
                           "proof": "approved_costed_sale_equation", "review_reason": row["reason"]})
        pending = [item for item in pending if _key(item["ref"]) not in approved]
    return {"mode": "v6_economic_benefit_audit_only",
            "ledger_snapshot": fin.digest, "cutover": cutover.isoformat(),
            "review_state": "complete" if not pending else "pending",
            "proved_benefits": len(proved), "pending_benefits": pending,
            "proved": proved,
            "note": "唯一 booked 对手不证明资产为真实现金或负债属本人；也不证明历史计价和分类闭合，不签发净费用"}

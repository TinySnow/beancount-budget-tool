"""Assemble explicit costed-source fixtures for legacy proof acceptance tests."""
from pathlib import Path
from datetime import date
from beancount.core import data
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.classification import _key
from beancount_budget_tool.v6.benefits import audit_economic_benefits, costed_sale_candidate


def propose_groups(root: Path, cutover: date, classifications: Path,
            report_currency: str) -> dict:
    benefit_audit = audit_economic_benefits(root, cutover, classifications)
    pending = {_key(row["ref"]) for row in benefit_audit["pending_benefits"]
               if row["reason_code"] == "BENEFIT_GROUP_REQUIRED"}
    proved = {_key(row["ref"]) for row in benefit_audit["proved"]
              if row["proof"] in {"exact_costed_sale_group_v1", "approved_costed_sale_equation"}}
    fin = load_financial(root)
    facts = FactIndex(fin)
    proposals = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        benefit = [p for p in entry.postings
                   if _key(facts.for_posting(p).as_dict()) in pending | proved]
        if len(benefit) != 1:
            continue
        candidate = costed_sale_candidate(entry, benefit[0], fin, facts, report_currency)
        if candidate is not None:
            proposals.append(candidate)
    return {"mode": "v6_benefit_group_proposals_only", "schema": 6,
            "cutover": cutover.isoformat(), "ledger_snapshot": fin.digest,
            "loader_version": fin.loader_version, "report_currency": report_currency,
            "pending_complex_benefits": len(pending), "proposed_groups": len(proposals),
            "automatically_proved_groups": sum(row["proof"] == "exact_costed_sale_group_v1"
                                                for row in benefit_audit["proved"]),
            "unsupported_complex_benefits": sum(row["reason_code"] == "BENEFIT_GROUP_SHAPE_UNSUPPORTED"
                                                for row in benefit_audit["pending_benefits"]),
            "proposals": proposals,
            "note": "只列明成本来源等式；明确模板由程序核对，不批准预算分类、资产用途或支付能力"}

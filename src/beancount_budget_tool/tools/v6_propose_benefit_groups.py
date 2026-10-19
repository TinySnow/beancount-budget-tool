"""Propose source-bound costed-sale benefit groups without approving them."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from beancount.core import data

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.benefits import audit_economic_benefits, costed_sale_candidate
from beancount_budget_tool.v6.classification import _key
from beancount_budget_tool.v6.facts import FactIndex


def propose(root: Path, cutover: date, classifications: Path,
            report_currency: str) -> dict:
    benefit_audit = audit_economic_benefits(root, cutover, classifications)
    pending = {_key(row["ref"]) for row in benefit_audit["pending_benefits"]
               if row["reason_code"] == "BENEFIT_GROUP_REQUIRED"}
    fin = load_financial(root)
    facts = FactIndex(fin)
    proposals = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        benefit = [p for p in entry.postings
                   if _key(facts.for_posting(p).as_dict()) in pending]
        if len(benefit) != 1:
            continue
        candidate = costed_sale_candidate(entry, benefit[0], fin, facts, report_currency)
        if candidate is not None:
            proposals.append(candidate)
    return {"mode": "v6_benefit_group_proposals_only", "schema": 6,
            "cutover": cutover.isoformat(), "ledger_snapshot": fin.digest,
            "loader_version": fin.loader_version, "report_currency": report_currency,
            "pending_complex_benefits": len(pending), "proposed_groups": len(proposals),
            "unsupported_complex_benefits": sum(row["reason_code"] == "BENEFIT_GROUP_SHAPE_UNSUPPORTED"
                                                for row in benefit_audit["pending_benefits"]),
            "proposals": proposals,
            "note": "候选恒等式只核账，不构成经济组批准或资产用途归属"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cutover", type=date.fromisoformat, required=True)
    parser.add_argument("--classifications", type=Path, required=True)
    parser.add_argument("--report-currency", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = propose(args.ledger, args.cutover, args.classifications,
                     args.report_currency)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k != "proposals"},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

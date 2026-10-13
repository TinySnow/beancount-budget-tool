"""Group missing v6 classifications with legacy hints; approve none of them."""

from __future__ import annotations

import argparse
from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date
from fractions import Fraction
import json
from pathlib import Path

from beancount.core import data

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.source import scan_v6_source


def propose(root: Path, cutover: date, through: date | None = None) -> dict:
    scan_v6_source(root, cutover, through)
    fin = load_financial(root)
    facts = FactIndex(fin)
    roots = {fin.options["name_expenses"], fin.options["name_income"]}
    groups = defaultdict(list)
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover or (
                through is not None and entry.date > through):
            continue
        meta = entry.meta or {}
        for posting in entry.postings:
            if (posting.account.split(":", 1)[0] not in roots or
                    "budget" in (posting.meta or {})):
                continue
            groups[posting.account].append({
                "ref": facts.for_posting(posting).as_dict(),
                "account": posting.account,
                "signed_units": str(Fraction(posting.units.number)),
                "commodity": posting.units.currency,
                "date": entry.date.isoformat(), "narration": entry.narration,
                "legacy_budget_hint": meta.get("legacy_budget"),
                "legacy_fund_hint": meta.get("legacy_fund"),
            })
    return {
        "mode": "v6_classification_proposals_only",
        "cutover": cutover.isoformat(),
        "through": through.isoformat() if through else None,
        "ledger_snapshot": fin.digest,
        "groups": [
            {"account": account, "count": len(items),
             "negative_count": sum(Fraction(item["signed_units"]) < 0 for item in items),
             "legacy_budget_hints": dict(sorted(Counter(
                 str(item["legacy_budget_hint"]) for item in items
                 if item["legacy_budget_hint"] is not None).items())),
             "candidates": items}
            for account, items in sorted(groups.items())],
        "note": "旧标签仅是迁移提示；本文件没有批准任何费用／收入分类",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    period = parser.add_mutually_exclusive_group(required=True)
    period.add_argument("--cutover", type=date.fromisoformat,
                        help="从指定月起，列出之后所有缺少行内 budget 的费用／收入")
    period.add_argument("--month", help="只列出 YYYY-MM 当月缺少行内 budget 的费用／收入")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.month:
        try:
            first = date.fromisoformat(args.month + "-01")
            if first.strftime("%Y-%m") != args.month:
                raise ValueError
        except ValueError:
            parser.error("--month 必须是有效的 YYYY-MM")
        last = date(first.year, first.month, monthrange(first.year, first.month)[1])
        output = propose(args.ledger, first, last)
    else:
        output = propose(args.ledger, args.cutover)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print({"groups": len(output["groups"]),
           "candidates": sum(g["count"] for g in output["groups"]),
           "output": str(args.output)})


if __name__ == "__main__":
    main()

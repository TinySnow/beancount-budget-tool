"""Write a private, source-bound H ownership review packet without guessing owners."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path

from beancount.core import data

from beancount_budget_tool.exact import exact, exact_text
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.classification import _key
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.ownership import audit_ownership


def propose(root: Path, cutover: date, accounts: Path) -> dict:
    audit = audit_ownership(root, cutover, accounts)
    pending = {_key(row["ref"]): row for row in audit["missing_h_ownership"]}
    fin = load_financial(root)
    facts = FactIndex(fin)
    rows = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        for posting in entry.postings:
            key = _key(facts.for_posting(posting).as_dict())
            if key not in pending:
                continue
            meta = posting.meta or {}
            rows.append({
                **pending[key], "date": entry.date.isoformat(),
                "payee": entry.payee, "narration": entry.narration,
                "source": f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}",
                "legacy_budget_hint": (entry.meta or {}).get("legacy_budget"),
                "legacy_fund_hint": (entry.meta or {}).get("legacy_fund"),
                "other_postings": [
                    {"account": other.account,
                     "signed_units": exact_text(exact(other.units.number)),
                     "commodity": other.units.currency}
                    for other in entry.postings if other is not posting],
                "status": "needs_review",
            })
    by_account = Counter(row["account"] for row in rows)
    return {"mode": "v6_ownership_proposals_only", "cutover": cutover.isoformat(),
            "ledger_snapshot": fin.digest, "candidates": len(rows),
            "by_account": dict(sorted(by_account.items())), "rows": rows,
            "note": "账户、叙述和旧标签仅为审阅线索，不构成逐腿 owner 或用途边界决定"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cutover", type=date.fromisoformat, required=True)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = propose(args.ledger, args.cutover, args.accounts)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k != "rows"},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

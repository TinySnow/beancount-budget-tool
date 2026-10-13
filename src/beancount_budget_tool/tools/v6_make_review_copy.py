"""Make an isolated v6 review copy while retaining legacy tag hints.

Only transaction-level budget/fund metadata are renamed to legacy_budget /
legacy_fund.  No transaction, posting, amount, account, cost, price or date is
changed.  The copy is never a certified v6 ledger by itself.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re

from beancount.core import data

from beancount_budget_tool.financial import load_financial


DIRECTIVE = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
TRANSACTION = re.compile(r"^\d{4}-\d{2}-\d{2}\s+[*!?]\s")
POSTING = re.compile(r"^[ \t]+[A-Z][A-Za-z0-9_-]*(?::[^ \t\r\n]+)+(?:[ \t]+|[\r\n]*$)")
LEGACY_TAG = re.compile(r"^([ \t]+)(budget|fund)([ \t]*:)")


def _rewrite(text: str) -> tuple[str, int]:
    in_transaction = False
    seen_posting = False
    changed = 0
    output = []
    for line in text.splitlines(keepends=True):
        if DIRECTIVE.match(line):
            in_transaction = bool(TRANSACTION.match(line))
            seen_posting = False
        elif line.strip() and not line[0].isspace():
            in_transaction = False
        if in_transaction and POSTING.match(line):
            seen_posting = True
        match = LEGACY_TAG.match(line) if in_transaction and not seen_posting else None
        if match:
            line = line[:match.start(2)] + "legacy_" + line[match.start(2):]
            changed += 1
        output.append(line)
    return "".join(output), changed


def _financial_facts(entries: tuple) -> tuple:
    result = []
    for entry in entries:
        if isinstance(entry, data.Transaction):
            entry = entry._replace(postings=tuple(p._replace(meta=None) for p in entry.postings))
        result.append(entry._replace(meta=None))
    return tuple(result)


def make_copy(root: Path, destination: Path) -> dict:
    source = load_financial(root)
    base = source.root.parent
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"目标目录已存在，拒绝覆盖：{destination}")
    paths = {}
    for path in source.sources:
        if not path.is_relative_to(base):
            raise ValueError(f"include 位于根账本目录外，不能保持相对路径：{path}")
        paths[path] = destination / path.relative_to(base)
    destination.mkdir(parents=True)
    changed = 0
    for original, target in paths.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        text = original.read_text(encoding="utf-8-sig")
        rewritten, count = _rewrite(text)
        target.write_text(rewritten, encoding="utf-8")
        changed += count
    review = load_financial(paths[source.root])
    if _financial_facts(source.entries) != _financial_facts(review.entries):
        raise ValueError("变换前后 Beancount 财务事实不一致，副本不可使用")
    return {"root": str(paths[source.root]), "source_files": len(paths),
            "renamed_transaction_tags": changed,
            "financial_facts_equal": True, "ledger_snapshot": review.digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    print(make_copy(args.ledger, args.destination))


if __name__ == "__main__":
    main()

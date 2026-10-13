"""Upgrade one reviewed as-of completeness statement for later importer appends."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

import yaml

from beancount_budget_tool.errors import require
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.provenance import as_of_booked_fingerprint
from beancount_budget_tool.yamlstrict import load_yaml


def upgrade(root: Path, cutover: date, as_of: date, accounts: Path, opening: Path,
            ownership: Path, obligations: Path, completeness: Path, output: Path) -> dict:
    require(output.resolve() != completeness.resolve() and not output.exists(),
            "REVIEW_OUTPUT_EXISTS", "须为完整性声明选择新的输出路径")
    original = audit_actual_snapshot(root, cutover, as_of, accounts, opening,
                                     ownership, obligations, completeness)
    fin = load_financial(root)
    require(original["ledger_snapshot"] == fin.digest,
            "REVIEW_UPGRADE_CHANGED", "升级期间账本发生变化")
    document = load_yaml(completeness)
    fingerprint = as_of_booked_fingerprint(fin, as_of)
    document["as_of_booked_fingerprint"] = fingerprint
    try:
        output.write_text(yaml.safe_dump(document, allow_unicode=True,
                                         sort_keys=False), encoding="utf-8")
        checked = audit_actual_snapshot(root, cutover, as_of, accounts, opening,
                                        ownership, obligations, output)
        require((checked["status"], checked["actual_spendable"], checked["reason_codes"]) ==
                (original["status"], original["actual_spendable"], original["reason_codes"]),
                "REVIEW_UPGRADE_CHANGED", "升级后的实际可花审计结果发生变化")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return {"mode": "v6_as_of_completeness_upgrade", "as_of": as_of.isoformat(),
            "as_of_booked_fingerprint": fingerprint,
            "status": checked["status"], "actual_spendable": checked["actual_spendable"],
            "output": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("ledger", "accounts", "opening", "ownership", "obligations",
                 "completeness", "output"):
        parser.add_argument("--" + name.replace("_", "-"), required=True, type=Path)
    parser.add_argument("--cutover", required=True, type=date.fromisoformat)
    parser.add_argument("--as-of", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    print(json.dumps(upgrade(args.ledger, args.cutover, args.as_of, args.accounts,
                             args.opening, args.ownership, args.obligations,
                             args.completeness, args.output), ensure_ascii=False,
                     sort_keys=True))


if __name__ == "__main__":
    main()

"""Upgrade reviewed account/opening forms to cutover-scoped provenance.

The source forms must first validate against the current ledger. New files are
written separately, then re-audited; no Beancount source is changed.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

import yaml

from beancount_budget_tool.errors import require
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.opening import audit_opening
from beancount_budget_tool.yamlstrict import load_yaml


def upgrade(root: Path, cutover: date, accounts: Path, opening: Path,
            output_accounts: Path, output_opening: Path) -> dict:
    """Validate old reviews, create new copies and verify their exact meaning."""
    source_paths = {accounts.resolve(), opening.resolve()}
    outputs = {output_accounts.resolve(), output_opening.resolve()}
    require(len(outputs) == 2 and not outputs & source_paths and
            not output_accounts.exists() and not output_opening.exists(),
            "REVIEW_OUTPUT_EXISTS", "须使用两个新的、互不相同的审阅文件路径")
    account_audit = audit_accounts(root, cutover, accounts)
    opening_audit = audit_opening(root, cutover, accounts, opening)
    fingerprint = account_audit["opening_fingerprint"]
    account_doc = load_yaml(accounts)
    opening_doc = load_yaml(opening)
    account_doc["opening_fingerprint"] = fingerprint
    opening_doc["opening_fingerprint"] = fingerprint
    try:
        output_accounts.write_text(yaml.safe_dump(account_doc, allow_unicode=True,
                                                  sort_keys=False), encoding="utf-8")
        output_opening.write_text(yaml.safe_dump(opening_doc, allow_unicode=True,
                                                sort_keys=False), encoding="utf-8")
        checked_accounts = audit_accounts(root, cutover, output_accounts)
        checked_opening = audit_opening(root, cutover, output_accounts, output_opening)
        require(checked_accounts["review_state"] == account_audit["review_state"] and
                checked_opening["review_state"] == opening_audit["review_state"] and
                checked_opening["missing_h_ownership"] == opening_audit["missing_h_ownership"] and
                checked_opening["missing_k_openings"] == opening_audit["missing_k_openings"],
                "REVIEW_UPGRADE_CHANGED", "升级后的审阅结果与原文件不一致")
    except Exception:
        output_accounts.unlink(missing_ok=True)
        output_opening.unlink(missing_ok=True)
        raise
    return {"mode": "v6_review_provenance_upgrade", "ledger_snapshot": account_audit["ledger_snapshot"],
            "opening_fingerprint": fingerprint,
            "account_review_state": checked_accounts["review_state"],
            "opening_review_state": checked_opening["review_state"],
            "output_accounts": str(output_accounts), "output_opening": str(output_opening)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--cutover", required=True, type=date.fromisoformat)
    parser.add_argument("--accounts", required=True, type=Path)
    parser.add_argument("--opening", required=True, type=Path)
    parser.add_argument("--output-accounts", required=True, type=Path)
    parser.add_argument("--output-opening", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(upgrade(args.ledger, args.cutover, args.accounts, args.opening,
                             args.output_accounts, args.output_opening),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

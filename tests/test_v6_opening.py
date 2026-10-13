"""Cutover allocations must conserve real H units and only confirm K book flow."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.opening import audit_opening


CUTOVER = date(2026, 10, 1)
LEDGER = (
    'option "operating_currency" "CNY"\n'
    '2026-09-01 open Assets:Cash\n'
    '2026-09-01 open Assets:Fund\n'
    '2026-09-01 open Equity:Opening\n'
    '2026-09-02 * "cash opening"\n'
    '  Assets:Cash 1000 CNY\n'
    '  Equity:Opening -1000 CNY\n'
    '2026-09-03 * "subscription"\n'
    '  Assets:Fund 100 CNY\n'
    '  Assets:Cash -100 CNY\n'
    '2026-10-02 * "redemption"\n'
    '  Assets:Fund -120 CNY\n'
    '  Assets:Cash 120 CNY\n'
)


class OpeningAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "main.bean"
        self.root.write_text(LEDGER, encoding="utf-8")
        self.accounts = base / "accounts.yml"
        self.opening = base / "opening.yml"
        digest = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        self.digest = digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": digest,
            "accounts": [
                {"account": "Assets:Cash", "semantics": "H",
                 "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                 "reason": "可核对真实现金"},
                {"account": "Assets:Fund", "semantics": "K",
                 "balance_semantics": "signed_subscription_flow",
                 "payment_scope": {"CNY": "excluded"}, "reason": "只有申赎累计"},
            ],
        }, allow_unicode=True), encoding="utf-8")

    def write_opening(self, h=None, k=None, digest=None):
        self.opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": digest or self.digest,
            "h_holdings": h if h is not None else [
                {"account": "Assets:Cash", "commodity": "CNY", "lot": "CNY/no-cost",
                 "owner": "-", "units": "500", "reason": "核对自由现金"},
                {"account": "Assets:Cash", "commodity": "CNY", "lot": "CNY/no-cost",
                 "owner": "旅行", "units": "400", "reason": "核对旅行专款"},
            ],
            "k_openings": k if k is not None else [
                {"account": "Assets:Fund", "commodity": "CNY", "lot": "CNY/no-cost",
                 "signed_units": "100", "reason": "核对申购减赎回累计"},
            ],
        }, allow_unicode=True), encoding="utf-8")

    def test_missing_opening_stays_pending_and_full_split_passes(self):
        pending = audit_opening(self.root, CUTOVER, self.accounts)
        self.assertEqual(pending["review_state"], "pending")
        self.assertEqual(pending["missing_h_ownership"][0]["unreviewed_units"], "900")
        self.assertEqual(pending["missing_k_openings"][0]["signed_units"], "100")
        self.write_opening()
        complete = audit_opening(self.root, CUTOVER, self.accounts, self.opening)
        self.assertEqual(complete["review_state"], "complete")
        self.assertEqual(complete["missing_h_ownership"], [])
        self.assertEqual(complete["missing_k_openings"], [])

    def test_partial_h_split_is_pending_and_overallocation_rejected(self):
        self.write_opening()
        doc = yaml.safe_load(self.opening.read_text(encoding="utf-8"))
        doc["h_holdings"][1]["units"] = "300"
        self.opening.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        result = audit_opening(self.root, CUTOVER, self.accounts, self.opening)
        self.assertEqual(result["missing_h_ownership"][0]["unreviewed_units"], "100")
        doc["h_holdings"][1]["units"] = "500"
        self.opening.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_opening(self.root, CUTOVER, self.accounts, self.opening)
        self.assertEqual(caught.exception.code, "OPENING_OVERALLOCATED")

    def test_k_mismatch_and_unknown_owner_rejected(self):
        self.write_opening()
        doc = yaml.safe_load(self.opening.read_text(encoding="utf-8"))
        doc["k_openings"][0]["signed_units"] = "120"
        self.opening.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_opening(self.root, CUTOVER, self.accounts, self.opening)
        self.assertEqual(caught.exception.code, "K_OPENING_MISMATCH")
        doc["k_openings"][0]["signed_units"] = "100"
        doc["h_holdings"][0]["owner"] = "?"
        self.opening.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_opening(self.root, CUTOVER, self.accounts, self.opening)
        self.assertEqual(caught.exception.code, "OPENING_OWNER_INVALID")

    def test_old_snapshot_is_ignored_but_opening_units_are_rechecked(self):
        self.write_opening(digest="0" * 64)
        self.assertEqual(audit_opening(self.root, CUTOVER, self.accounts, self.opening)
                         ["review_state"], "complete")
        document = yaml.safe_load(self.opening.read_text(encoding="utf-8"))
        del document["ledger_snapshot"]
        self.opening.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.assertEqual(audit_opening(self.root, CUTOVER, self.accounts, self.opening)
                         ["review_state"], "complete")
        self.root.write_text(LEDGER.replace("Assets:Cash 1000 CNY", "Assets:Cash 900 CNY")
                             .replace("Equity:Opening -1000 CNY", "Equity:Opening -900 CNY"),
                             encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_opening(self.root, CUTOVER, self.accounts, self.opening)
        self.assertEqual(caught.exception.code, "OPENING_OVERALLOCATED")

    def test_negative_k_opening_is_signed_book_flow_not_h_inventory(self):
        self.root.write_text(LEDGER.replace("Assets:Fund 100 CNY", "Assets:Fund -100 CNY")
                             .replace("Assets:Cash -100 CNY", "Assets:Cash 100 CNY"),
                             encoding="utf-8")
        accounts_doc = yaml.safe_load(self.accounts.read_text(encoding="utf-8"))
        accounts_doc["ledger_snapshot"] = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        self.accounts.write_text(yaml.safe_dump(accounts_doc, allow_unicode=True), encoding="utf-8")
        pending = audit_opening(self.root, CUTOVER, self.accounts)
        self.assertEqual(pending["missing_k_openings"][0]["signed_units"], "-100")
        self.assertEqual(pending["missing_h_ownership"][0]["unreviewed_units"], "1100")


if __name__ == "__main__":
    unittest.main()

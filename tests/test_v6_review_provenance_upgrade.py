"""A reviewed opening can be safely promoted to append-stable provenance."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.payable_cash import audit_payable_cash
from beancount_budget_tool.tools.v6_upgrade_review_provenance import upgrade


CUTOVER = date(2026, 9, 1)


class ReviewProvenanceUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.ledger = folder / "main.bean"
        self.accounts = folder / "accounts.yaml"
        self.opening = folder / "opening.yaml"
        self.ownership = folder / "ownership.yaml"
        self.new_accounts = folder / "new-accounts.yaml"
        self.new_opening = folder / "new-opening.yaml"
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 100 CNY\n'
            '  Equity:Opening -100 CNY\n', encoding="utf-8")
        snapshot = load_financial(self.ledger).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "report_currency": "CNY", "accounts": [{"account": "Assets:Bank",
                "semantics": "H", "balance_semantics": "real_inventory",
                "payment_scope": {"CNY": "payable"}, "reason": "reviewed bank"}]},
            allow_unicode=True), encoding="utf-8")
        self.opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "h_holdings": [{"account": "Assets:Bank", "commodity": "CNY",
                            "lot": "CNY/no-cost", "owner": "-", "units": "100",
                            "reason": "reviewed free money"}], "k_openings": []},
            allow_unicode=True), encoding="utf-8")
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "loader_version": load_financial(self.ledger).loader_version,
            "owners": [], "claims": []}, allow_unicode=True), encoding="utf-8")

    def test_upgrade_then_append_preserves_old_report_and_blocks_new_month(self):
        result = upgrade(self.ledger, CUTOVER, self.accounts, self.opening,
                         self.new_accounts, self.new_opening)
        self.assertEqual((result["account_review_state"], result["opening_review_state"]),
                         ("complete", "complete"))
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
                               '2026-10-02 * "food"\n'
                               '  Expenses:Food 3 CNY\n'
                               '  Assets:Bank -3 CNY\n', encoding="utf-8")
        old = audit_payable_cash(self.ledger, CUTOVER, date(2026, 9, 30),
                                 self.new_accounts, self.new_opening, self.ownership)
        new = audit_payable_cash(self.ledger, CUTOVER, date(2026, 10, 31),
                                 self.new_accounts, self.new_opening, self.ownership)
        self.assertEqual((old["review_state"], old["free_cash"]), ("complete", "100"))
        self.assertEqual((new["review_state"], new["payable_coordinates_pending"]),
                         ("pending", 1))
        with self.assertRaises(BudgetError) as caught:
            upgrade(self.ledger, CUTOVER, self.accounts, self.opening,
                    self.new_accounts, self.new_opening)
        self.assertEqual(caught.exception.code, "REVIEW_OUTPUT_EXISTS")


if __name__ == "__main__":
    unittest.main()

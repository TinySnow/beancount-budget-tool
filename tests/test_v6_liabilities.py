"""Liability audit keeps booked signs and does not invent responsibility."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.v6.liabilities import audit_liabilities


class LiabilityAuditTests(unittest.TestCase):
    def test_credit_purchase_and_repayment_are_booked_signed_coordinates(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-08-01 open Liabilities:Card\n'
                '2026-08-01 open Assets:Cash\n'
                '2026-08-01 open Expenses:Food\n'
                '2026-08-31 * "old card purchase"\n'
                '  Expenses:Food 50 CNY\n'
                '  Liabilities:Card -50 CNY\n'
                '2026-09-02 * "new card purchase"\n'
                '  Expenses:Food 300 CNY\n'
                '  Liabilities:Card -300 CNY\n'
                '2026-09-03 * "payment"\n'
                '  Liabilities:Card 100 CNY\n'
                '  Assets:Cash -100 CNY\n', encoding="utf-8")
            audit = audit_liabilities(root, date(2026, 9, 1))
            self.assertEqual(audit["mode"], "v6_liability_audit_only")
            self.assertEqual(audit["accounts_with_direct_postings"], 1)
            row = audit["accounts"][0]
            self.assertEqual(row["before_cutover_postings"], 1)
            self.assertEqual(row["from_cutover_postings"], 2)
            self.assertEqual(row["opening_coordinates"], [
                {"commodity": "CNY", "lot": "CNY/no-cost", "signed_units": "-50"}])
            self.assertEqual(row["source_total_coordinates"], [
                {"commodity": "CNY", "lot": "CNY/no-cost", "signed_units": "-250"}])
            self.assertEqual(row["latest_booked_date"], "2026-09-03")
            self.assertEqual(row["first_ref"]["posting_ordinal"], 2)

    def test_zero_balance_and_direct_parent_are_not_dropped(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-09-01 open Liabilities:Card\n'
                '2026-09-01 open Liabilities:Card:USD\n'
                '2026-09-01 open Equity:Clearing\n'
                '2026-09-02 * "parent cleared"\n'
                '  Liabilities:Card -10 CNY\n'
                '  Equity:Clearing 10 CNY\n'
                '2026-09-03 * "parent paid"\n'
                '  Liabilities:Card 10 CNY\n'
                '  Equity:Clearing -10 CNY\n'
                '2026-09-04 * "child debit"\n'
                '  Liabilities:Card:USD -2 USD\n'
                '  Equity:Clearing 2 USD\n', encoding="utf-8")
            audit = audit_liabilities(root, date(2026, 9, 1))
            self.assertEqual(audit["accounts_with_direct_postings"], 2)
            parent, child = audit["accounts"]
            self.assertTrue(parent["has_children_with_direct_postings"])
            self.assertEqual(parent["source_total_coordinates"][0]["signed_units"], "0")
            self.assertFalse(child["has_children_with_direct_postings"])


if __name__ == "__main__":
    unittest.main()

"""Monthly metrics certify independently and never substitute a foreign value."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.spending import audit_monthly_spending


class MonthlySpendingTests(unittest.TestCase):
    def test_complete_month_and_later_unpriced_foreign_expense(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            review = Path(folder) / "classes.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-09-01 open Assets:Cash\n'
                '2026-09-01 open Expenses:Food\n'
                '2026-09-01 open Income:Reward\n'
                '2026-09-01 open Liabilities:Card\n'
                '2026-09-02 * "food"\n'
                '  Expenses:Food 10 CNY\n'
                '  Assets:Cash -10 CNY\n'
                '2026-09-03 * "refund"\n'
                '  Expenses:Food -2 CNY\n'
                '  Assets:Cash 2 CNY\n'
                '2026-09-04 * "reward"\n'
                '  Assets:Cash 3 CNY\n'
                '  Income:Reward -3 CNY\n'
                '2026-10-02 * "foreign education"\n'
                '  Expenses:Food 1 USD\n'
                '  Liabilities:Card -1 USD\n', encoding="utf-8")
            candidates = audit_classification(root, date(2026, 9, 1))["missing_classifications"]
            review.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-09-01", "loader_version": BEANCOUNT_VERSION,
                "categories": [
                    {"id": "living", "name": "生活费", "parent": None, "aliases": []},
                    {"id": "food", "name": "生活费.餐饮", "parent": "living", "aliases": []}],
                "classifications": [
                    {"claim": row["ref"], "reason": "虚拟账本", "slices": [
                        {"id": f"slice_{i}", "category_id":
                         "living" if row["account"].startswith("Income:") else "food",
                         "signed_units": row["signed_units"]}]}
                    for i, row in enumerate(candidates)],
            }, allow_unicode=True), encoding="utf-8")
            september = audit_monthly_spending(root, date(2026, 9, 1), "2026-09",
                                               review, "CNY")
            net = september["metrics"]["net_expense"]
            income = september["metrics"]["income_supplement"]
            self.assertEqual((net["status"], net["report_total"]), ("CERTIFIED", "8"))
            food = next(row for row in net["categories"] if row["id"] == "food")
            self.assertEqual((food["direct_gross_spent"], food["direct_refunded"],
                              food["direct"]), ("10", "2", "8"))
            self.assertEqual((income["status"], income["report_total"]),
                             ("CERTIFIED", "3"))
            self.assertEqual(next(row["subtree"] for row in net["categories"]
                                  if row["id"] == "living"), "8")
            october = audit_monthly_spending(root, date(2026, 9, 1), "2026-10",
                                             review, "CNY")
            net = october["metrics"]["net_expense"]
            self.assertEqual(net["status"], "NEEDS_REVIEW")
            self.assertIsNone(net["report_total"])
            self.assertEqual(net["reason_codes"], ["VALUATION_MISSING"])
            self.assertIn({"month": "2026-10", "role": "net_expense", "category_id": "food",
                           "commodity": "USD", "signed_units": "1"}, net["native_totals"])
            parent = next(row for row in net["categories"] if row["id"] == "living")
            food = next(row for row in net["categories"] if row["id"] == "food")
            self.assertEqual((parent["direct_status"], parent["direct"],
                              parent["subtree_status"], parent["subtree"]),
                             ("CERTIFIED", "0", "NEEDS_REVIEW", None))
            self.assertEqual((food["direct_status"], food["direct"]),
                             ("NEEDS_REVIEW", None))
            self.assertEqual(october["metrics"]["income_supplement"]["report_total"], "0")


if __name__ == "__main__":
    unittest.main()

"""v6 classification claims are exact and tied to booked facts."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.classification import audit_classification


CUTOVER = date(2026, 10, 1)
HEADER = ('option "operating_currency" "CNY"\n'
          '2026-10-01 open Assets:Cash\n'
          '2026-10-01 open Expenses:Food\n'
          '2026-10-01 open Income:Rebate\n')
TWO_TRANSACTIONS = (
    '2026-10-02 * "shopping"\n'
    '  Expenses:Food 200 CNY\n'
    '  Assets:Cash -200 CNY\n'
    '2026-10-03 * "refund"\n'
    '  Expenses:Food -20 CNY\n'
    '  Assets:Cash 20 CNY\n')
CATEGORIES = [
    {"id": "food", "name": "生活费.餐饮", "parent": None, "aliases": ["餐饮"]},
    {"id": "travel", "name": "旅行", "parent": None, "aliases": []},
]


class ClassificationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main.bean"
        self.review = Path(self.temp.name) / "classification.yml"
        self.root.write_text(HEADER + TWO_TRANSACTIONS, encoding="utf-8")

    def initial(self):
        return audit_classification(self.root, CUTOVER)

    def declare(self, rows, categories=None):
        self.review.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "loader_version": BEANCOUNT_VERSION,
            "categories": CATEGORIES if categories is None else categories,
            "classifications": rows,
        }, allow_unicode=True), encoding="utf-8")
        return audit_classification(self.root, CUTOVER, self.review)

    def rows(self):
        refs = [row["ref"] for row in self.initial()["missing_classifications"]]
        return [
            {"claim": refs[0], "reason": "按购物项目审阅", "slices": [
                {"id": "shopping-food", "category_id": "food", "signed_units": "80"},
                {"id": "shopping-travel", "category_id": "travel", "signed_units": "120"}]},
            {"claim": refs[1], "reason": "原路退款", "slices": [
                {"id": "refund-food", "category_id": "food", "signed_units": "-20"}]},
        ]

    def test_split_and_negative_refund_are_exact(self):
        initial = self.initial()
        self.assertEqual(initial["review_state"], "pending")
        self.assertEqual(len(initial["missing_classifications"]), 2)
        result = self.declare(self.rows())
        self.assertEqual(result["review_state"], "complete")
        self.assertEqual(result["claimed_postings"], 2)
        self.assertEqual([s["signed_units"] for s in result["classifications"][0]["slices"]],
                         ["80", "120"])
        self.assertEqual(result["classifications"][1]["slices"][0]["signed_units"], "-20")

    def test_overallocation_duplicate_claim_and_stale_ref_reject(self):
        rows = self.rows()
        rows[0]["slices"][1]["signed_units"] = "121"
        with self.assertRaises(BudgetError) as caught:
            self.declare(rows)
        self.assertEqual(caught.exception.code, "CLASSIFICATION_UNITS_MISMATCH")

        rows = self.rows()
        rows.append(rows[0])
        with self.assertRaises(BudgetError) as caught:
            self.declare(rows)
        self.assertEqual(caught.exception.code, "CLASSIFICATION_DOUBLE_CLAIM")

        rows = self.rows()
        self.declare(rows)
        self.root.write_text(HEADER + TWO_TRANSACTIONS.replace("200 CNY", "201 CNY"),
                             encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_classification(self.root, CUTOVER, self.review)
        self.assertEqual(caught.exception.code, "CLAIM_REF_INVALID")

    def test_inline_and_external_cannot_claim_same_posting(self):
        self.root.write_text(HEADER + TWO_TRANSACTIONS.replace(
            '  Expenses:Food 200 CNY\n',
            '  Expenses:Food 200 CNY\n    budget: "餐饮"\n'), encoding="utf-8")
        first = self.declare([])
        self.assertEqual(first["claimed_postings"], 1)
        rows = [{"claim": first["missing_classifications"][0]["ref"],
                 "reason": "重复分类", "slices": [
                     {"id": "x", "category_id": "food", "signed_units": "-20"}]}]
        result = self.declare(rows)
        self.assertEqual(result["claimed_postings"], 2)
        inline_ref = result["classifications"][0]["ref"]
        rows.insert(0, {"claim": inline_ref, "reason": "重复行内", "slices": [
            {"id": "inline-duplicate", "category_id": "food", "signed_units": "200"}]})
        with self.assertRaises(BudgetError) as caught:
            self.declare(rows)
        self.assertEqual(caught.exception.code, "CLASSIFICATION_DOUBLE_CLAIM")

    def test_unrelated_insertion_does_not_invalidate_claim(self):
        rows = self.rows()
        self.declare(rows)
        unrelated = ('2026-10-01 * "unrelated"\n'
                     '  Assets:Cash 1 CNY\n'
                     '  Income:Rebate -1 CNY\n')
        self.root.write_text(HEADER + unrelated + TWO_TRANSACTIONS, encoding="utf-8")
        result = audit_classification(self.root, CUTOVER, self.review)
        self.assertEqual(result["claimed_postings"], 2)
        self.assertEqual(len(result["missing_classifications"]), 1)


if __name__ == "__main__":
    unittest.main()

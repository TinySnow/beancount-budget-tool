"""Virtual-ledger checks for explicit monthly consumption targets."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.targets import audit_monthly_expense_targets, load_expense_targets


CATEGORIES = [
    {"id": "living", "name": "生活费", "parent": None, "aliases": []},
    {"id": "food", "name": "生活费.餐饮", "parent": "living", "aliases": []},
]


class ExpenseTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.ledger = folder / "main.bean"
        self.classes = folder / "classes.yml"
        self.plans = folder / "plans.yml"
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Cash\n'
            '2026-09-01 open Expenses:Food\n'
            '2026-09-02 * "food"\n'
            '  Expenses:Food 10 CNY\n'
            '  Assets:Cash -10 CNY\n'
            '2026-09-03 * "refund"\n'
            '  Expenses:Food -10 CNY\n'
            '  Assets:Cash 10 CNY\n', encoding="utf-8")
        missing = audit_classification(self.ledger, date(2026, 9, 1))["missing_classifications"]
        self.classes.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "loader_version": BEANCOUNT_VERSION,
            "categories": CATEGORIES,
            "classifications": [
                {"claim": row["ref"], "reason": "virtual review", "slices": [
                    {"id": f"slice_{i}", "category_id": "food",
                     "signed_units": row["signed_units"]}]}
                for i, row in enumerate(missing)]}, allow_unicode=True), encoding="utf-8")

    def write_plan(self, text):
        self.plans.write_text(text, encoding="utf-8")

    def test_target_layers_and_zero_net_still_active(self):
        self.write_plan(
            'schema: 6\nreport_currency: CNY\n'
            'defaults:\n  expense_targets:\n    living: "100"\n    food: "30"\n'
            'effective_templates:\n  "2026-10":\n    expense_targets:\n      food: "40"\n'
            'months:\n  "2026-09":\n    expense_targets:\n      food: "35"\n')
        september = audit_monthly_expense_targets(
            self.ledger, date(2026, 9, 1), "2026-09", self.classes, self.plans, "CNY")
        self.assertEqual(september["status"], "CERTIFIED")
        rows = {row["id"]: row for row in september["categories"]}
        self.assertEqual((rows["food"]["active_direct"], rows["food"]["direct_spent"],
                          rows["food"]["direct_remaining"]), (True, "0", "35"))
        self.assertEqual((rows["food"]["direct_gross_spent"],
                          rows["food"]["direct_refunded"]), ("10", "10"))
        self.assertEqual((rows["living"]["direct_remaining"],
                          rows["living"]["subtree_target"],
                          rows["living"]["subtree_remaining"]), ("100", "135", "135"))
        october = load_expense_targets(self.plans, "2026-10", "CNY", CATEGORIES)
        november = load_expense_targets(self.plans, "2026-11", "CNY", CATEGORIES)
        self.assertEqual(october["targets"]["food"], "40")
        self.assertEqual(november["targets"]["food"], "40")

    def test_missing_active_target_never_becomes_zero(self):
        self.write_plan('schema: 6\nreport_currency: CNY\ndefaults:\n'
                        '  expense_targets:\n    living: "100"\n')
        report = audit_monthly_expense_targets(
            self.ledger, date(2026, 9, 1), "2026-09", self.classes, self.plans, "CNY")
        self.assertEqual((report["status"], report["missing_target_ids"]),
                         ("NEEDS_REVIEW", ["food"]))
        rows = {row["id"]: row for row in report["categories"]}
        self.assertIsNone(rows["food"]["direct_remaining"])
        self.assertIsNone(rows["living"]["subtree_remaining"])
        self.assertEqual(rows["living"]["direct_remaining"], "100")

    def test_parent_subtree_cap_covers_child_without_inventing_child_target(self):
        self.write_plan('schema: 6\nreport_currency: CNY\n'
                        'spendable_cap_category_id: living\nmonths:\n'
                        '  "2026-09":\n    expense_subtree_targets:\n      living: "2000"\n')
        report = audit_monthly_expense_targets(
            self.ledger, date(2026, 9, 1), "2026-09", self.classes, self.plans, "CNY")
        self.assertEqual((report["status"], report["missing_target_ids"]),
                         ("CERTIFIED", []))
        self.assertEqual(report["spendable_cap_category_id"], "living")
        rows = {row["id"]: row for row in report["categories"]}
        self.assertEqual((rows["living"]["direct_target"],
                          rows["living"]["direct_remaining_status"],
                          rows["living"]["subtree_target"],
                          rows["living"]["subtree_target_kind"],
                          rows["living"]["subtree_remaining"]),
                         (None, "NOT_APPLICABLE", "2000", "explicit", "2000"))
        self.assertEqual((rows["food"]["direct_remaining_status"],
                          rows["food"]["subtree_remaining_status"]),
                         ("NOT_APPLICABLE", "NOT_APPLICABLE"))

    def test_parent_and_child_subtree_caps_are_independent(self):
        self.write_plan('schema: 6\nreport_currency: CNY\ndefaults:\n'
                        '  expense_subtree_targets:\n    living: "2000"\n'
                        '    food: "100"\n')
        report = audit_monthly_expense_targets(
            self.ledger, date(2026, 9, 1), "2026-09", self.classes, self.plans, "CNY")
        rows = {row["id"]: row for row in report["categories"]}
        self.assertEqual(rows["living"]["subtree_target"], "2000")
        self.assertEqual(rows["food"]["subtree_target"], "100")

    def test_reject_ambiguous_or_invalid_plan(self):
        cases = [
            ('schema: 6\nreport_currency: CNY\nmonths:\n  2026-09:\n'
             '    expense_targets:\n      food: "3"\n', "PLAN_AMBIGUOUS"),
            ('schema: 6\nreport_currency: CNY\nmonths:\n  "2026-09": {}\n'
             '  "2026-09": {}\n', "YAML_DUPLICATE_KEY"),
            ('schema: 6\nreport_currency: CNY\nmonths:\n  "0000-09": {}\n',
             "PLAN_AMBIGUOUS"),
            ('schema: 6\nreport_currency: CNY\ndefaults:\n'
             '  expense_targets:\n    food: 3\n', "PLAN_AMOUNT_INVALID"),
            ('schema: 6\nreport_currency: CNY\ndefaults:\n'
             '  expense_targets:\n    food: null\n', "PLAN_AMOUNT_INVALID"),
            ('schema: 6\nreport_currency: CNY\ndefaults:\n'
             '  expense_targets:\n    other: "3"\n', "PLAN_CATEGORY_UNKNOWN"),
            ('schema: 6\nreport_currency: CNY\ndefaults:\n'
             '  expense_subtree_targets:\n    living: -1\n', "PLAN_AMOUNT_INVALID"),
            ('schema: 6\nreport_currency: CNY\nspendable_cap_category_id: unknown\n',
             "PLAN_CATEGORY_UNKNOWN"),
        ]
        for source, code in cases:
            with self.subTest(code=code):
                self.write_plan(source)
                with self.assertRaises(BudgetError) as caught:
                    load_expense_targets(self.plans, "2026-09", "CNY", CATEGORIES)
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()

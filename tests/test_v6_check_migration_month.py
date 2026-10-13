"""A month migration gate must preserve bookkeeping and other months."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_check_migration_month import check_month


BEFORE = '''option "operating_currency" "CNY"
2023-07-01 open Assets:Cash
2023-07-01 open Assets:Bank
2023-07-01 open Income:Salary
2023-07-01 open Expenses:Food

2023-07-05 * "salary"
  legacy_budget: "生活费"
  Assets:Cash 100 CNY
  Income:Salary -100 CNY

2023-08-02 * "lunch"
  budget: "旧交易级标签"
  Expenses:Food 10 CNY
  Assets:Cash -10 CNY
'''

AFTER = BEFORE.replace(
    '  legacy_budget: "生活费"\n  Assets:Cash 100 CNY\n  Income:Salary -100 CNY',
    '  Assets:Cash 100 CNY\n    fund: "-"\n  Income:Salary -100 CNY\n    budget: "生活费"')


class MonthlyMigrationGateTests(unittest.TestCase):
    def ledgers(self, base: Path, after: str = AFTER) -> tuple[Path, Path]:
        old = base / "before" / "main.bean"
        new = base / "after" / "main.bean"
        old.parent.mkdir()
        new.parent.mkdir()
        old.write_text(BEFORE, encoding="utf-8")
        new.write_text(after, encoding="utf-8")
        return old, new

    def test_one_month_passes_without_requiring_later_month_migration(self):
        with TemporaryDirectory() as folder:
            old, new = self.ledgers(Path(folder))
            result = check_month(old, new, "2023-07")
            self.assertTrue(result["financial_facts_equal"])
            self.assertTrue(result["outside_month_metadata_equal"])
            self.assertTrue(result["outside_month_source_equal"])
            self.assertEqual(result["month_source_audit"]["transactions"], 1)
            self.assertEqual(result["budget_semantics"], "NOT_AUDITED")

    def test_same_ledger_cannot_pass_as_a_comparison(self):
        with TemporaryDirectory() as folder:
            old, _ = self.ledgers(Path(folder))
            with self.assertRaises(BudgetError) as error:
                check_month(old, old, "2023-07")
            self.assertEqual(error.exception.code, "MIGRATION_SAME_INPUT")

    def test_booked_amount_change_fails(self):
        with TemporaryDirectory() as folder:
            old, new = self.ledgers(Path(folder), AFTER.replace("100 CNY", "101 CNY", 1))
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07")
            self.assertEqual(error.exception.code, "LOADER_ERROR")

    def test_balanced_booked_change_fails(self):
        with TemporaryDirectory() as folder:
            old, new = self.ledgers(Path(folder), AFTER.replace("100 CNY", "101 CNY"))
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07")
            self.assertEqual(error.exception.code, "MIGRATION_FINANCIAL_CHANGE")

    def test_explicit_correction_lists_both_booked_versions_and_stays_in_month(self):
        with TemporaryDirectory() as folder:
            changed = AFTER.replace("Assets:Cash 100 CNY", "Assets:Bank 100 CNY")
            old, new = self.ledgers(Path(folder), changed)
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07")
            self.assertEqual(error.exception.code, "MIGRATION_FINANCIAL_CHANGE")
            result = check_month(old, new, "2023-07", allow_financial_corrections=True)
            self.assertFalse(result["financial_facts_equal"])
            self.assertTrue(result["outside_month_source_equal"])
            self.assertEqual(len(result["financial_changes"]), 1)
            correction = result["financial_changes"][0]
            self.assertIn("Assets:Cash", correction["before"][0]["booked_fact"])
            self.assertIn("Assets:Bank", correction["after"][0]["booked_fact"])
            self.assertIn("main.bean:", correction["after"][0]["source"])

    def test_explicit_correction_still_rejects_other_month_changes(self):
        with TemporaryDirectory() as folder:
            changed = AFTER.replace("Assets:Cash -10 CNY", "Assets:Bank -10 CNY")
            old, new = self.ledgers(Path(folder), changed)
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07", allow_financial_corrections=True)
            self.assertEqual(error.exception.code, "MIGRATION_OUTSIDE_MONTH_SOURCE_CHANGE")

    def test_other_month_metadata_change_fails(self):
        with TemporaryDirectory() as folder:
            old, new = self.ledgers(Path(folder), AFTER.replace("旧交易级标签", "新标签"))
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07")
            self.assertEqual(error.exception.code, "MIGRATION_OUTSIDE_MONTH_SOURCE_CHANGE")

    def test_other_month_comment_change_fails(self):
        with TemporaryDirectory() as folder:
            old, new = self.ledgers(Path(folder), AFTER.replace(
                '2023-08-02 * "lunch"', '; new note\n2023-08-02 * "lunch"'))
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07")
            self.assertEqual(error.exception.code, "MIGRATION_OUTSIDE_MONTH_SOURCE_CHANGE")

    def test_loader_option_change_fails(self):
        with TemporaryDirectory() as folder:
            old, new = self.ledgers(Path(folder), AFTER.replace(
                '"operating_currency" "CNY"', '"operating_currency" "USD"'))
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07")
            self.assertEqual(error.exception.code, "MIGRATION_OPTIONS_CHANGE")

    def test_selected_month_illegal_tag_fails(self):
        with TemporaryDirectory() as folder:
            old, new = self.ledgers(Path(folder), AFTER.replace(
                '  Assets:Cash 100 CNY\n', '  budget: "交易级非法"\n  Assets:Cash 100 CNY\n'))
            with self.assertRaises(BudgetError) as error:
                check_month(old, new, "2023-07")
            self.assertEqual(error.exception.code, "TAG_LEVEL_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()

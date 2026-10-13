"""The migration review copy retains booked financial facts."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.tools.v6_make_review_copy import make_copy
from beancount_budget_tool.tools.v6_classification_proposals import propose
from beancount_budget_tool.v6.source import scan_v6_source
from datetime import date


class ReviewCopyTests(unittest.TestCase):
    def test_legacy_transaction_tags_are_retained_without_changing_postings(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "source" / "main.bean"
            root.parent.mkdir()
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-02 * "shopping"\n'
                '  budget: "旧费用分类"\n'
                '  fund: "旧用途提示"\n'
                '  Expenses:Food 10 CNY\n'
                '    budget: "生活费.餐饮"\n'
                '  Assets:Cash -10 CNY\n'
                '    fund: "-"\n', encoding="utf-8")
            dest = base / "copy"
            result = make_copy(root, dest)
            self.assertTrue(result["financial_facts_equal"])
            self.assertEqual(result["renamed_transaction_tags"], 2)
            copied = (dest / "main.bean").read_text(encoding="utf-8")
            self.assertIn('legacy_budget: "旧费用分类"', copied)
            self.assertIn('legacy_fund: "旧用途提示"', copied)
            self.assertIn('    budget: "生活费.餐饮"', copied)
            self.assertIn('    fund: "-"', copied)
            scan_v6_source(dest / "main.bean", date(2026, 10, 1))
            with self.assertRaises(FileExistsError):
                make_copy(root, dest)

    def test_legacy_hint_stays_a_proposal(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "source" / "main.bean"
            root.parent.mkdir()
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-02 * "shopping"\n'
                '  budget: "生活费"\n'
                '  Expenses:Food 10.50 CNY\n'
                '  Assets:Cash -10.50 CNY\n', encoding="utf-8")
            dest = base / "copy"
            make_copy(root, dest)
            result = propose(dest / "main.bean", date(2026, 10, 1))
            self.assertEqual(result["groups"][0]["count"], 1)
            self.assertEqual(result["groups"][0]["legacy_budget_hints"], {"生活费": 1})
            self.assertEqual(result["groups"][0]["candidates"][0]["signed_units"], "21/2")

    def test_monthly_proposals_include_only_unlabeled_postings_in_selected_month(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2023-07-01 open Assets:Cash\n'
                '2023-07-01 open Expenses:Food\n'
                '2023-07-02 * "July missing"\n'
                '  Expenses:Food 10 CNY\n'
                '  Assets:Cash -10 CNY\n'
                '2023-07-03 * "July reviewed"\n'
                '  Expenses:Food 2 CNY\n'
                '    budget: "生活费.餐饮"\n'
                '  Assets:Cash -2 CNY\n'
                '2023-08-02 * "August missing"\n'
                '  Expenses:Food 3 CNY\n'
                '  Assets:Cash -3 CNY\n', encoding="utf-8")
            july = propose(root, date(2023, 7, 1), date(2023, 7, 31))
            self.assertEqual(july["through"], "2023-07-31")
            self.assertEqual(july["groups"][0]["count"], 1)
            self.assertEqual(july["groups"][0]["candidates"][0]["narration"], "July missing")
            self.assertEqual(propose(root, date(2023, 8, 1), date(2023, 8, 31))
                             ["groups"][0]["candidates"][0]["narration"], "August missing")


if __name__ == "__main__":
    unittest.main()

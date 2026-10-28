"""v6 源事实门槛：官方 loader 会接受的一些标签，预算器仍须拒绝。"""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.source import scan_v6_source


HEADER = (
    'option "operating_currency" "CNY"\n'
    '2026-10-01 open Assets:Cash\n'
    '2026-10-01 open Expenses:Food\n'
)


class SourceGateTests(unittest.TestCase):
    def scan(self, transaction):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "main.bean"
            root.write_text(HEADER + transaction, encoding="utf-8")
            return scan_v6_source(root, date(2026, 10, 1))

    def test_valid_posting_level_tags(self):
        result = self.scan(
            '2026-10-02 * "餐饮"\n'
            '  tracking: "活动"\n'
            '  Expenses:Food 10 CNY\n'
            '    budget: "生活费.餐饮"\n'
            '  Assets:Cash -10 CNY\n'
            '    fund: "-"\n')
        self.assertEqual((result.transactions, result.postings), (1, 2))
        self.assertEqual(result.missing_inline_classification, 0)
        self.assertEqual(result.missing_inline_asset_intent, 0)
        self.assertEqual(result.source_files, 1)

    def test_transaction_level_budget_rejected_even_if_loader_accepts_it(self):
        with self.assertRaises(BudgetError) as caught:
            self.scan('2026-10-02 * "餐饮"\n'
                      '  budget: "生活费"\n'
                      '  Expenses:Food 10 CNY\n'
                      '  Assets:Cash -10 CNY\n')
        self.assertEqual(caught.exception.code, "TAG_LEVEL_FORBIDDEN")

    def test_fund_initial_and_multiple_delimiters_rejected(self):
        for label in ("储蓄|期初", "储蓄|划拨|损益", "-|划拨"):
            with self.subTest(label=label), self.assertRaises(BudgetError) as caught:
                self.scan('2026-10-02 * "餐饮"\n'
                          '  Expenses:Food 10 CNY\n'
                          '    budget: "餐饮"\n'
                          '  Assets:Cash -10 CNY\n'
                          f'    fund: "{label}"\n')
            self.assertEqual(caught.exception.code, "FUND_GRAMMAR")

    def test_missing_inline_tags_are_inventory_not_auto_approval(self):
        result = self.scan('2026-10-02 * "餐饮"\n'
                           '  Expenses:Food 10 CNY\n'
                           '  Assets:Cash -10 CNY\n')
        self.assertEqual(result.missing_inline_classification, 1)
        self.assertEqual(result.missing_inline_asset_intent, 1)

    def test_tracking_fullwidth_delimiter_rejected(self):
        with self.assertRaises(BudgetError) as caught:
            self.scan('2026-10-02 * "餐饮"\n'
                      '  tracking: "活动，交通"\n'
                      '  Expenses:Food 10 CNY\n'
                      '  Assets:Cash -10 CNY\n')
        self.assertEqual(caught.exception.code, "TRACKING_INVALID")


if __name__ == "__main__":
    unittest.main()

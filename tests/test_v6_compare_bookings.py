"""跨 loader 只读比较只在完全相同源输入上给出一致结论。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_compare_bookings import booking_snapshot, compare_snapshots


class BookingCompareTests(unittest.TestCase):
    def test_snapshot_and_equal_compare(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-02 * "food"\n'
                '  Expenses:Food 10 CNY\n'
                '    budget: "餐饮"\n'
                '  Assets:Cash -10 CNY\n', encoding="utf-8")
            first = booking_snapshot(ledger)
            second = booking_snapshot(ledger)
            result = compare_snapshots(first, second)
            self.assertTrue(result["booked_identical"])
            self.assertEqual(result["difference_count"], 0)
            self.assertEqual(first["sources"], second["sources"])

    def test_changed_booking_reports_difference_without_certifying(self):
        left = {"schema": 1, "root": "/same/main.bean", "sources": {"main.bean": "abc"},
                "loader_version": "2.3.6", "entries": [{"kind": "Transaction", "fields": {"amount": "10"}}]}
        right = {**left, "loader_version": "3.2.3",
                 "entries": [{"kind": "Transaction", "fields": {"amount": "11"}}]}
        result = compare_snapshots(left, right)
        self.assertFalse(result["booked_identical"])
        self.assertEqual(result["difference_count"], 1)
        self.assertNotEqual(result["left_booked_sha256"], result["right_booked_sha256"])

    def test_source_change_is_not_booking_equivalence(self):
        left = {"schema": 1, "root": "/same/main.bean", "sources": {"main.bean": "abc"},
                "loader_version": "2.3.6", "entries": []}
        right = {**left, "sources": {"main.bean": "def"}, "loader_version": "3.2.3"}
        with self.assertRaises(BudgetError) as caught:
            compare_snapshots(left, right)
        self.assertEqual(caught.exception.code, "BOOKING_COMPARE_SOURCE_MISMATCH")


if __name__ == "__main__":
    unittest.main()

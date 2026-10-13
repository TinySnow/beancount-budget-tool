"""A shared report load stays frozen and rejects source changes before output."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import frozen_financial, load_financial
from beancount_budget_tool.v6.facts import FactIndex, frozen_fact_indices


class FrozenFinancialTests(unittest.TestCase):
    def test_shared_source_and_end_of_report_validation(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text('2026-09-01 open Assets:Bank\n', encoding="utf-8")
            with frozen_financial(ledger) as first:
                self.assertIs(load_financial(ledger), first)
                self.assertIs(load_financial(ledger), first)
                with frozen_fact_indices():
                    self.assertIs(FactIndex(first), FactIndex(first))
                self.assertIsNot(FactIndex(first), FactIndex(first))
            with self.assertRaises(BudgetError) as caught:
                with frozen_financial(ledger) as frozen:
                    ledger.write_text(ledger.read_text(encoding="utf-8") +
                                      '2026-10-01 open Expenses:Food\n', encoding="utf-8")
                    self.assertIs(load_financial(ledger), frozen)
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")
            self.assertNotEqual(load_financial(ledger).digest, first.digest)


if __name__ == "__main__":
    unittest.main()

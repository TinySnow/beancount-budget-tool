"""A shared report load stays frozen and rejects source changes before output."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import date
from unittest.mock import patch

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, frozen_financial, load_financial, reference_financial
from beancount_budget_tool.v6.facts import FactIndex, frozen_fact_indices


class FrozenFinancialTests(unittest.TestCase):
    def test_historical_scope_ignores_future_booked_entries_but_keeps_source(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text('2023-07-01 open Assets:Cash\n'
                              '2023-08-01 open Assets:Future\n', encoding="utf-8")
            complete = load_financial(ledger)
            with booked_through(date(2023, 7, 31)):
                with frozen_financial(ledger) as bounded:
                    self.assertEqual(len(bounded.entries), 1)
                    self.assertEqual(bounded.digest, complete.digest)
                    self.assertIs(load_financial(ledger), bounded)
                    reference = reference_financial(bounded)
                    self.assertEqual(reference.entries, complete.entries)
                    self.assertEqual(reference.digest, bounded.digest)
                    self.assertEqual(len(load_financial(ledger).entries), 1)
            self.assertEqual(len(load_financial(ledger).entries), 2)

    def test_loader_does_not_consult_existing_pickle_cache(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text('2026-09-01 open Assets:Bank\n', encoding="utf-8")
            with patch("beancount_budget_tool.financial.loader.load_file",
                       side_effect=AssertionError("cached loader used")):
                self.assertEqual(len(load_financial(ledger).entries), 1)

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

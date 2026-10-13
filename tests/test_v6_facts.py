"""v6 source identities survive ordinary source edits and include pad facts."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount.core import data

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.facts import FactIndex, GeneratedPostingRef


HEADER = (
    'option "operating_currency" "CNY"\n'
    '2026-01-01 open Assets:Cash\n'
    '2026-01-01 open Equity:Opening\n'
    '2026-01-01 pad Assets:Cash Equity:Opening\n'
    '2026-01-02 balance Assets:Cash 100 CNY\n'
)
TX = (
    '2026-01-03 * "duplicate"\n'
    '  Assets:Cash -10 CNY\n'
    '  Equity:Opening 10 CNY\n'
)


class FactIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main.bean"

    def index(self, content):
        self.root.write_text(content, encoding="utf-8")
        fin = load_financial(self.root)
        return fin, FactIndex(fin)

    def test_pad_and_identical_raw_transactions_have_distinct_stable_refs(self):
        fin, index = self.index(HEADER + TX + TX)
        transactions = [e for e in fin.entries if isinstance(e, data.Transaction)]
        self.assertEqual((index.generated_transactions, index.generated_postings), (1, 2))
        pad, first, second = transactions
        pad_ref = index.for_posting(pad.postings[0])
        self.assertIsInstance(pad_ref, GeneratedPostingRef)
        self.assertEqual(pad_ref.transaction.generated_kind, "pad")
        self.assertEqual(pad_ref.transaction.loader_version, fin.loader_version)
        self.assertEqual(pad_ref.lot, "CNY/no-cost")
        self.assertEqual(index.for_transaction(first).occurrence, 1)
        self.assertEqual(index.for_transaction(second).occurrence, 2)
        self.assertNotEqual(index.for_posting(first.postings[0]),
                            index.for_posting(second.postings[0]))

    def test_unrelated_transaction_does_not_invalidate_existing_fact(self):
        _, before = self.index(HEADER + TX)
        target = next(ref for ref in before.posting_refs.values()
                      if not isinstance(ref, GeneratedPostingRef))
        extra = ('2026-01-04 * "other"\n'
                 '  Assets:Cash -1 CNY\n'
                 '  Equity:Opening 1 CNY\n')
        _, after = self.index(HEADER + TX + extra)
        self.assertIn(target, after.posting_refs.values())

    def test_changing_booked_amount_changes_fact_reference(self):
        _, before = self.index(HEADER + TX)
        first = next(ref for ref in before.posting_refs.values()
                     if not isinstance(ref, GeneratedPostingRef))
        changed = TX.replace("-10 CNY", "-11 CNY").replace(" 10 CNY", " 11 CNY")
        _, after = self.index(HEADER + changed)
        self.assertNotIn(first, after.posting_refs.values())


if __name__ == "__main__":
    unittest.main()

"""v6 source identities survive ordinary source edits and include pad facts."""

from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount.core import data

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial, financial_session, frozen_financial, booked_through
from beancount_budget_tool.refs import ReferenceIndex
from beancount_budget_tool.v6.facts import FactIndex, GeneratedPostingRef, reference_fact_index, frozen_fact_indices


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

    def test_bounded_references_and_pad_ordinals_match_the_independent_index(self):
        future = TX.replace("2026-01-03", "2026-02-03")
        self.root.write_text(HEADER + TX + TX + future, encoding="utf-8")
        with financial_session(), frozen_fact_indices(), booked_through(date(2026, 1, 31)), frozen_financial(self.root) as fin:
            scoped = FactIndex(fin)
            independent = ReferenceIndex(fin)
            complete = reference_fact_index(fin)
            self.assertEqual(scoped.generated_transactions, 1)
            for entry in fin.entries:
                if not isinstance(entry, data.Transaction):
                    continue
                for posting in entry.postings:
                    ref = scoped.for_posting(posting)
                    self.assertEqual(ref, complete.for_posting(posting))
                    if not isinstance(ref, GeneratedPostingRef):
                        self.assertEqual(ref, independent.for_posting(posting))

    def test_unsupported_generated_future_fact_does_not_poison_a_past_index(self):
        fin, _ = self.index(HEADER + TX)
        original = next(entry for entry in fin.entries if isinstance(entry, data.Transaction) and entry.flag != "P")
        future = original._replace(meta={}, date=date(2026, 2, 1),
                                   postings=[posting._replace(meta={}) for posting in original.postings])
        complete = replace(fin, entries=(*fin.entries, future))
        past = replace(complete, entries=fin.entries, reference_entries=complete.entries)
        index = FactIndex(past)
        self.assertEqual(index.for_posting(original.postings[0]), FactIndex(fin).for_posting(original.postings[0]))
        with self.assertRaises(BudgetError) as error:
            reference_fact_index(past)
        self.assertEqual(error.exception.code, "GENERATED_REF_UNSUPPORTED")

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

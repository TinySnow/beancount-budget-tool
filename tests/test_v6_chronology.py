"""User-declared cash order works across importer files without changing facts."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import loader
from beancount.core import data
import yaml

from beancount_budget_tool.chronology import order_entries
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial, booked_through, reference_financial
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.v6.tui import _review_reason


class CashChronologyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'main.bean'
        self.root.write_text('option "operating_currency" "CNY"\n'
                             '2023-07-01 open Assets:Bank\n2023-07-01 open Assets:Wallet\n'
                             '2023-07-01 open Income:Pay\n2023-07-01 open Expenses:Food\n'
                             '2023-08-01 * "opening bank"\n  Assets:Bank 1000 CNY\n  Income:Pay -1000 CNY\n'
                             '2023-08-02 * "opening wallet"\n  Assets:Wallet 46.32 CNY\n  Income:Pay -46.32 CNY\n'
                             'include "payment.bean"\ninclude "recharge.bean"\n', encoding='utf-8')
        self.payment = self.base / 'payment.bean'
        self.payment.write_text('2023-08-24 * "payment"\n  Expenses:Food 199 CNY\n  Assets:Wallet -199 CNY\n', encoding='utf-8')
        self.recharge = self.base / 'recharge.bean'
        self.recharge.write_text('\n' * 50 + '2023-08-24 * "recharge"\n  Assets:Bank -200 CNY\n  Assets:Wallet 200 CNY\n', encoding='utf-8')
        self.config = self.base / 'config.yml'
        config = {'schema': 1, 'ledger': 'main.bean', 'budgets': 'budgets.yml',
                  'cutover': '2023-07-01', 'month': '2023-08', 'report_currency': 'CNY',
                  'defaults': {'income_purpose': '生活费', 'expense_purpose': '生活费', 'expense_category': '生活开销'},
                  'accounts': {name: {'semantics': 'H', 'payment_scope': {'CNY': 'payable'}}
                               for name in ('Assets:Bank', 'Assets:Wallet')}}
        self.config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
        (self.base / 'budgets.yml').write_text('schema: 1\ndefaults:\n  expense_targets:\n    生活开销: 2000\nmonths:\n  "2023-08":\n    completeness_confirmed: true\n', encoding='utf-8')

    def mark(self, path=None, value='-1'):
        path = path or self.recharge
        text = path.read_text(encoding='utf-8')
        path.write_text(text.replace('* "recharge"\n', f'* "recharge"\n  budget-order: {value}\n'), encoding='utf-8')

    def txs(self, fin):
        return [entry for entry in fin.entries if isinstance(entry, data.Transaction)]

    def test_cross_file_payment_199_recharge_200_certifies_only_with_declared_order(self):
        before = audit_simple(self.config)
        self.assertEqual(before['status'], 'NEEDS_REVIEW')
        negative = next(row for row in before['review_items'] if row['reason_code'] == 'H_NEGATIVE_INVENTORY')
        self.assertIn('-3817/25', negative['detail'])  # -152.68
        self.assertIn('真实余额为 -152.68 CNY', _review_reason(negative))
        self.mark()
        originals = {path: path.read_bytes() for path in (self.root, self.payment, self.recharge, self.config)}
        after = audit_simple(self.config, include_transactions=True)
        self.assertEqual(after['status'], 'CERTIFIED_SCOPED', after['review_items'])
        rows = after['report']['transactions']['rows']
        self.assertLess(next(i for i,row in enumerate(rows) if row['narration']=='recharge'),
                        next(i for i,row in enumerate(rows) if row['narration']=='payment'))
        row = next(row for row in rows if row['narration']=='recharge')
        self.assertEqual(row['budget_order'], -1)
        self.assertTrue(any('显式同日顺序' in line for line in render_transaction(row, {})))
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_native_line_numbers_explain_failure_and_no_marker_keeps_native_order(self):
        entries, errors, _ = loader.load_file(str(self.root))
        self.assertFalse(errors)
        self.assertEqual(self.txs(load_financial(self.root)), [entry for entry in entries if isinstance(entry, data.Transaction)])
        same_day = [entry.narration for entry in entries if isinstance(entry, data.Transaction) and entry.date==date(2023,8,24)]
        self.assertEqual(same_day, ['payment', 'recharge'])

    def test_order_does_not_depend_on_file_names_include_order_or_line_numbers(self):
        self.mark()
        self.recharge.write_text('\n' * 200 + self.recharge.read_text(encoding='utf-8'), encoding='utf-8')
        self.root.write_text(self.root.read_text(encoding='utf-8').replace('include "payment.bean"\ninclude "recharge.bean"',
                             'include "recharge.bean"\ninclude "payment.bean"'), encoding='utf-8')
        self.assertEqual(audit_simple(self.config)['status'], 'CERTIFIED_SCOPED')

    def test_order_never_moves_funding_back_across_a_day(self):
        self.mark()
        self.recharge.write_text(self.recharge.read_text(encoding='utf-8').replace('2023-08-24','2023-08-25'), encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertTrue(any(row['reason_code']=='H_NEGATIVE_INVENTORY' for row in result['review_items']))

    def test_day_end_shortfall_is_still_rejected(self):
        self.mark()
        self.recharge.write_text(self.recharge.read_text(encoding='utf-8').replace('200 CNY','100 CNY'), encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertTrue(any(row['reason_code']=='H_NEGATIVE_INVENTORY' for row in result['review_items']))

    def test_booked_facts_and_source_refs_remain_exact(self):
        self.mark()
        official, errors, _ = loader.load_file(str(self.root))
        self.assertFalse(errors)
        financial = load_financial(self.root)
        key = lambda entry: (entry.meta['filename'],entry.meta['lineno'])
        self.assertEqual({key(entry):entry for entry in official}, {key(entry):entry for entry in financial.entries})
        facts = FactIndex(financial)
        for entry in self.txs(financial):
            for posting in entry.postings:
                self.assertEqual(facts.for_posting(posting).path, Path(entry.meta['filename']).name)

    def test_bounded_view_and_reference_view_share_declared_chronology(self):
        self.mark()
        self.recharge.write_text(self.recharge.read_text(encoding='utf-8') +
                                 '\n2023-09-01 * "future"\n  Assets:Bank 1 CNY\n  Income:Pay -1 CNY\n', encoding='utf-8')
        with booked_through(date(2023,8,31)):
            financial = load_financial(self.root)
        self.assertEqual([row.narration for row in self.txs(financial)][-2:], ['recharge','payment'])
        self.assertEqual(self.txs(reference_financial(financial))[-1].narration,'future')

    def test_invalid_order_values_have_source_locations(self):
        initial = self.recharge.read_text(encoding='utf-8')
        for value in ('"-1"', '-0.5', 'TRUE', 'NULL'):
            with self.subTest(value=value):
                self.recharge.write_text(initial, encoding='utf-8')
                self.mark(value=value)
                with self.assertRaises(BudgetError) as caught:
                    load_financial(self.root)
                self.assertEqual(caught.exception.code,'TRANSACTION_ORDER_INVALID')
                self.assertIn('recharge.bean:', caught.exception.source)

    def test_posting_level_and_nontransaction_order_are_rejected(self):
        for path,text in ((self.recharge, self.recharge.read_text(encoding='utf-8').replace('  Assets:Bank -200 CNY',
                          '  Assets:Bank -200 CNY\n    budget-order: -1')),
                          (self.root,self.root.read_text(encoding='utf-8').replace('2023-07-01 open Assets:Bank',
                           '2023-07-01 open Assets:Bank\n  budget-order: -1'))):
            old=path.read_bytes()
            path.write_text(text,encoding='utf-8')
            with self.assertRaises(BudgetError) as caught:
                load_financial(self.root)
            self.assertEqual(caught.exception.code,'TRANSACTION_ORDER_INVALID')
            path.write_bytes(old)

    def test_cost_or_pad_transactions_cannot_be_reordered_or_crossed(self):
        self.mark()
        financial = load_financial(self.root)
        txs=self.txs(financial)[-2:]
        marker, payment = txs
        shared_cost = payment._replace(postings=[
            payment.postings[0]._replace(cost=object()), payment.postings[1]])
        disjoint_cost = payment._replace(postings=[
            payment.postings[0]._replace(account="Assets:Fund", cost=object()),
            payment.postings[1]._replace(account="Assets:BrokerCash")])
        for label, protected in (("shared_cost", shared_cost),
                                 ("disjoint_pad", disjoint_cost._replace(flag="P"))):
            with self.subTest(label=label):
                with self.assertRaises(BudgetError) as caught:
                    order_entries([protected, marker])
                self.assertEqual(caught.exception.code,'TRANSACTION_ORDER_UNSUPPORTED')
        with self.assertRaises(BudgetError):
            marked = shared_cost._replace(meta={**shared_cost.meta,'budget-order':Decimal(-2)})
            order_entries([marked,marker])

    def test_disjoint_cost_transaction_can_be_crossed_by_declared_order(self):
        self.mark()
        financial = load_financial(self.root)
        marker, payment = self.txs(financial)[-2:]
        disjoint_cost = payment._replace(postings=[
            payment.postings[0]._replace(account="Assets:Fund", cost=object()),
            payment.postings[1]._replace(account="Assets:BrokerCash")])
        ordered = order_entries([disjoint_cost, marker])
        self.assertEqual([entry.narration for entry in ordered], ["recharge", "payment"])

    def test_cost_transaction_sharing_cash_account_still_blocks_crossing(self):
        self.mark()
        financial = load_financial(self.root)
        marker, payment = self.txs(financial)[-2:]
        shared_cash = payment._replace(postings=[
            payment.postings[0]._replace(account="Assets:Fund", cost=object()),
            payment.postings[1]._replace(account="Assets:Bank")])
        with self.assertRaises(BudgetError) as caught:
            order_entries([shared_cash, marker])
        self.assertEqual(caught.exception.code, "TRANSACTION_ORDER_UNSUPPORTED")

    def _txn(self, payment, narration, *, order=None, postings=None, flag="*"):
        meta = dict(payment.meta)
        if order is not None:
            meta["budget-order"] = Decimal(order)
        return payment._replace(narration=narration, meta=meta,
                                postings=postings or payment.postings, flag=flag)

    def test_relative_order_flip_detects_shared_cost_after_positions_shift(self):
        self.mark()
        financial = load_financial(self.root)
        _, payment = self.txs(financial)[-2:]
        cost = self._txn(payment, "B", postings=[
            payment.postings[0]._replace(account="Assets:Shared", cost=object()),
            payment.postings[1]._replace(account="Assets:B2")])
        shared = self._txn(payment, "X", order=-1, postings=[
            payment.postings[0]._replace(account="Assets:Shared"),
            payment.postings[1]._replace(account="Assets:X2")])
        u = self._txn(payment, "U", order=-3, postings=[
            payment.postings[0]._replace(account="Assets:OtherU"),
            payment.postings[1]._replace(account="Assets:U2")])
        v = self._txn(payment, "V", order=-2, postings=[
            payment.postings[0]._replace(account="Assets:OtherV"),
            payment.postings[1]._replace(account="Assets:V2")])
        with self.assertRaises(BudgetError) as caught:
            order_entries([u, cost, v, shared])
        self.assertEqual(caught.exception.code, "TRANSACTION_ORDER_UNSUPPORTED")

    def test_relative_order_flip_allows_disjoint_cost_after_positions_shift(self):
        self.mark()
        financial = load_financial(self.root)
        _, payment = self.txs(financial)[-2:]
        cost = self._txn(payment, "cost", postings=[
            payment.postings[0]._replace(account="Assets:Fund", cost=object()),
            payment.postings[1]._replace(account="Assets:BrokerCash")])
        disjoint = self._txn(payment, "disjoint", order=-1, postings=[
            payment.postings[0]._replace(account="Assets:Alpha"),
            payment.postings[1]._replace(account="Assets:Beta")])
        u = self._txn(payment, "u", order=-3)
        v = self._txn(payment, "v", order=-2)
        ordered = order_entries([u, cost, v, disjoint])
        self.assertEqual([entry.narration for entry in ordered], ["u", "v", "disjoint", "cost"])

    def test_indirectly_shifted_cost_does_not_block_itself(self):
        self.mark()
        financial = load_financial(self.root)
        _, payment = self.txs(financial)[-2:]
        cost = self._txn(payment, "cost", postings=[
            payment.postings[0]._replace(account="Assets:Fund", cost=object()),
            payment.postings[1]._replace(account="Assets:BrokerCash")])
        u = self._txn(payment, "u", order=-3)
        v = self._txn(payment, "v", order=-2)
        ordered = order_entries([u, cost, v])
        self.assertEqual([entry.narration for entry in ordered], ["u", "v", "cost"])

    def test_relative_flip_is_checked_in_both_directions(self):
        self.mark()
        financial = load_financial(self.root)
        _, payment = self.txs(financial)[-2:]
        cost = self._txn(payment, "cost", postings=[
            payment.postings[0]._replace(account="Assets:Fund", cost=object()),
            payment.postings[1]._replace(account="Assets:BrokerCash")])
        shared = self._txn(payment, "shared", order=-1, postings=[
            payment.postings[0]._replace(account="Assets:Fund"),
            payment.postings[1]._replace(account="Assets:Other")])
        for marker_order in (-1, 1):
            with self.subTest(marker_order=marker_order):
                marker = self._txn(payment, "marker", order=marker_order,
                                   postings=shared.postings)
                original = [cost, marker] if marker_order == -1 else [marker, cost]
                with self.assertRaises(BudgetError):
                    order_entries(original)

    def test_multiple_markers_and_barriers_use_pairwise_flips(self):
        self.mark()
        financial = load_financial(self.root)
        _, payment = self.txs(financial)[-2:]
        b1 = self._txn(payment, "b1", postings=[
            payment.postings[0]._replace(account="Assets:Alpha", cost=object()),
            payment.postings[1]._replace(account="Assets:Beta")])
        b2 = self._txn(payment, "b2", postings=[
            payment.postings[0]._replace(account="Assets:Fund", cost=object()),
            payment.postings[1]._replace(account="Assets:BrokerCash")])
        x = self._txn(payment, "x", order=-1, postings=[
            payment.postings[0]._replace(account="Assets:Fund"),
            payment.postings[1]._replace(account="Assets:Other")])
        u = self._txn(payment, "u", order=-4)
        v = self._txn(payment, "v", order=-3)
        with self.assertRaises(BudgetError) as caught:
            order_entries([u, b1, v, b2, x])
        self.assertEqual(caught.exception.code, "TRANSACTION_ORDER_UNSUPPORTED")

    def test_barrier_can_cross_an_entry_whose_absolute_index_stays(self):
        self.mark()
        financial = load_financial(self.root)
        _, payment = self.txs(financial)[-2:]
        cost = self._txn(payment, "B", postings=[
            payment.postings[0]._replace(account="Assets:Shared", cost=object()),
            payment.postings[1]._replace(account="Assets:B2")])
        shared = self._txn(payment, "X", order=1, postings=[
            payment.postings[0]._replace(account="Assets:Shared"),
            payment.postings[1]._replace(account="Assets:X2")])
        u = self._txn(payment, "U", order=2, postings=[
            payment.postings[0]._replace(account="Assets:OtherU"),
            payment.postings[1]._replace(account="Assets:U2")])
        v = self._txn(payment, "V", order=3, postings=[
            payment.postings[0]._replace(account="Assets:OtherV"),
            payment.postings[1]._replace(account="Assets:V2")])
        with self.assertRaises(BudgetError) as caught:
            order_entries([u, shared, cost, v])
        self.assertEqual(caught.exception.code, "TRANSACTION_ORDER_UNSUPPORTED")

    def test_barrier_can_cross_an_unchanged_index_when_resources_are_disjoint(self):
        self.mark()
        financial = load_financial(self.root)
        _, payment = self.txs(financial)[-2:]
        cost = self._txn(payment, "B", postings=[
            payment.postings[0]._replace(account="Assets:Shared", cost=object()),
            payment.postings[1]._replace(account="Assets:B2")])
        disjoint = self._txn(payment, "X", order=1, postings=[
            payment.postings[0]._replace(account="Assets:OtherX"),
            payment.postings[1]._replace(account="Assets:X2")])
        u = self._txn(payment, "U", order=2, postings=[
            payment.postings[0]._replace(account="Assets:OtherU"),
            payment.postings[1]._replace(account="Assets:U2")])
        v = self._txn(payment, "V", order=3, postings=[
            payment.postings[0]._replace(account="Assets:OtherV"),
            payment.postings[1]._replace(account="Assets:V2")])
        ordered = order_entries([u, disjoint, cost, v])
        self.assertEqual([entry.narration for entry in ordered], ["B", "X", "U", "V"])

    def test_tui_recompute_reads_new_order_after_failure(self):
        browser=SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertEqual(browser.outcome['status'],'NEEDS_REVIEW')
        self.mark()
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome['status'],'CERTIFIED_SCOPED')

    def test_pushmeta_cannot_implicitly_assign_order(self):
        self.recharge.write_text('pushmeta budget-order: -1\n'+self.recharge.read_text(encoding='utf-8')+
                                 '\npopmeta budget-order:\n',encoding='utf-8')
        with self.assertRaises(BudgetError) as caught:
            load_financial(self.root)
        self.assertEqual(caught.exception.code,'META_INJECTION')


if __name__=='__main__':
    unittest.main()

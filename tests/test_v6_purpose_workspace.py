"""Read-only projections: no recalculation, no mutation, no cross-total."""

from copy import deepcopy
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.purpose_workspace import (
    attribution_rows, explain_cash, explain_expense, explain_purpose,
)


class PurposeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            'report_currency': 'CNY',
            'owner_names': {'a': 'A', 'b': 'B'},
            'actual_spendable': {
                'status': 'CERTIFIED', 'actual_spendable': '100',
                'purpose_available': [
                    {'owner_id': 'a', 'units': '100'},
                    {'owner_id': 'b', 'units': '50'},
                ],
                'booked_debts': [{'obligation_id': 'o1', 'remaining_native': '10'}],
                'clearing_receivables': [{'clearing_id': 'c1', 'remaining_native': '5'}],
            },
            'expense_targets': {'categories': [
                {'id': 'food', 'name': 'Food', 'parent': None},
                {'id': 'dining', 'name': 'Dining', 'parent': 'food'},
                {'id': 'travel', 'name': 'Travel', 'parent': None},
            ]},
            'multicurrency': {'native_expenses': [
                {'currency': 'CNY', 'category_id': 'dining', 'net': '20'},
                {'currency': 'JPY', 'category_id': 'dining', 'net': '100'},
                {'currency': 'CNY', 'category_id': 'travel', 'net': '30'},
            ]},
            'transactions': {'rows': [
                {'date': '2026-09-02', 'narration': 'meal',
                 'postings': [
                     {'account': 'Expenses:Food', 'signed_units': '20',
                      'classification': {'slices': [{'category_id': 'dining'}]}},
                     {'account': 'Income:Food', 'signed_units': '-20',
                      'classification': {'slices': [{'category_id': 'dining'}]}},
                 ]},
                {'date': '2026-09-03', 'narration': 'trip',
                 'postings': [
                     {'account': 'Expenses:Travel', 'signed_units': '30',
                      'classification': {'slices': [{'category_id': 'travel'}]}},
                 ]},
            ]},
            'purpose_balances': {'purposes': [
                {'owner_id': 'a', 'name': 'A', 'default_roles': [],
                 'h_native_balances': [{'commodity': 'CNY', 'units': '100'}],
                 'k_book_purpose_changes': [],
                 'pool_book_cost_balances': []},
                {'owner_id': 'b', 'name': 'B', 'default_roles': [],
                 'h_native_balances': [],
                 'k_book_purpose_changes': [{'commodity': 'CNY', 'signed_change': '50'}],
                 'pool_book_cost_balances': [{'commodity': 'CNY', 'units': '70'}]},
            ]},
            'purpose_progress': {'owners': [
                {'owner_id': 'a', 'target_q': '20', 'actual_g': '10'},
            ]},
        }
        self.cash = {
            'report_currency': 'CNY',
            'known_payable_positions': [
                {'account': 'Assets:Cash', 'commodity': 'CNY', 'lot': 'CNY/no-cost',
                 'owner_units': [{'owner_id': 'a', 'units': '100'},
                                 {'owner_id': 'b', 'units': '50'}]},
            ],
            'pending_payable_positions': [{'account': 'Assets:Locked'}],
            'excluded_h_coordinates': [{'account': 'Assets:Fund', 'reason': 'excluded'}],
            'owner_units': [{'owner_id': 'a', 'units': '100'}],
            'spendable_owner_ids': ['a'],
            'free_cash': '100', 'outside_budget_cash': '0', 'total_payable_cash': '150',
        }
        self.ownership = {
            'owner_names': {'a': 'A', 'b': 'B'},
            'purpose_evidence': [
                {'ref': {'path': 'main.bean', 'occurrence': 1, 'posting_ordinal': 1},
                 'date': '2026-09-02', 'account': 'Assets:Cash',
                 'commodity': 'CNY', 'lot': 'CNY/no-cost', 'signed_units': '-10',
                 'source': 'main.bean:2', 'transaction_source': 'main.bean:1',
                 'payee': None, 'narration': 'meal', 'labels': {'fund': 'A'},
                 'before': {'a': '100', 'b': '50'}, 'before_state': 'complete',
                 'evidence': 'inline', 'review_reason': 'inline',
                 'slices': [{'owner_id': 'a', 'signed_units': '-10'}], 'reason_code': None},
                {'ref': {'path': 'main.bean', 'occurrence': 1, 'posting_ordinal': 2},
                 'date': '2026-09-02', 'account': 'Assets:Cash',
                 'commodity': 'CNY', 'lot': 'CNY/no-cost', 'signed_units': '-5',
                 'source': 'main.bean:3', 'before': None, 'before_state': 'pending',
                 'evidence': 'pending', 'reason_code': 'H_OWNER_PENDING',
                 'slices': []},
            ],
        }

    def test_attribution_rows_merge_review_and_keep_all_h_legs(self):
        before = deepcopy(self.ownership)
        reviews = [{'ref': deepcopy(self.ownership['purpose_evidence'][1]['ref']),
                    'reason_code': 'H_OWNER_PENDING', 'reason': 'choose fund',
                    'source': 'main.bean:3'}]
        rows = attribution_rows(self.ownership, reviews)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['review_reason'], 'choose fund')
        self.assertEqual(rows[1]['review_source'], 'main.bean:3')
        self.assertEqual(rows[0]['before'], {'a': '100', 'b': '50'})
        self.assertIsNone(rows[1]['before'])
        self.assertEqual(self.ownership, before)

    def test_attribution_rows_keep_all_matching_reviews(self):
        ref = deepcopy(self.ownership['purpose_evidence'][1]['ref'])
        rows = attribution_rows(self.ownership, [
            {'ref': ref, 'reason_code': 'H_OWNER_PENDING', 'reason': 'fund',
             'source': 'main.bean:3'},
            {'ref': ref, 'reason_code': 'K_EVENT_PENDING', 'reason': 'K cash',
             'source': 'main.bean:4'},
        ])
        self.assertEqual(rows[1]['reason_codes'], ['H_OWNER_PENDING', 'K_EVENT_PENDING'])
        self.assertEqual(len(rows[1]['review_items']), 2)
        self.assertEqual(rows[1]['reason_code'], 'H_OWNER_PENDING')

    def test_explain_cash_filters_owner_without_recalculating(self):
        before = deepcopy((self.report, self.cash))
        result = explain_cash(self.report, self.cash, 'A')
        self.assertEqual(result['selected_owner']['owner_id'], 'a')
        self.assertEqual(result['selected_available_cash'], '100')
        self.assertEqual(result['other_purposes'], [{'owner_id': 'b', 'units': '50'}])
        self.assertEqual(len(result['inventory']['known_payable_positions']), 1)
        self.assertEqual(result['inventory']['known_payable_positions'][0]['owner_units'],
                         [{'owner_id': 'a', 'units': '100'}])
        self.assertEqual(result['inventory']['excluded_h_coordinates'][0]['account'],
                         'Assets:Fund')
        self.assertEqual((self.report, self.cash), before)
        default = explain_cash(self.report, self.cash)
        self.assertEqual(default['selected_owner']['owner_id'], 'a')
        self.assertEqual(default['selected_owner']['is_default'], True)
        self.assertEqual(default['selected_available_cash'], '100')
        other = explain_cash(self.report, self.cash, 'B')
        self.assertEqual(other['selected_available_cash'], '50')
        self.assertEqual(other['global_actual_spendable']['actual_spendable'], '100')
        with self.assertRaises(BudgetError) as caught:
            explain_cash(self.report, self.cash, 'missing')
        self.assertEqual(caught.exception.code, 'PURPOSE_EXPLAIN_OWNER_UNKNOWN')

    def test_explain_expense_uses_only_real_expense_subtree(self):
        before = deepcopy(self.report)
        result = explain_expense(self.report, 'Food')
        self.assertEqual(self.report, before)
        self.assertEqual(result['scope_category_ids'], ['dining', 'food'])
        self.assertEqual([row['currency'] for row in result['native_expenses']], ['CNY', 'JPY'])
        self.assertEqual(len(result['transactions']), 1)
        uses = result['transactions'][0]['postings']
        self.assertEqual([row['account'] for row in uses], ['Expenses:Food'])
        with self.assertRaises(BudgetError) as caught:
            explain_expense(self.report, 'NoSuch')
        self.assertEqual(caught.exception.code, 'PURPOSE_EXPLAIN_CATEGORY_UNKNOWN')

    def test_cash_known_zero_is_not_unknown_and_k_is_not_merged(self):
        report = deepcopy(self.report)
        report['actual_spendable']['purpose_available'] = [
            {'owner_id': 'a', 'units': '100'}]
        cash = deepcopy(self.cash)
        cash['owner_units'] = []
        cash['known_payable_positions'] = []
        result = explain_cash(report, cash, 'B')
        self.assertEqual(result['selected_available_cash'], '0')
        self.assertEqual(result['global_actual_spendable']['actual_spendable'], '100')
        self.assertEqual(result['inventory']['known_payable_positions'], [])
        report['actual_spendable']['status'] = 'NEEDS_REVIEW'
        self.assertIsNone(explain_cash(report, cash, 'B')['selected_available_cash'])

    def test_explicit_owner_does_not_need_a_unique_default(self):
        cash = deepcopy(self.cash)
        cash['spendable_owner_ids'] = ['a', 'b']
        result = explain_cash(self.report, cash, 'B')
        self.assertEqual(result['selected_owner']['owner_id'], 'b')
        self.assertFalse(result['selected_owner']['is_default'])
        self.assertEqual(result['selected_available_cash'], '50')
        with self.assertRaises(BudgetError) as caught:
            explain_cash(self.report, cash)
        self.assertEqual(caught.exception.code, 'PURPOSE_EXPLAIN_OWNER_AMBIGUOUS')

    def test_explain_expense_marks_only_matching_slices(self):
        report = deepcopy(self.report)
        report['transactions']['rows'].append({
            'date': '2026-09-04', 'narration': 'split',
            'postings': [{'account': 'Expenses:Food', 'signed_units': '100',
                          'classification': {'slices': [
                              {'category_id': 'dining', 'signed_units': '30'},
                              {'category_id': 'travel', 'signed_units': '70'},
                          ]}}],
        })
        result = explain_expense(report, 'Food')
        row = next(row for row in result['transactions'] if row['narration'] == 'split')
        posting = row['postings'][0]
        self.assertEqual(posting['signed_units'], '100')
        self.assertEqual(posting['selected_slices'],
                         [{'category_id': 'dining', 'signed_units': '30'}])

    def test_explain_purpose_keeps_h_k_pool_separate(self):
        before = deepcopy(self.report)
        result = explain_purpose(self.report, 'B')
        self.assertEqual(self.report, before)
        self.assertEqual(result['owner_id'], 'b')
        self.assertEqual(result['purpose']['h_native_balances'], [])
        self.assertEqual(result['purpose']['k_book_purpose_changes'][0]['signed_change'], '50')
        self.assertEqual(result['purpose']['pool_book_cost_balances'][0]['units'], '70')
        self.assertIsNone(result['purpose_progress'])
        with self.assertRaises(BudgetError) as caught:
            explain_purpose(self.report, 'NoSuch')
        self.assertEqual(caught.exception.code, 'PURPOSE_EXPLAIN_OWNER_UNKNOWN')


if __name__ == '__main__':
    unittest.main()

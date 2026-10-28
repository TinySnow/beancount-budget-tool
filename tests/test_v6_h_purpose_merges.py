"""Explicit many-to-one H transfers conserve funds without relaxing overdrafts."""

from fractions import Fraction
from itertools import permutations
import unittest

from beancount_budget_tool.errors import BudgetError
import test_v6_h_redemptions as fixtures


class HPurposeMergeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HRedemptionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.doc['accounts']['Assets:Wealth']['opening']['CNY'] = {
            '投资': '80.25', '自由资金': '19.75', '额外储蓄': '2100'}
        self.f.plans['months']['2026-09']['allocation_targets']['储蓄'] = '100'

    def leg(self, account, units, fund=None):
        return f'  {account} {units} CNY\n' + (f'    fund: "{fund}"\n' if fund is not None else '')

    def body(self, source='投资80.25,自由资金19.75', target='储蓄', reverse=False):
        legs = [self.leg('Assets:Wealth', '-100', source), self.leg('Assets:Bank', '100', target)]
        return '2026-09-02 * "merge purpose"\n' + ''.join(reversed(legs) if reverse else legs)

    def net(self, report):
        return {report['owner_names'][key]: Fraction(value)
                for key, value in report['transactions']['rows'][0]['net_allocations'].items()}

    def balances(self, report):
        return {(row['account'], report['owner_names'][row['owner_id']]): Fraction(row['units'])
                for row in report['account_purposes']['rows'] if Fraction(row['units'])}

    def test_two_leg_merge_updates_each_source_destination_and_target_without_consumption(self):
        report = self.f.audit(self.body())
        self.assertEqual(self.net(report), {'投资': Fraction('-80.25'), '自由资金': Fraction('-19.75'), '储蓄': 100})
        self.assertEqual(self.balances(report), {('Assets:Wealth', '额外储蓄'): 2100,
                         ('Assets:Bank', '生活费'): 100, ('Assets:Bank', '储蓄'): 100})
        progress = {report['owner_names'][row['owner_id']]: row for row in report['purpose_progress']['owners']}
        self.assertEqual(progress['储蓄']['actual_g'], '100')
        self.assertEqual(report['transactions']['rows'][0]['purpose_events'][0]['kind'], 'h_transfer')
        self.assertEqual(report['actual_spendable']['actual_spendable'], '100')
        self.assertTrue(all(row['classification'] is None for row in report['transactions']['rows'][0]['postings']))

    def test_chinese_punctuation_and_posting_order_have_identical_financial_results(self):
        expected = self.f.audit(self.body())
        for source in ('自由资金19.75，投资80.25', '投资：80.25元｜自由资金:19.75;'):
            for reverse in (False, True):
                with self.subTest(source=source, reverse=reverse):
                    report = self.f.audit(self.body(source=source, reverse=reverse))
                    self.assertEqual(self.net(report), self.net(expected))
                    self.assertEqual(self.balances(report), self.balances(expected))

    def test_insufficient_source_purpose_is_not_topped_up_from_other_available_purposes(self):
        self.f.doc['accounts']['Assets:Wealth']['opening']['CNY'].update(投资='80.24', 自由资金='19.76')
        with self.assertRaises(BudgetError) as caught:
            self.f.audit(self.body())
        self.assertEqual(caught.exception.code, 'H_OWNER_NEGATIVE')
        self.assertIn('资金用途：投资', caught.exception.message)
        self.assertIn('本次缺口：0.01 CNY', caught.exception.message)

    def test_split_total_mismatch_still_fails_at_source(self):
        result = self.f.audit(self.body(source='投资80.25,自由资金19.74'), certified=False)
        self.assertIsNone(result['report'])
        self.assertTrue(any('加回分录金额' in row['detail'] for row in result['review_items']))

    def test_missing_mixed_source_is_still_ambiguous(self):
        result = self.f.audit(self.body(source=None), certified=False)
        self.assertIsNone(result['report'])
        self.assertIn('FUND_MIXED_SOURCE', {row['reason_code'] for row in result['review_items']})

    def test_missing_target_preserves_source_purposes_instead_of_inventing_merge(self):
        report = self.f.audit(self.body(target=None))
        self.assertEqual(self.net(report), {})
        balances = self.balances(report)
        self.assertEqual(balances[('Assets:Bank', '投资')], Fraction('80.25'))
        self.assertEqual(balances[('Assets:Bank', '自由资金')], Fraction('19.75'))

    def test_merge_to_an_existing_source_purpose_only_reallocates_the_other_share(self):
        report = self.f.audit(self.body(target='投资'))
        self.assertEqual(self.net(report), {'投资': Fraction('19.75'), '自由资金': Fraction('-19.75')})

    def test_multileg_merge_uses_same_rule_with_no_pairing_by_posting_order(self):
        legs = [self.leg('Assets:Wealth', '-80.25', '投资'),
                self.leg('Assets:Wealth', '-19.75', '自由资金'), self.leg('Assets:Bank', '100', '储蓄')]
        for order in permutations(legs):
            report = self.f.audit('2026-09-02 * "split posting merge"\n' + ''.join(order))
            self.assertEqual(self.net(report), {'投资': Fraction('-80.25'), '自由资金': Fraction('-19.75'), '储蓄': 100})
            self.assertEqual(report['transactions']['rows'][0]['purpose_events'][0]['kind'], 'h_bundle')

    def test_balances_carry_to_next_month_without_repeating_allocation(self):
        self.f.audit(self.body())
        report = self.f.audit(self.body(), month='2026-10')
        self.assertEqual(self.balances(report)[('Assets:Bank', '储蓄')], 100)
        self.assertTrue(all(row['actual_g'] == '0' for row in report['purpose_progress']['owners']))


if __name__ == '__main__':
    unittest.main()

"""Source evidence remains usable while default ambiguity cannot bypass views."""

import unittest
from datetime import date
from fractions import Fraction
import yaml

import test_v6_liability_rules as fixtures
from beancount_budget_tool.v6.simple_config import audit_simple_period, build_runtime
from beancount_budget_tool.v6.opening import audit_opening
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through
from beancount_budget_tool.v6.salary_cycle import audit_salary_cycle
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser


class AttributionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.LiabilityRuleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.doc['accounts']['Assets:Bank']['opening']['CNY'] = {'生活费': '6000', '储蓄': '4000'}

    def holdings(self, report):
        return {purpose['name']: sum((Fraction(row['units']) for row in purpose['h_native_balances']
                                     if row['commodity'] == 'CNY'), Fraction(0))
                for purpose in report['purpose_balances']['purposes']}

    def test_proved_receivable_reimbursement_restores_original_fund_in_mixed_cash(self):
        self.case.doc['liabilities']['Liabilities:Advance'] = {'role': 'receivable'}
        report = self.case.run_ledger('''2026-09-02 * "advance from saving"
  Assets:Bank -500 CNY
    fund: "储蓄"
  Liabilities:Advance 500 CNY
    budget: "储蓄"
2026-09-03 * "proved reimbursement without new fund"
  Assets:Bank 500 CNY
  Liabilities:Advance -500 CNY
    budget: "储蓄"
''')
        positions = self.holdings(report)
        self.assertEqual((positions['生活费'], positions['储蓄']), (6000, 4000))

    def test_proved_overpayment_return_restores_cash_source_not_debt_owner(self):
        report = self.case.run_ledger(self.case.purchase() + '''2026-09-03 * "prepay from saving"
  Assets:Bank -150 CNY
    fund: "储蓄"
  Liabilities:Card 150 CNY
    budget: "生活费"
2026-09-04 * "return original saving"
  Assets:Bank 50 CNY
  Liabilities:Card -50 CNY
''')
        positions = self.holdings(report)
        self.assertEqual((positions['生活费'], positions['储蓄']), (6000, 3900))
        self.assertEqual(report['actual_spendable']['booked_debts'], [])

    def test_ambiguous_cash_is_pending_in_month_range_cycle_and_browser(self):
        result = self.case.run_ledger('''2026-09-02 * "ambiguous spend"
  Assets:Bank -10 CNY
  Expenses:Food 10 CNY
''', certified=False)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        period = audit_simple_period(self.case.config, '2026-09', '2026-09')
        cycle = audit_salary_cycle(self.case.config, '2026-09-01', '2026-10-01',
                                   budget='100', category='生活开销')
        browser = SimpleReportBrowser(self.case.config)
        self.addCleanup(browser.close)
        browser.refresh()
        for outcome in (result, period, cycle, browser.outcome):
            self.assertEqual(outcome['status'], 'NEEDS_REVIEW', outcome)
            self.assertIsNone(outcome['report'])
            self.assertIn('H_OWNER_PENDING', {row['reason_code'] for row in outcome['review_items']})

    def test_cycle_guard_does_not_require_month_end_completeness(self):
        self.case.budgets['months']['2026-09']['completeness_confirmed'] = False
        self.case.run_ledger('''2026-09-02 * "explicit spend before month end"
  Assets:Bank -10 CNY
    fund: "储蓄"
  Expenses:Food 10 CNY
''', certified=False)
        cycle = audit_salary_cycle(self.case.config, '2026-09-01', '2026-10-01',
                                   budget='100', category='生活开销')
        self.assertEqual(cycle['status'], 'CERTIFIED_SCOPED', cycle)
        self.assertEqual(cycle['report']['cycle_budget']['remaining'], '90')

    def test_mixed_receivable_plus_new_income_cannot_claim_pure_restoration(self):
        self.case.doc['liabilities']['Liabilities:Advance'] = {'role': 'receivable'}
        result = self.case.run_ledger('''2026-09-02 * "advance from saving"
  Assets:Bank -500 CNY
    fund: "储蓄"
  Liabilities:Advance 500 CNY
    budget: "储蓄"
2026-09-03 * "reimbursement plus new income"
  Assets:Bank 600 CNY
  Liabilities:Advance -500 CNY
    budget: "储蓄"
  Income:Work -100 CNY
''', certified=False)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertIn('H_OWNER_PENDING', {r['reason_code'] for r in result['review_items']})

    def test_single_owner_keeps_existing_default_policy(self):
        self.case.doc['accounts']['Assets:Bank']['opening']['CNY'] = {'储蓄': '10000'}
        report = self.case.run_ledger('''2026-09-02 * "single source compatibility"
  Assets:Bank -10 CNY
  Expenses:Food 10 CNY
''')
        positions = self.holdings(report)
        self.assertEqual((positions['生活费'], positions['储蓄']), (-10, 10000))

    def test_income_reversal_in_mixed_cash_needs_its_own_fund(self):
        result = self.case.run_ledger('''2026-09-02 * "ambiguous income reversal"
  Assets:Bank -10 CNY
  Income:Work 10 CNY
''', certified=False)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIn('H_OWNER_PENDING', {r['reason_code'] for r in result['review_items']})

    def test_card_refund_changes_responsibility_not_h_cash(self):
        report = self.case.run_ledger(self.case.purchase() + '''2026-09-03 * "card refund"
  Liabilities:Card 20 CNY
    budget: "生活费"
  Expenses:Food -20 CNY
''')
        positions = self.holdings(report)
        self.assertEqual((positions['生活费'], positions['储蓄']), (6000, 4000))
        self.assertEqual(self.case.spent(report), 80)

    def test_inline_fund_and_external_claim_conflict_is_not_priority_override(self):
        self.case.run_ledger('''2026-09-02 * "explicit spend"
  Assets:Bank -10 CNY
    fund: "储蓄"
  Expenses:Food 10 CNY
''')
        runtime = build_runtime(self.case.config)
        self.addCleanup(runtime.close)
        compiled = yaml.safe_load(runtime.config.read_text(encoding='utf-8'))
        accounts = runtime.config.parent / compiled['accounts']
        opening = runtime.config.parent / compiled['opening']
        ownership = runtime.config.parent / compiled['ownership']
        reviewed = audit_ownership(self.case.ledger, date(2026, 9, 1), accounts, ownership, opening)
        row = next(r for r in reviewed['ownership'] if r['signed_units'] == '-10')
        document = yaml.safe_load(ownership.read_text(encoding='utf-8'))
        document['claims'].append({'claim': row['ref'], 'reason': 'duplicate evidence must reject',
            'slices': [{'id': 'duplicate', 'owner_id': row['slices'][0]['owner_id'],
                        'role': 'hold', 'signed_units': '-10'}]})
        ownership.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        with self.assertRaises(BudgetError) as caught:
            audit_ownership(self.case.ledger, date(2026, 9, 1), accounts, ownership, opening)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_DOUBLE_CLAIM')

    def test_jpy_single_owner_does_not_inherit_mixed_cny_ambiguity(self):
        account = self.case.doc['accounts']['Assets:Bank']
        account['payment_scope']['JPY'] = 'excluded'
        account['opening']['JPY'] = {'储蓄': '1000'}
        result = self.case.run_ledger('''2026-08-31 * "real JPY opening"
  Assets:Bank 1000 JPY
  Equity:Opening -1000 JPY
2026-09-02 * "single JPY source compatibility"
  Assets:Bank -10 JPY
  Expenses:Food 10 JPY
''', certified=False)
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED', result)
        positions = {(purpose['name'], row['commodity']): Fraction(row['units'])
                     for purpose in result['report']['purpose_balances']['purposes']
                     for row in purpose['h_native_balances']}
        self.assertEqual(positions['生活费', 'CNY'], 6000)
        self.assertEqual(positions['储蓄', 'CNY'], 4000)
        self.assertEqual(positions['储蓄', 'JPY'], 1000)
        self.assertEqual(positions['生活费', 'JPY'], -10)

    def test_early_cycle_ignores_proved_future_k_zero_opening(self):
        self.case.doc['account_groups'] = {'K': ['Assets:FutureFund']}
        self.case.run_ledger('''2026-09-01 open Assets:FutureFund
2026-09-02 * "explicit current spend"
  Assets:Bank -10 CNY
    fund: "储蓄"
  Expenses:Food 10 CNY
2026-09-20 * "later subscription"
  Assets:Bank -100 CNY
    fund: "储蓄"
  Assets:FutureFund 100 CNY
    fund: "储蓄"
''')
        cycle = audit_salary_cycle(self.case.config, '2026-09-01', '2026-09-15',
                                   budget='100', category='生活开销')
        self.assertEqual(cycle['status'], 'CERTIFIED_SCOPED', cycle)
        self.assertEqual(cycle['report']['cycle_budget']['remaining'], '90')

        runtime = build_runtime(self.case.config)
        self.addCleanup(runtime.close)
        compiled = yaml.safe_load(runtime.config.read_text(encoding='utf-8'))
        opening = runtime.config.parent / compiled['opening']
        original = yaml.safe_load(opening.read_text(encoding='utf-8'))
        for changes in ({'signed_units': '1'}, {'commodity': 'JPY', 'lot': 'JPY/no-cost'},
                        {'account': 'Assets:InventedFund'}):
            with self.subTest(changes=changes):
                document = {**original, 'k_openings': [{**original['k_openings'][0], **changes}]}
                opening.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
                with booked_through(date(2026, 9, 14)), self.assertRaises(BudgetError) as caught:
                    audit_opening(self.case.ledger, date(2026, 9, 1),
                                  runtime.config.parent / compiled['accounts'], opening)
                self.assertEqual(caught.exception.code, 'OPENING_COORDINATE_INVALID')

if __name__ == '__main__':
    unittest.main()

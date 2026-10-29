"""User-frozen native multi-currency report contract.

These tests intentionally assert the frozen scope: foreign income/expense is
reported in its own commodity, without requiring a CNY price or conversion.
"""

from fractions import Fraction
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.v6.monthly_report import render_workflow_text
from beancount_budget_tool.v6.period_report import csv_rows
from beancount_budget_tool.v6.salary_cycle import audit_salary_cycle
from beancount_budget_tool.v6.simple_config import audit_simple, audit_simple_period


class NativeMultiCurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve() / 'multicurrency'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/v6/multicurrency',
                        self.base)
        self.config = self.base / 'config.yml'

        # The frozen design does not use ledger prices. Remove the example's
        # CNY prices and keep the foreign purchase/income as native facts.
        ledger = self.base / 'main.bean'
        text = ledger.read_text(encoding='utf-8')
        text = text.replace('2025-01-17 price JPY 0.05 CNY\n', '')
        text = text.replace('2025-01-18 price USD 7 CNY\n', '')
        ledger.write_text(text, encoding='utf-8')

    def test_foreign_expense_needs_no_price_and_is_reported_natively(self):
        result = audit_simple(self.config, include_transactions=True)
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED', result.get('review_items'))
        self.assertIsNotNone(result['report'])
        native = result['report']['multicurrency']
        self.assertEqual(native['status'], 'CERTIFIED_NATIVE_SCOPED')
        expenses = {(row['currency'], row['category_name']): row
                    for row in native['native_expenses']}
        row = expenses[('JPY', '生活开销')]
        self.assertEqual((row['gross'], row['refund'], row['net']), ('550', '0', '550'))
        self.assertNotIn('report_value', row)
        self.assertEqual(result['report']['report_currency'], 'CNY')
        self.assertIsNone(result['report'].get('native_cny_total'))

    def test_foreign_income_is_native_and_not_cny(self):
        result = audit_simple(self.config, include_transactions=True)
        self.assertIsNotNone(result['report'])
        native = result['report']['multicurrency']
        income = next(row for row in native['native_income']
                      if row['currency'] == 'USD')
        self.assertEqual(Fraction(income['net']), 25)
        self.assertNotIn('report_value', income)

    def test_unpriced_foreign_is_not_a_valuation_review(self):
        result = audit_simple(self.config, include_transactions=True)
        codes = {row.get('reason_code') for row in result.get('review_items', [])}
        self.assertNotIn('VALUATION_MISSING', codes)
        self.assertNotIn('VALUATION_PRICE_STALE', codes)

    def test_native_text_has_no_cny_conversion_column(self):
        result = audit_simple(self.config, include_transactions=True)
        text = render_workflow_text(result)
        self.assertIn('550', text)
        self.assertIn('JPY', text)
        self.assertNotIn('折 CNY', text)
        self.assertNotIn('缺价', text)

    def test_cross_month_refund_keeps_native_currency(self):
        ledger = self.base / 'main.bean'
        ledger.write_text(ledger.read_text(encoding='utf-8') + '''
2025-02-10 * "Refund foreign purchase"
  Expenses:Entertainment -550 JPY
  Assets:Yen 550 JPY
    fund: "信用卡"
''', encoding='utf-8')

        january = audit_simple(self.config, '2025-01')
        february = audit_simple(self.config, '2025-02')
        self.assertEqual(january['status'], 'CERTIFIED_NATIVE_SCOPED')
        self.assertEqual(february['status'], 'CERTIFIED_NATIVE_SCOPED')
        jan_row = next(row for row in january['report']['multicurrency']['native_expenses']
                       if row['currency'] == 'JPY')
        feb_row = next(row for row in february['report']['multicurrency']['native_expenses']
                       if row['currency'] == 'JPY')
        self.assertEqual((jan_row['gross'], jan_row['refund'], jan_row['net']), ('550', '0', '550'))
        self.assertEqual((feb_row['gross'], feb_row['refund'], feb_row['net']), ('0', '550', '-550'))
        self.assertNotIn('report_value', feb_row)

        period = audit_simple_period(self.config, '2025-01', '2025-02')
        self.assertEqual(period['status'], 'CERTIFIED_NATIVE_SCOPED')
        native = period['report']['multicurrency']
        row = next(row for row in native['native_expenses'] if row['currency'] == 'JPY')
        self.assertEqual((row['gross'], row['refund'], row['net']), ('550', '550', '0'))
        self.assertEqual(native['native_totals'][0]['currency'], 'CNY')
        self.assertIn('JPY', {row['currency'] for row in native['native_expenses']})

    def test_salary_cycle_keeps_cny_cap_separate_from_jpy(self):
        result = audit_salary_cycle(self.config, '2025-01-01', '2025-02-01',
                                    budget='100', category='生活开销')
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED', result.get('review_items'))
        self.assertIsNotNone(result['report'])
        self.assertEqual(result['report']['cycle_budget']['net_spent'], '0')
        native = result['report']['multicurrency']
        row = next(row for row in native['native_expenses'] if row['currency'] == 'JPY')
        self.assertEqual(row['net'], '550')
        self.assertNotIn('report_value', row)

    def test_csv_exposes_native_metrics_only(self):
        outcome = audit_simple_period(self.config, '2025-01', '2025-01')
        records = [row for row in csv_rows(outcome)
                   if row.get('section') in ('native_expenses', 'native_income')]
        self.assertTrue(records)
        self.assertEqual({row['section'] for row in records},
                         {'native_expenses', 'native_income'})
        self.assertTrue(all(row['metric'] in {'gross', 'refund', 'net', 'budget', 'remaining'}
                            for row in records))
        self.assertNotIn('report_value', {row['metric'] for row in records})
        jpy = next(row for row in records
                   if row['section'] == 'native_expenses' and row['commodity'] == 'JPY'
                   and row['metric'] == 'net')
        self.assertEqual(jpy['value'], '550')

    def test_cny_capacity_does_not_use_foreign_units(self):
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED')
        self.assertEqual(result['report']['actual_spendable']['actual_spendable'], '1000')
        max_spend = result['report']['max_spendable']
        self.assertEqual(max_spend['max_spendable'], '100')
        self.assertEqual(max_spend['payable_after_obligations'], '1000')
        self.assertNotIn('JPY', str(max_spend))

    def test_salary_cycle_blocks_unresolved_foreign_liability(self):
        ledger = self.base / 'main.bean'
        ledger.write_text(ledger.read_text(encoding='utf-8') + '''
2025-01-20 * "uncovered foreign responsibility"
  Expenses:Entertainment 1 JPY
  Liabilities:Card -1 JPY
    budget: "信用卡"
''', encoding='utf-8')
        result = audit_salary_cycle(self.config, '2025-01-01', '2025-02-01',
                                    budget='100', category='生活开销')
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertIn('SETTLEMENT_VALUE_MISSING',
                      {row.get('reason_code') for row in result.get('review_items', [])})

    def test_salary_cycle_blocks_negative_h_inventory(self):
        ledger = self.base / 'main.bean'
        ledger.write_text(ledger.read_text(encoding='utf-8') + '''
2025-01-20 * "overspend native yen"
  Expenses:Entertainment 1 JPY
  Assets:Yen -1 JPY
    fund: "信用卡"
''', encoding='utf-8')
        result = audit_salary_cycle(self.config, '2025-01-01', '2025-02-01',
                                    budget='100', category='生活开销')
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertIn('H_NEGATIVE_INVENTORY',
                      {row.get('reason_code') for row in result.get('review_items', [])})

    def test_unassessed_cny_cap_still_leaves_native_report_visible(self):
        budgets = self.base / 'budgets.yml'
        import yaml
        document = yaml.safe_load(budgets.read_text(encoding='utf-8'))
        document['defaults']['expense_targets'] = {}
        budgets.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')

        result = audit_simple(self.config, include_transactions=True)
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED')
        self.assertIsNotNone(result['report'])
        self.assertEqual(result['report']['max_spendable']['status'], 'NOT_ASSESSED')
        self.assertIsNone(result['report']['max_spendable']['max_spendable'])
        row = next(row for row in result['report']['multicurrency']['native_expenses']
                   if row['currency'] == 'JPY')
        self.assertEqual(row['net'], '550')

    def test_period_keeps_currencies_separate(self):
        result = audit_simple_period(self.config, '2025-01', '2025-01')
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED', result.get('review_items'))
        self.assertIsNotNone(result['report'])
        native = result['report']['multicurrency']
        row = next(row for row in native['native_expenses']
                   if row['currency'] == 'JPY')
        self.assertEqual(row['net'], '550')
        self.assertNotIn('report_value', row)


if __name__ == '__main__':
    unittest.main()

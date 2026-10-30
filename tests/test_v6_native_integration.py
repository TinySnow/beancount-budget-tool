"""Native reports retain their scope across browser, export and finance gates."""

import csv
import io
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.v6.simple_config import audit_simple, audit_simple_period
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.period_report import _single_month_period, render_csv


class NativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / '原币 报表'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/v6/multicurrency', self.base)
        self.config = self.base / 'config.yml'

    def edit(self, name, change):
        path = self.base / name
        doc = yaml.safe_load(path.read_text(encoding='utf-8'))
        change(doc)
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')

    def test_browser_month_period_and_saved_cycle_keep_native_report_and_details(self):
        self.edit('budgets.yml', lambda doc: doc.update(salary_cycles={
            '2025-01-01': '100', '2025-02-02': '200'}))
        self.edit('config.yml', lambda doc: doc.update(expense_budget_basis='salary_cycle'))
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_NATIVE_SCOPED')
        self.assertTrue(browser.has_transactions())
        self.assertTrue(any(row['narration'] == 'Synthetic foreign purchase' for row in browser.transactions()))
        self.assertEqual(browser.outcome, audit_simple(self.config, include_transactions=True))
        self.assertEqual(browser.outcome['report']['salary_cycle']['config'], str(self.config.resolve()))
        browser.period, browser.period_range = True, ('2025-01', '2025-02')
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome, audit_simple_period(self.config, '2025-01', '2025-02'))
        for month in browser.outcome['report']['monthly_reports']:
            self.assertEqual(month['salary_cycle']['config'], str(self.config.resolve()))
        browser.select_saved_cycle('2025-01-01')
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_NATIVE_SCOPED')
        self.assertEqual(browser.outcome['config'], str(self.config.resolve()))
        self.assertEqual(browser.outcome['through'], '2025-02-01')
        self.assertEqual(browser.outcome['report']['cycle_budget']['net_spent'], '5')
        self.assertEqual(browser.outcome['report']['multicurrency']['native_expenses'][-1]['net'], '550')

    def test_monthly_csv_exports_native_status_and_exact_currency(self):
        result = audit_simple(self.config)
        rows = list(csv.DictReader(io.StringIO(render_csv(_single_month_period(result, self.config)))))
        row = next(row for row in rows if row['section'] == 'native_expenses' and row['metric'] == 'net')
        self.assertEqual((row['commodity'], row['value'], row['numerator'], row['denominator']),
                         ('JPY', '550', '550', '1'))
        self.assertEqual(row['status'], 'CERTIFIED_NATIVE_SCOPED')

    def test_no_conversion_auditor_runs_in_native_query_even_with_ledger_prices(self):
        ledger = self.base / 'main.bean'
        ledger.write_text(ledger.read_text(encoding='utf-8') +
                          '\n2025-01-17 price JPY 99 CNY\n2025-01-18 price USD 99 CNY\n', encoding='utf-8')
        with patch('beancount_budget_tool.v6.spending.audit_classification_valuation',
                   side_effect=AssertionError('native queries must not require conversion')):
            result = audit_simple(self.config)
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED')
        self.assertEqual(result['report']['max_spendable']['max_spendable'], '100')
        self.assertEqual(result['report']['multicurrency']['native_expenses'][0]['net'], '550')

    def test_not_assessed_cny_goal_does_not_suppress_native_report(self):
        self.edit('budgets.yml', lambda doc: doc['defaults']['expense_targets'].update(生活开销='not_assessed'))
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'CERTIFIED_NATIVE_SCOPED', result['review_items'])
        self.assertIsNotNone(result['report'])
        self.assertIsNone(result['report']['max_spendable']['max_spendable'])

    def test_foreign_cash_requires_financial_scope_before_native_certification(self):
        self.edit('config.yml', lambda doc: doc['account_groups']['H']['excluded'].remove('Assets:Dollars'))
        self.edit('config.yml', lambda doc: doc['account_groups']['H']['payable'].append('Assets:Dollars'))
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertTrue(result['review_items'])
        self.assertFalse((self.base / '.budget-state').exists())

    def test_native_report_cannot_certify_unresolved_foreign_liability(self):
        ledger = self.base / 'main.bean'
        ledger.write_text(ledger.read_text(encoding='utf-8') +
            '\n2025-01-20 * "uncovered foreign responsibility"\n'
            '  Expenses:Entertainment 1 JPY\n  Liabilities:Card -1 JPY\n    budget: "信用卡"\n', encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertIn('SETTLEMENT_VALUE_MISSING', {row['reason_code'] for row in result['review_items']})
        self.assertEqual(result['native_report']['status'], 'NEEDS_REVIEW')
        self.assertEqual(result['native_report']['native_expenses'][0]['net'], '551')
        records = list(csv.DictReader(io.StringIO(render_csv(_single_month_period(result, self.config)))))
        self.assertTrue(all(row['status'] == 'NEEDS_REVIEW' for row in records
                            if row['section'] == 'native_expenses' and row['metric'] == 'net'))

    def test_native_report_cannot_certify_negative_h_inventory(self):
        ledger = self.base / 'main.bean'
        ledger.write_text(ledger.read_text(encoding='utf-8') +
            '\n2025-01-20 * "overspend native yen"\n'
            '  Expenses:Entertainment 1 JPY\n  Assets:Yen -1 JPY\n    fund: "信用卡"\n', encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertIn('H_NEGATIVE_INVENTORY', {row['reason_code'] for row in result['review_items']})

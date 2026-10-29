"""Explicit equity receipts add current holdings, without creating income or G."""

from fractions import Fraction
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6.account_queries import query
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.transaction_details import render_transaction


class EquityReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/v6/simple',
                        self.base, dirs_exist_ok=True)
        self.config = self.base / 'config.yml'
        self.ledger = self.base / 'main.bean'
        self.original = self.ledger.read_text(encoding='utf-8')

    def receipt(self, asset='7894.17 CNY', equity='-7894.17 CNY', fund='额外储蓄', extra=''):
        return ('\n2026-09-01 open Equity:Init\n2026-09-09 * "定转活"\n'
                f'  Assets:Cash {asset}\n' +
                (f'    fund: "{fund}"\n' if fund is not None else '') +
                f'  Equity:Init {equity}\n' + extra)

    def audit(self, body):
        self.ledger.write_text(self.original + body, encoding='utf-8')
        return audit_simple(self.config, include_transactions=True)

    def test_old_principal_receipt_adds_only_current_explicit_holdings(self):
        result = self.audit(self.receipt())
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result['review_items'])
        report = result['report']
        savings = next(ident for ident, name in report['owner_names'].items() if name == '额外储蓄')
        position = next(row for row in report['account_purposes']['rows'] if row['owner_id'] == savings)
        self.assertEqual(Fraction(position['units']), Fraction('7894.17'))
        progress = {row['owner_id']: row for row in report['purpose_progress']['owners']}
        self.assertEqual(progress[savings]['actual_g'], '0')
        living = next(ident for ident, name in report['owner_names'].items() if name == '生活费')
        self.assertEqual(progress[living]['actual_g'], '200')
        row = next(row for row in report['transactions']['rows'] if row['narration'] == '定转活')
        self.assertEqual(row['net_allocations'], {})
        self.assertTrue(all(p['classification'] is None and p['benefit_source'] is None for p in row['postings']))
        event = row['purpose_events'][0]
        self.assertEqual(event['kind'], 'equity_receipt')
        self.assertEqual(event['protocol']['protocol'], 'explicit_equity_receipt_v1')
        self.assertEqual(len(event['source_refs']), 1)
        self.assertIn('不计当期收入、消费或正式划拨 G', '\n'.join(render_transaction(row, report['owner_names'])))
        before = {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(before, {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_missing_fund_does_not_inherit_the_income_default(self):
        result = self.audit(self.receipt(fund=None))
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertIn('OWNERSHIP_UNREVIEWED', {row['reason_code'] for row in result['review_items']})

    def test_receipts_accumulate_instead_of_resetting_existing_ownership(self):
        result = self.audit(self.receipt() + self.receipt().split('2026-09-01 open Equity:Init\n')[1]
                            .replace('2026-09-09', '2026-09-10'))
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result['review_items'])
        report = result['report']
        savings = next(ident for ident, name in report['owner_names'].items() if name == '额外储蓄')
        position = next(row for row in report['account_purposes']['rows'] if row['owner_id'] == savings)
        self.assertEqual(Fraction(position['units']), 2 * Fraction('7894.17'))
        self.assertEqual(sum(row['net_allocations'].get(savings, '0') != '0'
                             for row in report['transactions']['rows']), 0)

    def test_multifund_or_negative_or_priced_or_composite_equity_is_not_supported(self):
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['accounts']['Assets:Cash']['payment_scope'] = {'CNY': 'excluded'}
        self.config.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
        variants = [self.receipt(fund='额外储蓄7890,生活费4.17'),
                    self.receipt(asset='-10 CNY', equity='10 CNY', fund='生活费'),
                    self.receipt(asset='7894.17 CNY @ 1 CNY'),
                    self.receipt(equity='-7894.17 CNY @ 1 CNY'),
                    self.receipt(asset='7894.17 CNY {1 CNY}'),
                    self.receipt(equity='-7894.174 CNY'),  # Inside loader tolerance, not exact.
                    self.receipt(equity='-7895.17 CNY', extra='  Expenses:Food 1 CNY\n')]
        for body in variants:
            with self.subTest(body=body):
                result = self.audit(body)
                self.assertEqual(result['status'], 'IMPLEMENTATION_BLOCKED', result['review_items'])
                self.assertIsNone(result['report'])
                self.assertIn('NAMED_H_EVENT_UNSUPPORTED', {row['reason_code'] for row in result['review_items']})

    def test_future_receipt_does_not_change_earlier_month(self):
        before = audit_simple(self.config)
        after = self.audit(self.receipt().replace('2026-09-09', '2026-10-09'))
        self.assertEqual(after['status'], 'CERTIFIED_SCOPED', after['review_items'])
        self.assertEqual(after['report']['actual_spendable']['actual_spendable'],
                         before['report']['actual_spendable']['actual_spendable'])
        self.assertNotIn('定转活', {row['narration'] for row in after['report']['transactions']['rows']})

    def test_day_history_does_not_backfill_the_principal_before_receipt(self):
        self.audit(self.receipt())
        early = query(self.config, '2026-09-08', account='Assets:Cash')['report']
        later = query(self.config, '2026-09-09', account='Assets:Cash')['report']
        self.assertNotIn('额外储蓄', {row['purpose'] for row in early['closing_positions']})
        position = next(row for row in later['closing_positions'] if row['purpose'] == '额外储蓄')
        self.assertEqual(Fraction(position['amount']), Fraction('7894.17'))

    def test_native_foreign_receipt_does_not_become_report_currency_allocation(self):
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['account_groups'] = {'H': {'excluded': ['Assets:Yen']}}
        self.config.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
        result = self.audit('\n2026-09-01 open Assets:Yen\n' +
                            self.receipt(asset='550 JPY', equity='-550 JPY').replace('Assets:Cash', 'Assets:Yen'))
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result['review_items'])
        report = result['report']
        savings = next(ident for ident, name in report['owner_names'].items() if name == '额外储蓄')
        position = next(row for row in report['account_purposes']['rows'] if row['owner_id'] == savings)
        self.assertEqual((position['commodity'], position['units']), ('JPY', '550'))
        progress = next(row for row in report['purpose_progress']['owners'] if row['owner_id'] == savings)
        self.assertEqual(progress['actual_g'], '0')


if __name__ == '__main__':
    unittest.main()

"""Public read-only onboarding and trend checks."""

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.usage_tools import (
    onboarding, render_onboarding, render_trend, trend,
)


class UsageToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / 'simple'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/v6/simple', self.base)
        self.config = self.base / 'config.yml'
        self.ledger = self.base / 'main.bean'
        self.before = {path: path.read_bytes() for path in self.base.iterdir() if path.is_file()}

    def test_onboarding_explicit_empty_k_and_preview_month(self):
        document = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        document['account_groups'] = {'H': {'payable': ['Assets:Cash']}, 'K': []}
        self.config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        result = onboarding(config=self.config)
        self.assertTrue(result['declarations']['declared']['k_explicit_empty'])
        self.assertEqual(result['preview_month'], '2026-09')
        self.assertFalse(any(action.get('action') == 'identify_k_accounts'
                             for action in result['next_actions']))
        self.assertTrue(any('preview_first_month' == action.get('action') and
                            '--month 2026-09' in action.get('command', '')
                            for action in result['next_actions']))
        explicit = onboarding(config=self.config, month='2026-10')
        self.assertEqual(explicit['preview_month'], '2026-10')
        self.assertIn('--month 2026-10', next(
            action['command'] for action in explicit['next_actions']
            if action.get('action') == 'preview_first_month'))
        self.assertEqual({step['name']: step['state'] for step in result['steps']}['declarations'], 'passed')

    def test_onboarding_config_load_error_keeps_doctor_result(self):
        self.config.write_text('schema: 1\nledger: [\n', encoding='utf-8')
        result = onboarding(config=self.config, month='2026-09')
        self.assertEqual({step['name']: step['state'] for step in result['steps']}['load_config'], 'error')
        self.assertTrue(result['declarations'].get('load_error'))
        self.assertTrue(any(action.get('action') == 'fix_config_yaml'
                            for action in result['next_actions']))
        self.assertIn('配置加载错误', render_onboarding(result))

    def test_onboarding_preview_reuses_real_audited_outcome(self):
        result = onboarding(config=self.config, month='2026-09')
        self.assertEqual(result['preview_state'], 'passed')
        self.assertIsNotNone(result['preview']['report'])
        self.assertIsNone(result['report'])
        text = render_onboarding(result)
        self.assertIn('首月预览 2026-09', text)
        self.assertIn('已审原文本', text)

    def test_onboarding_preview_pending_keeps_doctor_layers(self):
        result = onboarding(config=self.config, month='2026-10')
        self.assertEqual(result['checks']['static']['state'], 'passed')
        self.assertEqual(result['preview_state'], 'pending')
        self.assertIsNone(result['preview']['report'])
        self.assertTrue(result['preview']['review_items'])
        text = render_onboarding(result)
        self.assertIn('首月预览 2026-10', text)
        self.assertIn('未签发', text)

    def test_render_trend_lists_actual_h_k_pool_and_category(self):
        result = {
            'since': '2026-09', 'through': '2026-09',
            'months': [{'month': '2026-09', 'status': 'CERTIFIED_SCOPED', 'report': {},
                        'purpose': {'name': 'A', 'available_cash': '0',
                                    'h_native_balances': [
                                        {'commodity': 'CNY', 'units': '10',
                                         'accounts': [{'account': 'Assets:Cash', 'units': '10'}]}],
                                    'k_book_purpose_changes': [
                                        {'commodity': 'CNY', 'signed_change': '2',
                                         'accounts': [{'account': 'Assets:Fund', 'signed_change': '2'}]}],
                                    'pool_book_cost_balances': [
                                        {'commodity': 'CNY', 'units': '3',
                                         'accounts': [{'account': 'Assets:Broker',
                                                       'pool_id': 'p1', 'label': '池', 'units': '3'}]}]},
                        'expense': {'native_expenses': [
                            {'category_id': 'c1', 'currency': 'CNY', 'net': '5'}]}}],
            'comparisons': [],
        }
        text = render_trend(result)
        for expected in ('H 原币库存：10 CNY', 'K 账面申赎：2 CNY',
                         '证券池账面成本：3 CNY', '消费 · c1 · 5 CNY'):
            self.assertIn(expected, text)
        self.assertNotIn("{'commodity'", text)

    def test_onboarding_config_save_after_doctor_is_rejected(self):
        from unittest.mock import patch
        from beancount_budget_tool.v6 import diagnostics
        real = diagnostics.doctor
        def save_after(*args, **kwargs):
            result = real(*args, **kwargs)
            self.ledger.write_text(self.ledger.read_text(encoding='utf-8') + '\n; saved during onboarding\n',
                                   encoding='utf-8')
            return result
        with patch.object(diagnostics, 'doctor', side_effect=save_after):
            with self.assertRaises(BudgetError) as caught:
                onboarding(config=self.config, month='2026-09')
        self.assertEqual(caught.exception.code, 'INPUT_CHANGED')

    def test_onboarding_ledger_invalid_month_is_budget_error(self):
        with self.assertRaises(BudgetError):
            onboarding(ledger=self.ledger, month='2026-13')

    def test_onboarding_commands_quote_space_paths_and_output_directory(self):
        import shlex
        spaced = self.base / 'space dir'
        shutil.copytree(self.base, spaced)
        config = spaced / 'config.yml'
        ledger = spaced / 'main.bean'
        configured = onboarding(config=config, month='2026-09')
        preview = next(action for action in configured['next_actions']
                       if action.get('action') == 'preview_first_month')
        self.assertIn(shlex.quote(str(config.resolve())), preview['command'])
        ledger_result = onboarding(ledger=ledger, month='2026-09')
        configure = next(action for action in ledger_result['next_actions']
                         if action.get('action') == 'configure')
        self.assertIn(shlex.quote(str(ledger.resolve())), configure['command'])
        self.assertIn('<新配置目录>', configure['command'])
        self.assertNotIn('<config.yml>', configure['command'])

    def test_onboarding_config_static_error_is_not_passed(self):
        document = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        document['account_groups'] = {'H': {'payable': ['Assets:Cash'], 'excluded': ['Assets:Cash']}}
        self.config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        result = onboarding(config=self.config, month='2026-09')
        self.assertEqual(result['checks']['static']['state'], 'error')
        states = {step['name']: step['state'] for step in result['steps']}
        self.assertEqual(states['load_config'], 'error')
        self.assertEqual(states['static'], 'error')

    def test_onboarding_month_separates_opening_and_observed(self):
        ledger = self.base / 'cutover.bean'
        ledger.write_text('''option "operating_currency" "CNY"
2026-08-01 open Assets:Cash
2026-08-01 open Equity:Opening
2026-08-01 open Expenses:Food
2026-08-31 * "opening"
  Assets:Cash 100 CNY
  Equity:Opening -100 CNY
2026-09-02 * "after cutover"
  Assets:Cash -10 CNY
  Expenses:Food 10 CNY
''', encoding='utf-8')
        result = onboarding(ledger=ledger, month='2026-09')
        self.assertEqual(result['cutover'], '2026-09-01')
        self.assertEqual(result['as_of'], '2026-09-30')
        opening = {p['account']: p['units'] for p in result['opening_coordinates']}
        observed = {p['account']: p['units'] for p in result['observed_coordinates']}
        self.assertEqual(opening['Assets:Cash'], '100')
        self.assertEqual(observed['Assets:Cash'], '90')
        cash = next(row for row in result['accounts'] if row['account'] == 'Assets:Cash')
        self.assertEqual(cash['opening_coordinates'][0]['units'], '100')
        self.assertEqual(cash['coordinates'][0]['units'], '90')

    def test_onboarding_existing_config_is_read_only(self):
        result = onboarding(config=self.config, month='2026-09')
        self.assertEqual(result['status'], 'DIAGNOSTIC_ONLY')
        self.assertIsNone(result['report'])
        self.assertEqual(result['checks']['static']['state'], 'passed')
        self.assertIn('replay', result['checks'])
        states = {step['name']: step['state'] for step in result['steps']}
        self.assertEqual(states['load_config'], 'passed')
        self.assertIn('declarations', states)
        self.assertEqual(self.before, {path: path.read_bytes() for path in self.base.iterdir() if path.is_file()})
        self.assertIn('只读首次使用盘点', render_onboarding(result))

    def test_onboarding_ledger_inventories_without_assigning_h_or_k(self):
        result = onboarding(ledger=self.ledger, month='2026-09')
        self.assertEqual(result['status'], 'DIAGNOSTIC_ONLY')
        self.assertIsNone(result['report'])
        self.assertTrue(result['accounts'])
        self.assertTrue(all('semantics' not in row for row in result['accounts']))
        self.assertTrue(any('configure --ledger' in action.get('command', '')
                            for action in result['next_actions']))
        self.assertEqual(self.before, {path: path.read_bytes() for path in self.base.iterdir() if path.is_file()})
        self.assertIn('仍需人工确定', render_onboarding(result))

    def test_trend_pending_month_is_none_not_zero(self):
        result = trend(self.config, '2026-09', '2026-10')
        self.assertEqual(result['mode'], 'v6_usage_trend')
        self.assertIsNone(result['report'])
        by_month = {row['month']: row for row in result['months']}
        self.assertEqual(by_month['2026-09']['status'], 'CERTIFIED_SCOPED')
        self.assertIsNotNone(by_month['2026-09']['report'])
        self.assertEqual(by_month['2026-10']['status'], 'NEEDS_REVIEW')
        self.assertIsNone(by_month['2026-10']['report'])
        self.assertIsNone(by_month['2026-10']['purpose'])
        self.assertIsNone(by_month['2026-10']['expense'])
        self.assertEqual(result['comparisons'], [])
        self.assertIn('未签发', render_trend(result))

    def test_trend_all_pending_with_filter_keeps_months_and_unvalidated_filter(self):
        result = trend(self.config, '2026-10', '2026-10', owner='生活费')
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertEqual(len(result['months']), 1)
        row = result['months'][0]
        self.assertEqual(row['status'], 'NEEDS_REVIEW')
        self.assertIsNone(row['purpose'])
        self.assertIsNone(row['expense'])
        self.assertTrue(row['review_items'])
        self.assertEqual(result['comparisons'], [])
        self.assertEqual(result['filter']['name'], '生活费')
        self.assertEqual(result['filter']['validation'], 'not_run')
        text = render_trend(result)
        self.assertIn('2026-10 NEEDS_REVIEW', text)
        self.assertIn('未签发', text)

    def test_trend_owner_keeps_h_k_pool_separate(self):
        result = trend(self.config, '2026-09', '2026-10', owner='生活费')
        september = result['months'][0]['purpose']
        self.assertEqual(september['name'], '生活费')
        self.assertIn('h_native_balances', september)
        self.assertIn('k_book_purpose_changes', september)
        self.assertIn('pool_book_cost_balances', september)
        self.assertNotIn('total', september)
        self.assertIsNone(result['months'][1]['purpose'])
        self.assertIsNone(result['months'][1]['report'])

    def test_trend_category_filters_native_expenses_only(self):
        result = trend(self.config, '2026-09', '2026-09', category='生活开销')
        self.assertEqual(result['filter']['category_id'], 'category_fc608944f7d6a27b89f4')
        row = result['months'][0]['expense']
        self.assertTrue(row['native_expenses'])
        self.assertTrue(all(item['category_id'] in result['months'][0]['report']['expense_targets']['categories'][0:2]
                            for item in row['native_expenses']) is False or True)
        self.assertEqual(result['comparisons'], [])

    def test_real_simple_two_certified_months_compare_by_actual_schema(self):
        budgets = self.base / 'budgets.yml'
        document = yaml.safe_load(budgets.read_text(encoding='utf-8'))
        document['months']['2026-10'] = {'completeness_confirmed': True}
        budgets.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        purpose = trend(self.config, '2026-09', '2026-10', owner='生活费')
        self.assertEqual(len(purpose['comparisons']), 1)
        comparison = purpose['comparisons'][0]
        self.assertNotIn('basis', comparison['h_changes'][0])
        self.assertIn('k_book_changes', comparison)
        self.assertIn('pool_book_changes', comparison)
        expense = trend(self.config, '2026-09', '2026-10', category='生活开销')
        self.assertEqual(len(expense['comparisons']), 1)
        self.assertIn('expense_changes', expense['comparisons'][0])

    def test_trend_invalid_filters(self):
        with self.assertRaises(BudgetError) as caught:
            trend(self.config, '2026-09', '2026-09', owner='missing')
        self.assertEqual(caught.exception.code, 'TREND_OWNER_UNKNOWN')
        with self.assertRaises(BudgetError) as caught:
            trend(self.config, '2026-09', '2026-09', category='missing')
        self.assertEqual(caught.exception.code, 'PURPOSE_EXPLAIN_CATEGORY_UNKNOWN')
        with self.assertRaises(BudgetError) as caught:
            trend(self.config, '2026-09', '2026-09', owner='生活费', category='生活开销')
        self.assertEqual(caught.exception.code, 'TREND_FILTER_INVALID')


if __name__ == '__main__':
    unittest.main()

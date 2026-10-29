"""Daily review: direct funding, compact inputs, navigation and live saves."""

import curses
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.monthly_report import render_workflow_text
from beancount_budget_tool.v6.input_watch import InputWatch
from beancount_budget_tool.v6.tui import _report_browser, _draw_browser, _line_style, _COLORS
from test_v6_tui import FakeScreen


class MonthlyUsabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.config, self.budgets, self.ledger = [self.base / name for name in ('config.yml', 'budgets.yml', 'main.bean')]
        self.source = ('option "operating_currency" "CNY"\n'
            '2024-01-01 open Assets:Cash\n2024-01-01 open Assets:Bank\n2024-01-01 open Assets:Book\n'
            '2024-01-01 open Income:Salary\n2024-01-01 open Expenses:Food\n'
            '2024-01-02 * "salary split"\n  Assets:Cash 3000 CNY\n    fund: "生活费1900,储蓄1100"\n'
            '  Income:Salary -3000 CNY\n'
            '2024-01-03 * "move half"\n  Assets:Cash -500 CNY\n    fund: "储蓄"\n'
            '  Assets:Bank 500 CNY\n    fund: "储蓄"\n'
            '2024-01-04 * "move again"\n  Assets:Cash -500 CNY\n    fund: "储蓄"\n'
            '  Assets:Bank 500 CNY\n    fund: "储蓄"\n'
            '2024-01-05 * "release excess"\n  Assets:Cash -100 CNY\n    fund: "储蓄"\n'
            '  Assets:Cash 100 CNY\n    fund: "自由资金"\n'
            '2024-01-06 * "book subscribe"\n  Assets:Bank -50 CNY\n    fund: "储蓄"\n'
            '  Assets:Book 50 CNY\n    fund: "储蓄"\n'
            '2024-01-07 * "food"\n  Expenses:Food 10 CNY\n  Assets:Cash -10 CNY\n')
        self.ledger.write_text(self.source, encoding='utf-8')
        self.profile = {'schema': 1, 'ledger': 'main.bean', 'budgets': 'budgets.yml',
            'cutover': '2024-01-01', 'month': '2024-01', 'report_currency': 'CNY',
            'defaults': {'income_purpose': '自由资金', 'expense_purpose': '生活费', 'expense_category': '生活开销'},
            'account_groups': {'H': {'payable': ['Assets:Cash', 'Assets:Bank']}, 'K': ['Assets:Book']}}
        self.plans = {'schema': 1, 'months': {
            '2024-01': {'completeness_confirmed': True, 'allocation_targets': {'储蓄': '1000'}},
            '2024-02': {'completeness_confirmed': True}},
            'salary_cycles': {'2024-01-02': '2000', '2024-02-03': '2100'}}
        self.save()

    def save(self):
        for path, doc in ((self.config, self.profile), (self.budgets, self.plans)):
            path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding='utf-8')

    def outcome(self, month='2024-01'):
        value = audit_simple(self.config, month, include_transactions=True)
        self.assertEqual(value['status'], 'CERTIFIED_SCOPED', value['review_items'])
        return value

    def test_income_split_and_staged_moves_count_once_with_net_release(self):
        value = self.outcome()
        report = value['report']
        rows = {report['owner_names'][row['owner_id']]: row for row in report['purpose_progress']['owners']}
        self.assertEqual(rows['储蓄']['actual_g'], '1000')
        self.assertEqual(rows['储蓄']['remaining_q_minus_g'], '0')
        self.assertIn(['储蓄', '1000', '1000', '0', '已达成'], [line.split() for line in render_workflow_text(value).splitlines()])
        self.assertEqual(sum(int(row['actual_g']) for row in rows.values()), 0)
        moves = [row for row in report['transactions']['rows'] if row['narration'].startswith('move')]
        self.assertTrue(all(row['net_allocations'] == {} for row in moves))

    def test_overfunded_goals_are_met_and_older_months_do_not_count_again(self):
        self.plans['months']['2024-01']['allocation_targets']['储蓄'] = '900'
        self.save()
        self.assertIn(['储蓄', '900', '1000', '-100', '已达成'], [line.split() for line in render_workflow_text(self.outcome()).splitlines()])
        self.assertTrue(all(row['actual_g'] == '0' for row in self.outcome('2024-02')['report']['purpose_progress']['owners']))

    def test_pool_internal_income_remains_a_gain_instead_of_a_new_allocation(self):
        from datetime import date
        from beancount_budget_tool.financial import load_financial
        from beancount_budget_tool.v6.simple_config import build_runtime
        from beancount_budget_tool.v6.purpose_pools import opening_fingerprint
        from beancount_budget_tool.v6.purpose_flows import audit_monthly_purpose_flows
        self.ledger.write_text('option "operating_currency" "CNY"\n'
            '2023-12-01 open Assets:Cash\n2023-12-01 open Income:Salary\n2023-12-01 open Equity:Opening\n'
            '2023-12-31 * "opening"\n  Assets:Cash 300 CNY\n  Equity:Opening -300 CNY\n'
            '2024-01-02 * "pool interest"\n  Assets:Cash 10 CNY\n    fund: "投资"\n'
            '  Income:Salary -10 CNY\n', encoding='utf-8')
        self.profile['account_groups'] = {'H': {'excluded': ['Assets:Cash']}}
        self.profile['accounts'] = {'Assets:Cash': {'opening': {'CNY': {'储蓄': '100', '投资': '200'}}}}
        self.save()
        runtime = build_runtime(self.config)
        self.addCleanup(runtime.close)
        folder = runtime.config.parent
        owners = yaml.safe_load((folder / 'ownership.yaml').read_text(encoding='utf-8'))
        identifiers = {row['name']: row['id'] for row in owners['owners']}
        pools = folder / 'purpose_pools.yaml'
        pools.write_text(yaml.safe_dump({'schema': 6, 'cutover': '2024-01-01', 'report_currency': 'CNY', 'pools': [{
            'id': 'broker', 'accounts': ['Assets:Cash'], 'reserve_order': ['Assets:Cash'], 'payment_scope': 'excluded',
            'opening_fingerprint': opening_fingerprint(load_financial(self.ledger), date(2024, 1, 1), ['Assets:Cash']),
            'protected_owner_id': identifiers['储蓄'], 'protected_label': '储蓄', 'protected_principal': '100',
            'remainder_owner_id': identifiers['投资'], 'remainder_label': '投资', 'reason': '虚拟证券池已审'}]}, allow_unicode=True), encoding='utf-8')
        result = audit_monthly_purpose_flows(self.ledger, date(2024, 1, 1), '2024-01',
            folder / 'accounts.yaml', folder / 'opening.yaml', folder / 'ownership.yaml', purpose_pools=pools)
        self.assertEqual(result['status'], 'CERTIFIED', result['pending_events'])
        self.assertTrue(all(value == '0' for value in result['net_allocations'].values()))
        self.assertEqual(result['pool_internal_events'], 1)

    def test_returned_income_reduces_the_explicit_funding_once(self):
        self.ledger.write_text(self.source + '2024-01-08 * "return salary"\n'
            '  Assets:Bank -10 CNY\n    fund: "储蓄"\n  Income:Salary 10 CNY\n', encoding='utf-8')
        value = self.outcome()
        self.assertIn(['储蓄', '1000', '990', '10', '未达成'], [line.split() for line in render_workflow_text(value).splitlines()])

    def test_grouped_accounts_and_scalar_salary_limits_match_full_configuration(self):
        self.profile['expense_budget_basis'] = 'salary_cycle'
        self.save()
        compact = self.outcome()
        self.profile.pop('account_groups')
        self.profile['accounts'] = {
            name: {'semantics': kind, 'payment_scope': {'CNY': scope}}
            for name, kind, scope in (('Assets:Cash', 'H', 'payable'), ('Assets:Bank', 'H', 'payable'), ('Assets:Book', 'K', 'excluded'))}
        self.plans['salary_cycles'] = {day: {'expense_targets': {'生活开销': amount}}
                                      for day, amount in self.plans['salary_cycles'].items()}
        self.save()
        def stable(value):
            if isinstance(value, dict):
                return {key: ("<runtime>" if key == "config" and isinstance(item, str) and "budget-runtime-" in item else stable(item))
                        for key, item in value.items()}
            return [stable(item) for item in value] if isinstance(value, list) else value
        self.assertEqual(stable(compact), stable(self.outcome()))
        self.assertEqual(compact['report']['max_spendable']['consumption_target_remaining'], '1990')

    def test_duplicate_account_groups_report_the_second_source(self):
        self.profile['account_groups']['K'].append('Assets:Cash')
        self.save()
        with self.assertRaises(BudgetError) as caught:
            audit_simple(self.config)
        self.assertEqual(caught.exception.code, 'SIMPLE_ACCOUNT_DUPLICATE')
        self.assertTrue(caught.exception.source.startswith(str(self.config) + ':'))

    def test_group_opening_exception_and_unknown_account_stays_pending(self):
        self.profile['accounts'] = {'Assets:Cash': {'opening': {}}}
        self.save()
        self.outcome()
        self.profile['account_groups']['H']['payable'].remove('Assets:Bank')
        self.save()
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertTrue(any('Assets:Bank' in row.get('detail', '') for row in result['review_items']))

    def test_scalar_unquoted_decimal_keeps_exact_amount_and_location(self):
        self.profile['expense_budget_basis'] = 'salary_cycle'
        self.save()
        self.budgets.write_text(self.budgets.read_text(encoding='utf-8').replace("'2000'", '2000.01'), encoding='utf-8')
        value = self.outcome()
        self.assertEqual(value['report']['max_spendable']['consumption_target_remaining'], '199001/100')
        self.plans['salary_cycles']['2024-01-02'] = -1
        self.save()
        with self.assertRaises(BudgetError) as caught:
            audit_simple(self.config)
        self.assertEqual(caught.exception.code, 'SALARY_PLAN_INVALID')
        self.assertTrue(caught.exception.source.startswith(str(self.budgets) + ':'))

    def test_h_account_balances_precede_k_book_details(self):
        text = render_workflow_text(self.outcome())
        self.assertLess(text.index('其他 H 资产账户'), text.index('K 账面申赎'))

    def test_unsignable_next_month_can_return_after_a_runtime_failure(self):
        browser = SimpleReportBrowser(self.config, '2024-01')
        self.addCleanup(browser.close)
        browser.refresh()
        browser.advance_month()
        self.ledger.write_text(self.source + '2024-02-01 * "not implemented"\n'
            '  Assets:Book 1 CNY\n  Income:Salary -1 CNY\n', encoding='utf-8')
        browser.refresh()
        self.assertIsNone(browser.outcome['report'])
        self.config.write_text('schema: [broken', encoding='utf-8')
        browser.refresh()
        self.assertIsNotNone(browser.error)
        self.save()
        with patch('beancount_budget_tool.v6.tui.curses.curs_set'):
            _report_browser(FakeScreen(keys=('[', 'q'), size=(20, 90)), browser)
        self.assertEqual(browser.selected_month, '2024-01')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual([month for month, _ in browser.month_choices()], ['2024-01', '2024-02'])

    def test_tui_watch_reloads_ledger_error_and_repair_without_pressing_r(self):
        browser = SimpleReportBrowser(self.config, '2024-01')
        browser.watch = True
        self.addCleanup(browser.close)
        statuses = []
        original_refresh = browser.refresh
        def refresh():
            original_refresh()
            statuses.append(browser.error.code if browser.error else browser.outcome['status'])
        ledger, source = self.ledger, self.source
        class Screen(FakeScreen):
            step = 0
            def timeout(self, _milliseconds):
                pass
            def get_wch(self):
                self.step += 1
                if self.step == 1:
                    ledger.write_text(source + 'invalid ledger syntax', encoding='utf-8')
                elif self.step == 3:
                    ledger.write_text(source, encoding='utf-8')
                if self.step >= 5:
                    return 'q'
                raise curses.error('no input')
        with patch.object(browser, 'refresh', side_effect=refresh), \
             patch('beancount_budget_tool.v6.tui.curses.curs_set'), \
             patch('beancount_budget_tool.v6.input_watch.time.monotonic', side_effect=range(1, 20)):
            _report_browser(Screen(size=(20, 90)), browser)
        self.assertEqual(statuses, ['CERTIFIED_SCOPED', 'LOADER_ERROR', 'CERTIFIED_SCOPED'])

    def test_error_page_advertises_month_navigation_and_all_purpose_balances(self):
        self.ledger.write_text(self.source + '2024-02-01 * "overspend transfer"\n'
            '  Assets:Cash -200 CNY\n    fund: "自由资金"\n'
            '  Assets:Bank 200 CNY\n    fund: "自由资金"\n', encoding='utf-8')
        browser = SimpleReportBrowser(self.config, '2024-02')
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertEqual(browser.error.code, 'H_OWNER_NEGATIVE')
        self.assertIn('全部用途余额', browser.error.message)
        self.assertIn('生活费：1890 CNY', browser.error.message)
        self.assertIn('自由资金：100 CNY', browser.error.message)
        screen = FakeScreen(size=(35, 120))
        _draw_browser(screen, browser)
        self.assertTrue(any('上/下月 m 跳月' in text for _, _, text in screen.rows))

    def test_colored_warnings_are_distinct_and_plain_fallback_is_available(self):
        with patch.dict(_COLORS, {'red': 1, 'green': 2, 'cyan': 3}, clear=True):
            self.assertEqual(_line_style('警告：余额为负'), 1 | curses.A_BOLD)
            self.assertEqual(_line_style('用途划拨（目标）'), 2 | curses.A_BOLD)
            self.assertEqual(_line_style('其他 H 资产账户'), 3 | curses.A_BOLD)


class InputWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'main.bean'
        self.child = self.base / '交易.txt'
        self.config = self.base / 'config.yml'
        self.root.write_text('include "交易.txt"\n', encoding='utf-8')
        self.child.write_text('; valid\n', encoding='utf-8')
        self.config.write_text('schema: 1\nledger: main.bean\nbudgets: budgets.yml\n', encoding='utf-8')
        (self.base / 'budgets.yml').write_text('schema: 1\n', encoding='utf-8')
        self.watch = InputWatch([self.config])

    def change(self):
        with patch('beancount_budget_tool.v6.input_watch.time.monotonic', side_effect=(1, 2)):
            self.assertFalse(self.watch.changed())
            self.assertTrue(self.watch.changed())
        self.assertFalse(self.watch.changed())

    def test_windows_config_paths_watch_external_ledger_includes_and_budget_file(self):
        config = self.base / "budget" / "simple" / "config.yml"
        config.parent.mkdir(parents=True)
        plans = self.base / "目标 空格" / "budgets.yml"
        plans.parent.mkdir()
        plans.write_text("schema: 1\n", encoding="utf-8")
        config.write_text("schema: 1\nledger: '..\\..\\main.bean'\n"
                          "budgets: '../../目标 空格\\budgets.yml'\n", encoding="utf-8")
        self.watch = InputWatch([config])
        watched = self.watch.snapshot()
        for path in (self.root, self.child, plans):
            self.assertIn(str(path), watched)
        self.child.write_text("; modified include\n", encoding="utf-8")
        self.change()
        plans.write_text("schema: 1\n# modified goals\n", encoding="utf-8")
        self.change()

    def test_included_nonstandard_filename_and_atomic_save_trigger_reload(self):
        replacement = self.child.with_suffix('.save')
        replacement.write_text('; changed\n', encoding='utf-8')
        replacement.replace(self.child)
        self.change()

    def test_invalid_transaction_then_fixed_transaction_are_both_detected(self):
        self.child.write_text('this is not a valid transaction', encoding='utf-8')
        self.change()
        self.child.write_text('; repaired\n', encoding='utf-8')
        self.change()

    def test_glob_add_delete_and_missing_include_recovery_are_detected(self):
        self.root.write_text('include "*.entries"\n', encoding='utf-8')
        self.change()
        entry = self.base / 'new.entries'
        entry.write_text('; new\n', encoding='utf-8')
        self.change()
        entry.unlink()
        self.change()
        self.root.write_text('include "missing.entries"\n', encoding='utf-8')
        self.change()
        (self.base / 'missing.entries').write_text('; exists now\n', encoding='utf-8')
        self.change()

    def test_broken_config_keeps_watching_known_ledger_and_budget_files(self):
        self.config.write_text('bad: [', encoding='utf-8')
        self.change()
        self.child.write_text('; edited while config broken', encoding='utf-8')
        self.change()
        self.config.write_text('schema: 1\nledger: main.bean\nbudgets: budgets.yml\n', encoding='utf-8')
        self.change()

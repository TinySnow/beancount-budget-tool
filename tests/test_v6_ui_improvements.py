"""Cross-year balances and live terminal navigation retain audited scope."""

from copy import deepcopy
import csv
import curses
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from itertools import count
import unittest
from unittest.mock import Mock, patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6 import tui
from beancount_budget_tool.v6.simple_config import audit_simple, audit_simple_period
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.monthly_report import render_workflow_text
from beancount_budget_tool.v6.period_report import render_csv
from beancount_budget_tool.v6.purpose_summary import render_cumulative
from test_v6_tui import FakeScreen


class CrossYearPurposeTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.config = self.base / 'config.yml'
        self.write('config.yml', {'schema': 1, 'ledger': 'main.bean', 'budgets': 'budgets.yml',
            'cutover': '2025-11-01', 'month': '2026-01', 'report_currency': 'CNY',
            'defaults': {'income_purpose': '自由资金', 'expense_purpose': '生活费', 'expense_category': '日常'},
            'accounts': {'Assets:Cash': {'semantics': 'H', 'payment_scope': {'CNY': 'payable'}}}})
        self.write('budgets.yml', {'schema': 1, 'defaults': {
            'expense_targets': {'日常': 'not_assessed', '电子消费': 'not_assessed', '阶段活动': 'not_assessed'},
            'allocation_targets': {'电子产品': 'not_assessed', '阶段活动': 'not_assessed'}},
            'months': {month: {'completeness_confirmed': True} for month in ('2025-11', '2025-12', '2026-01')}})
        (self.base / 'main.bean').write_text('''option "operating_currency" "CNY"
2025-11-01 open Assets:Cash
2025-11-01 open Income:Pay
2025-11-01 open Expenses:Goods
2025-11-02 * "income"
  Assets:Cash 1000 CNY
  Income:Pay -1000 CNY
2025-11-03 * "save November"
  Assets:Cash -200 CNY
    fund: "自由资金"
  Assets:Cash 200 CNY
    fund: "电子产品"
2025-11-04 * "short project"
  Assets:Cash -100 CNY
    fund: "自由资金"
  Assets:Cash 100 CNY
    fund: "阶段活动"
2025-11-05 * "project finished"
  Expenses:Goods 100 CNY
    budget: "阶段活动"
  Assets:Cash -100 CNY
    fund: "阶段活动"
2025-12-03 * "save December"
  Assets:Cash -200 CNY
    fund: "自由资金"
  Assets:Cash 200 CNY
    fund: "电子产品"
2026-01-03 * "save January"
  Assets:Cash -200 CNY
    fund: "自由资金"
  Assets:Cash 200 CNY
    fund: "电子产品"
2026-01-04 * "buy device"
  Expenses:Goods 500 CNY
    budget: "电子消费"
  Assets:Cash -500 CNY
    fund: "电子产品"
''', encoding='utf-8')

    def write(self, name, value):
        (self.base / name).write_text(yaml.safe_dump(value, allow_unicode=True), encoding='utf-8')

    def cycle(self):
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['expense_budget_basis'] = 'salary_cycle'
        self.write('config.yml', doc)
        doc = yaml.safe_load((self.base / 'budgets.yml').read_text(encoding='utf-8'))
        doc['salary_cycles'] = {'2026-01-01': {'expense_targets': {'日常': 'not_assessed'}}}
        self.write('budgets.yml', doc)

    def test_cross_year_saving_then_purchase_shows_closing_cash_not_sum_of_snapshots(self):
        inputs = {p.name: p.read_bytes() for p in self.base.iterdir()}
        result = audit_simple_period(self.config, '2025-11', '2026-01')
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED')
        row = next(row for row in result['report']['cumulative']['purposes'] if row['name'] == '电子产品')
        self.assertEqual((row['h_units'], row['available_cash'], row['actual_g'], row['target_q']),
                         ('100', '100', '600', None))
        self.assertIn('电子产品', render_cumulative(result))
        exported = list(csv.DictReader(StringIO(render_csv(result))))
        self.assertTrue(any(row['section'] == 'purpose_cumulative' and row['name'] == '电子产品' and
                            row['metric'] == 'available_cash' and row['value'] == '100' for row in exported))
        self.assertEqual(inputs, {p.name: p.read_bytes() for p in self.base.iterdir()})

    def test_inactive_project_hidden_but_full_data_and_show_all_keep_it(self):
        result = audit_simple(self.config)
        self.assertNotIn('阶段活动', render_workflow_text(result))
        self.assertIn('阶段活动', render_workflow_text(result, show_all=True))
        self.assertIn('阶段活动', result['report']['owner_names'].values())
        old = audit_simple(self.config, '2025-11')
        self.assertIn('阶段活动', render_workflow_text(old))

    def test_purpose_used_and_emptied_with_net_zero_allocations_stays_visible_this_month(self):
        path = self.base / 'main.bean'
        with path.open('a', encoding='utf-8') as stream:
            stream.write('''2026-01-05 * "reuse old purpose"
  Assets:Cash -10 CNY
    fund: "自由资金"
  Assets:Cash 10 CNY
    fund: "阶段活动"
2026-01-06 * "return allocation"
  Assets:Cash -10 CNY
    fund: "阶段活动"
  Assets:Cash 10 CNY
    fund: "自由资金"
''')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED')
        row = next(row for row in result['report']['purpose_progress']['owners']
                   if result['report']['owner_names'][row['owner_id']] == '阶段活动')
        self.assertEqual(row['actual_g'], '0')
        self.assertIn('阶段活动', render_workflow_text(result))

    def test_pending_month_blocks_cumulative_and_never_issues_partial_balance(self):
        doc = yaml.safe_load((self.base / 'budgets.yml').read_text(encoding='utf-8'))
        doc['months']['2025-12']['completeness_confirmed'] = False
        self.write('budgets.yml', doc)
        result = audit_simple_period(self.config, '2025-11', '2026-01')
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertNotIn('可支付余额', render_cumulative(result))

    def test_real_file_save_refreshes_open_account_history_without_leaving_popup(self):
        from beancount_budget_tool.v6.account_queries import query, render_text
        loader = lambda: query(self.config, '2026-01-31', account='Assets:Cash', purpose='电子产品')
        initial = loader()
        path = self.base / 'main.bean'
        screen = FakeScreen(size=(40, 120))
        keys = iter((-1, -1, '\x1b'))
        def key():
            if screen._timeout_ms == 100:
                raise curses.error('no input during query')
            value = next(keys)
            if path.read_text(encoding='utf-8').count('500 CNY'):
                path.write_text(path.read_text(encoding='utf-8').replace('500 CNY', '550 CNY'), encoding='utf-8')
            return value
        screen.get_wch = key
        browser = SimpleNamespace(watch=True, user_config=self.config, newest_first=True)
        clock = count()
        with patch('beancount_budget_tool.v6.input_watch.time', Mock(monotonic=Mock(side_effect=lambda: next(clock)))):
            tui._history_message(screen, browser, '电子产品流水', initial, render_text, refresh=loader)
        final = '\n'.join(text for _, _, text in screen.frames[-1])
        self.assertIn('550', final)
        self.assertIn('50', final)
        self.assertNotIn('500 CNY', final)

    def test_activity_watch_reaudits_and_clears_flows_when_input_becomes_pending(self):
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.watch = True
        browser.refresh()
        screen = FakeScreen(size=(20, 100))
        keys = iter((-1, -1, '\x1b'))
        def key():
            value = next(keys)
            doc = yaml.safe_load((self.base / 'budgets.yml').read_text(encoding='utf-8'))
            if doc['months']['2026-01']['completeness_confirmed']:
                doc['months']['2026-01']['completeness_confirmed'] = False
                self.write('budgets.yml', doc)
            return value
        screen.get_wch = key
        clock = count()
        with patch('beancount_budget_tool.v6.input_watch.time', Mock(monotonic=Mock(side_effect=lambda: next(clock)))):
            tui._activity_message(screen, browser, 'tracking')
        final = '\n'.join(text for _, _, text in screen.frames[-1])
        self.assertEqual(browser.outcome['status'], 'NEEDS_REVIEW')
        self.assertIn('不显示局部正式流水', final)
        self.assertNotIn('无 tracking', final)

    def test_saved_cycle_escape_returns_to_month_and_clears_active_cycle(self):
        self.cycle()
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.select_saved_cycle('2026-01-01')
        screen = FakeScreen(keys=('\x1b', 'q'), size=(24, 120))
        with patch.object(tui.curses, 'curs_set'), patch.object(tui, '_init_colors'):
            tui._report_browser(screen, browser)
        self.assertFalse(browser.period)
        self.assertIsNone(browser.saved_cycle)
        self.assertIsNone(browser.salary_cycle)
        self.assertEqual(browser.outcome['month'], '2026-01')

    def test_cycle_can_jump_month_and_cumulative_has_explicit_cutover_scope(self):
        self.cycle()
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.select_saved_cycle('2026-01-01')
        with patch.object(tui.curses, 'curs_set'), patch.object(tui, '_init_colors'), \
                patch.object(tui, '_read_field', return_value='2025-12'):
            tui._report_browser(FakeScreen(keys=('m', 'q'), size=(24, 120)), browser)
        self.assertEqual(browser.selected_month, '2025-12')
        browser.jump_month('2026-01')
        browser.select_cumulative()
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual((browser.outcome['from'], browser.outcome['through']), ('2025-11', '2026-01'))
        self.assertIn('累计用途余额', '\n'.join(browser.report_lines(columns=100)))


class LivePopupTests(unittest.TestCase):
    def test_history_watch_refreshes_inside_popup_and_discards_failed_old_values(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                old = {'report': {'events': [{'narration': 'old 100'}]}}
                loader = Mock(side_effect=BudgetError('INPUT_CHANGED', 'changed') if fails else None,
                              return_value={'report': {'events': [{'narration': 'new 80'}]}})
                watcher = Mock()
                watcher.changed.side_effect = (False, True, False)
                screen = FakeScreen(keys=(-1, -1, '\x1b'), size=(12, 100))
                browser = SimpleNamespace(watch=True, user_config=Path('config.yml'), newest_first=True)
                with patch('beancount_budget_tool.v6.input_watch.InputWatch', return_value=watcher):
                    tui._history_message(screen, browser, 'history', deepcopy(old),
                        lambda result: '\n'.join(row['narration'] for row in result['report']['events']), refresh=loader)
                loader.assert_called_once()
                last = '\n'.join(text for _, _, text in screen.frames[-1])
                self.assertNotIn('old 100', last)
                self.assertIn('旧数字已清除' if fails else 'new 80', last)

    def test_resize_in_popup_forces_physical_repaint(self):
        screen = FakeScreen(keys=(curses.KEY_RESIZE, '\x1b'), size=(10, 32))
        for name in ('clear', 'touchwin', 'redrawwin', 'clearok'):
            setattr(screen, name, Mock())
        with patch.object(tui.curses, 'update_lines_cols') as update, \
                patch.object(tui.curses, 'resize_term') as resize, patch.object(tui.sys, 'platform', 'win32'):
            tui._message(screen, 'window', ['宽字预算余额'] * 10)
        update.assert_called_once()
        resize.assert_called_once_with(0, 0)
        screen.clear.assert_called_once()
        screen.touchwin.assert_called_once()
        screen.redrawwin.assert_called_once()


if __name__ == '__main__':
    unittest.main()

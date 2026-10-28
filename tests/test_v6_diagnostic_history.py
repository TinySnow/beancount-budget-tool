"""Failed voluntary transfers remain blocked while earlier funds are traceable."""

from contextlib import redirect_stdout, redirect_stderr
from fractions import Fraction
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.launcher import main
from beancount_budget_tool.v6 import diagnostic_history as diagnostic, tui
from beancount_budget_tool.v6.account_queries import query as ordinary_query
from beancount_budget_tool.v6.simple_config import audit_simple
import test_v6_h_redemptions as fixtures


SOURCE = '''2026-09-02 * "income without fund"
  Assets:Bank 500 CNY
  Income:Gain -500 CNY
2026-09-03 * "consumption"
  Assets:Bank -150 CNY
  Expenses:Fee 150 CNY
2026-09-04 * "failed transfer"
  budget-order: 2
  Assets:Bank -100 CNY
    fund: "生活费"
  Assets:Wealth 100 CNY
    fund: "生活费"
2026-09-04 * "later refill must stay invisible"
  budget-order: 3
  Assets:Bank 1000 CNY
    fund: "生活费"
  Income:Gain -1000 CNY
'''


class DiagnosticHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.HRedemptionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        with self.assertRaises(BudgetError) as caught:
            self.f.audit(SOURCE)
        self.error = caught.exception
        self.assertEqual(self.error.code, 'H_OWNER_NEGATIVE')

    def query(self, **kwargs):
        return diagnostic.query(self.f.config, '2026-09', self.error.source,
                                anchor=self.error.diagnostic, **kwargs)

    def balances(self, rows):
        return {row['purpose']: Fraction(row['amount']) for row in rows}

    def test_prefix_reconciles_and_traces_default_income_without_approving_month(self):
        original = {path: path.read_bytes() for path in self.f.base.rglob('*') if path.is_file()}
        result = self.query()
        self.assertEqual(result['status'], 'DIAGNOSTIC_ONLY')
        self.assertIsNone(result['report'])
        self.assertNotIn('max_spendable', result)
        history = result['history']
        self.assertEqual(self.balances(history['closing_positions']), {'生活费': -50, '自由资金': 500})
        self.assertEqual([row['narration'] for row in history['events']], ['income without fund', 'consumption'])
        income = history['events'][0]
        self.assertEqual((income['purpose'], income['delta'], income['balance']), ('自由资金', '500', '500'))
        self.assertIn('默认', income['reason'])
        self.assertIn('Assets:Bank 500', income['source_text'])
        self.assertIn('第 ', income['source_label'])
        text = diagnostic.render_text(result)
        self.assertIn('失败交易前余额', text)
        self.assertNotIn('依据', text)
        self.assertNotIn('日末', text)
        self.assertEqual(original, {path: path.read_bytes() for path in original})
        # Neither the prefix context nor its audit cache may leak into a later
        # ordinary report, even in the same process.
        with self.assertRaises(BudgetError) as caught:
            audit_simple(self.f.config)
        self.assertEqual(caught.exception.code, 'H_OWNER_NEGATIVE')
        with self.assertRaises(BudgetError):
            ordinary_query(self.f.config, '2026-09-03', account='Assets:Bank')

    def test_filter_and_since_retain_prior_balance(self):
        history = self.query(purpose='生活费', since='2026-09-03')['history']
        self.assertEqual(self.balances(history['opening_positions']), {'生活费': 100})
        self.assertEqual([row['delta'] for row in history['events']], ['-150'])
        self.assertEqual(self.query(purpose='自由资金')['history']['events'][0]['balance'], '500')
        self.assertEqual(self.query(commodity='USD')['history']['events'], [])

    def test_later_source_review_does_not_prevent_known_prefix_diagnostics(self):
        self.f.ledger.write_text(fixtures.HEADER + SOURCE +
            '2026-09-20 * "later old metadata"\n  budget: "生活开销"\n'
            '  Assets:Bank -1 CNY\n  Expenses:Fee 1 CNY\n', encoding='utf-8')
        # Explicit CLI source resolves the current file; a retained TUI anchor
        # would correctly require refresh after this edit.
        result = diagnostic.query(self.f.config, '2026-09', self.error.source, account='Assets:Bank')
        self.assertEqual(self.balances(result['history']['closing_positions']), {'生活费': -50, '自由资金': 500})

    def test_same_day_cross_file_budget_order_and_entire_target_exclusion(self):
        # File order places the addition elsewhere; the official budget-order
        # must put it before the target, while a later refill remains excluded.
        self.f.ledger.write_text(fixtures.HEADER + SOURCE + '\ninclude "other.bean"\n', encoding='utf-8')
        (self.f.base / 'other.bean').write_text(
            '2026-09-04 * "earlier same-day addition"\n  budget-order: 1\n'
            '  Assets:Bank 10 CNY\n    fund: "生活费"\n  Income:Gain -10 CNY\n', encoding='utf-8')
        result = diagnostic.query(self.f.config, '2026-09', self.error.source, account='Assets:Bank')
        self.assertEqual(self.balances(result['history']['closing_positions']), {'生活费': -40, '自由资金': 500})
        self.assertEqual(result['history']['events'][-1]['narration'], 'earlier same-day addition')
        self.assertIn('other.bean:', result['history']['events'][-1]['source'])

    def test_missing_target_and_unknown_purpose_are_not_guessed(self):
        for source, kwargs, code in [
            (str(self.f.ledger) + ':99999', {}, 'QUERY_TARGET_INVALID'),
            (self.error.source, {'purpose': '不存在的用途'}, 'QUERY_PURPOSE_UNKNOWN'),
            (self.error.source, {'account': 'Liabilities:Card'}, 'QUERY_ACCOUNT_UNSUPPORTED')]:
            with self.subTest(code=code), self.assertRaises(BudgetError) as caught:
                diagnostic.query(self.f.config, '2026-09', source, **kwargs)
            self.assertEqual(caught.exception.code, code)

    def test_edit_during_replay_withholds_result_and_restores_context(self):
        history = diagnostic._history
        def edit(*args, **kwargs):
            result = history(*args, **kwargs)
            self.f.ledger.write_text(fixtures.HEADER + SOURCE + '\n; changed while replaying\n', encoding='utf-8')
            return result
        with patch.object(diagnostic, '_history', side_effect=edit), self.assertRaises(BudgetError) as caught:
            self.query()
        self.assertEqual(caught.exception.code, 'INPUT_CHANGED')
        from beancount_budget_tool.financial import load_financial
        fin = load_financial(self.f.ledger)
        self.assertTrue(any(getattr(entry, 'narration', '') == 'later refill must stay invisible' for entry in fin.entries))

    def test_changed_source_rejects_old_error_anchor(self):
        self.f.ledger.write_text((fixtures.HEADER + SOURCE).replace('500 CNY', '501 CNY'), encoding='utf-8')
        with self.assertRaises(BudgetError) as caught:
            self.query()
        self.assertEqual(caught.exception.code, 'QUERY_SOURCE_CHANGED')

    def test_earlier_missing_ownership_is_not_treated_as_zero(self):
        self.f.doc['accounts']['Assets:Bank']['opening'] = {}
        self.f.write(self.f.config, self.f.doc)
        with self.assertRaises(BudgetError) as caught:
            self.query()
        self.assertEqual(caught.exception.code, 'QUERY_ATTRIBUTION_PENDING')

    def test_cli_json_and_csv_explicitly_label_diagnostic_and_do_not_overwrite(self):
        args = ['history', '--config', str(self.f.config), '--as-of', '2026-09-30',
                '--before-source', self.error.source, '--account', 'Assets:Bank', '--purpose', '自由资金']
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main([*args, '--format', 'json']), 0)
        result = json.loads(stream.getvalue())
        self.assertIsNone(result['report'])
        self.assertEqual(result['status'], 'DIAGNOSTIC_ONLY')
        self.assertEqual(len(result['history']['events']), 1)
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(main([*args, '--format', 'csv']), 0)
        self.assertIn('diagnostic_only', stream.getvalue())
        with redirect_stderr(StringIO()):
            self.assertEqual(main([*args, '--output', str(self.f.ledger)]), 2)

    def test_tui_error_history_returns_without_changing_failure(self):
        browser = SimpleNamespace(help=False, error=self.error, outcome=None,
                                  config=self.f.config, user_config=self.f.config, selected_month='2026-09')
        with patch.object(tui, '_read_field', return_value='自由资金'), patch.object(tui, '_message') as display:
            tui._inspect_account(None, browser, history=True)
        text = '\n'.join(display.call_args.args[2])
        self.assertIn('income without fund', text)
        self.assertIn('500', text)
        self.assertIs(browser.error, self.error)
        self.assertIsNone(browser.outcome)
        with patch.object(tui, '_read_field', return_value=None), patch.object(tui, '_message') as display:
            tui._inspect_account(None, browser, history=True)
            display.assert_not_called()


if __name__ == '__main__':
    unittest.main()

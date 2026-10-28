"""Current public workflow: one config pair and one liability interpretation."""
from contextlib import redirect_stderr
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.launcher import COMMANDS, main
from beancount_budget_tool.v6.input_compiler import default_config
from beancount_budget_tool.v6.simple_config import build_runtime, audit_simple
from beancount_budget_tool.yamlstrict import yaml_path_location
import test_v6_liability_rules as liability_fixtures


class CurrentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.case = liability_fixtures.LiabilityRuleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_ordinary_payable_and_credit_need_no_account_or_mode_switch(self):
        c = self.case
        c.doc.pop('liabilities')
        report = c.run_ledger(c.purchase() + c.payment(amount='60'))
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9900')
        self.assertEqual(report['actual_spendable']['booked_debts'][0]['remaining_native'], '40')
        self.assertEqual(c.spent(report), 100)
        self.assertEqual(c.outflows(report)['debt_payments'], '60')
        self.assertFalse((c.base / '.budget-state').exists())

    def test_optional_purpose_flag_is_a_noop_for_existing_current_configs(self):
        c = self.case
        first = c.run_ledger(c.purchase() + c.payment())
        c.doc['liability_budget_mode'] = 'purpose'
        second = c.run_ledger(c.purchase() + c.payment())
        self.assertEqual(first, second)

    def test_removed_whole_account_purpose_is_located_and_not_interpreted(self):
        c = self.case
        c.doc['liabilities']['Liabilities:Card']['purpose'] = '生活费'
        with self.assertRaises(BudgetError) as caught:
            c.run_ledger(c.purchase())
        self.assertEqual(caught.exception.code, 'LIABILITY_MODE_REMOVED')
        self.assertEqual(caught.exception.source,
                         yaml_path_location(c.config, ('liabilities', 'Liabilities:Card', 'purpose')))

    def test_removed_classification_mode_is_located(self):
        c = self.case
        c.doc['liability_budget_mode'] = 'classification'
        with self.assertRaises(BudgetError) as caught:
            c.run_ledger(c.purchase())
        self.assertEqual(caught.exception.code, 'LIABILITY_MODE_REMOVED')
        self.assertEqual(caught.exception.source, yaml_path_location(c.config, ('liability_budget_mode',)))

    def test_legacy_hidden_account_mode_cannot_override_current_public_rules(self):
        c = self.case
        expected = c.run_ledger(c.purchase())
        old = c.base / '.budget-state/config/reviews.json.gz'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'legacy account mode and approved claims')
        result = audit_simple(c.config, include_transactions=True)
        self.assertEqual(result['report'], expected)
        self.assertEqual(old.read_bytes(), b'legacy account mode and approved claims')

    def test_retired_commands_give_actionable_guidance(self):
        for name in ('init', 'next-month', 'project', 'review', 'review-month', 'review-refs', 'classification-proposals'):
            with self.subTest(command=name), redirect_stderr(io.StringIO()) as output:
                self.assertEqual(main([name, '--help']), 2)
                self.assertIn('COMMAND_RETIRED', output.getvalue())
                self.assertIn('config.yml', output.getvalue())
            self.assertNotIn(name, COMMANDS)

    def test_default_discovery_does_not_choose_between_two_configs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch('pathlib.Path.cwd', return_value=root):
                self.assertIsNone(default_config())
                (root / 'budget/simple').mkdir(parents=True)
                selected = root / 'budget/simple/config.yml'
                selected.write_text('schema: 1', encoding='utf-8')
                self.assertEqual(default_config(), selected.resolve())
                (root / 'config.yml').write_text('schema: 1', encoding='utf-8')
                with self.assertRaises(BudgetError) as caught:
                    default_config()
                self.assertEqual(caught.exception.code, 'CONFIG_AMBIGUOUS')

    def test_default_launcher_and_module_route_to_same_current_parser(self):
        with patch('beancount_budget_tool.v6.input_compiler.default_config', return_value=self.case.config), \
             patch('beancount_budget_tool.v6.monthly_report.main', return_value=0) as monthly:
            self.assertEqual(main([]), 0)
            monthly.assert_called_once_with([])

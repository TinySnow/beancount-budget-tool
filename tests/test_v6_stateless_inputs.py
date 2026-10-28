"""The ledger and two YAML files are the complete runtime authority."""

from contextlib import redirect_stderr
from copy import deepcopy
from hashlib import sha256
from io import StringIO
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.launcher import main, COMMANDS
from beancount_budget_tool.v6.simple_config import audit_simple, create_files, build_runtime, save_snapshot, _write
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.tui import _edit_review, _confirm_month_end
from beancount_budget_tool.v6.input_watch import InputWatch
from test_v6_tui import FakeScreen


class StatelessInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/v6/simple', self.base, dirs_exist_ok=True)
        self.config, self.budgets = self.base / 'config.yml', self.base / 'budgets.yml'

    def manifest(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def test_queries_and_refresh_leave_only_the_same_inputs(self):
        before = self.manifest()
        expected = audit_simple(self.config, include_transactions=True)
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        for _ in range(2):
            browser.refresh()
            self.assertIsNone(browser.error)
            self.assertEqual(browser.outcome, expected)
        self.assertEqual(self.manifest(), before)
        self.assertFalse((self.base / '.budget-state').exists())

    def test_poisoned_old_review_file_is_never_read_or_used(self):
        expected = audit_simple(self.config)
        old = self.base / '.budget-state/config/reviews.json.gz'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'poisoned legacy review: must not be opened')
        before = self.manifest()
        real_read = Path.read_bytes
        def guarded(path):
            if '.budget-state' in path.parts:
                raise AssertionError('runtime attempted to read hidden state')
            return real_read(path)
        with patch.object(Path, 'read_bytes', guarded):
            self.assertEqual(audit_simple(self.config), expected)
        self.assertEqual(self.manifest(), before)

    def test_hidden_confirmation_cannot_certify_a_missing_yaml_decision(self):
        plans = yaml.safe_load(self.budgets.read_text(encoding='utf-8'))
        plans['months']['2026-09'].pop('completeness_confirmed', None)
        _write(self.budgets, plans)
        old = self.base / '.budget-state/config/reviews.json.gz'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'{"approved":true,"completeness_confirmed":true}')
        outcome = audit_simple(self.config)
        self.assertEqual(outcome['status'], 'NEEDS_REVIEW')
        self.assertIsNone(outcome['report'])
        item = next(row for row in outcome['review_items'] if row['reason_code'] == 'EXTERNAL_SCOPE_PENDING')
        self.assertIn('budgets.yml:', item['source'])
        self.assertNotIn('按 f 确认', item['detail'])
        plans['months']['2026-09']['completeness_confirmed'] = True
        _write(self.budgets, plans)
        self.assertEqual(audit_simple(self.config)['status'], 'CERTIFIED_SCOPED')

    def test_month_end_dialog_cannot_approve_or_write_inputs(self):
        browser = SimpleReportBrowser(self.config, '2026-10')
        self.addCleanup(browser.close)
        browser.refresh()
        before = self.manifest()
        with patch('beancount_budget_tool.v6.tui._read_field') as prompt, patch('beancount_budget_tool.v6.tui._message') as message:
            _confirm_month_end(FakeScreen(), browser)
        prompt.assert_not_called()
        self.assertIn('budgets.yml', message.call_args.args[1])
        self.assertEqual(self.manifest(), before)
        self.assertEqual(browser.outcome['status'], 'NEEDS_REVIEW')

    def test_edit_dialog_only_points_to_explicit_configuration(self):
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['classification_defaults'] = False
        _write(self.config, doc)
        ledger = self.base / 'main.bean'
        ledger.write_text(ledger.read_text(encoding='utf-8').replace('    budget: "生活开销.餐饮"\n', ''), encoding='utf-8')
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertTrue(browser.reviews())
        before = self.manifest()
        with patch('beancount_budget_tool.v6.tui._read_field') as prompt, patch('beancount_budget_tool.v6.tui._message') as message:
            _edit_review(FakeScreen(), browser)
        prompt.assert_not_called()
        self.assertIn('YAML', message.call_args.args[1])
        from beancount_budget_tool.v6.tui import _draw_browser, HELP
        browser.help = True
        screen = FakeScreen(size=(50, 250))
        _draw_browser(screen, browser)
        help_text = '\n'.join(row[2] for row in screen.rows) + '\n'.join(HELP)
        self.assertIn('修改指引', help_text)
        self.assertNotIn('保存分类', help_text)
        self.assertNotIn('保存内部记录', help_text)
        self.assertNotIn('tui-reviews/', help_text)
        self.assertEqual(self.manifest(), before)
        self.assertIsNone(browser.outcome['report'])

    def test_retired_state_commands_never_create_or_delete_files(self):
        before = self.manifest()
        for command in ('state', 'clean', 'review'):
            with redirect_stderr(StringIO()) as text:
                self.assertEqual(main([command, '--config', str(self.config)]), 2)
            self.assertIn('COMMAND_RETIRED', text.getvalue())
            self.assertNotIn(command, COMMANDS)
        self.assertEqual(self.manifest(), before)

    def test_retired_save_api_cannot_hide_a_decision(self):
        before = self.manifest()
        with self.assertRaises(BudgetError) as caught:
            save_snapshot(self.config, self.base / 'untrusted.yaml')
        self.assertEqual(caught.exception.code, 'REVIEW_SAVE_REMOVED')
        self.assertEqual(self.manifest(), before)

    def test_declarations_accept_values_not_external_paths_or_unknown_protocols(self):
        original = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        for extra in ({'ownership': '/tmp/claims.yaml'}, {'unknown_protocol': {}}, {'ownership': {'defaults': {}}}):
            doc = deepcopy(original)
            doc['declarations'] = extra
            _write(self.config, doc)
            with self.assertRaises(BudgetError) as caught:
                audit_simple(self.config)
            self.assertEqual(caught.exception.code, 'SIMPLE_DECLARATIONS_INVALID')

    def test_watcher_does_not_observe_legacy_review_changes(self):
        watcher = InputWatch([self.config])
        before = watcher.snapshot()
        old = self.base / '.budget-state/config/reviews.json.gz'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'not an input')
        self.assertEqual(watcher.snapshot(), before)

    def test_configure_new_pair_creates_no_hidden_state(self):
        target = self.base / 'new'
        result = create_files(target, ledger=self.base / 'main.bean', month='2026-09')
        self.assertEqual({p.name for p in target.iterdir()}, {'config.yml', 'budgets.yml'})
        self.assertIsNone(audit_simple(Path(result['config']))['report'])

    def test_explicit_source_claims_live_in_yaml_and_expire_after_transaction_edit(self):
        from test_v6_review_decisions import ReviewDecisionsTests, apply_review
        fixture = ReviewDecisionsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.create()
        fixture.approve()
        explicit = apply_review(fixture.sheet)
        pair = Path(create_files(fixture.base / 'two-files', old_config=Path(explicit['config']))['config'])
        self.assertEqual({p.name for p in pair.parent.iterdir()}, {'config.yml', 'budgets.yml'})
        doc = yaml.safe_load(pair.read_text(encoding='utf-8'))
        self.assertTrue(doc['declarations']['ownership']['claims'])
        self.assertTrue(doc['declarations']['classifications']['classifications'])
        current, previous = audit_simple(pair)['report'], deepcopy(explicit['workflow']['report'])
        # Configuration migration renders public-name plans into new document
        # bytes; compare every financial/source field, not that file's digest.
        for report in (current, previous):
            for key in ('expense_targets', 'purpose_progress'):
                report[key].pop('plan_sha256', None)
        self.assertEqual(current, previous)
        before = pair.read_bytes()
        fixture.ledger.write_text(fixture.ledger.read_text(encoding='utf-8').replace(
            '"mixed cash move"', '"changed source"'), encoding='utf-8')
        with self.assertRaises(BudgetError) as caught:
            audit_simple(pair)
        self.assertEqual(caught.exception.code, 'OWNERSHIP_REF_INVALID')
        self.assertEqual(pair.read_bytes(), before)
        self.assertFalse((pair.parent / '.budget-state').exists())


if __name__ == '__main__':
    unittest.main()

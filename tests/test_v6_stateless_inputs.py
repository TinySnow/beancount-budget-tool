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
from beancount_budget_tool.v6.simple_config import audit_simple, create_files, build_runtime, _write
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

    def test_explicit_exact_total_fx_uses_ledger_only_and_loses_proof_after_fund_edit(self):
        ledger = self.base / 'main.bean'
        source = ledger.read_text(encoding='utf-8') + (
            '\n2026-09-01 open Assets:JPY\n2026-09-04 * "buy yen"\n'
            '  Assets:JPY 550 JPY @@ 25.70 CNY\n    fund: "生活费"\n'
            '  Assets:Cash -25.70 CNY\n    fund: "生活费"\n')
        ledger.write_text(source, encoding='utf-8')
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['account_groups'] = {'H': {'excluded': ['Assets:JPY']}}
        _write(self.config, doc)
        before = self.manifest()
        result = audit_simple(self.config, include_transactions=True)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result['review_items'])
        report = result['report']
        living = next(row for row in report['purpose_progress']['owners']
                      if report['owner_names'][row['owner_id']] == '生活费')
        self.assertEqual(living['actual_g'], '200')  # Exchange preserves purpose, adding no G.
        self.assertEqual(report['max_spendable']['consumption_target_remaining'], '190')
        self.assertEqual(self.manifest(), before)
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        ledger.write_text(source.replace('  Assets:JPY 550 JPY @@ 25.70 CNY\n    fund: "生活费"\n',
                                         '  Assets:JPY 550 JPY @@ 25.70 CNY\n'), encoding='utf-8')
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertIsNone(browser.outcome['report'])
        item = next(row for row in browser.reviews() if row['reason_code'] == 'H_FX_PENDING')
        self.assertIn('fund', item['detail'])
        self.assertNotIn('在 h_fx 中', item['detail'])
        self.assertFalse((self.base / '.budget-state').exists())

    def fx_source(self):
        ledger = self.base / 'main.bean'
        source = ledger.read_text(encoding='utf-8') + (
            '\n2026-09-01 open Assets:JPY\n2026-09-04 * "buy yen"\n'
            '  Assets:JPY 550 JPY @@ 25.70 CNY\n    fund: "生活费"\n'
            '  Assets:Cash -25.70 CNY\n    fund: "生活费"\n')
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['account_groups'] = {'H': {'excluded': ['Assets:JPY']}}
        _write(self.config, doc)
        return ledger, source

    def test_fx_with_fee_cannot_use_the_two_leg_shortcut(self):
        ledger, source = self.fx_source()
        source = source.replace('  Assets:Cash -25.70 CNY\n    fund: "生活费"\n',
                                '  Assets:Cash -26.70 CNY\n    fund: "生活费"\n  Expenses:Food 1 CNY\n')
        ledger.write_text(source, encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'IMPLEMENTATION_BLOCKED')
        self.assertIsNone(result['report'])

    def test_purchase_total_is_not_a_quote_for_later_foreign_consumption(self):
        ledger, source = self.fx_source()
        ledger.write_text(source + '\n2026-09-05 * "foreign expense"\n'
            '  Expenses:Food 10 JPY\n  Assets:JPY -10 JPY\n    fund: "生活费"\n', encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertTrue(any(row['reason_code'] == 'VALUATION_MISSING' for row in result['review_items']), result['review_items'])

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

    def test_save_api_and_browser_publication_transition_are_removed(self):
        before = self.manifest()
        from beancount_budget_tool.v6 import simple_config
        from beancount_budget_tool.v6.tui import ReportBrowser
        self.assertFalse(hasattr(simple_config, 'save_snapshot'))
        self.assertFalse(hasattr(ReportBrowser, 'show_saved_month'))
        self.assertFalse(hasattr(SimpleReportBrowser, 'show_saved_month'))
        self.assertEqual(self.manifest(), before)

    def test_new_declarations_field_is_rejected_in_both_public_files(self):
        original = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        for extra in ({}, {'ownership': '/tmp/claims.yaml'}, {'unknown_protocol': {}}, {'ownership': {'defaults': {}}}):
            doc = deepcopy(original)
            doc['declarations'] = extra
            _write(self.config, doc)
            with self.assertRaises(BudgetError) as caught:
                audit_simple(self.config)
            self.assertEqual(caught.exception.code, 'SIMPLE_CONFIG_INVALID')
        _write(self.config, original)
        plans = yaml.safe_load(self.budgets.read_text(encoding='utf-8'))
        plans['months']['2026-09']['declarations'] = {}
        _write(self.budgets, plans)
        with self.assertRaises(BudgetError) as caught:
            audit_simple(self.config)
        self.assertEqual(caught.exception.code, 'SIMPLE_BUDGETS_INVALID')

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

    def test_old_external_claims_cannot_be_silently_migrated_or_discarded(self):
        from test_v6_review_decisions import ReviewDecisionsTests, apply_review
        fixture = ReviewDecisionsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.create()
        fixture.approve()
        explicit = apply_review(fixture.sheet)
        target = fixture.base / 'two-files'
        before = {p:p.read_bytes() for p in fixture.base.rglob('*') if p.is_file()}
        with self.assertRaises(BudgetError) as caught:
            create_files(target, old_config=Path(explicit['config']))
        self.assertEqual(caught.exception.code, 'SIMPLE_IMPORT_UNSUPPORTED')
        self.assertIn('ownership.claims', caught.exception.message)
        self.assertIn('classifications.classifications', caught.exception.message)
        self.assertFalse(target.exists())
        self.assertEqual({p:p.read_bytes() for p in fixture.base.rglob('*') if p.is_file()}, before)
        self.assertFalse((fixture.base / '.budget-state').exists())

    def test_unsupported_protocol_evidence_prevents_any_migration_output(self):
        from test_v6_simple_config import SimpleConfigTests
        fixture = SimpleConfigTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        header = yaml.safe_load(fixture.old.read_text(encoding='utf-8'))
        for domain, field in (
                ('accounts', 'same_day_settlements'), ('accounts', 'credit_card_whitelist'),
                ('ownership', 'transfer_orders'), ('obligations', 'events'),
                ('obligations', 'opening'), ('benefit_groups', 'approvals'),
                ('purpose_pools', 'pools'), ('h_transfers', 'events'),
                ('h_fx', 'events'), ('k_events', 'events')):
            with self.subTest(domain=domain, field=field):
                path = fixture.old.parent / header[domain]
                before = path.read_bytes()
                document = yaml.safe_load(before)
                document[field] = [{'explicit': 'external evidence cannot be discarded'}]
                _write(path, document)
                target = fixture.base / 'unsupported-output'
                with self.assertRaises(BudgetError) as caught:
                    create_files(target, old_config=fixture.old)
                self.assertEqual(caught.exception.code, 'SIMPLE_IMPORT_UNSUPPORTED')
                self.assertIn(domain + '.' + field, caught.exception.message)
                self.assertFalse(target.exists())
                path.write_bytes(before)

    def test_custom_category_parent_and_wrong_month_confirmation_cannot_certify_during_migration(self):
        from test_v6_simple_config import SimpleConfigTests
        fixture = SimpleConfigTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for filename, code in (('classifications.yaml', 'classifications.parent'),
                               ('completeness.yaml', 'completeness.context')):
            with self.subTest(filename=filename):
                path = fixture.old.parent / filename
                before = path.read_bytes()
                document = yaml.safe_load(before)
                if filename == 'classifications.yaml':
                    next(row for row in document['categories'] if row['name'] == '日常开销.餐饮')['parent'] = None
                else:
                    document['as_of'] = '2026-10-31'
                _write(path, document)
                target = fixture.base / 'invalid-migration'
                with self.assertRaises(BudgetError) as caught:
                    create_files(target, old_config=fixture.old)
                self.assertEqual(caught.exception.code, 'SIMPLE_IMPORT_UNSUPPORTED')
                self.assertIn(code, caught.exception.message)
                self.assertFalse(target.exists())
                path.write_bytes(before)


if __name__ == '__main__':
    unittest.main()

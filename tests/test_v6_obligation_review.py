"""Review card responsibilities and payments without copying source references."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import create_files
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.obligation_review import prepare_review, save_review, validate_decision
from beancount_budget_tool.v6.tui import _edit_review
from test_v6_tui import FakeScreen
import test_v6_native_flows as native_tests


PURCHASE = ('2026-08-01 open Liabilities:Other\n'
    '2026-09-02 * "joint purchase"\n  Expenses:Food 25 CNY\n  Expenses:Travel 15 CNY\n'
    '  Assets:Cash -10 CNY\n  Liabilities:Card -20 CNY\n  Liabilities:Other -10 CNY\n')
PAYMENTS = ('2026-10-03 * "partial"\n  Assets:Cash -10 CNY\n  Liabilities:Card 10 CNY\n'
    '2026-11-04 * "final"\n  Assets:Cash -10 CNY\n  Liabilities:Card 10 CNY\n'
    '2026-11-05 * "other full"\n  Assets:Cash -10 CNY\n  Liabilities:Other 10 CNY\n')


class ObligationReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        native_tests.NativeFlowTests.configure(self, PURCHASE + PAYMENTS)
        self.advanced = self.config
        files = create_files(self.base / 'simple', old_config=self.advanced)
        self.simple = Path(files['config'])
        budgets = Path(files['budgets'])
        document = yaml.safe_load(budgets.read_text(encoding='utf-8'))
        for month in ('2026-10', '2026-11'):
            document.setdefault('months', {})[month] = {'completeness_confirmed': True}
        budgets.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        self.originals = {path: path.read_bytes() for path in [self.ledger, *self.advanced.parent.glob('*.yaml')]}

    def browser(self):
        value = SimpleReportBrowser(self.simple, '2026-09')
        self.addCleanup(value.close)
        value.refresh()
        return value

    def selected(self, browser, account='Liabilities:Card'):
        return next(row for row in browser.reviews() if row.get('account') == account and row['kind'] == 'booked_obligation')

    def submit(self, browser, choice, *, account='Liabilities:Card', approve='y', shape=None):
        items = browser.items()
        browser.view = 'reviews'
        browser.selected = next(index for index, row in enumerate(items) if row.get('account') == account and row['kind'] == 'booked_obligation' and (shape is None or row.get('shape') == shape))
        with patch('beancount_budget_tool.v6.tui._read_field', side_effect=[choice, '虚拟账本逐条核对', approve]), \
             patch('beancount_budget_tool.v6.tui._message') as message:
            _edit_review(FakeScreen(size=(30, 120)), browser)
        return message

    def approve_purchases(self, browser):
        for account in ('Liabilities:Card', 'Liabilities:Other'):
            message = self.submit(browser, '生活费', account=account)
            self.assertEqual(message.call_args.args[1], '已保存审阅决定')

    def test_two_file_tui_reviews_purchases_then_partial_and_final_payments(self):
        browser = self.browser()
        self.assertEqual(browser.outcome['status'], 'NEEDS_REVIEW')
        self.submit(browser, '生活费')
        self.assertEqual(browser.outcome['status'], 'NEEDS_REVIEW')
        self.assertIsNone(browser.outcome['report'])
        self.submit(browser, '生活费', account='Liabilities:Other')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(browser.outcome['report']['actual_spendable']['actual_spendable'], '110')
        ids = {row['account']: row['obligation_id'] for row in browser.outcome['report']['actual_spendable']['booked_debts']}
        for month, account in (('2026-10', 'Liabilities:Card'), ('2026-11', 'Liabilities:Card'), ('2026-11', 'Liabilities:Other')):
            browser.jump_month(month)
            browser.refresh()
            prepared = prepare_review(browser.config, self.selected(browser, account))
            self.assertEqual(list(prepared.choices), [ids[account]])
            self.assertEqual(prepared.initial, '1')
            self.submit(browser, '1', account=account)
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(browser.outcome['report']['actual_spendable']['actual_spendable'], '110')
        self.assertEqual(browser.outcome['report']['actual_spendable']['booked_debts'], [])
        self.assertEqual(browser.outcome['report']['default_outflows']['totals'][0]['debt_payments'], '20')
        self.assertTrue(all(path.read_bytes() == original for path, original in self.originals.items()))
        browser.jump_month('2026-09'); browser.refresh()
        self.assertEqual(browser.outcome['report']['actual_spendable']['actual_spendable'], '110')
        self.assertEqual(len(browser.outcome['report']['actual_spendable']['booked_debts']), 2)
        reopened = SimpleReportBrowser(self.simple, '2026-11')
        self.addCleanup(reopened.close); reopened.refresh()
        self.assertEqual(reopened.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(reopened.outcome['report']['actual_spendable']['actual_spendable'], '110')

    def test_cancel_and_no_approval_do_not_save_or_modify_inputs(self):
        browser = self.browser()
        before = list((self.simple.parent / '.budget-state').rglob('*'))
        self.submit(browser, '生活费', approve='n')
        self.assertEqual(list((self.simple.parent / '.budget-state').rglob('*')), before)
        prepared = prepare_review(browser.config, self.selected(browser))
        target = self.base / 'unapproved'
        with self.assertRaises(BudgetError) as caught:
            save_review(prepared, '生活费', 'test', target)
        self.assertEqual(caught.exception.code, 'REVIEW_APPROVAL_REQUIRED')
        self.assertFalse(target.exists())

    def test_purchase_rejects_unknown_split_and_outside_owners_and_missing_reason(self):
        browser = self.browser()
        prepared = prepare_review(browser.config, self.selected(browser))
        for value in ('-', '未知用途', '生活费10,自由资金10'):
            with self.subTest(value=value), self.assertRaises(BudgetError):
                validate_decision(prepared, value, 'test')
        with self.assertRaises(BudgetError) as caught:
            validate_decision(prepared, '生活费', '')
        self.assertEqual(caught.exception.code, 'REVIEW_REASON_REQUIRED')
        self.assertEqual(validate_decision(prepared, '生活费', 'test'), [('生活费', 20)])

    def test_source_change_or_existing_claim_cannot_be_saved_twice(self):
        prepared = prepare_review(self.advanced, self.selected(self.browser()))
        target = self.base / 'changed'
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8').replace('Card -20', 'Card -21').replace('Other -10', 'Other -9'), encoding='utf-8')
        with self.assertRaises(BudgetError):
            save_review(prepared, '生活费', 'test', target, approved=True)
        self.assertFalse(target.exists())

    def test_legacy_config_publication_preserves_the_original_and_remaining_reviews(self):
        item = next(row for row in audit_month_workflow(self.advanced)['review_items'] if row.get('account') == 'Liabilities:Card')
        prepared = prepare_review(self.advanced, item)
        result = save_review(prepared, '生活费', 'test', self.base / 'legacy', approved=True)
        self.assertEqual(result['status'], 'DECISIONS_SAVED')
        self.assertEqual(result['workflow']['status'], 'NEEDS_REVIEW')
        self.assertTrue(all(path.read_bytes() == original for path, original in self.originals.items()))
        with self.assertRaises(BudgetError):
            save_review(prepared, '生活费', 'test', self.base / 'legacy', approved=True)

    def test_unsupported_and_advance_items_are_never_offered_as_purchase_or_payment(self):
        browser = self.browser()
        item = deepcopy(self.selected(browser))
        for changes in ({'reason_code': 'OBLIGATION_EVENT_UNSUPPORTED'}, {'shape': 'income_payable'}, {'shape': 'unsupported'}):
            with self.subTest(changes=changes), self.assertRaises(BudgetError) as caught:
                prepare_review(browser.config, {**item, **changes})
            self.assertEqual(caught.exception.code, 'TUI_REVIEW_UNSUPPORTED')

    def test_prefix_stops_before_the_whole_joint_transaction(self):
        from datetime import date
        from beancount_budget_tool.v6.obligations import audit_simple_obligations
        from beancount_budget_tool.v6.classification import _key
        doc = yaml.safe_load(self.advanced.read_text(encoding='utf-8'))
        path = self.advanced.parent / doc['obligations']
        all_events = audit_simple_obligations(self.ledger, date(2026, 9, 1), path)
        second = next(row for row in all_events['events'] if row['account'] == 'Liabilities:Other')
        prefix = audit_simple_obligations(self.ledger, date(2026, 9, 1), path,
                                          before_ref=_key(second['liability_ref']))
        self.assertEqual(prefix['events'], [])
        self.assertEqual(prefix['obligations'], [])
        self.assertEqual(prefix['before_liability_ref'], second['liability_ref'])

    def test_same_day_later_purchase_is_only_an_explicit_clearing_not_prior_balance(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8') +
            '2026-10-03 * "later same day"\n  Expenses:Food 50 CNY\n  Liabilities:Card -50 CNY\n', encoding='utf-8')
        browser = self.browser()
        self.approve_purchases(browser)
        old_id = next(row['obligation_id'] for row in browser.outcome['report']['actual_spendable']['booked_debts']
                      if row['account'] == 'Liabilities:Card')
        browser.jump_month('2026-10'); browser.refresh()
        self.submit(browser, '生活费', shape='consumer_purchase')
        prepared = prepare_review(browser.config, self.selected(browser))
        self.assertIn(old_id, prepared.choices)
        self.assertNotIn('same_day_source_ref', prepared.choices[old_id])
        self.assertIn('未偿 20 CNY', prepared.choice_labels[0])
        later = [choice for choice in prepared.choices.values() if 'same_day_source_ref' in choice]
        self.assertEqual(len(later), 1)
        self.assertIn('同日清算（先后未知）', later[0]['label'])
        self.assertEqual(prepared.initial, '')
        self.assertIsNone(browser.outcome['report'])

    def test_payment_does_not_offer_other_card_ids_or_allow_overpayment(self):
        browser = self.browser()
        self.approve_purchases(browser)
        other_id = next(row['obligation_id'] for row in browser.outcome['report']['actual_spendable']['booked_debts']
                        if row['account'] == 'Liabilities:Other')
        browser.jump_month('2026-10'); browser.refresh()
        prepared = prepare_review(browser.config, self.selected(browser))
        with self.assertRaises(BudgetError):
            validate_decision(prepared, other_id, 'test')
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8').replace(
            '2026-10-03 * "partial"\n  Assets:Cash -10 CNY\n  Liabilities:Card 10 CNY',
            '2026-10-03 * "partial"\n  Assets:Cash -25 CNY\n  Liabilities:Card 25 CNY'), encoding='utf-8')
        browser.refresh()
        with self.assertRaises(BudgetError) as caught:
            prepare_review(browser.config, self.selected(browser))
        self.assertEqual(caught.exception.code, 'TUI_OBLIGATION_REVIEW_PENDING')

    def test_changed_sidecar_and_already_claimed_source_are_rejected(self):
        item = next(row for row in audit_month_workflow(self.advanced)['review_items'] if row.get('account') == 'Liabilities:Card')
        prepared = prepare_review(self.advanced, item)
        result = save_review(prepared, '生活费', 'test', self.base / 'saved', approved=True)
        with self.assertRaises(BudgetError) as caught:
            prepare_review(Path(result['config']), item)
        self.assertEqual(caught.exception.code, 'REVIEW_SOURCE_CHANGED')
        source = self.advanced.parent / 'ownership.yaml'
        source.write_text(source.read_text(encoding='utf-8') + '\n# changed while reviewing\n', encoding='utf-8')
        with self.assertRaises(BudgetError) as caught:
            save_review(prepared, '生活费', 'test', self.base / 'stale', approved=True)
        self.assertEqual(caught.exception.code, 'INPUT_CHANGED')
        self.assertFalse((self.base / 'stale').exists())

    def test_numeric_payment_choice_remains_bound_to_the_original_id(self):
        browser = self.browser()
        self.approve_purchases(browser)
        browser.jump_month('2026-10'); browser.refresh()
        prepared = prepare_review(browser.config, self.selected(browser))
        ident = list(prepared.choices)[0]
        # Reordering the refreshed menu must not reinterpret the user's number.
        fresh = deepcopy(prepared)
        fresh.choices = {'another_id': {'label': 'another liability'}, **fresh.choices}
        with patch('beancount_budget_tool.v6.obligation_review.prepare_review', return_value=fresh):
            result = save_review(prepared, '1', 'test', self.base / 'numbered', approved=True)
        saved = yaml.safe_load((Path(result['config']).parent / 'obligations.yaml').read_text(encoding='utf-8'))
        self.assertEqual(saved['events'][-1]['obligation_id'], ident)

    def test_published_decision_survives_a_following_report_failure(self):
        item = next(row for row in audit_month_workflow(self.advanced)['review_items'] if row.get('account') == 'Liabilities:Card')
        prepared = prepare_review(self.advanced, item)
        current = audit_month_workflow(self.advanced)
        with patch('beancount_budget_tool.v6.obligation_review.audit_month_workflow',
                   side_effect=[current, BudgetError('TEST_REPORT_ERROR', '模拟保存后报告失败')]):
            result = save_review(prepared, '生活费', 'test', self.base / 'report-failed', approved=True)
        self.assertEqual(result['status'], 'DECISIONS_SAVED')
        self.assertIsNone(result['workflow'])
        self.assertEqual(result['report_error']['code'], 'TEST_REPORT_ERROR')
        self.assertTrue(Path(result['config']).is_file())
        saved = yaml.safe_load((Path(result['config']).parent / 'obligations.yaml').read_text(encoding='utf-8'))
        self.assertEqual(len(saved['events']), 1)
        self.assertTrue(all(path.read_bytes() == original for path, original in self.originals.items()))

    def test_invalid_staged_claim_is_not_published_and_reports_a_real_source(self):
        item = next(row for row in audit_month_workflow(self.advanced)['review_items'] if row.get('account') == 'Liabilities:Card')
        prepared = prepare_review(self.advanced, item)
        from beancount_budget_tool.v6.obligations import audit_simple_obligations
        def guarded(root, cutover, declarations, *args, **kwargs):
            if declarations.name == 'obligations.yaml' and 'budget-debt-review-' in str(declarations):
                raise BudgetError('TEST_INVALID_CLAIM', '模拟正式校验拒绝', str(declarations) + ':20')
            return audit_simple_obligations(root, cutover, declarations, *args, **kwargs)
        with patch('beancount_budget_tool.v6.obligation_review.audit_simple_obligations', side_effect=guarded):
            with self.assertRaises(BudgetError) as caught:
                save_review(prepared, '生活费', 'test', self.base / 'invalid-stage', approved=True)
        self.assertEqual(caught.exception.source, item['source'])
        self.assertFalse((self.base / 'invalid-stage').exists())

    def test_payment_without_reviewed_earlier_debt_stays_unclaimed(self):
        browser = self.browser()
        browser.jump_month('2026-10'); browser.refresh()
        item = self.selected(browser)
        self.assertEqual(item['shape'], 'asset_outflow')
        payment = next(row for row in browser.reviews() if row.get('date') == '2026-10-03')
        with self.assertRaises(BudgetError) as caught:
            prepare_review(browser.config, payment)
        self.assertEqual(caught.exception.code, 'TUI_OBLIGATION_REVIEW_PENDING')

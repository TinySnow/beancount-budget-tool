"""A reviewed same-day clearing needs source identity, not invented timestamps."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.classification import _key
from beancount_budget_tool.v6.input_compiler import read_month_config, month_inputs
from beancount_budget_tool.v6.obligation_review import prepare_review, save_review, validate_decision
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6.monthly_report import render_text
from beancount_budget_tool.v6.transaction_details import render_transaction
import test_v6_group_payments as grouped


class SameDayClearingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = grouped.GroupPaymentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        raw = self.fixture.ledger.read_text(encoding='utf-8')
        start = raw.index('2026-09-03 *')
        payment = raw.index('2026-09-04 *')
        self.fixture.ledger.write_text(raw[:start] + raw[payment:] + raw[start:payment].replace('2026-09-03', '2026-09-04'), encoding='utf-8')
        self.original = self.fixture.ledger.read_bytes()

    def prepared(self, browser):
        return prepare_review(browser.config, self.fixture.row(browser, 'asset_outflow', '1499/2'))

    def ready(self):
        browser = self.fixture.browser()
        self.fixture.approve_origins(browser)
        self.fixture.submit(browser, 'asset_outflow', '1', '1000')
        return browser

    def approve(self, browser):
        prepared = self.prepared(browser)
        ident = next(ident for ident, choice in prepared.choices.items() if 'same_day_source_ref' in choice)
        self.fixture.submit(browser, 'asset_outflow', ident, '1499/2')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED', browser.outcome)
        return ident

    def declaration(self, browser):
        doc, paths, cutover = read_month_config(browser.config)
        sidecar = paths['obligations']
        return doc, paths, cutover, sidecar, yaml.safe_load(sidecar.read_text(encoding='utf-8'))

    def audit_declaration(self, browser, document, *, before_ref=None):
        doc, paths, cutover, _, _ = self.declaration(browser)
        target = self.fixture.base / 'checked-obligations.yaml'
        target.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')
        with month_inputs(paths['ledger'], doc['month'], cutover) as (_, last):
            return audit_simple_obligations(paths['ledger'], cutover, target, last, before_ref=before_ref)

    def test_original_payment_first_order_certifies_after_explicit_same_day_binding(self):
        browser = self.ready()
        prepared = self.prepared(browser)
        self.assertEqual(prepared.initial, '')  # Same-day association is never preselected.
        self.assertIn('先后未知', prepared.choice_labels[0])
        self.assertIn('不确认先后', prepared.guidance)
        self.assertIsNone(browser.outcome['report'])
        ident = self.approve(browser)
        report = browser.outcome['report']
        self.assertEqual(report['actual_spendable']['actual_spendable'], '1803/2')
        self.assertEqual(report['actual_spendable']['booked_debts'], [])
        self.assertEqual(len(report['actual_spendable']['same_day_clearings']), 1)
        self.assertIn('未确认实际日内先后', render_text(report))
        self.assertEqual(report['default_outflows']['totals'][0]['debt_payments'], '3499/2')
        rows = report['transactions']['rows']
        payment = next(row for row in rows if row['narration'].startswith('return two'))
        purchase = next(row for row in rows if row['narration'] == 'joint phone purchase')
        self.assertLess(rows.index(payment), rows.index(purchase))
        linked = next(row for row in payment['liabilities'] if row['obligation_id'] == ident)
        self.assertEqual(linked['clearing_protocol'], 'same_day_obligation_clearing_v1')
        self.assertEqual(linked['intraday_order'], 'not_asserted')
        self.assertEqual(linked['same_day_source_ref'], purchase['liabilities'][0]['liability_ref'])
        self.assertEqual(payment['net_allocations'], {})
        self.assertEqual(len(payment['purpose_events']), 1)
        self.assertIn('不确认真实日内先后', '\n'.join(render_transaction(payment, report['owner_names'])))
        self.assertEqual(audit_simple(self.fixture.simple, include_transactions=True)['report'], report)
        plain = audit_simple(self.fixture.simple)['report']
        self.assertNotIn('transactions', plain)
        self.assertEqual(plain['actual_spendable']['same_day_clearings'], report['actual_spendable']['same_day_clearings'])
        self.assertEqual(self.fixture.ledger.read_bytes(), self.original)
        self.assertNotIn(b'budget-order', self.fixture.ledger.read_bytes())

    def test_payment_before_purchase_does_not_consume_the_later_responsibility_implicitly(self):
        browser = self.ready()
        doc, paths, cutover, sidecar, document = self.declaration(browser)
        prepared = self.prepared(browser)
        result = save_review(prepared, '1', '关联同日购机，记不清日内先后', self.fixture.base / 'saved', approved=True)
        next_config = Path(result['config'])
        next_doc, next_paths, _ = read_month_config(next_config)
        document = yaml.safe_load(next_paths['obligations'].read_text(encoding='utf-8'))
        payment = document['events'][-1]
        payment.pop('same_day_source_ref')
        with self.assertRaises(BudgetError) as caught:
            self.audit_declaration(browser, document)
        self.assertEqual(caught.exception.code, 'OBLIGATION_SETTLEMENT_UNKNOWN')

    def test_unreviewed_same_day_source_is_not_offered(self):
        browser = self.fixture.browser()
        self.fixture.submit(browser, 'asset_inflow', '生活费')
        self.fixture.submit(browser, 'asset_outflow', '1', '1000')
        with self.assertRaises(BudgetError) as caught:
            self.prepared(browser)
        self.assertEqual(caught.exception.code, 'TUI_OBLIGATION_REVIEW_PENDING')

    def test_new_source_is_not_a_prior_balance_in_an_intraday_prefix(self):
        browser = self.ready()
        self.approve(browser)
        _, _, _, _, document = self.declaration(browser)
        payment = next(row for row in document['events'] if 'same_day_source_ref' in row)
        prefix = self.audit_declaration(browser, document, before_ref=_key(payment['same_day_source_ref']))
        self.assertEqual(prefix['review_state'], 'pending')
        self.assertEqual(prefix['pending_same_day_prepayments'], {payment['obligation_id']: '1499/2'})
        self.assertNotIn(payment['obligation_id'], {row['obligation_id'] for row in prefix['obligations']})

    def test_cross_day_reference_is_rejected_even_if_all_sources_and_amounts_exist(self):
        browser = self.ready()
        self.approve(browser)
        _, _, _, _, document = self.declaration(browser)
        # Reuse a real earlier receipt as a bogus target instead of fabricating a hash.
        receipt = next(row for row in document['events'] if row['kind'] == 'payable_receipt')
        payment = next(row for row in document['events'] if 'same_day_source_ref' in row)
        payment['same_day_source_ref'] = receipt['liability_ref']
        payment['obligation_id'] = receipt['obligation_id']
        with self.assertRaises(BudgetError) as caught:
            self.audit_declaration(browser, document)
        self.assertEqual(caught.exception.code, 'SAME_DAY_CLEARING_INVALID')

    def test_missing_approval_wrong_id_and_stale_target_are_rejected(self):
        browser = self.ready()
        self.approve(browser)
        _, _, _, _, original = self.declaration(browser)
        for variant in ('unapproved', 'wrong-id', 'stale'):
            with self.subTest(variant=variant):
                doc = deepcopy(original)
                payment = next(row for row in doc['events'] if 'same_day_source_ref' in row)
                if variant == 'unapproved':
                    doc['events'] = [row for row in doc['events'] if row['kind'] != 'consumer_purchase']
                elif variant == 'wrong-id':
                    payment['obligation_id'] = 'another_id'
                else:
                    payment['same_day_source_ref']['transaction_sha256'] = '0' * 64
                with self.assertRaises(BudgetError) as caught:
                    self.audit_declaration(browser, doc)
                self.assertEqual(caught.exception.code, 'SAME_DAY_CLEARING_INVALID')

    def test_partial_same_day_clearing_leaves_only_the_actual_remainder(self):
        raw = self.fixture.ledger.read_text(encoding='utf-8')
        self.fixture.ledger.write_text(raw.replace('Card 749.50 CNY\n  Assets:Bank -1749.50',
                                                 'Card 100 CNY\n  Assets:Bank -1100'), encoding='utf-8')
        browser = self.fixture.browser()
        self.fixture.approve_origins(browser)
        self.fixture.submit(browser, 'asset_outflow', '1', '1000')
        prepared = prepare_review(browser.config, self.fixture.row(browser, 'asset_outflow', '100'))
        self.assertIn('same_day_source_ref', prepared.choices[next(iter(prepared.choices))])
        self.fixture.submit(browser, 'asset_outflow', '1', '100')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        debts = browser.outcome['report']['actual_spendable']['booked_debts']
        self.assertEqual([row['remaining_native'] for row in debts], ['1299/2'])
        self.assertEqual(browser.outcome['report']['actual_spendable']['actual_spendable'], '1803/2')

    def test_cross_day_later_purchase_is_not_a_same_day_clearing_candidate(self):
        raw = self.fixture.ledger.read_text(encoding='utf-8')
        self.fixture.ledger.write_text(raw.replace('2026-09-04 * "shop"', '2026-09-05 * "shop"'), encoding='utf-8')
        browser = self.ready()
        with self.assertRaises(BudgetError):
            self.prepared(browser)
        _, _, _, _, document = self.declaration(browser)
        audit = self.audit_declaration(browser, document)
        payment = next(row for row in audit['events'] if row['shape'] == 'asset_outflow' and row['signed_units'] == '1499/2')
        purchase = next(row for row in audit['events'] if row['kind'] == 'consumer_purchase')
        document['events'].append({'kind': 'payment', 'reason': 'invalid cross-day binding',
            'liability_ref': payment['liability_ref'], 'counterpart_refs': payment['counterpart_refs'],
            'obligation_id': purchase['obligation_id'], 'same_day_source_ref': purchase['liability_ref']})
        with self.assertRaises(BudgetError) as caught:
            self.audit_declaration(browser, document)
        self.assertEqual(caught.exception.code, 'SAME_DAY_CLEARING_INVALID')

    def test_joint_prepayment_capacity_cannot_be_reused_by_another_leg(self):
        raw = self.fixture.ledger.read_text(encoding='utf-8')
        self.fixture.ledger.write_text(raw.replace('Card 749.50 CNY\n  Assets:Bank -1749.50',
            'Card 400 CNY\n  Liabilities:Card 400 CNY\n  Assets:Bank -1800'), encoding='utf-8')
        browser = self.ready()
        self.fixture.submit(browser, 'asset_outflow', '1', '400')
        with self.assertRaises(BudgetError):
            prepare_review(browser.config, self.fixture.row(browser, 'asset_outflow', '400'))
        _, _, _, _, document = self.declaration(browser)
        first = next(row for row in document['events'] if 'same_day_source_ref' in row)
        audit = self.audit_declaration(browser, document)
        pending = next(row for row in audit['events'] if row['status'] != 'CERTIFIED' and row['signed_units'] == '400')
        document['events'].append({**first, 'liability_ref': pending['liability_ref'],
                                  'counterpart_refs': pending['counterpart_refs']})
        with self.assertRaises(BudgetError) as caught:
            self.audit_declaration(browser, document)
        self.assertEqual(caught.exception.code, 'OBLIGATION_SETTLEMENT_EXCESS')

    def test_clearance_does_not_allow_real_cash_overdraft(self):
        browser = self.ready()
        self.approve(browser)
        self.fixture.ledger.write_text(self.fixture.ledger.read_text(encoding='utf-8').replace('3000 CNY', '200 CNY'), encoding='utf-8')
        browser.refresh()
        self.assertNotEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertIsNone(browser.outcome['report'])
        self.assertTrue(any(row['reason_code'] == 'H_NEGATIVE_INVENTORY' for row in browser.outcome['review_items']))

    def test_future_same_day_clearing_is_validated_without_replaying_in_old_month(self):
        self.fixture.ledger.write_text(self.fixture.ledger.read_text(encoding='utf-8').replace('2026-09-04', '2026-10-04'), encoding='utf-8')
        plans = self.fixture.simple.parent / 'budgets.yml'
        document = yaml.safe_load(plans.read_text(encoding='utf-8'))
        document.setdefault('months', {})['2026-10'] = {'completeness_confirmed': True}
        plans.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        browser = self.fixture.browser()
        self.fixture.submit(browser, 'asset_inflow', '生活费')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        old = deepcopy(browser.outcome['report'])
        browser.jump_month('2026-10'); browser.refresh()
        self.fixture.submit(browser, 'consumer_purchase', '生活费')
        self.fixture.submit(browser, 'asset_outflow', '1', '1000')
        self.approve(browser)
        browser.jump_month('2026-09'); browser.refresh()
        self.assertEqual(browser.outcome['report'], old)
        _, _, _, _, document = self.declaration(browser)
        payment = next(row for row in document['events'] if 'same_day_source_ref' in row)
        payment['same_day_source_ref']['transaction_sha256'] = '0' * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit_declaration(browser, document)
        self.assertEqual(caught.exception.code, 'SAME_DAY_CLEARING_INVALID')

    def test_same_day_choice_requires_approval_and_reason_without_rewriting_ledger(self):
        browser = self.ready()
        prepared = self.prepared(browser)
        with self.assertRaises(BudgetError) as caught:
            validate_decision(prepared, '1', '')
        self.assertEqual(caught.exception.code, 'REVIEW_REASON_REQUIRED')
        output = self.fixture.base / 'unapproved'
        with self.assertRaises(BudgetError) as caught:
            save_review(prepared, '1', 'same-day association', output)
        self.assertEqual(caught.exception.code, 'REVIEW_APPROVAL_REQUIRED')
        self.assertFalse(output.exists())
        self.assertEqual(self.fixture.ledger.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()

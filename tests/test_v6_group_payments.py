"""Explicit payable receipts and one cash payment with several debt legs."""

from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.obligation_review import prepare_review, save_review
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.simple_config import create_files, audit_simple
from fixture_months import ProtocolBrowser
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.v6.tui import _edit_review
from test_v6_tui import FakeScreen
import test_v6_native_flows as native_tests


TRANSACTIONS = '''2026-09-01 * "income"
  Assets:Bank 3000 CNY
    fund: "生活费"
  Income:Salary -3000 CNY
2026-09-02 * "relative" "refundable receipt"
  Assets:Bank 1000 CNY
    fund: "生活费"
  Liabilities:Card -1000 CNY
2026-09-03 * "shop" "joint phone purchase"
  Expenses:Food 1499 CNY
  Expenses:Travel 749.5 CNY
  Liabilities:Card -749.50 CNY
  Assets:Bank -1499 CNY
2026-09-04 * "relative" "return two liabilities in one payment"
  Liabilities:Card 1000 CNY
  Liabilities:Card 749.50 CNY
  Assets:Bank -1749.50 CNY
'''


class GroupPaymentTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.configure(TRANSACTIONS)

    def configure(self, transactions):
        native_tests.NativeFlowTests.configure(self, transactions)
        result = create_files(self.base / 'simple', old_config=self.config)
        self.simple = Path(result['config'])
        self.originals = {path: path.read_bytes() for path in [self.ledger, self.simple, Path(result['budgets'])]}

    def browser(self):
        browser = ProtocolBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        return browser

    def row(self, browser, shape, amount=None):
        return min((row for row in browser.reviews() if row.get('shape') == shape and
                    (amount is None or row['signed_units'] == amount)),
                   key=lambda row: (row['date'], int(row['source'].rsplit(':', 1)[1])))

    def submit(self, browser, shape, decision, amount=None):
        browser.view = 'reviews'
        row = self.row(browser, shape, amount)
        browser.selected = browser.items().index(row)
        from uuid import uuid4
        prepared = prepare_review(browser.config, row)
        result = save_review(prepared, decision, '逐条核对虚拟来源', self.base / ('explicit-' + uuid4().hex), approved=True)
        browser.configs[browser.index] = Path(result["config"]).resolve()
        browser.refresh()

    def approve_origins(self, browser):
        self.submit(browser, 'asset_inflow', '生活费')
        self.submit(browser, 'consumer_purchase', '生活费')

    def approve_payments(self, browser):
        self.submit(browser, 'asset_outflow', '1', '1000')
        self.submit(browser, 'asset_outflow', '1', '1499/2')

    def test_receipt_purchase_and_two_debt_legs_use_one_real_cash_payment(self):
        browser = self.browser()
        self.assertNotEqual(browser.outcome['status'], 'IMPLEMENTATION_BLOCKED')
        self.approve_origins(browser)
        self.submit(browser, 'asset_outflow', '1', '1000')
        self.assertIsNone(browser.outcome['report'])
        prepared = prepare_review(browser.config, self.row(browser, 'asset_outflow', '1499/2'))
        self.assertEqual(len(prepared.choices), 1)  # Already-used sibling capacity is removed.
        self.submit(browser, 'asset_outflow', '1', '1499/2')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED', browser.outcome)
        report = browser.outcome['report']
        self.assertEqual(report['actual_spendable']['actual_spendable'], '1803/2')
        self.assertEqual(report['actual_spendable']['booked_debts'], [])
        flows = report['default_outflows']['totals'][0]
        self.assertEqual(flows['debt_payments'], '3499/2')
        self.assertEqual(flows['expenses_paid'], '1499')
        tx = next(row for row in report['transactions']['rows'] if row['narration'].startswith('return two'))
        self.assertEqual(len(tx['purpose_events']), 1)
        self.assertEqual(tx['net_allocations'], {})
        self.assertEqual(len(tx['purpose_events'][0]['protocol']['obligation_ids']), 2)
        cash = next(row for row in tx['postings'] if row['account'] == 'Assets:Bank')
        self.assertEqual(len(cash['default_outflows']), 1)
        self.assertEqual(len(cash['default_outflows'][0]['counterpart_refs']), 2)
        receipt = next(row for row in report['transactions']['rows'] if row['narration'] == 'refundable receipt')
        self.assertEqual(receipt['net_allocations'], {})
        self.assertEqual(receipt['liabilities'][0]['kind'], 'payable_receipt')
        self.assertEqual(native_tests.NativeFlowTests.income_total(self), '3000')
        detail = '\n'.join(render_transaction(tx, report['owner_names']))
        self.assertEqual(detail.count('合并付款核对：'), 1)
        reopened = audit_month_workflow(browser.config, include_transactions=True)
        self.assertEqual(reopened['report'], report)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in self.originals.items()))

    def test_group_can_be_reviewed_in_reverse_posting_order_without_reusing_capacity(self):
        browser = self.browser()
        self.approve_origins(browser)
        prepared = prepare_review(browser.config, self.row(browser, 'asset_outflow', '1499/2'))
        index = next(index for index, choice in enumerate(prepared.choices.values(), 1)
                     if 'joint phone purchase' in choice['label'])
        self.submit(browser, 'asset_outflow', str(index), '1499/2')
        self.assertIsNone(browser.outcome['report'])
        prepared = prepare_review(browser.config, self.row(browser, 'asset_outflow', '1000'))
        self.assertEqual(len(prepared.choices), 1)
        self.submit(browser, 'asset_outflow', '1', '1000')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(browser.outcome['report']['actual_spendable']['actual_spendable'], '1803/2')

    def test_two_legs_may_partly_pay_one_id_when_joint_capacity_is_sufficient(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8').replace(
            'Assets:Bank 1000 CNY', 'Assets:Bank 2000 CNY').replace(
            'Liabilities:Card -1000 CNY', 'Liabilities:Card -2000 CNY'), encoding='utf-8')
        browser = self.browser()
        self.approve_origins(browser)
        self.submit(browser, 'asset_outflow', '1', '1000')
        prepared = prepare_review(browser.config, self.row(browser, 'asset_outflow', '1499/2'))
        index = next(index for index, choice in enumerate(prepared.choices.values(), 1)
                     if 'refundable receipt' in choice['label'])
        self.submit(browser, 'asset_outflow', str(index), '1499/2')
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        debts = browser.outcome['report']['actual_spendable']['booked_debts']
        self.assertEqual(sorted(row['remaining_native'] for row in debts), ['1499/2', '501/2'])
        self.assertEqual(browser.outcome['report']['default_outflows']['totals'][0]['debt_payments'], '3499/2')

    def test_earlier_unreviewed_payment_message_points_to_its_source(self):
        raw = self.ledger.read_text(encoding='utf-8')
        begin = raw.index('2026-09-04 *')
        self.ledger.write_text(raw[:begin] + '''2026-09-04 * "earlier single payment"
  Liabilities:Card 100 CNY
  Assets:Bank -100 CNY
2026-09-05 * "later single payment"
  Liabilities:Card 100 CNY
  Assets:Bank -100 CNY
''', encoding='utf-8')
        browser = self.browser()
        self.approve_origins(browser)
        later = next(row for row in browser.reviews() if row.get('shape') == 'asset_outflow' and row['date'] == '2026-09-05')
        earlier = self.row(browser, 'asset_outflow')
        with self.assertRaises(BudgetError) as caught:
            prepare_review(browser.config, later)
        self.assertIn(earlier['source'], caught.exception.message)
        self.assertIn('清单展示顺序不代表重放顺序', caught.exception.message)

    def test_missing_receipt_fund_stays_separate_from_approved_liability(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8').replace(
            '  Assets:Bank 1000 CNY\n    fund: "生活费"', '  Assets:Bank 1000 CNY'), encoding='utf-8')
        browser = self.browser()
        self.submit(browser, 'asset_inflow', '生活费')
        self.assertIsNone(browser.outcome['report'])
        self.assertTrue(any(row['reason_code'] == 'OWNERSHIP_UNREVIEWED' for row in browser.reviews()))

    def test_same_day_later_receipt_cannot_cover_earlier_group_payment(self):
        source = self.ledger.read_text(encoding='utf-8').replace('2026-09-04 * "relative"',
                                                               '2026-09-01 * "relative"')
        self.ledger.write_text(source, encoding='utf-8')
        browser = self.browser()
        self.approve_origins(browser)
        with self.assertRaises(BudgetError) as caught:
            prepare_review(browser.config, self.row(browser, 'asset_outflow', '1000'))
        self.assertEqual(caught.exception.code, 'TUI_OBLIGATION_REVIEW_PENDING')

    def test_group_requires_all_other_source_refs_and_rejects_repeated_or_stale_refs(self):
        browser = self.browser()
        self.approve_origins(browser)
        prepared = prepare_review(browser.config, self.row(browser, 'asset_outflow', '1000'))
        result = save_review(prepared, '1', 'test', self.base / 'advanced-payment', approved=True)
        config = Path(result['config'])
        doc = yaml.safe_load(config.read_text(encoding='utf-8'))
        sidecar = config.parent / doc['obligations']
        document = yaml.safe_load(sidecar.read_text(encoding='utf-8'))
        original = deepcopy(document)
        for variant in ('missing', 'duplicate', 'stale'):
            with self.subTest(variant=variant):
                changed = deepcopy(original)
                row = changed['events'][-1]
                if variant == 'missing': row['counterpart_refs'].pop()
                elif variant == 'duplicate': row['counterpart_refs'].append(row['counterpart_refs'][0])
                else: row['counterpart_refs'][0]['signed_units'] = '-1'
                sidecar.write_text(yaml.safe_dump(changed, allow_unicode=True), encoding='utf-8')
                with self.assertRaises(BudgetError):
                    audit_simple_obligations(self.ledger, date(2026, 9, 1), sidecar, date(2026, 9, 30))

    def test_balanced_amount_change_invalidates_the_entire_reviewed_payment(self):
        browser = self.browser()
        self.approve_origins(browser)
        self.approve_payments(browser)
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        raw = self.ledger.read_text(encoding='utf-8')
        self.ledger.write_text(raw.replace('  Liabilities:Card 1000 CNY\n  Liabilities:Card 749.50 CNY',
                                         '  Liabilities:Card 1100 CNY\n  Liabilities:Card 649.50 CNY'), encoding='utf-8')
        browser.refresh()
        self.assertIsNone(browser.outcome)
        self.assertIsNotNone(browser.error)

    def test_future_reviewed_group_is_validated_without_replaying_it_into_old_month(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8').replace(
            '2026-09-04 *', '2026-10-04 *'), encoding='utf-8')
        plans = self.simple.parent / 'budgets.yml'
        doc = yaml.safe_load(plans.read_text(encoding='utf-8'))
        doc.setdefault('months', {})['2026-10'] = {'completeness_confirmed': True}
        plans.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
        browser = self.browser()
        self.approve_origins(browser)
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        old = deepcopy(browser.outcome['report'])
        self.assertEqual(len(old['actual_spendable']['booked_debts']), 2)
        browser.jump_month('2026-10'); browser.refresh()
        self.approve_payments(browser)
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(browser.outcome['report']['actual_spendable']['actual_spendable'], '1803/2')
        self.assertEqual(browser.outcome['report']['actual_spendable']['booked_debts'], [])
        browser.jump_month('2026-09'); browser.refresh()
        self.assertEqual(browser.outcome['report'], old)

    def test_zero_mixed_direction_extra_income_and_price_stay_unsupported(self):
        raw = self.ledger.read_text(encoding='utf-8')
        variants = [raw.replace('Card 749.50 CNY\n  Assets:Bank -1749.50', 'Card 0 CNY\n  Assets:Bank -1000'),
                    raw.replace('Card 749.50 CNY\n  Assets:Bank -1749.50', 'Card -1 CNY\n  Assets:Bank -999'),
                    raw.replace('  Assets:Bank -1749.50 CNY', '  Assets:Bank -1750.50 CNY\n  Income:Salary 1 CNY'),
                    raw.replace('  Assets:Bank -1749.50 CNY', '  Assets:Bank -1749.50 CNY @ 1 CNY')]
        for source in variants:
            with self.subTest(source=source[-220:]):
                self.ledger.write_text(source, encoding='utf-8')
                report = audit_simple_obligations(self.ledger, date(2026, 9, 1))
                rows = [row for row in report['events'] if row['date'] == '2026-09-04']
                self.assertTrue(all(row['shape'] == 'unsupported' for row in rows))

    def test_ambiguous_equal_payables_offer_explicit_choices_instead_of_guessing(self):
        # Reuse the existing pure receipt shape, with two equal outstanding IDs.
        raw = self.ledger.read_text(encoding='utf-8')
        start = raw.index('2026-09-02 *')
        self.ledger.write_text(raw[:start] + '''2026-09-02 * "peer A" "receipt A"
  Assets:Bank 520 CNY
    fund: "生活费"
  Liabilities:Card -260 CNY
  Income:Salary -260 CNY
2026-09-03 * "peer B" "receipt B"
  Assets:Bank 520 CNY
    fund: "生活费"
  Liabilities:Card -260 CNY
  Income:Salary -260 CNY
2026-09-04 * "forward A"
  Assets:Bank -260 CNY
  Liabilities:Card 260 CNY
2026-09-04 * "forward B"
  Assets:Bank -260 CNY
  Liabilities:Card 260 CNY
''', encoding='utf-8')
        browser = self.browser()
        prepared = prepare_review(browser.config, self.row(browser, 'asset_outflow'))
        self.assertEqual(len(prepared.choices), 2)
        self.assertEqual(prepared.initial, '')
        self.assertTrue(any('peer A' in label for label in prepared.choice_labels))
        self.assertTrue(any('peer B' in label for label in prepared.choice_labels))
        self.submit(browser, 'asset_outflow', '1')
        # The last remaining collected payable is now unambiguous, so its
        # existing exact-receipt protocol can clear the second payment.
        self.assertEqual(browser.outcome['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(browser.outcome['report']['default_outflows']['totals'][0]['debt_payments'], '520')


if __name__ == '__main__':
    unittest.main()

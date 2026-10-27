"""Posting liability budgets choose responsibility without duplicating cash."""
from datetime import date
from fractions import Fraction
from pathlib import Path
import random
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import build_runtime, create_files, audit_simple
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.v6.monthly_report import render_text
import test_v6_liability_rules as fixtures


class PostingLiabilityBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LiabilityRuleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.doc['liability_budget_mode'] = 'purpose'
        self.fixture.doc.pop('liabilities')

    def audit(self, body, month='2026-09', *, certified=True):
        return self.fixture.run_ledger(body, month, certified=certified)

    def debt(self, body, month='2026-09'):
        result = self.audit(body, month, certified=False)
        runtime = build_runtime(self.fixture.config, month)
        self.addCleanup(runtime.close)
        report = audit_simple_obligations(self.fixture.ledger, date(2026, 9, 1),
            runtime.config.parent/'obligations.yaml', date.fromisoformat(month+'-30'))
        return result, report

    def named_balances(self, report):
        return {self.owner_name(row['owner_id']): Fraction(row['remaining_native']) for row in report['obligations']}

    def owner_name(self, owner):
        from beancount_budget_tool.yamlstrict import load_yaml
        runtime = build_runtime(self.fixture.config)
        try:
            return next(row['name'] for row in load_yaml(runtime.config.parent/'ownership.yaml')['owners'] if row['id']==owner)
        finally:
            runtime.close()

    def purchase(self, label='生活费', amount='100', day='2026-09-02'):
        return f'{day} * "purchase"\n  Expenses:Food {amount} CNY\n  Liabilities:Card -{amount} CNY\n    budget: "{label}"\n'

    def payment(self, label=None, amount='100', day='2026-09-03'):
        return f'{day} * "payment"\n  Assets:Bank -{amount} CNY\n  Liabilities:Card {amount} CNY\n'+(f'    budget: "{label}"\n' if label is not None else '')

    def test_tagged_payable_receipts_and_forwarding_need_no_account_rules(self):
        body = ''
        for day in ('2026-09-02', '2026-09-03'):
            body += f'{day} * "personal plus colleague"\n  Assets:Bank 520 CNY\n    fund: "生活费"\n'
            body += '  Income:Work -260 CNY\n  Liabilities:Colleague -260 CNY\n    budget: "同事差旅费"\n'
        for day in ('2026-09-04', '2026-09-05'):
            body += f'{day} * "forward"\n  Assets:Bank -260 CNY\n  Liabilities:Colleague 260 CNY\n    budget: "同事差旅费"\n'
        report = self.audit(body)
        self.assertEqual(report['actual_spendable']['actual_spendable'], '10520')
        self.assertEqual(report['actual_spendable']['booked_debts'], [])
        self.assertEqual(self.fixture.spent(report), 0)
        self.assertTrue(all(not row['net_allocations'] for row in report['transactions']['rows']))
        self.assertIn('同事差旅费', report['owner_names'].values())

    def test_same_account_two_purposes_are_independent_and_can_be_paid_separately(self):
        body = self.purchase('生活费60,旅游40')
        _, debt = self.debt(body + self.payment('生活费', '60'))
        self.assertEqual(self.named_balances(debt), {'旅游': 40})
        report = self.audit(body + self.payment('生活费', '60') + self.payment('旅游', '40', '2026-09-04'))
        self.assertEqual(self.fixture.spent(report), 100)
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9900')
        self.assertEqual(self.fixture.outflows(report)['debt_payments'], '100')

    def test_full_payment_without_label_clears_exact_complete_vector(self):
        report = self.audit(self.purchase('生活费60,旅游40') + self.payment())
        self.assertEqual(report['actual_spendable']['booked_debts'], [])
        self.assertEqual(self.fixture.spent(report), 100)
        details = '\n'.join(render_transaction(report['transactions']['rows'][-1], report['owner_names']))
        self.assertIn('按逐笔 budget 清算', details)
        self.assertIn('生活费', details)
        self.assertIn('旅游', details)

    def test_ambiguous_partial_payment_requires_budget_not_approval(self):
        result = self.audit(self.purchase('生活费60,旅游40') + self.payment(amount='50'), certified=False)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        items = [row for row in result['review_items'] if row['reason_code']=='LIABILITY_BUDGET_AMBIGUOUS']
        self.assertTrue(items, result['review_items'])
        self.assertTrue(all('main.bean:' in row['source'] for row in items))
        self.assertTrue(any('欠款 60 CNY' in row['detail'] and '欠款 40 CNY' in row['detail'] for row in items))
        self.assertFalse(any(row['reason_kind']=='implementation_limit' for row in items))

    def test_budget_overrides_account_default_purpose(self):
        self.fixture.doc['liabilities'] = {'Liabilities:Card': {'role':'credit_card', 'purpose':'生活费'}}
        _, debt = self.debt(self.purchase('旅游'))
        self.assertEqual(self.named_balances(debt), {'旅游': 100})
        self.audit(self.purchase('旅游') + self.payment('旅游'))

    def test_role_can_be_configured_without_whole_account_purpose(self):
        self.fixture.doc['liabilities'] = {'Liabilities:Card': {'role':'credit_card'}}
        self.audit(self.purchase('旅游') + self.payment())

    def test_wrong_purpose_does_not_cancel_another_purposes_debt(self):
        result, debt = self.debt(self.purchase('生活费') + self.payment('旅游'))
        self.assertEqual(self.named_balances(debt), {'生活费': 100})
        self.assertEqual(debt['clearing_advances'][0]['remaining_native'], '100')
        self.assertEqual(debt['review_state'], 'complete')
        self.assertEqual(result['report']['actual_spendable']['actual_spendable'], '9800')
        text = render_text(result['report'])
        self.assertIn('（生活费）', text)
        self.assertIn('（旅游）', text)

    def test_prepayment_before_same_day_purchase_preserves_order_and_clears(self):
        body = self.payment('生活费60,旅游40', day='2026-09-02') + self.purchase('生活费60,旅游40')
        report = self.audit(body)
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9900')
        self.assertEqual([row['kind'] for row in report['liability_activity']['events']], ['payment','consumer_purchase'])

    def test_duplicate_account_legs_and_mixed_income_match_real_march_shape(self):
        body = '2026-09-02 * "borrow"\n  Assets:Bank 1000 CNY\n    fund: "生活费"\n'
        body += '  Liabilities:Colleague -1000 CNY\n    budget: "兄长"\n'
        body += '2026-09-03 * "repay before purchase"\n  Assets:Bank -1749.50 CNY\n'
        body += '  Liabilities:Colleague 1000 CNY\n    budget: "兄长"\n  Liabilities:Colleague 749.50 CNY\n    budget: "兄长"\n'
        body += '2026-09-03 * "purchase"\n  Assets:Bank -1499 CNY\n  Expenses:Food 2248.50 CNY\n'
        body += '  Liabilities:Colleague -749.50 CNY\n    budget: "兄长"\n'
        report = self.audit(body)
        self.assertEqual(self.fixture.spent(report), Fraction('2248.50'))
        self.assertEqual(self.fixture.outflows(report)['debt_payments'], '3499/2')
        self.assertEqual(report['actual_spendable']['booked_debts'], [])
        self.assertEqual(report['actual_spendable']['actual_spendable'], '15503/2')

    def test_principal_and_interest_do_not_double_count(self):
        body = '2026-09-02 * "borrow"\n  Assets:Bank 1000 CNY\n    fund: "生活费"\n  Liabilities:Colleague -1000 CNY\n    budget: "生活费600,旅游400"\n'
        body += '2026-09-03 * "repay"\n  Assets:Bank -1010 CNY\n  Expenses:Interest 10 CNY\n  Liabilities:Colleague 1000 CNY\n    budget: "生活费600,旅游400"\n'
        report = self.audit(body)
        self.assertEqual(self.fixture.spent(report), 10)
        self.assertEqual(self.fixture.outflows(report)['debt_payments'], '1000')
        self.assertEqual(self.fixture.outflows(report)['expenses_paid'], '10')

    def test_budget_split_must_equal_whole_liability_posting(self):
        result = self.audit(self.purchase('生活费60,旅游30'), certified=False)
        self.assertIsNone(result['report'])
        self.assertTrue(any(row['reason_code']=='TAG_SYNTAX_INVALID' and 'main.bean:' in row['source']
                            for row in result['review_items']))

    def test_future_repayment_does_not_remove_september_debt(self):
        _, debt = self.debt(self.purchase('生活费60,旅游40') + self.payment(day='2026-10-03'))
        self.assertEqual(self.named_balances(debt), {'生活费':60,'旅游':40})
        report = self.audit(self.purchase('生活费60,旅游40') + self.payment(day='2026-10-03'), '2026-10')
        self.assertEqual(report['actual_spendable']['booked_debts'], [])
        self.assertEqual(self.fixture.spent(report), 0)

    def test_credit_return_restores_cash_purpose_not_debt_budget(self):
        self.fixture.doc['accounts']['Assets:Bank']['opening']['CNY']={'生活费':'9000','储蓄':'1000'}
        body = self.purchase('旅游') + '2026-09-03 * "prepay from savings"\n  Assets:Bank -150 CNY\n    fund: "储蓄"\n'
        body += '  Liabilities:Card 150 CNY\n    budget: "旅游"\n'
        body += '2026-09-04 * "return overpayment"\n  Assets:Bank 50 CNY\n  Liabilities:Card -50 CNY\n    budget: "旅游"\n'
        report = self.audit(body)
        receipt = report['transactions']['rows'][-1]['postings'][0]
        self.assertEqual(receipt['h_purpose']['slices'][0]['name'], '储蓄')
        self.assertEqual(self.fixture.outflows(report)['net_outflow'], '0')
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9000')

    def test_missing_budget_uses_unique_debt_purpose_even_after_other_purposes_exist(self):
        body = self.purchase('生活费60,旅游40') + self.payment('旅游', '40') + self.payment(amount='60',day='2026-09-04')
        report = self.audit(body)
        self.assertEqual(report['actual_spendable']['actual_spendable'],'9900')

    def test_refund_preserves_purpose_and_consumption_is_reversed_once(self):
        body = self.purchase('旅游') + '2026-09-03 * "refund"\n  Expenses:Food -30 CNY\n  Liabilities:Card 30 CNY\n    budget: "旅游"\n'
        report = self.audit(body + self.payment('旅游', '70', '2026-09-04'))
        self.assertEqual(self.fixture.spent(report), 70)
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9930')

    def test_explicit_debt_transfers_preserve_each_purpose(self):
        self.fixture.doc['liabilities'] = {'Liabilities:Card': {'role':'credit_card', 'purpose':'生活费'},
                                          'Liabilities:Colleague': {'role':'payable', 'purpose':'旅游'}}
        body = self.purchase('生活费60,旅游40')
        body += '2026-09-03 * "transfer debt"\n  Liabilities:Card 100 CNY\n    budget: "生活费60,旅游40"\n'
        body += '  Liabilities:Colleague -100 CNY\n    budget: "旅游40,生活费60"\n'
        body += '2026-09-04 * "repay"\n  Assets:Bank -100 CNY\n  Liabilities:Colleague 100 CNY\n'
        report = self.audit(body)
        self.assertEqual(self.fixture.spent(report), 100)
        self.assertEqual(self.fixture.outflows(report)['debt_payments'], '100')

    def test_debt_transfer_cannot_change_responsibility_purposes(self):
        body = self.purchase('生活费')
        body += '2026-09-03 * "transfer debt"\n  Liabilities:Card 100 CNY\n    budget: "生活费"\n'
        body += '  Liabilities:Colleague -100 CNY\n    budget: "旅游"\n'
        result = self.audit(body, certified=False)
        self.assertIsNone(result['report'])
        self.assertIn('LIABILITY_BUDGET_TRANSFER_MISMATCH', {r['reason_code'] for r in result['review_items']})

    def test_multi_purpose_receivable_restores_its_actual_cash_purposes(self):
        self.fixture.doc['liabilities'] = {'Liabilities:Advance': {'role':'receivable'}}
        self.fixture.doc['accounts']['Assets:Bank']['opening']['CNY']={'生活费':'9000','储蓄':'1000'}
        body = '2026-09-02 * "advance"\n  Assets:Bank -150 CNY\n    fund: "生活费50,储蓄100"\n'
        body += '  Liabilities:Advance 150 CNY\n    budget: "生活费50,储蓄100"\n'
        body += '2026-09-03 * "reimbursement"\n  Assets:Bank 150 CNY\n  Liabilities:Advance -150 CNY\n'
        report = self.audit(body)
        receipt = report['transactions']['rows'][-1]['postings'][0]['h_purpose']['slices']
        self.assertEqual({p['name']:Fraction(p['signed_units']) for p in receipt}, {'生活费':50,'储蓄':100})
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9000')
        self.assertEqual(self.fixture.spent(report), 0)

    def test_ignored_budget_does_not_erase_a_financial_debt(self):
        result, debt = self.debt(self.purchase('-'))
        self.assertEqual(self.named_balances(debt), {'生活费':100})
        self.assertEqual(result['report']['actual_spendable']['actual_spendable'], '9900')

    def test_unknown_mode_has_configuration_location(self):
        self.fixture.doc['liability_budget_mode'] = 'typo'
        with self.assertRaises(BudgetError) as error:
            self.audit(self.purchase())
        self.assertEqual(error.exception.code,'SIMPLE_CONFIG_INVALID')
        self.assertIn('config.yml:',error.exception.source)

    def test_off_switch_restores_legacy_semantics_without_sticky_rules(self):
        body = self.purchase('生活费60,旅游40') + self.payment()
        self.audit(body)
        self.fixture.doc.pop('liability_budget_mode')
        result = self.audit(body,certified=False)
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIn('OBLIGATION_UNCLAIMED',{r['reason_code'] for r in result['review_items']})

    def test_config_export_preserves_posting_mode(self):
        self.audit(self.purchase('生活费60,旅游40') + self.payment())
        runtime = build_runtime(self.fixture.config)
        try:
            converted = create_files(self.fixture.base/'converted', old_config=runtime.config)
        finally:
            runtime.close()
        from beancount_budget_tool.yamlstrict import load_yaml
        doc = load_yaml(Path(converted['config']))
        self.assertEqual(doc['liability_budget_mode'], 'purpose')
        self.assertEqual(doc.get('liabilities',{}), {})
        reopened = audit_simple(Path(converted['config']), include_transactions=True)
        self.assertEqual(reopened['status'], 'CERTIFIED_SCOPED')
        self.assertEqual(reopened['report']['actual_spendable']['actual_spendable'], '9900')

    def test_credit_return_is_identified_by_purpose_even_when_account_net_was_zero(self):
        body = self.purchase('生活费') + self.payment('旅游')
        body += '2026-09-04 * "return purpose credit"\n  Assets:Bank 100 CNY\n'
        body += '  Liabilities:Card -100 CNY\n    budget: "旅游"\n'
        report = self.audit(body)
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9900')
        self.assertEqual(self.fixture.outflows(report)['credit_returns'], '100')
        self.assertEqual(report['actual_spendable']['booked_debts'][0]['remaining_native'], '100')
        self.assertEqual(report['transactions']['rows'][-1]['postings'][0]['h_purpose']['slices'][0]['name'], '生活费')

    def test_two_cards_and_cash_share_consumption_only_once(self):
        body = '2026-09-02 * "joint purchase"\n  Expenses:Food 120 CNY\n  Assets:Bank -20 CNY\n'
        body += '  Liabilities:Card -60 CNY\n    budget: "生活费40,旅游20"\n'
        body += '  Liabilities:Colleague -40 CNY\n    budget: "旅游"\n'
        body += '2026-09-03 * "joint payment"\n  Assets:Bank -100 CNY\n'
        body += '  Liabilities:Card 60 CNY\n  Liabilities:Colleague 40 CNY\n'
        report = self.audit(body)
        self.assertEqual(self.fixture.spent(report), 120)
        self.assertEqual(report['actual_spendable']['actual_spendable'], '9880')
        self.assertEqual(self.fixture.outflows(report)['debt_payments'], '100')
        self.assertEqual(self.fixture.outflows(report)['expenses_paid'], '20')

    def test_bullet_loan_keeps_due_date_for_each_purpose_slice(self):
        self.fixture.doc['liabilities']={'Liabilities:Loan':{'role':'loan','due_on':'2026-10-15'}}
        body = '2026-09-02 * "loan"\n  Assets:Bank 100 CNY\n'
        body += '  Liabilities:Loan -100 CNY\n    budget: "生活费60,旅游40"\n'
        _, debt = self.debt(body)
        self.assertEqual(self.named_balances(debt),{'生活费':60,'旅游':40})
        self.assertTrue(all(row['due_on']=='2026-10-15' and row['debt_kind']=='nonconsumer' for row in debt['obligations']))

    def test_tagged_random_sequences_match_independent_integer_cent_model(self):
        # Exercise separate positive credits and debts, then clear each named
        # vector. The oracle knows no runtime IDs or implementation helpers.
        for seed in range(8):
            with self.subTest(seed=seed):
                rng=random.Random(seed); balances={'生活费':0,'旅游':0}
                cash=1000000; spending=0; body=''
                for index in range(24):
                    owner=rng.choice(list(balances)); cents=rng.randint(1,8000)
                    value=f'{cents//100}.{cents%100:02d}'
                    if rng.choice((True,False)):
                        body+=self.purchase(owner,value,day=f'2026-09-{index+2:02d}')
                        balances[owner]-=cents;spending+=cents
                    else:
                        body+=self.payment(owner,value,day=f'2026-09-{index+2:02d}')
                        balances[owner]+=cents;cash-=cents
                for owner,balance in balances.items():
                    value=f'{abs(balance)//100}.{abs(balance)%100:02d}'
                    if balance<0:
                        body+=self.payment(owner,value,day='2026-09-28');cash+=balance
                    elif balance>0:
                        body+='2026-09-28 * "return prepayment"\n'+f'  Assets:Bank {value} CNY\n'
                        body+=f'  Liabilities:Card -{value} CNY\n    budget: "{owner}"\n';cash+=balance
                report=self.audit(body)
                self.assertEqual(Fraction(report['actual_spendable']['actual_spendable']),Fraction(cash,100))
                self.assertEqual(self.fixture.spent(report),Fraction(spending,100))
                self.assertEqual(report['actual_spendable']['booked_debts'],[])
                self.assertEqual(report['actual_spendable']['clearing_receivables'],[])

    def test_cutover_only_debt_is_reserved_without_recounting_old_consumption(self):
        self.fixture.doc['accounts']['Assets:Bank']['opening']['CNY']={'生活费':'10100'}
        body='2026-08-31 * "old loan"\n  Assets:Bank 100 CNY\n  Liabilities:Card -100 CNY\n'
        report=self.audit(body)
        self.assertEqual(self.fixture.spent(report),0)
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10000')
        self.assertEqual(report['actual_spendable']['booked_debts'][0]['remaining_native'],'100')

    def test_export_keeps_global_mode_before_first_liability_transaction(self):
        self.audit('')
        runtime=build_runtime(self.fixture.config)
        try:
            converted=create_files(self.fixture.base/'no-debt-converted',old_config=runtime.config)
        finally:
            runtime.close()
        from beancount_budget_tool.yamlstrict import load_yaml
        self.assertEqual(load_yaml(Path(converted['config']))['liability_budget_mode'],'purpose')

    def test_partly_ignored_budget_is_rejected_instead_of_dropping_a_financial_amount(self):
        result=self.audit(self.purchase('旅游60,-40'),certified=False)
        self.assertIsNone(result['report'])
        self.assertIn('TAG_SYNTAX_INVALID',{row['reason_code'] for row in result['review_items']})

    def test_unresolved_previous_month_dependency_still_shows_original_ledger_line(self):
        body=self.purchase('生活费60,旅游40')+self.payment(amount='50')
        result=self.audit(body,'2026-10',certified=False)
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        items=[row for row in result['review_items'] if row['reason_code']=='LIABILITY_BUDGET_AMBIGUOUS']
        self.assertTrue(items,result['review_items'])
        self.assertTrue(all('main.bean:' in row['source'] for row in items))
        self.assertTrue(any(row['date']=='2026-09-03' for row in items))


if __name__ == '__main__':
    unittest.main()

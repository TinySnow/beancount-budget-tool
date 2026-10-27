"""A proved prepaid principal and personal income may arrive in one receipt."""
from fractions import Fraction
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.exact import amount_text
from beancount_budget_tool.v6.simple_config import build_runtime
from beancount_budget_tool.v6.spending import audit_monthly_spending
from datetime import date
import test_v6_liability_rules as fixtures


class CreditIncomeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LiabilityRuleTests();self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.doc['liability_budget_mode']='purpose'
        self.case.doc.pop('liabilities')

    def prepay(self, amount='240', fund='生活费', day='2026-09-02'):
        return f'{day} * "forward before receipt"\n  Assets:Bank -{amount} CNY\n    fund: "{fund}"\n'+f'  Liabilities:Colleague {amount} CNY\n    budget: "同事差旅费"\n'

    def receipt(self, credit='240', income='240', fund='生活费', day='2026-09-02'):
        amount=Fraction(credit)+Fraction(income)
        value=amount_text(amount)
        return f'{day} * "personal income and repayment"\n  Assets:Bank {value} CNY\n'+(f'    fund: "{fund}"\n' if fund is not None else '')+f'  Liabilities:Colleague -{credit} CNY\n    budget: "同事差旅费"\n  Income:Work -{income} CNY\n'

    def run_source(self, source, month='2026-09', *, certified=True):
        return self.case.run_ledger(source,month,certified=certified)

    def income(self):
        runtime=build_runtime(self.case.config)
        try:
            from beancount_budget_tool.yamlstrict import load_yaml
            doc=load_yaml(runtime.config)
            result=audit_monthly_spending(self.case.ledger,date(2026,9,1),'2026-09',runtime.config.parent/doc['classifications'],'CNY')
            return result['metrics']['income_supplement']['report_total']
        finally:runtime.close()

    def test_same_day_payment_then_income_receipt_counts_only_240_income_and_return(self):
        report=self.run_source(self.prepay()+self.receipt())
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10240')
        self.assertEqual(report['actual_spendable']['booked_debts'],[])
        self.assertEqual(report['actual_spendable']['clearing_receivables'],[])
        self.assertEqual(self.income(),'240')
        out=self.case.outflows(report)
        self.assertEqual(out['debt_payments'],'240')
        self.assertEqual(out['credit_returns'],'240')
        self.assertEqual(out['net_outflow'],'0')
        self.assertEqual(self.case.spent(report),0)
        self.assertTrue(all(not row['net_allocations'] for row in report['transactions']['rows']))
        from beancount_budget_tool.v6.transaction_details import render_transaction
        detail='\n'.join(render_transaction(report['transactions']['rows'][-1],report['owner_names']))
        self.assertIn('原预付收回 240＋个人收入 240',detail)

    def test_missing_receipt_fund_restores_prepayment_and_defaults_only_personal_income(self):
        report=self.run_source(self.prepay()+self.receipt(fund=None))
        parts=report['transactions']['rows'][-1]['postings'][0]['h_purpose']['slices']
        self.assertEqual({p['name']:Fraction(p['signed_units']) for p in parts},{'生活费':240,'自由资金':240})
        self.assertEqual(self.case.outflows(report)['credit_returns'],'240')
        self.assertEqual(self.income(),'240')

    def test_return_from_savings_plus_living_income_does_not_misclassify_income_as_cash_return(self):
        self.case.doc['accounts']['Assets:Bank']['opening']['CNY']={'生活费':'9000','储蓄':'1000'}
        report=self.run_source(self.prepay(fund='储蓄')+self.receipt(fund='储蓄240,生活费240'))
        self.assertEqual(report['actual_spendable']['actual_spendable'],'9240')
        self.assertEqual(self.case.outflows(report).get('credit_returns','0'),'0')
        self.assertEqual(self.case.outflows(report)['net_outflow'],'0')
        self.assertEqual(self.income(),'240')

    def test_income_does_not_allow_reassigning_the_returned_principal_purpose(self):
        self.case.doc['accounts']['Assets:Bank']['opening']['CNY']={'生活费':'9000','储蓄':'1000'}
        with self.assertRaises(BudgetError) as error:
            self.run_source(self.prepay(fund='储蓄')+self.receipt(fund='生活费'))
        self.assertEqual(error.exception.code,'LIABILITY_CREDIT_RECEIPT_PURPOSE_MISMATCH')
        self.assertIn('储蓄',error.exception.message)
        self.assertIn('main.bean:',error.exception.source)

    def test_partial_return_leaves_credit_and_does_not_invent_income(self):
        report=self.run_source(self.prepay(amount='300')+self.receipt())
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10180')
        self.assertEqual(report['actual_spendable']['clearing_receivables'][0]['remaining_native'],'60')
        self.assertEqual(self.case.outflows(report)['credit_returns'],'240')
        self.assertEqual(self.case.outflows(report)['net_outflow'],'60')

    def test_cross_month_advance_is_not_discharged_by_a_future_receipt(self):
        source=self.prepay(day='2026-09-30')+self.receipt(day='2026-10-02')
        september=self.run_source(source)
        self.assertEqual(september['actual_spendable']['actual_spendable'],'9760')
        self.assertEqual(september['actual_spendable']['clearing_receivables'][0]['remaining_native'],'240')
        october=self.run_source(source,'2026-10')
        self.assertEqual(october['actual_spendable']['actual_spendable'],'10240')
        self.assertEqual(self.case.outflows(october)['credit_returns'],'240')
        self.assertEqual(self.case.outflows(october)['net_outflow'],'-240')

    def test_new_borrowing_in_the_same_liability_leg_remains_an_explicit_program_gap(self):
        result=self.run_source(self.prepay()+self.receipt(credit='300'),certified=False)
        self.assertEqual(result['status'],'IMPLEMENTATION_BLOCKED')
        self.assertIn('LIABILITY_CREDIT_MIXED_UNSUPPORTED',{r['reason_code'] for r in result['review_items']})

    def test_legacy_account_rule_also_handles_proved_income_and_prepaid_return(self):
        self.case.doc.pop('liability_budget_mode')
        self.case.doc['liabilities']={'Liabilities:Colleague':{'role':'payable','purpose':'生活费'}}
        report=self.run_source(self.prepay()+self.receipt())
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10240')
        self.assertEqual(self.case.outflows(report)['credit_returns'],'240')

    def test_income_before_payment_preserves_the_existing_ordinary_payable_path(self):
        report=self.run_source(self.receipt()+self.prepay())
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10240')
        self.assertEqual(self.case.outflows(report).get('credit_returns','0'),'0')
        self.assertEqual(self.case.outflows(report)['debt_payments'],'240')
        self.assertEqual(self.income(),'240')

    def test_two_income_postings_count_each_actual_amount_only_once(self):
        receipt=self.receipt().replace('  Income:Work -240 CNY\n',
            '  Income:Work -10.11 CNY\n  Income:Work -229.89 CNY\n')
        report=self.run_source(self.prepay()+receipt)
        self.assertEqual(self.income(),'240')
        self.assertEqual(self.case.outflows(report)['credit_returns'],'240')
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10240')

    def test_multiple_budget_slices_on_one_liability_do_not_duplicate_returned_cash(self):
        source=(self.prepay()+self.receipt()).replace('budget: "同事差旅费"','budget: "同事甲120,同事乙120"')
        report=self.run_source(source)
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10240')
        self.assertEqual(self.case.outflows(report)['credit_returns'],'240')
        self.assertEqual(self.income(),'240')

    def test_zero_income_uses_existing_pure_credit_return_semantics(self):
        report=self.run_source(self.prepay()+self.receipt(income='0'))
        self.assertEqual(report['actual_spendable']['actual_spendable'],'10000')
        self.assertEqual(self.case.outflows(report)['credit_returns'],'240')
        self.assertNotIn('credit_income_receipt',report['transactions']['rows'][-1])

    def test_ambiguous_original_credit_purpose_stays_data_review_not_guessed_income(self):
        self.case.doc['accounts']['Assets:Bank']['opening']['CNY']={'生活费':'9000','储蓄':'1000'}
        source='2026-09-01 * "purchase"\n  Expenses:Food 100 CNY\n  Liabilities:Colleague -100 CNY\n    budget: "同事差旅费"\n'
        source+=self.prepay(amount='150',fund='生活费75,储蓄75')
        source+=self.receipt(credit='50',income='40',day='2026-09-03')
        result=self.run_source(source,certified=False)
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertIn('LIABILITY_CREDIT_PURPOSE_PENDING',{r['reason_code'] for r in result['review_items']})
        self.assertIsNone(result['report'])

    def test_several_receiving_cash_accounts_retain_the_joint_protocol_boundary(self):
        # This optional account is not in the ordinary one-cash equation.
        self.case.doc['accounts']['Assets:Other']={'semantics':'H','payment_scope':{'CNY':'payable'}}
        source='2026-08-01 open Assets:Other\n'+self.prepay()
        source+=self.receipt().replace('  Assets:Bank 480 CNY\n    fund: "生活费"\n',
            '  Assets:Bank 240 CNY\n    fund: "生活费"\n  Assets:Other 240 CNY\n    fund: "生活费"\n')
        result=self.run_source(source,certified=False)
        self.assertEqual(result['status'],'IMPLEMENTATION_BLOCKED')
        self.assertIn('LIABILITY_CREDIT_MIXED_UNSUPPORTED',{r['reason_code'] for r in result['review_items']})


if __name__=='__main__':unittest.main()

"""Joint H equations compose transfers and expenses, without posting-count cases."""
from fractions import Fraction
from itertools import permutations
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.monthly_report import render_text
from beancount_budget_tool.v6.transaction_details import render_transaction
import test_v6_h_redemptions as fixtures


class HBundleTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HRedemptionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def leg(self,account,amount,fund=None):
        return f'  {account} {amount} CNY\n'+(f'    fund: "{fund}"\n' if fund is not None else '')

    def body(self, *, fee_fund='生活费', fee='0.1', refund=False):
        return ('2026-09-02 * "withdraw with fee"\n'+self.leg('Assets:Bank','10','额外储蓄')+
                self.leg('Assets:Wealth','-10','额外储蓄')+self.leg('Expenses:Fee','-'+fee if refund else fee)+
                self.leg('Assets:Bank',fee if refund else '-'+fee,fee_fund))

    def event(self,report):
        return report['transactions']['rows'][0]['purpose_events'][0]['protocol']

    def outflow(self,report):return report['default_outflows']['totals'][0]

    def test_user_withdrawal_transfers_ten_and_consumes_only_point_one(self):
        report=self.f.audit(self.body())
        self.assertEqual(self.outflow(report)['expenses_paid'],'1/10')
        self.assertEqual(report['transactions']['rows'][0]['net_allocations'],{})
        e=self.event(report)
        named={report['owner_names'][k]:v for k,v in e['purpose_residuals'].items()}
        self.assertEqual(named,{'生活费':'-1/10'})
        self.assertEqual((e['asset_total'],e['expense_total']),('-1/10','1/10'))
        detail='\n'.join(render_transaction(report['transactions']['rows'][0],report['owner_names']))
        self.assertIn('资产净变动 -0.1＋费用净额 0.1＝0 CNY',detail)
        self.assertEqual(report['actual_spendable']['actual_spendable'],'999/10')

    def test_four_through_twelve_postings_share_one_equation_not_shape_specific_code(self):
        for count in range(2,11):
            with self.subTest(asset_receivers=count-1):
                openings='';legs=self.leg('Assets:Wealth',str(-(count-1)),'额外储蓄')
                for i in range(count-1):
                    name=f'Assets:Destination:N{i}'
                    self.f.doc['accounts'][name]={'semantics':'H','payment_scope':{'CNY':'payable'}}
                    openings+=f'2026-09-01 open {name}\n'
                    legs+=self.leg(name,'1','额外储蓄')
                body=openings+'2026-09-02 * "N leg withdraw"\n'+legs+self.leg('Expenses:Fee','0.1')+self.leg('Assets:Bank','-0.1','生活费')
                report=self.f.audit(body)
                self.assertEqual(self.outflow(report)['expenses_paid'],'1/10')
                self.assertEqual(self.event(report)['net_allocations'],{})
                self.assertEqual(len(self.event(report)['asset_refs']),count+1)

    def test_all_posting_permutations_have_the_same_financial_results(self):
        legs=[self.leg('Assets:Bank','10','额外储蓄'),self.leg('Assets:Wealth','-10','额外储蓄'),
              self.leg('Expenses:Fee','0.1'),self.leg('Assets:Bank','-0.1','生活费')]
        original=None
        for order in permutations(legs):
            report=self.f.audit('2026-09-02 * "reorder"\n'+''.join(order))
            values=(report['actual_spendable']['actual_spendable'],report['purpose_balances'],self.outflow(report))
            if original is None:original=values
            self.assertEqual(values,original)

    def test_multiple_fee_legs_and_split_funds_are_exactly_conserved(self):
        self.f.doc['accounts']['Assets:Wealth']['opening']['CNY']={'额外储蓄':'1700','生活费':'500'}
        report=self.f.audit('2026-09-02 * "mixed funds"\n'+self.leg('Assets:Bank','10','额外储蓄7,生活费3')+
            self.leg('Assets:Wealth','-10','生活费3,额外储蓄7')+self.leg('Expenses:Fee','0.03')+
            self.leg('Expenses:Fee','0.07')+self.leg('Assets:Bank','-0.1','生活费'))
        self.assertEqual(self.outflow(report)['expenses_paid'],'1/10')
        self.assertEqual(self.event(report)['net_allocations'],{})
        self.assertEqual(len(self.event(report)['expense_refs']),2)

    def test_missing_fee_fund_uses_default_after_declared_transfers_cancel(self):
        report=self.f.audit(self.body(fee_fund=None))
        self.assertEqual(self.outflow(report)['expenses_paid'],'1/10')
        self.assertEqual(report['transactions']['rows'][0]['postings'][-1]['h_purpose']['origin'],'inferred')

    def test_missing_transfer_fund_is_not_defaulted_to_consumption(self):
        result=self.f.audit(self.body().replace(self.leg('Assets:Bank','10','额外储蓄'),self.leg('Assets:Bank','10')),certified=False)
        self.assertIsNone(result['report'])
        self.assertIn('OWNERSHIP_UNREVIEWED',{r['reason_code'] for r in result['review_items']})
        self.assertFalse(any(r['reason_kind']=='implementation_limit' for r in result['review_items']))

    def test_fee_refund_composes_with_transfer_and_proves_only_net_benefit(self):
        report=self.f.audit(self.body(refund=True))
        self.assertEqual(self.outflow(report)['expense_refunds'],'1/10')
        self.assertEqual(self.event(report)['net_allocations'],{})
        self.assertEqual(report['actual_spendable']['actual_spendable'],'1001/10')

    def test_different_purposes_with_fee_are_data_ambiguity_not_a_program_limit(self):
        body=self.body().replace(self.leg('Assets:Bank','10','额外储蓄'),self.leg('Assets:Bank','10','旅游'))
        result=self.f.audit(body,certified=False)
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertEqual({r['reason_code'] for r in result['review_items']},{'H_BUNDLE_PURPOSE_AMBIGUOUS'})
        self.assertTrue(all('main.bean:' in r['source'] for r in result['review_items']))

    def test_pure_multi_asset_reallocation_counts_net_vector_once(self):
        report=self.f.audit('2026-09-02 * "allocate"\n'+self.leg('Assets:Wealth','-10','额外储蓄')+
            self.leg('Assets:Bank','4','生活费')+self.leg('Assets:Bank','6','旅游'))
        g=report['transactions']['rows'][0]['net_allocations']
        self.assertEqual({report['owner_names'][k]:v for k,v in g.items()},{'额外储蓄':'-10','生活费':'4','旅游':'6'})
        self.assertEqual(self.outflow(report)['expenses_paid'],'0')

    def test_voluntary_transfer_cannot_be_funded_by_a_purpose_deficit(self):
        self.f.doc['accounts']['Assets:Wealth']['opening']['CNY']={'额外储蓄':'5','生活费':'2195'}
        with self.assertRaises(BudgetError) as error:self.f.audit(self.body())
        self.assertEqual(error.exception.code,'H_OWNER_NEGATIVE')
        self.assertIn('额外储蓄',error.exception.message)
        self.assertIn('来源合计可划出：5 CNY',error.exception.message)
        self.assertIn('本次缺口：5 CNY',error.exception.message)

    def test_fee_can_make_consumption_purpose_negative_without_funding_transfer(self):
        self.f.doc['accounts']['Assets:Bank']['opening']['CNY']={'自由资金':'100'}
        report=self.f.audit(self.body())
        row=next(r for r in report['purpose_balances']['purposes'] if r['name']=='生活费')
        self.assertEqual(row['h_native_balances'][0]['units'],'-1/10')
        self.assertTrue(row['has_negative_h_position'])

    def test_several_payers_prove_total_but_do_not_invent_individual_fee_allocation(self):
        self.f.doc['accounts']['Assets:Wealth']['opening']['CNY']={'生活费':'2200'}
        self.f.doc['accounts']['Assets:Destination']={'semantics':'H','payment_scope':{'CNY':'payable'}}
        report=self.f.audit('2026-09-01 open Assets:Destination\n2026-09-02 * "joint"\n'+
            self.leg('Assets:Bank','-10','生活费')+self.leg('Assets:Wealth','-10','生活费')+
            self.leg('Assets:Destination','15','生活费')+self.leg('Expenses:Fee','5'))
        out=self.outflow(report)
        self.assertEqual(out['expenses_paid'],'5')
        self.assertIsNone(out['accounts'][0]['account'])
        self.assertEqual(out['accounts'][0]['source_accounts'],['Assets:Bank','Assets:Wealth'])
        self.assertIn('联合来源（未猜各账户分摊）',render_text(report))

    def test_exact_native_equation_does_not_use_loader_tolerance(self):
        result=self.f.audit('option "inferred_tolerance_default" "CNY:1"\n'+self.body().replace('Expenses:Fee 0.1 CNY','Expenses:Fee 0.11 CNY'),certified=False)
        self.assertIsNone(result['report'])
        self.assertEqual({r['reason_code'] for r in result['review_items']},{'H_BUNDLE_UNBALANCED'})

    def test_future_bundle_does_not_change_earlier_months(self):
        report=self.f.audit(self.body().replace('2026-09-02','2026-10-02'))
        self.assertEqual(report['transactions']['rows'],[])
        self.assertEqual(self.outflow(report)['expenses_paid'],'0')

    def test_single_paying_purpose_proves_allocation_and_fee_separately(self):
        # The outgoing purpose, not the Expenses budget name, pays the fee.
        report=self.f.audit('2026-09-02 * "first saving allocation"\n'+self.leg('Assets:Bank','-10','生活费')+
            self.leg('Assets:Wealth','10','额外储蓄')+self.leg('Expenses:Fee','0.1')+self.leg('Assets:Bank','-0.1','生活费'))
        e=self.event(report)
        self.assertEqual(self.outflow(report)['expenses_paid'],'1/10')
        self.assertEqual({report['owner_names'][k]:v for k,v in e['net_allocations'].items()},{'生活费':'-10','额外储蓄':'10'})
        self.assertEqual({report['owner_names'][k]:v for k,v in e['expense_by_owner'].items()},{'生活费':'1/10'})

    def test_mixed_source_cross_account_merge_to_one_purpose_is_explicit(self):
        self.f.doc['accounts']['Assets:Destination']={'semantics':'H','payment_scope':{'CNY':'payable'}}
        report=self.f.audit('2026-09-01 open Assets:Destination\n2026-09-02 * "mixed merge"\n'+
            self.leg('Assets:Bank','-10','生活费')+self.leg('Assets:Wealth','-10','额外储蓄')+
            self.leg('Assets:Destination','20','旅游'))
        self.assertEqual({report['owner_names'][k]:v for k,v in self.event(report)['net_allocations'].items()},
                         {'生活费':'-10','额外储蓄':'-10','旅游':'20'})

    def test_both_sides_mixed_cross_account_remap_still_requires_virtual_allocation(self):
        self.f.doc['accounts']['Assets:Destination']={'semantics':'H','payment_scope':{'CNY':'payable'}}
        result=self.f.audit('2026-09-01 open Assets:Destination\n2026-09-02 * "mixed remap"\n'+
            self.leg('Assets:Bank','-10','生活费')+self.leg('Assets:Wealth','-10','额外储蓄')+
            self.leg('Assets:Destination','20','生活费5,额外储蓄15'),certified=False)
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertEqual({r['reason_code'] for r in result['review_items']},{'H_TRANSFER_MIXED_REMAP_PENDING'})

    def test_same_account_multi_purpose_virtual_allocation_is_explicit(self):
        self.f.doc['accounts']['Assets:Bank']['opening']['CNY']={'生活费':'50','额外储蓄':'50'}
        report=self.f.audit('2026-09-02 * "virtual"\n'+self.leg('Assets:Bank','-10','生活费')+
            self.leg('Assets:Bank','-10','额外储蓄')+self.leg('Assets:Bank','20','旅游'))
        self.assertEqual({report['owner_names'][k]:v for k,v in self.event(report)['net_allocations'].items()},
                         {'生活费':'-10','额外储蓄':'-10','旅游':'20'})

    def test_unassigned_fund_is_not_a_registered_allocation_target(self):
        self.f.doc['accounts']['Assets:Wealth']['opening']['CNY']={'-':'2200'}
        report=self.f.audit('2026-09-02 * "assign"\n'+self.leg('Assets:Wealth','-10','-')+
            self.leg('Assets:Bank','4','生活费')+self.leg('Assets:Bank','6','旅游'))
        self.assertNotIn('-',self.event(report)['net_allocations'])
        self.assertEqual(sum(Fraction(v) for v in self.event(report)['net_allocations'].values()),10)

    def test_cash_expense_ref_points_to_the_paying_purpose_not_another_transfer_leg(self):
        # Match the user's same-wallet two outgoing legs with distinct purposes.
        self.f.doc['accounts']['Assets:Bank']['opening']['CNY']={'额外储蓄':'50','生活费':'50'}
        report=self.f.audit('2026-09-02 * "source and fee in one account"\n'+
            self.leg('Assets:Wealth','10','额外储蓄')+self.leg('Assets:Bank','-10','额外储蓄')+
            self.leg('Expenses:Fee','0.1')+self.leg('Assets:Bank','-0.1','生活费'))
        event=report['default_outflows']['events'][0]
        self.assertEqual(event['asset_ref'],report['transactions']['rows'][0]['postings'][-1]['ref'])

    def test_many_receivers_inherit_unique_source_inventory_without_tags(self):
        self.f.doc['accounts']['Assets:Destination']={'semantics':'H','payment_scope':{'CNY':'payable'}}
        report=self.f.audit('2026-09-01 open Assets:Destination\n2026-09-02 * "inherit"\n'+
            self.leg('Assets:Wealth','-10')+self.leg('Assets:Bank','6')+self.leg('Assets:Destination','4'))
        self.assertEqual(self.event(report)['net_allocations'],{})
        for posting in report['transactions']['rows'][0]['postings']:
            self.assertEqual(posting['h_purpose']['slices'][0]['name'],'额外储蓄')

    def test_one_receiver_inherits_complete_vector_of_many_sources(self):
        self.f.doc['accounts']['Assets:Destination']={'semantics':'H','payment_scope':{'CNY':'payable'}}
        report=self.f.audit('2026-09-01 open Assets:Destination\n2026-09-02 * "merge"\n'+
            self.leg('Assets:Wealth','-10')+self.leg('Assets:Bank','-6')+self.leg('Assets:Destination','16'))
        self.assertEqual(self.event(report)['net_allocations'],{})
        recipient=report['transactions']['rows'][0]['postings'][-1]['h_purpose']['slices']
        self.assertEqual({p['name']:p['signed_units'] for p in recipient},{'额外储蓄':'10','生活费':'6'})

    def test_mixed_inherited_sources_with_many_unspecified_receivers_are_data_review(self):
        for name in ('Assets:Destination:A','Assets:Destination:B'):
            self.f.doc['accounts'][name]={'semantics':'H','payment_scope':{'CNY':'payable'}}
        result=self.f.audit('2026-09-01 open Assets:Destination:A\n2026-09-01 open Assets:Destination:B\n'
            '2026-09-02 * "ambiguous receivers"\n'+self.leg('Assets:Wealth','-10')+
            self.leg('Assets:Bank','-6')+self.leg('Assets:Destination:A','8')+self.leg('Assets:Destination:B','8'),certified=False)
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertIn('FUND_SOURCE_UNRESOLVED',{r['reason_code'] for r in result['review_items']})
        self.assertFalse(any(r['reason_kind']=='implementation_limit' for r in result['review_items']))

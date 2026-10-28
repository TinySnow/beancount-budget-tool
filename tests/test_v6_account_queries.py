"""Native day cutoffs and account histories reconcile without future knowledge."""
import csv
from contextlib import redirect_stdout, redirect_stderr
from fractions import Fraction
from io import StringIO
import unittest
from unittest.mock import patch

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.launcher import main
from beancount_budget_tool.v6.account_queries import query, render_text, render_csv
from beancount_budget_tool.v6 import tui
import test_v6_h_redemptions as redemptions
import test_v6_native_flows as native
import test_v6_liability_budgets as debts


class AccountQueryTests(unittest.TestCase):
    def setUp(self):
        self.f = redemptions.HRedemptionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.audit(self.f.source() + self.f.source('1200', '1216.11', '-16.11', day='2026-09-06'))

    def balances(self, rows):
        return {(r['account'], r['purpose'], r['commodity'], r['basis']): Fraction(r['amount']) for r in rows if Fraction(r['amount'])}

    def history(self, **kwargs):
        return query(self.f.config, '2026-09-30', account='Assets:Wealth', **kwargs)['report']

    def test_daily_cutoff_never_uses_later_redemption(self):
        before = query(self.f.config, '2026-09-01')['report']
        after = query(self.f.config, '2026-09-02')['report']
        self.assertEqual(self.balances(before['positions'])[('Assets:Wealth','额外储蓄','CNY','H')], 2200)
        self.assertEqual(self.balances(after['positions'])[('Assets:Wealth','额外储蓄','CNY','H')], 1200)
        self.assertNotIn('max_spendable', before)
        self.f.audit(self.f.source() + self.f.source('1200', '1220', '-20', day='2026-09-06'))
        self.assertEqual(before, query(self.f.config, '2026-09-01')['report'])
        self.assertEqual(after, query(self.f.config, '2026-09-02')['report'])

    def test_history_has_posting_line_source_text_running_balances_and_reconciled_closing(self):
        result = self.history(purpose='额外储蓄')
        self.assertEqual([r['delta'] for r in result['events']], ['-1000','-1200'])
        self.assertEqual([r['balance'] for r in result['events']], ['1200','0'])
        self.assertEqual(result['closing_positions'], [])
        for row in result['events']:
            line = int(row['source'].rsplit(':',1)[1])
            self.assertIn('Assets:Wealth', self.f.ledger.read_text(encoding='utf-8').splitlines()[line-1])
            self.assertIn('redemption', row['source_text'])
            self.assertIn('行内 fund', row['reason'])
            self.assertIn(f'第 {line} 行', row['source_label'])
        cutoff = self.history(since='2026-09-03')
        self.assertEqual(cutoff['opening_positions'][0]['amount'], '1200')
        self.assertEqual([r['date'] for r in cutoff['events']], ['2026-09-06'])
        self.assertEqual(self.history(commodity='USD')['events'], [])

    def test_internal_reallocation_records_both_deltas_not_a_new_asset(self):
        source = self.f.source() + ('2026-09-03 * "move purpose"\n'
            '  Assets:Bank -10.71 CNY\n    fund: "额外储蓄"\n'
            '  Assets:Bank 10.71 CNY\n    fund: "生活费"\n')
        self.f.audit(source)
        result = query(self.f.config,'2026-09-03',account='Assets:Bank')['report']
        move = [r for r in result['events'] if r['narration']=='move purpose']
        self.assertEqual(sum(Fraction(r['delta']) for r in move), 0)
        self.assertEqual({r['purpose']:Fraction(r['delta']) for r in move}, {'额外储蓄':Fraction('-10.71'),'生活费':Fraction('10.71')})
        self.assertEqual(self.balances(result['closing_positions']), self.balances([
            r for r in query(self.f.config,'2026-09-03')['report']['positions'] if r['account']=='Assets:Bank']))

    def test_income_and_expenses_are_flows_not_inventory(self):
        self.f.audit(self.f.source(receipt='1005.71', fee='5'))
        income = query(self.f.config,'2026-09-02',account='Income:Gain')['report']
        fee = query(self.f.config,'2026-09-02',account='Expenses:Fee',purpose='生活开销')['report']
        self.assertEqual((income['events'][0]['delta'],income['events'][0]['basis']), ('1071/100','INCOME'))
        self.assertEqual((fee['events'][0]['delta'],fee['events'][0]['basis']), ('5','EXPENSE'))
        self.assertIn('累计净费用',render_text(query(self.f.config,'2026-09-02',account='Expenses:Fee')))

    def test_parent_month_review_withholds_all_query_numbers(self):
        self.f.plans['months']['2026-09']['completeness_confirmed'] = False
        self.f.audit(self.f.source(), certified=False)
        result = query(self.f.config, '2026-09-02')
        self.assertIsNone(result['report'])
        self.assertTrue(result['review_items'])
        self.assertNotIn('positions',result)
        self.assertIn('EXTERNAL_SCOPE_PENDING',render_text(result))

    def test_date_names_and_range_errors_are_specific(self):
        for day,kwargs,code in [
            ('2026-9-2',{},'QUERY_DATE_INVALID'), ('2026-02-30',{},'QUERY_DATE_INVALID'),
            ('2026-08-31',{},'QUERY_BEFORE_CUTOVER'),
            ('2026-09-02',{'since':'2026-09-03'},'QUERY_RANGE_INVALID'),
            ('2026-09-02',{'account':'Assets:Unknown'},'QUERY_ACCOUNT_UNKNOWN'),
            ('2026-09-02',{'account':'Assets:Bank','purpose':'未知'},'QUERY_PURPOSE_UNKNOWN'),
            ('2026-09-02',{'account':'Equity:Opening'},'QUERY_ACCOUNT_UNSUPPORTED')]:
            with self.subTest(code=code), self.assertRaises(BudgetError) as error:
                query(self.f.config,day,**kwargs)
            self.assertEqual(error.exception.code,code)

    def test_next_month_query_carries_balances_and_all_history(self):
        self.f.audit(self.f.source() + self.f.source('1200','1216.11','-16.11',day='2026-10-02'))
        report = query(self.f.config,'2026-10-01',account='Assets:Wealth')['report']
        self.assertEqual(report['closing_positions'][0]['amount'],'1200')
        self.assertEqual([r['date'] for r in report['events']],['2026-09-02'])

    def test_cli_exports_source_locations_and_never_overwrites_input(self):
        originals = {p:p.read_bytes() for p in self.f.base.iterdir() if p.is_file()}
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['history','--config',str(self.f.config),'--as-of','2026-09-30',
                                  '--account','Assets:Wealth','--budget','额外储蓄','--format','csv']),0)
        rows = list(csv.DictReader(StringIO(output.getvalue())))
        self.assertEqual(sum(r['section']=='change' for r in rows),2)
        self.assertTrue(all('main.bean:' in r['source'] for r in rows if r['section']=='change'))
        target = self.f.base/'用途 流水.csv'
        with redirect_stdout(StringIO()):
            self.assertEqual(main(['snapshot','--config',str(self.f.config),'--as-of','2026-09-02','--format','csv','--output',str(target)]),0)
        self.assertTrue(target.read_bytes().startswith(b'\xef\xbb\xbf'))
        with redirect_stderr(StringIO()):
            self.assertEqual(main(['snapshot','--config',str(self.f.config),'--as-of','2026-09-02','--output',str(self.f.ledger)]),2)
        self.assertEqual(originals,{p:p.read_bytes() for p in originals})

    def test_tui_uses_public_config_and_returns_from_both_views(self):
        from types import SimpleNamespace
        browser = SimpleNamespace(help=False,config=self.f.config,user_config=self.f.config,selected_month='2026-09')
        with patch.object(tui,'_read_field',side_effect=['2026-09-02']), patch.object(tui,'_message') as display:
            tui._inspect_account(None,browser)
            self.assertIn('1200', '\n'.join(display.call_args.args[2]))
        with patch.object(tui,'_read_field',side_effect=['2026-09-02','Assets:Wealth','额外储蓄']), patch.object(tui,'_message') as display:
            tui._inspect_account(None,browser,history=True)
            self.assertIn('第 14 行', '\n'.join(display.call_args.args[2]))
        with patch.object(tui,'_read_field',return_value=None), patch.object(tui,'_message') as display:
            tui._inspect_account(None,browser)
            display.assert_not_called()

    def test_later_first_use_of_k_account_does_not_invalidate_earlier_day(self):
        self.f.doc['accounts']['Assets:NewBook']={'semantics':'K','payment_scope':{'CNY':'excluded'}}
        self.f.audit('2026-09-10 open Assets:NewBook\n2026-09-20 * "subscribe later"\n'
                     '  Assets:Bank -50 CNY\n    fund: "生活费"\n'
                     '  Assets:NewBook 50 CNY\n    fund: "生活费"\n')
        before=query(self.f.config,'2026-09-02')['report']
        self.assertFalse(any(r['account']=='Assets:NewBook' for r in before['positions']))
        self.assertNotIn('Assets:NewBook',before['accounts'])
        self.assertEqual(query(self.f.config,'2026-09-20',account='Assets:NewBook')['report']['closing_positions'][0]['amount'],'50')

    def test_cutover_pad_is_source_bound_default_opening_not_income(self):
        self.f.doc['accounts']['Assets:Bank'].pop('opening')
        self.f.audit('',certified=False)
        self.f.ledger.write_text('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n2026-08-01 open Equity:Opening\n'
            '2026-09-01 pad Assets:Bank Equity:Opening\n2026-09-02 balance Assets:Bank 100 CNY\n',encoding='utf-8')
        result=query(self.f.config,'2026-09-01',account='Assets:Bank')['report']
        self.assertEqual(result['closing_positions'][0]['amount'],'100')
        self.assertEqual(result['closing_positions'][0]['purpose'],'自由资金')
        self.assertIn('generated_kind',result['events'][0]['transaction_ref'])
        self.assertIn('pad Assets:Bank',result['events'][0]['source_text'])
        self.assertIn('第 4 行',result['events'][0]['source_label'])

    def test_month_end_distribution_matches_existing_signed_month(self):
        report=self.f.audit(self.f.source())
        snapshot=query(self.f.config,'2026-09-30')['report']
        self.assertEqual(snapshot['purpose_balances'],report['purpose_balances'])

    def test_source_change_during_query_is_detected(self):
        from beancount_budget_tool.v6 import account_queries
        original=account_queries._read
        def changed(q):
            result=original(q)
            self.f.ledger.write_text(self.f.ledger.read_text(encoding='utf-8')+'\n; changed during query\n',encoding='utf-8')
            return result
        with patch.object(account_queries,'_read',side_effect=changed), self.assertRaises(BudgetError) as error:
            query(self.f.config,'2026-09-02')
        self.assertEqual(error.exception.code,'INPUT_CHANGED')

    def test_foreign_units_remain_separate_without_inventing_exchange_rates(self):
        self.f.doc['accounts']['Assets:Bank']['payment_scope']['USD']='excluded'
        self.f.doc['accounts']['Assets:Bank']['opening']['USD']={'生活费':'100'}
        self.f.write(self.f.config,self.f.doc)
        self.f.ledger.write_text(redemptions.HEADER + ('2026-08-31 * "dollars opening"\n'
            '  Assets:Bank 100 USD\n  Equity:Opening -100 USD\n'),encoding='utf-8')
        outcome=query(self.f.config,'2026-09-03',account='Assets:Bank')
        self.assertEqual(outcome['status'],'CERTIFIED_SCOPED',outcome['review_items'])
        result=outcome['report']
        self.assertEqual({r['commodity']:Fraction(r['amount']) for r in result['closing_positions']},{'CNY':100,'USD':100})
        usd=query(self.f.config,'2026-09-03',account='Assets:Bank',commodity='USD')['report']
        self.assertEqual([r['commodity'] for r in usd['closing_positions']],['USD'])
        self.assertEqual(usd['events'],[])


class SpecialAccountQueryTests(unittest.TestCase):
    def fixture(self, cls):
        f=cls();f.setUp();self.addCleanup(f.doCleanups);return f

    def test_k_flows_close_to_zero_and_zero_gifts_keep_sources_without_fake_value(self):
        f=self.fixture(native.NativeFlowTests)
        f.report(f.redemption()+f.zero_records())
        subscribed=query(f.config,'2026-09-02',account='Assets:Book')['report']
        self.assertEqual(subscribed['closing_positions'][0]['amount'],'100')
        self.assertEqual(subscribed['closing_positions'][0]['basis'],'K')
        result=query(f.config,'2026-09-30',account='Assets:Book')['report']
        self.assertEqual([r['delta'] for r in result['events']],['100','-100','0','0'])
        self.assertEqual(result['closing_positions'],[])
        self.assertIn('赠送份额和价值未记录',result['events'][-1]['reason'])
        self.assertNotEqual(result['events'][-1]['refs'],result['events'][-2]['refs'])

    def test_liability_purposes_keep_opposite_balances_when_account_net_zero(self):
        f=self.fixture(debts.PostingLiabilityBudgetTests)
        f.audit(f.purchase('生活费')+f.payment('旅游'))
        result=query(f.fixture.config,'2026-09-03',account='Liabilities:Card')['report']
        balances={r['purpose']:Fraction(r['amount']) for r in result['closing_positions']}
        self.assertEqual(balances,{'生活费':-100,'旅游':100})
        selected=query(f.fixture.config,'2026-09-03',account='Liabilities:Card',purpose='旅游')['report']
        self.assertEqual(len(selected['events']),1)
        self.assertEqual(selected['events'][0]['delta'],'100')

    def test_partial_debt_payments_and_clearance_reconcile_at_each_day(self):
        f=self.fixture(debts.PostingLiabilityBudgetTests)
        f.audit(f.purchase('生活费60,旅游40')+f.payment('生活费','60')+f.payment('旅游','40','2026-09-04'))
        for day,expected in [('2026-09-02',{'生活费':-60,'旅游':-40}),('2026-09-03',{'旅游':-40}),('2026-09-04',{})]:
            result=query(f.fixture.config,day,account='Liabilities:Card')['report']
            self.assertEqual({r['purpose']:Fraction(r['amount']) for r in result['closing_positions']},expected)

    def test_pool_history_uses_book_cost_and_labels_nominal_reallocation(self):
        from pathlib import Path
        import yaml
        from beancount_budget_tool.v6.monthly_report import audit_month_workflow
        f=self.fixture(native.NativeFlowTests)
        source = ('2026-08-01 open Assets:Stock\n'
                  '2026-09-02 * "repo buy"\n  Assets:Cash -50 CNY\n  Assets:Stock 5 SEC {10 CNY}\n'
                  '2026-09-03 * "repo maturity"\n  Assets:Cash 55 CNY\n  Assets:Stock -5 SEC {10 CNY}\n'
                  '  Income:Bonus -5 CNY\n')
        f.configure(source)
        def read(path):return yaml.safe_load(path.read_text(encoding='utf-8'))
        def save(path,doc):path.write_text(yaml.safe_dump(doc,allow_unicode=True),encoding='utf-8')
        config=read(f.config);base=f.config.parent
        accounts=read(base/config['accounts'])
        for row in accounts['accounts']:
            if row['account'] in ('Assets:Cash','Assets:Stock'):row['payment_scope']={'CNY':'excluded'} if row['account']=='Assets:Cash' else {'SEC':'excluded'}
        save(base/config['accounts'],accounts)
        opening=read(base/config['opening']);opening['h_holdings']=[r for r in opening['h_holdings'] if r['account']!='Assets:Cash'];save(base/config['opening'],opening)
        owners=read(base/config['ownership']);owners['owners'] += [{'id':i,'name':n,'aliases':[]} for i,n in [('reserve','储蓄'),('invest','投资')]];save(base/config['ownership'],owners)
        plans=read(base/config['purpose_plans'])
        plans.pop('months',None)
        plans.setdefault('defaults',{})['allocation_targets']={r['id']:'not_assessed' for r in owners['owners']};save(base/config['purpose_plans'],plans)
        save(base/config['purpose_pools'],{'schema':6,'cutover':'2026-09-01','report_currency':'CNY','pools':[{
            'id':'broker','accounts':['Assets:Cash','Assets:Stock'],'reserve_order':['Assets:Cash','Assets:Stock'],
            'payment_scope':'excluded','protected_owner_id':'reserve','protected_label':'储蓄','protected_principal':'80',
            'remainder_owner_id':'invest','remainder_label':'投资','reason':'virtual pool'}]})
        report=audit_month_workflow(f.config)
        self.assertEqual(report['status'],'CERTIFIED_SCOPED',report['review_items'])
        result=query(f.config,'2026-09-02',account='Assets:Stock')['report']
        self.assertEqual({r['purpose']:Fraction(r['amount']) for r in result['closing_positions']},{'储蓄':30,'投资':20})
        self.assertTrue(all(r['commodity']=='CNY' and r['basis']=='POOL' for r in result['events']))
        self.assertTrue(all('不代表真实账户间划拨' in r['reason'] for r in result['events']))
        self.assertEqual(query(f.config,'2026-09-03',account='Assets:Stock')['report']['closing_positions'],[])

    def test_legacy_account_rule_debt_and_excess_credit_history(self):
        import test_v6_liability_rules as legacy
        f=self.fixture(legacy.LiabilityRuleTests)
        f.run_ledger(f.purchase()+f.payment(amount='120'))
        before=query(f.config,'2026-09-02',account='Liabilities:Card',purpose='生活费')['report']
        self.assertEqual(before['closing_positions'][0]['amount'],'-100')
        after=query(f.config,'2026-09-03',account='Liabilities:Card',purpose='生活费')['report']
        self.assertEqual(after['closing_positions'][0]['amount'],'20')
        self.assertEqual([r['delta'] for r in after['events']],['-100','120'])

    def test_advanced_sidecar_edit_during_query_invalidates_view(self):
        from beancount_budget_tool.v6 import account_queries
        f=self.fixture(native.NativeFlowTests)
        f.report(f.redemption())
        original=account_queries._read
        def changed(q):
            result=original(q)
            path=q.paths['ownership']
            path.write_text(path.read_text(encoding='utf-8')+'\n# edited during query\n',encoding='utf-8')
            return result
        with patch.object(account_queries,'_read',side_effect=changed), self.assertRaises(BudgetError) as error:
            query(f.config,'2026-09-02')
        self.assertEqual(error.exception.code,'INPUT_CHANGED')

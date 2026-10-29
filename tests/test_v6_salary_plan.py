"""Monthly bookkeeping and persistent payday limits have independent boundaries."""

from contextlib import redirect_stdout
from copy import deepcopy
from fractions import Fraction
from io import StringIO
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple,audit_simple_period,build_runtime,create_files
from beancount_budget_tool.v6.monthly_report import render_workflow_text
from beancount_budget_tool.v6.period_report import main as report_main,render_csv
from beancount_budget_tool.v6.salary_plan import audit_configured_cycle
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.tui import _select_salary_cycle
from beancount_budget_tool.v6.tui_export import write_export
from test_v6_tui import FakeScreen


class SalaryPlanTests(unittest.TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve()
        self.config=self.base/'config.yml';self.budgets=self.base/'budgets.yml'
        self.ledger=self.base/'main.bean'
        self.ledger.write_text('option "operating_currency" "CNY"\n'
            '2023-07-01 open Assets:Cash\n2023-07-01 open Income:Salary\n2023-07-01 open Expenses:Food\n'
            '2023-07-01 * "seed"\n  Assets:Cash 1000 CNY\n  Income:Salary -1000 CNY\n'
            '2023-07-07 * "previous cycle"\n  Expenses:Food 9 CNY\n  Assets:Cash -9 CNY\n'
            '2023-07-08 * "payday purchase"\n  Expenses:Food 10 CNY\n  Assets:Cash -10 CNY\n'
            '2023-07-31 * "July end"\n  Expenses:Food 20 CNY\n  Assets:Cash -20 CNY\n'
            '2023-08-01 * "old cycle final day"\n  Expenses:Food 5 CNY\n  Assets:Cash -5 CNY\n'
            '2023-08-02 * "next payday purchase"\n  Expenses:Food 7 CNY\n  Assets:Cash -7 CNY\n'
            '2023-08-30 * "refund"\n  Expenses:Food -2 CNY\n  Assets:Cash 2 CNY\n',encoding='utf-8')
        self.profile={'schema':1,'ledger':'main.bean','budgets':'budgets.yml','cutover':'2023-07-01',
            'month':'2023-08','report_currency':'CNY','expense_budget_basis':'salary_cycle',
            'defaults':{'income_purpose':'生活费','expense_purpose':'生活费','expense_category':'生活开销'},
            'accounts':{'Assets:Cash':{'semantics':'H','payment_scope':{'CNY':'payable'}}}}
        self.plans={'schema':1,'months':{'2023-07':{'completeness_confirmed':True},'2023-08':{'completeness_confirmed':True}},
            'salary_cycles':{'2023-07-08':{'expense_targets':{'生活开销':'100'}},'2023-08-02':{'expense_targets':{'生活开销':'200'}}}}
        self.write(self.config,self.profile);self.write(self.budgets,self.plans)

    def write(self,path,doc):
        path.write_text(yaml.safe_dump(doc,allow_unicode=True,sort_keys=False),encoding='utf-8')

    def test_july_monthly_cap_uses_payday_expenses_without_calendar_goal(self):
        result=audit_simple(self.config,'2023-07')
        self.assertEqual(result['status'],'CERTIFIED_SCOPED',result['review_items'])
        report=result['report'];cap=report['max_spendable'];cycle=report['salary_cycle']
        self.assertEqual(cap['max_spendable'],'70')
        self.assertEqual(cap['basis'],'salary_cycle')
        self.assertEqual(cycle['report']['cycle_budget']['net_spent'],'30')
        self.assertEqual(cycle['through'],'2023-07-31')
        self.assertEqual(cycle['next_payday'],'2023-08-02')
        monthly=next(row for row in report['expense_targets']['categories'] if row['name']=='生活开销')
        self.assertEqual(monthly['subtree_spent'],'39')
        self.assertIn('发薪周期剩余额度 70',render_workflow_text(result))
        text = render_workflow_text(result)
        self.assertIn('发薪周期进度（截至月末 2023-07-31）', text)
        self.assertIn('完整发薪周期：2023-07-08 至 2023-08-01', text)
        self.assertIn('按 w 查看完整周期消费', text)

    def test_august_starts_new_cycle_including_all_of_next_payday(self):
        result=audit_simple(self.config)
        self.assertEqual(result['status'],'CERTIFIED_SCOPED',result['review_items'])
        cap=result['report']['max_spendable']
        self.assertEqual(cap['cycle_start'],'2023-08-02')
        self.assertEqual(cap['max_spendable'],'195')
        self.assertIsNone(cap['next_payday'])
        self.assertEqual(result['report']['salary_cycle']['report']['cycle_budget']['refunded'],'2')
        self.assertIn('完整周期终点未定；跨月消费继续累计', render_workflow_text(result))

    def test_closed_saved_cycle_includes_august_first_excludes_next_payday(self):
        result=audit_configured_cycle(self.config,'2023-07-08')
        self.assertEqual(result['status'],'CERTIFIED_SCOPED')
        self.assertEqual(result['through'],'2023-08-01')
        self.assertEqual(result['next_payday'],'2023-08-02')
        self.assertEqual(result['report']['cycle_budget']['remaining'],'65')
        self.assertEqual({row['narration'] for row in result['report']['events']},{'payday purchase','July end','old cycle final day'})

    def test_june_ninth_cycle_includes_july_fourteenth_but_not_fifteenth(self):
        replacements = {'2023-07-01': '2023-06-01', '2023-07-07': '2023-06-08',
                        '2023-07-08': '2023-06-09', '2023-07-31': '2023-06-30',
                        '2023-08-01': '2023-07-01', '2023-08-02': '2023-07-15',
                        '2023-08-30': '2023-07-30'}
        import re
        text = re.sub(r'2023-\d{2}-\d{2}', lambda match: replacements[match[0]],
                      self.ledger.read_text(encoding='utf-8'))
        text += ('\n2023-07-14 * "last day purchase"\n'
                 '  Expenses:Food 11 CNY\n  Assets:Cash -11 CNY\n')
        self.ledger.write_text(text, encoding='utf-8')
        self.profile.update(cutover='2023-06-01', month='2023-06')
        self.plans['months'] = {month: {'completeness_confirmed': True}
                                for month in ('2023-06', '2023-07')}
        self.plans['salary_cycles'] = {'2023-06-09': {'expense_targets': {'生活开销': '100'}},
                                      '2023-07-15': {'expense_targets': {'生活开销': '200'}}}
        self.write(self.config, self.profile); self.write(self.budgets, self.plans)
        before = self.ledger.read_bytes()
        browser = SimpleReportBrowser(self.config, '2023-06')
        self.addCleanup(browser.close)
        browser.select_saved_cycle('2023-06-09'); browser.refresh()
        self.assertIsNone(browser.error)
        result = browser.outcome
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result['review_items'])
        self.assertEqual((result['from'], result['through'], result['next_payday']),
                         ('2023-06-09', '2023-07-14', '2023-07-15'))
        self.assertEqual(result['report']['cycle_budget']['net_spent'], '46')
        self.assertEqual(result['report']['cycle_budget']['remaining'], '54')
        self.assertEqual({row['date'] for row in result['report']['events']},
                         {'2023-06-09', '2023-06-30', '2023-07-01', '2023-07-14'})
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_as_of_truncates_a_saved_cycle_and_late_cutoff_cannot_extend_it(self):
        early=audit_configured_cycle(self.config,'2023-07-08','2023-07-20')
        self.assertEqual(early['report']['cycle_budget']['remaining'],'90')
        later=audit_configured_cycle(self.config,'2023-07-08','2023-09-30')
        self.assertEqual(later['through'],'2023-08-01')
        self.assertEqual(later['report']['cycle_budget']['remaining'],'65')

    def test_a_cycle_crosses_month_end_without_resetting_or_double_counting(self):
        self.plans['salary_cycles'].pop('2023-08-02');self.write(self.budgets,self.plans)
        july=audit_simple(self.config,'2023-07');august=audit_simple(self.config,'2023-08')
        self.assertEqual(july['report']['max_spendable']['max_spendable'],'70')
        self.assertEqual(august['report']['max_spendable']['max_spendable'],'60')
        self.assertEqual(august['report']['max_spendable']['cycle_start'],'2023-07-08')

    def test_certified_payable_cash_is_still_an_upper_bound(self):
        self.plans['salary_cycles']['2023-08-02']['expense_targets']['生活开销']='2000';self.write(self.budgets,self.plans)
        result=audit_simple(self.config)['report']
        self.assertEqual(result['max_spendable']['max_spendable'],result['actual_spendable']['actual_spendable'])
        self.assertEqual(result['max_spendable']['consumption_target_remaining'],'1995')

    def test_overspending_is_a_certified_negative_not_a_review(self):
        self.plans['salary_cycles']['2023-08-02']['expense_targets']['生活开销']='1';self.write(self.budgets,self.plans)
        result=audit_simple(self.config)
        self.assertEqual(result['status'],'CERTIFIED_SCOPED')
        self.assertEqual(result['report']['max_spendable']['max_spendable'],'-4')

    def test_missing_cycle_or_target_produces_located_review(self):
        for cycles,code in (({},'SALARY_CYCLE_MISSING'),({'2023-08-02':{'expense_targets':{}}},'SALARY_CYCLE_TARGET_MISSING'),
                            ({'2023-08-02':{'expense_targets':{'生活开销':None}}},'SALARY_CYCLE_TARGET_MISSING')):
            with self.subTest(code=code,cycles=cycles):
                self.plans['salary_cycles']=cycles;self.write(self.budgets,self.plans)
                result=audit_simple(self.config)
                self.assertEqual(result['status'],'NEEDS_REVIEW')
                self.assertIsNone(result['report'])
                self.assertTrue(any(row['reason_code']==code for row in result['review_items']))
                self.assertTrue(all(str(self.budgets)+':' in row['source'] for row in result['review_items']))

    def test_not_assessed_is_explicit_and_does_not_block_bookkeeping(self):
        self.plans['salary_cycles']['2023-08-02']['expense_targets']['生活开销']='not_assessed';self.write(self.budgets,self.plans)
        result=audit_simple(self.config)
        self.assertEqual(result['status'],'CERTIFIED_SCOPED')
        self.assertEqual(result['report']['max_spendable']['status'],'NOT_ASSESSED')
        self.assertIsNone(result['report']['max_spendable']['max_spendable'])

    def test_explicit_calendar_goal_is_independent_and_not_used_as_salary_cap(self):
        self.plans['months']['2023-08']['expense_targets']={'生活开销':'1000'};self.write(self.budgets,self.plans)
        result=audit_simple(self.config)
        self.assertEqual(result['status'],'CERTIFIED_SCOPED')
        self.assertEqual(result['report']['max_spendable']['max_spendable'],'195')
        root=next(r for r in result['report']['expense_targets']['categories'] if r['name']=='生活开销')
        self.assertEqual(root['subtree_remaining'],'990')

    def test_saved_source_edit_recomputes_and_restart_retains_the_target(self):
        browser=SimpleReportBrowser(self.config);self.addCleanup(browser.close)
        browser.refresh();self.assertEqual(browser.outcome['report']['max_spendable']['max_spendable'],'195')
        self.plans['salary_cycles']['2023-08-02']['expense_targets']['生活开销']='50';self.write(self.budgets,self.plans)
        browser.refresh();self.assertEqual(browser.outcome['report']['max_spendable']['max_spendable'],'45')
        self.assertEqual(audit_simple(self.config)['report']['max_spendable']['max_spendable'],'45')

    def test_tui_w_selects_saved_cycle_in_one_prompt_and_exports_it(self):
        browser=SimpleReportBrowser(self.config);self.addCleanup(browser.close);browser.refresh()
        with patch('beancount_budget_tool.v6.tui._read_field',return_value='2023-07-08') as field:
            _select_salary_cycle(FakeScreen(),browser)
        self.assertEqual(field.call_count,1)
        self.assertEqual(browser.saved_cycle,'2023-07-08')
        self.assertEqual(browser.outcome['report']['cycle_budget']['remaining'],'65')
        write_export(browser.outcome,self.config,self.base/'salary.csv','csv',period=True)
        self.assertIn('cycle_remaining,65',(self.base/'salary.csv').read_text(encoding='utf-8-sig'))
        browser.toggle_period();browser.refresh()
        self.assertEqual(browser.outcome['report']['max_spendable']['max_spendable'],'195')
        browser.toggle_period();browser.refresh()
        self.assertEqual(browser.outcome['report']['cycle_budget']['remaining'],'65')

    def test_period_csv_keeps_cycle_snapshots_not_a_sum_of_remaining_allowances(self):
        result=audit_simple_period(self.config,'2023-07','2023-08')
        self.assertEqual(result['status'],'CERTIFIED_SCOPED',result.get('review_items'))
        self.assertEqual(result['report']['closing']['max_spendable']['max_spendable'],'195')
        csv=render_csv(result)
        self.assertIn('salary_cycle',csv)
        self.assertIn('跨月不重置',csv)

    def test_cli_saved_cycle_needs_no_repeated_quota(self):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(report_main(['--config',str(self.config),'--cycle','2023-07-08','--format','json']),0)
        result=json.loads(output.getvalue())
        self.assertEqual(result['report']['cycle_budget']['remaining'],'65')
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(report_main(['--config',str(self.config),'--cycle','current','--format','json']),0)
        self.assertEqual(json.loads(output.getvalue())['report']['cycle_budget']['remaining'],'195')

    def test_unquoted_decimal_target_keeps_exact_source_precision(self):
        text=self.budgets.read_text(encoding='utf-8').replace("'100'",'100.000000000000000001').replace("'200'",'200.000000000000000001')
        self.budgets.write_text(text,encoding='utf-8')
        result=audit_simple(self.config)
        self.assertEqual(Fraction(result['report']['max_spendable']['max_spendable']),Fraction('195.000000000000000001'))

    def test_invalid_cycles_have_budget_file_locations(self):
        original=deepcopy(self.plans)
        for cycles in ([],{'2023-02-30':{'expense_targets':{}}},{'2023-08-02':{'extra':1}},
                       {'2023-08-02':{'expense_targets':{'生活开销':-1}}},
                       {'2023-08-02':{'expense_targets':{'生活开销':True}}}):
            self.plans=deepcopy(original);self.plans['salary_cycles']=cycles;self.write(self.budgets,self.plans)
            with self.assertRaises(BudgetError) as caught:audit_simple(self.config)
            self.assertTrue(caught.exception.source.startswith(str(self.budgets)+':'))

    def test_payday_under_months_points_to_salary_cycles_instead_of_asking_for_more_quotes(self):
        self.write(self.budgets,{'schema':1,'months':{'2023-08-14':{'expense_targets':{'生活开销':'0'}}}})
        with self.assertRaises(BudgetError) as caught:audit_simple(self.config,'2023-07')
        self.assertEqual(caught.exception.code,'PLAN_AMBIGUOUS')
        self.assertIn('months 只接受自然月键',caught.exception.message)
        self.assertIn('顶层 salary_cycles',caught.exception.message)
        self.assertIn('2023-08-14',caught.exception.message)
        self.assertTrue(caught.exception.source.startswith(str(self.budgets)+':'))

    def first_august_payday(self):
        self.plans['salary_cycles']={'2023-08-14':{'expense_targets':{'生活开销':'0'}}}
        self.write(self.budgets,self.plans)

    def test_july_before_first_payday_can_certify_bookkeeping_without_inventing_a_cap(self):
        self.first_august_payday()
        original=self.ledger.read_bytes()
        result=audit_simple(self.config,'2023-07')
        self.assertEqual(result['status'],'CERTIFIED_SCOPED',result['review_items'])
        report=result['report']
        self.assertEqual(report['salary_cycle']['status'],'NOT_STARTED')
        self.assertEqual(report['salary_cycle']['first_payday'],'2023-08-14')
        self.assertEqual(report['actual_spendable']['status'],'CERTIFIED')
        self.assertEqual(report['max_spendable']['status'],'NOT_ASSESSED')
        self.assertIsNone(report['max_spendable']['max_spendable'])
        self.assertIn('工资周期目标尚未生效',render_workflow_text(result))
        self.assertEqual(self.ledger.read_bytes(),original)

    def test_first_payday_activates_a_real_zero_goal_without_charging_earlier_consumption(self):
        self.first_august_payday()
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8')+
            '\n2023-08-20 * "after first salary"\n  Expenses:Food 20 CNY\n  Assets:Cash -20 CNY\n',encoding='utf-8')
        result=audit_simple(self.config)
        self.assertEqual(result['status'],'CERTIFIED_SCOPED',result['review_items'])
        cycle=result['report']['salary_cycle']['report']['cycle_budget']
        self.assertEqual(cycle['target'],'0')
        self.assertEqual(cycle['net_spent'],'18')
        self.assertEqual(result['report']['max_spendable']['max_spendable'],'-18')

    def test_independent_before_first_query_reports_not_started_not_missing_or_zero(self):
        self.first_august_payday()
        result=audit_configured_cycle(self.config,as_of='2023-07-31')
        self.assertEqual(result['status'],'NOT_STARTED')
        self.assertIsNone(result['from'])
        self.assertIsNone(result['report'])
        self.assertEqual(result['review_items'],[])
        from beancount_budget_tool.v6.salary_cycle import render_text,render_csv as salary_csv
        self.assertIn('工资周期目标尚未生效',render_text(result))
        self.assertNotIn('处理待审',render_text(result))
        self.assertIn('salary_cycle_not_started',salary_csv(result))

    def test_tui_before_first_salary_keeps_month_view_and_explains_future_cycle(self):
        self.first_august_payday()
        browser=SimpleReportBrowser(self.config,'2023-07');self.addCleanup(browser.close);browser.refresh()
        with patch('beancount_budget_tool.v6.tui._message') as message,patch('beancount_budget_tool.v6.tui._read_field') as field:
            _select_salary_cycle(FakeScreen(),browser)
        self.assertFalse(browser.period)
        field.assert_not_called()
        self.assertEqual(message.call_args.args[1],'工资周期目标尚未生效')

    def test_before_first_period_snapshot_is_explicit_in_cumulative_csv(self):
        self.first_august_payday()
        result=audit_simple_period(self.config,'2023-07','2023-08')
        self.assertEqual(result['status'],'CERTIFIED_SCOPED',result['review_items'])
        self.assertIn('salary_cycle,',render_csv(result))
        self.assertIn('NOT_STARTED',render_csv(result))

    def test_before_first_period_does_not_approve_missing_bookkeeping_evidence(self):
        self.first_august_payday()
        self.plans['months']['2023-07'].pop('completeness_confirmed');self.write(self.budgets,self.plans)
        result=audit_simple(self.config,'2023-07')
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertTrue(any(row['reason_code']=='EXTERNAL_SCOPE_PENDING' for row in result['review_items']))

    def test_advanced_sidecar_decimal_has_the_same_exact_amount_semantics(self):
        from beancount_budget_tool.v6.salary_plan import audit_plan,month_end
        runtime=build_runtime(self.config);self.addCleanup(runtime.close)
        path=runtime.config.parent/'salary_cycle.yaml'
        text=path.read_text(encoding='utf-8')
        self.assertIn("'200'",text)
        path.write_text(text.replace("'200'",'200.000000000000000001'),encoding='utf-8')
        result=audit_plan(runtime.config,path,month_end('2023-08'))
        self.assertEqual(Fraction(result['report']['cycle_budget']['remaining']),Fraction('195.000000000000000001'))

    def test_unmodified_calendar_mode_retains_its_existing_cap(self):
        self.profile.pop('expense_budget_basis');self.write(self.config,self.profile)
        self.plans['defaults']={'expense_targets':{'生活开销':'1000'}};self.write(self.budgets,self.plans)
        result=audit_simple(self.config)
        self.assertEqual(result['status'],'CERTIFIED_SCOPED')
        self.assertNotIn('salary_cycle',result['report'])
        self.assertEqual(result['report']['max_spendable']['max_spendable'],'951')

    def test_next_payday_multiple_expenses_all_belong_to_the_new_cycle(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8')+
            '\n2023-08-02 * "second purchase"\n  Expenses:Food 3 CNY\n  Assets:Cash -3 CNY\n',encoding='utf-8')
        first=audit_configured_cycle(self.config,'2023-07-08')
        second=audit_simple(self.config)
        self.assertEqual(first['report']['cycle_budget']['remaining'],'65')
        self.assertEqual(second['report']['max_spendable']['max_spendable'],'192')

    def test_multiple_category_targets_are_separate_subtree_limits(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8').replace('Expenses:Food 7 CNY',
            'Expenses:Food 7 CNY\n    budget: "生活开销.餐饮"'),encoding='utf-8')
        self.plans['salary_cycles']['2023-08-02']['expense_targets']['生活开销.餐饮']='10'
        self.write(self.budgets,self.plans)
        result=audit_configured_cycle(self.config,'2023-08-02')
        targets={row['category_name']:row for row in result['report']['cycle_targets']}
        self.assertEqual(targets['生活开销']['remaining'],'195')
        self.assertEqual(targets['生活开销.餐饮']['remaining'],'3')
        from beancount_budget_tool.v6.salary_cycle import render_text,render_csv as salary_csv
        self.assertIn('生活开销.餐饮：周期额度 10',render_text(result))
        self.assertIn('生活开销.餐饮,cycle_remaining,3',salary_csv(result))

    def test_cycle_configuration_does_not_approve_month_end_completeness(self):
        self.plans['months']['2023-08'].pop('completeness_confirmed')
        self.write(self.budgets,self.plans)
        result=audit_simple(self.config)
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertTrue(any(row['reason_code']=='EXTERNAL_SCOPE_PENDING' for row in result['review_items']))

    def test_period_before_cutover_is_reviewed_at_the_budget_source(self):
        self.plans['salary_cycles']={'2023-06-08':{'expense_targets':{'生活开销':'200'}}}
        self.write(self.budgets,self.plans)
        result=audit_simple(self.config,'2023-07')
        self.assertEqual(result['status'],'NEEDS_REVIEW')
        self.assertTrue(any(row['reason_code']=='SALARY_CYCLE_BEFORE_CUTOVER' and str(self.budgets)+':' in row['source'] for row in result['review_items']))

    def test_tui_open_cycle_uses_the_viewed_month_instead_of_the_default_month(self):
        self.profile['month']='2023-07';self.write(self.config,self.profile)
        browser=SimpleReportBrowser(self.config,'2023-08');self.addCleanup(browser.close)
        browser.refresh();browser.select_saved_cycle('2023-08-02');browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome['through'],'2023-08-31')
        self.assertEqual(browser.outcome['report']['cycle_budget']['remaining'],'195')

    def test_budget_edited_during_cycle_audit_invalidates_the_result(self):
        from beancount_budget_tool.v6.salary_cycle import audit_salary_cycle
        def changed(*args,**kwargs):
            result=audit_salary_cycle(*args,**kwargs)
            self.budgets.write_text(self.budgets.read_text(encoding='utf-8')+'\n# concurrent edit\n',encoding='utf-8')
            return result
        with patch('beancount_budget_tool.v6.salary_cycle.audit_salary_cycle',side_effect=changed):
            with self.assertRaises(BudgetError) as caught:audit_simple(self.config)
        self.assertEqual(caught.exception.code,'INPUT_CHANGED')

    def test_target_aliases_resolve_to_one_category_and_cannot_double_declare(self):
        from beancount_budget_tool.v6.salary_plan import audit_plan,month_end
        runtime=build_runtime(self.config);self.addCleanup(runtime.close)
        path=runtime.config.parent/'classifications.yaml'
        registry=yaml.safe_load(path.read_text(encoding='utf-8'))
        next(row for row in registry['categories'] if row['name']=='生活开销')['aliases']=['日常']
        self.write(path,registry)
        planpath=runtime.config.parent/'salary_cycle.yaml'
        plan=yaml.safe_load(planpath.read_text(encoding='utf-8'))
        plan['category']='日常';plan['cycles']['2023-08-02']['expense_targets']={'日常':'200'}
        self.write(planpath,plan)
        self.assertEqual(audit_plan(runtime.config,planpath,month_end('2023-08'))['report']['cycle_budget']['remaining'],'195')
        plan['cycles']['2023-08-02']['expense_targets']['生活开销']='200';self.write(planpath,plan)
        with self.assertRaises(BudgetError) as caught:audit_plan(runtime.config,planpath,month_end('2023-08'))
        self.assertEqual(caught.exception.code,'SALARY_CYCLE_TARGET_DUPLICATE')

    def test_edit_between_saved_plan_read_and_runtime_build_is_not_silently_accepted(self):
        from beancount_budget_tool.v6.simple_config import build_runtime as build
        def changed(*args,**kwargs):
            self.budgets.write_text(self.budgets.read_text(encoding='utf-8')+'\n# changed before compilation\n',encoding='utf-8')
            return build(*args,**kwargs)
        with patch('beancount_budget_tool.v6.simple_config.build_runtime',side_effect=changed):
            with self.assertRaises(BudgetError) as caught:audit_configured_cycle(self.config,'2023-08-02')
        self.assertEqual(caught.exception.code,'INPUT_CHANGED')

    def test_advanced_export_and_remigration_preserve_salary_basis(self):
        runtime=build_runtime(self.config)
        self.addCleanup(runtime.close)
        result=create_files(self.base/'migrated',old_config=runtime.config)
        after=audit_simple(Path(result['config']))
        self.assertEqual(after['status'],'CERTIFIED_SCOPED',after.get('review_items'))
        self.assertEqual(after['report']['max_spendable']['max_spendable'],'195')


if __name__=='__main__':unittest.main()

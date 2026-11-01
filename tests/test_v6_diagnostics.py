"""Public diagnostics prove before-state and never authorize pending money."""

from contextlib import redirect_stdout
from datetime import date
from fractions import Fraction
import io
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.launcher import main
from beancount_budget_tool.runtime_inputs import input_bytes
from beancount_budget_tool.v6.diagnostics import doctor, attribution, explain, render_text, render_csv
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.simple_config import audit_simple, build_runtime
from beancount_budget_tool.query import QuerySession


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        example = Path(__file__).resolve().parents[1] / 'examples/v6/simple'
        shutil.copytree(example, self.base, dirs_exist_ok=True)
        self.config = self.base/'config.yml'
        self.ledger = self.base/'main.bean'

    def append(self, text):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8')+'\n'+text, encoding='utf-8')

    def mixed(self):
        self.append('''2026-09-04 * "ambiguous"
  Assets:Cash -5 CNY
  Expenses:Food 5 CNY
2026-09-05 * "explicit after unknown"
  Assets:Cash -1 CNY
    fund: "生活费"
  Expenses:Food 1 CNY
2026-09-06 * "still unknown"
  Assets:Cash -1 CNY
  Expenses:Food 1 CNY
''')

    def test_static_does_not_replay_and_reports_not_run(self):
        with patch('beancount_budget_tool.v6.diagnostics._context', side_effect=AssertionError('replay')):
            result = doctor(self.config, replay=False)
        self.assertEqual(result['checks']['static']['state'], 'passed')
        self.assertEqual(result['checks']['replay']['state'], 'not_run')
        self.assertIsNone(result['report'])
        self.assertIn('重放审计：未运行', render_text(result))

    def test_duplicate_group_is_located_and_replay_not_claimed(self):
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['account_groups'] = {'H': {'payable':['Assets:Cash'], 'excluded':['Assets:Cash']}}
        self.config.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
        result = doctor(self.config)
        self.assertEqual(result['checks']['static']['state'], 'error')
        self.assertEqual(result['checks']['replay']['state'], 'not_run')
        issue = result['checks']['static']['issues'][0]
        self.assertEqual(issue['reason_code'], 'SIMPLE_ACCOUNT_DUPLICATE')
        self.assertTrue(issue['source'].startswith(str(self.config)))

    def test_static_success_does_not_hide_ledger_parse_failure(self):
        self.append('not valid beancount')
        self.assertEqual(doctor(self.config, replay=False)['checks']['static']['state'], 'passed')
        result = doctor(self.config)
        self.assertEqual(result['checks']['replay']['state'], 'error')
        self.assertIsNone(result['report'])

    def test_workspace_before_vector_unknown_and_filters_are_read_only(self):
        self.mixed()
        before = {p: p.read_bytes() for p in self.base.iterdir() if p.is_file()}
        result = attribution(self.config, all_rows=True)
        names = result['owner_names']
        pending = next(row for row in result['rows'] if row['narration']=='ambiguous')
        self.assertEqual({names[k]:v for k,v in pending['before'].items()}, {'自由资金':'300', '生活费':'190'})
        self.assertEqual(pending['reason_code'], 'H_OWNER_PENDING')
        self.assertIn('Expenses:Food 5 CNY', pending['source_text'])
        later = next(row for row in result['rows'] if row['narration']=='explicit after unknown')
        self.assertIsNone(later['before'])
        self.assertEqual(later['before_state'], 'pending')
        self.assertEqual(later['evidence'], 'inline')
        pending_only = attribution(self.config)
        self.assertFalse(any(row['narration']=='explicit after unknown' for row in pending_only['rows']))
        self.assertGreater(pending_only['summary']['unknown_before_h_legs'], 0)
        filtered = attribution(self.config, reason='H_OWNER_PENDING', amount='-5')
        self.assertEqual(len(filtered['rows']), 1)
        self.assertEqual(filtered['summary']['h_postings'], result['summary']['h_postings'])
        self.assertEqual(filtered['review_items'], result['review_items'])
        self.assertEqual(before, {p:p.read_bytes() for p in before})
        self.assertFalse((self.base/'.budget-state').exists())
        self.assertIn('DIAGNOSTIC_ONLY', render_csv(filtered))

    def test_both_virtual_transfer_legs_capture_the_same_before_state(self):
        result = attribution(self.config, all_rows=True)
        rows = [r for r in result['rows'] if r['narration']=='正式分出生活费']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['before'], rows[1]['before'])
        self.assertEqual(list(rows[0]['before'].values()), ['500'])
        self.assertEqual(rows[0]['evidence'], 'protocol')
        self.assertEqual(rows[1]['evidence'], 'inline')

    def test_evidence_is_opt_in_and_does_not_change_original_audit(self):
        with QuerySession():
            runtime = build_runtime(self.config, materialize=False)
            self.addCleanup(runtime.close)
            doc = yaml.safe_load(input_bytes(runtime.config))
            args = (self.ledger, date(2026,9,1), runtime.config.parent/doc['accounts'],
                    runtime.config.parent/doc['ownership'], runtime.config.parent/doc['opening'])
            original = audit_ownership(*args)
            traced = audit_ownership(*args, include_evidence=True)
            self.assertNotIn('purpose_evidence', original)
            self.assertEqual(original, {k:v for k,v in traced.items() if k not in {'purpose_evidence','owner_names'}})
            self.assertEqual(len(traced['purpose_evidence']), original['h_postings'])

    def test_before_vector_isolated_by_exact_cost_lot(self):
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        lots = [json.dumps(['STK', price, 'CNY', day, None], ensure_ascii=False)
                for price, day in [('5','2026-08-01'),('6','2026-08-02')]]
        doc['accounts']['Assets:Security'] = {'semantics':'H', 'payment_scope':{'STK':'excluded'},
            'opening': [{'commodity':'STK','lot':lots[0],'owners':{'A':'10'}},
                        {'commodity':'STK','lot':lots[1],'owners':{'B':'20'}}]}
        self.config.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
        self.append('''2026-08-01 open Assets:Security
2026-08-01 open Equity:Opening
2026-08-31 * "two exact lots"
  Assets:Security 10 STK {5 CNY, 2026-08-01}
  Assets:Security 20 STK {6 CNY, 2026-08-02}
  Equity:Opening -170 CNY
2026-09-04 * "one exact lot"
  Assets:Security -2 STK {5 CNY, 2026-08-01}
    fund: "A"
  Assets:Cash 10 CNY
    fund: "A"
''')
        with QuerySession():
            runtime = build_runtime(self.config, materialize=False)
            self.addCleanup(runtime.close)
            paths = yaml.safe_load(input_bytes(runtime.config))
            owned = audit_ownership(self.ledger, date(2026,9,1),
                runtime.config.parent/paths['accounts'], runtime.config.parent/paths['ownership'],
                runtime.config.parent/paths['opening'], include_evidence=True)
            row = next(r for r in owned['purpose_evidence'] if r['account']=='Assets:Security')
            self.assertEqual(row['lot'], lots[0])
            self.assertEqual({owned['owner_names'][k]:v for k,v in row['before'].items()}, {'A':'10'})

    def test_pending_month_has_workspace_but_no_explained_money(self):
        self.mixed()
        result = doctor(self.config)
        self.assertEqual(result['checks']['static']['state'], 'passed')
        self.assertEqual(result['checks']['replay']['state'], 'pending')
        for metric in ('cash','expense','purpose','max'):
            row = explain(self.config, metric=metric, owner='生活费' if metric=='purpose' else None)
            self.assertEqual(row['status'], 'NEEDS_REVIEW')
            self.assertIsNone(row['report'])
            self.assertIsNone(row['explanation'])
            self.assertIn('H_OWNER_PENDING', {r['reason_code'] for r in row['review_items']})

    def test_future_currency_declaration_is_known_but_not_replayed(self):
        doc = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        doc['accounts']['Assets:Cash']['payment_scope']['JPY'] = 'excluded'
        self.config.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
        self.append('''2026-10-02 * "future JPY"
  Assets:Cash 100 JPY
    fund: "自由资金"
  Income:Salary -100 JPY
''')
        result = doctor(self.config, '2026-09')
        self.assertEqual(result['checks']['replay']['state'], 'passed', result)
        rows = attribution(self.config, '2026-09', all_rows=True)['rows']
        self.assertEqual({r['commodity'] for r in rows}, {'CNY'})
        self.assertEqual(explain(self.config, '2026-09')['explanation']['selected_available_cash'], '190')

    def test_preflight_is_not_monthly_completeness_approval(self):
        path = self.base/'budgets.yml'
        doc = yaml.safe_load(path.read_text(encoding='utf-8'))
        doc['months']['2026-09']['completeness_confirmed'] = False
        path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding='utf-8')
        result = doctor(self.config)
        self.assertEqual(result['checks']['replay']['state'], 'passed')
        self.assertIsNone(explain(self.config)['explanation'])
        self.assertEqual(audit_simple(self.config)['status'], 'NEEDS_REVIEW')

    def test_changed_input_cannot_return_stale_attribution(self):
        from beancount_budget_tool.v6.purpose_workspace import attribution_rows
        def changed(*args, **kwargs):
            result = attribution_rows(*args, **kwargs)
            self.append('''2026-09-07 * "new known spend"
  Assets:Cash -1 CNY
    fund: "生活费"
  Expenses:Food 1 CNY
''')
            return result
        with patch('beancount_budget_tool.v6.purpose_workspace.attribution_rows', side_effect=changed):
            with self.assertRaises(BudgetError) as caught:
                attribution(self.config, all_rows=True)
        self.assertIn(caught.exception.code, {'INPUT_CHANGED','LEDGER_CHANGED','SOURCE_CHANGED'})

    def test_public_dispatch_and_snapshot_match_signed_report(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['doctor','--config',str(self.config),'--static-only','--format','json']), 0)
        self.assertEqual(json.loads(output.getvalue())['checks']['replay']['state'], 'not_run')
        result = explain(self.config, metric='purpose', owner='生活费')
        report = audit_simple(self.config)['report']
        self.assertEqual(result['ledger_snapshot'], report['ledger_snapshot'])
        self.assertIsNotNone(result['explanation'])


if __name__ == '__main__': unittest.main()

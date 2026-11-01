"""Independent projections for K book flows and tracking topic flows."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.activity_views import (
    k_flows, render_k_flows, render_k_summary, render_tracking_flows,
)
from beancount_budget_tool.v6.k_book_purposes import audit_k_book_purposes
from beancount_budget_tool.v6.k_events import audit_k_events
from beancount_budget_tool.v6.purpose_summary import active_owners, summarize
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.simple_config import audit_simple, audit_simple_period


CUTOVER = date(2026, 9, 1)


class KTrackingFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self._temporary = temporary
        folder = Path(temporary.name)
        self.root = folder / 'main.bean'
        self.accounts = folder / 'accounts.yml'
        self.ownership = folder / 'ownership.yml'
        self.events = folder / 'k-events.yml'
        self.opening = folder / 'opening.yml'
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Assets:FundBook\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-31 * "cash opening"\n'
            '  Assets:Cash 500 CNY\n'
            '  Equity:Opening -500 CNY\n'
            '2026-09-02 * "fund subscription"\n'
            '  Assets:FundBook 200 CNY\n'
            '  Assets:Cash -200 CNY\n'
            '2026-09-03 * "fund redemption"\n'
            '  Assets:FundBook -100 CNY\n'
            '  Assets:Cash 100 CNY\n', encoding='utf-8')
        self.snapshot = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            'schema': 6, 'cutover': CUTOVER.isoformat(), 'report_currency': 'CNY',
            'ledger_snapshot': self.snapshot, 'accounts': [
                {'account': 'Assets:Cash', 'semantics': 'H',
                 'balance_semantics': 'real_inventory',
                 'payment_scope': {'CNY': 'payable'}, 'reason': 'virtual cash'},
                {'account': 'Assets:FundBook', 'semantics': 'K',
                 'balance_semantics': 'signed_subscription_flow',
                 'payment_scope': {'CNY': 'excluded'}, 'reason': 'virtual book flow'}]},
            allow_unicode=True), encoding='utf-8')
        missing = audit_ownership(self.root, CUTOVER, self.accounts)['missing_h_ownership']
        self.write_ownership([
            {'claim': missing[0]['ref'], 'reason': 'subscription cash',
             'slices': [
                 {'id': 'travel_out', 'owner_id': 'travel', 'role': 'hold',
                  'signed_units': '-100'},
                 {'id': 'insurance_out', 'owner_id': 'insurance', 'role': 'hold',
                  'signed_units': '-100'}]},
            {'claim': missing[1]['ref'], 'reason': 'redemption cash',
             'slices': [
                 {'id': 'travel_back', 'owner_id': 'travel', 'role': 'hold',
                  'signed_units': '100'}]},
        ])
        candidates = audit_k_events(self.root, CUTOVER, self.accounts, self.ownership)['events']
        self.subscription = candidates[0]
        self.redemption = candidates[1]
        self.events.write_text(yaml.safe_dump({
            'schema': 6, 'cutover': CUTOVER.isoformat(),
            'ledger_snapshot': self.snapshot, 'loader_version': BEANCOUNT_VERSION,
            'events': [
                {'k_ref': deepcopy(self.subscription['k_ref']),
                 'cash_ref': deepcopy(self.subscription['cash_ref']),
                 'kind': 'subscription', 'reason': 'reviewed subscription',
                 'slices': [
                     {'id': 'travel_buy', 'k_owner_id': 'travel',
                      'cash_owner_id': 'travel', 'signed_k_units': '100',
                      'signed_cash_units': '-100', 'boundary_role': 'preserve'},
                     {'id': 'insurance_buy', 'k_owner_id': 'insurance',
                      'cash_owner_id': 'insurance', 'signed_k_units': '100',
                      'signed_cash_units': '-100', 'boundary_role': 'preserve'}]},
                {'k_ref': deepcopy(self.redemption['k_ref']),
                 'cash_ref': deepcopy(self.redemption['cash_ref']),
                 'kind': 'redemption', 'reason': 'reviewed redemption',
                 'slices': [
                     {'id': 'travel_sell', 'k_owner_id': 'travel',
                      'cash_owner_id': 'travel', 'signed_k_units': '-100',
                      'signed_cash_units': '100', 'boundary_role': 'preserve'}]},
            ]}, allow_unicode=True), encoding='utf-8')
        self.opening.write_text(yaml.safe_dump({
            'schema': 6, 'cutover': CUTOVER.isoformat(),
            'ledger_snapshot': self.snapshot, 'h_holdings': [],
            'k_openings': [{'account': 'Assets:FundBook', 'commodity': 'CNY',
                            'lot': 'CNY/no-cost', 'signed_units': '0',
                            'reason': 'reviewed zero book opening'}]},
            allow_unicode=True), encoding='utf-8')
        self.result = audit_k_book_purposes(
            self.root, CUTOVER, date(2026, 9, 30), self.accounts,
            self.opening, self.ownership, self.events)
        self.names = {'travel': '旅行', 'insurance': '保险'}

    def write_ownership(self, claims):
        self.ownership.write_text(yaml.safe_dump({
            'schema': 6, 'cutover': CUTOVER.isoformat(),
            'ledger_snapshot': self.snapshot, 'loader_version': BEANCOUNT_VERSION,
            'owners': [{'id': 'travel', 'name': '旅行', 'aliases': []},
                       {'id': 'insurance', 'name': '保险', 'aliases': []}],
            'claims': claims}, allow_unicode=True), encoding='utf-8')

    def test_k_projection_keeps_events_and_source(self):
        self.assertEqual(self.result['status'], 'CERTIFIED')
        events = self.result['events']
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event['source'] for event in events))
        flows = k_flows(self.result)
        self.assertEqual([row['direction'] for row in flows],
                         ['subscription', 'redemption'])
        self.assertEqual(len(k_flows(self.result, '2026-09')), 2)
        self.assertEqual(k_flows(self.result, '2026-10'), [])
        self.assertEqual(flows[0]['signed_k_units'], '200')
        self.assertEqual(flows[1]['signed_k_units'], '-100')

    def test_summary_and_flows_use_book_language(self):
        summary = render_k_summary(self.result, self.names, columns=120)
        self.assertTrue(any('K 账面申赎汇总' in line for line in summary))
        self.assertIn('账面变动', '\n'.join(summary))
        self.assertNotIn('份额', '\n'.join(summary).splitlines()[1])
        flows = render_k_flows(k_flows(self.result), self.names, columns=120)
        text = '\n'.join(flows)
        self.assertIn('申购', text)
        self.assertIn('赎回', text)
        self.assertIn('已审', text)
        self.assertNotIn('CERTIFIED_BOOK_FLOW', text)
        self.assertIn('旅行', text)
        self.assertIn('保险', text)
        self.assertNotIn('可赎回金额', text)


    def test_month_summary_shows_offsetting_events_and_show_all_restores_rows(self):
        event = {
            'date': '2026-09-02', 'k_account': 'Assets:FundBook',
            'commodity': 'CNY', 'shape': 'simple_cash', 'status': 'CERTIFIED',
            'signed_k_units': '100', 'cash_account': 'Assets:Cash',
            'slices': [{'k_owner_id': 'travel', 'signed_k_units': '100'}],
        }
        reverse = {**event, 'date': '2026-09-03', 'signed_k_units': '-100',
                   'slices': [{'k_owner_id': 'travel', 'signed_k_units': '-100'}]}
        book = {'events': [event, reverse], 'rows': [{
            'account': 'Assets:FundBook', 'commodity': 'CNY',
            'booked_opening': '0', 'booked_change': '0', 'booked_end': '0',
            'unreviewed_change': '0', 'pending_event_count': 0,
            'purpose_changes': [], 'status': 'CERTIFIED_BOOK_FLOW'}]}
        summary = render_k_summary(book, self.names, month='2026-09', columns=120)
        text = '\n'.join(summary)
        self.assertIn('Assets:FundBook', text)
        self.assertIn('0 CNY', text)
        self.assertIn('2', text)
        restored = render_k_summary(book, self.names, month='2026-10', show_all=True, columns=120)
        self.assertIn('Assets:FundBook', '\n'.join(restored))

    def test_k_flows_keep_same_day_ledger_order_and_show_source(self):
        book = {'events': [
            {'date': '2026-09-02', 'k_account': 'Assets:Z', 'commodity': 'CNY',
             'shape': 'simple_cash', 'status': 'CERTIFIED', 'signed_k_units': '1',
             'source': 'main.bean:10', 'cash_account': 'Assets:Cash'},
            {'date': '2026-09-02', 'k_account': 'Assets:A', 'commodity': 'CNY',
             'shape': 'simple_cash', 'status': 'CERTIFIED', 'signed_k_units': '2',
             'source': 'main.bean:2', 'cash_account': 'Assets:Cash'},
        ]}
        flows = k_flows(book)
        self.assertEqual([row['k_account'] for row in flows], ['Assets:Z', 'Assets:A'])
        text = '\n'.join(render_k_flows(flows, self.names, columns=160))
        self.assertIn('main.bean:10', text)
        self.assertIn('来源', text)

    def simple_k_fixture(self, *, fund=True):
        folder = Path(self._temporary.name)
        base = folder / 'simple-k'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/v6/simple', base)
        config = base / 'config.yml'
        document = yaml.safe_load(config.read_text(encoding='utf-8'))
        document['account_groups'] = {'K': ['Assets:FundBook']}
        config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        ledger = base / 'main.bean'
        text = ledger.read_text(encoding='utf-8').replace(
            '2026-09-01 open Expenses:Food\n',
            '2026-09-01 open Expenses:Food\n2026-09-01 open Assets:FundBook\n')
        text += ('\n2026-09-04 * "虚拟申购"\n'
                 '  Assets:FundBook 100 CNY\n' +
                 ('    fund: "生活费"\n' if fund else '') +
                 '  Assets:Cash -100 CNY\n    fund: "生活费"\n'
                 '\n2026-09-05 * "虚拟专题"\n'
                 '  Expenses:Food 20 CNY\n    tracking: "大额支出"\n'
                 '  Assets:Cash -20 CNY\n    fund: "生活费"\n')
        ledger.write_text(text, encoding='utf-8')
        return base

    def test_display_status_is_short_but_json_status_is_unchanged(self):
        event = {'date': '2026-09-02', 'k_account': 'Assets:FundBook',
                 'commodity': 'CNY', 'shape': 'simple_cash', 'status': 'CERTIFIED',
                 'signed_k_units': '100', 'cash_account': 'Assets:Cash', 'source': 'x:1',
                 'slices': [{'k_owner_id': 'travel', 'signed_k_units': '100'}]}
        book = {'events': [event], 'rows': [{
            'account': 'Assets:FundBook', 'commodity': 'CNY', 'booked_opening': '0',
            'booked_change': '100', 'booked_end': '100', 'unreviewed_change': '0',
            'pending_event_count': 0, 'purpose_changes': [], 'status': 'CERTIFIED_BOOK_FLOW'}]}
        self.assertIn('已审', '\n'.join(render_k_summary(book, self.names)))
        self.assertIn('已审', '\n'.join(render_k_flows(k_flows(book), self.names)))
        self.assertEqual(k_flows(book)[0]['status'], 'CERTIFIED')
        pending = {**event, 'status': 'NEEDS_REVIEW'}
        self.assertIn('待审', '\n'.join(render_k_flows(k_flows({'events': [pending]}), self.names)))
        self.assertEqual(k_flows({'events': [pending]})[0]['status'], 'NEEDS_REVIEW')

    def test_zero_net_round_trip_owner_remains_active(self):
        report = {
            'active_owner_ids': ['round_trip'],
            'purpose_balances': {'purposes': []},
            'purpose_progress': {'owners': [
                {'owner_id': 'round_trip', 'actual_g': '0', 'target_q': '0',
                 'status': 'CERTIFIED'},
            ]},
        }
        self.assertIn('round_trip', active_owners(report))
        report['active_owner_ids'] = []
        report['k_flows'] = [{'slices': [{'k_owner_id': 'k_only', 'signed_k_units': '1'}]}]
        self.assertIn('k_only', active_owners(report))

    def test_public_simple_and_period_expose_k_tracking_and_cumulative(self):
        base = self.simple_k_fixture()
        config = base / 'config.yml'
        result = audit_simple(config, include_transactions=True)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result.get('review_items'))
        report = result['report']
        self.assertEqual(len(report['k_flows']), 1)
        self.assertEqual(report['k_flows'][0]['direction'], 'subscription')
        self.assertEqual(len(report['tracking']['events']), 1)
        self.assertTrue(report['k_flows'][0]['source'])
        self.assertTrue(report['tracking']['events'][0]['source'])

        period = audit_simple_period(config, '2026-09', '2026-09')
        self.assertEqual(period['status'], 'CERTIFIED_SCOPED', period.get('review_items'))
        report = period['report']
        self.assertEqual(len(report['k_flows']), 1)
        self.assertEqual(len(report['tracking_events']), 1)
        cumulative = report['cumulative']
        self.assertEqual(cumulative['report_currency'], 'CNY')
        living = next(row for row in cumulative['purposes'] if row['name'] == '生活费')
        self.assertEqual(living['k_book_change'], '100')
        self.assertNotEqual(living['available_cash'], '100')
        self.assertEqual(living['pool_book_cost'], '0')

    def test_cumulative_separates_currency_k_and_pool_from_cash(self):
        balance = lambda owner, name, h, k=None, pool=None, roles=(): {
            'owner_id': owner, 'name': name, 'default_roles': list(roles),
            'h_native_balances': h, 'k_book_purpose_changes': k or [],
            'pool_book_cost_balances': pool or []}
        report = {
            'report_currency': 'CNY', 'as_of': '2025-12-31',
            'actual_spendable': {
                'status': 'CERTIFIED', 'actual_spendable': '100',
                'purpose_available': [{'owner_id': 'savings', 'units': '50'}],
            },
            'default_outflows': {'owner_id': 'living'},
            'purpose_balances': {'purposes': [
                balance('living', '生活费', [{'commodity': 'CNY', 'units': '100'}],
                        roles=('expense_default',)),
                balance('savings', '储蓄', [{'commodity': 'CNY', 'units': '50'}]),
                balance('jpy', '日元', [{'commodity': 'JPY', 'units': '1000'}]),
                balance('k', '基金', [], k=[{'commodity': 'CNY', 'signed_change': '200'}]),
                balance('pool', '证券池', [], pool=[{'commodity': 'CNY', 'units': '300'}]),
            ], 'outside_budget': None},
            'purpose_progress': {'owners': [
                {'owner_id': owner, 'target_q': '10', 'actual_g': '0'}
                for owner in ('living', 'savings', 'jpy', 'k', 'pool')]},
        }
        summary = summarize([report], '2025-12', '2025-12')
        rows = {(row['name'], row['commodity']): row for row in summary['purposes']}
        self.assertEqual(rows[('生活费', 'CNY')]['available_cash'], '100')
        self.assertEqual(rows[('储蓄', 'CNY')]['available_cash'], '50')
        self.assertIsNone(rows[('日元', 'JPY')]['available_cash'])
        self.assertEqual(rows[('基金', 'CNY')]['k_book_change'], '200')
        self.assertNotIn(rows[('基金', 'CNY')]['available_cash'], ('200', '300'))
        self.assertEqual(rows[('证券池', 'CNY')]['pool_book_cost'], '300')
        self.assertNotIn(rows[('证券池', 'CNY')]['available_cash'], ('200', '300'))

    def test_pending_k_does_not_publish_cumulative(self):
        base = self.simple_k_fixture(fund=False)
        result = audit_simple(base / 'config.yml')
        self.assertEqual(result['status'], 'NEEDS_REVIEW')
        self.assertIsNone(result['report'])
        self.assertTrue({'K_CASH_OWNER_PENDING', 'K_EVENT_PENDING'} &
                        {row.get('reason_code') for row in result.get('review_items', [])})

    def test_tracking_flows_keep_source_and_topic(self):
        lines = render_tracking_flows([
            {'date': '2025-08-06', 'topic': '大额支出',
             'account': 'Expenses:Consume-消费:家居箱包及装扮',
             'signed_units': '15283/20', 'commodity': 'CNY',
             'source': 'src/transactions/2025/08/jd.bean:10'},
        ], columns=120)
        text = '\n'.join(lines)
        self.assertIn('专题流水', text)
        self.assertIn('大额支出', text)
        self.assertIn('764.15', text)
        self.assertIn('jd.bean:10', text)
        self.assertIn('不改变预算', text)


if __name__ == '__main__':
    unittest.main()

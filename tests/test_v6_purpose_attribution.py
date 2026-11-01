"""Known-answer checks for mixed-purpose H account attribution."""

from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.v6.salary_cycle import audit_salary_cycle
from beancount_budget_tool.v6.simple_config import audit_simple, audit_simple_period
from beancount_budget_tool.exact import exact_text


class PurposeAttributionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.ledger = self.base / 'main.bean'
        self.config = self.base / 'config.yml'
        self.budgets = self.base / 'budgets.yml'
        self.ledger.write_text('''option "operating_currency" "CNY"
2026-08-01 open Assets:Cash
2026-08-01 open Expenses:Food
2026-08-01 open Income:Salary
2026-08-01 open Equity:Opening
2026-08-31 * "opening"
  Assets:Cash 1000 CNY
  Equity:Opening -1000 CNY
2026-09-02 * "B spend"
  Expenses:Food 100 CNY
  Assets:Cash -100 CNY
    fund: "B"
2026-09-03 * "same-account virtual transfer"
  Assets:Cash -50 CNY
    fund: "A"
  Assets:Cash 50 CNY
    fund: "B"
2026-09-04 * "B cash refund"
  Assets:Cash 20 CNY
    fund: "B"
  Expenses:Food -20 CNY
''', encoding='utf-8')
        self.config.write_text(yaml.safe_dump({
            'schema': 1, 'ledger': 'main.bean', 'budgets': 'budgets.yml',
            'cutover': '2026-09-01', 'month': '2026-09', 'report_currency': 'CNY',
            'defaults': {'income_purpose': '自由资金', 'expense_purpose': '生活费',
                         'expense_category': '生活开销'},
            'account_groups': {'H': {'payable': ['Assets:Cash']}},
            'accounts': {'Assets:Cash': {'opening': {'CNY': {'A': '600', 'B': '400'}}}},
        }, allow_unicode=True), encoding='utf-8')
        self.budgets.write_text(yaml.safe_dump({
            'schema': 1, 'defaults': {'expense_targets': {'生活开销': '1000'}},
            'months': {'2026-09': {'completeness_confirmed': True}},
        }, allow_unicode=True), encoding='utf-8')

    def purpose_units(self, report):
        rows = {}
        for purpose in report['purpose_balances']['purposes']:
            for coordinate in purpose['h_native_balances']:
                rows[purpose['name']] = exact_text(rows.get(purpose['name'], Fraction(0)) + Fraction(coordinate['units']))
        return rows

    def test_no_fund_income_into_mixed_owner_is_pending(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + """
2026-09-05 * "ambiguous income"
  Assets:Cash 100 CNY
  Income:Salary -100 CNY
""", encoding="utf-8")
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW", result.get("review_items"))
        self.assertIsNone(result["report"])

    def test_jpy_coordinate_is_not_judged_by_cny_owner_balances(self):
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["accounts"]["Assets:Cash"]["opening"]["JPY"] = {"A": "600", "B": "400"}
        self.config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + """
2026-08-31 * "real JPY opening"
  Assets:Cash 1000 JPY
  Equity:Opening -1000 JPY
2026-09-05 * "ambiguous JPY spend"
  Expenses:Food 10 JPY
  Assets:Cash -10 JPY
""", encoding="utf-8")
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW", result.get("review_items"))
        self.assertIsNone(result["report"])

    def test_future_transaction_does_not_change_past_attribution(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + """
2026-10-01 * "future ambiguous spend"
  Expenses:Food 10 CNY
  Assets:Cash -10 CNY
""", encoding="utf-8")
        result = audit_simple(self.config, month="2026-09")
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result.get("review_items"))
        self.assertEqual(self.purpose_units(result["report"]), {"A": "550", "B": "370"})

    def test_explicit_mixed_purpose_known_answers(self):
        result = audit_simple(self.config, include_transactions=True)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result.get('review_items'))
        self.assertEqual(self.purpose_units(result['report']), {'A': '550', 'B': '370'})
        total = sum(Fraction(value) for value in self.purpose_units(result['report']).values())
        self.assertEqual(total, 920)

    def add_bank(self):
        document = yaml.safe_load(self.config.read_text(encoding='utf-8'))
        document['account_groups']['H']['payable'].append('Assets:Bank')
        self.config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding='utf-8')
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8').replace(
            '2026-08-01 open Expenses:Food\n',
            '2026-08-01 open Expenses:Food\n2026-08-01 open Assets:Bank\n'), encoding='utf-8')

    def add_liability(self):
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["accounts"]["Liabilities:Card"] = {"semantics": "credit_card"}
        self.config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            "2026-08-01 open Expenses:Food\n",
            "2026-08-01 open Expenses:Food\n2026-08-01 open Liabilities:Card\n"), encoding="utf-8")

    def test_no_fund_multi_owner_liability_payment_is_pending(self):
        self.add_liability()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + """
2026-09-05 * "ambiguous card payment"
  Liabilities:Card 10 CNY
  Assets:Cash -10 CNY
""", encoding="utf-8")
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW", result.get("review_items"))
        self.assertIsNone(result["report"])
        self.assertIn("H_OWNER_PENDING",
                      {row.get("reason_code") for row in result.get("review_items", [])})

    def test_ambiguous_attribution_blocks_month_period_and_salary_cycle(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + """
2026-09-05 * "ambiguous mixed-purpose spend"
  Expenses:Food 10 CNY
  Assets:Cash -10 CNY
""", encoding="utf-8")
        month = audit_simple(self.config)
        self.assertEqual(month["status"], "NEEDS_REVIEW", month.get("review_items"))
        self.assertIsNone(month["report"])
        period = audit_simple_period(self.config, "2026-09", "2026-09")
        self.assertEqual(period["status"], "NEEDS_REVIEW", period.get("review_items"))
        self.assertIsNone(period["report"])
        cycle = audit_salary_cycle(self.config, "2026-09-01", "2026-10-01",
                                   budget="1000", category="生活开销")
        self.assertEqual(cycle["status"], "NEEDS_REVIEW", cycle.get("review_items"))
        self.assertIsNone(cycle["report"])

    def test_salary_cycle_ignores_future_ambiguous_attribution(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + """
2026-10-05 * "future ambiguous spend"
  Expenses:Food 10 CNY
  Assets:Cash -10 CNY
""", encoding="utf-8")
        result = audit_salary_cycle(self.config, "2026-09-01", "2026-10-01",
                                    budget="1000", category="生活开销")
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result.get("review_items"))
        self.assertIsNotNone(result["report"])

    def test_no_fund_cash_refund_is_pending(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8') + '''
2026-09-05 * "ambiguous cash refund"
  Assets:Cash 10 CNY
  Expenses:Food -10 CNY
''', encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW', result.get('review_items'))
        self.assertIsNone(result['report'])

    def test_cross_account_same_purpose_transfer_has_no_gain(self):
        self.add_bank()
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8') + '''
2026-09-05 * "cross-account same purpose"
  Assets:Cash -50 CNY
    fund: "A"
  Assets:Bank 50 CNY
    fund: "A"
''', encoding='utf-8')
        result = audit_simple(self.config, include_transactions=True)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result.get('review_items'))
        self.assertEqual(self.purpose_units(result['report']), {'A': '550', 'B': '370'})
        transfer = next(row for row in result['report']['transactions']['rows']
                        if row['narration'] == 'cross-account same purpose')
        self.assertEqual(transfer['net_allocations'], {})

    def test_full_vector_transfer_inherits_both_owners(self):
        self.add_bank()
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8') + '''
2026-09-05 * "whole vector move"
  Assets:Cash -920 CNY
  Assets:Bank 920 CNY
''', encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result.get('review_items'))
        self.assertEqual(self.purpose_units(result['report']), {'A': '550', 'B': '370'})

    def test_no_fund_multi_owner_is_pending(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8') + '''
2026-09-05 * "ambiguous mixed-purpose spend"
  Expenses:Food 10 CNY
  Assets:Cash -10 CNY
''', encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'NEEDS_REVIEW', result.get('review_items'))
        self.assertIsNone(result['report'])
        self.assertTrue({'H_OWNER_PENDING', 'OWNERSHIP_UNREVIEWED'} &
                        {row.get('reason_code') for row in result.get('review_items', [])})

    def test_explicit_multi_slice_is_not_ambiguous(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8') + '''
2026-09-05 * "explicit split spend"
  Expenses:Food 100 CNY
  Assets:Cash -100 CNY
    fund: "A30,B70"
''', encoding='utf-8')
        result = audit_simple(self.config)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result.get('review_items'))
        self.assertEqual(self.purpose_units(result['report']), {'A': '520', 'B': '300'})


if __name__ == '__main__':
    unittest.main()

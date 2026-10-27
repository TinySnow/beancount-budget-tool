"""H inventory redemptions preserve principal and count only actual income."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple, build_runtime
from beancount_budget_tool.v6.spending import audit_monthly_spending
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.yamlstrict import load_yaml


HEADER = '''option "operating_currency" "CNY"
2026-08-01 open Assets:Bank
2026-08-01 open Assets:Wealth
2026-08-01 open Equity:Opening
2026-08-01 open Income:Gain
2026-08-01 open Expenses:Fee
2026-08-31 * "opening"
  Assets:Bank 100 CNY
  Assets:Wealth 2200 CNY
  Equity:Opening -2300 CNY
'''


class HRedemptionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.ledger = self.base / "main.bean"
        self.config = self.base / "config.yml"
        self.doc = {"schema": 1, "ledger": "main.bean", "budgets": "budgets.yml", "month": "2026-09",
                    "cutover": "2026-09-01", "report_currency": "CNY",
                    "defaults": {"income_purpose": "自由资金", "expense_purpose": "生活费", "expense_category": "生活开销"},
                    "accounts": {
                        "Assets:Bank": {"semantics": "H", "payment_scope": {"CNY": "payable"}, "opening": {"CNY": {"生活费": "100"}}},
                        "Assets:Wealth": {"semantics": "H", "payment_scope": {"CNY": "excluded"}, "opening": {"CNY": {"额外储蓄": "2200"}}}}}
        self.plans = {"schema": 1, "months": {month: {"completeness_confirmed": True,
                     "expense_targets": {"生活开销": "500"}, "allocation_targets": {"额外储蓄": "1000"}}
                     for month in ("2026-09", "2026-10")}}

    def write(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def source(self, principal="1000", receipt="1010.71", income="-10.71", *, source_fund="额外储蓄", cash_fund="额外储蓄", fee=None, day="2026-09-02"):
        return (f'{day} * "redemption"\n  Assets:Bank {receipt} CNY\n' +
                (f'    fund: "{cash_fund}"\n' if cash_fund is not None else '') +
                f'  Assets:Wealth -{principal} CNY\n' +
                (f'    fund: "{source_fund}"\n' if source_fund is not None else '') +
                f'  Income:Gain {income} CNY\n' +
                (f'  Expenses:Fee {fee} CNY\n' if fee is not None else ''))

    def audit(self, source, month="2026-09", *, certified=True):
        self.ledger.write_text(HEADER + source, encoding="utf-8")
        self.write(self.config, self.doc)
        self.write(self.base / "budgets.yml", self.plans)
        original = {path: path.read_bytes() for path in (self.ledger, self.config, self.base / "budgets.yml")}
        result = audit_simple(self.config, month, include_transactions=True)
        self.assertEqual(original, {path: path.read_bytes() for path in original})
        if certified:
            self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
            self.assertEqual(result["review_items"], [])
            return result["report"]
        return result

    def income(self):
        runtime = build_runtime(self.config)
        self.addCleanup(runtime.close)
        paths = load_yaml(runtime.config)
        audit = audit_monthly_spending(self.ledger, date(2026, 9, 1), "2026-09",
                                       runtime.config.parent / paths["classifications"], "CNY")
        return audit["metrics"]["income_supplement"]["report_total"]

    def test_two_h_principal_and_income_redemptions_have_zero_g(self):
        report = self.audit(self.source() + self.source("1200", "1216.11", "-16.11", day="2026-09-06"))
        self.assertEqual(self.income(), "1341/50")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "100")
        self.assertTrue(all(row["actual_g"] == "0" for row in report["purpose_progress"]["owners"]))
        transactions = report["transactions"]["rows"]
        self.assertTrue(all(row["net_allocations"] == {} for row in transactions))
        self.assertEqual([row["purpose_events"][0]["kind"] for row in transactions], ["h_redemption", "h_redemption"])
        detail = '\n'.join(render_transaction(transactions[0], report["owner_names"]))
        self.assertIn("本金 1000", detail)
        self.assertIn("1010.71", detail)
        self.assertIn("10.71", detail)

    def test_loss_and_deducted_fee_do_not_reclassify_principal_as_consumption(self):
        report = self.audit(self.source(receipt="994", income="5", fee="1"))
        self.assertEqual(self.income(), "-5")
        event = report["transactions"]["rows"][0]["purpose_events"][0]["protocol"]
        self.assertEqual(event["principal_native"], "1000")
        owner = next(ident for ident, name in report["owner_names"].items() if name == "额外储蓄")
        self.assertEqual(event["net_purpose_changes"], {owner: "-6"})
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")

    def test_partial_redemption_keeps_remaining_real_inventory(self):
        report = self.audit(self.source(principal="100", receipt="102", income="-2"))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "100")
        self.assertEqual(self.income(), "2")
        balances = {(row["account"], report["owner_names"][row["owner_id"]]): row["units"]
                    for row in report["account_purposes"]["rows"]}
        self.assertEqual(balances["Assets:Wealth", "额外储蓄"], "2100")
        self.assertEqual(balances["Assets:Bank", "额外储蓄"], "102")
        self.assertTrue(all(row["actual_g"] == "0" for row in report["purpose_progress"]["owners"]))

    def test_mismatched_purposes_are_data_review_with_asset_account_and_line(self):
        result = self.audit(self.source(cash_fund="生活费"), certified=False)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertNotIn("NAMED_H_EVENT_UNSUPPORTED", {r["reason_code"] for r in result["review_items"]})
        review = next(r for r in result["review_items"] if r["reason_code"] == "H_REDEMPTION_PURPOSE_MISMATCH")
        self.assertIn(review["account"], ("Assets:Bank", "Assets:Wealth"))
        self.assertIn("main.bean:", review["source"])

    def test_missing_cash_purpose_does_not_default_the_principal_to_income(self):
        result = self.audit(self.source(cash_fund=None), certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("OWNERSHIP_UNREVIEWED", {r["reason_code"] for r in result["review_items"]})

    def test_redemption_cannot_overdraw_a_proved_source_purpose(self):
        self.doc["accounts"]["Assets:Wealth"]["opening"]["CNY"] = {"额外储蓄": "500", "生活费": "1700"}
        with self.assertRaises(BudgetError) as error:
            self.audit(self.source())
        self.assertEqual(error.exception.code, "H_OWNER_NEGATIVE")
        self.assertIn("可划出 500 CNY", error.exception.message)

    def test_future_redemption_does_not_create_current_cash_or_allocation(self):
        report = self.audit(self.source(day="2026-10-02"))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "100")
        self.assertEqual(report["transactions"]["rows"], [])
        self.assertTrue(all(row["actual_g"] == "0" for row in report["purpose_progress"]["owners"]))

    def test_exact_equation_required_even_with_loose_loader_tolerance(self):
        result = self.audit('option "inferred_tolerance_default" "CNY:1"\n' + self.source(income="-10.72"), certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("NAMED_H_EVENT_UNSUPPORTED", {r["reason_code"] for r in result["review_items"]})

    def test_multiple_purposes_use_same_name_net_changes_without_g(self):
        self.doc["accounts"]["Assets:Wealth"]["opening"]["CNY"] = {"额外储蓄": "1200", "生活费": "1000"}
        report = self.audit(self.source(principal="100", receipt="105", income="-5",
            source_fund="额外储蓄50,生活费50", cash_fund="生活费55,额外储蓄50"))
        tx = report["transactions"]["rows"][0]
        event = tx["purpose_events"][0]["protocol"]
        changes = {report["owner_names"][ident]: value for ident, value in event["net_purpose_changes"].items()}
        self.assertEqual(changes, {"额外储蓄": "0", "生活费": "5"})
        self.assertEqual(tx["net_allocations"], {})
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "155")

    def test_without_income_leg_does_not_silently_treat_a_purchase_with_change_as_redemption(self):
        result = self.audit(self.source(receipt="999", fee="1").replace('  Income:Gain -10.71 CNY\n', ''),
                            certified=False)
        self.assertEqual(result['status'], 'CERTIFIED_SCOPED', result['review_items'])
        tx = result['report']['transactions']['rows'][0]
        self.assertEqual(tx['purpose_events'][0]['kind'], 'h_bundle')
        self.assertEqual(tx['purpose_events'][0]['protocol']['expense_total'], '1')
        self.assertEqual(tx['net_allocations'], {})
        self.assertEqual(self.income(), '0')

    def test_zero_income_does_not_create_profit_or_allocation(self):
        report = self.audit(self.source(receipt="1000", income="0"))
        self.assertEqual(self.income(), "0")
        self.assertEqual(report["transactions"]["rows"][0]["net_allocations"], {})

    def test_reallocating_after_redemption_requires_and_counts_only_virtual_transfer(self):
        source = self.source() + '2026-09-03 * "reallocate"\n  Assets:Bank -10.71 CNY\n    fund: "额外储蓄"\n'
        source += '  Assets:Bank 10.71 CNY\n    fund: "生活费"\n'
        report = self.audit(source)
        values = {report["owner_names"][r["owner_id"]]: r["actual_g"] for r in report["purpose_progress"]["owners"]}
        self.assertEqual(values["生活费"], "1071/100")
        self.assertEqual(values["额外储蓄"], "-1071/100")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "11071/100")
        self.assertEqual(report["transactions"]["rows"][0]["net_allocations"], {})

    def test_multiple_income_legs_remain_an_explicit_implementation_gap(self):
        source = self.source(income="-5").replace('  Income:Gain -5 CNY\n',
            '  Income:Gain -5 CNY\n  Income:Gain -5.71 CNY\n')
        result = self.audit(source, certified=False)
        self.assertEqual(result["status"], "IMPLEMENTATION_BLOCKED")
        self.assertTrue(all(r.get("account") for r in result["review_items"] if r["reason_code"] == "NAMED_H_EVENT_UNSUPPORTED"))


if __name__ == "__main__":
    unittest.main()

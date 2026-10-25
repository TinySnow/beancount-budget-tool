"""A mixed receipt keeps personal income and a collected payable separate."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple, build_runtime
from beancount_budget_tool.v6.spending import audit_monthly_spending
from beancount_budget_tool.v6.transaction_details import render_transaction


HEADER = '''option "operating_currency" "CNY"
2023-11-01 open Assets:Bank
2023-11-01 open Assets:Wallet
2023-11-01 open Liabilities:Colleague
2023-11-01 open Income:Work
2023-11-01 open Income:Bonus
2023-11-01 open Equity:Opening
2023-11-30 * "wallet opening"
  Assets:Wallet 240 CNY
  Equity:Opening -240 CNY
'''
RECEIPT = '''2023-12-12 * "bank" "own income and collected colleague allowance"
  Assets:Bank 480 CNY
    fund: "生活费"
  Liabilities:Colleague -240 CNY
  Income:Work -240 CNY
    budget: "自由资金"
'''
PAYMENT = '''2023-12-12 * "wallet" "forward collected allowance"
  Liabilities:Colleague 240 CNY
  Assets:Wallet -240 CNY
'''


class PayableReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.ledger = self.base / "main.bean"
        self.config = self.base / "config.yml"
        self.budgets = self.base / "budgets.yml"
        self.configuration = {"schema": 1, "ledger": "main.bean", "budgets": "budgets.yml",
            "month": "2023-12", "cutover": "2023-12-01", "report_currency": "CNY",
            "defaults": {"income_purpose": "自由资金", "expense_purpose": "生活费", "expense_category": "生活开销"},
            "accounts": {"Assets:Bank": {"semantics": "H", "payment_scope": {"CNY": "payable"}},
                         "Assets:Wallet": {"semantics": "H", "payment_scope": {"CNY": "payable"},
                                           "opening": {"CNY": {"生活费": "240"}}}}}
        self.plans = {"schema": 1, "months": {month: {"completeness_confirmed": True,
            "expense_targets": {"生活开销": "not_assessed"}} for month in ("2023-12", "2024-01")}}
        self.write(self.config, self.configuration)
        self.write(self.budgets, self.plans)

    def write(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def audit(self, receipt=RECEIPT, payment=PAYMENT, month="2023-12"):
        self.ledger.write_text(HEADER + receipt + payment, encoding="utf-8")
        return audit_simple(self.config, month, include_transactions=True)

    def report(self, outcome):
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        return outcome["report"]

    def income_total(self):
        runtime = build_runtime(self.config)
        try:
            doc = yaml.safe_load(runtime.config.read_text(encoding="utf-8"))
            spending = audit_monthly_spending(self.ledger, date(2023, 12, 1), "2023-12",
                                              runtime.config.parent / doc["classifications"], "CNY")
            return spending["metrics"]["income_supplement"]["report_total"]
        finally:
            runtime.close()

    def test_actual_user_shape_is_certified_without_fund_on_liability_or_wallet(self):
        report = self.report(self.audit())
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "480")
        for row in report["purpose_progress"]["owners"]:
            self.assertEqual(row["actual_g"], "0")
        rows = report["transactions"]["rows"]
        receipt = next(row for row in rows if row["narration"].startswith("own income"))
        payment = next(row for row in rows if row["narration"].startswith("forward"))
        event = receipt["liabilities"][0]
        self.assertEqual((event["kind"], event["cash_received"], event["income_received"], event["payable_received"]),
                         ("payable_receipt", "480", "240", "240"))
        self.assertEqual(payment["liabilities"][0]["obligation_id"], event["obligation_id"])
        text = "\n".join(render_transaction(receipt, report["owner_names"]))
        self.assertIn("480＝收入 240＋应付款 240", text)
        self.assertEqual(len(event["counterpart_refs"]), 2)
        self.assertEqual(self.income_total(), "240")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")
        self.assertEqual(report["default_outflows"]["totals"][0]["debt_payments"], "240")

    def test_unforwarded_or_partly_forwarded_money_stays_a_responsibility(self):
        for amount, remaining in ((0, "240"), (120, "120"), (240, "0")):
            with self.subTest(amount=amount):
                payment = PAYMENT.replace("240", str(amount)) if amount else ""
                report = self.report(self.audit(payment=payment))
                self.assertEqual(report["actual_spendable"]["actual_spendable"], "480")
                receipt = report["transactions"]["rows"][0]["liabilities"][0]
                self.assertEqual(receipt["payable_received"], "240")
                unsettled = report["actual_spendable"]["unresolved_obligations"]
                self.assertEqual([row["remaining_native"] for row in unsettled], [remaining] if amount < 240 else [])

    def test_multiple_income_legs_do_not_count_the_payable_as_income(self):
        receipt = RECEIPT.replace('  Income:Work -240 CNY\n',
            '  Income:Work -200 CNY\n    budget: "自由资金"\n  Income:Bonus -40 CNY\n')
        report = self.report(self.audit(receipt=receipt))
        event = report["transactions"]["rows"][0]["liabilities"][0]
        self.assertEqual(event["income_received"], "240")
        self.assertEqual(len(event["income_refs"]), 2)

    def test_cross_month_forwarding_does_not_recreate_income_or_release_cash_early(self):
        payment = PAYMENT.replace("2023-12-12", "2024-01-02")
        december = self.report(self.audit(payment=payment))
        january = self.report(self.audit(payment=payment, month="2024-01"))
        self.assertEqual(december["actual_spendable"]["actual_spendable"], "480")
        self.assertEqual(january["actual_spendable"]["actual_spendable"], "480")
        self.assertEqual(len(january["transactions"]["rows"]), 1)

    def test_mixed_owners_and_nonexact_receipts_are_not_auto_approved(self):
        receipt = RECEIPT.replace('fund: "生活费"', 'fund: "生活费240，储蓄240"')
        outcome = self.audit(receipt=receipt)
        self.assertNotEqual(outcome["status"], "CERTIFIED_SCOPED")
        self.assertIsNone(outcome["report"])
        receipt = 'option "inferred_tolerance_default" "CNY:1"\n' + RECEIPT.replace("480 CNY", "480.001 CNY")
        outcome = self.audit(receipt=receipt)
        self.assertNotEqual(outcome["status"], "CERTIFIED_SCOPED")
        self.assertIsNone(outcome["report"])

    def test_ambiguous_payables_and_excess_payments_require_review(self):
        second = RECEIPT.replace("2023-12-12", "2023-12-13")
        payment = PAYMENT.replace("2023-12-12", "2023-12-14")
        outcome = self.audit(receipt=RECEIPT + second, payment=payment)
        self.assertTrue(any(row["reason_code"] == "OBLIGATION_UNCLAIMED" for row in outcome["review_items"]))
        self.assertIsNone(outcome["report"])

    def test_excess_payment_does_not_silently_reduce_or_clip_the_payable(self):
        payment = PAYMENT.replace("240", "250").replace("Assets:Wallet", "Assets:Bank")
        outcome = self.audit(payment=payment)
        self.assertTrue(any(row["reason_code"] == "OBLIGATION_UNCLAIMED" for row in outcome["review_items"]))
        self.assertIsNone(outcome["report"])

    def test_separate_partial_payments_do_not_charge_consumption_or_income_twice(self):
        payment = PAYMENT.replace("240", "120")
        report = self.report(self.audit(payment=payment + payment))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "480")
        self.assertEqual(report["actual_spendable"]["unresolved_obligations"], [])
        self.assertEqual(self.income_total(), "240")
        self.assertEqual(report["default_outflows"]["totals"][0]["debt_payments"], "240")

    def test_prices_and_contradicting_income_directions_stay_outside_the_protocol(self):
        with self.assertRaises(BudgetError) as error:
            self.audit(receipt=RECEIPT.replace("480 CNY", "480 CNY @ 1 CNY"))
        self.assertEqual(error.exception.code, "PAYMENT_SCOPE_INVALID")
        receipt = RECEIPT.replace('  Income:Work -240 CNY\n',
            '  Income:Work -300 CNY\n    budget: "自由资金"\n  Income:Bonus 60 CNY\n')
        outcome = self.audit(receipt=receipt)
        self.assertNotEqual(outcome["status"], "CERTIFIED_SCOPED")
        self.assertIsNone(outcome["report"])

    def test_future_invalid_fund_does_not_hide_current_unpaid_responsibility(self):
        future = PAYMENT.replace("2023-12-12", "2024-01-02").replace(
            "  Liabilities:Colleague 240 CNY\n", '  Liabilities:Colleague 240 CNY\n    fund: "生活费"\n')
        report = self.report(self.audit(payment=future))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "480")
        self.assertEqual(report["actual_spendable"]["unresolved_obligations"][0]["remaining_native"], "240")

    def test_fund_on_liability_still_is_a_source_tag_error(self):
        outcome = self.audit(receipt=RECEIPT.replace('  Liabilities:Colleague -240 CNY\n',
            '  Liabilities:Colleague -240 CNY\n    fund: "生活费"\n'))
        self.assertEqual(outcome["review_items"][0]["reason_code"], "TAG_LEVEL_FORBIDDEN")
        self.assertIsNone(outcome["report"])


if __name__ == "__main__":
    unittest.main()

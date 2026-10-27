"""Adversarial debt histories: independently expected cash, consumption and scope."""

from datetime import date
from fractions import Fraction
import random
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6.simple_config import audit_simple_period
from beancount_budget_tool.v6.simple_config import build_runtime
from beancount_budget_tool.v6.simple_config import save_snapshot
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.yamlstrict import load_yaml
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.facts import FactIndex
from beancount.core import data
from beancount_budget_tool.v6.period_report import csv_rows, render_text as render_period_text
import test_v6_liability_rules as basic


class LiabilityCornerTests(unittest.TestCase):
    def setUp(self):
        self.case = basic.LiabilityRuleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_cross_month_refund_stays_in_refund_month_and_is_not_bank_cash(self):
        c = self.case
        transactions = c.purchase(amount="300") + c.payment(amount="300")
        transactions += '2026-10-03 * "refund after payoff"\n  Expenses:Food -100 CNY\n  Liabilities:Card 100 CNY\n'
        september = c.run_ledger(transactions)
        october = c.run_ledger(transactions, "2026-10")
        self.assertEqual(c.spent(september), 300)
        self.assertEqual(c.spent(october), -100)
        self.assertEqual(october["actual_spendable"]["actual_spendable"], "9700")
        self.assertEqual(october["actual_spendable"]["clearing_receivables"][0]["remaining_native"], "100")

    def test_overpay_then_spend_credit_has_no_second_bank_debit(self):
        c = self.case
        report = c.run_ledger(c.purchase() + c.payment(amount="200") + c.purchase("2026-09-04", "60"))
        self.assertEqual(c.spent(report), 160)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9800")
        self.assertEqual(report["actual_spendable"]["clearing_receivables"][0]["remaining_native"], "40")
        self.assertEqual(c.outflows(report)["debt_payments"], "200")

    def test_joint_cash_and_prepaid_card_purchase_is_not_a_credit_return(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9900", "储蓄": "100"}
        report = c.run_ledger('2026-09-02 * "prepay from saving"\n  Assets:Bank -50 CNY\n    fund: "储蓄"\n  Liabilities:Card 50 CNY\n'
            '2026-09-03 * "buy with cash and credit"\n  Expenses:Food 100 CNY\n'
            '  Assets:Bank -50 CNY\n  Liabilities:Card -50 CNY\n')
        self.assertEqual(c.spent(report), 100)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9850")
        self.assertEqual(c.outflows(report)["expenses_paid"], "50")
        self.assertNotIn("credit_returns", c.outflows(report))

    def test_cash_purchase_does_not_trigger_a_pending_credit_return_group(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9900", "储蓄": "100"}
        c.doc["liabilities"]["Liabilities:Colleague"] = {"role": "credit_card"}
        report = c.run_ledger('2026-09-02 * "joint mixed prepay"\n  Assets:Bank -100 CNY\n    fund: "生活费60,储蓄40"\n'
            '  Liabilities:Card 50 CNY\n  Liabilities:Colleague 50 CNY\n'
            '2026-09-03 * "purchase not return"\n  Expenses:Food 100 CNY\n  Assets:Bank -50 CNY\n  Liabilities:Card -50 CNY\n')
        self.assertEqual(c.spent(report), 100)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9790")
        self.assertEqual(c.outflows(report)["expenses_paid"], "50")

    def test_return_of_overpayment_restores_paying_purpose_without_income(self):
        c = self.case
        report = c.run_ledger(c.purchase() + c.payment(amount="150") +
            '2026-09-04 * "return overpayment"\n  Assets:Bank 50 CNY\n  Liabilities:Card -50 CNY\n')
        self.assertEqual(c.spent(report), 100)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        self.assertEqual(report["actual_spendable"]["booked_debts"], [])
        self.assertEqual(c.outflows(report)["credit_returns"], "50")
        self.assertEqual(c.outflows(report)["net_outflow"], "100")

    def test_refund_credit_retrieved_next_month_restores_cash_not_income(self):
        c = self.case
        report = c.run_ledger(c.purchase() + c.payment() +
            '2026-10-02 * "refund"\n  Expenses:Food -100 CNY\n  Liabilities:Card 100 CNY\n'
            '2026-10-03 * "return credit"\n  Assets:Bank 100 CNY\n  Liabilities:Card -100 CNY\n', "2026-10")
        self.assertEqual(c.spent(report), -100)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")
        period = audit_simple_period(c.config, "2026-09", "2026-10")
        self.assertEqual(period["report"]["default_outflows"][0]["credit_returns"], "100")
        self.assertEqual(period["report"]["default_outflows"][0]["net_outflow"], "0")
        returns = [r for r in csv_rows(period) if r["section"] == "default_outflows" and r["metric"] == "credit_returns"]
        self.assertEqual([(r["scope"], r["value"]) for r in returns], [("month", "100"), ("period", "100")])
        self.assertIn("预付／溢缴退回 100", render_period_text(period))

    def test_joint_payment_from_two_banks_counts_each_cash_leg_once(self):
        c = self.case
        c.doc["accounts"]["Assets:Other"] = {"semantics": "H", "payment_scope": {"CNY": "payable"}}
        report = c.run_ledger('2026-08-01 open Assets:Other\n'
            '2026-09-01 * "move"\n  Assets:Bank -40 CNY\n  Assets:Other 40 CNY\n' + c.purchase() +
            '2026-09-03 * "joint repay"\n  Assets:Bank -60 CNY\n  Assets:Other -40 CNY\n  Liabilities:Card 100 CNY\n')
        self.assertEqual(c.spent(report), 100)
        self.assertEqual(c.outflows(report)["debt_payments"], "100")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")

    def test_equal_and_opposite_balances_in_different_accounts_never_cancel_debt(self):
        c = self.case
        c.doc["liabilities"]["Liabilities:Colleague"] = {"role": "payable"}
        report = c.run_ledger(c.purchase() +
            '2026-09-03 * "prepay other creditor"\n  Assets:Bank -100 CNY\n  Liabilities:Colleague 100 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9800")
        self.assertEqual(report["actual_spendable"]["booked_debts"][0]["remaining_native"], "100")
        self.assertEqual(report["actual_spendable"]["clearing_receivables"][0]["remaining_native"], "100")

    def test_joint_receivable_purposes_are_proved_by_exact_fund_slices(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9900", "储蓄": "100"}
        c.doc["liabilities"] = {"Liabilities:Advance": {"role": "receivable"},
                                "Liabilities:Colleague": {"role": "receivable"}}
        report = c.run_ledger('2026-09-02 * "two advances"\n  Assets:Bank -100 CNY\n    fund: "生活费60,储蓄40"\n'
            '  Liabilities:Advance 60 CNY\n  Liabilities:Colleague 40 CNY\n    budget: "储蓄"\n'
            '2026-09-03 * "two reimbursements"\n  Assets:Bank 100 CNY\n    fund: "生活费60,储蓄40"\n'
            '  Liabilities:Advance -60 CNY\n  Liabilities:Colleague -40 CNY\n    budget: "储蓄"\n')
        self.assertEqual(c.spent(report), 0)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        self.assertEqual(c.outflows(report)["advances_paid"], "60")
        self.assertEqual(c.outflows(report)["advance_reimbursements"], "60")

    def test_future_maturity_loan_becomes_overdue_in_following_month(self):
        c = self.case
        c.doc["liabilities"] = {"Liabilities:Loan": {"role": "loan", "due_on": "2026-10-02"}}
        transactions = '2026-09-02 * "borrow"\n  Assets:Bank 1000 CNY\n  Liabilities:Loan -1000 CNY\n'
        transactions += '2026-10-05 * "partial repayment"\n  Assets:Bank -200 CNY\n  Liabilities:Loan 200 CNY\n'
        september = c.run_ledger(transactions)
        october = c.run_ledger(transactions, "2026-10")
        november = c.run_ledger(transactions, "2026-11")
        self.assertEqual(september["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(october["actual_spendable"]["actual_spendable"], "9000")
        self.assertEqual(november["actual_spendable"]["actual_spendable"], "9000")
        self.assertEqual(november["actual_spendable"]["booked_debts"][0]["remaining_native"], "800")


    def test_one_cent_partial_repayments_remain_exact(self):
        c = self.case
        report = c.run_ledger(c.purchase(amount="0.03") + c.payment(amount="0.01") + c.payment("2026-09-04", "0.01"))
        self.assertEqual(c.spent(report), Fraction(3, 100))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "999997/100")
        self.assertEqual(report["actual_spendable"]["booked_debts"][0]["remaining_native"], "1/100")

    def test_overpayment_returns_to_actual_cash_purpose_not_responsibility_purpose(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9700", "储蓄": "300"}
        payment = c.payment(amount="150").replace('  Assets:Bank -150 CNY\n', '  Assets:Bank -150 CNY\n    fund: "储蓄"\n')
        report = c.run_ledger(c.purchase() + payment +
            '2026-09-04 * "return"\n  Assets:Bank 50 CNY\n  Liabilities:Card -50 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9700")
        bank = report["transactions"]["rows"][-1]["postings"][0]
        self.assertEqual([(p["name"], p["signed_units"]) for p in bank["h_purpose"]["slices"]], [("储蓄", "50")])

    def mixed_credit(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9900", "储蓄": "100"}
        return '2026-09-02 * "prepay"\n  Assets:Bank -100 CNY\n    fund: "生活费60,储蓄40"\n  Liabilities:Card 100 CNY\n'

    def test_full_return_of_explicit_mixed_prepayment_preserves_both_purposes(self):
        c = self.case
        report = c.run_ledger(self.mixed_credit() +
            '2026-09-03 * "return all"\n  Assets:Bank 100 CNY\n  Liabilities:Card -100 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        parts = report["transactions"]["rows"][-1]["postings"][0]["h_purpose"]["slices"]
        self.assertEqual({p["name"]: p["signed_units"] for p in parts}, {"生活费": "60", "储蓄": "40"})

    def test_partial_mixed_credit_return_requires_choice_instead_of_proportion(self):
        c = self.case
        result = c.run_ledger(self.mixed_credit() +
            '2026-09-03 * "return part"\n  Assets:Bank 40 CNY\n  Liabilities:Card -40 CNY\n', certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("LIABILITY_CREDIT_PURPOSE_PENDING", {r["reason_code"] for r in result["review_items"]})

    def test_explicit_return_cannot_overdraw_a_proven_credit_purpose(self):
        c = self.case
        with self.assertRaises(BudgetError) as error:
            c.run_ledger(self.mixed_credit() +
                '2026-09-03 * "impossible purpose"\n  Assets:Bank 70 CNY\n    fund: "储蓄"\n  Liabilities:Card -70 CNY\n')
        self.assertEqual(error.exception.code, "LIABILITY_CREDIT_OWNER_EXCEEDED")
        self.assertIn("main.bean:", error.exception.source)
        self.assertIn("资金用途：储蓄", error.exception.message)
        self.assertIn("本次取回：70 CNY", error.exception.message)
        self.assertIn("已证明可取回上限：40 CNY", error.exception.message)
        self.assertIn("本次缺口：30 CNY", error.exception.message)

    def test_partial_debt_payment_retains_bounds_even_if_credit_owner_is_ambiguous(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9800", "储蓄": "200"}
        transactions = c.purchase() + c.payment(amount="200").replace('  Assets:Bank -200 CNY\n',
            '  Assets:Bank -200 CNY\n    fund: "生活费150,储蓄50"\n')
        with self.assertRaises(BudgetError) as error:
            c.run_ledger(transactions + '2026-09-04 * "exceed possible saving credit"\n  Assets:Bank 100 CNY\n'
                         '    fund: "储蓄"\n  Liabilities:Card -100 CNY\n')
        self.assertEqual(error.exception.code, "LIABILITY_CREDIT_OWNER_EXCEEDED")
        report = c.run_ledger(transactions + '2026-09-04 * "choose saving"\n  Assets:Bank 50 CNY\n'
            '    fund: "储蓄"\n  Liabilities:Card -50 CNY\n'
            '2026-09-05 * "only living can remain"\n  Assets:Bank 50 CNY\n  Liabilities:Card -50 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9700")

    def test_full_returns_from_two_known_credit_pools_validate_combined_purpose(self):
        c = self.case
        c.doc["liabilities"]["Liabilities:Colleague"] = {"role": "credit_card"}
        transactions = c.payment(amount="100") + '2026-09-04 * "other prepayment"\n  Assets:Bank -100 CNY\n  Liabilities:Colleague 100 CNY\n'
        report = c.run_ledger(transactions + '2026-09-05 * "joint return"\n  Assets:Bank 200 CNY\n'
            '  Liabilities:Card -100 CNY\n  Liabilities:Colleague -100 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(c.outflows(report)["credit_returns"], "200")
        with self.assertRaises(BudgetError) as error:
            c.run_ledger(transactions + '2026-09-05 * "wrong joint return purpose"\n  Assets:Bank 200 CNY\n'
                '    fund: "储蓄"\n  Liabilities:Card -100 CNY\n  Liabilities:Colleague -100 CNY\n')
        self.assertEqual(error.exception.code, "LIABILITY_CREDIT_OWNER_EXCEEDED")

    def test_repeated_same_account_legs_do_not_duplicate_mixed_cash_credit(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9900", "储蓄": "100"}
        transactions = '2026-09-02 * "split credit legs"\n  Assets:Bank -100 CNY\n    fund: "生活费60,储蓄40"\n'
        transactions += '  Liabilities:Card 60 CNY\n  Liabilities:Card 40 CNY\n'
        report = c.run_ledger(transactions + '2026-09-03 * "split return legs"\n  Assets:Bank 100 CNY\n'
            '  Liabilities:Card -60 CNY\n  Liabilities:Card -40 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        self.assertEqual(c.outflows(report)["credit_returns"], "60")
        with self.assertRaises(BudgetError) as error:
            c.run_ledger(transactions + '2026-09-03 * "cannot duplicate saving capacity"\n  Assets:Bank 80 CNY\n'
                         '    fund: "储蓄"\n  Liabilities:Card -80 CNY\n')
        self.assertEqual(error.exception.code, "LIABILITY_CREDIT_OWNER_EXCEEDED")

    def test_joint_mixed_prepayment_source_is_an_explicit_program_gap_when_returned(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9900", "储蓄": "100"}
        c.doc["liabilities"]["Liabilities:Colleague"] = {"role": "credit_card"}
        result = c.run_ledger('2026-09-02 * "mixed joint prepay"\n  Assets:Bank -100 CNY\n    fund: "生活费60,储蓄40"\n'
            '  Liabilities:Card 50 CNY\n  Liabilities:Colleague 50 CNY\n'
            '2026-09-03 * "cannot override source by labeling return"\n  Assets:Bank 100 CNY\n    fund: "储蓄"\n'
            '  Liabilities:Card -50 CNY\n  Liabilities:Colleague -50 CNY\n', certified=False)
        self.assertEqual(result["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("LIABILITY_CREDIT_GROUP_UNSUPPORTED", {r["reason_code"] for r in result["review_items"]})

    def test_pure_credit_transfer_keeps_proved_purpose_without_new_income(self):
        c = self.case
        c.doc["liabilities"]["Liabilities:Colleague"] = {"role": "credit_card"}
        report = c.run_ledger(c.payment(amount="100") + '2026-09-04 * "credit transferred"\n'
            '  Liabilities:Card -100 CNY\n  Liabilities:Colleague 100 CNY\n'
            '2026-09-05 * "return credit"\n  Assets:Bank 100 CNY\n  Liabilities:Colleague -100 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(c.spent(report), 0)

    def test_zero_cash_leg_does_not_add_a_credit_purpose(self):
        c = self.case
        c.doc["accounts"]["Assets:Other"] = {"semantics": "H", "payment_scope": {"CNY": "payable"}}
        report = c.run_ledger('2026-08-01 open Assets:Other\n' + c.payment(amount="50") +
            '2026-09-04 * "credit return"\n  Assets:Bank 50 CNY\n  Assets:Other 0 CNY\n    fund: "储蓄"\n  Liabilities:Card -50 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(c.outflows(report)["credit_returns"], "50")

    def test_external_approved_cash_purpose_is_used_for_receivable_validation(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9000", "储蓄": "1000"}
        c.doc["liabilities"] = {"Liabilities:Advance": {"role": "receivable"}}
        c.run_ledger('2026-09-02 * "advance"\n  Assets:Bank -100 CNY\n  Liabilities:Advance 100 CNY\n    budget: "储蓄"\n'
            '2026-09-03 * "reimburse"\n  Assets:Bank 100 CNY\n  Liabilities:Advance -100 CNY\n    budget: "储蓄"\n', certified=False)
        runtime = build_runtime(c.config)
        self.addCleanup(runtime.close)
        ownership = runtime.config.parent / "ownership.yaml"
        document = load_yaml(ownership)
        owner = next(row["id"] for row in document["owners"] if row["name"] == "储蓄")
        fin = load_financial(c.ledger)
        posting = next(p for e in fin.entries if isinstance(e, data.Transaction) and e.date == date(2026, 9, 2)
                       for p in e.postings if p.account == "Assets:Bank")
        document["claims"] = [{"claim": FactIndex(fin).for_posting(posting).as_dict(), "reason": "虚拟账本明确批准储蓄代垫",
                               "slices": [{"id": "approved_advance", "owner_id": owner, "role": "hold", "signed_units": "-100"}]}]
        c.write(ownership, document)
        save_snapshot(c.config, runtime.config)
        result = audit_simple(c.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertEqual(result["report"]["actual_spendable"]["actual_spendable"], "9000")

    def test_explicit_partial_credit_return_retains_proven_remaining_purpose(self):
        c = self.case
        report = c.run_ledger(self.mixed_credit() +
            '2026-09-03 * "return saving"\n  Assets:Bank 40 CNY\n    fund: "储蓄"\n  Liabilities:Card -40 CNY\n'
            '2026-09-04 * "return living"\n  Assets:Bank 60 CNY\n  Liabilities:Card -60 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        self.assertEqual(c.outflows(report)["credit_returns"], "60")

    def test_spending_partial_mixed_credit_does_not_invent_a_remaining_purpose(self):
        c = self.case
        result = c.run_ledger(self.mixed_credit() + c.purchase("2026-09-03", "30") +
            '2026-09-04 * "return remainder"\n  Assets:Bank 70 CNY\n  Liabilities:Card -70 CNY\n', certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("LIABILITY_CREDIT_PURPOSE_PENDING", {r["reason_code"] for r in result["review_items"]})

    def test_credit_return_plus_new_borrowing_is_not_silently_pure_credit_return(self):
        c = self.case
        result = c.run_ledger(c.payment(amount="50") +
            '2026-09-04 * "withdraw beyond credit"\n  Assets:Bank 100 CNY\n  Liabilities:Card -100 CNY\n', certified=False)
        self.assertEqual(result["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("LIABILITY_CREDIT_MIXED_UNSUPPORTED", {r["reason_code"] for r in result["review_items"]})

    def test_zero_debt_leg_does_not_turn_credit_return_into_new_borrowing(self):
        c = self.case
        c.doc["liabilities"]["Liabilities:Colleague"] = {"role": "payable"}
        report = c.run_ledger(c.payment(amount="50") +
            '2026-09-04 * "return with zero leg"\n  Assets:Bank 50 CNY\n  Liabilities:Card -50 CNY\n  Liabilities:Colleague 0 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(c.outflows(report)["credit_returns"], "50")

    def test_joint_advance_and_debt_payment_use_known_purpose_constraints(self):
        c = self.case
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9900", "储蓄": "100"}
        c.doc["liabilities"]["Liabilities:Advance"] = {"role": "receivable"}
        report = c.run_ledger(c.purchase(amount="40") +
            '2026-09-03 * "repay and advance"\n  Assets:Bank -100 CNY\n    fund: "生活费60,储蓄40"\n'
            '  Liabilities:Card 40 CNY\n  Liabilities:Advance 60 CNY\n')
        self.assertEqual(c.outflows(report)["advances_paid"], "60")
        self.assertEqual(c.outflows(report)["debt_payments"], "0")
        self.assertEqual(c.spent(report), 40)

    def test_credit_in_one_currency_cannot_cancel_debt_in_another(self):
        c = self.case
        result = c.run_ledger(c.payment(amount="100") +
            '2026-09-04 * "USD purchase"\n  Expenses:Food 10 USD\n  Liabilities:Card -10 USD\n', certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("SETTLEMENT_VALUE_MISSING", {r["reason_code"] for r in result["review_items"]})
        runtime = build_runtime(c.config)
        self.addCleanup(runtime.close)
        audit = audit_simple_obligations(c.ledger, date(2026, 9, 1), runtime.config.parent / "obligations.yaml", date(2026, 9, 30))
        self.assertEqual([(r["commodity"], r["remaining_native"]) for r in audit["obligations"]], [("USD", "10")])
        self.assertEqual([(r["commodity"], r["remaining_native"]) for r in audit["clearing_advances"]], [("CNY", "100")])

    def test_same_category_on_expense_and_debt_does_not_double_charge(self):
        c = self.case
        report = c.run_ledger(c.purchase().replace('  Liabilities:Card -100 CNY\n',
            '  Liabilities:Card -100 CNY\n    budget: "生活开销"\n') +
            c.payment(label='    budget: "生活开销"\n'))
        self.assertEqual(c.spent(report), 100)
        self.assertEqual(report["max_spendable"]["max_spendable"], "9900")

    def test_unrelated_zero_card_leg_does_not_hide_a_cash_expense(self):
        c = self.case
        report = c.run_ledger('2026-09-02 * "cash expense with zero debt"\n  Assets:Bank -10 CNY\n'
            '  Expenses:Food 10 CNY\n  Liabilities:Card 0 CNY\n')
        self.assertEqual(c.spent(report), 10)
        self.assertEqual(c.outflows(report)["expenses_paid"], "10")

    def test_prepayment_from_before_cutover_needs_its_purpose_or_explicit_return_fund(self):
        c = self.case
        transactions = '2026-08-31 * "old credit"\n  Assets:Bank -50 CNY\n  Liabilities:Card 50 CNY\n'
        c.doc["accounts"]["Assets:Bank"]["opening"]["CNY"]["生活费"] = "9950"
        receipt = '2026-09-03 * "old credit returned"\n  Assets:Bank 50 CNY\n  Liabilities:Card -50 CNY\n'
        result = c.run_ledger(transactions + receipt, certified=False)
        self.assertIn("LIABILITY_CREDIT_PURPOSE_PENDING", {r["reason_code"] for r in result["review_items"]})
        report = c.run_ledger(transactions + receipt.replace('  Assets:Bank 50 CNY\n', '  Assets:Bank 50 CNY\n    fund: "生活费"\n'))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")

    def test_randomized_credit_histories_match_an_independent_cent_oracle(self):
        c = self.case
        money = lambda cents: f"{cents // 100}.{cents % 100:02d}"
        for seed in range(12):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                cash, liability, expense, paid, returned = 1000000, 0, 0, 0, 0
                lines = []
                for index in range(45):
                    action = rng.choice(["purchase", "payment", "refund"] + (["return"] if liability > 0 else []))
                    cents = rng.randint(1, min(liability, 5000) if action == "return" else 5000)
                    day = f"2026-09-{index // 2 + 2:02d}"
                    lines.append(f'{day} * "{action} {index}"\n')
                    if action == "purchase":
                        expense += cents; liability -= cents
                        lines.append(f'  Expenses:Food {money(cents)} CNY\n  Liabilities:Card -{money(cents)} CNY\n')
                    elif action == "refund":
                        expense -= cents; liability += cents
                        lines.append(f'  Expenses:Food -{money(cents)} CNY\n  Liabilities:Card {money(cents)} CNY\n')
                    elif action == "payment":
                        cash -= cents; liability += cents; paid += cents
                        lines.append(f'  Assets:Bank -{money(cents)} CNY\n  Liabilities:Card {money(cents)} CNY\n')
                    else:
                        cash += cents; liability -= cents; returned += cents
                        lines.append(f'  Assets:Bank {money(cents)} CNY\n  Liabilities:Card -{money(cents)} CNY\n')
                report = c.run_ledger(''.join(lines))
                self.assertEqual(c.spent(report), Fraction(expense, 100))
                self.assertEqual(Fraction(report["actual_spendable"]["actual_spendable"]), Fraction(cash - max(-liability, 0), 100))
                totals = c.outflows(report)
                self.assertEqual(Fraction(totals["debt_payments"]), Fraction(paid, 100))
                self.assertEqual(Fraction(totals.get("credit_returns", "0")), Fraction(returned, 100))
                self.assertEqual(Fraction(totals["net_outflow"]), Fraction(paid - returned, 100))


if __name__ == "__main__":
    unittest.main()

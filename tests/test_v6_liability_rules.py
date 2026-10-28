"""Ordinary configured debts require no per-transaction human approvals."""

from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple, build_runtime, save_snapshot, audit_simple_period, create_files
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.monthly_report import render_text
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.v6.period_report import csv_rows
from beancount_budget_tool.yamlstrict import yaml_path_location
from datetime import date


HEADER = '''option "operating_currency" "CNY"
2026-08-01 open Assets:Bank
2026-08-01 open Equity:Opening
2026-08-01 open Expenses:Food
2026-08-01 open Expenses:Interest
2026-08-01 open Income:Work
2026-08-01 open Liabilities:Card
2026-08-01 open Liabilities:Loan
2026-08-01 open Liabilities:Colleague
2026-08-01 open Liabilities:Advance
2026-08-31 * "opening"
  Assets:Bank 10000 CNY
  Equity:Opening -10000 CNY
'''


class LiabilityRuleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.ledger = self.base / "main.bean"
        self.config = self.base / "config.yml"
        self.plans = self.base / "budgets.yml"
        self.doc = {"schema": 1, "ledger": "main.bean", "cutover": "2026-09-01", "month": "2026-09",
                    "report_currency": "CNY", "budgets": "budgets.yml",
                    "defaults": {"income_purpose": "自由资金", "expense_purpose": "生活费", "expense_category": "生活开销"},
                    "accounts": {"Assets:Bank": {"semantics": "H", "payment_scope": {"CNY": "payable"},
                                                 "opening": {"CNY": {"生活费": "10000"}}}},
                    "liabilities": {"Liabilities:Card": {"role": "credit_card"}}}
        self.budgets = {"schema": 1, "defaults": {"expense_targets": {"生活开销": "20000"}},
                        "months": {month: {"completeness_confirmed": True} for month in ("2026-09", "2026-10", "2026-11")}}

    def write(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def run_ledger(self, transactions, month="2026-09", *, certified=True):
        self.ledger.write_text(HEADER + transactions, encoding="utf-8")
        self.write(self.config, self.doc)
        self.write(self.plans, self.budgets)
        original = {p: p.read_bytes() for p in (self.ledger, self.config, self.plans)}
        result = audit_simple(self.config, month, include_transactions=True)
        self.assertEqual(original, {p: p.read_bytes() for p in original})
        if certified:
            self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
            self.assertEqual(result["review_items"], [])
            self.assertFalse((self.base / ".budget-state").exists())
            return result["report"]
        return result

    def spent(self, report):
        return Fraction(next(row["subtree_spent"] for row in report["expense_targets"]["categories"] if row["name"] == "生活开销"))

    def outflows(self, report):
        return report["default_outflows"]["totals"][0]

    def purchase(self, when="2026-09-02", amount="100"):
        return f'{when} * "purchase"\n  Expenses:Food {amount} CNY\n  Liabilities:Card -{amount} CNY\n'

    def payment(self, when="2026-09-03", amount="100", label='    budget: "生活费"\n'):
        return f'{when} * "payment"\n  Assets:Bank -{amount} CNY\n  Liabilities:Card {amount} CNY\n' + label

    def test_purchase_and_repayment_charge_consumption_once(self):
        report = self.run_ledger(self.purchase() + self.payment())
        self.assertEqual(self.spent(report), 100)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        self.assertEqual(self.outflows(report)["debt_payments"], "100")
        self.assertEqual(self.outflows(report)["expenses_paid"], "0")
        activity = report["liability_activity"]
        self.assertEqual(activity["budget_totals"], [{"kind": "payment", "category": "生活费", "commodity": "CNY", "signed_liability_units": "100"}])
        self.assertIn("不重复计入消费", render_text(report))
        self.assertTrue(all(e["claim_origin"] == "account_rule" for e in activity["events"]))
        detail = "\n".join(render_transaction(report["transactions"]["rows"][-1], report["owner_names"]))
        self.assertIn("负债 budget（不新增消费）", detail)

    def test_partial_repayment_and_future_month_do_not_change_previous_month(self):
        transactions = self.purchase(amount="300") + self.payment(amount="100")
        september = self.run_ledger(transactions)
        self.assertEqual(september["actual_spendable"]["actual_spendable"], "9700")
        remaining = september["actual_spendable"]["booked_debts"]
        self.assertEqual(remaining[0]["remaining_native"], "200")
        transactions += self.payment("2026-10-03", "200")
        again = self.run_ledger(transactions)
        for key in ("actual_spendable", "uncovered_responsibility", "booked_debts", "clearing_receivables"):
            self.assertEqual(again["actual_spendable"][key], september["actual_spendable"][key])
        october = self.run_ledger(transactions, "2026-10")
        self.assertEqual(self.spent(october), 0)
        self.assertEqual(october["actual_spendable"]["actual_spendable"], "9700")
        self.assertEqual(october["actual_spendable"]["booked_debts"], [])

    def test_refund_to_card_and_excess_credit_never_manufacture_cash(self):
        transactions = self.purchase() + '2026-09-04 * "refund"\n  Expenses:Food -120 CNY\n  Liabilities:Card 120 CNY\n    budget: "生活费"\n'
        report = self.run_ledger(transactions)
        self.assertEqual(self.spent(report), -20)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(report["actual_spendable"]["clearing_receivables"][0]["remaining_native"], "20")
        self.assertEqual(self.outflows(report)["gross_outflow"], "0")
        self.assertEqual(self.outflows(report)["gross_inflow"], "0")

    def test_same_day_payment_before_purchase_is_real_prepayment_not_invented_order(self):
        report = self.run_ledger(self.payment("2026-09-02") + self.purchase())
        self.assertEqual(self.spent(report), 100)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        self.assertEqual([e["kind"] for e in report["liability_activity"]["events"]], ["payment", "consumer_purchase"])

    def test_principal_plus_interest_split_by_postings(self):
        self.doc["liabilities"] = {"Liabilities:Loan": {"role": "loan", "due_on": "2026-09-30"}}
        report = self.run_ledger('2026-09-01 * "loan"\n  Assets:Bank 1000 CNY\n  Liabilities:Loan -1000 CNY\n'
            '2026-09-20 * "repay"\n  Assets:Bank -1010 CNY\n  Liabilities:Loan 1000 CNY\n    budget: "生活费"\n  Expenses:Interest 10 CNY\n')
        self.assertEqual(self.spent(report), 10)
        self.assertEqual(self.outflows(report)["expenses_paid"], "10")
        self.assertEqual(self.outflows(report)["debt_payments"], "1000")
        self.assertEqual(self.outflows(report)["gross_outflow"], "1010")

    def test_future_bullet_loan_is_disclosed_without_deducting_all_future_principal(self):
        self.doc["liabilities"] = {"Liabilities:Loan": {"role": "loan", "due_on": "2026-10-31"}}
        transactions = '2026-09-01 * "loan"\n  Assets:Bank 1000 CNY\n  Liabilities:Loan -1000 CNY\n'
        september = self.run_ledger(transactions)
        self.assertEqual(september["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(september["actual_spendable"]["future_loan_principal"][0]["remaining_native"], "1000")
        october = self.run_ledger(transactions, "2026-10")
        self.assertEqual(october["actual_spendable"]["actual_spendable"], "9000")

    def test_mixed_own_income_and_money_held_for_others_is_not_consumption(self):
        self.doc["liabilities"] = {"Liabilities:Colleague": {"role": "payable"}}
        report = self.run_ledger('2026-09-02 * "mixed receipt"\n  Assets:Bank 480 CNY\n    fund: "生活费"\n'
            '  Income:Work -240 CNY\n  Liabilities:Colleague -240 CNY\n    budget: "同事差旅费"\n'
            '2026-09-03 * "forward"\n  Assets:Bank -240 CNY\n  Liabilities:Colleague 240 CNY\n    budget: "同事差旅费"\n')
        self.assertEqual(self.spent(report), 0)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10240")
        self.assertEqual(self.outflows(report)["debt_payments"], "240")

    def test_many_payables_settle_as_declared_pool_without_matching_equal_receipts(self):
        self.doc["liabilities"] = {"Liabilities:Colleague": {"role": "payable"}}
        transactions = ''.join(f'2026-09-02 * "receipt {i}"\n  Assets:Bank 260 CNY\n  Liabilities:Colleague -260 CNY\n' for i in range(2))
        transactions += '2026-09-03 * "forward"\n  Assets:Bank -520 CNY\n  Liabilities:Colleague 520 CNY\n'
        report = self.run_ledger(transactions)
        self.assertEqual(self.spent(report), 0)
        self.assertEqual(report["actual_spendable"]["booked_debts"], [])

    def test_receivable_reimbursement_restores_original_purpose_not_income_default(self):
        self.doc["liabilities"] = {"Liabilities:Advance": {"role": "receivable"}}
        report = self.run_ledger('2026-09-02 * "advance"\n  Assets:Bank -500 CNY\n  Liabilities:Advance 500 CNY\n'
            '2026-09-03 * "reimburse"\n  Assets:Bank 500 CNY\n  Liabilities:Advance -500 CNY\n')
        self.assertEqual(self.spent(report), 0)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "10000")
        self.assertEqual(self.outflows(report)["advances_paid"], "500")
        self.assertEqual(self.outflows(report)["advance_reimbursements"], "500")

    def test_refinancing_does_not_create_consumption_or_payment(self):
        self.doc["liabilities"]["Liabilities:Colleague"] = {"role": "payable"}
        report = self.run_ledger(self.purchase() + '2026-09-03 * "refinance"\n  Liabilities:Card 100 CNY\n  Liabilities:Colleague -100 CNY\n    budget: "生活费"\n')
        self.assertEqual(self.spent(report), 100)
        self.assertEqual(self.outflows(report)["gross_outflow"], "0")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")

    def test_consumer_debt_cannot_be_hidden_as_a_future_nonconsumer_loan(self):
        self.doc["liabilities"]["Liabilities:Loan"] = {"role": "loan", "due_on": "2028-01-01"}
        result = self.run_ledger(self.purchase() + '2026-09-03 * "refinance"\n  Liabilities:Card 100 CNY\n  Liabilities:Loan -100 CNY\n', certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("OBLIGATION_EVENT_UNSUPPORTED", {row["reason_code"] for row in result["review_items"]})

    def test_two_cards_share_one_cash_payment_once(self):
        self.doc["liabilities"]["Liabilities:Colleague"] = {"role": "credit_card"}
        report = self.run_ledger('2026-09-02 * "two cards"\n  Expenses:Food 300 CNY\n  Liabilities:Card -100 CNY\n'
            '  Liabilities:Colleague -200 CNY\n2026-09-03 * "one payment"\n  Assets:Bank -300 CNY\n'
            '  Liabilities:Card 100 CNY\n    budget: "生活费"\n  Liabilities:Colleague 200 CNY\n    budget: "生活费"\n')
        self.assertEqual(self.spent(report), 300)
        self.assertEqual(self.outflows(report)["debt_payments"], "300")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9700")

    def test_opening_card_debt_is_carried_without_recounting_old_expenses(self):
        transactions = self.purchase("2026-08-31", "300") + self.payment(amount="100")
        report = self.run_ledger(transactions)
        self.assertEqual(self.spent(report), 0)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9700")
        self.assertEqual(report["actual_spendable"]["booked_debts"][0]["remaining_native"], "200")
        self.assertEqual(len(report["actual_spendable"]["opening_responsibilities"]), 1)
        self.assertIn("未清已记", render_text(report))

    def test_receivable_rule_must_not_reassign_reimbursement_to_a_different_purpose(self):
        self.doc["liabilities"] = {"Liabilities:Advance": {"role": "receivable"}}
        result = self.run_ledger('2026-09-02 * "advance"\n  Assets:Bank -500 CNY\n  Liabilities:Advance 500 CNY\n    budget: "储蓄"\n', certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("LIABILITY_RULE_PURPOSE_MISMATCH", {row["reason_code"] for row in result["review_items"]})

    def test_salary_cycle_spending_does_not_count_repayment_again(self):
        self.doc["expense_budget_basis"] = "salary_cycle"
        self.budgets["salary_cycles"] = {"2026-09-01": "500", "2026-10-01": "500"}
        report = self.run_ledger(self.purchase() + self.payment())
        cycle = report["salary_cycle"]["report"]["cycle_targets"][0]
        self.assertEqual(cycle["net_spent"], "100")
        self.assertEqual(cycle["remaining"], "400")
        self.assertEqual(report["max_spendable"]["max_spendable"], "400")

    def test_removing_public_rule_does_not_resurrect_it_from_saved_state(self):
        self.run_ledger(self.purchase() + self.payment())
        runtime = build_runtime(self.config)
        self.addCleanup(runtime.close)
        save_snapshot(self.config, runtime.config)
        self.doc.pop("liabilities")
        self.write(self.config, self.doc)
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")




    def test_editing_old_amounts_recomputes_pool_instead_of_preserving_an_old_approval(self):
        report = self.run_ledger(self.purchase() + self.payment())
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9900")
        report = self.run_ledger(self.purchase(amount="130") + self.payment())
        self.assertEqual(self.spent(report), 130)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "9870")
        self.assertEqual(report["actual_spendable"]["booked_debts"][0]["remaining_native"], "30")

    def test_rule_and_budget_categories_survive_public_configuration_migration(self):
        self.run_ledger(self.purchase() + self.payment())
        runtime = build_runtime(self.config)
        self.addCleanup(runtime.close)
        migrated = create_files(self.base / "migrated", old_config=runtime.config)
        document = yaml.safe_load(Path(migrated["config"]).read_text(encoding="utf-8"))
        self.assertEqual(document["liabilities"], self.doc["liabilities"])
        result = audit_simple(Path(migrated["config"]))
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertEqual(result["report"]["liability_activity"]["budget_totals"][0]["signed_liability_units"], "100")

    def test_csv_separates_liability_categories_from_consumption(self):
        self.run_ledger(self.purchase() + self.payment())
        rows = csv_rows(audit_simple_period(self.config, "2026-09", "2026-09"))
        debt = [r for r in rows if r["section"] == "liability_budget"]
        self.assertEqual([(r["name"], r["value"]) for r in debt], [("生活费", "100")])
        expense = [r for r in rows if r["section"] == "expense_total" and r["metric"] == "direct_spent"]
        self.assertEqual(expense[0]["value"], "100")

    def test_multicategory_liability_budget_validates_total_but_not_as_expense(self):
        report = self.run_ledger(self.purchase().replace('Liabilities:Card -100 CNY\n','Liabilities:Card -100 CNY\n    budget: \"偿债.甲60,偿债.乙40\"\n') + self.payment(label='    budget: "偿债.甲60,偿债.乙40"\n'))
        self.assertEqual(self.spent(report), 100)
        self.assertEqual({r["category"] for r in report["liability_activity"]["budget_totals"]}, {"偿债.甲", "偿债.乙"})
        invalid = self.run_ledger(self.purchase() + self.payment(label='    budget: "偿债.甲60,偿债.乙39"\n'), certified=False)
        self.assertIsNone(invalid["report"])
        self.assertTrue(all(row["reason_code"] == "TAG_SYNTAX_INVALID" for row in invalid["review_items"]))
        self.assertTrue(all("main.bean:" in row["source"] for row in invalid["review_items"]))

    def test_default_liability_account_needs_no_account_rule(self):
        self.doc.pop("liabilities")
        result = self.run_ledger(self.purchase() + self.payment(), certified=False)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["review_items"], [])

    def test_receivable_cannot_hide_a_payable_by_overreimbursement(self):
        self.doc["liabilities"] = {"Liabilities:Advance": {"role": "receivable"}}
        result = self.run_ledger('2026-09-02 * "unexpected receipt"\n  Assets:Bank 500 CNY\n  Liabilities:Advance -500 CNY\n', certified=False)
        self.assertIsNone(result["report"])
        self.assertIn("LIABILITY_RULE_BALANCE_INVALID", {r["reason_code"] for r in result["review_items"]})

    def test_loan_requires_due_date_and_reports_config_line(self):
        self.doc["liabilities"] = {"Liabilities:Loan": {"role": "loan"}}
        with self.assertRaises(BudgetError) as error:
            self.run_ledger('')
        self.assertEqual(error.exception.code, "LIABILITY_RULE_INVALID")
        self.assertEqual(error.exception.source, yaml_path_location(self.config, ("liabilities", "Liabilities:Loan")))

    def test_incomplete_advanced_rule_document_is_a_located_error_not_keyerror(self):
        self.run_ledger(self.purchase())
        runtime = build_runtime(self.config)
        self.addCleanup(runtime.close)
        path = runtime.config.parent / "obligations.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document.pop("owners")
        self.write(path, document)
        with self.assertRaises(BudgetError) as error:
            audit_simple_obligations(self.ledger, date(2026, 9, 1), path)
        self.assertEqual(error.exception.code, "OBLIGATION_DECLARATION_INVALID")
        self.assertEqual(error.exception.source, yaml_path_location(path, ()))

    def test_interest_from_multiple_cash_accounts_is_an_implementation_gap_not_data_review(self):
        self.doc["accounts"]["Assets:Other"] = {"semantics": "H", "payment_scope": {"CNY": "payable"}}
        result = self.run_ledger('2026-08-01 open Assets:Other\n'
            '2026-09-01 * "move cash"\n  Assets:Bank -20 CNY\n  Assets:Other 20 CNY\n' + self.purchase() +
            '2026-09-03 * "joint repayment with interest"\n  Assets:Bank -90 CNY\n  Assets:Other -20 CNY\n'
            '  Liabilities:Card 100 CNY\n  Expenses:Interest 10 CNY\n', certified=False)
        self.assertEqual(result["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("DEFAULT_OUTFLOW_EVENT_UNSUPPORTED", {row["reason_code"] for row in result["review_items"]})

    def test_principal_and_interest_with_mixed_purposes_requires_actual_split(self):
        self.doc["accounts"]["Assets:Bank"]["opening"]["CNY"] = {"生活费": "9990", "储蓄": "10"}
        result = self.run_ledger(self.purchase() +
            '2026-09-03 * "mixed purposes"\n  Assets:Bank -110 CNY\n    fund: "生活费100,储蓄10"\n'
            '  Liabilities:Card 100 CNY\n  Expenses:Interest 10 CNY\n', certified=False)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIn("DEFAULT_CASH_SPLIT_AMBIGUOUS", {row["reason_code"] for row in result["review_items"]})


if __name__ == "__main__":
    unittest.main()

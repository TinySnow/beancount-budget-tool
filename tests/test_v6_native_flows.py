"""Native multi-leg cash flows certify joint evidence without inventing pairings."""

from pathlib import Path
from datetime import date
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.tools.v6_init import create_setup, compile_setup
from beancount_budget_tool.tools.v6_propose_benefit_groups import propose
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.review_diagnostics import review_item


HEADER = (
    'option "operating_currency" "CNY"\n'
    '2026-08-01 open Assets:Cash\n2026-08-01 open Assets:Bank\n'
    '2026-08-01 open Assets:Book\n2026-08-01 open Equity:Opening\n'
    '2026-08-01 open Expenses:Food\n2026-08-01 open Expenses:Travel\n'
    '2026-08-01 open Income:Salary\n2026-08-01 open Income:Bonus\n'
    '2026-08-01 open Liabilities:Card\n'
    '2026-08-31 * "opening"\n  Assets:Cash 100 CNY\n  Assets:Bank 50 CNY\n  Equity:Opening -150 CNY\n'
)


class NativeFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.ledger = self.base / "main.bean"

    def configure(self, transactions, bank_owner="生活费"):
        self.ledger = self.base / "main.bean"
        self.ledger.write_text(HEADER + transactions, encoding="utf-8")
        setup = Path(create_setup(self.ledger, "2026-09", self.base / "initial",
                                  income_purpose="自由资金", expense_purpose="生活费",
                                  expense_category="生活开销")["setup"])
        document = yaml.safe_load(setup.read_text(encoding="utf-8"))
        document["defaults"]["confirmed"] = document["completeness_confirmed"] = True
        document["expense_targets"] = {"生活开销": "500"}
        document["allocation_targets"] = {name: "not_assessed" for name in document["allocation_targets"]}
        for account in document["accounts"]:
            account.update(semantics="K" if account["account"] == "Assets:Book" else "H", reason="虚拟账本已审")
            account["payment_scope"] = {key: "payable" if account["semantics"] == "H" else "excluded"
                                        for key in account["payment_scope"]}
            for opening in account["opening"]:
                opening["owners"] = {bank_owner if account["account"] == "Assets:Bank" else "生活费": opening["signed_units"]}
        setup.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.config = Path(compile_setup(setup)["config"])
        return self.config

    def report(self, transactions, *, bank_owner="生活费"):
        self.configure(transactions, bank_owner)
        outcome = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        self.assertEqual(outcome["review_items"], [])
        report = outcome["report"]
        self.assertTrue(all(row["actual_g"] == "0" for row in report["purpose_progress"]["owners"]))
        self.assertTrue(all(row["net_allocations"] == {} for row in report["transactions"]["rows"]))
        return report

    def test_two_expense_accounts_one_default_payer_certify(self):
        report = self.report('2026-09-02 * "food and travel"\n'
            '  Expenses:Food 6 CNY\n  Expenses:Travel 4 CNY\n  Assets:Cash -10 CNY\n')
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "10")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "140")
        self.assertEqual(report["max_spendable"]["max_spendable"], "140")
        row = report["transactions"]["rows"][0]
        self.assertEqual(len(row["purpose_events"]), 1)
        self.assertEqual(row["postings"][2]["h_purpose"]["origin"], "inferred")
        self.assertEqual(row["postings"][2]["h_purpose"]["slices"][0]["name"], "生活费")

    def test_two_payment_accounts_with_defaults_and_multiple_expenses(self):
        report = self.report('2026-09-02 * "shared payment"\n'
            '  Expenses:Food 7 CNY\n  Expenses:Travel 3 CNY\n'
            '  Assets:Cash -6 CNY\n  Assets:Bank -4 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "140")
        total = report["default_outflows"]["totals"][0]
        self.assertEqual(total["expenses_paid"], "10")
        self.assertEqual({row["account"]: row["expenses_paid"] for row in total["accounts"]},
                         {"Assets:Cash": "6", "Assets:Bank": "4"})
        self.assertEqual(len(report["transactions"]["rows"][0]["purpose_events"][0]["h_refs"]), 2)

    def test_explicit_savings_payer_is_independent_of_expense_category(self):
        report = self.report('2026-09-02 * "two funding purposes"\n'
            '  Expenses:Food 10 CNY\n  Assets:Cash -6 CNY\n'
            '  Assets:Bank -4 CNY\n    fund: "储蓄"\n', bank_owner="储蓄")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "94")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "6")
        expense = next(row for row in report["expense_targets"]["categories"] if row["name"] == "生活开销")
        self.assertEqual(expense["subtree_spent"], "10")
        bank = next(part for part in report["transactions"]["rows"][0]["postings"] if part["account"] == "Assets:Bank")
        self.assertEqual(bank["h_purpose"]["slices"][0]["name"], "储蓄")

    def test_multi_refund_joint_evidence_keeps_totals_without_pairwise_claims(self):
        report = self.report('2026-09-02 * "joint refund"\n'
            '  Expenses:Food -3 CNY\n  Expenses:Travel -4 CNY\n'
            '  Assets:Cash 5 CNY\n  Assets:Bank 2 CNY\n')
        self.assertEqual(report["default_outflows"]["totals"][0]["expense_refunds"], "7")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "157")
        posts = report["transactions"]["rows"][0]["postings"]
        benefits = [part["benefit_source"] for part in posts if part["benefit_source"]]
        self.assertEqual(len(benefits), 2)
        self.assertEqual(benefits[0]["evidence_group"], benefits[1]["evidence_group"])
        self.assertTrue(all("evidence_ref" not in item for item in benefits))
        group = benefits[0]["evidence_group"]
        self.assertEqual((group["asset_total"], group["benefit_total"]), ("7", "-7"))
        self.assertEqual(len(group["asset_refs"]), 2)
        self.assertEqual(len(group["benefit_refs"]), 2)

    def test_joint_income_and_full_reversal_use_income_purpose_not_spending(self):
        report = self.report('2026-09-02 * "joint income"\n'
            '  Income:Salary -5 CNY\n  Income:Bonus -7 CNY\n'
            '  Assets:Cash 8 CNY\n  Assets:Bank 4 CNY\n'
            '2026-09-03 * "income returned"\n'
            '  Income:Salary 5 CNY\n  Income:Bonus 7 CNY\n'
            '  Assets:Cash -8 CNY\n  Assets:Bank -4 CNY\n')
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "150")
        self.assertEqual(report["default_outflows"]["totals"][0]["net_outflow"], "0")
        for row in report["transactions"]["rows"]:
            for part in row["postings"]:
                if part["h_purpose"]:
                    self.assertEqual(part["h_purpose"]["slices"][0]["name"], "自由资金")

    def test_same_amount_separate_receipts_have_distinct_source_groups(self):
        report = self.report('2026-09-02 * "first receipt"\n'
            '  Income:Salary -5 CNY\n  Income:Bonus -5 CNY\n  Assets:Cash 10 CNY\n'
            '2026-09-02 * "second receipt"\n'
            '  Income:Salary -5 CNY\n  Income:Bonus -5 CNY\n  Assets:Cash 10 CNY\n')
        groups = [row["postings"][0]["benefit_source"]["evidence_group"] for row in report["transactions"]["rows"]]
        self.assertNotEqual(groups[0]["transaction_ref"], groups[1]["transaction_ref"])

    def test_explicit_multi_owner_split_remains_on_each_actual_asset(self):
        report = self.report('2026-09-02 * "split purposes"\n'
            '  Expenses:Food 10 CNY\n'
            '  Assets:Cash -6 CNY\n    fund: "生活费4，自由资金2"\n'
            '  Assets:Bank -4 CNY\n    fund: "生活费"\n')
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "8")
        warnings = report["account_purposes"]["negative_purpose_positions"]
        self.assertEqual(len(warnings), 1)  # Explicit spending can take the named free share below zero.

    def test_cash_receipt_and_refund_domains_must_not_share_an_automatic_group(self):
        self.configure('2026-09-02 * "mixed benefit domains"\n'
            '  Expenses:Food -5 CNY\n  Income:Bonus -5 CNY\n'
            '  Assets:Cash 10 CNY\n    fund: "生活费"\n')
        outcome = audit_month_workflow(self.config)
        self.assertIsNone(outcome["report"])
        codes = {item["reason_code"] for item in outcome["review_items"]}
        self.assertIn("BENEFIT_GROUP_SHAPE_UNSUPPORTED", codes)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertTrue(all(item["reason_kind"] == "implementation_limit" for item in outcome["review_items"]
                            if item["reason_code"] == "BENEFIT_GROUP_SHAPE_UNSUPPORTED"))
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        proposal = propose(self.ledger, date(2026, 9, 1),
                           (self.config.parent / document["classifications"]).resolve(), "CNY")
        self.assertEqual(proposal["unsupported_complex_benefits"], 2)
        self.assertEqual(proposal["proposed_groups"], 0)

    def test_asset_change_or_compound_k_fee_does_not_become_an_ordinary_expense(self):
        for name, transactions in (
            ("change", '  Assets:Cash -20 CNY\n    fund: "储蓄"\n'
                        '  Assets:Bank 10 CNY\n    fund: "生活费"\n  Expenses:Food 10 CNY\n'),
            ("K fee", '  Assets:Cash -21 CNY\n    fund: "生活费"\n'
                       '  Assets:Book 20 CNY\n    fund: "生活费"\n  Expenses:Food 1 CNY\n'),
        ):
            with self.subTest(name=name), TemporaryDirectory() as folder:
                original = self.base
                self.base = Path(folder)
                self.configure(f'2026-09-02 * "{name}"\n' + transactions)
                outcome = audit_month_workflow(self.config)
                self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
                self.assertIsNone(outcome["report"])
                expected = "DEFAULT_OUTFLOW_EVENT_UNSUPPORTED" if name == "change" else "K_EVENT_SHAPE_UNSUPPORTED"
                self.assertIn(expected, {row["reason_code"] for row in outcome["review_items"]})
                self.base = original

    def test_unsupported_named_event_is_an_implementation_limit(self):
        self.assertEqual(review_item("NAMED_H_EVENT_UNSUPPORTED", "ledger.bean:5")["reason_kind"],
                         "implementation_limit")

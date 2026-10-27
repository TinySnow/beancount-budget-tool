"""Native multi-leg cash flows certify joint evidence without inventing pairings."""

from pathlib import Path
from datetime import date
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through
from fixture_inputs import create_setup, compile_setup
from fixture_benefits import propose_groups as propose
from beancount_budget_tool.v6.k_events import audit_k_events
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_text
from beancount_budget_tool.v6.review_diagnostics import review_item
from beancount_budget_tool.v6.spending import audit_monthly_spending
from beancount_budget_tool.v6.transaction_details import render_transaction


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
        document["allocation_targets"].setdefault(bank_owner, "not_assessed")
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

    def redemption(self, receipt="105", income="-5", k_purpose="生活费", cash_purpose="生活费", extra=""):
        return ('2026-08-01 open Income:Gain\n'
                '2026-09-02 * "subscribe"\n'
                '  Assets:Cash -100 CNY\n    fund: "生活费"\n'
                '  Assets:Book 100 CNY\n    fund: "生活费"\n'
                '2026-09-03 * "redeem"\n'
                f'  Assets:Cash {receipt} CNY\n' +
                (f'    fund: "{cash_purpose}"\n' if cash_purpose else '') +
                f'  Assets:Book -100 CNY\n    fund: "{k_purpose}"\n'
                f'  Income:Gain {income} CNY\n    budget: "投资"\n' + extra)

    def income_total(self):
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        spending = audit_monthly_spending(self.ledger, date(2026, 9, 1), "2026-09",
                                          self.config.parent / document["classifications"], "CNY")
        return spending["metrics"]["income_supplement"]["report_total"]

    def zero_records(self, fund="额外储蓄", units="0.00", income="-0.00", extra=""):
        source = '2026-08-01 open Income:Gift\n'
        for ident in ("gift-a", "gift-b"):
            source += ('2026-09-12 * "基金平台" "活动赠送"\n'
                       f'  orderId: "{ident}"\n  Assets:Book {units} CNY\n' +
                       (f'    fund: "{fund}"\n' if fund else '') +
                       f'  Income:Gift {income} CNY\n' + extra)
        return source

    def test_zero_k_gifts_preserve_both_sources_without_inventing_cash_shares_or_value(self):
        report = self.report(self.zero_records())
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "150")
        self.assertEqual(report["max_spendable"]["max_spendable"], "150")
        self.assertEqual(self.income_total(), "0")
        records = report["k_book_purposes"]["zero_value_records"]
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["k_ref"], records[1]["k_ref"])
        self.assertTrue(all(row["cash_ref"] is None and row["share_quantity"] is None and
                            row["market_value"] is None and row["monetary_change"] == "0" for row in records))
        self.assertTrue(all(row["kind"] == row["shape"] == "zero_record" for row in records))
        self.assertTrue(all(len(row["zero_refs"]) == 2 for row in records))
        self.assertIn("保留 2 条 K 零金额记录", render_text(report))
        for tx in report["transactions"]["rows"]:
            event = tx["purpose_events"][0]
            self.assertEqual(event["h_refs"], [])
            self.assertEqual(event["source_refs"], [tx["postings"][0]["ref"]])
            self.assertEqual(tx["postings"][0]["k_purpose"]["slices"][0]["name"], "额外储蓄")
            detail = "\n".join(render_transaction(tx, report["owner_names"]))
            self.assertIn("实际份额与基金价值未记录", detail)
            self.assertIn('orderId: "gift-', detail)

    def test_zero_k_record_without_fund_needs_no_monetary_ownership_decision(self):
        report = self.report(self.zero_records(fund=None))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "150")
        self.assertTrue(all(row["slices"] == [] for row in report["k_book_purposes"]["zero_value_records"]))

    def test_all_zero_record_can_include_extra_zero_postings(self):
        report = self.report(self.zero_records(extra='  Expenses:Food 0.00 CNY\n'))
        self.assertEqual(len(report["k_book_purposes"]["zero_value_records"]), 2)
        self.assertTrue(all(len(row["zero_refs"]) == 3 for row in report["k_book_purposes"]["zero_value_records"]))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "150")

    def test_tiny_nonzero_k_gift_is_not_rounded_to_a_zero_record(self):
        self.configure(self.zero_records(units="0.00000001", income="-0.00000001"))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIsNone(outcome["report"])
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_zero_k_leg_cannot_hide_nonzero_income_and_cash(self):
        self.configure(self.zero_records(income="-1", extra='  Assets:Cash 1 CNY\n    fund: "自由资金"\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIsNone(outcome["report"])
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_zero_with_an_explicit_price_is_not_an_unpriced_zero_record(self):
        self.configure(self.zero_records(income="-0.00").replace('Income:Gift -0.00 CNY', 'Income:Gift -0.00 CNY @ 1 CNY'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_future_zero_records_do_not_enter_the_earlier_month(self):
        self.configure(self.zero_records())
        before = audit_month_workflow(self.config)["report"]
        future = self.zero_records().replace('2026-08-01 open Income:Gift\n', '').replace(
            '2026-09-12', '2026-10-12').replace('gift-', 'future-')
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + future, encoding="utf-8")
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(len(outcome["report"]["k_book_purposes"]["zero_value_records"]), 2)
        self.assertEqual(outcome["report"]["actual_spendable"]["actual_spendable"],
                         before["actual_spendable"]["actual_spendable"])

    def test_zero_fund_annotations_still_validate_registered_names(self):
        self.configure(self.zero_records())
        doc = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        path = self.config.parent / doc["ownership"]
        owners = yaml.safe_load(path.read_text(encoding="utf-8"))
        owners["owners"] = [row for row in owners["owners"] if row["name"] != "额外储蓄"]
        path.write_text(yaml.safe_dump(owners, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_month_workflow(self.config)
        self.assertEqual(caught.exception.code, "OWNER_UNKNOWN")
        self.assertIn("额外储蓄", caught.exception.message)
        self.assertTrue(caught.exception.source.startswith(str(self.ledger)))

    def test_book_redemption_gain_certifies_without_recounting_principal_or_allocations(self):
        report = self.report(self.redemption())
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "155")
        self.assertEqual(report["k_book_purposes"]["rows"][0]["booked_end"], "0")
        transaction = next(row for row in report["transactions"]["rows"] if row["narration"] == "redeem")
        protocol = transaction["purpose_events"][0]["protocol"]
        self.assertEqual(protocol["shape"], "book_redemption_income")
        self.assertEqual((protocol["principal_book_value"], protocol["signed_cash_units"], protocol["income_value"]),
                         ("100", "105", "5"))
        income = next(row for row in transaction["postings"] if row["account"] == "Income:Gain")
        self.assertEqual(income["benefit_source"]["income_report_value"], "5")
        self.assertEqual(income["benefit_source"]["principal_book_value"], "100")
        self.assertEqual(self.income_total(), "5")
        self.assertIn("赎回核对：实收 105 CNY＝账面本金 100 CNY＋损益 5 CNY", "\n".join(
            render_transaction(transaction, report["owner_names"])))

    def test_book_redemption_loss_reduces_real_cash_and_purpose_without_new_expense(self):
        report = self.report(self.redemption("92.47", "7.53"))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "14247/100")
        self.assertEqual(report["default_outflows"]["totals"][0]["net_outflow"], "0")
        transaction = next(row for row in report["transactions"]["rows"] if row["narration"] == "redeem")
        protocol = transaction["purpose_events"][0]["protocol"]
        self.assertEqual(protocol["income_value"], "-753/100")
        self.assertEqual(protocol["slices"][0]["signed_k_units"], "-100")
        self.assertEqual(protocol["slices"][0]["signed_cash_units"], "9247/100")
        self.assertEqual(self.income_total(), "-753/100")

    def test_book_redemption_zero_income_still_preserves_the_purpose(self):
        report = self.report(self.redemption("100", "0"))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "150")

    def test_book_redemption_requires_the_cash_owner_instead_of_defaulting_principal_to_income(self):
        self.configure(self.redemption(cash_purpose=None))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["report"])
        self.assertIn("K_CASH_OWNER_PENDING", {row["reason_code"] for row in outcome["review_items"]})

    def mixed_redemption(self, receipt="105", income="-5", cash_purpose="生活费55,储蓄50", extra=""):
        return self.redemption(receipt, income, "生活费50,储蓄50", cash_purpose, extra).replace(
            '  Assets:Book 100 CNY\n    fund: "生活费"',
            '  Assets:Book 100 CNY\n    fund: "生活费50,储蓄50"')

    def mixed_report(self, source):
        self.configure(source)
        outcome = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        report = outcome["report"]
        tx = next(row for row in report["transactions"]["rows"] if row["narration"] == "redeem")
        self.assertEqual(tx["net_allocations"], {})
        event = tx["purpose_events"][0]["protocol"]
        self.assertEqual(event["protocol"], "matched_purpose_book_redemption_v1")
        self.assertEqual(event["net_allocations"], {})
        self.assertEqual(report["k_book_purposes"]["rows"][0]["purpose_changes"], [])
        return report, tx, event

    def named_changes(self, report, event):
        return {report["owner_names"][owner]: value for owner, value in event["net_purpose_changes"].items()}

    def test_mixed_redemption_assigns_net_gain_without_recounting_allocation(self):
        report, tx, event = self.mixed_report(self.mixed_redemption())
        self.assertEqual(self.named_changes(report, event), {"生活费": "5", "储蓄": "0"})
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "105")
        progress = {report["owner_names"][row["owner_id"]]: row["actual_g"] for row in report["purpose_progress"]["owners"]}
        self.assertEqual((progress["生活费"], progress["储蓄"]), ("-50", "50"))
        self.assertEqual(self.income_total(), "5")
        detail = "\n".join(render_transaction(tx, report["owner_names"]))
        self.assertIn("生活费：本金 50，实收 55，净变动 5 CNY", detail)
        self.assertIn("储蓄：本金 50，实收 50，净变动 0 CNY", detail)
        self.assertIn("不据此拆分各用途毛收益／费用", detail)

    def test_mixed_redemption_can_assign_loss_to_a_protected_purpose(self):
        report, tx, event = self.mixed_report(self.mixed_redemption("95", "5", "生活费50,储蓄45"))
        self.assertEqual(self.named_changes(report, event), {"生活费": "0", "储蓄": "-5"})
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "100")
        self.assertEqual(self.income_total(), "-5")

    def test_mixed_redemption_keeps_net_changes_and_all_fee_sources_separate(self):
        source = self.mixed_redemption("102", "-5", "储蓄55,生活费47",
                                       '  Expenses:Food 1 CNY\n  Expenses:Travel 2 CNY\n')
        report, tx, event = self.mixed_report(source)
        self.assertEqual(self.named_changes(report, event), {"生活费": "-3", "储蓄": "5"})
        self.assertEqual(event["fee_value"], "3")
        self.assertEqual(len(event["fee_refs"]), 2)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "97")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")
        self.assertEqual(self.income_total(), "5")
        self.assertFalse(any("income_value" in part or "fee_value" in part for part in event["slices"]))

    def test_mixed_redemption_without_income_does_not_invent_an_income_source(self):
        source = self.mixed_redemption("99", "-5", "生活费49,储蓄50", '  Expenses:Food 1 CNY\n')
        source = source.replace('  Income:Gain -5 CNY\n    budget: "投资"\n', '')
        report, tx, event = self.mixed_report(source)
        self.assertEqual(self.named_changes(report, event), {"生活费": "-1", "储蓄": "0"})
        self.assertIsNone(event["income_ref"])
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "99")
        self.assertFalse(any(row["account"].startswith("Income:") for row in tx["postings"]))

    def test_mixed_redemption_equal_principal_and_receipt_has_zero_net_changes(self):
        report, tx, event = self.mixed_report(self.mixed_redemption("100", "0", "储蓄50,生活费50"))
        self.assertEqual(self.named_changes(report, event), {"生活费": "0", "储蓄": "0"})
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "100")

    def test_mixed_redemption_uses_exact_decimal_net_changes(self):
        report, tx, event = self.mixed_report(self.mixed_redemption("99.97", "0.03", "储蓄49.99,生活费49.98"))
        self.assertEqual(self.named_changes(report, event), {"生活费": "-1/50", "储蓄": "-1/100"})
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "4999/50")

    def test_redemption_purpose_change_needs_a_separate_virtual_transfer(self):
        self.configure(self.redemption(cash_purpose="储蓄"))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["report"])
        item = next(row for row in outcome["review_items"] if row["reason_code"] == "K_REDEMPTION_PURPOSE_MISMATCH")
        self.assertEqual(item["reason_kind"], "data_review")
        self.assertTrue(item["source"].startswith(str(self.ledger.resolve())))
        self.assertIn("虚拟调拨", item["detail"])

    def test_mixed_redemption_can_be_followed_by_an_explicit_virtual_transfer(self):
        source = self.mixed_redemption() + (
            '2026-09-04 * "reallocate"\n'
            '  Assets:Cash -5 CNY\n    fund: "生活费"\n'
            '  Assets:Cash 5 CNY\n    fund: "储蓄"\n')
        report, tx, event = self.mixed_report(source)
        self.assertEqual(self.named_changes(report, event), {"生活费": "5", "储蓄": "0"})
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "100")
        progress = {report["owner_names"][row["owner_id"]]: row["actual_g"] for row in report["purpose_progress"]["owners"]}
        self.assertEqual((progress["生活费"], progress["储蓄"]), ("-55", "55"))
        transfer = next(row for row in report["transactions"]["rows"] if row["narration"] == "reallocate")
        self.assertEqual({report["owner_names"][key]: value for key, value in transfer["net_allocations"].items()},
                         {"生活费": "-5", "储蓄": "5"})

    def test_future_mixed_redemption_keeps_current_book_purposes_and_cash(self):
        self.configure(self.mixed_redemption().replace('2026-09-03', '2026-10-03'))
        outcome = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        report = outcome["report"]
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "50")
        self.assertEqual(report["k_book_purposes"]["rows"][0]["booked_end"], "100")
        self.assertFalse(any(row["narration"] == "redeem" for row in report["transactions"]["rows"]))

    def test_book_redemption_with_separate_fee_proves_every_component_once(self):
        report = self.report(self.redemption("104", "-5", extra='  Expenses:Food 1 CNY\n'))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "154")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")
        self.assertEqual(self.income_total(), "5")
        expense = next(row for row in report["expense_targets"]["categories"] if row["name"] == "生活开销")
        self.assertEqual(expense["subtree_spent"], "1")
        transaction = next(row for row in report["transactions"]["rows"] if row["narration"] == "redeem")
        event = transaction["purpose_events"][0]["protocol"]
        self.assertEqual(event["protocol"], "same_purpose_book_redemption_costs_v1")
        self.assertEqual((event["principal_book_value"], event["income_value"], event["fee_value"],
                          event["signed_cash_units"]), ("100", "5", "1", "104"))
        fee = next(row for row in transaction["postings"] if row["account"] == "Expenses:Food")
        self.assertEqual(event["fee_refs"], [fee["ref"]])
        self.assertEqual(event["fee_components"][0]["signed_units"], "1")
        income = next(row for row in transaction["postings"] if row["account"] == "Income:Gain")
        self.assertEqual(income["benefit_source"]["fee_evidence_refs"], [fee["ref"]])
        self.assertEqual(income["benefit_source"]["fee_report_value"], "1")
        self.assertEqual(report["k_book_purposes"]["rows"][0]["booked_end"], "0")
        self.assertIn("赎回核对：实收 104 CNY＝账面本金 100 CNY＋损益 5 CNY－费用 1 CNY",
                      "\n".join(render_transaction(transaction, report["owner_names"])))

    def test_book_redemption_preserves_multiple_fee_legs_and_inferred_income(self):
        source = self.redemption("102", "-5", extra='  Expenses:Food 1 CNY\n  Expenses:Travel 2 CNY\n')
        report = self.report(source.replace('Income:Gain -5 CNY', 'Income:Gain'))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "152")
        self.assertEqual(self.income_total(), "5")
        transaction = next(row for row in report["transactions"]["rows"] if row["narration"] == "redeem")
        event = transaction["purpose_events"][0]["protocol"]
        self.assertEqual(event["fee_value"], "3")
        self.assertEqual(len(event["fee_refs"]), 2)
        self.assertEqual({row["account"] for row in event["fee_components"]}, {"Expenses:Food", "Expenses:Travel"})
        self.assertEqual(report["default_outflows"]["totals"][0]["net_outflow"], "0")

    def test_book_redemption_with_fees_without_income_does_not_create_an_income_leg(self):
        source = self.redemption("99", "-5", extra='  Expenses:Food 1 CNY\n')
        source = source.replace('  Income:Gain -5 CNY\n    budget: "投资"\n', '')
        report = self.report(source)
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "149")
        self.assertEqual(self.income_total(), "0")
        transaction = next(row for row in report["transactions"]["rows"] if row["narration"] == "redeem")
        event = transaction["purpose_events"][0]["protocol"]
        self.assertIsNone(event["income_ref"])
        self.assertEqual(event["income_value"], "0")
        self.assertFalse(any(row["account"].startswith("Income:") for row in transaction["postings"]))
        self.assertEqual(report["default_outflows"]["totals"][0]["net_outflow"], "0")

    def test_book_redemption_loss_and_fee_are_not_charged_twice(self):
        report = self.report(self.redemption("94", "5", extra='  Expenses:Food 1 CNY\n'))
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "144")
        self.assertEqual(self.income_total(), "-5")
        expense = next(row for row in report["expense_targets"]["categories"] if row["name"] == "生活开销")
        self.assertEqual(expense["subtree_spent"], "1")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")

    def test_fee_budget_category_does_not_choose_another_asset_purpose(self):
        source = self.redemption("104", "-5", k_purpose="储蓄", cash_purpose="储蓄", extra='  Expenses:Food 1 CNY\n')
        source = source.replace('  Assets:Book 100 CNY\n    fund: "生活费"',
                                '  Assets:Book 100 CNY\n    fund: "储蓄"')
        self.configure(source)
        outcome = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        report = outcome["report"]
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "50")
        transaction = next(row for row in report["transactions"]["rows"] if row["narration"] == "redeem")
        self.assertEqual(transaction["net_allocations"], {})
        cash = next(row for row in transaction["postings"] if row["account"] == "Assets:Cash")
        self.assertEqual(cash["h_purpose"]["slices"][0]["name"], "储蓄")
        self.assertEqual(cash["h_purpose"]["slices"][0]["signed_units"], "104")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")

    def test_fee_redemption_still_requires_explicit_cash_purpose(self):
        self.configure(self.redemption("104", "-5", cash_purpose=None, extra='  Expenses:Food 1 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIn("OWNERSHIP_UNREVIEWED", {row["reason_code"] for row in outcome["review_items"]})

    def test_fee_redemption_cannot_silently_change_the_principal_purpose(self):
        self.configure(self.redemption("104", "-5", cash_purpose="储蓄", extra='  Expenses:Food 1 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIn("K_REDEMPTION_PURPOSE_MISMATCH", {row["reason_code"] for row in outcome["review_items"]})

    def test_fee_refund_cannot_be_treated_as_a_deducted_redemption_cost(self):
        self.configure(self.redemption("106", "-5", extra='  Expenses:Food -1 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_fee_price_is_not_a_native_redemption_amount(self):
        self.configure(self.redemption("104", "-5", extra='  Expenses:Food 1 CNY @ 1 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_foreign_fee_is_not_folded_into_a_report_currency_redemption(self):
        self.configure(self.redemption("104", "-5", extra='  Expenses:Food 1 USD @ 1 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_loader_tolerance_does_not_prove_an_inexact_fee_equation(self):
        self.configure('option "inferred_tolerance_default" "CNY:1"\n' +
                       self.redemption("104", "-5.01", extra='  Expenses:Food 1 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_redemption_fee_cannot_hide_an_extra_liability_counterpart(self):
        self.configure(self.redemption("104", "-5", extra='  Expenses:Food 1 CNY\n  Liabilities:Card 0 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_multiple_income_legs_need_their_own_redemption_protocol(self):
        self.configure(self.redemption("105", "-5", extra='  Income:Bonus -1 CNY\n  Expenses:Food 1 CNY\n'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_future_fee_redemption_does_not_change_earlier_money_or_flows(self):
        source = self.redemption("104", "-5", extra='  Expenses:Food 1 CNY\n')
        initial, redemption = source.split('2026-09-03 * "redeem"', 1)
        self.configure(initial)
        before = audit_month_workflow(self.config, include_transactions=True)["report"]
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + '2026-10-03 * "redeem"' + redemption,
                               encoding="utf-8")
        result = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        after = result["report"]
        self.assertEqual(after["actual_spendable"]["actual_spendable"], before["actual_spendable"]["actual_spendable"])
        self.assertEqual(after["transactions"]["rows"], before["transactions"]["rows"])
        self.assertEqual(after["expense_targets"], before["expense_targets"] | {"ledger_snapshot": after["ledger_snapshot"]})
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        with booked_through(date(2026, 10, 31)):
            events = audit_k_events(self.ledger, date(2026, 9, 1), self.config.parent / document["accounts"],
                                    self.config.parent / document["ownership"])
        self.assertEqual(events["certified_events"], 2)
        self.assertEqual(events["events"][-1]["fee_value"], "1")

    def test_future_book_redemption_does_not_change_the_previous_month(self):
        self.configure(self.redemption().replace('2026-09-03', '2026-10-03'))
        before = audit_month_workflow(self.config)
        self.assertEqual(before["status"], "CERTIFIED_SCOPED")
        self.assertEqual(before["report"]["actual_spendable"]["actual_spendable"], "50")
        self.assertEqual(before["report"]["k_book_purposes"]["rows"][0]["booked_end"], "100")
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        with booked_through(date(2026, 10, 31)):
            after = audit_k_events(self.ledger, date(2026, 9, 1), self.config.parent / document["accounts"],
                                   self.config.parent / document["ownership"])
        self.assertEqual(after["certified_events"], 2)
        self.assertEqual(after["events"][-1]["income_value"], "5")

    def test_loader_tolerance_cannot_prove_an_inexact_book_redemption(self):
        self.configure('option "inferred_tolerance_default" "CNY:1"\n' + self.redemption("105", "-5.01"))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIn("K_EVENT_SHAPE_UNSUPPORTED", {row["reason_code"] for row in outcome["review_items"]})

    def test_voluntary_k_subscription_reports_the_actual_source_purpose_shortage(self):
        self.configure('2026-09-02 * "subscribe"\n'
                       '  Assets:Cash -100 CNY\n    fund: "储蓄"\n'
                       '  Assets:Book 100 CNY\n    fund: "储蓄"\n')
        with self.assertRaises(BudgetError) as caught:
            audit_month_workflow(self.config)
        self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")
        self.assertIn("资金用途：储蓄", caught.exception.message)
        self.assertIn("划拨前余额：0 CNY", caught.exception.message)
        self.assertIn("本次净划出：100 CNY", caught.exception.message)
        self.assertEqual(caught.exception.source, f"{self.ledger.resolve()}:16")

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

    def test_h_change_uses_joint_protocol_while_k_fee_keeps_its_distinct_semantics(self):
        for name, transactions in (
            ("change", '  Assets:Cash -20 CNY\n    fund: "生活费"\n'
                        '  Assets:Bank 10 CNY\n    fund: "生活费"\n  Expenses:Food 10 CNY\n'),
            ("K fee", '  Assets:Cash -21 CNY\n    fund: "生活费"\n'
                       '  Assets:Book 20 CNY\n    fund: "生活费"\n  Expenses:Food 1 CNY\n'),
        ):
            with self.subTest(name=name), TemporaryDirectory() as folder:
                original = self.base
                self.base = Path(folder)
                self.configure(f'2026-09-02 * "{name}"\n' + transactions)
                outcome = audit_month_workflow(self.config)
                if name == 'change':
                    self.assertEqual(outcome['status'], 'CERTIFIED_SCOPED', outcome['review_items'])
                    self.assertEqual(outcome['report']['default_outflows']['totals'][0]['expenses_paid'], '10')
                else:
                    self.assertEqual(outcome['status'], 'IMPLEMENTATION_BLOCKED')
                    self.assertIsNone(outcome['report'])
                    self.assertIn('K_EVENT_SHAPE_UNSUPPORTED', {row['reason_code'] for row in outcome['review_items']})
                self.base = original

    def test_unsupported_named_event_is_an_implementation_limit(self):
        self.assertEqual(review_item("NAMED_H_EVENT_UNSUPPORTED", "ledger.bean:5")["reason_kind"],
                         "implementation_limit")

"""Whole native purchase evidence, separate cash ownership and card responsibility."""

from copy import deepcopy
from datetime import date
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

import test_v6_native_flows as native_tests
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, frozen_financial, load_financial
from fixture_inputs import SetupDumper as _SetupDumper
from fixture_months import create_next_month, compile_next_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.default_outflows import audit_default_outflows
from beancount_budget_tool.v6.native_flows import native_card_purchase
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.transaction_details import render_transaction


CUTOVER = date(2026, 9, 1)
PURCHASE = ('2026-09-02 * "cash and card"\n'
            '  Expenses:Food 25 CNY\n  Expenses:Travel 15 CNY\n'
            '  Assets:Cash -10 CNY\n  Liabilities:Card -30 CNY\n')


class JointCardPurchaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def configure(self, transactions=PURCHASE, bank_owner="生活费", debt_owner="生活费"):
        native_tests.NativeFlowTests.configure(self, transactions, bank_owner)
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.declarations = (self.config.parent / config["obligations"]).resolve()
        self.document = yaml.safe_load(self.declarations.read_text(encoding="utf-8"))
        owners = {row["name"]: row["id"] for row in self.document["owners"]}
        debt_ids = {}
        purchases = 0
        for event in audit_simple_obligations(self.ledger, CUTOVER)["events"]:
            field = "counterpart_refs" if "counterpart_refs" in event else "counterpart_ref"
            purchase = event["shape"] == "consumer_purchase"
            if purchase:
                debt_ids[event["account"]] = "joint" if purchases == 0 else f"joint_{purchases}"
                purchases += 1
            owner = debt_owner[event["account"]] if isinstance(debt_owner, dict) else debt_owner
            self.document["events"].append({
                "liability_ref": event["liability_ref"], field: event[field],
                "kind": "consumer_purchase" if purchase else "payment", "obligation_id": debt_ids[event["account"]],
                **({"owner_id": owners[owner]} if purchase else {}),
                "reason": "虚拟账本完整消费组及还款已审"})
        self.save()

    def save(self):
        self.declarations.write_text(yaml.safe_dump(self.document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def audit(self):
        outcome = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        self.assertTrue(all(row["actual_g"] == "0" for row in outcome["report"]["purpose_progress"]["owners"]))
        return outcome["report"]

    def test_cash_plus_card_counts_expense_once_and_reserves_only_card_share(self):
        self.configure()
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "110")
        self.assertEqual(report["actual_spendable"]["booked_debts"][0]["remaining_native"], "30")
        expense = next(row for row in report["expense_targets"]["categories"] if row["name"] == "生活开销")
        self.assertEqual(expense["subtree_spent"], "40")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "10")
        row = report["transactions"]["rows"][0]
        debt = row["liabilities"][0]
        self.assertIsNone(debt["counterpart_ref"])
        self.assertEqual((len(debt["counterpart_refs"]), len(debt["expense_refs"]), len(debt["asset_refs"])), (3, 2, 1))
        self.assertEqual(row["purpose_events"][0]["h_refs"], debt["asset_refs"])
        cash = next(p for p in row["postings"] if p["account"] == "Assets:Cash")
        self.assertEqual(cash["h_purpose"]["origin"], "inferred")

    def test_multiple_cash_accounts_use_their_actual_purposes_not_card_owner(self):
        transactions = PURCHASE.replace('  Assets:Cash -10 CNY',
            '  Assets:Cash -6 CNY\n  Assets:Bank -4 CNY\n    fund: "储蓄"')
        self.configure(transactions, bank_owner="储蓄")
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "64")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "6")
        cashrefs = report["transactions"]["rows"][0]["liabilities"][0]["asset_refs"]
        self.assertEqual(len(cashrefs), 2)

    def test_several_expenses_can_be_fully_charged_to_one_card(self):
        self.configure(PURCHASE.replace('  Assets:Cash -10 CNY\n', '').replace('-30 CNY', '-40 CNY'))
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "110")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")
        self.assertEqual(report["transactions"]["rows"][0]["liabilities"][0]["asset_refs"], [])

    def test_partial_and_final_repayments_across_months_never_repeat_consumption(self):
        self.configure(PURCHASE + '2026-10-03 * "partial"\n'
            '  Assets:Cash -10 CNY\n    fund: "生活费"\n  Liabilities:Card 10 CNY\n'
            '2026-11-04 * "final"\n  Assets:Cash -20 CNY\n    fund: "生活费"\n'
            '  Liabilities:Card 20 CNY\n')
        reports = [self.audit()]
        for month in ("2026-10", "2026-11"):
            directory = self.base / month
            create_next_month(self.config, directory)
            worksheet = directory / "next-month.yaml"
            sheet = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
            sheet.update(targets_confirmed=True, completeness_confirmed=True,
                         completeness_reason="虚拟账本已完整列出")
            worksheet.write_text(yaml.dump(sheet, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")
            self.config = Path(compile_next_month(worksheet)["config"])
            reports.append(self.audit())
        self.assertEqual([r["actual_spendable"]["actual_spendable"] for r in reports], ["110"] * 3)
        self.assertEqual([sum((Fraction(c["direct_spent"]) for c in r["expense_targets"]["categories"]), Fraction(0))
                          for r in reports], [40, 0, 0])
        self.assertEqual([r["default_outflows"]["totals"][0]["expenses_paid"] for r in reports], ["10", "0", "0"])
        self.assertEqual([r["default_outflows"]["totals"][0]["debt_payments"] for r in reports], ["0", "10", "20"])

    def test_unreviewed_responsibility_is_data_review_with_complete_group_and_location(self):
        self.configure()
        self.document["events"] = []
        self.save()
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["report"])
        item = next(row for row in outcome["review_items"] if row["kind"] == "booked_obligation")
        self.assertEqual(item["reason_code"], "OBLIGATION_UNCLAIMED")
        self.assertEqual(len(item["counterpart_refs"]), 3)
        self.assertTrue(item["source"].startswith(str(self.ledger) + ":"))
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        standalone = audit_default_outflows(self.ledger, CUTOVER, "2026-09",
            self.config.parent / config["accounts"], self.config.parent / config["ownership"],
            self.config.parent / config["opening"])
        self.assertIsNone(standalone["totals"])
        self.assertEqual(standalone["reason_codes"], ["OBLIGATION_UNCLAIMED"])

    def test_whole_group_rejects_missing_duplicate_wrong_or_legacy_single_ref(self):
        self.configure()
        original = deepcopy(self.document)
        for mutation in ("missing", "duplicate", "wrong", "single"):
            with self.subTest(mutation=mutation):
                self.document = deepcopy(original)
                row = self.document["events"][0]
                if mutation == "missing":
                    row["counterpart_refs"].pop()
                elif mutation == "duplicate":
                    row["counterpart_refs"].append(row["counterpart_refs"][0])
                elif mutation == "wrong":
                    row["counterpart_refs"][0]["fact_sha256"] = "0" * 64
                else:
                    row["counterpart_ref"] = row.pop("counterpart_refs")[0]
                self.save()
                with self.assertRaises(BudgetError) as caught:
                    audit_simple_obligations(self.ledger, CUTOVER, self.declarations)
                self.assertEqual(caught.exception.code, "OBLIGATION_COUNTERPART_INVALID")
                self.assertTrue(caught.exception.source.startswith(str(self.declarations) + ":"))

    def test_ref_order_is_irrelevant_but_changed_transaction_expires_decision(self):
        self.configure()
        self.document["events"][0]["counterpart_refs"].reverse()
        self.save()
        self.audit()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace('Food 25', 'Food 24').replace('Travel 15', 'Travel 16'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_simple_obligations(self.ledger, CUTOVER, self.declarations)
        self.assertEqual(caught.exception.code, "OBLIGATION_REF_INVALID")

    def test_future_group_is_validated_without_replaying_it_in_an_old_month(self):
        self.configure(PURCHASE.replace('2026-09-02', '2026-10-02'))
        with booked_through(date(2026, 9, 30)), frozen_financial(self.ledger):
            old = audit_simple_obligations(self.ledger, CUTOVER, self.declarations)
            self.assertEqual((old["events"], old["obligations"]), ([], []))
        self.document["events"][0]["counterpart_refs"].pop()
        self.save()
        with booked_through(date(2026, 9, 30)), frozen_financial(self.ledger):
            with self.assertRaises(BudgetError) as caught:
                audit_simple_obligations(self.ledger, CUTOVER, self.declarations)
        self.assertEqual(caught.exception.code, "OBLIGATION_COUNTERPART_INVALID")

    def test_malformed_group_refs_are_located_configuration_errors(self):
        self.configure()
        original = deepcopy(self.document)
        for refs in ([], {}, [date(2026, 9, 2)], [{"bad": date(2026, 9, 2)}]):
            with self.subTest(refs=refs):
                self.document = deepcopy(original)
                self.document["events"][0]["counterpart_refs"] = refs
                self.save()
                with self.assertRaises(BudgetError) as caught:
                    audit_simple_obligations(self.ledger, CUTOVER, self.declarations)
                self.assertEqual(caught.exception.code, "OBLIGATION_CLAIM_INVALID")
                self.assertTrue(caught.exception.source.startswith(str(self.declarations) + ":"))

    def test_booked_rounding_tolerance_cannot_hide_a_purchase_residual(self):
        self.ledger = self.base / "rounding.bean"
        self.ledger.write_text(native_tests.HEADER + PURCHASE.replace('25 CNY', '25.00 CNY')
            .replace('15 CNY', '15.00 CNY').replace('-10 CNY', '-10.001 CNY'), encoding="utf-8")
        events = audit_simple_obligations(self.ledger, CUTOVER)["events"]
        self.assertEqual(events[0]["reason_codes"], ["OBLIGATION_EVENT_UNSUPPORTED"])

    def test_refunds_change_and_prices_stay_implementation_limits(self):
        for mutation, body in (
            ("refund", PURCHASE.replace('Food 25', 'Food -5').replace('Travel 15', 'Travel 45')),
            ("change", PURCHASE.replace('Assets:Cash -10', 'Assets:Cash 10').replace('-30 CNY', '-50 CNY')),
            ("prices", PURCHASE.replace('CNY\n', 'CNY @ 1 USD\n')),
            ("cost", PURCHASE.replace('Food 25 CNY', 'Food 25 CNY {1 CNY}')),
        ):
            with self.subTest(mutation=mutation):
                self.ledger = self.base / "shape.bean"
                self.ledger.write_text(native_tests.HEADER + '2026-08-01 open Liabilities:Other\n' + body, encoding="utf-8")
                fin = load_financial(self.ledger)
                entry = next(e for e in fin.entries if getattr(e, "narration", None) == "cash and card")
                self.assertIsNone(native_card_purchase(fin, entry))
                audit = audit_simple_obligations(self.ledger, CUTOVER)
                self.assertTrue(all(e["reason_codes"] == ["OBLIGATION_EVENT_UNSUPPORTED"] for e in audit["events"]))

    def multi_purchase(self, body=PURCHASE):
        return ('2026-08-01 open Liabilities:Other\n' + body.replace('Liabilities:Card -30 CNY',
                'Liabilities:Card -20 CNY\n  Liabilities:Other -10 CNY'))

    def test_two_cards_and_cash_preserve_each_debt_and_count_cash_once(self):
        self.configure(self.multi_purchase())
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "110")
        self.assertEqual({row["account"]: row["remaining_native"] for row in report["actual_spendable"]["booked_debts"]},
                         {"Liabilities:Card": "20", "Liabilities:Other": "10"})
        expense = next(row for row in report["expense_targets"]["categories"] if row["name"] == "生活开销")
        self.assertEqual(expense["subtree_spent"], "40")
        flows = report["default_outflows"]
        self.assertEqual(flows["totals"][0]["expenses_paid"], "10")
        self.assertEqual(len(flows["events"]), 1)
        self.assertEqual(set(flows["events"][0]["obligation_ids"]), {"joint", "joint_1"})
        tx = report["transactions"]["rows"][0]
        self.assertEqual(len(tx["liabilities"]), 2)
        self.assertEqual(len(tx["purpose_events"]), 1)
        for row in tx["liabilities"]:
            self.assertEqual(row["proof"], "native_joint_card_purchase_v1")
            self.assertEqual(len(row["liability_refs"]), 2)
            self.assertEqual((row["expense_total"], row["cash_paid"], row["debt_total"]), ("40", "10", "30"))
            self.assertEqual(len(row["counterpart_refs"]), 4)
        detail = "\n".join(render_transaction(tx, report["owner_names"]))
        self.assertEqual(detail.count("多卡消费核对"), 1)
        self.assertIn("消费 40＝现金实付 10＋新增欠款 30 CNY", detail)
        self.assertIn("本条负债新增欠款：20 CNY", detail)
        self.assertIn("本条负债新增欠款：10 CNY", detail)

    def test_two_cards_without_cash_do_not_fabricate_asset_payments(self):
        body = self.multi_purchase().replace('  Assets:Cash -10 CNY\n', '').replace('Card -20 CNY', 'Card -30 CNY')
        self.configure(body)
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "110")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")
        tx = report["transactions"]["rows"][0]
        self.assertEqual(tx["purpose_events"], [])
        self.assertTrue(all(row["cash_paid"] == "0" and row["asset_refs"] == [] for row in tx["liabilities"]))

    def test_three_cards_multiple_expenses_and_cash_are_not_a_two_card_special_case(self):
        body = ('2026-08-01 open Liabilities:Third\n' + self.multi_purchase()).replace(
            'Card -20 CNY', 'Card -15 CNY\n  Liabilities:Third -5 CNY')
        self.configure(body)
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "110")
        self.assertEqual(len(report["actual_spendable"]["booked_debts"]), 3)
        self.assertEqual(len(report["default_outflows"]["events"]), 1)
        self.assertEqual(len(report["default_outflows"]["events"][0]["obligation_ids"]), 3)

    def test_multiple_cards_and_cash_accounts_keep_payment_purposes_independent(self):
        body = self.multi_purchase().replace('  Assets:Cash -10 CNY',
            '  Assets:Cash -6 CNY\n  Assets:Bank -4 CNY\n    fund: "储蓄"')
        self.configure(body, bank_owner="储蓄")
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "64")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "6")
        self.assertEqual(len(report["transactions"]["rows"][0]["purpose_events"][0]["h_refs"]), 2)

    def test_joint_cards_keep_distinct_responsibility_purposes_and_coverage(self):
        self.configure(self.multi_purchase(), bank_owner="储蓄",
                       debt_owner={"Liabilities:Card": "生活费", "Liabilities:Other": "储蓄"})
        pending = audit_month_workflow(self.config)
        self.assertEqual(pending["status"], "NEEDS_REVIEW")
        self.assertTrue(any(row["reason_code"] == "DEBT_COVERAGE_PENDING" for row in pending["review_items"]))
        debts = audit_simple_obligations(self.ledger, CUTOVER, self.declarations)["obligations"]
        protected = next(row for row in debts if row["account"] == "Liabilities:Other")
        coverage = self.config.parent / "coverage.yaml"
        coverage.write_text(yaml.safe_dump({"schema": 6, "cutover": "2026-09-01", "as_of": "2026-09-30",
            "report_currency": "CNY", "decisions": [{"obligation_id": protected["obligation_id"],
                "purchase_ref": protected["purchase_ref"], "reason": "虚拟储蓄责任使用已审储蓄现金",
                "resources": [{"account": "Assets:Bank", "commodity": "CNY", "lot": "CNY/no-cost",
                               "owner_id": protected["owner_id"], "amount": "10"}]}]}, allow_unicode=True), encoding="utf-8")
        doc = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        doc["debt_coverage"] = coverage.name
        self.config.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "70")
        self.assertEqual(report["actual_spendable"]["coverage_allocations"][0]["amount"], "10")
        names = report["owner_names"]
        self.assertEqual({row["account"]: names[row["owner_id"]] for row in report["actual_spendable"]["booked_debts"]},
                         {"Liabilities:Card": "生活费", "Liabilities:Other": "储蓄"})
        available = {names[row["owner_id"]]: row["units"] for row in report["actual_spendable"]["purpose_available"]}
        self.assertEqual(available["储蓄"], "40")

    def test_fractional_cash_and_each_card_amount_are_kept_exact(self):
        body = self.multi_purchase().replace('Cash -10 CNY', 'Cash -10.01 CNY').replace(
            'Card -20 CNY', 'Card -20.02 CNY').replace('Other -10 CNY', 'Other -9.97 CNY')
        self.configure(body)
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "110")
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "1001/100")
        row = report["transactions"]["rows"][0]["liabilities"][0]
        self.assertEqual((row["cash_paid"], row["debt_total"]), ("1001/100", "2999/100"))

    def test_positive_zero_foreign_or_mixed_domain_card_legs_remain_unsupported(self):
        base = self.multi_purchase()
        bodies = [
            base.replace('Other -10 CNY', 'Other 10 CNY').replace('Card -20 CNY', 'Card -40 CNY'),
            base.replace('Other -10 CNY', 'Other 0 CNY').replace('Card -20 CNY', 'Card -30 CNY'),
            base.replace('Other -10 CNY', 'Other -10 USD @ 1 CNY'),
            base.replace('Food 25 CNY', 'Food 26 CNY') + '  Income:Bonus -1 CNY\n',
        ]
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                ledger = self.base / f"unsupported-{index}.bean"
                ledger.write_text(native_tests.HEADER + body, encoding="utf-8")
                events = audit_simple_obligations(ledger, CUTOVER)["events"]
                self.assertTrue(all(row["status"] == "UNSUPPORTED" for row in events))

    def test_partial_group_approval_cannot_certify_cash_using_the_last_card(self):
        self.configure(self.multi_purchase())
        original = deepcopy(self.document)
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        for removed in (0, 1):
            with self.subTest(removed=removed):
                self.document = deepcopy(original)
                missing = self.document["events"].pop(removed)
                self.save()
                outcome = audit_month_workflow(self.config)
                self.assertEqual(outcome["status"], "NEEDS_REVIEW")
                self.assertIsNone(outcome["report"])
                item = next(row for row in outcome["review_items"] if row["kind"] == "booked_obligation")
                self.assertEqual(item["ref"], missing["liability_ref"])
                self.assertEqual(item["reason_code"], "OBLIGATION_UNCLAIMED")
                self.assertEqual(len(item["liability_refs"]), 2)
                self.assertTrue(item["source"].startswith(str(self.ledger) + ":"))
                flows = audit_default_outflows(self.ledger, CUTOVER, "2026-09", self.config.parent / config["accounts"],
                    self.config.parent / config["ownership"], self.config.parent / config["opening"], self.declarations)
                self.assertEqual(flows["status"], "NEEDS_REVIEW")
                self.assertIsNone(flows["totals"])
                self.assertEqual(flows["events"], [])

    def test_each_card_requires_its_own_id_and_complete_counterpart_group(self):
        self.configure(self.multi_purchase())
        original = deepcopy(self.document)
        for mutation in ("duplicate id", "missing other card", "wrong source"):
            with self.subTest(mutation=mutation):
                self.document = deepcopy(original)
                row = self.document["events"][1]
                if mutation == "duplicate id":
                    row["obligation_id"] = self.document["events"][0]["obligation_id"]
                    expected = "DUPLICATE_OBLIGATION_CLAIM"
                elif mutation == "missing other card":
                    row["counterpart_refs"].pop()
                    expected = "OBLIGATION_COUNTERPART_INVALID"
                else:
                    row["counterpart_refs"][0]["fact_sha256"] = "0" * 64
                    expected = "OBLIGATION_COUNTERPART_INVALID"
                self.save()
                with self.assertRaises(BudgetError) as caught:
                    audit_simple_obligations(self.ledger, CUTOVER, self.declarations)
                self.assertEqual(caught.exception.code, expected)
                self.assertTrue(caught.exception.source.startswith(str(self.declarations) + ":"))

    def test_partial_and_full_payments_on_different_cards_across_three_months(self):
        body = self.multi_purchase() + ('2026-10-03 * "first card partial"\n'
            '  Assets:Cash -10 CNY\n    fund: "生活费"\n  Liabilities:Card 10 CNY\n'
            '2026-10-04 * "other card full"\n'
            '  Assets:Cash -10 CNY\n    fund: "生活费"\n  Liabilities:Other 10 CNY\n'
            '2026-11-05 * "first card final"\n'
            '  Assets:Cash -10 CNY\n    fund: "生活费"\n  Liabilities:Card 10 CNY\n')
        self.configure(body)
        reports = [self.audit()]
        for month in ("2026-10", "2026-11"):
            directory = self.base / month
            create_next_month(self.config, directory)
            worksheet = directory / "next-month.yaml"
            sheet = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
            sheet.update(targets_confirmed=True, completeness_confirmed=True, completeness_reason="虚拟多卡账本完整")
            worksheet.write_text(yaml.dump(sheet, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")
            self.config = Path(compile_next_month(worksheet)["config"])
            reports.append(self.audit())
        self.assertEqual([r["actual_spendable"]["actual_spendable"] for r in reports], ["110"] * 3)
        self.assertEqual([r["default_outflows"]["totals"][0]["debt_payments"] for r in reports], ["0", "20", "10"])
        self.assertEqual([len(r["actual_spendable"]["booked_debts"]) for r in reports], [2, 1, 0])
        self.assertEqual([sum((Fraction(row["direct_spent"]) for row in r["expense_targets"]["categories"]), Fraction(0))
                          for r in reports], [40, 0, 0])

    def test_future_multi_card_group_is_verified_but_never_applied_to_old_month(self):
        self.configure(self.multi_purchase().replace('2026-09-02', '2026-10-02'))
        report = self.audit()
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "150")
        self.assertEqual(report["actual_spendable"]["booked_debts"], [])
        self.document["events"][1]["counterpart_refs"].pop()
        self.save()
        with self.assertRaises(BudgetError) as caught:
            audit_month_workflow(self.config)
        self.assertEqual(caught.exception.code, "OBLIGATION_COUNTERPART_INVALID")

    def test_changed_second_card_amount_expires_the_whole_purchase_claim(self):
        self.configure(self.multi_purchase())
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace('Card -20 CNY', 'Card -21 CNY')
                               .replace('Other -10 CNY', 'Other -9 CNY'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_simple_obligations(self.ledger, CUTOVER, self.declarations)
        self.assertEqual(caught.exception.code, "OBLIGATION_REF_INVALID")

    def test_k_cash_leg_cannot_supply_h_default_ownership(self):
        self.configure(PURCHASE.replace('Assets:Cash -10', 'Assets:Book -10'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIsNone(outcome["report"])


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
from beancount_budget_tool.tools.v6_init import _SetupDumper
from beancount_budget_tool.tools.v6_next_month import create_next_month, compile_next_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.default_outflows import audit_default_outflows
from beancount_budget_tool.v6.native_flows import native_card_purchase
from beancount_budget_tool.v6.obligations import audit_simple_obligations


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
        for event in audit_simple_obligations(self.ledger, CUTOVER)["events"]:
            field = "counterpart_refs" if "counterpart_refs" in event else "counterpart_ref"
            purchase = event["shape"] == "consumer_purchase"
            self.document["events"].append({
                "liability_ref": event["liability_ref"], field: event[field],
                "kind": "consumer_purchase" if purchase else "payment", "obligation_id": "joint",
                **({"owner_id": owners[debt_owner]} if purchase else {}),
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

    def test_refunds_change_multiple_cards_and_prices_stay_implementation_limits(self):
        for mutation, body in (
            ("refund", PURCHASE.replace('Food 25', 'Food -5').replace('Travel 15', 'Travel 45')),
            ("change", PURCHASE.replace('Assets:Cash -10', 'Assets:Cash 10').replace('-30 CNY', '-50 CNY')),
            ("multiple cards", PURCHASE.replace('Liabilities:Card -30 CNY',
                'Liabilities:Card -20 CNY\n  Liabilities:Other -10 CNY')),
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

    def test_k_cash_leg_cannot_supply_h_default_ownership(self):
        self.configure(PURCHASE.replace('Assets:Cash -10', 'Assets:Book -10'))
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIsNone(outcome["report"])


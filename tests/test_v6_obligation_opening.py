"""Cutover residuals carry identity without replaying historical budget flows."""

from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
from beancount.core import data
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.tools.v6_review_source_refs import diagnose
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.obligations import audit_simple_obligations


CUTOVER = date(2026, 9, 1)


class ObligationOpeningTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.ledger = self.base / "main.bean"

    def save(self, key, value):
        path = self.base / (key + ".yaml")
        path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def ref(self, narration, account):
        fin = load_financial(self.ledger)
        facts = FactIndex(fin)
        entry = next(row for row in fin.entries if isinstance(row, data.Transaction) and row.narration == narration)
        return facts.for_posting(next(posting for posting in entry.postings if posting.account == account)).as_dict()

    def fixture(self, *, mixed=False, payments=True, owner="living", equity=False, foreign=False):
        self.clearing_account = "Liabilities:Card" if mixed else "Liabilities:Clearing"
        if equity or foreign:
            history = ('2026-08-20 * "old bank opening"\n'
                       '  Assets:Bank 1000 CNY\n  Equity:Opening -1000 CNY\n'
                       '2026-08-25 * "old card origin"\n'
                       f'  Liabilities:Card -300 {"USD" if foreign else "CNY"}\n'
                       f'  Equity:Opening 300 {"USD" if foreign else "CNY"}\n')
            payments = False
        else:
            history = ('2026-08-20 * "old bank opening"\n'
                       '  Assets:Bank 1500 CNY\n  Equity:Opening -1500 CNY\n'
                       '2026-08-25 * "old card origin"\n'
                       '  Expenses:Food 500 CNY\n  Liabilities:Card -500 CNY\n'
                       '2026-08-26 * "old partial payment"\n'
                       '  Liabilities:Card 200 CNY\n  Assets:Bank -200 CNY\n'
                       '2026-08-27 * "old employer advance"\n'
                       f'  {self.clearing_account} 500 CNY\n  Assets:Bank -500 CNY\n'
                       '2026-08-28 * "old partial reimbursement"\n'
                       f'  {self.clearing_account} -200 CNY\n  Assets:Bank 200 CNY\n')
        current = ('2026-09-10 * "card payment"\n'
                   '  Liabilities:Card 100 CNY\n  Assets:Bank -100 CNY\n    fund: "生活费"\n'
                   '2026-09-20 * "employer reimbursement"\n'
                   f'  {self.clearing_account} -100 CNY\n  Assets:Bank 100 CNY\n    fund: "生活费"\n') if payments else ""
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n2026-08-01 open Liabilities:Card\n'
            '2026-08-01 open Liabilities:Clearing\n2026-08-01 open Expenses:Food\n'
            '2026-08-01 open Equity:Opening\n' + history + current, encoding="utf-8")
        self.snapshot = load_financial(self.ledger).digest
        self.owners = [{"id": ident, "name": name, "aliases": []}
                       for ident, name in (("living", "生活费"), ("free", "自由资金"), ("travel", "旅游"))]
        self.save("accounts", {"schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
                               "accounts": [{"account": "Assets:Bank", "semantics": "H",
                                             "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                                             "reason": "reviewed virtual bank"}]})
        self.save("opening", {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                              "h_holdings": [{"account": "Assets:Bank", "commodity": "CNY", "lot": "CNY/no-cost",
                                              "owner": "living", "units": "1000", "reason": "cutover cash"}],
                              "k_openings": []})
        self.save("ownership", {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                                "loader_version": BEANCOUNT_VERSION, "owners": self.owners,
                                "defaults": {"income_owner_id": "free", "expense_owner_id": "living"}, "claims": []})
        candidates = audit_simple_obligations(self.ledger, CUTOVER)["events"]
        self.document = {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                         "loader_version": BEANCOUNT_VERSION, "owners": self.owners,
                         "opening": [{"liability_ref": self.ref("old card origin", "Liabilities:Card"),
                                      "kind": "consumer_debt", "obligation_id": "old-card", "owner_id": owner,
                                      "remaining_native": "300", "reason": "500 originally, 200 paid before cutover"}],
                         "events": [{"liability_ref": row["liability_ref"], "counterpart_ref": row["counterpart_ref"],
                                     "kind": "payment" if row["shape"] == "asset_outflow" else "advance_reimbursement",
                                     **({"obligation_id": "old-card"} if row["shape"] == "asset_outflow" else
                                        {"clearing_id": "old-advance"}), "reason": "settle reviewed cutover residual"}
                                    for row in candidates]}
        if not equity and not foreign:
            self.document["opening"].append({"liability_ref": self.ref("old employer advance", self.clearing_account),
                                              "kind": "advance_receivable", "clearing_id": "old-advance",
                                              "remaining_native": "300", "reason": "500 originally, 200 reimbursed before cutover"})
        self.save("obligations", self.document)

    def audit(self, as_of=date(2026, 9, 30), coverage=None):
        scope = self.save("completeness", {"schema": 6, "cutover": CUTOVER.isoformat(), "as_of": as_of.isoformat(),
                                           "unbooked_obligations": "none", "inflight_payments": "none",
                                           "reason": "complete synthetic date"})
        return audit_actual_snapshot(self.ledger, CUTOVER, as_of,
                                     *(self.base / (key + ".yaml") for key in ("accounts", "opening", "ownership", "obligations")),
                                     scope, coverage)

    def obligations(self):
        return audit_simple_obligations(self.ledger, CUTOVER, self.save("obligations", self.document))

    def config(self):
        self.audit()
        self.save("classifications", {"schema": 6, "cutover": CUTOVER.isoformat(), "loader_version": BEANCOUNT_VERSION,
                                      "categories": [{"id": "food", "name": "餐饮", "parent": None, "aliases": []}],
                                      "classifications": []})
        self.save("expense_plans", {"schema": 6, "report_currency": "CNY", "spendable_cap_category_id": "food",
                                    "defaults": {"expense_subtree_targets": {"food": "2000"}}})
        self.save("purpose_plans", {"schema": 6, "report_currency": "CNY", "defaults": {
            "allocation_targets": {row["id"]: "not_assessed" for row in self.owners}}})
        for key in ("h_transfers", "h_fx", "k_events"):
            self.save(key, {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                            "loader_version": BEANCOUNT_VERSION, "events": []})
        self.save("purpose_pools", {"schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY", "pools": []})
        return self.save("month", {"schema": 6, "ledger": "main.bean", "cutover": CUTOVER.isoformat(),
                                   "month": "2026-09", "report_currency": "CNY",
                                   **{key: key + ".yaml" for key in (
                                       "accounts", "opening", "ownership", "obligations", "completeness", "classifications",
                                       "expense_plans", "purpose_plans", "h_transfers", "h_fx", "k_events", "purpose_pools")}})

    def test_opening_debt_and_advance_seed_only_residuals_then_settle(self):
        self.fixture()
        before = self.audit(CUTOVER)
        self.assertEqual((before["status"], before["actual_spendable"]), ("CERTIFIED", "700"))
        self.assertEqual(before["booked_debts"][0]["remaining_native"], "300")
        self.assertEqual(before["clearing_receivables"][0]["remaining_native"], "300")
        after = self.audit()
        self.assertEqual((after["status"], after["actual_spendable"]), ("CERTIFIED", "800"))
        self.assertEqual(after["booked_debts"][0]["remaining_native"], "200")
        self.assertEqual(after["clearing_receivables"][0]["remaining_native"], "200")
        self.assertEqual(len(after["opening_responsibilities"]), 2)
        self.assertEqual(after["opening_responsibilities"][0]["remaining_native"], "300")

    def test_missing_opening_is_reviewable_even_with_reviewed_later_payment(self):
        self.fixture()
        self.document.pop("opening")
        debt = self.obligations()
        self.assertEqual((debt["review_state"], debt["pending_events"]), ("pending", 2))
        self.assertEqual(len(debt["opening_pending"]), 2)
        candidate = debt["opening_pending"][0]["source_candidates"][0]
        self.assertEqual(candidate["liability_ref"], self.ref("old card origin", "Liabilities:Card"))
        self.assertEqual(debt["events"][0]["reason_codes"], ["OBLIGATION_OPENING_REQUIRED"])
        config = self.config()
        outcome = audit_month_workflow(config)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["report"])
        self.assertTrue(all(row["reason_kind"] == "data_review" for row in outcome["review_items"]))
        self.assertTrue(any(row["reason_code"] == "LIABILITY_OPENING_PENDING" and row["source"].startswith(
            str(self.ledger.resolve()) + ":") for row in outcome["review_items"]))

    def test_declared_net_zero_does_not_hide_opening_personal_debt(self):
        self.fixture(mixed=True, payments=False)
        report = self.audit()
        self.assertEqual(report["unresolved_liabilities"], [])
        self.assertEqual(report["actual_spendable"], "700")
        self.assertEqual(report["uncovered_responsibility"], "300")
        self.document["opening"].pop()
        with self.assertRaises(BudgetError) as caught:
            self.obligations()
        self.assertEqual(caught.exception.code, "OBLIGATION_OPENING_MISMATCH")

    def test_wrong_total_has_yaml_position_and_actual_amounts(self):
        self.fixture()
        self.document["opening"][0]["remaining_native"] = "299"
        with self.assertRaises(BudgetError) as caught:
            self.obligations()
        self.assertEqual(caught.exception.code, "OBLIGATION_OPENING_MISMATCH")
        self.assertIn("-299", caught.exception.message)
        self.assertIn("-300", caught.exception.message)
        self.assertTrue(caught.exception.source.startswith(str((self.base / "obligations.yaml").resolve()) + ":"))

    def test_original_source_cannot_be_claimed_twice_or_exceeded(self):
        self.fixture()
        duplicate = deepcopy(self.document["opening"][0])
        duplicate["obligation_id"] = "other-card"
        self.document["opening"].append(duplicate)
        with self.assertRaises(BudgetError) as caught:
            self.obligations()
        self.assertEqual(caught.exception.code, "DUPLICATE_OBLIGATION_OPENING")
        self.document["opening"].pop()
        self.document["opening"][0]["remaining_native"] = "501"
        with self.assertRaises(BudgetError) as caught:
            self.obligations()
        self.assertEqual(caught.exception.code, "OBLIGATION_OPENING_EXCESS")

    def test_opening_ref_must_exist_before_cutover_and_match_exactly(self):
        self.fixture()
        original = deepcopy(self.document["opening"][0]["liability_ref"])
        for ref in (self.ref("card payment", "Liabilities:Card"),
                    {**original, "fact_sha256": "0" * 64}, {**original, "occurrence": True}):
            with self.subTest(ref=ref):
                self.document["opening"][0]["liability_ref"] = ref
                with self.assertRaises(BudgetError) as caught:
                    self.obligations()
                self.assertEqual(caught.exception.code, "OBLIGATION_OPENING_REF_INVALID")

    def test_unknown_owner_invalid_amount_or_direction_rejected(self):
        self.fixture()
        original = deepcopy(self.document)
        for field, value, code in (("owner_id", "unknown", "OBLIGATION_OWNER_INVALID"),
                                    ("remaining_native", 300, "OBLIGATION_OPENING_AMOUNT_INVALID"),
                                    ("remaining_native", "0", "OBLIGATION_OPENING_AMOUNT_INVALID"),
                                    ("remaining_native", "-300", "OBLIGATION_OPENING_AMOUNT_INVALID"),
                                    ("kind", "advance_receivable", "OBLIGATION_OPENING_KIND_INVALID")):
            with self.subTest(field=field, value=value):
                self.document = deepcopy(original)
                self.document["opening"][0][field] = value
                with self.assertRaises(BudgetError) as caught:
                    self.obligations()
                self.assertEqual(caught.exception.code, code)

    def test_equity_carry_in_is_explicitly_reviewed_as_consumer_debt(self):
        self.fixture(equity=True)
        self.assertEqual(self.audit()["actual_spendable"], "700")

    def test_multiple_opening_ids_on_one_account_reconcile_jointly(self):
        self.fixture(payments=False)
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
                               '2026-08-29 * "second old purchase"\n'
                               '  Expenses:Food 100 CNY\n  Liabilities:Card -100 CNY\n', encoding="utf-8")
        self.document["opening"].append({"liability_ref": self.ref("second old purchase", "Liabilities:Card"),
                                         "kind": "consumer_debt", "obligation_id": "second-card", "owner_id": "living",
                                         "remaining_native": "100", "reason": "second outstanding purchase"})
        self.obligations()
        report = self.audit()
        self.assertEqual((report["status"], report["actual_spendable"]), ("CERTIFIED", "600"))
        self.assertEqual(len(report["booked_debts"]), 2)

    def test_full_settlement_and_later_reimbursement_preserve_identity(self):
        self.fixture()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
                               '2026-09-25 * "last card payment"\n'
                               '  Liabilities:Card 200 CNY\n  Assets:Bank -200 CNY\n    fund: "生活费"\n'
                               '2026-10-05 * "last employer reimbursement"\n'
                               '  Liabilities:Clearing -200 CNY\n  Assets:Bank 200 CNY\n    fund: "生活费"\n', encoding="utf-8")
        candidates = audit_simple_obligations(self.ledger, CUTOVER)["events"][-2:]
        for row in candidates:
            payment = row["shape"] == "asset_outflow"
            self.document["events"].append({"liability_ref": row["liability_ref"],
                                             "counterpart_ref": row["counterpart_ref"],
                                             "kind": "payment" if payment else "advance_reimbursement",
                                             **({"obligation_id": "old-card"} if payment else {"clearing_id": "old-advance"}),
                                             "reason": "final settlement of carried identity"})
        self.obligations()
        september = self.audit()
        self.assertEqual(september["actual_spendable"], "800")
        self.assertEqual(september["booked_debts"], [])
        self.assertEqual(september["clearing_receivables"][0]["remaining_native"], "200")
        october = self.audit(date(2026, 10, 31))
        self.assertEqual(october["actual_spendable"], "1000")
        self.assertEqual(october["clearing_receivables"], [])
        self.assertEqual(audit_month_workflow(self.config())["status"], "CERTIFIED_SCOPED")

    def test_borrowing_origin_requires_a_separate_nonconsumer_protocol(self):
        self.fixture(payments=False)
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace('Expenses:Food 500 CNY', 'Assets:Bank 500 CNY'),
                               encoding="utf-8")
        self.document["opening"][0]["liability_ref"] = self.ref("old card origin", "Liabilities:Card")
        with self.assertRaises(BudgetError) as caught:
            self.obligations()
        self.assertEqual(caught.exception.code, "OBLIGATION_OPENING_SOURCE_UNSUPPORTED")

    def test_new_purchase_cannot_reuse_an_opening_obligation_id(self):
        self.fixture()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
                               '2026-09-22 * "new purchase"\n'
                               '  Expenses:Food 10 CNY\n  Liabilities:Card -10 CNY\n', encoding="utf-8")
        row = audit_simple_obligations(self.ledger, CUTOVER)["events"][-1]
        self.document["events"].append({"liability_ref": row["liability_ref"], "counterpart_ref": row["counterpart_ref"],
                                         "kind": "consumer_purchase", "obligation_id": "old-card", "owner_id": "living",
                                         "reason": "invalid reuse of carried identity"})
        with self.assertRaises(BudgetError) as caught:
            self.obligations()
        self.assertEqual(caught.exception.code, "DUPLICATE_OBLIGATION_CLAIM")

    def test_foreign_opening_does_not_bypass_report_date_valuation(self):
        self.fixture(foreign=True)
        report = self.audit()
        self.assertEqual(report["reason_codes"], ["SETTLEMENT_VALUE_MISSING"])
        self.assertIsNone(report["actual_spendable"])
        self.assertEqual(report["booked_obligation_state"], "complete")

    def test_protected_opening_debt_uses_existing_explicit_coverage_protocol(self):
        self.fixture(owner="travel", payments=False)
        pending = self.audit()
        self.assertEqual(pending["reason_codes"], ["DEBT_COVERAGE_PENDING"])
        debt = pending["booked_debts"][0]
        coverage = self.save("coverage", {"schema": 6, "cutover": CUTOVER.isoformat(), "as_of": "2026-09-30",
                                          "report_currency": "CNY", "decisions": [{
                                              "obligation_id": "old-card", "purchase_ref": debt["purchase_ref"],
                                              "resources": [{"account": "Assets:Bank", "commodity": "CNY",
                                                             "lot": "CNY/no-cost", "owner_id": "living", "amount": "300"}],
                                              "reason": "reviewed daily cash reservation for old travel debt"}]})
        report = self.audit(coverage=coverage)
        self.assertEqual(report["actual_spendable"], "700")
        self.assertEqual(report["uncovered_responsibility"], "0")

    def test_old_origins_not_counted_as_current_month_expense_or_allocation(self):
        self.fixture()
        config = self.config()
        outcome = audit_month_workflow(config)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome)
        report = outcome["report"]
        self.assertEqual(report["max_spendable"]["max_spendable"], "800")
        self.assertEqual(report["expense_targets"]["categories"][0]["subtree_spent"], "0")
        self.assertTrue(all(row["actual_g"] == "0" for row in report["purpose_progress"]["owners"]))
        self.assertEqual(render_workflow_text(outcome).count("（期初结转）"), 2)
        refs = next(row for row in diagnose(config)["sidecars"] if row["field"] == "obligations")
        self.assertEqual(refs["references"], 6)
        self.assertEqual(refs["stale_references"], [])


if __name__ == "__main__":
    unittest.main()

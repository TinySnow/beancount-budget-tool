"""Reviewed advances reduce cash once and stay separate from personal debt."""

from datetime import date
import calendar
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.obligations import audit_simple_obligations


CUTOVER = date(2026, 9, 1)


class ClearingSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.ledger = self.base / "main.bean"

    def save(self, key, value):
        path = self.base / (key + ".yaml")
        path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def fixture(self, *, reimbursements=(("2026-09-20", 200), ("2026-10-05", 300)),
                protected=False, foreign=False, card_debt=0, second_advance=False):
        asset = "Assets:Foreign" if foreign else "Assets:Travel" if protected else "Assets:Bank"
        currency, advance = ("USD", 50) if foreign else ("CNY", 500)
        owner, name = ("travel", "旅游") if protected else ("living", "生活费")
        openings = [("Assets:Bank", "CNY", "living", "1000")]
        if asset != "Assets:Bank":
            openings.append((asset, currency, owner, "100" if foreign else "600"))
        source = ('option "operating_currency" "CNY"\n'
                  '2026-08-01 open Liabilities:Clearing\n'
                  '2026-08-01 open Equity:Opening\n'
                  '2026-08-01 open Expenses:Daily\n')
        source += "".join(f"2026-08-01 open {account}\n" for account, _, _, _ in openings)
        for account, commodity, _, amount in openings:
            source += (f'2026-08-31 * "opening {account}"\n'
                       f'  {account} {amount} {commodity}\n'
                       f'  Equity:Opening -{amount} {commodity}\n')
        source += (f'2026-09-08 * "pay for employer"\n'
                   f'  Liabilities:Clearing {advance} {currency}\n'
                   f'  {asset} -{advance} {currency}\n    fund: "{name}"\n')
        if card_debt:
            source += (f'2026-09-09 * "personal card purchase"\n'
                       f'  Expenses:Daily {card_debt} CNY\n    budget: "生活开销"\n'
                       f'  Liabilities:Clearing -{card_debt} CNY\n')
        if second_advance:
            source += ('2026-09-10 * "second employer advance"\n'
                       '  Liabilities:Clearing 100 CNY\n'
                       '  Assets:Bank -100 CNY\n    fund: "生活费"\n')
        for day, amount in reimbursements:
            source += (f'{day} * "employer reimbursement"\n'
                       f'  {asset} {amount} {currency}\n    fund: "{name}"\n'
                       f'  Liabilities:Clearing -{amount} {currency}\n')
        self.ledger.write_text(source, encoding="utf-8")
        self.snapshot = load_financial(self.ledger).digest
        self.owners = [{"id": ident, "name": label, "aliases": []}
                       for ident, label in (("living", "生活费"), ("free", "自由资金"), ("travel", "旅游"))]
        self.save("accounts", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "accounts": [{"account": account, "semantics": "H", "balance_semantics": "real_inventory",
                          "payment_scope": {commodity: "excluded" if commodity == "USD" else "payable"},
                          "reason": "reviewed virtual cash"}
                         for account, commodity, _, _ in openings]})
        self.save("opening", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
            "h_holdings": [{"account": account, "commodity": commodity, "lot": commodity + "/no-cost",
                            "owner": purpose, "units": amount, "reason": "reviewed opening"}
                           for account, commodity, purpose, amount in openings], "k_openings": []})
        self.save("ownership", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
            "loader_version": BEANCOUNT_VERSION, "owners": self.owners,
            "defaults": {"income_owner_id": "free", "expense_owner_id": "living"}, "claims": []})
        self.candidates = audit_simple_obligations(self.ledger, CUTOVER)["events"]
        claims = []
        for row in self.candidates:
            purchase = row["shape"] == "consumer_purchase"
            claims.append({"liability_ref": row["liability_ref"], "counterpart_ref": row["counterpart_ref"],
                           "kind": "consumer_purchase" if purchase else
                           "advance_outflow" if row["shape"] == "asset_outflow" else "advance_reimbursement",
                           **({"obligation_id": "personal-card", "owner_id": "living"} if purchase else
                              {"clearing_id": "second-advance" if row["date"] == "2026-09-10" else "employer-phone"}),
                           "reason": "reviewed employer advance or personal purchase"})
        self.obligation_document = {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
            "loader_version": BEANCOUNT_VERSION, "owners": self.owners, "events": claims}
        self.save("obligations", self.obligation_document)

    def statement(self, as_of):
        return self.save("completeness", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "as_of": as_of.isoformat(),
            "unbooked_obligations": "none", "inflight_payments": "none", "reason": "complete virtual day"})

    def audit(self, as_of=date(2026, 9, 30), coverage=None):
        return audit_actual_snapshot(
            self.ledger, CUTOVER, as_of,
            *(self.base / (key + ".yaml") for key in ("accounts", "opening", "ownership", "obligations")),
            self.statement(as_of), coverage)

    def config(self, month):
        self.save("classifications", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "loader_version": BEANCOUNT_VERSION,
            "categories": [{"id": "daily", "name": "生活开销", "parent": None, "aliases": []}],
            "classifications": []})
        self.save("expense_plans", {
            "schema": 6, "report_currency": "CNY", "spendable_cap_category_id": "daily",
            "defaults": {"expense_subtree_targets": {"daily": "2000"}}})
        self.save("purpose_plans", {
            "schema": 6, "report_currency": "CNY",
            "defaults": {"allocation_targets": {row["id"]: "not_assessed" for row in self.owners}}})
        for key in ("h_transfers", "h_fx", "k_events"):
            self.save(key, {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                            "loader_version": BEANCOUNT_VERSION, "events": []})
        self.save("purpose_pools", {"schema": 6, "cutover": CUTOVER.isoformat(),
                                    "report_currency": "CNY", "pools": []})
        year, number = map(int, month.split("-"))
        self.statement(date(year, number, calendar.monthrange(year, number)[1]))
        return self.save("month", {
            "schema": 6, "ledger": "main.bean", "cutover": CUTOVER.isoformat(),
            "month": month, "report_currency": "CNY",
            **{key: key + ".yaml" for key in (
                "accounts", "opening", "ownership", "obligations", "completeness", "classifications",
                "expense_plans", "purpose_plans", "h_transfers", "h_fx", "k_events", "purpose_pools")}})

    def test_outflow_partial_and_cross_month_reimbursement_follow_cash_once(self):
        self.fixture()
        for day, available, receivable in (("2026-09-07", "1000", None),
                                           ("2026-09-08", "500", "500"),
                                           ("2026-09-30", "700", "300"),
                                           ("2026-10-05", "1000", None)):
            with self.subTest(day=day):
                report = self.audit(date.fromisoformat(day))
                self.assertEqual((report["status"], report["actual_spendable"]), ("CERTIFIED", available))
                self.assertEqual(report["booked_debts"], [])
                self.assertEqual(report["uncovered_responsibility"], "0")
                if receivable is None:
                    self.assertEqual(report["clearing_receivables"], [])
                else:
                    row = report["clearing_receivables"][0]
                    self.assertEqual((row["clearing_id"], row["remaining_native"]), ("employer-phone", receivable))
                    self.assertEqual(row["source"], f"{self.ledger.resolve()}:10")

    def test_mixed_debt_and_receivable_do_not_net_away_personal_responsibility(self):
        self.fixture(reimbursements=(), card_debt=500)
        report = self.audit()
        self.assertEqual(report["unresolved_liabilities"], [])  # Same-coordinate booked balance is zero.
        self.assertEqual((report["status"], report["actual_spendable"]), ("CERTIFIED", "0"))
        self.assertEqual(report["booked_debts"][0]["remaining_native"], "500")
        self.assertEqual(report["clearing_receivables"][0]["remaining_native"], "500")
        self.assertEqual(report["uncovered_responsibility"], "500")

    def test_multiple_advances_keep_distinct_source_identities(self):
        self.fixture(reimbursements=(), second_advance=True)
        report = self.audit()
        self.assertEqual((report["status"], report["actual_spendable"]), ("CERTIFIED", "400"))
        self.assertEqual([(row["clearing_id"], row["remaining_native"])
                          for row in report["clearing_receivables"]],
                         [("employer-phone", "500"), ("second-advance", "100")])

    def test_protected_advance_does_not_deduct_daily_cash(self):
        self.fixture(protected=True)
        report = self.audit()
        self.assertEqual((report["status"], report["actual_spendable"]), ("CERTIFIED", "1000"))
        self.assertEqual(report["clearing_receivables"][0]["remaining_native"], "300")
        self.assertEqual(report["coverage_allocations"], [])

    def test_foreign_receivable_stays_native_without_a_fabricated_valuation(self):
        self.fixture(foreign=True, reimbursements=(("2026-09-20", 20), ("2026-10-05", 30)))
        report = self.audit()
        self.assertEqual((report["status"], report["actual_spendable"]), ("CERTIFIED", "1000"))
        self.assertEqual((report["clearing_receivables"][0]["commodity"],
                          report["clearing_receivables"][0]["remaining_native"]), ("USD", "30"))

    def test_unclaimed_reimbursement_retains_source_and_signed_mismatch(self):
        self.fixture()
        self.obligation_document["events"].pop(1)
        self.save("obligations", self.obligation_document)
        report = self.audit()
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertIsNone(report["actual_spendable"])
        self.assertIsNone(report["clearing_receivables"])
        unclaimed = next(row for row in report["review_items"] if row["reason_code"] == "OBLIGATION_UNCLAIMED")
        mismatch = next(row for row in report["review_items"] if row["reason_code"] == "BOOKED_LIABILITY_UNMATCHED")
        self.assertTrue(unclaimed["source"].startswith(str(self.ledger.resolve()) + ":"))
        self.assertEqual((mismatch["signed_booked_units"], mismatch["reviewed_remaining"],
                          mismatch["reviewed_receivable"], mismatch["signed_reviewed_units"]),
                         ("300", "0", "500", "500"))

    def test_zero_net_balance_does_not_allow_unreviewed_debt_or_advance(self):
        self.fixture(reimbursements=(), card_debt=500)
        self.obligation_document["events"] = []
        self.save("obligations", self.obligation_document)
        report = self.audit()
        self.assertEqual(report["unresolved_liabilities"], [])
        self.assertEqual(report["reason_codes"], ["BOOKED_OBLIGATIONS_PENDING"])
        self.assertIsNone(report["actual_spendable"])
        self.assertEqual(len(report["review_items"]), 2)

    def test_future_unreviewed_reimbursement_only_blocks_its_own_month(self):
        self.fixture()
        self.obligation_document["events"].pop()
        self.save("obligations", self.obligation_document)
        september = audit_month_workflow(self.config("2026-09"))
        self.assertEqual(september["status"], "CERTIFIED_SCOPED")
        self.assertEqual(september["report"]["actual_spendable"]["actual_spendable"], "700")
        october = audit_month_workflow(self.config("2026-10"))
        self.assertEqual(october["status"], "NEEDS_REVIEW")
        self.assertIsNone(october["report"])
        issue = next(row for row in october["review_items"] if row["reason_code"] == "OBLIGATION_UNCLAIMED")
        self.assertEqual(issue["source"], str(self.ledger.resolve()) + ":20")

    def test_future_reference_lookup_does_not_allow_stale_current_claims(self):
        self.fixture()
        self.obligation_document["events"][0]["liability_ref"]["fact_sha256"] = "0" * 64
        self.save("obligations", self.obligation_document)
        with self.assertRaises(BudgetError) as caught:
            audit_month_workflow(self.config("2026-09"))
        self.assertEqual(caught.exception.code, "OBLIGATION_REF_INVALID")

    def test_receivable_cannot_be_used_as_debt_coverage_cash(self):
        self.fixture(reimbursements=(), card_debt=300)
        debt = self.audit()["booked_debts"][0]
        coverage = self.save("coverage", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "as_of": "2026-09-30", "report_currency": "CNY",
            "decisions": [{"obligation_id": debt["obligation_id"], "purchase_ref": debt["purchase_ref"],
                           "resources": [{"account": "Liabilities:Clearing", "commodity": "CNY",
                                          "lot": "CNY/no-cost", "owner_id": "living", "amount": "300"}],
                           "reason": "invalid reservation of an unreceived reimbursement"}]})
        with self.assertRaises(BudgetError) as caught:
            self.audit(coverage=coverage)
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_RESOURCE_INVALID")

    def test_full_month_reports_open_receivables_and_later_settlement(self):
        self.fixture()
        september = audit_month_workflow(self.config("2026-09"))
        self.assertEqual(september["status"], "CERTIFIED_SCOPED", september)
        report = september["report"]
        self.assertEqual(report["max_spendable"]["max_spendable"], "700")
        self.assertTrue(all(row["actual_g"] == "0" for row in report["purpose_progress"]["owners"]))
        self.assertEqual(report["expense_targets"]["categories"][0]["subtree_spent"], "0")
        text = render_workflow_text(september)
        self.assertIn("待报销代垫（应收原币；未到账，不计入可花现金）", text)
        self.assertIn("employer-phone：待收 300 CNY", text)
        self.assertIn(str(self.ledger.resolve()) + ":10", text)
        october = audit_month_workflow(self.config("2026-10"))
        self.assertEqual(october["status"], "CERTIFIED_SCOPED", october)
        self.assertEqual(october["report"]["max_spendable"]["max_spendable"], "1000")
        self.assertEqual(october["report"]["actual_spendable"]["clearing_receivables"], [])
        self.assertNotIn("待报销代垫", render_workflow_text(october))


if __name__ == "__main__":
    unittest.main()

"""Explicit coverage binds real cash capacity to reviewed outstanding debts."""

from datetime import date
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.cli import main as audit_cli
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.reference_checks import diagnose


CUTOVER = date(2026, 9, 1)
AS_OF = date(2026, 9, 30)


class DebtCoverageTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def save(self, name, document):
        path = self.base / name
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def fixture(self, purchases=(300,), *, payment=False, excluded=False,
                cash_expense=0, negative_owner=False):
        ledger = self.base / "main.bean"
        source = (
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Daily\n'
            '2026-08-01 open Assets:Travel\n'
            '2026-08-01 open Assets:Other\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Travel\n'
            '2026-08-01 open Expenses:Daily\n'
            '2026-08-01 open Liabilities:Card\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Daily 1000 CNY\n'
            '  Assets:Travel 100 CNY\n'
            '  Assets:Other 250 CNY\n'
            '  Equity:Opening -1350 CNY\n')
        for index, amount in enumerate(purchases):
            source += (f'2026-09-{index + 2:02d} * "travel card purchase"\n'
                       f'  Expenses:Travel {amount} CNY\n    budget: "旅游消费"\n'
                       f'  Liabilities:Card -{amount} CNY\n')
        if payment:
            source += ('2026-09-20 * "travel card payment"\n'
                       '  Assets:Travel -100 CNY\n    fund: "旅游"\n'
                       '  Liabilities:Card 100 CNY\n')
        if cash_expense:
            source += (f'2026-09-21 * "daily expense from mixed account"\n'
                       f'  Expenses:Daily {cash_expense} CNY\n    budget: "生活开销"\n'
                       f'  Assets:Travel -{cash_expense} CNY\n')
        if negative_owner:
            source += ('2026-09-22 * "travel expense from other account"\n'
                       '  Expenses:Travel 200 CNY\n    budget: "旅游消费"\n'
                       '  Assets:Other -200 CNY\n    fund: "旅游"\n')
        ledger.write_text(source, encoding="utf-8")
        self.snapshot = load_financial(ledger).digest
        self.owner_rows = [{"id": ident, "name": name, "aliases": []}
                           for ident, name in (("living", "生活费"), ("free", "自由资金"),
                                               ("travel", "旅游"), ("savings", "储蓄"))]
        self.save("accounts.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "accounts": [{"account": account, "semantics": "H", "balance_semantics": "real_inventory",
                          "payment_scope": {"CNY": "excluded" if excluded and account == "Assets:Travel"
                                            else "payable"}, "reason": "reviewed virtual cash"}
                         for account in ("Assets:Daily", "Assets:Travel", "Assets:Other")]})
        self.save("opening.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
            "h_holdings": [{"account": account, "commodity": "CNY", "lot": "CNY/no-cost",
                            "owner": owner, "units": units, "reason": "reviewed opening"}
                           for account, owner, units in (("Assets:Daily", "living", "1000"),
                                                         ("Assets:Travel", "travel", "100"),
                                                         ("Assets:Other", "savings", "250"))],
            "k_openings": []})
        self.save("ownership.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
            "loader_version": BEANCOUNT_VERSION, "owners": self.owner_rows,
            "defaults": {"income_owner_id": "free", "expense_owner_id": "living"}, "claims": []})
        events = audit_simple_obligations(ledger, CUTOVER)["events"]
        claims = []
        for index, row in enumerate(events):
            purchase = row["shape"] == "consumer_purchase"
            claims.append({"liability_ref": row["liability_ref"], "counterpart_ref": row["counterpart_ref"],
                           "kind": "consumer_purchase" if purchase else "payment",
                           "obligation_id": f"purchase-{index}" if purchase else "purchase-0",
                           **({"owner_id": "travel"} if purchase else {}),
                           "reason": "reviewed responsibility"})
        self.save("obligations.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
            "loader_version": BEANCOUNT_VERSION, "owners": self.owner_rows, "events": claims})
        self.save("completeness.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "as_of": AS_OF.isoformat(),
            "unbooked_obligations": "none", "inflight_payments": "none", "reason": "virtual complete month"})
        self.debts = self.audit()["booked_debts"]
        return ledger

    def audit(self, coverage=None):
        return audit_actual_snapshot(
            self.base / "main.bean", CUTOVER, AS_OF,
            *(self.base / (key + ".yaml") for key in ("accounts", "opening", "ownership", "obligations")),
            self.base / "completeness.yaml", coverage)

    def declaration(self, amounts, *, account="Assets:Travel", owner="travel"):
        return {
            "schema": 6, "cutover": CUTOVER.isoformat(), "as_of": AS_OF.isoformat(),
            "report_currency": "CNY", "decisions": [
                {"obligation_id": row["obligation_id"], "purchase_ref": row["purchase_ref"],
                 "resources": ([] if amount == 0 else [
                     {"account": account, "commodity": "CNY", "lot": "CNY/no-cost",
                      "owner_id": owner, "amount": str(amount)}]), "reason": "explicit virtual reservation"}
                for row, amount in zip(self.debts, amounts, strict=True)]}

    def covered(self, amounts, **kwargs):
        return self.audit(self.save("coverage.yaml", self.declaration(amounts, **kwargs)))

    def test_same_name_does_not_imply_coverage(self):
        self.fixture()
        pending = self.audit()
        self.assertIsNone(pending["actual_spendable"])
        issue = next(row for row in pending["review_items"] if row["reason_code"] == "DEBT_COVERAGE_PENDING")
        self.assertEqual(issue["reason_kind"], "data_review")
        self.assertEqual(issue["obligation_id"], "purchase-0")

    def test_full_and_partial_coverage_follow_frozen_examples(self):
        self.fixture(purchases=(83,))
        full = self.covered([83])
        self.assertEqual(full["actual_spendable"], "1000")
        available = {row["owner_id"]: row["units"] for row in full["purpose_available"]}
        self.assertEqual(available["travel"], "17")
        self.fixture()
        partial = self.covered([100])
        self.assertEqual(partial["actual_spendable"], "800")
        self.assertEqual(partial["uncovered_responsibility"], "200")
        self.assertEqual(partial["obligation_coverage"], [
            {"obligation_id": "purchase-0", "covered": "100", "uncovered": "200"}])
        available = {row["owner_id"]: row["units"] for row in partial["purpose_available"]}
        self.assertEqual(available["travel"], "-200")

    def test_explicit_no_reservation_keeps_full_pressure(self):
        self.fixture()
        report = self.covered([0])
        self.assertEqual(report["actual_spendable"], "700")
        self.assertEqual(report["coverage_allocations"], [])

    def test_shared_cash_capacity_is_jointly_checked(self):
        self.fixture(purchases=(300, 300))
        valid = self.covered([70, 30])
        self.assertEqual(valid["actual_spendable"], "500")
        with self.assertRaises(BudgetError) as caught:
            self.covered([70, 60])
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_RESOURCE_EXCEEDED")
        self.assertTrue(caught.exception.source.startswith(f"{(self.base / 'coverage.yaml').resolve()}:"))

    def test_reservation_never_exceeds_responsibility_or_physical_cash(self):
        self.fixture(purchases=(83,))
        with self.assertRaises(BudgetError) as caught:
            self.covered([100])
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_OBLIGATION_EXCEEDED")
        self.fixture(cash_expense=80)
        with self.assertRaises(BudgetError) as caught:
            self.covered([100])
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_INVENTORY_EXCEEDED")

    def test_negative_net_purpose_cannot_supply_coverage(self):
        self.fixture(negative_owner=True)
        with self.assertRaises(BudgetError) as caught:
            self.covered([100])
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_OWNER_EXCEEDED")

    def test_excluded_cash_and_stale_purchase_ref_are_rejected(self):
        self.fixture(excluded=True)
        with self.assertRaises(BudgetError) as caught:
            self.covered([100])
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_RESOURCE_INVALID")
        self.fixture()
        document = self.declaration([100])
        document["decisions"][0]["purchase_ref"]["fact_sha256"] = "0" * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("coverage.yaml", document))
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_REF_INVALID")
        document = self.declaration([100])
        document["decisions"][0]["purchase_ref"]["transaction_sha256"] = date(2026, 9, 1)
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("coverage.yaml", document))
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_REF_INVALID")

    def test_payment_and_release_do_not_deduct_again(self):
        self.fixture()
        before = self.covered([100])
        self.fixture(payment=True)
        after = self.covered([0])
        self.assertEqual(before["actual_spendable"], after["actual_spendable"])
        self.assertEqual(after["actual_spendable"], "800")
        with self.assertRaises(BudgetError) as caught:
            self.covered([100])
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_RESOURCE_INVALID")

    def test_missing_decision_and_stale_report_date_return_no_partial_number(self):
        self.fixture(purchases=(300, 300))
        document = self.declaration([100, 0])
        document["decisions"].pop()
        pending = self.audit(self.save("coverage.yaml", document))
        self.assertEqual(pending["status"], "NEEDS_REVIEW")
        self.assertIsNone(pending["actual_spendable"])
        self.assertIsNone(pending["coverage_allocations"])
        self.assertEqual([row["obligation_id"] for row in pending["review_items"]], ["purchase-1"])
        document = self.declaration([100, 0])
        document["as_of"] = "2026-09-29"
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("coverage.yaml", document))
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_INVALID")
        self.assertEqual(caught.exception.source, f"{(self.base / 'coverage.yaml').resolve()}:3")

    def test_fully_paid_debt_requires_release_of_old_reservation(self):
        self.fixture(purchases=(100,))
        document = self.declaration([100])
        before = self.covered([100])
        self.fixture(purchases=(100,), payment=True)
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("coverage.yaml", document))
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_OBLIGATION_UNKNOWN")
        after = self.covered([])
        self.assertEqual(before["actual_spendable"], after["actual_spendable"])
        self.assertEqual(after["actual_spendable"], "1000")

    def test_monthly_report_and_ref_review_include_coverage(self):
        self.fixture()
        self.covered([100])
        self.save("classifications.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "loader_version": BEANCOUNT_VERSION,
            "categories": [{"id": ident, "name": name, "parent": None, "aliases": []}
                           for ident, name in (("living_expense", "生活开销"), ("travel_expense", "旅游消费"))],
            "classifications": []})
        self.save("expense_plans.yaml", {
            "schema": 6, "report_currency": "CNY", "spendable_cap_category_id": "living_expense",
            "defaults": {"expense_targets": {"travel_expense": "500"},
                         "expense_subtree_targets": {"living_expense": "2000"}}})
        self.save("purpose_plans.yaml", {
            "schema": 6, "report_currency": "CNY",
            "defaults": {"allocation_targets": {row["id"]: "not_assessed" for row in self.owner_rows}}})
        for key in ("h_transfers", "h_fx", "k_events"):
            self.save(key + ".yaml", {
                "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                "loader_version": BEANCOUNT_VERSION, "events": []})
        self.save("purpose_pools.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY", "pools": []})
        config = self.save("month.yaml", {
            "schema": 6, "ledger": "main.bean", "cutover": CUTOVER.isoformat(),
            "month": "2026-09", "report_currency": "CNY", "debt_coverage": "coverage.yaml",
            **{key: key + ".yaml" for key in (
                "accounts", "opening", "ownership", "obligations", "completeness", "classifications",
                "expense_plans", "purpose_plans", "h_transfers", "h_fx", "k_events", "purpose_pools")}})
        result = audit_month_workflow(config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "800")
        progress = {row["owner_id"]: row["actual_g"]
                    for row in result["report"]["purpose_progress"]["owners"]}
        self.assertEqual(progress["travel"], "0")
        held = next(row for row in result["report"]["account_purposes"]["rows"]
                    if row["account"] == "Assets:Travel" and row["owner_id"] == "travel")
        self.assertEqual(held["units"], "100")
        text = render_workflow_text(result)
        self.assertIn("专用覆盖 100，未覆盖 200", text)
        self.assertIn("旅游：-200 CNY", text)
        refs = diagnose(config)
        coverage = next(row for row in refs["sidecars"] if row["field"] == "debt_coverage")
        self.assertEqual(coverage["references"], 1)
        self.assertEqual(coverage["stale_references"], [])
        output = StringIO()
        with redirect_stdout(output):
            code = audit_cli([
                "--actual-audit", "--ledger", str(self.base / "main.bean"),
                "--cutover", CUTOVER.isoformat(), "--as-of", AS_OF.isoformat(),
                *[argument for key in ("accounts", "opening", "ownership", "obligations", "completeness")
                  for argument in ("--" + key, str(self.base / (key + ".yaml")))],
                "--debt-coverage", str(self.base / "coverage.yaml")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["actual_spendable"], "800")


if __name__ == "__main__":
    unittest.main()

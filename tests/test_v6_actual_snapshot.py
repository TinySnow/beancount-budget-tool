"""No actual-spendable number without scoped cash and offbook evidence."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.provenance import asset_opening_fingerprint, as_of_booked_fingerprint
from beancount_budget_tool.tools.v6_upgrade_completeness import upgrade


CUTOVER = date(2026, 9, 1)
AS_OF = date(2026, 9, 30)


class ActualSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.accounts = folder / "accounts.yml"
        self.opening = folder / "opening.yml"
        self.ownership = folder / "ownership.yml"
        self.obligations = folder / "obligations.yml"
        self.completeness = folder / "complete.yml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 100 CNY\n'
            '  Equity:Opening -100 CNY\n'
            '2026-09-02 * "food"\n'
            '  Expenses:Food 20 CNY\n'
            '  Assets:Bank -20 CNY\n', encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": self.snapshot, "accounts": [
                {"account": "Assets:Bank", "semantics": "H",
                 "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                 "reason": "reviewed payable cash"}]}, allow_unicode=True), encoding="utf-8")
        self.opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
            "h_holdings": [{"account": "Assets:Bank", "commodity": "CNY",
                            "lot": "CNY/no-cost", "owner": "-", "units": "100",
                            "reason": "reviewed free opening"}], "k_openings": []},
            allow_unicode=True), encoding="utf-8")
        ref = audit_ownership(self.root, CUTOVER, self.accounts)["missing_h_ownership"][0]["ref"]
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
            "owners": [], "claims": [{"claim": ref, "reason": "reviewed food payment",
                                    "slices": [{"id": "food", "owner_id": "-",
                                                "role": "hold", "signed_units": "-20"}]}]},
            allow_unicode=True), encoding="utf-8")
        self.obligations.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
            "owners": [], "events": []}, allow_unicode=True), encoding="utf-8")

    def write_completeness(self, snapshot=None):
        self.completeness.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "as_of": AS_OF.isoformat(),
            "ledger_snapshot": snapshot or self.snapshot,
            "unbooked_obligations": "none", "inflight_payments": "none",
            "reason": "reviewed virtual complete date end"},
            allow_unicode=True), encoding="utf-8")

    def audit(self, completeness=None):
        return audit_actual_snapshot(self.root, CUTOVER, AS_OF,
                                     self.accounts, self.opening, self.ownership,
                                     self.obligations, completeness)

    def test_audited_debt_free_snapshot_uses_pure_core(self):
        self.write_completeness()
        report = self.audit(self.completeness)
        self.assertEqual((report["status"], report["actual_spendable"]),
                         ("CERTIFIED", "80"))
        self.assertEqual(report["reason_codes"], [])
        self.assertIn("本报告截至 2026-09-30 日末", report["note"])
        self.assertIn("此后新发生的消费、负债和付款不倒扣", report["note"])
        self.assertIn("截至报告日已发生但漏记", report["note"])

    def test_missing_external_scope_never_uses_zero_by_default(self):
        report = self.audit()
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertIsNone(report["actual_spendable"])
        self.assertIn("EXTERNAL_SCOPE_PENDING", report["reason_codes"])

    def test_dated_scope_declaration_does_not_depend_on_ledger_hash(self):
        self.write_completeness("0" * 64)
        self.assertEqual(self.audit(self.completeness)["actual_spendable"], "80")
        document = yaml.safe_load(self.completeness.read_text(encoding="utf-8"))
        del document["ledger_snapshot"]
        self.completeness.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.assertEqual(self.audit(self.completeness)["actual_spendable"], "80")

    def test_historical_statement_survives_imports_but_new_claims_still_need_review(self):
        fingerprint = asset_opening_fingerprint(load_financial(self.root), CUTOVER)
        for path in (self.accounts, self.opening):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["opening_fingerprint"] = fingerprint
            path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.write_completeness()
        upgraded = self.completeness.with_name("complete-scoped.yml")
        result = upgrade(self.root, CUTOVER, AS_OF, self.accounts, self.opening,
                         self.ownership, self.obligations, self.completeness, upgraded)
        self.assertEqual(result["actual_spendable"], "80")
        before = as_of_booked_fingerprint(load_financial(self.root), AS_OF)
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-10-02 * "later purchase"\n'
                             '  Expenses:Food 3 CNY\n'
                             '  Assets:Bank -3 CNY\n', encoding="utf-8")
        self.assertEqual(as_of_booked_fingerprint(load_financial(self.root), AS_OF), before)
        self.assertEqual(self.audit(upgraded)["actual_spendable"], "80")
        self.assertEqual(self.audit(self.completeness)["actual_spendable"], "80")
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-09-25 * "late imported September purchase"\n'
                             '  Expenses:Food 5 CNY\n'
                             '  Assets:Bank -5 CNY\n', encoding="utf-8")
        self.assertNotEqual(as_of_booked_fingerprint(load_financial(self.root), AS_OF), before)
        report = self.audit(upgraded)
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertIsNone(report["actual_spendable"])
        self.assertIn("PAYABLE_CASH_PENDING", report["reason_codes"])


if __name__ == "__main__":
    unittest.main()

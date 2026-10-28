"""Payable cash totals require complete scoped H inventory."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.payable_cash import audit_payable_cash


CUTOVER = date(2026, 9, 1)


class PayableCashTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.accounts = folder / "accounts.yml"
        self.opening = folder / "opening.yml"
        self.ownership = folder / "ownership.yml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Assets:Broker\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 100 CNY\n'
            '  Assets:Broker 50 CNY\n'
            '  Equity:Opening -150 CNY\n'
            '2026-09-02 * "food"\n'
            '  Expenses:Food 20 CNY\n'
            '  Assets:Bank -20 CNY\n', encoding="utf-8")
        snapshot = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": snapshot, "accounts": [
                {"account": name, "semantics": "H", "balance_semantics": "real_inventory",
                 "payment_scope": {"CNY": scope}, "reason": "reviewed scope"}
                for name, scope in (("Assets:Bank", "payable"), ("Assets:Broker", "excluded"))]},
            allow_unicode=True), encoding="utf-8")
        self.opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "h_holdings": [
                {"account": "Assets:Bank", "commodity": "CNY", "lot": "CNY/no-cost",
                 "owner": "-", "units": "60", "reason": "reviewed free opening"},
                {"account": "Assets:Bank", "commodity": "CNY", "lot": "CNY/no-cost",
                 "owner": "travel", "units": "40", "reason": "reviewed reserve"}],
            "k_openings": []}, allow_unicode=True), encoding="utf-8")
        missing = audit_ownership(self.root, CUTOVER, self.accounts)["missing_h_ownership"]
        assert len(missing) == 1 and missing[0]["account"] == "Assets:Bank"
        self.ref = missing[0]["ref"]
        self.snapshot = snapshot
        self.write_ownership(True)

    def write_ownership(self, claim):
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "travel", "name": "旅行", "aliases": []}],
            "claims": ([{"claim": self.ref, "reason": "reviewed cash expense",
                        "slices": [{"id": "food_cash", "owner_id": "-",
                                    "role": "hold", "signed_units": "-20"}]}]
                       if claim else [])}, allow_unicode=True), encoding="utf-8")

    def audit(self):
        return audit_payable_cash(self.root, CUTOVER, date(2026, 9, 30),
                                  self.accounts, self.opening, self.ownership)

    def upgrade_opening_provenance(self):
        fingerprint = audit_accounts(self.root, CUTOVER)["opening_fingerprint"]
        for path in (self.accounts, self.opening):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["opening_fingerprint"] = fingerprint
            path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    def test_excluded_unknown_holdings_do_not_block_complete_payable_cash(self):
        report = self.audit()
        self.assertEqual((report["review_state"], report["free_cash"],
                          report["total_payable_cash"]), ("complete", "40", "80"))
        self.assertEqual(report["owner_units"], [
            {"owner_id": "-", "units": "40"},
            {"owner_id": "travel", "units": "40"}])
        self.assertEqual(report["payable_coordinates_pending"], 0)
        self.assertEqual(len(report["excluded_h_coordinates"]), 1)

    def test_unknown_payable_coordinate_blocks_all_totals(self):
        self.write_ownership(False)
        report = self.audit()
        self.assertEqual(report["review_state"], "pending")
        self.assertEqual(report["payable_coordinates_pending"], 1)
        self.assertIsNone(report["free_cash"])
        self.assertIsNone(report["total_payable_cash"])
        self.assertIsNone(report["owner_units"])

    def test_later_import_preserves_old_snapshot_and_exposes_new_gap(self):
        self.upgrade_opening_provenance()
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-10-02 * "new imported food"\n'
                             '  Expenses:Food 3 CNY\n'
                             '  Assets:Bank -3 CNY\n', encoding="utf-8")
        old = self.audit()
        self.assertEqual((old["review_state"], old["free_cash"]), ("complete", "40"))
        new = audit_payable_cash(self.root, CUTOVER, date(2026, 10, 31),
                                 self.accounts, self.opening, self.ownership)
        self.assertEqual(new["review_state"], "pending")
        self.assertEqual(new["payable_coordinates_pending"], 1)

    def test_changed_old_posting_invalidates_reviewed_claim(self):
        self.upgrade_opening_provenance()
        source = self.root.read_text(encoding="utf-8")
        self.root.write_text(source.replace("20 CNY", "21 CNY"), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, "OWNERSHIP_REF_INVALID")

    def test_changed_pre_cutover_asset_invalidates_opening_amount(self):
        self.upgrade_opening_provenance()
        source = self.root.read_text(encoding="utf-8")
        self.root.write_text(source.replace("Assets:Bank 100 CNY", "Assets:Bank 90 CNY")
                             .replace("Equity:Opening -150 CNY", "Equity:Opening -140 CNY"),
                             encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, "OPENING_OVERALLOCATED")


if __name__ == "__main__":
    unittest.main()

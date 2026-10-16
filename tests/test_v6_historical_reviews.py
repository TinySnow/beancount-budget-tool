"""Shared decisions must preserve old-month totals and reject stale sources."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, load_financial
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.ownership import audit_ownership


CUTOVER = date(2026, 9, 1)
SEPTEMBER_END = date(2026, 9, 30)


class HistoricalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "main.bean"
        self.accounts = self.base / "accounts.yaml"
        self.ownership = self.base / "ownership.yaml"
        self.classifications = self.base / "classifications.yaml"
        self.opening = self.base / "opening.yaml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Cash\n'
            '2026-09-01 open Income:Salary\n'
            '2026-09-01 open Expenses:Food\n'
            '2026-09-02 * "salary"\n'
            '  Assets:Cash 100 CNY\n'
            '  Income:Salary -100 CNY\n'
            '2026-09-03 * "return excess salary"\n'
            '  Income:Salary 20 CNY\n'
            '  Assets:Cash -20 CNY\n'
            '2026-09-04 * "September food"\n'
            '  Expenses:Food 10 CNY\n'
            '  Assets:Cash -10 CNY\n'
            '2026-10-04 * "October food"\n'
            '  Expenses:Food 10 CNY\n'
            '  Assets:Cash -10 CNY\n', encoding="utf-8")
        snapshot = load_financial(self.root).digest
        self.save(self.accounts, {
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": snapshot, "report_currency": "CNY",
            "accounts": [{"account": "Assets:Cash", "semantics": "H",
                          "balance_semantics": "real_inventory",
                          "payment_scope": {"CNY": "payable"}, "reason": "virtual"}]})
        self.save(self.opening, {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "h_holdings": [], "k_openings": []})
        self.save(self.ownership, {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": ident, "name": name, "aliases": []}
                       for ident, name in (("free", "自由资金"), ("living", "生活费"))],
            "defaults": {"income_owner_id": "free", "expense_owner_id": "living"},
            "claims": []})
        self.save(self.classifications, {
            "schema": 6, "cutover": CUTOVER.isoformat(), "loader_version": BEANCOUNT_VERSION,
            "categories": [{"id": ident, "name": name, "aliases": [], "parent": None}
                           for ident, name in (("free", "自由资金"), ("living", "生活开销"))],
            "defaults": {"income_category_id": "free", "expense_category_id": "living"},
            "classifications": []})

    def save(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")

    def shared_claims(self):
        classes = yaml.safe_load(self.classifications.read_text(encoding="utf-8"))
        classified = audit_classification(self.root, CUTOVER, self.classifications)
        classes["classifications"] = [
            {"claim": row["ref"], "reason": "reviewed", "slices": [
                {"id": f"category{index}", "category_id": row["slices"][0]["category_id"],
                 "signed_units": row["slices"][0]["signed_units"]}]}
            for index, row in enumerate(classified["classifications"])]
        self.save(self.classifications, classes)
        owners = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        owned = audit_ownership(self.root, CUTOVER, self.accounts, self.ownership)
        owners["claims"] = [
            {"claim": row["ref"], "reason": "reviewed", "slices": [
                {"id": f"owner{index}", "owner_id": row["slices"][0]["owner_id"],
                 "role": "hold", "signed_units": row["slices"][0]["signed_units"]}]}
            for index, row in enumerate(owned["ownership"])]
        self.save(self.ownership, owners)

    def test_income_reversal_debits_income_default_and_preserves_expense_default(self):
        with booked_through(SEPTEMBER_END):
            owned = audit_ownership(self.root, CUTOVER, self.accounts, self.ownership)
            self.assertEqual(owned["review_state"], "complete")
            self.assertEqual([(row["slices"][0]["owner_id"],
                               row["slices"][0]["signed_units"])
                              for row in owned["ownership"]],
                             [("free", "100"), ("free", "-20"), ("living", "-10")])
            held = audit_h_holdings(self.root, CUTOVER, SEPTEMBER_END,
                                   self.accounts, self.opening, self.ownership)
            self.assertEqual(held["positions"][0]["owner_units"], [
                {"owner_id": "free", "units": "80"},
                {"owner_id": "living", "units": "-10"}])

    def test_explicit_fund_overrides_income_reversal_default(self):
        self.root.write_text(self.root.read_text(encoding="utf-8").replace(
            '  Assets:Cash -20 CNY\n', '  Assets:Cash -20 CNY\n    fund: "生活费"\n'),
            encoding="utf-8")
        owned = audit_ownership(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual(owned["ownership"][1]["slices"][0]["owner_id"], "living")

    def test_shared_decisions_do_not_enter_historical_balances(self):
        self.shared_claims()
        with booked_through(SEPTEMBER_END):
            classes = audit_classification(self.root, CUTOVER, self.classifications)
            self.assertEqual((classes["review_state"], classes["claimed_postings"]),
                             ("complete", 3))
            owned = audit_ownership(self.root, CUTOVER, self.accounts, self.ownership)
            self.assertEqual((owned["review_state"], owned["claimed_h_postings"]),
                             ("complete", 3))
            held = audit_h_holdings(self.root, CUTOVER, SEPTEMBER_END,
                                   self.accounts, self.opening, self.ownership)
            self.assertEqual(held["positions"][0]["owner_units"], [
                {"owner_id": "free", "units": "80"},
                {"owner_id": "living", "units": "-10"}])
        with booked_through(date(2026, 10, 31)):
            self.assertEqual(audit_classification(self.root, CUTOVER, self.classifications)
                             ["claimed_postings"], 4)
            held = audit_h_holdings(self.root, CUTOVER, date(2026, 10, 31),
                                   self.accounts, self.opening, self.ownership)
            self.assertEqual(held["positions"][0]["owner_units"][1]["units"], "-20")

    def test_changed_future_or_current_sources_are_still_rejected_with_yaml_location(self):
        self.shared_claims()
        original = self.root.read_text(encoding="utf-8")
        for narration in ("September food", "October food"):
            with self.subTest(narration=narration):
                self.root.write_text(original.replace(narration, narration + " corrected"),
                                     encoding="utf-8")
                with booked_through(SEPTEMBER_END):
                    for operation, code, path in (
                        (lambda: audit_classification(self.root, CUTOVER, self.classifications),
                         "CLAIM_REF_INVALID", self.classifications),
                        (lambda: audit_ownership(self.root, CUTOVER, self.accounts, self.ownership),
                         "OWNERSHIP_REF_INVALID", self.ownership)):
                        with self.assertRaises(BudgetError) as caught:
                            operation()
                        self.assertEqual(caught.exception.code, code)
                        filename, lineno = caught.exception.source.rsplit(":", 1)
                        self.assertEqual(filename, str(path.resolve()))
                        self.assertTrue(lineno.isdecimal(), caught.exception.source)

    def test_invalid_defaults_have_config_file_and_line(self):
        for path, field, operation, code in (
            (self.ownership, "income_owner_id",
             lambda: audit_ownership(self.root, CUTOVER, self.accounts, self.ownership),
             "OWNERSHIP_DEFAULT_INVALID"),
            (self.classifications, "income_category_id",
             lambda: audit_classification(self.root, CUTOVER, self.classifications),
             "CLASSIFICATION_DEFAULT_INVALID")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["defaults"][field] = "unknown"
            self.save(path, document)
            with self.assertRaises(BudgetError) as caught:
                operation()
            self.assertEqual(caught.exception.code, code)
            expected_line = next(index for index, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1) if line == "defaults:")
            self.assertEqual(caught.exception.source, f"{path.resolve()}:{expected_line}")

    def test_future_reference_must_still_belong_to_the_claim_domain(self):
        self.shared_claims()
        classes = yaml.safe_load(self.classifications.read_text(encoding="utf-8"))
        owners = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        expense_ref = classes["classifications"][-1]["claim"]
        cash_ref = owners["claims"][-1]["claim"]
        classes["classifications"][-1]["claim"] = cash_ref
        owners["claims"][-1]["claim"] = expense_ref
        self.save(self.classifications, classes)
        self.save(self.ownership, owners)
        with booked_through(SEPTEMBER_END):
            with self.assertRaises(BudgetError) as caught:
                audit_classification(self.root, CUTOVER, self.classifications)
            self.assertEqual(caught.exception.code, "CLAIM_REF_INVALID")
            with self.assertRaises(BudgetError) as caught:
                audit_ownership(self.root, CUTOVER, self.accounts, self.ownership)
            self.assertEqual(caught.exception.code, "OWNERSHIP_REF_INVALID")


if __name__ == "__main__":
    unittest.main()

"""Date-end H owner replay on a virtual Beancount ledger."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.ownership import audit_ownership


class HHoldingsTests(unittest.TestCase):
    def make_case(self, folder: Path, expense: int = 20,
                  claim: bool = True, full_opening: bool = True):
        root = folder / "main.bean"
        accounts = folder / "accounts.yml"
        opening = folder / "opening.yml"
        ownership = folder / "ownership.yml"
        root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Cash 100 CNY\n'
            '  Equity:Opening -100 CNY\n'
            '2026-09-02 * "food"\n'
            f'  Expenses:Food {expense} CNY\n'
            f'  Assets:Cash -{expense} CNY\n', encoding="utf-8")
        snapshot = load_financial(root).digest
        accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "report_currency": "CNY",
            "ledger_snapshot": snapshot, "accounts": [
                {"account": "Assets:Cash", "semantics": "H",
                 "balance_semantics": "real_inventory",
                 "payment_scope": {"CNY": "payable"}, "reason": "virtual cash"}]},
            allow_unicode=True), encoding="utf-8")
        h = [{"account": "Assets:Cash", "commodity": "CNY", "lot": "CNY/no-cost",
              "owner": "-", "units": "50", "reason": "virtual opening"}]
        if full_opening:
            h.append({"account": "Assets:Cash", "commodity": "CNY",
                      "lot": "CNY/no-cost", "owner": "travel", "units": "50",
                      "reason": "virtual protected cash"})
        opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "ledger_snapshot": snapshot,
            "h_holdings": h, "k_openings": []}, allow_unicode=True), encoding="utf-8")
        unclaimed = audit_ownership(root, date(2026, 9, 1), accounts)
        ref = unclaimed["missing_h_ownership"][0]["ref"]
        claims = ([{"claim": ref, "reason": "virtual owner",
                    "slices": [{"id": "cash_payment", "owner_id": "-",
                                "role": "hold", "signed_units": f"-{expense}"}]}]
                  if claim else [])
        ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "ledger_snapshot": snapshot,
            "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "travel", "name": "旅行", "aliases": []}],
            "claims": claims}, allow_unicode=True), encoding="utf-8")
        return root, accounts, opening, ownership

    def test_certified_owner_inventory(self):
        with TemporaryDirectory() as folder:
            args = self.make_case(Path(folder))
            report = audit_h_holdings(args[0], date(2026, 9, 1),
                                      date(2026, 9, 30), *args[1:])
            self.assertEqual((report["review_state"], report["certified_coordinates"],
                              report["pending_coordinates"]), ("complete", 1, 0))
            self.assertEqual(report["positions"][0]["owner_units"], [
                {"owner_id": "-", "units": "30"},
                {"owner_id": "travel", "units": "50"}])

    def test_unknown_claim_or_opening_never_yields_partial_holding(self):
        for claim, full_opening in ((False, True), (True, False)):
            with self.subTest(claim=claim, full_opening=full_opening):
                with TemporaryDirectory() as folder:
                    args = self.make_case(Path(folder), claim=claim,
                                          full_opening=full_opening)
                    report = audit_h_holdings(args[0], date(2026, 9, 1),
                                              date(2026, 9, 30), *args[1:])
                    self.assertEqual(report["review_state"], "pending")
                    self.assertIsNone(report["positions"][0]["owner_units"])

    def test_owner_overdraft_is_rejected_even_when_booked_h_stays_positive(self):
        with TemporaryDirectory() as folder:
            args = self.make_case(Path(folder), expense=60)
            with self.assertRaises(BudgetError) as caught:
                audit_h_holdings(args[0], date(2026, 9, 1),
                                 date(2026, 9, 30), *args[1:])
            self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")


if __name__ == "__main__":
    unittest.main()

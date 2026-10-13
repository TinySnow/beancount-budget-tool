"""An approved free-only month expands to explicit H evidence or rejects."""

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
from beancount_budget_tool.tools.v6_expand_free_ownership import expand


CUTOVER = date(2026, 9, 1)


class FreeOwnershipPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.accounts = folder / "accounts.yaml"
        self.opening = folder / "opening.yaml"
        self.ownership = folder / "ownership.yaml"
        self.policies = folder / "policies.yaml"
        self.out_opening = folder / "expanded-opening.yaml"
        self.out_ownership = folder / "expanded-ownership.yaml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Wallet\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Income:Gift\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Wallet 50 CNY\n'
            '  Equity:Opening -50 CNY\n'
            '2026-09-02 * "gift"\n'
            '  Assets:Wallet 20 CNY\n'
            '  Income:Gift -20 CNY\n'
            '2026-09-03 * "meal"\n'
            '  Expenses:Food 10 CNY\n'
            '  Assets:Wallet -10 CNY\n', encoding="utf-8")
        digest = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": digest,
            "report_currency": "CNY", "accounts": [{
                "account": "Assets:Wallet", "semantics": "H",
                "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                "reason": "reviewed virtual wallet"}]}, allow_unicode=True), encoding="utf-8")
        self.opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": digest,
            "h_holdings": [], "k_openings": []}, allow_unicode=True), encoding="utf-8")
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": digest,
            "loader_version": BEANCOUNT_VERSION, "owners": [], "claims": []},
            allow_unicode=True), encoding="utf-8")
        self.policies.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": digest,
            "loader_version": BEANCOUNT_VERSION, "policies": [{
                "account": "Assets:Wallet", "commodity": "CNY", "lot": "CNY/no-cost",
                "month": "2026-09", "opening_units": "50",
                "reason": "human reviewed entire month as free"}]}, allow_unicode=True),
            encoding="utf-8")

    def run_expand(self):
        opening, ownership, summary = expand(self.root, CUTOVER, self.accounts,
                                              self.opening, self.ownership, self.policies)
        self.out_opening.write_text(yaml.safe_dump(opening, allow_unicode=True), encoding="utf-8")
        self.out_ownership.write_text(yaml.safe_dump(ownership, allow_unicode=True), encoding="utf-8")
        return summary

    def test_approved_month_expands_opening_and_all_missing_legs(self):
        summary = self.run_expand()
        self.assertEqual((summary["generated_openings"], summary["generated_claims"]), (1, 2))
        report = audit_h_holdings(self.root, CUTOVER, date(2026, 9, 30),
                                  self.accounts, self.out_opening, self.out_ownership)
        self.assertEqual(report["review_state"], "complete")
        self.assertEqual(report["positions"][0]["owner_units"],
                         [{"owner_id": "-", "units": "60"}])

    def test_named_owner_anywhere_rejects_entire_month(self):
        missing = audit_ownership(self.root, CUTOVER, self.accounts)["missing_h_ownership"]
        owner = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        owner["owners"] = [{"id": "savings", "name": "储蓄", "aliases": []}]
        owner["claims"] = [{"claim": missing[0]["ref"], "reason": "named part",
                            "slices": [{"id": "named_gift", "owner_id": "savings",
                                        "role": "hold", "signed_units": "20"}]}]
        self.ownership.write_text(yaml.safe_dump(owner, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.run_expand()
        self.assertEqual(caught.exception.code, "FREE_POLICY_NAMED_CONFLICT")

    def test_opening_must_match_booked_amount(self):
        policy = yaml.safe_load(self.policies.read_text(encoding="utf-8"))
        policy["policies"][0]["opening_units"] = "51"
        self.policies.write_text(yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.run_expand()
        self.assertEqual(caught.exception.code, "FREE_POLICY_OPENING_MISMATCH")


if __name__ == "__main__":
    unittest.main()

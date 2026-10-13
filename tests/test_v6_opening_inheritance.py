"""Reviewed cutover ownership can seed safe fund inheritance on H transfers."""

from datetime import date
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.h_transfers import audit_h_transfers
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.cli import main as audit_main
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.purpose_flows import audit_monthly_purpose_flows


CUTOVER = date(2026, 10, 1)


class OpeningInheritanceTests(unittest.TestCase):
    def fixture(self, base: Path, slices: list[tuple[str, str]], transfer: str,
                debit_living: str | None = None,
                transfer_owner: str | None = None):
        root = base / "main.bean"
        accounts = base / "accounts.yaml"
        opening = base / "opening.yaml"
        ownership = base / "ownership.yaml"
        total = sum(int(amount) for _, amount in slices)
        expense = (f'2026-10-01 * "expense"\n'
                   f'  Expenses:Food {debit_living} CNY\n'
                   f'  Assets:Bank:A -{debit_living} CNY\n') if debit_living else ''
        from_fund = f'    fund: "{transfer_owner}"\n' if transfer_owner else ''
        to_fund = f'    fund: "{transfer_owner}"\n' if transfer_owner else ''
        root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Bank:A\n'
            '2026-09-01 open Assets:Bank:B\n'
            '2026-09-01 open Equity:Opening\n'
            '2026-09-01 open Expenses:Food\n'
            '2026-09-30 * "opening"\n'
            f'  Assets:Bank:A {total} CNY\n'
            f'  Equity:Opening -{total} CNY\n'
            f'{expense}'
            '2026-10-02 * "move"\n'
            f'  Assets:Bank:A -{transfer} CNY\n'
            f'{from_fund}'
            f'  Assets:Bank:B {transfer} CNY\n'
            f'{to_fund}', encoding="utf-8")
        snapshot = load_financial(root).digest

        def save(path, document):
            path.write_text(yaml.safe_dump(document, allow_unicode=True,
                                           sort_keys=False), encoding="utf-8")

        save(accounts, {"schema": 6, "cutover": CUTOVER.isoformat(),
                        "ledger_snapshot": snapshot, "report_currency": "CNY",
                        "accounts": [{"account": account, "semantics": "H",
                                      "balance_semantics": "real_inventory",
                                      "payment_scope": {"CNY": "payable"},
                                      "reason": "virtual account review"}
                                     for account in ("Assets:Bank:A", "Assets:Bank:B")]})
        save(opening, {"schema": 6, "cutover": CUTOVER.isoformat(),
                       "ledger_snapshot": snapshot,
                       "h_holdings": [{"account": "Assets:Bank:A", "commodity": "CNY",
                                       "lot": "CNY/no-cost", "owner": owner,
                                       "units": amount, "reason": "virtual opening review"}
                                      for owner, amount in slices],
                       "k_openings": []})
        save(ownership, {"schema": 6, "cutover": CUTOVER.isoformat(),
                         "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION,
                         "owners": [{"id": "free", "name": "自由资金", "aliases": []},
                                    {"id": "savings", "name": "储蓄", "aliases": []},
                                    {"id": "living", "name": "生活费", "aliases": []}],
                         "defaults": {"income_owner_id": "free",
                                      "expense_owner_id": "living"}, "claims": []})
        return root, accounts, opening, ownership

    def test_single_reviewed_opening_owner_inherits_unlabeled_transfer(self):
        with TemporaryDirectory() as folder:
            root, accounts, opening, ownership = self.fixture(
                Path(folder), [("free", "100")], "40")
            without = audit_ownership(root, CUTOVER, accounts, ownership)
            self.assertEqual(without["review_state"], "pending")
            self.assertIn("FUND_SOURCE_OPENING_PENDING",
                          {row.get("reason_code") for row in without["missing_h_ownership"]})
            owned = audit_ownership(root, CUTOVER, accounts, ownership, opening)
            self.assertEqual((owned["review_state"], owned["claimed_h_postings"]),
                             ("complete", 2))
            output = StringIO()
            with redirect_stdout(output):
                exit_code = audit_main(["--ledger", str(root), "--cutover", CUTOVER.isoformat(),
                                        "--accounts", str(accounts), "--ownership", str(ownership),
                                        "--opening", str(opening), "--ownership-audit"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.getvalue())["review_state"], "complete")
            transfer = audit_h_transfers(root, CUTOVER, accounts, ownership,
                                         opening=opening)["events"][0]
            self.assertEqual((transfer["status"], transfer["net_allocations"]),
                             ("CERTIFIED", {}))
            positions = audit_h_holdings(root, CUTOVER, date(2026, 10, 31),
                                         accounts, opening, ownership)["positions"]
            self.assertEqual([(row["account"], row["owner_units"])
                              for row in positions], [
                ("Assets:Bank:A", [{"owner_id": "free", "units": "60"}]),
                ("Assets:Bank:B", [{"owner_id": "free", "units": "40"}])])
            flows = audit_monthly_purpose_flows(root, CUTOVER, "2026-10", accounts,
                                                opening, ownership)
            self.assertEqual((flows["status"], flows["net_allocations"]["free"]),
                             ("CERTIFIED", "0"))

    def test_mixed_reviewed_opening_still_requires_partial_source_choice(self):
        with TemporaryDirectory() as folder:
            root, accounts, opening, ownership = self.fixture(
                Path(folder), [("free", "100"), ("savings", "100")], "40")
            owned = audit_ownership(root, CUTOVER, accounts, ownership, opening)
            self.assertEqual(owned["review_state"], "pending")
            self.assertEqual({row.get("reason_code") for row in owned["missing_h_ownership"]},
                             {"FUND_MIXED_SOURCE", "FUND_SOURCE_UNRESOLVED"})

    def test_full_mixed_opening_can_move_with_both_funds_omitted(self):
        with TemporaryDirectory() as folder:
            root, accounts, opening, ownership = self.fixture(
                Path(folder), [("free", "100"), ("savings", "100")], "200")
            owned = audit_ownership(root, CUTOVER, accounts, ownership, opening)
            self.assertEqual(owned["review_state"], "complete")
            transfer = audit_h_transfers(root, CUTOVER, accounts, ownership,
                                         opening=opening)["events"][0]
            self.assertEqual((transfer["status"], transfer["net_allocations"]),
                             ("CERTIFIED", {}))
            self.assertEqual(transfer["to_owner_units"],
                             {"free": "100", "savings": "100"})

    def test_partial_opening_does_not_authorize_inheritance(self):
        with TemporaryDirectory() as folder:
            root, accounts, opening, ownership = self.fixture(
                Path(folder), [("free", "100"), ("savings", "100")], "200")
            document = yaml.safe_load(opening.read_text(encoding="utf-8"))
            document["h_holdings"] = document["h_holdings"][:1]
            opening.write_text(yaml.safe_dump(document, allow_unicode=True,
                                              sort_keys=False), encoding="utf-8")
            owned = audit_ownership(root, CUTOVER, accounts, ownership, opening)
            self.assertEqual(owned["review_state"], "pending")
            self.assertIn("FUND_SOURCE_OPENING_PENDING",
                          {row.get("reason_code") for row in owned["missing_h_ownership"]})

    def test_existing_expense_deficit_does_not_block_unrelated_transfer(self):
        with TemporaryDirectory() as folder:
            root, accounts, opening, ownership = self.fixture(
                Path(folder), [("free", "100"), ("living", "100")], "40",
                debit_living="120", transfer_owner="自由资金")
            owned = audit_ownership(root, CUTOVER, accounts, ownership, opening)
            self.assertEqual(owned["review_state"], "complete")
            positions = audit_h_holdings(root, CUTOVER, date(2026, 10, 31),
                                         accounts, opening, ownership)["positions"]
            account_a = next(row for row in positions if row["account"] == "Assets:Bank:A")
            self.assertEqual(account_a["owner_units"], [
                {"owner_id": "free", "units": "60"},
                {"owner_id": "living", "units": "-20"}])

    def test_voluntary_transfer_cannot_worsen_existing_deficit(self):
        with TemporaryDirectory() as folder:
            root, accounts, opening, ownership = self.fixture(
                Path(folder), [("free", "100"), ("living", "100")], "10",
                debit_living="120", transfer_owner="生活费")
            with self.assertRaises(BudgetError) as caught:
                audit_ownership(root, CUTOVER, accounts, ownership, opening)
            self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")


if __name__ == "__main__":
    unittest.main()

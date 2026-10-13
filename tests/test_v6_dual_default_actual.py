"""Income stays staged until explicitly allocated to the spending purpose."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.monthly_report import summarize_account_purposes
from beancount_budget_tool.v6.payable_cash import audit_payable_cash


CUTOVER = date(2026, 10, 1)
AS_OF = date(2026, 10, 31)


class DualDefaultActualTests(unittest.TestCase):
    def test_income_allocation_expense_deficit_and_outside_cash(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "main.bean"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Bank:Daily\n'
                '2026-10-01 open Assets:Bank:Savings\n'
                '2026-10-01 open Income:Salary\n'
                '2026-10-01 open Income:Gift\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-02 * "工资"\n'
                '  Assets:Bank:Daily 5000 CNY\n'
                '  Income:Salary -5000 CNY\n'
                '2026-10-03 * "预留生活费"\n'
                '  Assets:Bank:Daily 2000 CNY\n'
                '    fund: "生活费"\n'
                '  Assets:Bank:Daily -2000 CNY\n'
                '2026-10-04 * "首次划入储蓄"\n'
                '  Assets:Bank:Daily -700 CNY\n'
                '    fund: "自由资金"\n'
                '  Assets:Bank:Savings 700 CNY\n'
                '    fund: "储蓄"\n'
                '2026-10-05 * "生活费超支"\n'
                '  Expenses:Food 2100 CNY\n'
                '  Assets:Bank:Daily -2100 CNY\n'
                '2026-10-06 * "预算外入账"\n'
                '  Assets:Bank:Daily 30 CNY\n'
                '    fund: "-"\n'
                '  Income:Gift -30 CNY\n'
                '    budget: "-"\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts = base / "accounts.yml"
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "report_currency": "CNY", "ledger_snapshot": snapshot,
                "accounts": [{"account": account, "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable"},
                              "reason": "虚拟账本直接支付现金"}
                             for account in ("Assets:Bank:Daily", "Assets:Bank:Savings")]},
                allow_unicode=True), encoding="utf-8")
            opening = base / "opening.yml"
            opening.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": snapshot, "h_holdings": [], "k_openings": []},
                allow_unicode=True), encoding="utf-8")
            ownership = base / "ownership.yml"
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": ident, "name": name, "aliases": []}
                           for ident, name in (("free", "自由资金"),
                                               ("living", "生活费"),
                                               ("savings", "储蓄"))],
                "defaults": {"income_owner_id": "free",
                             "expense_owner_id": "living"}, "claims": []},
                allow_unicode=True), encoding="utf-8")
            obligations = base / "obligations.yml"
            obligations.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION,
                "owners": [], "events": []}, allow_unicode=True), encoding="utf-8")
            completeness = base / "completeness.yml"
            completeness.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "as_of": AS_OF.isoformat(), "ledger_snapshot": snapshot,
                "unbooked_obligations": "none", "inflight_payments": "none",
                "reason": "虚拟账本无本人债务和在途付款"},
                allow_unicode=True), encoding="utf-8")

            cash = audit_payable_cash(root, CUTOVER, AS_OF, accounts, opening, ownership)
            self.assertEqual(cash["review_state"], "complete")
            self.assertEqual(cash["spendable_owner_ids"], ["living"])
            self.assertEqual(cash["free_cash"], "-100")
            self.assertEqual(cash["outside_budget_cash"], "30")
            self.assertEqual(cash["total_payable_cash"], "2930")
            self.assertEqual({row["owner_id"]: row["units"] for row in cash["owner_units"]},
                             {"-": "30", "free": "2300", "living": "-100", "savings": "700"})
            holdings = audit_h_holdings(root, CUTOVER, AS_OF, accounts, opening, ownership)
            locations = summarize_account_purposes(
                holdings, {"ledger_snapshot": snapshot, "as_of": AS_OF.isoformat(),
                           "pools": []})
            self.assertEqual(locations["status"], "CERTIFIED_H_OUTSIDE_POOLS")
            self.assertEqual({(row["account"], row["owner_id"]): row["units"]
                              for row in locations["rows"]},
                             {("Assets:Bank:Daily", "-"): "30",
                              ("Assets:Bank:Daily", "free"): "2300",
                              ("Assets:Bank:Daily", "living"): "-100",
                              ("Assets:Bank:Savings", "savings"): "700"})
            actual = audit_actual_snapshot(root, CUTOVER, AS_OF, accounts, opening,
                                           ownership, obligations, completeness)
            self.assertEqual(actual["status"], "CERTIFIED")
            self.assertEqual(actual["actual_spendable"], "-100")
            self.assertEqual(actual["outside_budget_cash"], "30")
            pending = audit_actual_snapshot(root, CUTOVER, AS_OF, accounts, opening,
                                            ownership, obligations)
            self.assertEqual(pending["status"], "NEEDS_REVIEW")
            self.assertIsNone(pending["actual_spendable"])
            self.assertIn("EXTERNAL_SCOPE_PENDING", pending["reason_codes"])


if __name__ == "__main__":
    unittest.main()

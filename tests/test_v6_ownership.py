"""H owner claims are exact, while K book-flow legs remain separate."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.ownership import audit_ownership


class OwnershipAuditTests(unittest.TestCase):
    def test_h_split_and_k_exclusion(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            accounts = Path(folder) / "accounts.yml"
            review = Path(folder) / "ownership.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Assets:Fund\n'
                '2026-10-01 open Income:Pay\n'
                '2026-10-02 * "income"\n'
                '  Assets:Cash 20 CNY\n'
                '  Income:Pay -20 CNY\n'
                '2026-10-03 * "fund subscription"\n'
                '  Assets:Cash -10 CNY\n'
                '    fund: "-"\n'
                '  Assets:Fund 10 CNY\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "report_currency": "CNY",
                "ledger_snapshot": snapshot,
                "accounts": [
                    {"account": "Assets:Cash", "semantics": "H",
                     "balance_semantics": "real_inventory",
                     "payment_scope": {"CNY": "pending"}, "reason": "虚拟账本可核现金"},
                    {"account": "Assets:Fund", "semantics": "K",
                     "balance_semantics": "signed_subscription_flow",
                     "payment_scope": {"CNY": "excluded"}, "reason": "虚拟账本累计申赎"}],
            }, allow_unicode=True), encoding="utf-8")
            unclaimed = audit_ownership(root, date(2026, 10, 1), accounts)
            self.assertEqual(unclaimed["h_postings"], 2)
            self.assertEqual(len(unclaimed["k_book_flow_postings"]), 1)
            self.assertEqual(len(unclaimed["missing_h_ownership"]), 1)
            ref = unclaimed["missing_h_ownership"][0]["ref"]
            review.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": "investment", "name": "投资", "aliases": []}],
                "claims": [{"claim": ref, "reason": "虚拟账本用途拆分", "slices": [
                    {"id": "free_half", "owner_id": "-", "role": "hold", "signed_units": "10"},
                    {"id": "invest_half", "owner_id": "investment",
                     "role": "allocation", "signed_units": "10"}]}],
            }, allow_unicode=True), encoding="utf-8")
            audit = audit_ownership(root, date(2026, 10, 1), accounts, review)
            self.assertEqual(audit["review_state"], "complete")
            self.assertEqual(audit["claimed_h_postings"], 2)
            self.assertEqual(len(audit["pending_payment_scope"]), 1)
            self.assertEqual(len(audit["k_book_flow_postings"]), 1)
            changed = yaml.safe_load(review.read_text(encoding="utf-8"))
            changed["claims"][0]["slices"][1]["signed_units"] = "11"
            review.write_text(yaml.safe_dump(changed, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(BudgetError) as error:
                audit_ownership(root, date(2026, 10, 1), accounts, review)
            self.assertEqual(error.exception.code, "OWNERSHIP_UNITS_MISMATCH")


if __name__ == "__main__":
    unittest.main()

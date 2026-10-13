"""A rolling security position can protect principal without fixing a lot."""

from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.cli import main
from beancount_budget_tool.v6.purpose_pools import audit_purpose_pools, opening_fingerprint


LEDGER = '''option "operating_currency" "CNY"
2026-08-01 open Assets:Broker:Cash
2026-08-01 open Assets:Broker:Securities
2026-08-01 open Equity:Opening-Balances
2026-08-01 open Income:Interest
2026-08-01 open Expenses:Fee
2026-08-31 * "opening"
  Assets:Broker:Cash 4000 CNY
  Equity:Opening-Balances -4000 CNY
2026-09-02 * "repo buy"
  Assets:Broker:Securities 30 SEC {100 CNY}
  Assets:Broker:Cash -3000 CNY
2026-09-03 * "repo maturity"
  Assets:Broker:Securities -30 SEC {100 CNY}
  Assets:Broker:Cash 3001 CNY
  Income:Interest -1 CNY
2026-09-04 * "fee"
  Assets:Broker:Cash -2 CNY
  Expenses:Fee 2 CNY
2026-09-04 price SEC 90 CNY
'''


class PurposePoolTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.ledger = folder / "main.bean"
        self.pools = folder / "pools.yml"
        self.ledger.write_text(LEDGER, encoding="utf-8")
        self.write_pool("3000")

    def write_pool(self, amount, accounts=None):
        self.pools.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01",
            "report_currency": "CNY", "pools": [{
                "id": "broker", "accounts": accounts or
                ["Assets:Broker:Cash", "Assets:Broker:Securities"],
                "payment_scope": "excluded",
                "reserve_order": accounts or
                ["Assets:Broker:Cash", "Assets:Broker:Securities"],
                "opening_fingerprint": opening_fingerprint(
                    load_financial(self.ledger), date(2026, 9, 1), accounts or
                    ["Assets:Broker:Cash", "Assets:Broker:Securities"]),
                "protected_owner_id": "savings", "protected_label": "额外储蓄",
                "protected_principal": amount,
                "remainder_owner_id": "investment", "remainder_label": "待投资金",
                "reason": "user protects original savings principal across repo rollovers"}]},
            allow_unicode=True), encoding="utf-8")

    def audit(self):
        return audit_purpose_pools(self.ledger, date(2026, 9, 1), date(2026, 9, 30), self.pools)

    def test_rollover_keeps_nominal_principal_without_lot_assignment(self):
        report = self.audit()
        pool = report["pools"][0]
        self.assertEqual(pool["opening_book_cost"], "4000")
        self.assertEqual(pool["minimum_book_cost_since_cutover"], "3999")
        self.assertEqual(pool["protected_nominal_principal"], "3000")
        self.assertEqual(pool["remainder_book_cost"], "999")
        self.assertIsNone(pool["market_value"])
        self.assertEqual(pool["post_cutover_postings"], 5)
        self.assertEqual(pool["account_purposes"], [
            {"account": "Assets:Broker:Cash", "book_cost": "3999",
             "protected_owner_id": "savings", "protected_label": "额外储蓄",
             "protected_nominal": "3000", "remainder_owner_id": "investment",
             "remainder_label": "待投资金", "remainder_book_cost": "999"},
            {"account": "Assets:Broker:Securities", "book_cost": "0",
             "protected_owner_id": "savings", "protected_label": "额外储蓄",
             "protected_nominal": "0", "remainder_owner_id": "investment",
             "remainder_label": "待投资金", "remainder_book_cost": "0"}])
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--ledger", str(self.ledger), "--cutover", "2026-09-01",
                         "--purpose-pool-audit", "--purpose-pools", str(self.pools),
                         "--as-of", "2026-09-30"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["pools"][0]["remainder_book_cost"], "999")

    def test_shortfall_fails_closed(self):
        self.write_pool("4000")
        with self.assertRaises(BudgetError) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, "POOL_PRINCIPAL_UNCOVERED")

    def test_buy_moves_display_protection_without_a_manual_security_claim(self):
        report = audit_purpose_pools(self.ledger, date(2026, 9, 1), date(2026, 9, 2), self.pools)
        cash, security = report["pools"][0]["account_purposes"]
        self.assertEqual((cash["protected_nominal"], cash["remainder_book_cost"]),
                         ("1000", "0"))
        self.assertEqual((security["protected_nominal"], security["remainder_book_cost"]),
                         ("2000", "1000"))

    def test_omitting_security_account_cannot_cover_buy(self):
        self.write_pool("3000", ["Assets:Broker:Cash"])
        with self.assertRaises(BudgetError) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, "POOL_PRINCIPAL_UNCOVERED")

    def test_changed_source_recomputes_pool_coverage(self):
        changed = LEDGER.replace("Assets:Broker:Cash 4000 CNY", "Assets:Broker:Cash 4100 CNY")
        changed = changed.replace("Equity:Opening-Balances -4000 CNY",
                                  "Equity:Opening-Balances -4100 CNY")
        self.ledger.write_text(changed, encoding="utf-8")
        report = self.audit()
        self.assertEqual(report["pools"][0]["opening_book_cost"], "4100")
        self.assertEqual(report["pools"][0]["remainder_book_cost"], "1099")
        document = yaml.safe_load(self.pools.read_text(encoding="utf-8"))
        del document["pools"][0]["opening_fingerprint"]
        self.pools.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.assertEqual(self.audit()["pools"][0]["remainder_book_cost"], "1099")
        document["pools"][0]["protected_principal"] = "4200"
        self.pools.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, "POOL_PRINCIPAL_UNCOVERED")

    def test_new_import_after_cutover_does_not_expire_policy(self):
        self.ledger.write_text(LEDGER + '2026-09-05 * "new importer fee"\n'
                               '  Assets:Broker:Cash -1 CNY\n'
                               '  Expenses:Fee 1 CNY\n', encoding="utf-8")
        report = self.audit()
        self.assertEqual(report["pools"][0]["remainder_book_cost"], "998")


if __name__ == "__main__":
    unittest.main()

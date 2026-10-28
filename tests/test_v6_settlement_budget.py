"""A virtual card ledger proves budget cost without rewriting USD consumption."""

from datetime import date
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
from beancount.core import data
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.cli import main as v6_main
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.h_fx import audit_h_fx
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.settlement_budget import audit_settlement_budget_values
from beancount_budget_tool.v6.targets import audit_monthly_expense_targets
from beancount_budget_tool.v6.valuation import audit_classification_valuation


CUTOVER = date(2026, 9, 1)


class SettlementBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.ledger = folder / "main.bean"
        self.accounts = folder / "accounts.yaml"
        self.ownership = folder / "ownership.yaml"
        self.obligations = folder / "obligations.yaml"
        self.fx = folder / "fx.yaml"
        self.classes = folder / "classes.yaml"
        self.plans = folder / "plans.yaml"
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Liabilities:Card\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Education\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 1000 CNY\n'
            '  Equity:Opening -1000 CNY\n'
            '2026-09-11 * "subscription"\n'
            '  Expenses:Education 21.76 USD\n'
            '  Liabilities:Card -21.76 USD\n'
            '2026-09-28 * "card payment"\n'
            '  Liabilities:Card 21.76 USD\n'
            '  Assets:Bank -21.76 USD\n'
            '2026-09-28 * "currency purchase"\n'
            '  Assets:Bank 21.76 USD @@ 146.41 CNY\n'
            '  Assets:Bank -146.41 CNY\n', encoding="utf-8")
        fin = load_financial(self.ledger)
        facts = FactIndex(fin)
        refs = {(entry.narration, posting.account, posting.units.currency):
                facts.for_posting(posting).as_dict()
                for entry in fin.entries if isinstance(entry, data.Transaction)
                for posting in entry.postings}
        self.refs = refs
        purchase_expense = refs["subscription", "Expenses:Education", "USD"]
        purchase_debt = refs["subscription", "Liabilities:Card", "USD"]
        payment_debt = refs["card payment", "Liabilities:Card", "USD"]
        payment_asset = refs["card payment", "Assets:Bank", "USD"]
        funding = refs["currency purchase", "Assets:Bank", "USD"]
        cny_debit = refs["currency purchase", "Assets:Bank", "CNY"]
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": fin.digest,
            "accounts": [{"account": "Assets:Bank", "semantics": "H",
                          "balance_semantics": "real_inventory",
                          "payment_scope": {"CNY": "payable", "USD": "excluded"},
                          "reason": "reviewed virtual bank"}],
            "credit_card_whitelist": ["Liabilities:Card"],
            "same_day_settlements": [{"asset_account": "Assets:Bank",
                                      "credit_card_account": "Liabilities:Card",
                                      "debit_ref": payment_asset,
                                      "liability_ref": payment_debt,
                                      "funding_ref": funding,
                                      "reason": "reviewed exact same-day repayment"}]},
            allow_unicode=True), encoding="utf-8")
        missing_h = audit_ownership(self.ledger, CUTOVER, self.accounts)["missing_h_ownership"]
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": fin.digest, "loader_version": BEANCOUNT_VERSION,
            "owners": [], "claims": [{"claim": row["ref"], "reason": "free virtual bank money",
                                     "slices": [{"id": f"h_{i}", "owner_id": "-",
                                                 "role": "hold", "signed_units": row["signed_units"]}]}
                                    for i, row in enumerate(missing_h)]},
            allow_unicode=True), encoding="utf-8")
        self.fx.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": fin.digest, "loader_version": BEANCOUNT_VERSION,
            "events": [{"from_ref": cny_debit, "to_ref": funding,
                        "owner_id": "-", "signed_from_units": "-146.41",
                        "signed_to_units": "21.76", "boundary_role": "preserve",
                        "reason": "actual CNY funding"}]},
            allow_unicode=True), encoding="utf-8")
        self.obligations.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": fin.digest, "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "living", "name": "living", "aliases": []}],
            "events": [
                {"liability_ref": purchase_debt, "counterpart_ref": purchase_expense,
                 "kind": "consumer_purchase", "obligation_id": "subscription",
                 "owner_id": "living", "reason": "living responsibility"},
                {"liability_ref": payment_debt, "counterpart_ref": payment_asset,
                 "kind": "payment", "obligation_id": "subscription",
                 "reason": "payment of same debt"}]},
            allow_unicode=True), encoding="utf-8")
        missing_expense = audit_classification(self.ledger, CUTOVER)["missing_classifications"]
        self.classes.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "loader_version": BEANCOUNT_VERSION,
            "categories": [{"id": "living", "name": "living", "parent": None, "aliases": []},
                           {"id": "education", "name": "education", "parent": "living", "aliases": []}],
            "classifications": [{"claim": row["ref"], "reason": "reviewed subscription",
                                 "slices": [{"id": "education_1", "category_id": "education",
                                             "signed_units": row["signed_units"]}]}
                                for row in missing_expense]}, allow_unicode=True), encoding="utf-8")
        self.plans.write_text('schema: 6\nreport_currency: CNY\nmonths:\n'
                              '  "2026-09":\n    expense_subtree_targets:\n      living: "2000"\n',
                              encoding="utf-8")

    def test_actual_settlement_cost_only_affects_budget_comparison(self):
        historical = audit_classification_valuation(self.ledger, CUTOVER, self.classes, "CNY")
        self.assertEqual(len(historical["missing_historical_value"]), 1)
        self.assertEqual(historical["missing_historical_value"][0]["signed_units"], "544/25")
        values = audit_settlement_budget_values(self.ledger, CUTOVER, "2026-09", self.classes,
                                                "CNY", self.accounts, self.ownership,
                                                self.obligations, self.fx)["values"]
        self.assertEqual(len(values), 1)
        self.assertEqual((values[0]["native_amount"], values[0]["budget_comparison_value"],
                          values[0]["settlement_date"]), ("544/25", "14641/100", "2026-09-28"))
        report = audit_monthly_expense_targets(self.ledger, CUTOVER, "2026-09", self.classes,
                                               self.plans, "CNY", accounts=self.accounts,
                                               ownership=self.ownership, obligations=self.obligations,
                                               h_fx=self.fx)
        self.assertEqual(report["status"], "CERTIFIED")
        living = next(row for row in report["categories"] if row["id"] == "living")
        self.assertEqual((living["subtree_spent"], living["subtree_remaining"]),
                         ("14641/100", "185359/100"))

    def test_without_reviewed_fx_target_remains_pending(self):
        self.fx.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": load_financial(self.ledger).digest,
            "loader_version": BEANCOUNT_VERSION, "events": []}), encoding="utf-8")
        report = audit_monthly_expense_targets(self.ledger, CUTOVER, "2026-09", self.classes,
                                               self.plans, "CNY", accounts=self.accounts,
                                               ownership=self.ownership, obligations=self.obligations,
                                               h_fx=self.fx)
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertEqual(report["foreign_settlement_costs"], [])

    def test_cli_exposes_original_currency_and_actual_payment_cost(self):
        output = StringIO()
        with redirect_stdout(output):
            code = v6_main(["--ledger", str(self.ledger), "--cutover", "2026-09-01",
                            "--target-audit", "--month", "2026-09",
                            "--classifications", str(self.classes),
                            "--plans", str(self.plans), "--report-currency", "CNY",
                            "--accounts", str(self.accounts), "--ownership", str(self.ownership),
                            "--obligations", str(self.obligations), "--h-fx", str(self.fx)])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        proof = report["foreign_settlement_costs"][0]
        self.assertEqual((proof["native_commodity"], proof["native_amount"],
                          proof["budget_comparison_value"], proof["historical_report_value"]),
                         ("USD", "544/25", "14641/100", None))


if __name__ == "__main__":
    unittest.main()

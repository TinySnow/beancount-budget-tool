"""Purpose target declarations never derive intent from actual transfers."""

from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
import json
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.cli import main
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.purpose_targets import load_purpose_targets


OWNERS = {"savings", "travel", "investment"}


class PurposeTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "purpose.yml"

    def write(self, body: str):
        self.path.write_text("schema: 6\nreport_currency: CNY\n" + body,
                             encoding="utf-8")

    def read(self, month="2026-09"):
        return load_purpose_targets(self.path, month, "CNY", OWNERS)

    def test_default_effective_month_and_explicit_no_assessment(self):
        self.write('defaults:\n  allocation_targets:\n    savings: "700"\n'
                   '    travel: not_assessed\n'
                   'effective_templates:\n  "2026-10":\n    allocation_targets:\n'
                   '      savings: "800"\n'
                   'months:\n  "2026-09":\n    allocation_targets:\n'
                   '      investment: "2000"\n')
        september = {row["owner_id"]: row for row in self.read()["owners"]}
        self.assertEqual((september["savings"]["target"],
                          september["travel"]["status"],
                          september["investment"]["target"]),
                         ("700", "NOT_ASSESSED", "2000"))
        october = {row["owner_id"]: row for row in self.read("2026-10")["owners"]}
        self.assertEqual(october["savings"]["target"], "800")
        self.assertEqual(october["investment"]["status"], "MISSING")

    def test_missing_is_not_zero_and_zero_is_a_real_target(self):
        self.write('defaults:\n  allocation_targets:\n    savings: "0"\n')
        report = self.read()
        rows = {row["owner_id"]: row for row in report["owners"]}
        self.assertEqual((rows["savings"]["status"], rows["savings"]["target"]),
                         ("TARGET", "0"))
        self.assertEqual(report["missing_target_ids"], ["investment", "travel"])

    def test_invalid_targets_rejected(self):
        cases = [
            ('months:\n  2026-09:\n    allocation_targets: {}\n', "PLAN_AMBIGUOUS"),
            ('months:\n  "2026-09": {}\n  "2026-09": {}\n', "YAML_DUPLICATE_KEY"),
            ('defaults:\n  allocation_targets:\n    unknown: "5"\n', "PLAN_OWNER_UNKNOWN"),
            ('defaults:\n  allocation_targets:\n    savings: 5\n', "PLAN_AMOUNT_INVALID"),
            ('defaults:\n  allocation_targets:\n    savings: null\n', "PLAN_AMOUNT_INVALID"),
            ('defaults:\n  allocation_targets:\n    savings: "-1"\n', "PLAN_AMOUNT_INVALID"),
            ('defaults:\n  allocation_targets:\n    savings: "01"\n', "PLAN_AMOUNT_INVALID"),
        ]
        for body, code in cases:
            with self.subTest(code=code, body=body):
                self.write(body)
                with self.assertRaises(BudgetError) as caught:
                    self.read()
                self.assertEqual(caught.exception.code, code)

    def test_virtual_ledger_cli_binds_owner_registry_but_not_actual_g(self):
        folder = self.path.parent
        ledger = folder / "main.bean"
        accounts = folder / "accounts.yml"
        ownership = folder / "ownership.yml"
        ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Cash\n'
            '2026-09-01 open Income:Salary\n'
            '2026-09-02 * "salary"\n'
            '  Assets:Cash 100 CNY\n'
            '  Income:Salary -100 CNY\n', encoding="utf-8")
        snapshot = load_financial(ledger).digest
        accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "report_currency": "CNY",
            "ledger_snapshot": snapshot, "accounts": [{
                "account": "Assets:Cash", "semantics": "H",
                "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                "reason": "virtual cash"}]}, allow_unicode=True), encoding="utf-8")
        ref = audit_ownership(ledger, date(2026, 9, 1), accounts)["missing_h_ownership"][0]["ref"]
        ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "ledger_snapshot": snapshot,
            "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "savings", "name": "储蓄", "aliases": []}],
            "claims": [{"claim": ref, "reason": "salary initially free",
                        "slices": [{"id": "salary", "owner_id": "-", "role": "hold",
                                    "signed_units": "100"}]}]},
            allow_unicode=True), encoding="utf-8")
        self.write('months:\n  "2026-09":\n    allocation_targets:\n'
                   '      savings: "700"\n')
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--ledger", str(ledger), "--cutover", "2026-09-01",
                         "--purpose-target-audit", "--accounts", str(accounts),
                         "--ownership", str(ownership), "--plans", str(self.path),
                         "--report-currency", "CNY", "--month", "2026-09"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["ledger_snapshot"], snapshot)
        self.assertEqual(report["owners"], [
            {"owner_id": "savings", "status": "TARGET", "target": "700"}])
        self.assertNotIn("actual_allocation", report)


if __name__ == "__main__":
    unittest.main()

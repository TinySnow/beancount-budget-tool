"""A topic records gross expense and refund without creating budget transfers."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.v6.targets import audit_monthly_expense_targets
from beancount_budget_tool.v6.tracking import audit_monthly_tracking


class MonthlyTrackingTests(unittest.TestCase):
    def test_expense_and_refund_keep_gross_and_net(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Bank\n'
                '2026-10-01 open Expenses:Appliance\n'
                '2026-10-02 * "buy"\n'
                '  tracking: "大额支出"\n'
                '  Expenses:Appliance 800 CNY\n'
                '    budget: "家电"\n'
                '    tracking: "大额支出,家电"\n'
                '  Assets:Bank -800 CNY\n'
                '2026-10-03 * "refund"\n'
                '  Expenses:Appliance -760 CNY\n'
                '    budget: "家电"\n'
                '    tracking: "大额支出,家电"\n'
                '  Assets:Bank 760 CNY\n', encoding="utf-8")
            result = audit_monthly_tracking(ledger, date(2026, 10, 1), "2026-10")
            self.assertEqual(result["topics"], [
                {"topic": "大额支出", "commodity": "CNY", "spent": "800",
                 "returned": "760", "net": "40"},
                {"topic": "家电", "commodity": "CNY", "spent": "800",
                 "returned": "760", "net": "40"}])
            self.assertEqual(len(result["events"]), 4)
            self.assertTrue(all(Path(row["source"].rsplit(":", 1)[0]).resolve() == ledger.resolve()
                                for row in result["events"]))
            classes = Path(folder) / "classes.yaml"
            classes.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01",
                "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "appliance", "name": "家电",
                                "parent": None, "aliases": []}],
                "classifications": []}, allow_unicode=True), encoding="utf-8")
            plans = Path(folder) / "plans.yaml"
            plans.write_text(yaml.safe_dump({
                "schema": 6, "report_currency": "CNY",
                "defaults": {"expense_targets": {"appliance": "1000"}}}),
                encoding="utf-8")
            budget = audit_monthly_expense_targets(
                ledger, date(2026, 10, 1), "2026-10", classes, plans, "CNY")
            self.assertEqual(budget["status"], "CERTIFIED")
            appliance = budget["categories"][0]
            self.assertEqual((appliance["direct_gross_spent"],
                              appliance["direct_refunded"], appliance["direct_spent"],
                              appliance["direct_remaining"]),
                             ("800", "760", "40", "960"))


if __name__ == "__main__":
    unittest.main()

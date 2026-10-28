"""Historical values are proved at the expense event, not inferred later."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.valuation import audit_classification_valuation


class ValuationAuditTests(unittest.TestCase):
    def test_explicit_report_currency_price_precedes_native_units(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            review = Path(folder) / "classification.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-02 * "priced native"\n'
                '  Expenses:Food 10 CNY @ 2 CNY\n'
                '  Assets:Cash -20 CNY\n', encoding="utf-8")
            candidate = audit_classification(root, date(2026, 10, 1))["missing_classifications"][0]
            review.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "food", "name": "餐饮", "parent": None, "aliases": []}],
                "classifications": [{"claim": candidate["ref"], "reason": "虚拟账本审阅", "slices": [
                    {"id": "priced_native", "category_id": "food", "signed_units": "10"}]}],
            }, allow_unicode=True), encoding="utf-8")
            audit = audit_classification_valuation(root, date(2026, 10, 1), review, "CNY")
            self.assertEqual(audit["valued"][0]["historical_report_value"], "20")
            self.assertEqual(audit["valued"][0]["proof"], "source_price_@")

    def test_native_price_and_missing_foreign_value(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            review = Path(folder) / "classification.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-01 open Liabilities:Card\n'
                '2026-10-02 * "native"\n'
                '  Expenses:Food 10 CNY\n'
                '  Assets:Cash -10 CNY\n'
                '2026-10-03 * "priced"\n'
                '  Expenses:Food 3 USD @ 2 CNY\n'
                '  Assets:Cash -6 CNY\n'
                '2026-10-04 * "total priced"\n'
                '  Expenses:Food 2 USD @@ 15 CNY\n'
                '  Assets:Cash -15 CNY\n'
                '2026-10-05 * "card without historical price"\n'
                '  Expenses:Food 1 USD\n'
                '  Liabilities:Card -1 USD\n', encoding="utf-8")
            inventory = audit_classification(root, date(2026, 10, 1))
            refs = [row["ref"] for row in inventory["missing_classifications"]]
            review.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "food", "name": "餐饮", "parent": None, "aliases": []}],
                "classifications": [
                    {"claim": ref, "reason": "虚拟账本审阅", "slices": [
                        {"id": f"slice_{i}", "category_id": "food",
                         "signed_units": inventory["missing_classifications"][i]["signed_units"]}]}
                    for i, ref in enumerate(refs)],
            }, allow_unicode=True), encoding="utf-8")
            audit = audit_classification_valuation(root, date(2026, 10, 1), review, "CNY")
            self.assertEqual(audit["review_state"], "pending")
            self.assertEqual(audit["valued_postings"], 3)
            self.assertEqual([v["historical_report_value"] for v in audit["valued"]],
                             ["10", "6", "15"])
            self.assertEqual([v["proof"] for v in audit["valued"]],
                             ["native_report_currency", "source_price_@", "source_price_@@"])
            self.assertEqual(len(audit["missing_historical_value"]), 1)
            self.assertEqual(audit["missing_historical_value"][0]["reason_code"],
                             "HISTORICAL_VALUE_MISSING")
            self.assertEqual(audit["missing_historical_value"][0]["native_slices"][0]
                             ["signed_units"], "1")
            self.assertIn({"month": "2026-10", "role": "net_expense",
                           "category_id": "food", "commodity": "USD", "signed_units": "6"},
                          audit["native_totals"])

    def test_negative_foreign_refund_keeps_value_sign(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            review = Path(folder) / "classification.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-02 * "refund"\n'
                '  Expenses:Food -1 USD @ 2 CNY\n'
                '  Assets:Cash 2 CNY\n', encoding="utf-8")
            ref = audit_classification(root, date(2026, 10, 1))["missing_classifications"][0]["ref"]
            review.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "food", "name": "餐饮", "parent": None, "aliases": []}],
                "classifications": [{"claim": ref, "reason": "退款审阅", "slices": [
                    {"id": "refund", "category_id": "food", "signed_units": "-1"}]}],
            }, allow_unicode=True), encoding="utf-8")
            audit = audit_classification_valuation(root, date(2026, 10, 1), review, "CNY")
            self.assertEqual(audit["review_state"], "complete")
            self.assertEqual(audit["valued"][0]["historical_report_value"], "-2")


if __name__ == "__main__":
    unittest.main()

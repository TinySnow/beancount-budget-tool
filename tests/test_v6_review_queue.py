"""Importer review lists external decisions but not rolling pool trades."""

from collections import Counter
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.provenance import asset_opening_fingerprint
from beancount_budget_tool.v6.purpose_pools import opening_fingerprint
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.review_queue import audit_review_queue, render_text


CUTOVER = date(2026, 9, 1)


class ReviewQueueTests(unittest.TestCase):
    def test_new_bank_spend_and_pool_boundary_are_reviewed(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            ledger = base / "main.bean"
            ledger.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-08-01 open Assets:Bank\n'
                '2026-08-01 open Assets:Broker:Cash\n'
                '2026-08-01 open Assets:Broker:Securities\n'
                '2026-08-01 open Equity:Opening\n'
                '2026-08-01 open Expenses:Food\n'
                '2026-08-31 * "opening"\n'
                '  Assets:Bank 100 CNY\n'
                '  Assets:Broker:Cash 4000 CNY\n'
                '  Equity:Opening -4100 CNY\n'
                '2026-10-02 * "allocate"\n'
                '  Assets:Bank -20 CNY\n'
                '  Assets:Broker:Cash 20 CNY\n'
                '2026-10-03 * "food"\n'
                '  Expenses:Food 5 CNY\n'
                '  Assets:Bank -5 CNY\n'
                '2026-10-05 * "broker buy"\n'
                '  Assets:Broker:Securities 1 SEC {100 CNY}\n'
                '  Assets:Broker:Cash -100 CNY\n', encoding="utf-8")
            fin = load_financial(ledger)
            snapshot = fin.digest
            accounts = base / "accounts.yaml"
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
                "opening_fingerprint": asset_opening_fingerprint(fin, CUTOVER),
                "report_currency": "CNY", "accounts": [
                    {"account": name, "semantics": "H", "balance_semantics": "real_inventory",
                     "payment_scope": {currency: scope}, "reason": "reviewed account"}
                    for name, currency, scope in (
                        ("Assets:Bank", "CNY", "payable"),
                        ("Assets:Broker:Cash", "CNY", "excluded"),
                        ("Assets:Broker:Securities", "SEC", "excluded"))]},
                allow_unicode=True), encoding="utf-8")
            ownership = base / "ownership.yaml"
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION,
                "owners": [], "claims": []}, allow_unicode=True), encoding="utf-8")
            obligations = base / "obligations.yaml"
            obligations.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION,
                "owners": [], "events": []}, allow_unicode=True), encoding="utf-8")
            classifications = base / "classifications.yaml"
            classifications.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(),
                "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "food", "name": "餐饮", "parent": None,
                                "aliases": []}], "classifications": []},
                allow_unicode=True), encoding="utf-8")
            pool = base / "pool.yaml"
            pool_accounts = ["Assets:Broker:Cash", "Assets:Broker:Securities"]
            pool.write_text(yaml.safe_dump({
                "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
                "pools": [{"id": "broker", "accounts": pool_accounts,
                           "reserve_order": pool_accounts,
                           "opening_fingerprint": opening_fingerprint(fin, CUTOVER, pool_accounts),
                           "payment_scope": "excluded", "protected_owner_id": "savings",
                           "protected_label": "额外储蓄", "protected_principal": "3000",
                           "remainder_owner_id": "investment", "remainder_label": "待投资金",
                           "reason": "reviewed protected principal"}]},
                allow_unicode=True), encoding="utf-8")
            config = base / "report.yaml"
            config.write_text(yaml.safe_dump({
                "schema": 6, "ledger": "main.bean", "cutover": CUTOVER.isoformat(),
                "month": "2026-10",
                "accounts": "accounts.yaml", "ownership": "ownership.yaml",
                "obligations": "obligations.yaml", "classifications": "classifications.yaml",
                "purpose_pools": "pool.yaml"}, allow_unicode=True), encoding="utf-8")
            report = audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 12))
            self.assertEqual(report["counts"], {
                "asset_ownership": 2, "classification": 1, "h_transfer": 1,
                "pool_boundary": 1})
            self.assertEqual(report["suppressed_pool_internal_h_postings"], 2)
            self.assertEqual(report["source_review_state"], "NEEDS_REVIEW")
            self.assertEqual(Counter(row["date"] for row in report["items"]),
                             {"2026-10-02": 3, "2026-10-03": 2})
            food = next(row for row in report["items"] if row["kind"] == "classification")
            self.assertEqual(food["source"], f"{ledger.resolve()}:15")
            self.assertIn(f"{ledger.resolve()}:15", render_text(report))
            self.assertEqual(report["booked_transactions_in_range"], 3)
            month = audit_month_workflow(config)
            self.assertEqual((month["status"], month["stage"]),
                             ("NEEDS_REVIEW", "source"))
            self.assertIsNone(month["report"])
            self.assertIn(f"{ledger.resolve()}:15", render_workflow_text(month))
            quiet = audit_review_queue(config, CUTOVER, date(2026, 9, 30))
            self.assertEqual(quiet["source_review_state"], "NO_SOURCE_ITEMS")
            self.assertIn("最近已记交易日 2026-08-31", render_text(quiet))



if __name__ == "__main__":
    unittest.main()

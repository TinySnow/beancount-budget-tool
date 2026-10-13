"""A tiny reviewed ledger reaches the report branch of the one-command flow."""

from datetime import date
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.monthly_report import (
    audit_month_workflow, main, render_workflow_text,
)
from beancount_budget_tool.v6.purpose_pools import opening_fingerprint
from beancount_budget_tool.tools.v6_review_migration_month import review_month


class MonthWorkflowEndToEndTests(unittest.TestCase):
    def test_reviewed_virtual_month_reaches_scoped_report(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            ledger = base / "main.bean"
            ledger.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-09-01 open Assets:Bank\n'
                '2026-09-01 open Assets:Broker\n'
                '2026-09-01 open Equity:Opening\n'
                '2026-09-01 open Expenses:Food\n'
                '2026-09-30 * "opening"\n'
                '  Assets:Bank 100 CNY\n'
                '  Assets:Broker 10 CNY\n'
                '  Equity:Opening -110 CNY\n'
                '2026-10-02 * "food"\n'
                '  Expenses:Food 10 CNY\n'
                '    budget: "餐饮"\n'
                '    tracking: "十月日常"\n'
                '  Assets:Bank -10 CNY\n', encoding="utf-8")
            snapshot = load_financial(ledger).digest
            cutover = "2026-10-01"
            as_of = "2026-10-31"

            def save(name, value):
                (base / name).write_text(
                    yaml.safe_dump(value, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
                return name

            save("accounts.yaml", {
                "schema": 6, "cutover": cutover, "ledger_snapshot": snapshot,
                "report_currency": "CNY", "accounts": [
                    {"account": account, "semantics": "H",
                     "balance_semantics": "real_inventory",
                     "payment_scope": {"CNY": scope}, "reason": "virtual review"}
                    for account, scope in (("Assets:Bank", "payable"),
                                           ("Assets:Broker", "excluded"))]})
            save("opening.yaml", {
                "schema": 6, "cutover": cutover, "ledger_snapshot": snapshot,
                "h_holdings": [
                    {"account": account, "commodity": "CNY", "lot": "CNY/no-cost",
                     "owner": owner, "units": amount, "reason": "virtual opening"}
                    for account, owner, amount in (("Assets:Bank", "free", "100"),
                                                   ("Assets:Broker", "investment", "10"))],
                "k_openings": []})
            owner_rows = [{"id": ident, "name": name, "aliases": []}
                          for ident, name in (("free", "自由资金"), ("living", "生活费"),
                                              ("savings", "储蓄"), ("investment", "投资"))]
            save("ownership.yaml", {
                "schema": 6, "cutover": cutover, "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION, "owners": owner_rows,
                "defaults": {"income_owner_id": "free", "expense_owner_id": "living"},
                "claims": []})
            save("obligations.yaml", {
                "schema": 6, "cutover": cutover, "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION, "owners": [], "events": []})
            save("classifications.yaml", {
                "schema": 6, "cutover": cutover, "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "food", "name": "餐饮", "parent": None,
                                "aliases": []}], "classifications": []})
            save("expense-plans.yaml", {
                "schema": 6, "report_currency": "CNY",
                "spendable_cap_category_id": "food",
                "defaults": {"expense_subtree_targets": {"food": "0"}}})
            save("purpose-plans.yaml", {
                "schema": 6, "report_currency": "CNY",
                "defaults": {"allocation_targets": {
                    owner["id"]: "not_assessed" for owner in owner_rows}}})
            for name in ("h-transfers", "k-events", "h-fx"):
                save(name + ".yaml", {
                    "schema": 6, "cutover": cutover, "ledger_snapshot": snapshot,
                    "loader_version": BEANCOUNT_VERSION, "events": []})
            pool_fingerprint = opening_fingerprint(
                load_financial(ledger), date(2026, 10, 1), ["Assets:Broker"])
            save("purpose-pools.yaml", {
                "schema": 6, "cutover": cutover, "report_currency": "CNY",
                "pools": [{"id": "broker", "accounts": ["Assets:Broker"],
                           "reserve_order": ["Assets:Broker"],
                           "opening_fingerprint": pool_fingerprint,
                           "payment_scope": "excluded", "protected_owner_id": "savings",
                           "protected_label": "额外储蓄", "protected_principal": "1",
                           "remainder_owner_id": "investment",
                           "remainder_label": "待投资金", "reason": "virtual protection"}]})
            save("completeness.yaml", {
                "schema": 6, "cutover": cutover, "as_of": as_of,
                "ledger_snapshot": snapshot, "unbooked_obligations": "none",
                "inflight_payments": "none", "reason": "virtual month complete"})
            save("month.yaml", {
                "schema": 6, "ledger": "main.bean", "cutover": cutover,
                "month": "2026-10", "report_currency": "CNY",
                **{key: key + ".yaml"
                   for key in ("accounts", "opening", "ownership", "obligations",
                               "classifications", "completeness")},
                **{key: key.replace("_", "-") + ".yaml"
                   for key in ("expense_plans", "purpose_plans", "h_transfers",
                               "k_events", "h_fx", "purpose_pools")}})

            outcome = audit_month_workflow(base / "month.yaml")
            self.assertEqual((outcome["status"], outcome["stage"]),
                             ("CERTIFIED_SCOPED", "report"))
            self.assertEqual(outcome["report"]["actual_spendable"]["actual_spendable"],
                             "-10")
            self.assertEqual(outcome["report"]["max_spendable"]["max_spendable"], "-10")
            self.assertEqual(outcome["report"]["account_purposes"]["rows"], [
                {"account": "Assets:Bank", "commodity": "CNY",
                 "owner_id": "free", "units": "100"},
                {"account": "Assets:Bank", "commodity": "CNY",
                 "owner_id": "living", "units": "-10"}])
            self.assertEqual(outcome["report"]["account_purposes"]
                             ["negative_purpose_positions"], [
                                 {"account": "Assets:Bank", "commodity": "CNY",
                                  "owner_id": "living", "units": "-10"}])
            self.assertIn("Assets:Bank：自由资金 100 CNY", render_workflow_text(outcome))
            self.assertIn("Assets:Bank：生活费 -10 CNY", render_workflow_text(outcome))
            self.assertIn("警告：Assets:Bank 的 生活费 用途份额为 -10 CNY",
                          render_workflow_text(outcome))
            self.assertIn("实际 10，差额 -10 CNY（支出 10，退款／冲回 0）",
                          render_workflow_text(outcome))
            self.assertIn("十月日常：支出 10，退款／冲回 0，净额 10 CNY",
                          render_workflow_text(outcome))
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(base / "month.yaml")])
            self.assertEqual(code, 0)
            self.assertIn("最多能花：-10 CNY", output.getvalue())

            checkpoint = base / "checkpoint" / "main.bean"
            checkpoint.parent.mkdir()
            checkpoint.write_bytes(ledger.read_bytes())
            migrated = review_month(checkpoint, ledger, "2026-10",
                                    date(2026, 10, 1), base / "month.yaml")
            self.assertEqual((migrated["status"], migrated["stage"]),
                             ("CERTIFIED_SCOPED", "report"))
            self.assertTrue(migrated["structural_check"]["financial_facts_equal"])


if __name__ == "__main__":
    unittest.main()

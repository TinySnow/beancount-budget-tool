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
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.ownership import audit_ownership


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
            purpose_rows = {row["owner_id"]: row
                            for row in outcome["report"]["purpose_balances"]["purposes"]}
            self.assertEqual(purpose_rows["free"]["h_native_balances"][0]["units"], "100")
            self.assertEqual(purpose_rows["living"]["h_native_balances"][0]["units"], "-10")
            self.assertEqual(purpose_rows["savings"]["h_native_balances"], [])
            self.assertEqual(purpose_rows["savings"]["pool_book_cost_balances"][0]["units"], "1")
            self.assertEqual(purpose_rows["investment"]["pool_book_cost_balances"][0]["units"], "9")
            self.assertIn("自由资金（默认收入用途）：\n    H 用途余额：100 CNY",
                          render_workflow_text(outcome))
            self.assertIn("生活费（默认支出用途）：", render_workflow_text(outcome))
            self.assertIn("H 用途余额：-10 CNY", render_workflow_text(outcome))
            self.assertIn("储蓄：\n    H 用途余额：0（无余额）\n    证券池账面成本：1 CNY",
                          render_workflow_text(outcome))
            self.assertIn("警告：Assets:Bank 的 生活费 用途份额为 -10 CNY",
                          render_workflow_text(outcome))
            self.assertIn("实际 10，差额 -10 CNY（支出 10，退款／冲回 0）",
                          render_workflow_text(outcome))
            self.assertIn("十月日常：支出 10，退款／冲回 0，净额 10 CNY",
                          render_workflow_text(outcome))

            # A shared review file may grow as the next month is migrated.
            # Rerunning October must keep its complete report and balances.
            ledger.write_text(ledger.read_text(encoding="utf-8") +
                              '2026-11-02 * "next month food"\n'
                              '  Expenses:Food 3 CNY\n'
                              '  Assets:Bank -3 CNY\n', encoding="utf-8")
            future_category = audit_classification(
                ledger, date(2026, 10, 1), base / "classifications.yaml")
            future_ref = future_category["missing_classifications"][-1]["ref"]
            classes = yaml.safe_load((base / "classifications.yaml").read_text(encoding="utf-8"))
            classes["classifications"] = [{"claim": future_ref, "reason": "November review",
                                           "slices": [{"id": "november-food", "category_id": "food",
                                                       "signed_units": "3"}]}]
            save("classifications.yaml", classes)
            owned = audit_ownership(ledger, date(2026, 10, 1), base / "accounts.yaml",
                                    base / "ownership.yaml", base / "opening.yaml")
            owners = yaml.safe_load((base / "ownership.yaml").read_text(encoding="utf-8"))
            owners["claims"] = [{"claim": owned["ownership"][-1]["ref"],
                                 "reason": "November review", "slices": [
                                     {"id": "november-payment", "owner_id": "living",
                                      "role": "hold", "signed_units": "-3"}]}]
            save("ownership.yaml", owners)
            repeated = audit_month_workflow(base / "month.yaml")
            self.assertEqual(repeated["status"], "CERTIFIED_SCOPED")
            self.assertEqual(repeated["report"]["account_purposes"],
                             outcome["report"]["account_purposes"])
            self.assertEqual(repeated["report"]["actual_spendable"]["actual_spendable"], "-10")
            self.assertEqual(repeated["report"]["max_spendable"]["max_spendable"], "-10")

            output = StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(base / "month.yaml")])
            self.assertEqual(code, 0)
            self.assertIn("最多能花：-10 CNY", output.getvalue())

            # A privately enabled loss policy certifies signed nominal balances;
            # it must not turn a deficit into payable cash or a purpose allocation.
            original_ledger = ledger.read_text(encoding="utf-8")
            original_pool = yaml.safe_load((base / "purpose-pools.yaml").read_text(encoding="utf-8"))
            loss_pool = yaml.safe_load(yaml.safe_dump(original_pool))
            loss_pool["pools"][0].update(protected_principal="9", remainder_policy="allow_negative")
            save("purpose-pools.yaml", loss_pool)
            ledger.write_text(original_ledger + '2026-10-03 * "broker fee"\n'
                              '  Assets:Broker -3 CNY\n'
                              '  Expenses:Food 3 CNY\n'
                              '    budget: "餐饮"\n', encoding="utf-8")
            loss = audit_month_workflow(base / "month.yaml")
            self.assertEqual(loss["status"], "CERTIFIED_SCOPED", loss["review_items"])
            self.assertEqual(loss["review_items"], [])
            nominal = {row["owner_id"]: row for row in loss["report"]["purpose_balances"]["purposes"]}
            self.assertEqual(nominal["investment"]["pool_book_cost_balances"][0]["units"], "-2")
            self.assertTrue(nominal["investment"]["has_negative_pool_position"])
            self.assertIn("名义本金账面缺口 2 CNY", render_workflow_text(loss))
            self.assertIn("部分证券池账户的该用途账面余额为负", render_workflow_text(loss))
            self.assertEqual(loss["report"]["actual_spendable"]["actual_spendable"], "-10")
            self.assertEqual(loss["report"]["purpose_progress"]["owners"],
                             outcome["report"]["purpose_progress"]["owners"])
            save("purpose-pools.yaml", original_pool)
            ledger.write_text(original_ledger, encoding="utf-8")

            checkpoint = base / "checkpoint" / "main.bean"
            checkpoint.parent.mkdir()
            checkpoint.write_bytes(ledger.read_bytes())
            migrated = review_month(checkpoint, ledger, "2026-10",
                                    date(2026, 10, 1), base / "month.yaml")
            self.assertEqual((migrated["status"], migrated["stage"]),
                             ("CERTIFIED_SCOPED", "report"))
            self.assertTrue(migrated["structural_check"]["financial_facts_equal"])

            config = base / "month.yaml"
            document = yaml.safe_load(config.read_text(encoding="utf-8"))
            del document["completeness"]
            save("month.yaml", document)
            pending = audit_month_workflow(config)
            self.assertEqual(pending["status"], "NEEDS_REVIEW")
            self.assertIsNone(pending["report"])
            self.assertEqual(len(pending["review_items"]), 1)
            item = pending["review_items"][0]
            self.assertEqual(item["metrics"], ["实际可花", "最多能花"])
            self.assertEqual(item["source"], f"{config.resolve()}:1")
            self.assertIn("本月末的完整性声明", render_workflow_text(pending))
            self.assertNotIn("已审月报", render_workflow_text(pending))

            (base / "expense-plans.yaml").write_text(
                "schema: 6\nreport_currency: CNY\nspendable_cap_category_id: food\n"
                "months:\n  '2026-10': {}\n", encoding="utf-8")
            missing_plan = audit_month_workflow(config)
            items = {row["reason_code"]: row for row in missing_plan["review_items"]}
            self.assertEqual(items["PLAN_MISSING"]["target_id"], "food")
            self.assertIn("餐饮", items["PLAN_MISSING"]["detail"])
            plan_lines = (base / "expense-plans.yaml").read_text(encoding="utf-8").splitlines()
            month_line = plan_lines.index("  '2026-10': {}") + 1
            self.assertEqual(items["PLAN_MISSING"]["source"],
                             f"{(base / 'expense-plans.yaml').resolve()}:{month_line}")


if __name__ == "__main__":
    unittest.main()

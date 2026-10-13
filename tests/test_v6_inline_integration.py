"""Booked virtual ledger exercises both inline slice compilers."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.payable_cash import audit_payable_cash
from beancount_budget_tool.v6.h_transfers import audit_h_transfers
from beancount_budget_tool.v6.purpose_flows import audit_monthly_purpose_flows


class InlineIntegrationTests(unittest.TestCase):
    def test_explicit_fund_with_nonzero_opening_is_not_a_pending_source(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            accounts = Path(folder) / "accounts.yml"
            ownership = Path(folder) / "ownership.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-09-01 open Assets:Bank:Daily\n'
                '2026-09-01 open Income:Salary\n'
                '2026-09-30 * "期初资金"\n'
                '  Assets:Bank:Daily 100 CNY\n'
                '  Income:Salary -100 CNY\n'
                '2026-10-02 * "另有收入"\n'
                '  Assets:Bank:Daily 50 CNY\n'
                '    fund: "自由资金"\n'
                '  Income:Salary -50 CNY\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "report_currency": "CNY",
                "ledger_snapshot": snapshot,
                "accounts": [{"account": "Assets:Bank:Daily", "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable"},
                              "reason": "虚拟账本现金"}]}, allow_unicode=True), encoding="utf-8")
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": "free", "name": "自由资金", "aliases": []}],
                "defaults": {"income_owner_id": "free", "expense_owner_id": "free"},
                "claims": []}, allow_unicode=True), encoding="utf-8")
            report = audit_ownership(root, date(2026, 10, 1), accounts, ownership)
            self.assertEqual(report["review_state"], "complete")
            self.assertEqual(report["unknown_account_semantics"], [])
            self.assertEqual(report["missing_h_ownership"], [])

    def test_example_two_first_allocation_needs_no_transfer_sidecar(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            accounts = Path(folder) / "accounts.yml"
            ownership = Path(folder) / "ownership.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Bank:Daily\n'
                '2026-10-01 open Assets:Bank:Savings\n'
                '2026-10-01 open Income:Salary\n'
                '2026-10-02 * "工资"\n'
                '  Assets:Bank:Daily 1000 CNY\n'
                '  Income:Salary -1000 CNY\n'
                '2026-10-03 * "首次划入储蓄"\n'
                '  Assets:Bank:Daily -700 CNY\n'
                '    fund: "自由资金"\n'
                '  Assets:Bank:Savings 700 CNY\n'
                '    fund: "储蓄"\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "report_currency": "CNY",
                "ledger_snapshot": snapshot,
                "accounts": [{"account": account, "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable"},
                              "reason": "虚拟账本现金"}
                             for account in ("Assets:Bank:Daily", "Assets:Bank:Savings")]},
                allow_unicode=True), encoding="utf-8")
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": "free", "name": "自由资金", "aliases": []},
                           {"id": "savings", "name": "储蓄", "aliases": []}],
                "defaults": {"income_owner_id": "free", "expense_owner_id": "free"},
                "claims": []}, allow_unicode=True), encoding="utf-8")
            events = audit_h_transfers(root, date(2026, 10, 1), accounts, ownership)
            self.assertEqual(events["events"][0]["status"], "CERTIFIED")
            self.assertEqual(events["events"][0]["net_allocations"],
                             {"free": "-700", "savings": "700"})

    def test_cross_account_mixed_fund_remap_needs_separate_virtual_allocation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            accounts = Path(folder) / "accounts.yml"
            ownership = Path(folder) / "ownership.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Bank:A\n'
                '2026-10-01 open Assets:Bank:B\n'
                '2026-10-01 open Income:Salary\n'
                '2026-10-02 * "期内收入"\n'
                '  Assets:Bank:A 200 CNY\n'
                '    fund: "自由资金100，储蓄100"\n'
                '  Income:Salary -200 CNY\n'
                '2026-10-03 * "跨账户混合用途错配"\n'
                '  Assets:Bank:A -200 CNY\n'
                '    fund: "自由资金100，储蓄100"\n'
                '  Assets:Bank:B 200 CNY\n'
                '    fund: "自由资金50，储蓄150"\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "report_currency": "CNY",
                "ledger_snapshot": snapshot,
                "accounts": [{"account": account, "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable"},
                              "reason": "虚拟账本现金"}
                             for account in ("Assets:Bank:A", "Assets:Bank:B")]},
                allow_unicode=True), encoding="utf-8")
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": "free", "name": "自由资金", "aliases": []},
                           {"id": "savings", "name": "储蓄", "aliases": []}],
                "defaults": {"income_owner_id": "free", "expense_owner_id": "free"},
                "claims": []}, allow_unicode=True), encoding="utf-8")
            result = audit_h_transfers(root, date(2026, 10, 1), accounts, ownership)
            self.assertEqual(result["events"][0]["status"], "NEEDS_REVIEW")
            self.assertEqual(result["events"][0]["reason_codes"],
                             ["H_TRANSFER_MIXED_REMAP_PENDING"])

    def test_default_income_then_mixed_partial_transfer_waits_for_choice(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            accounts = Path(folder) / "accounts.yml"
            ownership = Path(folder) / "ownership.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Bank:Daily\n'
                '2026-10-01 open Assets:Bank:Savings\n'
                '2026-10-01 open Income:Salary\n'
                '2026-10-08 * "工资"\n'
                '  Assets:Bank:Daily 5000 CNY\n'
                '  Income:Salary -5000 CNY\n'
                '2026-10-08 * "用作生活费"\n'
                '  Assets:Bank:Daily 2000 CNY\n'
                '    fund: "生活费"\n'
                '  Assets:Bank:Daily -2000 CNY\n'
                '2026-10-09 * "部分划转，未指定来源"\n'
                '  Assets:Bank:Daily -1000 CNY\n'
                '  Assets:Bank:Savings 1000 CNY\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "report_currency": "CNY",
                "ledger_snapshot": snapshot,
                "accounts": [{"account": account, "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable"},
                              "reason": "虚拟账本现金"}
                             for account in ("Assets:Bank:Daily", "Assets:Bank:Savings")]},
                allow_unicode=True), encoding="utf-8")
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": "free", "name": "自由资金", "aliases": []},
                           {"id": "living", "name": "生活费", "aliases": []}],
                "defaults": {"income_owner_id": "free", "expense_owner_id": "living"},
                "claims": []}, allow_unicode=True), encoding="utf-8")
            report = audit_ownership(root, date(2026, 10, 1), accounts, ownership)
            self.assertEqual(report["review_state"], "pending")
            self.assertEqual(report["claimed_h_postings"], 3)
            self.assertEqual(len(report["missing_h_ownership"]), 2)
            self.assertEqual(report["ownership"][0]["origin"], "inferred")
            self.assertEqual({row["reason_code"] for row in report["missing_h_ownership"]},
                             {"FUND_MIXED_SOURCE", "FUND_SOURCE_UNRESOLVED"})

    def test_example_one_infers_source_from_before_same_account_transaction(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            accounts = Path(folder) / "accounts.yml"
            ownership = Path(folder) / "ownership.yml"
            opening = Path(folder) / "opening.yml"
            categories = Path(folder) / "categories.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Bank:Daily\n'
                '2026-10-01 open Income:Salary\n'
                '2026-10-08 * "雇主" "工资"\n'
                '  Assets:Bank:Daily 5000 CNY\n'
                '    fund: "自由资金"\n'
                '  Income:Salary -5000 CNY\n'
                '2026-10-08 * "虚拟账务" "用作生活费"\n'
                '  Assets:Bank:Daily 2000 CNY\n'
                '    fund: "生活费"\n'
                '  Assets:Bank:Daily -2000 CNY\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "report_currency": "CNY",
                "ledger_snapshot": snapshot,
                "accounts": [{"account": "Assets:Bank:Daily", "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable"},
                              "reason": "虚拟账本现金"}]}, allow_unicode=True), encoding="utf-8")
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": "free", "name": "自由资金", "aliases": []},
                           {"id": "living", "name": "生活费", "aliases": []}],
                "defaults": {"income_owner_id": "free", "expense_owner_id": "living"},
                "claims": []}, allow_unicode=True), encoding="utf-8")
            opening.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "h_holdings": [], "k_openings": []}, allow_unicode=True), encoding="utf-8")
            categories.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01",
                "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "free", "name": "自由资金", "parent": None,
                                "aliases": []},
                               {"id": "living", "name": "生活费", "parent": None,
                                "aliases": []}],
                "defaults": {"income_category_id": "free",
                             "expense_category_id": "living"},
                "classifications": []}, allow_unicode=True), encoding="utf-8")
            owned = audit_ownership(root, date(2026, 10, 1), accounts, ownership)
            classified = audit_classification(root, date(2026, 10, 1), categories)
            self.assertEqual(owned["review_state"], "complete")
            self.assertEqual(classified["review_state"], "complete")
            self.assertEqual(classified["classifications"][0]["origin"], "default")
            self.assertEqual([(part["category_id"], part["signed_units"])
                              for part in classified["classifications"][0]["slices"]],
                             [("free", "-5000")])
            self.assertEqual([(row["origin"], row["slices"][0]["owner_id"],
                               row["slices"][0]["signed_units"])
                              for row in owned["ownership"]], [
                                  ("inline", "free", "5000"),
                                  ("inline", "living", "2000"),
                                  ("inferred", "free", "-2000")])
            report = audit_h_holdings(root, date(2026, 10, 1), date(2026, 10, 8),
                                      accounts, opening, ownership)
            self.assertEqual(report["review_state"], "complete")
            self.assertEqual(report["positions"][0]["owner_units"], [
                {"owner_id": "free", "units": "3000"},
                {"owner_id": "living", "units": "2000"}])
            transfers = audit_h_transfers(root, date(2026, 10, 1), accounts, ownership)
            self.assertEqual(transfers["events"][0]["status"], "CERTIFIED")
            self.assertEqual(transfers["events"][0]["net_allocations"],
                             {"free": "-2000", "living": "2000"})
            flows = audit_monthly_purpose_flows(root, date(2026, 10, 1), "2026-10",
                                                accounts, opening, ownership)
            self.assertEqual(flows["status"], "CERTIFIED")
            self.assertEqual(flows["net_allocations"],
                             {"free": "-2000", "living": "2000"})
            cash = audit_payable_cash(root, date(2026, 10, 1), date(2026, 10, 8),
                                      accounts, opening, ownership)
            self.assertEqual(cash["review_state"], "complete")
            self.assertEqual(cash["spendable_owner_ids"], ["living"])
            self.assertEqual(cash["free_cash"], "2000")
            self.assertEqual(cash["outside_budget_cash"], "0")

    def test_multi_purpose_and_budget_compile_from_booked_postings(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            accounts = Path(folder) / "accounts.yml"
            ownership = Path(folder) / "ownership.yml"
            categories = Path(folder) / "categories.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-10-01 open Assets:Bank\n'
                '2026-10-01 open Income:Salary\n'
                '2026-10-02 * "工资"\n'
                '  Assets:Bank 300 CNY\n'
                '    fund: "储蓄100，旅游200"\n'
                '  Income:Salary -300 CNY\n'
                '    budget: "储蓄100，旅游200"\n', encoding="utf-8")
            snapshot = load_financial(root).digest
            accounts.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "report_currency": "CNY",
                "ledger_snapshot": snapshot,
                "accounts": [{"account": "Assets:Bank", "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable"},
                              "reason": "虚拟账本现金"}]}, allow_unicode=True), encoding="utf-8")
            names = [{"id": ident, "name": name, "aliases": []}
                     for ident, name in (("savings", "储蓄"), ("travel", "旅游"))]
            ownership.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "ledger_snapshot": snapshot,
                "loader_version": BEANCOUNT_VERSION, "owners": names, "claims": []},
                allow_unicode=True), encoding="utf-8")
            categories.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01",
                "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": row["id"], "name": row["name"],
                                "parent": None, "aliases": []} for row in names],
                "classifications": []}, allow_unicode=True), encoding="utf-8")
            owned = audit_ownership(root, date(2026, 10, 1), accounts, ownership)
            classified = audit_classification(root, date(2026, 10, 1), categories)
            self.assertEqual(owned["review_state"], "complete")
            self.assertEqual(classified["review_state"], "complete")
            self.assertEqual({p["owner_id"]: p["signed_units"]
                              for p in owned["ownership"][0]["slices"]},
                             {"savings": "100", "travel": "200"})
            self.assertEqual({p["category_id"]: p["signed_units"]
                              for p in classified["classifications"][0]["slices"]},
                             {"savings": "-100", "travel": "-200"})


if __name__ == "__main__":
    unittest.main()

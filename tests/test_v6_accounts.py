"""账户语义只接受人工声明；真实账本的旧标签不影响本阶段盘点。"""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.accounts import audit_accounts


CUTOVER = date(2026, 10, 1)
LEDGER = (
    'option "operating_currency" "CNY"\n'
    '2026-09-01 open Assets:Cash\n'
    '2026-09-01 open Assets:Fund\n'
    '2026-09-01 open Assets:Fund:Child\n'
    '2026-09-01 open Equity:Opening\n'
    '2026-09-02 * "现金期初"\n'
    '  Assets:Cash 1000 CNY\n'
    '  Equity:Opening -1000 CNY\n'
    '2026-09-03 * "父账户直接腿，期初净零"\n'
    '  Assets:Fund 10 CNY\n'
    '  Assets:Fund -10 CNY\n'
    '2026-09-04 * "申购"\n'
    '  Assets:Fund:Child 100 CNY\n'
    '  Assets:Cash -100 CNY\n'
    '2026-10-02 * "赎回超过累计申购，K 可以为负"\n'
    '  Assets:Fund:Child -120 CNY\n'
    '  Assets:Cash 120 CNY\n'
)


class AccountAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main.bean"
        self.root.write_text(LEDGER, encoding="utf-8")

    def declaration(self, digest, *, fund="K", cash_scope="payable", missing=None):
        rows = [
            {"account": "Assets:Cash", "semantics": "H", "balance_semantics": "real_inventory",
             "payment_scope": {"CNY": cash_scope}, "reason": "可核对并可结算的人民币现金"},
            {"account": "Assets:Fund", "semantics": "K",
             "balance_semantics": "signed_subscription_flow",
             "payment_scope": {"CNY": "excluded"}, "reason": "直接记账父账户也需审阅"},
            {"account": "Assets:Fund:Child", "semantics": fund,
             "balance_semantics": "signed_subscription_flow" if fund == "K" else "real_inventory",
             "payment_scope": {"CNY": "excluded"}, "reason": "仅记录累计申购减赎回"},
        ]
        if missing:
            rows = [row for row in rows if row["account"] != missing]
        document = {"schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
                    "ledger_snapshot": digest, "accounts": rows}
        path = Path(self.temp.name) / "accounts.yml"
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return path

    def test_inventory_includes_zero_parent_and_does_not_guess(self):
        result = audit_accounts(self.root, CUTOVER)
        self.assertEqual(result["review_state"], "pending")
        self.assertEqual(result["accounts_with_direct_postings"], 3)
        parent = next(x for x in result["accounts"] if x["account"] == "Assets:Fund")
        self.assertTrue(parent["has_children_with_direct_postings"])
        self.assertEqual(parent["opening_coordinates"][0]["signed_units"], "0")
        self.assertNotIn("semantics", parent)

    def test_independent_semantics_and_payment_scope(self):
        inventory = audit_accounts(self.root, CUTOVER)
        path = self.declaration(inventory["ledger_snapshot"])
        result = audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(result["review_state"], "complete")
        self.assertEqual(result["missing_declarations"], [])
        self.assertEqual(next(x for x in result["accounts"] if x["account"] == "Assets:Fund:Child")
                         ["semantics"], "K")
        self.assertEqual(result["mode"], "v6_account_audit_only")

    def test_missing_declaration_remains_pending(self):
        digest = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        path = self.declaration(digest, missing="Assets:Cash")
        result = audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(result["review_state"], "pending")
        self.assertEqual(result["missing_declarations"], ["Assets:Cash"])

    def test_confirmed_h_semantics_can_wait_for_payment_scope_review(self):
        digest = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        path = self.declaration(digest, cash_scope="pending")
        result = audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(result["review_state"], "pending")
        self.assertEqual(result["missing_declarations"], [])
        self.assertEqual(result["pending_payment_scope"], ["Assets:Cash/CNY"])
        self.assertEqual(next(x for x in result["accounts"] if x["account"] == "Assets:Cash")
                         ["semantics"], "H")

    def test_multicurrency_cash_can_pay_in_report_currency_only(self):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Cash\n'
            '2026-09-01 open Equity:Opening\n'
            '2026-09-02 * "two cash currencies"\n'
            '  Assets:Cash 100 CNY\n'
            '  Assets:Cash 100 JPY @ 1 CNY\n'
            '  Equity:Opening -200 CNY\n', encoding="utf-8")
        inventory = audit_accounts(self.root, CUTOVER)
        self.assertTrue(inventory["accounts"][0]["has_price"])
        digest = inventory["ledger_snapshot"]
        path = Path(self.temp.name) / "accounts.yml"
        document = {"schema": 6, "cutover": CUTOVER.isoformat(),
                    "report_currency": "CNY", "ledger_snapshot": digest,
                    "accounts": [{"account": "Assets:Cash", "semantics": "H",
                                  "balance_semantics": "real_inventory",
                                  "payment_scope": {"CNY": "payable", "JPY": "excluded"},
                                  "reason": "人民币可支付，日元不在报告货币直接支付范围"}]}
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.assertEqual(audit_accounts(self.root, CUTOVER, path)["review_state"], "complete")
        document["accounts"][0]["payment_scope"]["JPY"] = "payable"
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(caught.exception.code, "PAYMENT_SCOPE_INVALID")

    def test_negative_k_cannot_be_relabelled_h(self):
        digest = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        path = self.declaration(digest, fund="H")
        with self.assertRaises(BudgetError) as caught:
            audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(caught.exception.code, "H_NEGATIVE_INVENTORY")

    def test_stale_snapshot_does_not_invalidate_current_account_review(self):
        path = self.declaration("0" * 64)
        self.assertEqual(audit_accounts(self.root, CUTOVER, path)["review_state"], "complete")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        del document["ledger_snapshot"]
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        self.assertEqual(audit_accounts(self.root, CUTOVER, path)["review_state"], "complete")
        self.root.write_text(LEDGER.replace("Assets:Fund:Child", "Assets:Fund:Corrected"),
                             encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(caught.exception.code, "ACCOUNT_UNKNOWN")

    def test_k_cannot_be_payable(self):
        digest = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        path = self.declaration(digest)
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["accounts"][2]["payment_scope"]["CNY"] = "payable"
        path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(caught.exception.code, "PAYMENT_SCOPE_INVALID")

    def test_k_cost_lot_rejected_even_when_balance_positive(self):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Fund\n'
            '2026-09-01 open Equity:Opening\n'
            '2026-09-02 * "有成本的证券不是 K 现金流"\n'
            '  Assets:Fund 1 ABC {10 CNY}\n'
            '  Equity:Opening -10 CNY\n', encoding="utf-8")
        digest = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        path = Path(self.temp.name) / "accounts.yml"
        path.write_text(yaml.safe_dump({"schema": 6, "cutover": CUTOVER.isoformat(),
                                       "report_currency": "CNY", "ledger_snapshot": digest,
                                       "accounts": [{"account": "Assets:Fund", "semantics": "K",
                                                     "balance_semantics": "signed_subscription_flow",
                                                     "payment_scope": {"ABC": "excluded"},
                                                     "reason": "故意的无效声明"}]},
                                      allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(caught.exception.code, "K_SHAPE_INVALID")

    def test_official_pad_postings_do_not_look_like_missing_price_sources(self):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Cash\n'
            '2026-09-01 open Equity:Opening\n'
            '2026-09-01 pad Assets:Cash Equity:Opening\n'
            '2026-09-02 balance Assets:Cash 100 CNY\n', encoding="utf-8")
        inventory = audit_accounts(self.root, CUTOVER)
        self.assertFalse(inventory["accounts"][0]["source_unavailable"])
        path = Path(self.temp.name) / "accounts.yml"
        path.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": inventory["ledger_snapshot"],
            "accounts": [{"account": "Assets:Cash", "semantics": "H",
                          "balance_semantics": "real_inventory",
                          "payment_scope": {"CNY": "payable"},
                          "reason": "官方 pad 形成的可核对人民币现金"}],
        }, allow_unicode=True), encoding="utf-8")
        self.assertEqual(audit_accounts(self.root, CUTOVER, path)["review_state"], "complete")

    def test_same_day_later_inflow_does_not_erase_earlier_negative_h(self):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-09-01 open Assets:Cash\n'
            '2026-09-01 open Expenses:Food\n'
            '2026-09-01 open Income:Gift\n'
            '2026-10-02 * "spend first"\n'
            '  Expenses:Food 10 CNY\n'
            '  Assets:Cash -10 CNY\n'
            '2026-10-02 * "receive later"\n'
            '  Assets:Cash 10 CNY\n'
            '  Income:Gift -10 CNY\n', encoding="utf-8")
        digest = audit_accounts(self.root, CUTOVER)["ledger_snapshot"]
        path = Path(self.temp.name) / "accounts.yml"
        path.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": digest,
            "accounts": [{"account": "Assets:Cash", "semantics": "H",
                          "balance_semantics": "real_inventory",
                          "payment_scope": {"CNY": "pending"},
                          "reason": "可核对真实现金，但仍按 booked 顺序审阅"}],
        }, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_accounts(self.root, CUTOVER, path)
        self.assertEqual(caught.exception.code, "H_NEGATIVE_INVENTORY")


if __name__ == "__main__":
    unittest.main()

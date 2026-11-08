"""Account-level fund declarations cover recurring securities cash flows."""

from pathlib import Path
from datetime import date
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple, build_runtime
from beancount_budget_tool.v6.ownership import audit_ownership

HEADER = '''option "operating_currency" "CNY"
2026-08-01 open Assets:Galaxy:Cash
2026-08-01 open Assets:Galaxy:Sec
2026-08-01 open Income:Gain
2026-08-01 open Expenses:Fee
2026-08-01 open Equity:Opening
2026-08-31 * "opening"
  Assets:Galaxy:Cash 1100 CNY
  Equity:Opening -1100 CNY
'''
BUY = '''2026-09-02 * "buy"
  Assets:Galaxy:Sec 100 SEC_ETF {1 CNY}
  Assets:Galaxy:Cash -100.10 CNY
  Expenses:Fee 0.10 CNY
'''
SELL = '''2026-09-03 * "sell"
  Assets:Galaxy:Sec -100 SEC_ETF {1 CNY, 2026-09-02} @ 1.2 CNY
  Assets:Galaxy:Cash 120 CNY
  Income:Gain -20 CNY
'''
EXTRA = '''2026-09-04 * "extra"
  Assets:Galaxy:Sec 10 SEC_ETF {1 CNY}
    fund: "额外储蓄"
  Assets:Galaxy:Cash -10.01 CNY
    fund: "额外储蓄"
  Expenses:Fee 0.01 CNY
'''


class AccountDefaultFundTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.ledger = self.base / "main.bean"
        self.config = self.base / "config.yml"
        self.config_doc = {
            "schema": 1, "ledger": "main.bean", "budgets": "budgets.yml",
            "cutover": "2026-09-01", "month": "2026-09", "report_currency": "CNY",
            "defaults": {"income_purpose": "自由资金", "expense_purpose": "生活费",
                         "expense_category": "生活开销"},
            "account_groups": {"H": {"excluded": ["Assets:Galaxy:Cash", "Assets:Galaxy:Sec"]}},
            "accounts": {
                "Assets:Galaxy:Cash": {"fund": "投资",
                    "opening": {"CNY": {"投资": "1000", "额外储蓄": "100"}}},
                "Assets:Galaxy:Sec": {"fund": "投资"},
            },
        }
        (self.base / "budgets.yml").write_text(yaml.safe_dump({
            "schema": 1, "defaults": {"expense_targets": {"生活开销": "100"}},
            "months": {"2026-09": {"completeness_confirmed": True}},
        }, allow_unicode=True), encoding="utf-8")

    def audit(self, extra="", *, config=None):
        self.ledger.write_text(HEADER + BUY + SELL + extra, encoding="utf-8")
        self.config.write_text(yaml.safe_dump(config or self.config_doc, allow_unicode=True),
                               encoding="utf-8")
        return audit_simple(self.config, "2026-09", include_transactions=True)

    def test_default_fund_covers_securities_without_per_trade_tags(self):
        result = self.audit()
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertNotIn("OWNERSHIP_UNREVIEWED",
                         {row["reason_code"] for row in result["review_items"]})
        report = result["report"]
        self.assertTrue(report["transactions"]["rows"])
        events = [event for row in report["transactions"]["rows"]
                  for event in row.get("purpose_events", [])
                  if event.get("kind") == "account_default_internal"]
        self.assertTrue(events)
        for event in events:
            proof = event["protocol"]
            self.assertEqual(proof["protocol"], "account_default_internal_v1")
            self.assertEqual(proof["shape"], "securities_cost_or_price")
            self.assertTrue(proof["asset_refs"])
            self.assertTrue(proof["income_expense_refs"])
            self.assertTrue(proof["proof"]["has_cost_or_price"])
            self.assertTrue(event["source_refs"])

    def test_explicit_inline_fund_overrides_the_account_default(self):
        result = self.audit(EXTRA)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        report = result["report"]
        balances = {(row["account"], report["owner_names"][row["owner_id"]]): row["units"]
                    for row in report["account_purposes"]["rows"]}
        self.assertEqual(balances.get(("Assets:Galaxy:Sec", "额外储蓄")), "10")
        self.assertEqual(balances.get(("Assets:Galaxy:Cash", "额外储蓄")), "8999/100")

    def test_default_fund_does_not_swallow_pure_income_allocation(self):
        explicit = '''2026-09-01 * "income"
  Assets:Galaxy:Cash 10 CNY
    fund: "投资"
  Income:Gain -10 CNY
'''
        default = '''2026-09-01 * "income default"
  Assets:Galaxy:Cash 10 CNY
  Income:Gain -10 CNY
'''
        def allocations(extra):
            result = self.audit(extra)
            self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
            report = result["report"]
            return {report["owner_names"][row["owner_id"]]: row["actual_g"]
                    for row in report["purpose_progress"]["owners"]}
        explicit_g = allocations(explicit)
        default_g = allocations(default)
        self.assertEqual(explicit_g, default_g)
        self.assertEqual(default_g.get("投资"), "10")
        self.assertEqual(default_g.get("自由资金"), "-10")

    def test_net_receipt_with_income_and_expense_is_not_internal(self):
        extra = '''2026-09-05 * "net receipt"
  Assets:Galaxy:Cash 10 CNY
  Income:Gain -20 CNY
  Expenses:Fee 10 CNY
'''
        result = self.audit(extra)
        self.assertIn(result["status"], ("NEEDS_REVIEW", "IMPLEMENTATION_BLOCKED"))
        self.assertIsNone(result["report"])
        self.assertIn("NAMED_H_EVENT_UNSUPPORTED",
                      {row["reason_code"] for row in result["review_items"]})

    def test_default_fund_is_rejected_on_k_accounts(self):
        document = dict(self.config_doc)
        document["account_groups"] = {"H": {"excluded": ["Assets:Galaxy:Cash"]},
                                      "K": ["Assets:Galaxy:Sec"]}
        document["accounts"] = {"Assets:Galaxy:Sec": {"fund": "投资"}}
        with self.assertRaises(BudgetError) as caught:
            self.audit(config=document)
        self.assertEqual(caught.exception.code, "SIMPLE_ACCOUNT_INVALID")

    def test_group_h_future_or_open_only_account_need_not_repeat_semantics(self):
        ledger = self.base / "grouped.bean"
        ledger.write_text('''option "operating_currency" "CNY"
2026-08-01 open Assets:Cash
2026-08-01 open Assets:Sec
2026-08-01 open Income:Gain
2026-08-01 open Equity:Opening
2026-08-31 * "opening"
  Assets:Cash 100 CNY
  Equity:Opening -100 CNY
2026-09-02 * "income"
  Assets:Cash 10 CNY
  Income:Gain -10 CNY
''', encoding="utf-8")
        config = self.base / "grouped.yml"
        config.write_text(yaml.safe_dump({
            "schema": 1, "ledger": "grouped.bean", "budgets": "grouped-budgets.yml",
            "cutover": "2026-09-01", "month": "2026-09", "report_currency": "CNY",
            "defaults": {"income_purpose": "自由资金", "expense_purpose": "生活费",
                         "expense_category": "生活开销"},
            "account_groups": {"H": {"excluded": ["Assets:Cash", "Assets:Sec"]}},
            "accounts": {"Assets:Cash": {"opening": {"CNY": {"投资": "100"}}},
                         "Assets:Sec": {"fund": "投资"}},
        }, allow_unicode=True), encoding="utf-8")
        (self.base / "grouped-budgets.yml").write_text(yaml.safe_dump({
            "schema": 1, "months": {"2026-09": {"completeness_confirmed": True}},
        }, allow_unicode=True), encoding="utf-8")
        result = audit_simple(config, "2026-09", include_transactions=True)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])

    def test_account_defaults_fund_registers_a_new_purpose(self):
        ledger = self.base / "defaults.bean"
        ledger.write_text('''option "operating_currency" "CNY"
2026-08-01 open Assets:Cash
2026-08-01 open Income:Salary
2026-08-01 open Equity:Opening
2026-08-31 * "opening"
  Assets:Cash 100 CNY
  Equity:Opening -100 CNY
2026-09-02 * "salary"
  Assets:Cash 10 CNY
  Income:Salary -10 CNY
''', encoding="utf-8")
        config = self.base / "defaults.yml"
        config.write_text(yaml.safe_dump({
            "schema": 1, "ledger": "defaults.bean", "budgets": "defaults-budgets.yml",
            "cutover": "2026-09-01", "month": "2026-09", "report_currency": "CNY",
            "defaults": {"income_purpose": "自由资金", "expense_purpose": "生活费",
                         "expense_category": "生活开销"},
            "account_groups": {"H": {"excluded": ["Assets:Future"]}},
            "account_defaults": {"semantics": "H", "payment_scope": {"CNY": "payable"},
                                 "opening": {"CNY": {"投资": "100"}}, "fund": "全新用途"},
        }, allow_unicode=True), encoding="utf-8")
        (self.base / "defaults-budgets.yml").write_text(yaml.safe_dump({
            "schema": 1, "months": {"2026-09": {"completeness_confirmed": True}},
        }, allow_unicode=True), encoding="utf-8")
        result = audit_simple(config, "2026-09", include_transactions=True)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])

    def test_fund_on_income_account_is_rejected(self):
        document = dict(self.config_doc)
        document["accounts"] = {**self.config_doc["accounts"],
                                "Income:Gain": {"fund": "投资"}}
        with self.assertRaises(BudgetError) as caught:
            self.audit(config=document)
        self.assertEqual(caught.exception.code, "SIMPLE_ACCOUNT_INVALID")

    def test_fund_on_future_k_account_is_rejected(self):
        document = dict(self.config_doc)
        document["accounts"] = {**self.config_doc["accounts"],
                                "Assets:Future": {"semantics": "K", "fund": "投资"}}
        with self.assertRaises(BudgetError) as caught:
            self.audit(config=document)
        self.assertEqual(caught.exception.code, "SIMPLE_ACCOUNT_INVALID")

    def test_fund_on_future_h_account_is_allowed(self):
        document = dict(self.config_doc)
        document["accounts"] = {**self.config_doc["accounts"],
                                "Assets:Future": {"semantics": "H", "fund": "投资"}}
        result = self.audit(config=document)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])

    def test_invalid_fund_name_is_rejected(self):
        document = dict(self.config_doc)
        document["accounts"] = {
            "Assets:Galaxy:Cash": {"fund": "投资|其他"},
            "Assets:Galaxy:Sec": {"fund": "投资"},
        }
        with self.assertRaises(BudgetError) as caught:
            self.audit(config=document)
        self.assertEqual(caught.exception.code, "OWNER_NAME_INVALID")

    def test_external_claim_overrides_default_without_reason_text_changing_its_origin(self):
        self.audit()
        self.ledger.write_text(HEADER + BUY, encoding="utf-8")
        runtime = build_runtime(self.config, materialize=True)
        try:
            document = yaml.safe_load(runtime.config.read_text(encoding="utf-8"))
            paths = {key: runtime.config.parent / document[key]
                     for key in ("ledger", "accounts", "ownership", "opening")}
            def ownership():
                return audit_ownership(paths["ledger"], date(2026, 9, 1), paths["accounts"],
                                       paths["ownership"], paths["opening"])
            initial = ownership()
            self.assertTrue(all(row["origin"] == "account_default" for row in initial["ownership"]))
            declarations = yaml.safe_load(paths["ownership"].read_text(encoding="utf-8"))
            extra = next(row["id"] for row in declarations["owners"] if row["name"] == "额外储蓄")
            security = next(row for row in initial["ownership"] if row["account"] == "Assets:Galaxy:Sec")
            declarations["claims"] = [{"claim": security["ref"],
                "reason": "人工审核；账户级默认用途：此处只是备注文字",
                "slices": [{"id": "reviewed_security", "owner_id": extra,
                            "role": "hold", "signed_units": "100"}]}]
            paths["ownership"].write_text(yaml.safe_dump(declarations, allow_unicode=True), encoding="utf-8")
            reviewed = ownership()
            security = next(row for row in reviewed["ownership"] if row["account"] == "Assets:Galaxy:Sec")
            self.assertEqual(security["origin"], "external")
            self.assertEqual(security["slices"][0]["owner_id"], extra)
            cash = next(row for row in reviewed["ownership"] if row["account"] == "Assets:Galaxy:Cash")
            self.assertEqual(cash["origin"], "account_default")
        finally:
            runtime.close()

    def test_a_later_sale_cannot_fund_an_earlier_purchase_from_another_owner(self):
        document = {**self.config_doc, "accounts": {**self.config_doc["accounts"],
            "Assets:Galaxy:Cash": {"fund": "投资", "opening": {"CNY": {"额外储蓄": "1100"}}}}}
        with self.assertRaises(BudgetError) as caught:
            self.audit(config=document)
        self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")
        self.assertIn("100 CNY", caught.exception.message)

    def test_only_fee_may_overdraw_a_purpose_during_a_purchase(self):
        document = {**self.config_doc, "accounts": {**self.config_doc["accounts"],
            "Assets:Galaxy:Cash": {"fund": "投资",
                "opening": {"CNY": {"投资": "100", "额外储蓄": "1000"}}}}}
        result = self.audit(config=document)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])

    def test_default_expense_purpose_counts_only_trade_fee_not_investment_principal(self):
        document = {**self.config_doc,
                    "defaults": {**self.config_doc["defaults"], "expense_purpose": "投资"}}
        result = self.audit(config=document)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        totals = result["report"]["default_outflows"]["totals"]
        self.assertEqual(totals[0]["expenses_paid"], "1/10")
        self.assertEqual(totals[0]["gross_outflow"], "1/10")

    def test_multi_cash_fee_cannot_cover_a_different_accounts_principal_shortfall(self):
        from copy import deepcopy
        for principal in ("99.90", "100"):
            with self.subTest(principal=principal):
                document = deepcopy(self.config_doc)
                document["defaults"]["expense_purpose"] = "投资"
                document["account_groups"]["H"]["excluded"].append("Assets:FeeCash")
                document["accounts"]["Assets:Galaxy:Cash"]["opening"] = {
                    "CNY": {"投资": principal, **({"额外储蓄": "0.10"} if principal == "99.90" else {})}}
                document["accounts"]["Assets:FeeCash"] = {
                    "fund": "投资", "opening": {"CNY": {"投资": "100"}}}
                header = HEADER.replace("2026-08-01 open Income:Gain",
                    "2026-08-01 open Assets:FeeCash\n2026-08-01 open Income:Gain")
                header = header.replace("  Assets:Galaxy:Cash 1100 CNY", "  Assets:Galaxy:Cash 100 CNY\n  Assets:FeeCash 100 CNY")
                header = header.replace("Equity:Opening -1100 CNY", "Equity:Opening -200 CNY")
                buy = BUY.replace("  Assets:Galaxy:Cash -100.10 CNY",
                                  "  Assets:Galaxy:Cash -100 CNY\n  Assets:FeeCash -0.10 CNY")
                self.ledger.write_text(header + buy, encoding="utf-8")
                self.config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
                if principal == "99.90":
                    with self.assertRaises(BudgetError) as caught:
                        audit_simple(self.config)
                    self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")
                else:
                    result = audit_simple(self.config)
                    self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
                    self.assertEqual(result["report"]["default_outflows"]["totals"][0]["expenses_paid"], "1/10")

    def test_netted_sale_fee_is_an_expense_but_not_a_separate_cash_payment(self):
        self.audit()
        document = {**self.config_doc,
                    "defaults": {**self.config_doc["defaults"], "expense_purpose": "投资"}}
        sell = SELL.replace("  Assets:Galaxy:Cash 120 CNY", "  Assets:Galaxy:Cash 119.90 CNY")
        sell += "  Expenses:Fee 0.10 CNY\n"
        self.ledger.write_text(HEADER + BUY + sell, encoding="utf-8")
        self.config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertEqual(result["report"]["default_outflows"]["totals"][0]["expenses_paid"], "1/10")
        self.assertEqual(result["report"]["multicurrency"]["native_totals"][0]["net"], "1/5")

    def test_new_income_alongside_a_purchase_is_not_silently_treated_as_disposal_income(self):
        result = self.audit('''2026-09-04 * "salary and purchase"
  Assets:Galaxy:Sec 10 SEC_ETF {1 CNY}
  Assets:Galaxy:Cash -9 CNY
  Income:Gain -1 CNY
''')
        self.assertIsNone(result["report"])
        self.assertIn("NAMED_H_EVENT_UNSUPPORTED", {row["reason_code"] for row in result["review_items"]})

    def test_different_owners_in_a_security_trade_still_need_a_boundary_protocol(self):
        result = self.audit(BUY.replace('"buy"', '"different owners"')
            .replace("100 SEC_ETF", "1 SEC_ETF").replace("-100.10 CNY", "-1.01 CNY")
            .replace("0.10 CNY", "0.01 CNY")
            .replace("  Assets:Galaxy:Sec 1 SEC_ETF {1 CNY}\n",
                     '  Assets:Galaxy:Sec 1 SEC_ETF {1 CNY}\n    fund: "额外储蓄"\n'))
        self.assertIsNone(result["report"])
        self.assertIn("NAMED_H_EVENT_UNSUPPORTED", {row["reason_code"] for row in result["review_items"]})


if __name__ == "__main__":
    unittest.main()

"""Default-purpose payments are cash movements, not another expense charge."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from beancount.core import data
import yaml

from beancount_budget_tool.financial import booked_through, load_financial
from beancount_budget_tool.tools.v6_init import create_setup, compile_setup
from beancount_budget_tool.v6.default_outflows import audit_default_outflows
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.review_diagnostics import review_item


CUTOVER = date(2026, 9, 1)
LAST = date(2026, 9, 30)
HEADER = (
    'option "operating_currency" "CNY"\n'
    '2026-08-01 open Assets:Cash\n'
    '2026-08-01 open Assets:Bank\n'
    '2026-08-01 open Assets:FundBook\n'
    '2026-08-01 open Equity:Opening\n'
    '2026-08-01 open Income:Reward\n'
    '2026-08-01 open Expenses:Food\n'
    '2026-08-01 open Liabilities:Card\n'
    '2026-08-01 open Liabilities:Clearing\n'
    '2026-08-31 * "opening"\n'
    '  Assets:Cash 1000 CNY\n'
    '  Equity:Opening -1000 CNY\n')
TRANSACTIONS = (
    '2026-09-02 * "mixed expense"\n'
    '  Expenses:Food 50 CNY\n'
    '  Assets:Cash -50 CNY\n    fund: "生活费20,储蓄30"\n'
    '2026-09-03 * "refund"\n'
    '  Expenses:Food -5 CNY\n'
    '  Assets:Cash 5 CNY\n'
    '2026-09-04 * "card purchase"\n'
    '  Expenses:Food 100 CNY\n'
    '  Liabilities:Card -100 CNY\n'
    '2026-09-05 * "card payment"\n'
    '  Liabilities:Card 40 CNY\n'
    '  Assets:Cash -40 CNY\n    fund: "生活费"\n'
    '2026-09-06 * "bank transfer"\n'
    '  Assets:Cash -100 CNY\n    fund: "生活费"\n'
    '  Assets:Bank 100 CNY\n    fund: "生活费"\n'
    '2026-09-07 * "virtual reallocation"\n'
    '  Assets:Cash -30 CNY\n    fund: "生活费"\n'
    '  Assets:Cash 30 CNY\n    fund: "储蓄"\n'
    '2026-09-08 * "fund subscription"\n'
    '  Assets:Cash -20 CNY\n    fund: "生活费"\n'
    '  Assets:FundBook 20 CNY\n    fund: "生活费"\n'
    '2026-09-09 * "advance"\n'
    '  Liabilities:Clearing 50 CNY\n'
    '  Assets:Cash -50 CNY\n    fund: "生活费"\n'
    '2026-09-10 * "reimbursement"\n'
    '  Liabilities:Clearing -30 CNY\n'
    '  Assets:Cash 30 CNY\n    fund: "生活费"\n'
    '2026-09-11 * "excluded category payment"\n'
    '  Expenses:Food 10 CNY\n    budget: "-"\n'
    '  Assets:Cash -10 CNY\n    fund: "生活费"\n'
    '2026-09-12 * "outside purpose payment"\n'
    '  Expenses:Food 10 CNY\n'
    '  Assets:Cash -10 CNY\n    fund: "-"\n')


class DefaultOutflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ledger = self.base / "main.bean"
        self.ledger.write_text(HEADER + TRANSACTIONS, encoding="utf-8")

    def save(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def configure(self):
        setup = Path(create_setup(self.ledger, "2026-09", self.base / "setup", currency="CNY",
                                 income_purpose="自由资金", expense_purpose="生活费",
                                 expense_category="生活开销")["setup"])
        worksheet = yaml.safe_load(setup.read_text(encoding="utf-8"))
        worksheet["defaults"]["confirmed"] = True
        worksheet["completeness_confirmed"] = True
        for row in worksheet["accounts"]:
            row["semantics"] = "K" if row["account"] == "Assets:FundBook" else "H"
            row["payment_scope"] = {commodity: "payable" if commodity == "CNY" and
                                      row["semantics"] == "H" and not row["account"].startswith("Assets:Broker") else "excluded"
                                    for commodity in row["payment_scope"]}
            row["reason"] = "virtual reviewed cash or book-flow account"
            for coordinate in row["opening"]:
                coordinate["owners"] = ({"投资": coordinate["signed_units"]}
                                        if row["account"].startswith("Assets:Broker") else
                                        {"生活费": "850", "储蓄": "100", "-": "50"}
                                        if coordinate["commodity"] == "CNY"
                                        else {"生活费": coordinate["signed_units"]})
        worksheet["expense_targets"] = {"生活开销": "500"}
        worksheet["allocation_targets"] = {name: "not_assessed" for name in worksheet["allocation_targets"]}
        if any(row["account"].startswith("Assets:Broker") for row in worksheet["accounts"]):
            worksheet["allocation_targets"]["投资"] = "not_assessed"
        self.save(setup, worksheet)
        compiled = compile_setup(setup)
        self.config = Path(compiled["config"])
        self.folder = self.config.parent
        self.accounts, self.owners, self.opening, self.debts = [
            self.folder / (name + ".yaml") for name in ("accounts", "ownership", "opening", "obligations")]
        owner_doc = yaml.safe_load(self.owners.read_text(encoding="utf-8"))
        self.owner = owner_doc["defaults"]["expense_owner_id"]
        document = yaml.safe_load(self.debts.read_text(encoding="utf-8"))
        document["events"] = []
        with booked_through(LAST):
            fin = load_financial(self.ledger)
            facts = FactIndex(fin)
            for entry in fin.entries:
                if not isinstance(entry, data.Transaction):
                    continue
                name = entry.narration
                if name.startswith("card purchase"):
                    index, kind = 1, "consumer_purchase"
                elif name.startswith("card payment"):
                    index, kind = 0, "payment"
                elif name in ("advance", "reimbursement"):
                    index, kind = 0, "advance_outflow" if name == "advance" else "advance_reimbursement"
                else:
                    continue
                item = {"liability_ref": facts.for_posting(entry.postings[index]).as_dict(),
                        "counterpart_ref": facts.for_posting(entry.postings[1 - index]).as_dict(),
                        "kind": kind, "reason": "virtual responsibility review"}
                if name in ("advance", "reimbursement"):
                    item["clearing_id"] = "employer"
                else:
                    item["obligation_id"] = "card_usd" if name.endswith("USD") else "card_cny"
                    if kind == "consumer_purchase":
                        item["owner_id"] = self.owner
                document["events"].append(item)
        self.save(self.debts, document)

    def audit(self):
        return audit_default_outflows(self.ledger, CUTOVER, "2026-09", self.accounts,
                                      self.owners, self.opening, self.debts)

    def test_mixed_funding_refunds_partial_card_payment_and_advances_are_separate(self):
        self.configure()
        result = self.audit()
        self.assertEqual(result["status"], "CERTIFIED")
        cny = result["totals"][0]
        self.assertEqual({key: cny[key] for key in ("expenses_paid", "expense_refunds", "debt_payments",
                          "advances_paid", "advance_reimbursements", "gross_outflow", "gross_inflow", "net_outflow")},
                         {"expenses_paid": "30", "expense_refunds": "5", "debt_payments": "40",
                          "advances_paid": "50", "advance_reimbursements": "30",
                          "gross_outflow": "120", "gross_inflow": "35", "net_outflow": "85"})
        names = {row["narration"] for row in result["events"]}
        self.assertNotIn("card purchase", names)
        self.assertNotIn("bank transfer", names)
        self.assertNotIn("virtual reallocation", names)
        self.assertNotIn("fund subscription", names)
        self.assertNotIn("outside purpose payment", names)
        self.assertIn("excluded category payment", names)
        self.assertTrue(all(row["source"] and row["asset_ref"] and row["counterpart_refs"] for row in result["events"]))

    def test_monthly_text_json_and_tui_source_share_the_projection_without_double_deduction(self):
        self.configure()
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(outcome["report"]["default_outflows"]["totals"][0]["net_outflow"], "85")
        self.assertEqual(outcome["report"]["actual_spendable"]["actual_spendable"], "655")
        self.assertEqual(outcome["report"]["max_spendable"]["max_spendable"], "345")
        self.assertEqual(next(row["actual_g"] for row in outcome["report"]["purpose_progress"]["owners"]
                              if row["owner_id"] == self.owner), "-30")
        text = render_workflow_text(outcome)
        self.assertIn("默认支出资金收付：生活费", text)
        self.assertIn("CNY：净流出 85（流出 120，流入 35）", text)
        self.assertIn("消费实付 30，消费退款 5，还债 40", text)
        self.assertIn("代垫 50，报销收回 30", text)

    def test_card_responsibility_owner_does_not_choose_the_paying_purpose(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            '  Assets:Cash -40 CNY\n    fund: "生活费"',
            '  Assets:Cash -40 CNY\n    fund: "生活费15,储蓄25"'), encoding="utf-8")
        self.configure()
        self.assertEqual(self.audit()["totals"][0]["debt_payments"], "15")

    def test_foreign_payments_stay_native_and_fx_is_not_charged_again(self):
        source = self.ledger.read_text(encoding="utf-8").replace(
            '  Equity:Opening -1000 CNY',
            '  Equity:Opening -1000 CNY\n  Assets:Cash 20 USD\n  Equity:Opening -20 USD')
        source += ('2026-09-13 * "card purchase USD"\n'
                   '  Expenses:Food 10 USD\n  Liabilities:Card -10 USD\n'
                   '2026-09-14 * "card payment USD"\n'
                   '  Liabilities:Card 10 USD\n  Assets:Cash -10 USD\n    fund: "生活费"\n'
                   '2026-09-15 * "FX"\n'
                   '  Assets:Cash -7 CNY\n    fund: "生活费"\n'
                   '  Assets:Cash 1 USD @@ 7 CNY\n    fund: "生活费"\n')
        self.ledger.write_text(source, encoding="utf-8")
        self.configure()
        result = self.audit()
        self.assertEqual(result["status"], "CERTIFIED")
        totals = {row["commodity"]: row for row in result["totals"]}
        self.assertEqual(totals["USD"]["debt_payments"], "10")
        self.assertEqual(totals["CNY"]["net_outflow"], "85")
        self.assertNotIn("FX", {row["narration"] for row in result["events"]})

    def test_unsupported_mixed_flow_has_no_partial_zero_and_is_an_implementation_limit(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
            '2026-09-16 * "compound subscription fee"\n'
            '  Assets:Cash -21 CNY\n    fund: "生活费"\n'
            '  Assets:FundBook 20 CNY\n    fund: "生活费"\n'
            '  Expenses:Food 1 CNY\n', encoding="utf-8")
        self.configure()
        result = self.audit()
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["totals"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["review_items"][0]["reason_kind"], "implementation_limit")
        self.assertEqual(result["reason_codes"], ["DEFAULT_OUTFLOW_EVENT_UNSUPPORTED"])

    def test_future_unmigrated_transaction_does_not_enter_current_summary(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
            '2026-10-01 * "future expense"\n  budget: "old tag"\n'
            '  Expenses:Food 999 CNY\n  Assets:Cash -999 CNY\n', encoding="utf-8")
        self.configure()
        self.assertEqual(self.audit()["totals"][0]["net_outflow"], "85")

    def test_net_income_with_fee_does_not_manufacture_an_asset_payment(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
            '2026-09-17 * "income net of fee"\n'
            '  Assets:Cash 69 CNY\n    fund: "生活费"\n'
            '  Income:Reward -70 CNY\n  Expenses:Food 1 CNY\n', encoding="utf-8")
        self.configure()
        self.assertEqual(self.audit()["totals"][0]["expenses_paid"], "30")

    def test_net_refund_can_exceed_this_months_payments(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            '  Expenses:Food -5 CNY\n  Assets:Cash 5 CNY',
            '  Expenses:Food -100 CNY\n  Assets:Cash 100 CNY'), encoding="utf-8")
        self.configure()
        result = self.audit()
        self.assertEqual(result["totals"][0]["expense_refunds"], "100")
        self.assertEqual(result["totals"][0]["net_outflow"], "-10")

    def test_missing_responsibility_does_not_sign_a_partial_total(self):
        self.configure()
        document = yaml.safe_load(self.debts.read_text(encoding="utf-8"))
        document["events"] = []
        self.save(self.debts, document)
        result = self.audit()
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["totals"])
        self.assertEqual(result["events"], [])
        self.assertIn("OBLIGATION_UNCLAIMED", result["reason_codes"])

    def test_implementation_limit_joins_monthly_review_with_source_and_no_report(self):
        self.configure()
        pending = self.audit()
        source = pending["events"][0]["source"]
        pending.update(status="NEEDS_REVIEW", totals=None, events=[],
                       reason_codes=["DEFAULT_OUTFLOW_EVENT_UNSUPPORTED"],
                       review_items=[review_item("DEFAULT_OUTFLOW_EVENT_UNSUPPORTED", source)])
        with patch("beancount_budget_tool.v6.monthly_report.audit_default_outflows", return_value=pending):
            outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIsNone(outcome["report"])
        self.assertEqual(outcome["review_items"][0]["source"], source)
        self.assertIn("默认支出资金收付", outcome["review_items"][0]["metrics"])
        self.assertIn("程序实现缺口", render_workflow_text(outcome))

    def configure_pool(self, conflict=False):
        source = self.ledger.read_text(encoding="utf-8").replace(
            '2026-08-01 open Equity:Opening',
            '2026-08-01 open Assets:Broker:Cash\n2026-08-01 open Assets:Broker:Stock\n'
            '2026-08-01 open Equity:Opening')
        source += ('2026-08-30 * "broker opening"\n'
                   '  Assets:Broker:Cash 100 CNY\n'
                   '  Assets:Broker:Stock 100 FUND {1 CNY}\n'
                   '  Equity:Opening -200 CNY\n'
                   '2026-09-17 * "imported broker sale"\n'
                   '  Assets:Broker:Stock -1 FUND {1 CNY}\n'
                   '  Assets:Broker:Cash 1.90 CNY\n'
                   '  Expenses:Food 0.10 CNY\n'
                   '  Income:Reward -1 CNY\n')
        if conflict:
            source += ('2026-09-18 * "conflicting broker cash purpose"\n'
                       '  Expenses:Food 1 CNY\n'
                       '  Assets:Broker:Cash -1 CNY\n    fund: "生活费"\n')
        self.ledger.write_text(source, encoding="utf-8")
        self.configure()
        owner_doc = yaml.safe_load(self.owners.read_text(encoding="utf-8"))
        ids = {row["name"]: row["id"] for row in owner_doc["owners"]}
        self.pool = self.folder / "purpose_pools.yaml"
        self.save(self.pool, {"schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "pools": [{"id": "broker", "accounts": ["Assets:Broker:Cash", "Assets:Broker:Stock"],
                       "reserve_order": ["Assets:Broker:Cash", "Assets:Broker:Stock"],
                       "payment_scope": "excluded", "protected_owner_id": ids["储蓄"],
                       "protected_label": "额外储蓄", "protected_principal": "20",
                       "remainder_owner_id": ids["投资"], "remainder_label": "投资", "reason": "reviewed pool"}]})

    def test_reviewed_nondefault_pool_does_not_require_per_lot_purpose_claims(self):
        self.configure_pool()
        without_pool = self.audit()
        self.assertIn("OWNERSHIP_PENDING", without_pool["reason_codes"])
        result = audit_default_outflows(self.ledger, CUTOVER, "2026-09", self.accounts,
                                        self.owners, self.opening, self.debts, self.pool)
        self.assertEqual(result["status"], "CERTIFIED")
        self.assertEqual(result["totals"][0]["net_outflow"], "85")
        self.assertEqual(result["excluded_pools"][0]["id"], "broker")

    def test_pool_exclusion_does_not_hide_an_explicit_default_purpose_conflict(self):
        self.configure_pool(conflict=True)
        result = audit_default_outflows(self.ledger, CUTOVER, "2026-09", self.accounts,
                                        self.owners, self.opening, self.debts, self.pool)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["totals"])
        self.assertIn("PURPOSE_POOL_PENDING", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()

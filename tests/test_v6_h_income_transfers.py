"""Income can accompany a proved, purpose-preserving H inventory transfer."""

from datetime import date
from fractions import Fraction
from itertools import permutations
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import audit_simple, audit_simple_period, build_runtime
from beancount_budget_tool.v6.spending import audit_monthly_spending
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.yamlstrict import load_yaml


HEADER = '''option "operating_currency" "CNY"
2026-08-01 open Assets:Bank
2026-08-01 open Assets:Digital
2026-08-01 open Equity:Opening
2026-08-01 open Income:Salary
2026-08-01 open Expenses:Fee
2026-08-31 * "opening"
  Assets:Bank 100 CNY
  Assets:Digital 0.28 CNY
  Equity:Opening -100.28 CNY
'''
FUNDS = "储蓄526.22，储蓄0.28，生活费2000，投资2000，电子产品900，旅游900，保险200"


class HIncomeTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.ledger = self.base / "main.bean"
        self.config = self.base / "config.yml"
        self.doc = {"schema": 1, "ledger": "main.bean", "budgets": "budgets.yml",
                    "month": "2026-09", "cutover": "2026-09-01", "report_currency": "CNY",
                    "defaults": {"income_purpose": "自由资金", "expense_purpose": "生活费",
                                 "expense_category": "生活开销"},
                    "accounts": {name: {"semantics": "H", "payment_scope": {"CNY": "payable"},
                                        "opening": {"CNY": {"生活费": units}}}
                                 for name, units in (("Assets:Bank", "100"), ("Assets:Digital", "0.28"))}}
        self.plans = {"schema": 1, "months": {month: {"completeness_confirmed": True,
                       "expense_targets": {"生活开销": "500"}}
                       for month in ("2026-09", "2026-10")}}

    def source(self, funds=FUNDS, *, principal="0.28", receipt="6526.50", income="-6526.22",
               order=(0, 1, 2), day="2026-09-02", fee=None, source_fund="生活费"):
        legs = [f'  Assets:Bank {receipt} CNY\n' +
                (f'    fund: "{funds}"\n' if funds is not None else ''),
                f'  Assets:Digital -{principal} CNY\n    fund: "{source_fund}"\n',
                f'  Income:Salary {income} CNY\n']
        return (f'{day} * "income and cash transfer"\n' + ''.join(legs[i] for i in order) +
                (f'  Expenses:Fee {fee} CNY\n' if fee is not None else ''))

    def audit(self, source, *, header=HEADER, month="2026-09"):
        self.ledger.write_text(header + source, encoding="utf-8")
        self.config.write_text(yaml.safe_dump(self.doc, allow_unicode=True), encoding="utf-8")
        (self.base / "budgets.yml").write_text(yaml.safe_dump(self.plans, allow_unicode=True), encoding="utf-8")
        before = {p: p.read_bytes() for p in self.base.iterdir()}
        result = audit_simple(self.config, month, include_transactions=True)
        self.assertEqual({p: p.read_bytes() for p in self.base.iterdir()}, before)
        return result

    def certified(self, source, **kwargs):
        result = self.audit(source, **kwargs)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertEqual(result["review_items"], [])
        return result["report"]

    def income(self):
        runtime = build_runtime(self.config)
        try:
            paths = load_yaml(runtime.config)
            audit = audit_monthly_spending(self.ledger, date(2026, 9, 1), "2026-09",
                runtime.config.parent / paths["classifications"], "CNY")
            return Fraction(audit["metrics"]["income_supplement"]["report_total"])
        finally:
            runtime.close()

    def test_salary_receipt_preserves_wallet_principal_and_counts_only_real_income(self):
        report = self.certified(self.source())
        tx = report["transactions"]["rows"][0]
        self.assertEqual([e["kind"] for e in tx["purpose_events"]], ["h_income_transfer"])
        proof = tx["purpose_events"][0]["protocol"]
        self.assertEqual(proof["protocol"], "native_h_income_transfer_v1")
        income = {report["owner_names"][owner]: Fraction(amount)
                  for owner, amount in proof["income_by_owner"].items()}
        self.assertEqual(income, {"生活费": Fraction("1999.72"), "储蓄": Fraction("526.50"),
            "投资": Fraction(2000), "电子产品": Fraction(900), "旅游": Fraction(900), "保险": Fraction(200)})
        self.assertEqual(sum(income.values()), Fraction("6526.22"))
        self.assertEqual(self.income(), Fraction("6526.22"))
        balances = {(row["account"], report["owner_names"][row["owner_id"]]): Fraction(row["units"])
                    for row in report["account_purposes"]["rows"]}
        self.assertFalse(any(amount for (account, _), amount in balances.items() if account == "Assets:Digital"))
        self.assertEqual(balances["Assets:Bank", "生活费"], 2100)
        self.assertEqual(balances["Assets:Bank", "储蓄"], Fraction("526.50"))
        allocations = {report["owner_names"][owner]: Fraction(amount)
                       for owner, amount in tx["net_allocations"].items()}
        self.assertEqual(allocations, {**income, "自由资金": -Fraction("6526.22")})
        self.assertEqual(sum(allocations.values()), 0)
        self.assertEqual(report["default_outflows"]["totals"][0]["expenses_paid"], "0")
        detail = '\n'.join(render_transaction(tx, report["owner_names"]))
        self.assertIn("原有本金 0.28＋收入 6526.22", detail)
        self.assertNotIn("H 赎回核对", detail)
        self.assertIn("新增收入 1999.72", detail)

    def test_combined_and_explicit_two_transaction_forms_have_identical_balances_income_and_g(self):
        combined = self.certified(self.source())
        income = ('2026-09-02 * "salary into wallet"\n  Assets:Digital 6526.22 CNY\n'
                  '    fund: "储蓄526.22，储蓄0.28，生活费1999.72，投资2000，电子产品900，旅游900，保险200"\n'
                  '  Income:Salary -6526.22 CNY\n')
        transfer = (f'2026-09-02 * "withdraw whole wallet"\n  Assets:Bank 6526.50 CNY\n'
                    f'    fund: "{FUNDS}"\n  Assets:Digital -6526.50 CNY\n    fund: "{FUNDS}"\n')
        split = self.certified(income + transfer)
        def balances(report):
            return {(row["account"], report["owner_names"][row["owner_id"]]): Fraction(row["units"])
                    for row in report["account_purposes"]["rows"] if Fraction(row["units"])}
        self.assertEqual(balances(combined), balances(split))
        self.assertEqual(self.income(), Fraction("6526.22"))
        self.assertEqual(combined["actual_spendable"]["actual_spendable"],
                         split["actual_spendable"]["actual_spendable"])
        def allocations(report):
            return {report["owner_names"][row["owner_id"]]: Fraction(row["actual_g"])
                    for row in report["purpose_progress"]["owners"]}
        self.assertEqual(allocations(combined), allocations(split))

    def test_posting_and_duplicate_slice_order_do_not_pair_principal_to_a_matching_number(self):
        for order in permutations(range(3)):
            with self.subTest(order=order):
                report = self.certified(self.source("保险200；旅游900；电子产品900；投资2000；生活费2000；储蓄0.28；储蓄526.22", order=order))
                self.assertEqual(report["transactions"]["rows"][0]["purpose_events"][0]["kind"], "h_income_transfer")

    def test_income_allocations_respect_the_configured_income_purpose(self):
        self.doc["defaults"]["income_purpose"] = "生活费"
        report = self.certified(self.source())
        tx = report["transactions"]["rows"][0]
        allocations = {report["owner_names"][owner]: Fraction(amount)
                       for owner, amount in tx["net_allocations"].items()}
        self.assertEqual(allocations["生活费"], -Fraction("4526.50"))
        self.assertEqual(allocations["储蓄"], Fraction("526.50"))
        self.assertNotIn("自由资金", allocations)
        self.assertEqual(sum(allocations.values()), 0)

    def test_each_of_multiple_principal_purposes_is_preserved_before_new_income(self):
        self.doc["accounts"]["Assets:Digital"]["opening"]["CNY"] = {"生活费": "0.14", "储蓄": "0.14"}
        report = self.certified(self.source("生活费100.14，储蓄0.14，旅游10", receipt="110.28", income="-110",
                                            source_fund="储蓄0.14，生活费0.14"))
        proof = report["transactions"]["rows"][0]["purpose_events"][0]["protocol"]
        self.assertEqual({report["owner_names"][owner]: Fraction(amount)
                          for owner, amount in proof["income_by_owner"].items()}, {"生活费": 100, "旅游": 10})
        for row in report["account_purposes"]["rows"]:
            if row["account"] == "Assets:Digital":
                self.assertEqual(Fraction(row["units"]), 0)

    def test_period_counts_only_the_income_month_flow_and_keeps_closing_cash(self):
        monthly = self.certified(self.source())
        period = audit_simple_period(self.config, "2026-09", "2026-10")
        self.assertEqual(period["status"], "CERTIFIED_SCOPED", period["review_items"])
        report = period["report"]
        self.assertEqual({row["name"]: Fraction(row["actual_g"]) for row in report["purpose_allocations"]},
                         {monthly["owner_names"][row["owner_id"]]: Fraction(row["actual_g"])
                          for row in monthly["purpose_progress"]["owners"]})
        self.assertEqual(report["closing"]["actual_spendable"]["actual_spendable"], "2100")

    def test_source_principal_cannot_be_changed_to_another_purpose_under_cover_of_income(self):
        for funds in ("生活费0.27，储蓄6526.23", "储蓄6526.50"):
            with self.subTest(funds=funds):
                result = self.audit(self.source(funds))
                self.assertIsNone(result["report"])
                self.assertIn("H_REDEMPTION_PURPOSE_MISMATCH", {r["reason_code"] for r in result["review_items"]})

    def test_zero_income_loss_and_fee_do_not_enter_the_positive_income_protocol(self):
        for kwargs in ({"funds": "生活费0.14，储蓄0.14", "receipt": "0.28", "income": "0"},
                       {"funds": "生活费0.05，储蓄0.05", "receipt": "0.10", "income": "0.18"},
                       {"funds": "生活费0.28，储蓄19.99", "receipt": "20.27", "income": "-20", "fee": "0.01"}):
            with self.subTest(kwargs=kwargs):
                result = self.audit(self.source(**kwargs))
                self.assertIsNone(result["report"])
                self.assertIn("H_REDEMPTION_PURPOSE_MISMATCH", {r["reason_code"] for r in result["review_items"]})

    def test_withdrawal_cannot_exceed_proved_source_inventory(self):
        self.doc["accounts"]["Assets:Digital"]["opening"]["CNY"] = {"生活费": "0.27", "储蓄": "0.01"}
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.source())
        self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")

    def test_missing_cash_fund_does_not_apply_an_income_default_to_the_principal(self):
        result = self.audit(self.source(None))
        self.assertIsNone(result["report"])
        self.assertIn("OWNERSHIP_UNREVIEWED", {r["reason_code"] for r in result["review_items"]})

    def test_loader_tolerance_is_not_a_proof_of_the_income_equation(self):
        result = self.audit(self.source(income="-6526.23"),
                            header=HEADER + 'option "inferred_tolerance_default" "CNY:1"\n')
        self.assertIsNone(result["report"])
        self.assertIn("NAMED_H_EVENT_UNSUPPORTED", {r["reason_code"] for r in result["review_items"]})

    def test_future_income_transfer_does_not_change_the_current_month(self):
        report = self.certified(self.source(day="2026-10-02"))
        self.assertEqual(report["transactions"]["rows"], [])
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "2507/25")
        self.assertEqual(self.income(), 0)


if __name__ == "__main__":
    unittest.main()

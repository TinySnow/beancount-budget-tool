"""Foreign native debt, historic expense and locked settlement stay distinct."""

from copy import deepcopy
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.tools.v6_review_source_refs import diagnose
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.cli import main as audit_cli
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.obligations import audit_simple_obligations


CUTOVER = date(2026, 9, 1)
AS_OF = date(2026, 9, 30)


class DebtSettlementTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.ledger = self.base / "main.bean"

    def save(self, key, document):
        path = self.base / (key + ".yaml")
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def fixture(self, purchases=(100,), *, owner="living", priced=True, native_fee=0):
        source = ('option "operating_currency" "CNY"\n'
                  '2026-08-01 open Assets:Bank\n2026-08-01 open Assets:Travel\n'
                  '2026-08-01 open Assets:USD\n2026-08-01 open Expenses:Food\n'
                  '2026-08-01 open Liabilities:Card\n2026-08-01 open Equity:Opening\n'
                  '2026-08-31 * "opening"\n'
                  '  Assets:Bank 1000 CNY\n  Assets:Travel 100 CNY\n  Equity:Opening -1100 CNY\n'
                  '2026-08-31 * "zero dollar opening"\n  Assets:USD 0 USD\n  Equity:Opening 0 USD\n')
        for index, amount in enumerate(purchases):
            price = f" @@ {amount * 7} CNY" if priced else ""
            source += (f'2026-09-{index + 2:02d} * "foreign card purchase {index}"\n'
                       f'  Expenses:Food {amount} USD{price}\n    budget: "餐饮"\n'
                       f'  Liabilities:Card -{amount} USD{price}\n')
        if native_fee:
            source += (f'2026-09-04 * "native card fee"\n'
                       f'  Expenses:Food {native_fee} CNY\n    budget: "餐饮"\n'
                       f'  Liabilities:Card -{native_fee} CNY\n')
        self.ledger.write_text(source, encoding="utf-8")
        self.snapshot = load_financial(self.ledger).digest
        self.owners = [{"id": ident, "name": name, "aliases": []}
                       for ident, name in (("living", "生活费"), ("free", "自由资金"), ("travel", "旅游"))]
        self.save("accounts", {"schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
                               "accounts": [{"account": account, "semantics": "H", "balance_semantics": "real_inventory",
                                             "payment_scope": {currency: "excluded" if currency == "USD" else "payable"},
                                             "reason": "reviewed synthetic bank"}
                                            for account, currency in (("Assets:Bank", "CNY"), ("Assets:Travel", "CNY"), ("Assets:USD", "USD"))]})
        self.save("opening", {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                              "h_holdings": [{"account": account, "commodity": "CNY", "lot": "CNY/no-cost",
                                              "owner": purpose, "units": amount, "reason": "opening cash"}
                                             for account, purpose, amount in (("Assets:Bank", "living", "1000"), ("Assets:Travel", "travel", "100"))],
                              "k_openings": []})
        self.save("ownership", {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                                "loader_version": BEANCOUNT_VERSION, "owners": self.owners,
                                "defaults": {"income_owner_id": "free", "expense_owner_id": "living"}, "claims": []})
        self.debt_owner = owner
        self.write_claims()
        self.save("classifications", {"schema": 6, "cutover": CUTOVER.isoformat(), "loader_version": BEANCOUNT_VERSION,
                                      "categories": [{"id": "food", "name": "餐饮", "parent": None, "aliases": []}], "classifications": []})

    def write_claims(self):
        candidates = audit_simple_obligations(self.ledger, CUTOVER)["events"]
        self.save("obligations", {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                                  "loader_version": BEANCOUNT_VERSION, "owners": self.owners,
                                  "events": [{"liability_ref": row["liability_ref"], "counterpart_ref": row["counterpart_ref"],
                                              "kind": "consumer_purchase" if row["shape"] == "consumer_purchase" else "payment",
                                              "obligation_id": f"purchase-{index}" if row["shape"] == "consumer_purchase" else "purchase-0",
                                              **({"owner_id": self.debt_owner} if row["shape"] == "consumer_purchase" else {}),
                                              "reason": "reviewed source responsibility"}
                                             for index, row in enumerate(candidates)]})

    def audit(self, settlements=None, coverage=None, as_of=AS_OF):
        statement = self.save("completeness", {"schema": 6, "cutover": CUTOVER.isoformat(), "as_of": as_of.isoformat(),
                                               "unbooked_obligations": "none", "inflight_payments": "none", "reason": "reviewed synthetic scope"})
        return audit_actual_snapshot(self.ledger, CUTOVER, as_of,
                                     *(self.base / (key + ".yaml") for key in ("accounts", "opening", "ownership", "obligations")),
                                     statement, coverage, settlements)

    def declaration(self, amounts=("800",), *, unlocked=False):
        debts = self.audit()["booked_debts"]
        return {"schema": 6, "cutover": CUTOVER.isoformat(), "as_of": AS_OF.isoformat(), "report_currency": "CNY",
                "statements": [{"id": "card-statement", "kind": "unlocked" if unlocked else "locked_bill",
                                "evidence": "synthetic bank statement #2026-09, reviewed all-in repayment amount",
                                "reason": "reviewed current whole balances and statement allocation",
                                **({} if unlocked else {"locked_on": "2026-09-05", "valid_until": "2026-10-31",
                                                        "report_amount": str(sum((Decimal(amount) for amount in amounts), Decimal(0)))}),
                                "allocations": [{"obligation_id": row["obligation_id"], "purchase_ref": row["purchase_ref"],
                                                 "remaining_native": row["remaining_native"],
                                                 **({} if unlocked else {"report_amount": str(amounts[index])})}
                                                for index, row in enumerate(debts)]}]}

    def config(self, *, with_settlements=True):
        self.save("expense_plans", {"schema": 6, "report_currency": "CNY", "spendable_cap_category_id": "food",
                                    "defaults": {"expense_subtree_targets": {"food": "2000"}}})
        self.save("purpose_plans", {"schema": 6, "report_currency": "CNY", "defaults": {
            "allocation_targets": {row["id"]: "not_assessed" for row in self.owners}}})
        for key in ("h_transfers", "h_fx", "k_events"):
            self.save(key, {"schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": self.snapshot,
                            "loader_version": BEANCOUNT_VERSION, "events": []})
        self.save("purpose_pools", {"schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY", "pools": []})
        return self.save("month", {"schema": 6, "ledger": "main.bean", "cutover": CUTOVER.isoformat(),
                                   "month": "2026-09", "report_currency": "CNY",
                                   **({"debt_settlements": "settlements.yaml"} if with_settlements else {}),
                                   **{key: key + ".yaml" for key in (
                                       "accounts", "opening", "ownership", "obligations", "completeness", "classifications",
                                       "expense_plans", "purpose_plans", "h_transfers", "h_fx", "k_events", "purpose_pools")}})

    def coverage(self, amounts):
        debts = self.audit()["booked_debts"]
        return self.save("coverage", {"schema": 6, "cutover": CUTOVER.isoformat(), "as_of": AS_OF.isoformat(), "report_currency": "CNY",
                                      "decisions": [{"obligation_id": row["obligation_id"], "purchase_ref": row["purchase_ref"],
                                                     "resources": [{"account": "Assets:Travel", "commodity": "CNY", "lot": "CNY/no-cost",
                                                                    "owner_id": "travel", "amount": amount}] if amount != "0" else [],
                                                     "reason": "confirmed dedicated report-currency cash"}
                                                    for row, amount in zip(debts, amounts)]})

    def pay(self, native, report):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
                               f'2026-09-20 * "buy dollars"\n'
                               f'  Assets:USD {native} USD @@ {report} CNY\n    fund: "生活费"\n'
                               f'  Assets:Bank -{report} CNY\n    fund: "生活费"\n'
                               f'2026-09-20 * "card payment"\n'
                               f'  Liabilities:Card {native} USD\n'
                               f'  Assets:USD -{native} USD\n    fund: "生活费"\n', encoding="utf-8")
        self.write_claims()

    def test_month_keeps_historic_700_and_occupies_locked_800(self):
        self.fixture()
        self.audit(self.save("settlements", self.declaration()))
        config = self.config()
        result = audit_month_workflow(config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result)
        report = result["report"]
        self.assertEqual(report["expense_targets"]["categories"][0]["subtree_spent"], "700")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "200")
        self.assertEqual(report["actual_spendable"]["booked_debts"][0]["remaining_native"], "100")
        self.assertEqual(report["max_spendable"]["max_spendable"], "200")
        rendered = render_workflow_text(result)
        self.assertIn("100 USD", rendered)
        self.assertIn("锁定清偿 800 CNY", rendered)
        self.assertIn("覆盖与未覆盖均按锁定清偿金额", rendered)
        refs = next(row for row in diagnose(config)["sidecars"] if row["field"] == "debt_settlements")
        self.assertEqual(refs["references"], 1)
        self.assertEqual(refs["stale_references"], [])

    def test_missing_decision_and_explicit_unlocked_path_have_different_states(self):
        self.fixture()
        missing = self.audit()
        issue = next(row for row in missing["review_items"] if row["reason_code"] == "SETTLEMENT_VALUE_MISSING")
        self.assertEqual((issue["obligation_id"], issue["reason_kind"]), ("purchase-0", "data_review"))
        self.audit(self.save("settlements", self.declaration(unlocked=True)))
        result = audit_month_workflow(self.config())
        self.assertEqual(result["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIsNone(result["report"])
        self.assertEqual(result["review_items"][0]["reason_code"], "SETTLEMENT_PATH_UNSUPPORTED")
        self.assertEqual(result["review_items"][0]["reason_kind"], "implementation_limit")

    def test_lock_does_not_fill_missing_historical_expense_price(self):
        self.fixture(priced=False)
        actual = self.audit(self.save("settlements", self.declaration()))
        self.assertEqual(actual["actual_spendable"], "200")
        result = audit_month_workflow(self.config())
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["report"])
        self.assertIn("VALUATION_MISSING", [row["reason_code"] for row in result["review_items"]])

    def test_grouped_amounts_and_fixed_fee_allocate_exactly_once(self):
        self.fixture(purchases=(60, 40))
        document = self.declaration(("500.005", "300.005"))
        report = self.audit(self.save("settlements", document))
        self.assertEqual(report["actual_spendable"], "19999/100")
        document["statements"][0]["report_amount"] = "800"
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", document))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_ALLOCATION_MISMATCH")
        self.assertTrue(caught.exception.source.startswith(str((self.base / "settlements.yaml").resolve()) + ":"))

    def test_mixed_native_fee_remains_a_single_native_obligation(self):
        self.fixture(native_fee=10)
        document = self.declaration(("800", "10"))
        report = self.audit(self.save("settlements", document))
        self.assertEqual(report["actual_spendable"], "190")
        document["statements"][0]["allocations"][1]["report_amount"] = "20"
        document["statements"][0]["report_amount"] = "820"
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", document))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_CURRENCY_INVALID")

    def test_duplicate_bill_evidence_or_obligation_cannot_be_counted_twice(self):
        self.fixture(purchases=(60, 40))
        original = self.declaration(("480", "320"))
        for mode in ("id", "evidence", "obligation"):
            with self.subTest(mode=mode):
                document = deepcopy(original)
                duplicate = deepcopy(document["statements"][0])
                if mode != "id":
                    duplicate["id"] = "another-statement"
                if mode == "obligation":
                    duplicate["evidence"] = "another statement reference"
                document["statements"].append(duplicate)
                with self.assertRaises(BudgetError) as caught:
                    self.audit(self.save("settlements", document))
                self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_DUPLICATE")

    def test_future_expired_or_predated_lock_and_wrong_report_date_rejected(self):
        self.fixture()
        original = self.declaration()
        for field, value in (("locked_on", "2026-10-01"), ("valid_until", "2026-09-29"),
                             ("locked_on", "2026-09-01"), ("locked_on", date(2026, 9, 5))):
            with self.subTest(field=field, value=value):
                document = deepcopy(original)
                document["statements"][0][field] = value
                with self.assertRaises(BudgetError) as caught:
                    self.audit(self.save("settlements", document))
                self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_TIME_INVALID")
        original["as_of"] = "2026-10-31"
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", original))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_INVALID")

    def test_wrong_native_amount_and_stale_purchase_ref_rejected(self):
        self.fixture()
        document = self.declaration()
        document["statements"][0]["allocations"][0]["remaining_native"] = "99"
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", document))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_NATIVE_MISMATCH")
        document = self.declaration()
        document["statements"][0]["allocations"][0]["purchase_ref"]["fact_sha256"] = "0" * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", document))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_REF_INVALID")

    def test_partial_repayment_requires_current_whole_balance_revaluation(self):
        self.fixture()
        original = self.declaration()
        self.pay(20, 160)
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", original))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_NATIVE_MISMATCH")
        after = self.audit(self.save("settlements", self.declaration(("640",))))
        self.assertEqual(after["actual_spendable"], "200")
        self.assertEqual(after["booked_debts"][0]["remaining_native"], "80")

    def test_full_repayment_drops_old_lock_without_double_deduction(self):
        self.fixture()
        original = self.declaration()
        self.pay(100, 800)
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", original))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_OBLIGATION_UNKNOWN")
        after = self.audit()
        self.assertEqual(after["actual_spendable"], "200")
        self.assertEqual(after["booked_debts"], [])
        self.assertEqual(after["settlement_allocations"], [])

    def test_report_currency_coverage_joint_capacity_for_foreign_debts(self):
        self.fixture(purchases=(10, 10), owner="travel")
        settlements = self.save("settlements", self.declaration(("80", "80")))
        report = self.audit(settlements, self.coverage(("70", "30")))
        self.assertEqual(report["actual_spendable"], "940")
        self.assertEqual(report["uncovered_responsibility"], "60")
        with self.assertRaises(BudgetError) as caught:
            self.audit(settlements, self.coverage(("70", "40")))
        self.assertEqual(caught.exception.code, "DEBT_COVERAGE_RESOURCE_EXCEEDED")

    def test_one_missing_value_prevents_partial_numbers(self):
        self.fixture(purchases=(10, 10))
        document = self.declaration(("80", "80"))
        document["statements"][0]["allocations"].pop()
        document["statements"][0]["report_amount"] = "80"
        result = self.audit(self.save("settlements", document))
        self.assertIsNone(result["actual_spendable"])
        self.assertIsNone(result["settlement_allocations"])
        self.assertIsNone(result["coverage_allocations"])
        self.assertEqual(result["review_items"][0]["obligation_id"], "purchase-1")

    def test_cli_passes_the_optional_settlement_file(self):
        self.fixture()
        settlements = self.save("settlements", self.declaration())
        self.audit(settlements)
        output = StringIO()
        with redirect_stdout(output):
            code = audit_cli(["--actual-audit", "--ledger", str(self.ledger), "--cutover", CUTOVER.isoformat(),
                              "--as-of", AS_OF.isoformat(), "--debt-settlements", str(settlements),
                              *[argument for key in ("accounts", "opening", "ownership", "obligations", "completeness")
                                for argument in ("--" + key, str(self.base / (key + ".yaml")))]])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["actual_spendable"], "200")

    def test_priced_foreign_opening_uses_same_lock_without_current_expense(self):
        self.fixture()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace('2026-09-02 *', '2026-08-25 *'), encoding="utf-8")
        pending = audit_simple_obligations(self.ledger, CUTOVER)
        origin = pending["opening_pending"][0]["source_candidates"][0]
        document = yaml.safe_load((self.base / "obligations.yaml").read_text(encoding="utf-8"))
        document["events"] = []
        document["opening"] = [{"kind": "consumer_debt", "obligation_id": "purchase-0", "owner_id": "living",
                                "liability_ref": origin["liability_ref"], "remaining_native": "100",
                                "reason": "reviewed foreign card cutover residual"}]
        self.save("obligations", document)
        self.audit(self.save("settlements", self.declaration()))
        result = audit_month_workflow(self.config())
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["actual_spendable"]["actual_spendable"], "200")
        self.assertEqual(result["report"]["expense_targets"]["categories"][0]["subtree_spent"], "0")

    def test_zero_locked_amount_is_an_explicit_value_and_not_missing(self):
        self.fixture()
        report = self.audit(self.save("settlements", self.declaration(("0",))))
        self.assertEqual(report["actual_spendable"], "1000")
        self.assertEqual(report["settlement_allocations"][0]["report_amount"], "0")

    def test_invalid_amount_and_reference_field_types_have_yaml_positions(self):
        self.fixture()
        original = self.declaration()
        for raw in (800, True, "-1", "nan", "1/2"):
            with self.subTest(amount=raw):
                document = deepcopy(original)
                document["statements"][0]["report_amount"] = raw
                with self.assertRaises(BudgetError) as caught:
                    self.audit(self.save("settlements", document))
                self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_AMOUNT_INVALID")
                self.assertTrue(caught.exception.source.startswith(str((self.base / "settlements.yaml").resolve()) + ":"))
        original["statements"][0]["allocations"][0]["purchase_ref"]["occurrence"] = date(2026, 9, 2)
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.save("settlements", original))
        self.assertEqual(caught.exception.code, "DEBT_SETTLEMENT_REF_INVALID")

    def test_future_payment_does_not_enter_an_earlier_actual_snapshot(self):
        self.fixture()
        document = self.declaration()
        document["as_of"] = "2026-09-10"
        self.pay(100, 800)
        result = self.audit(self.save("settlements", document), as_of=date(2026, 9, 10))
        self.assertEqual(result["actual_spendable"], "200")
        self.assertEqual(result["booked_debts"][0]["remaining_native"], "100")

    def test_loader_tolerance_does_not_hide_a_priced_debt_residual(self):
        self.fixture()
        self.ledger.write_text('option "inferred_tolerance_default" "CNY:1"\n' +
                               self.ledger.read_text(encoding="utf-8").replace('Liabilities:Card -100 USD @@ 700 CNY',
                                                               'Liabilities:Card -100 USD @@ 700.01 CNY'), encoding="utf-8")
        load_financial(self.ledger)  # Loader tolerance accepts this source.
        with self.assertRaises(BudgetError) as caught:
            audit_simple_obligations(self.ledger, CUTOVER)
        self.assertEqual(caught.exception.code, "OBLIGATION_PRICE_MISMATCH")


if __name__ == "__main__":
    unittest.main()

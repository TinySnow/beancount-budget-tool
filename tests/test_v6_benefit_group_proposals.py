"""A candidate group records full refs and the booked cost equation."""

from datetime import date
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.benefits import audit_economic_benefits
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.exact import amount_text
from beancount_budget_tool.tools.v6_init import create_setup, compile_setup
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.spending import audit_monthly_spending
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_propose_benefit_groups import propose


class BenefitGroupProposalTests(unittest.TestCase):
    def test_exact_sale_source_is_proved_and_legacy_review_remains_supported(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            review = Path(folder) / "classes.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-09-01 open Assets:Cash\n'
                '2026-09-01 open Assets:Security\n'
                '2026-09-01 open Income:Gain\n'
                '2026-09-01 open Expenses:Fee\n'
                '2026-09-01 * "buy"\n'
                '  Assets:Security 10 SH {2 CNY}\n'
                '  Assets:Cash -20 CNY\n'
                '2026-09-02 * "sell"\n'
                '  Assets:Security -10 SH {2 CNY} @ 3 CNY\n'
                '  Assets:Cash 29 CNY\n'
                '  Expenses:Fee 1 CNY\n'
                '  Income:Gain -10 CNY\n', encoding="utf-8")
            candidates = audit_classification(root, date(2026, 9, 1))["missing_classifications"]
            review.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-09-01", "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "generic", "name": "通用", "parent": None, "aliases": []}],
                "classifications": [{"claim": row["ref"], "reason": "虚拟账本",
                                     "slices": [{"id": f"slice_{i}", "category_id": "generic",
                                                 "signed_units": row["signed_units"]}]}
                                    for i, row in enumerate(candidates)],
            }, allow_unicode=True), encoding="utf-8")
            result = propose(root, date(2026, 9, 1), review, "CNY")
            self.assertEqual(result["pending_complex_benefits"], 0)
            self.assertEqual(result["automatically_proved_groups"], 1)
            self.assertEqual(result["proposed_groups"], 1)
            group = result["proposals"][0]
            self.assertEqual(group["status"], "proposed")
            self.assertEqual((group["principal_report_value"], group["income_report_value"],
                              group["fee_report_value"], group["cash_receipt_report_value"]),
                             ("20", "10", "1", "29"))
            self.assertEqual(len(group["fee_refs"]), 1)
            approval = Path(folder) / "benefits.yml"
            approval.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-09-01",
                "ledger_snapshot": result["ledger_snapshot"],
                "loader_version": BEANCOUNT_VERSION, "report_currency": "CNY",
                "approvals": [{"income_ref": group["income_ref"],
                               "reason": "虚拟账本逐项核对成本与收益"}],
            }, allow_unicode=True), encoding="utf-8")
            benefit_audit = audit_economic_benefits(root, date(2026, 9, 1), review, approval)
            self.assertEqual(benefit_audit["review_state"], "complete")
            self.assertEqual(benefit_audit["proved_benefits"], 1)
            self.assertEqual(benefit_audit["proved"][0]["proof"],
                             "approved_costed_sale_equation")
            stale = yaml.safe_load(approval.read_text(encoding="utf-8"))
            stale["ledger_snapshot"] = "stale"
            approval.write_text(yaml.safe_dump(stale, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(BudgetError) as error:
                audit_economic_benefits(root, date(2026, 9, 1), review, approval)
            self.assertEqual(error.exception.code, "BENEFIT_GROUP_INVALID")


class MultilotSaleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.review = self.base / "main.bean", self.base / "classes.yml"
        self.cutover = date(2026, 1, 1)

    def save(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def case(self, lots=((10, 2), (20, 3), (30, 4)), fees=(1, 2), gain=100):
        principal = sum(Fraction(qty) * Fraction(cost) for qty, cost in lots)
        receipt = principal + Fraction(gain) - sum(map(Fraction, fees))
        source = ('option "operating_currency" "CNY"\n'
                  '2025-12-01 open Assets:Cash\n2025-12-01 open Assets:Stock\n'
                  '2025-12-01 open Assets:Wallet\n2025-12-01 open Equity:Opening\n'
                  '2025-12-01 open Expenses:Fee\n2025-12-01 open Expenses:Tax\n'
                  '2025-12-01 open Income:Gain\n'
                  '2025-12-01 * "opening"\n  Assets:Cash 1000 CNY\n'
                  '  Assets:Wallet 100 CNY\n  Equity:Opening -1100 CNY\n')
        for qty, cost in lots:
            source += (f'2025-12-02 * "buy"\n  Assets:Stock {qty} SH {{{cost} CNY, 2025-12-02}}\n'
                       f'  Assets:Cash -{amount_text(Fraction(qty) * Fraction(cost))} CNY\n')
        source += '2026-01-12 * "sell"\n'
        for qty, cost in lots:
            source += f'  Assets:Stock -{qty} SH {{{cost} CNY, 2025-12-02}}\n'
        source += f'  Assets:Cash {amount_text(receipt)} CNY\n'
        for index, fee in enumerate(fees):
            account = "Expenses:Fee" if index == 0 else "Expenses:Tax"
            source += f'  {account} {fee} CNY\n    budget: "生活开销"\n'
        source += ('  Income:Gain 0 CNY\n' if Fraction(gain) == 0 else '  Income:Gain\n')
        source += '    budget: "投资"\n'  # Exercise importer-style booked balancing.
        self.root.write_text(source, encoding="utf-8")
        self.save(self.review, {
            "schema": 6, "cutover": self.cutover.isoformat(), "loader_version": BEANCOUNT_VERSION,
            "categories": [{"id": ident, "name": name, "parent": None, "aliases": []}
                           for ident, name in (("investment", "投资"), ("living", "生活开销"))],
            "classifications": []})
        return principal, receipt

    def audit(self, approved=None):
        return audit_economic_benefits(self.root, self.cutover, self.review, approved)

    def test_multiple_lots_and_fee_legs_prove_one_income_without_manual_equation_approval(self):
        self.case()
        result = self.audit()
        self.assertEqual((result["review_state"], result["proved_benefits"]), ("complete", 1))
        group = result["proved"][0]
        self.assertEqual(group["proof"], "exact_costed_sale_group_v1")
        self.assertEqual((group["principal_report_value"], group["income_report_value"],
                          group["fee_report_value"], group["cash_receipt_report_value"]), ("200", "100", "3", "297"))
        self.assertEqual(len(group["inventory_evidence_refs"]), 3)
        self.assertEqual(len(group["fee_evidence_refs"]), 2)
        self.assertEqual(sum(Fraction(row["principal_report_value"]) for row in group["inventory_components"]), 200)
        refs = group["inventory_evidence_refs"] + group["fee_evidence_refs"] + [group["cash_evidence_ref"]]
        self.assertEqual(len({row["transaction_sha256"] for row in refs}), 1)
        self.assertEqual(len({row["posting_ordinal"] for row in refs}), len(refs))
        spending = audit_monthly_spending(self.root, self.cutover, "2026-01", self.review, "CNY")
        self.assertEqual(spending["metrics"]["income_supplement"]["report_total"], "100")
        self.assertEqual(spending["metrics"]["net_expense"]["report_total"], "3")

    def test_lot_count_is_not_a_posting_limit(self):
        for count in (1, 2, 5, 25):
            with self.subTest(lots=count):
                principal, receipt = self.case(lots=[(1, cost) for cost in range(1, count + 1)])
                group = self.audit()["proved"][0]
                self.assertEqual(Fraction(group["principal_report_value"]), principal)
                self.assertEqual(Fraction(group["cash_receipt_report_value"]), receipt)
                self.assertEqual(len(group.get("inventory_evidence_refs", [group.get("inventory_evidence_ref")])), count)

    def test_losses_and_zero_income_use_the_same_group_without_turning_principal_into_income(self):
        for gain in (-10, 0):
            with self.subTest(gain=gain):
                self.case(gain=gain)
                group = self.audit()["proved"][0]
                self.assertEqual(group["income_report_value"], str(gain))
                spending = audit_monthly_spending(self.root, self.cutover, "2026-01", self.review, "CNY")
                self.assertEqual(spending["metrics"]["income_supplement"]["report_total"], str(gain))
                self.assertEqual(spending["metrics"]["net_expense"]["report_total"], "3")

    def test_no_fee_and_explicit_zero_fee_are_both_supported(self):
        for fees in ((), (0,)):
            with self.subTest(fees=fees):
                self.case(fees=fees)
                group = self.audit()["proved"][0]
                self.assertEqual(group["fee_report_value"], "0")
                self.assertEqual(group["cash_receipt_report_value"], "300")

    def test_complete_equation_does_not_choose_an_unknown_budget_category(self):
        self.case()
        document = yaml.safe_load(self.review.read_text(encoding="utf-8"))
        document["classifications"] = [row for row in document["classifications"]
                                        if row["slices"][0]["category_id"] != "investment"]
        self.save(self.review, document)
        self.root.write_text(self.root.read_text(encoding="utf-8").replace('    budget: "投资"\n', ''), encoding="utf-8")
        result = self.audit()
        self.assertEqual(result["review_state"], "pending")
        self.assertEqual(result["pending_benefits"][0]["reason_code"], "BENEFIT_CLASSIFICATION_PENDING")

    def test_old_explicit_group_review_is_validated_and_not_counted_twice(self):
        self.case()
        fin = load_financial(self.root)
        group = self.audit()["proved"][0]
        approved = self.base / "approved.yml"
        self.save(approved, {"schema": 6, "cutover": self.cutover.isoformat(), "ledger_snapshot": fin.digest,
            "loader_version": BEANCOUNT_VERSION, "report_currency": "CNY",
            "approvals": [{"income_ref": group["ref"], "reason": "existing reviewed group"}]})
        result = self.audit(approved)
        self.assertEqual(result["proved_benefits"], 1)
        self.assertEqual(result["proved"][0]["proof"], "approved_costed_sale_equation")
        self.root.write_text(self.root.read_text(encoding="utf-8").replace('"sell"', '"corrected sale"'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(approved)
        self.assertEqual(caught.exception.code, "BENEFIT_GROUP_REF_INVALID")
        self.assertTrue(caught.exception.source.startswith(str(approved) + ":"))

    def test_unbalanced_tolerated_source_is_not_certified(self):
        self.case()
        original = self.root.read_text(encoding="utf-8")
        self.root.write_text('option "inferred_tolerance_default" "CNY:1"\n' +
                             original.replace('  Income:Gain\n', '  Income:Gain -99 CNY\n'), encoding="utf-8")
        result = self.audit()
        self.assertEqual(result["proved_benefits"], 0)
        self.assertIn("BENEFIT_GROUP_SHAPE_UNSUPPORTED", {row["reason_code"] for row in result["pending_benefits"]})

    def test_an_extra_cash_counterpart_cannot_be_ignored_to_prove_a_subset(self):
        self.case()
        self.root.write_text(self.root.read_text(encoding="utf-8").replace(
            '  Assets:Cash 297 CNY\n', '  Assets:Cash 197 CNY\n  Assets:Wallet 100 CNY\n'), encoding="utf-8")
        result = self.audit()
        self.assertEqual(result["proved_benefits"], 0)
        self.assertEqual(result["pending_benefits"][0]["reason_code"], "BENEFIT_GROUP_SHAPE_UNSUPPORTED")

    def test_full_month_with_a_pool_keeps_fees_income_and_protected_principal_separate(self):
        self.case()
        setup = Path(create_setup(self.root, "2026-01", self.base / "setup",
                     income_purpose="自由资金", expense_purpose="生活费", expense_category="生活开销")["setup"])
        worksheet = yaml.safe_load(setup.read_text(encoding="utf-8"))
        worksheet["defaults"]["confirmed"] = worksheet["completeness_confirmed"] = True
        worksheet["expense_targets"] = {"生活开销": "100", "投资": "not_assessed"}
        worksheet["allocation_targets"] = {name: "not_assessed" for name in ("自由资金", "生活费", "储蓄", "投资")}
        for row in worksheet["accounts"]:
            row.update(semantics="H", reason="virtual reviewed account")
            row["payment_scope"] = {currency: "payable" if row["account"] == "Assets:Wallet" else "excluded"
                                    for currency in row["payment_scope"]}
            for coordinate in row["opening"]:
                coordinate["owners"] = {"生活费" if row["account"] == "Assets:Wallet" else "投资": coordinate["signed_units"]}
        self.save(setup, worksheet)
        config = Path(compile_setup(setup)["config"])
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        owners = yaml.safe_load((config.parent / document["ownership"]).read_text(encoding="utf-8"))
        ids = {row["name"]: row["id"] for row in owners["owners"]}
        opening = config.parent / document["opening"]
        opening_doc = yaml.safe_load(opening.read_text(encoding="utf-8"))
        opening_doc["h_holdings"] = [row for row in opening_doc["h_holdings"] if row["account"] == "Assets:Wallet"]
        self.save(opening, opening_doc)  # The pool does not require per-lot purpose approvals.
        document["purpose_pools"] = "pools.yml"
        self.save(config.parent / "pools.yml", {"schema": 6, "cutover": self.cutover.isoformat(), "report_currency": "CNY",
            "pools": [{"id": "broker", "accounts": ["Assets:Cash", "Assets:Stock"],
                       "reserve_order": ["Assets:Cash", "Assets:Stock"], "payment_scope": "excluded",
                       "protected_owner_id": ids["储蓄"], "protected_label": "储蓄", "protected_principal": "300",
                       "remainder_owner_id": ids["投资"], "remainder_label": "投资", "reason": "virtual pool"}]})
        self.save(config, document)
        outcome = audit_month_workflow(config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        report = outcome["report"]
        pool = report["purpose_pools"]["pools"][0]
        self.assertEqual((pool["protected_nominal_principal"], pool["remainder_book_cost"]), ("300", "797"))
        self.assertEqual(report["max_spendable"]["max_spendable"], "97")
        self.assertTrue(all(row["actual_g"] == "0" for row in report["purpose_progress"]["owners"]))
        sale = report["transactions"]["rows"][0]
        self.assertIn("到账 297 CNY＝成本本金合计 200 CNY＋损益 100 CNY－费用合计 3 CNY", "\n".join(
            render_transaction(sale, report["owner_names"])))


if __name__ == "__main__":
    unittest.main()

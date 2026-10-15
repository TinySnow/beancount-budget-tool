"""Prepare consecutive months without reseeding balances or reusing dated approvals."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_init import _SetupDumper, compile_setup
from beancount_budget_tool.tools.v6_next_month import create_next_month, compile_next_month, confirm_month, main
from beancount_budget_tool.tools.v6_review_decisions import create_review, apply_review
from beancount_budget_tool.v6.monthly_report import audit_month_workflow


class NextMonthTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        examples = Path(__file__).resolve().parents[1] / "examples" / "v6"
        self.ledger = self.base / "main.bean"
        self.ledger.write_bytes((examples / "first-month.bean").read_bytes())
        self.ledger.write_text(self.ledger.read_text() +
            '2026-10-02 * "October salary"\n'
            '  Assets:Cash 500 CNY\n  Income:Salary -500 CNY\n'
            '2026-10-03 * "October living allocation"\n'
            '  Assets:Cash 200 CNY\n    fund: "待支出资金"\n'
            '  Assets:Cash -200 CNY\n    fund: "已收入资金"\n'
            '2026-10-04 * "October food"\n'
            '  Assets:Cash -20 CNY\n  Expenses:Food 20 CNY\n', encoding="utf-8")
        setup = yaml.safe_load((examples / "first-month-setup.yaml").read_text())
        setup["ledger"] = "main.bean"
        self.save(self.base / "setup.yaml", setup)
        self.config = Path(compile_setup(self.base / "setup.yaml")["config"])
        self.originals = {path: path.read_bytes() for path in [self.ledger, *self.config.parent.glob("*.yaml")]}
        self.output = self.base / "october-setup"
        self.worksheet = self.output / "next-month.yaml"

    def save(self, path, document):
        path.write_text(yaml.dump(document, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def create(self):
        return create_next_month(self.config, self.output)

    def approve(self, complete=True):
        document = yaml.safe_load(self.worksheet.read_text())
        document["targets_confirmed"] = True
        document["completeness_confirmed"] = complete
        document["completeness_reason"] = "virtual October imports and responsibilities checked" if complete else None
        for row in document["account_changes"]:
            row["semantics"] = row["semantics"] or "H"
            row["payment_scope"] = {currency: "payable" if currency == "CNY" else "excluded"
                                    for currency in row["payment_scope"]}
            row["reason"] = "virtual account reviewed"
        self.save(self.worksheet, document)
        return document

    def test_draft_does_not_approve_goals_or_month_end(self):
        result = self.create()
        self.assertEqual(result["month"], "2026-10")
        self.assertEqual(result["changed_accounts"], 0)
        self.assertEqual(result["booked_transactions_in_month"], 3)
        document = yaml.safe_load(self.worksheet.read_text())
        self.assertEqual(document["cutover"], "2026-09-01")
        self.assertEqual(document["expense_subtree_targets"], {"日常开销": "200"})
        self.assertFalse(document["targets_confirmed"])
        self.assertFalse(document["completeness_confirmed"])
        self.assertEqual(compile_next_month(self.worksheet)["status"], "NEEDS_REVIEW")
        self.assertFalse((self.output / "compiled").exists())
        self.assertTrue(all(path.read_bytes() == content for path, content in self.originals.items()))

    def test_confirmed_next_month_replays_original_cutover_and_keeps_old_month(self):
        self.create()
        self.approve()
        result = compile_next_month(self.worksheet)
        self.assertEqual(result["status"], "MONTH_PREPARED")
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED", result["workflow"]["review_items"])
        report = result["workflow"]["report"]
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "370")
        self.assertEqual(report["max_spendable"]["max_spendable"], "180")
        config = yaml.safe_load(Path(result["config"]).read_text())
        self.assertEqual(config["cutover"], "2026-09-01")
        self.assertEqual((Path(result["config"]).parent / config["opening"]).resolve(), self.config.parent / "opening.yaml")
        self.assertEqual(audit_month_workflow(self.config)["report"]["max_spendable"]["max_spendable"], "190")
        self.assertTrue(all(path.read_bytes() == content for path, content in self.originals.items()))

    def test_month_without_new_completeness_stays_pending(self):
        self.create()
        self.approve(complete=False)
        result = compile_next_month(self.worksheet)
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["workflow"]["report"])
        self.assertTrue(any(row["reason_code"] == "EXTERNAL_SCOPE_PENDING" for row in result["workflow"]["review_items"]))
        config = yaml.safe_load(Path(result["config"]).read_text())
        self.assertNotIn("completeness", config)

    def test_dated_debt_coverage_and_quotes_are_not_carried(self):
        settings = yaml.safe_load(self.config.read_text())
        for field in ("debt_coverage", "debt_settlements"):
            path = self.config.parent / (field + ".yaml")
            self.save(path, {"schema": 6, "as_of": "2026-09-30", "marker": "old date only"})
            settings[field] = path.name
        self.save(self.config, settings)
        self.create()
        notes = yaml.safe_load((self.output / "preparation.yaml").read_text())
        self.assertEqual(set(notes["not_reused"]), {"completeness", "debt_coverage", "debt_settlements"})
        self.approve()
        result = compile_next_month(self.worksheet)
        settings = yaml.safe_load(Path(result["config"]).read_text())
        self.assertNotIn("debt_coverage", settings)
        self.assertNotIn("debt_settlements", settings)
        statement = yaml.safe_load((Path(result["config"]).parent / settings["completeness"]).read_text())
        self.assertEqual(statement["as_of"], "2026-10-31")

    def test_future_template_and_override_have_priority_over_previous_proposals(self):
        expense = self.config.parent / "expense_plans.yaml"
        document = yaml.safe_load(expense.read_text())
        ident = next(iter(document["months"]["2026-09"]["expense_subtree_targets"]))
        document["effective_templates"] = {"2026-10": {"expense_subtree_targets": {ident: "210.25"}}}
        document["months"]["2026-10"] = {"expense_subtree_targets": {ident: "220.125"}}
        self.save(expense, document)
        purpose = self.config.parent / "purpose_plans.yaml"
        goals = yaml.safe_load(purpose.read_text())
        ident = next(key for key, value in goals["months"]["2026-09"]["allocation_targets"].items() if value == "200")
        goals["effective_templates"] = {"2026-10": {"allocation_targets": {ident: "205.50"}}}
        self.save(purpose, goals)
        self.create()
        document = yaml.safe_load(self.worksheet.read_text())
        self.assertEqual(document["expense_subtree_targets"], {"日常开销": "220.125"})
        self.assertEqual(document["allocation_targets"]["待支出资金"], "205.5")
        self.approve()
        result = compile_next_month(self.worksheet)
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        saved = yaml.safe_load((Path(result["config"]).parent / "expense_plans.yaml").read_text())
        self.assertEqual(saved["months"]["2026-10"]["expense_subtree_targets"], {next(iter(saved["months"]["2026-09"]["expense_subtree_targets"])): "220.125"})

    def test_new_h_account_is_reviewed_once_then_used_without_new_opening_money(self):
        self.ledger.write_text(self.ledger.read_text() +
            '2026-10-01 open Assets:NewBank\n'
            '2026-10-05 * "move free funds"\n'
            '  Assets:Cash -30 CNY\n    fund: "已收入资金"\n'
            '  Assets:NewBank 30 CNY\n', encoding="utf-8")
        self.assertEqual(self.create()["changed_accounts"], 1)
        self.assertEqual(compile_next_month(self.worksheet)["status"], "NEEDS_REVIEW")
        self.approve()
        result = compile_next_month(self.worksheet)
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["workflow"]["report"]["max_spendable"]["max_spendable"], "180")

    def test_new_currency_in_known_account_keeps_its_semantics(self):
        self.ledger.write_text(self.ledger.read_text() +
            '2026-10-05 * "USD gift"\n'
            '  Assets:Cash 10 USD\n  Income:Salary -10 USD\n', encoding="utf-8")
        self.create()
        document = self.approve()
        self.assertEqual(document["account_changes"][0]["payment_scope"], {"USD": "excluded"})
        self.assertEqual(document["account_changes"][0]["semantics"], "H")
        result = compile_next_month(self.worksheet)
        saved = yaml.safe_load((Path(result["config"]).parent / "accounts.yaml").read_text())
        self.assertEqual(saved["accounts"][0]["payment_scope"], {"CNY": "payable", "USD": "excluded"})

    def test_new_zero_opening_k_is_acknowledged_as_book_flow_not_h_money(self):
        self.ledger.write_text(self.ledger.read_text() +
            '2026-10-01 open Assets:FundBook\n'
            '2026-10-05 * "subscription"\n'
            '  Assets:Cash -30 CNY\n    fund: "已收入资金"\n'
            '  Assets:FundBook 30 CNY\n    fund: "已收入资金"\n', encoding="utf-8")
        self.create()
        document = self.approve()
        document["account_changes"][0].update(semantics="K", payment_scope={"CNY": "excluded"})
        self.save(self.worksheet, document)
        result = compile_next_month(self.worksheet)
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED", result.get("report_error"))
        opening = yaml.safe_load((Path(result["config"]).parent / "opening.yaml").read_text())
        self.assertEqual(opening["k_openings"][0]["signed_units"], "0")
        self.assertEqual(opening["h_holdings"], [])
        self.assertEqual(result["workflow"]["report"]["actual_spendable"]["actual_spendable"], "370")

    def test_missing_targets_invalid_names_amounts_and_confirmation_have_positions(self):
        self.create()
        original = self.approve()
        for key, value in (("allocation_targets", {}), ("expense_subtree_targets", {"日常开销": None}),
                           ("targets_confirmed", False), ("completeness_reason", None)):
            with self.subTest(key=key):
                document = yaml.safe_load(yaml.safe_dump(original))
                document[key] = value
                self.save(self.worksheet, document)
                result = compile_next_month(self.worksheet)
                self.assertEqual(result["status"], "NEEDS_REVIEW")
                self.assertTrue(all(row["source"].startswith(str(self.worksheet) + ":") for row in result["review_items"]))
                self.assertFalse((self.output / "compiled").exists())
        original["expense_subtree_targets"] = {"unknown": "10"}
        self.save(self.worksheet, original)
        with self.assertRaises(BudgetError) as caught:
            compile_next_month(self.worksheet)
        self.assertEqual(caught.exception.code, "NEXT_MONTH_NAME_UNKNOWN")

    def test_new_import_after_draft_requires_account_changes_to_be_revisited(self):
        self.create()
        self.approve()
        self.ledger.write_text(self.ledger.read_text() +
            '2026-10-01 open Assets:NewBank\n'
            '2026-10-05 * "new salary bank"\n'
            '  Assets:NewBank 10 CNY\n  Income:Salary -10 CNY\n', encoding="utf-8")
        result = compile_next_month(self.worksheet)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["config"])

    def test_year_transition_empty_month_and_overwrite_guard(self):
        settings = yaml.safe_load(self.config.read_text())
        settings["month"] = "2026-12"
        self.save(self.config, settings)
        with redirect_stdout(StringIO()) as captured:
            self.assertEqual(main(["--config", str(self.config), "--output", str(self.output)]), 0)
        self.assertIn("2027-01", captured.getvalue())
        self.assertIn("本月没有已记交易", captured.getvalue())
        with self.assertRaises(BudgetError):
            self.create()

    def test_context_change_or_existing_semantics_change_is_rejected(self):
        self.create()
        document = self.approve()
        document["cutover"] = "2026-10-01"
        self.save(self.worksheet, document)
        with self.assertRaises(BudgetError) as caught:
            compile_next_month(self.worksheet)
        self.assertEqual(caught.exception.code, "NEXT_MONTH_CONTEXT_CHANGED")

    def test_ambiguous_new_transfer_is_reviewed_after_month_preparation(self):
        self.ledger.write_text(self.ledger.read_text().replace(
            '  Assets:Cash -200 CNY\n    fund: "已收入资金"\n',
            '  Assets:Cash -200 CNY\n'), encoding="utf-8")
        self.create()
        self.approve()
        prepared = compile_next_month(self.worksheet)
        self.assertEqual(prepared["workflow"]["status"], "NEEDS_REVIEW")
        draft = create_review(Path(prepared["config"]), self.base / "decisions")
        self.assertEqual(draft["editable_items"], 1)
        sheet = Path(draft["worksheet"])
        document = yaml.safe_load(sheet.read_text())
        document["rows"][0].update(decision="已收入资金", reason="salary funds assigned to living", approved=True)
        self.save(sheet, document)
        result = apply_review(sheet)
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["workflow"]["report"]["max_spendable"]["max_spendable"], "180")

    def test_original_approved_account_semantics_cannot_be_changed_in_month_preparation(self):
        accounts_path = self.config.parent / "accounts.yaml"
        accounts = yaml.safe_load(accounts_path.read_text())
        accounts["accounts"][0]["payment_scope"]["CNY"] = "pending"
        self.save(accounts_path, accounts)
        self.create()
        document = self.approve()
        document["account_changes"][0].update(semantics="K", payment_scope={"CNY": "excluded"})
        self.save(self.worksheet, document)
        with self.assertRaises(BudgetError) as caught:
            compile_next_month(self.worksheet)
        self.assertEqual(caught.exception.code, "NEXT_MONTH_SEMANTICS_CHANGED")

    def test_deleting_an_inherited_target_cannot_silently_restore_it(self):
        expense = self.config.parent / "expense_plans.yaml"
        plans = yaml.safe_load(expense.read_text())
        plans["defaults"] = {"expense_targets": {"category_1": "10"}}
        self.save(expense, plans)
        self.create()
        document = self.approve()
        document["expense_targets"] = {}
        self.save(self.worksheet, document)
        result = compile_next_month(self.worksheet)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["config"])
        self.assertTrue(any("原计划已为本月声明" in item["detail"] for item in result["review_items"]))

    def test_month_end_confirmation_preserves_latest_transaction_decisions(self):
        self.ledger.write_text(self.ledger.read_text().replace(
            '  Assets:Cash -200 CNY\n    fund: "已收入资金"\n',
            '  Assets:Cash -200 CNY\n'), encoding="utf-8")
        self.create()
        self.approve(complete=False)
        prepared = compile_next_month(self.worksheet)
        draft = create_review(Path(prepared["config"]), self.base / "decisions")
        sheet = Path(draft["worksheet"])
        document = yaml.safe_load(sheet.read_text())
        document["rows"][0].update(decision="已收入资金", reason="salary allocation reviewed", approved=True)
        self.save(sheet, document)
        reviewed = apply_review(sheet)
        self.assertEqual(reviewed["workflow"]["status"], "NEEDS_REVIEW")
        self.assertTrue(any(row["reason_code"] == "EXTERNAL_SCOPE_PENDING" for row in reviewed["workflow"]["review_items"]))
        closed = confirm_month(Path(reviewed["config"]), "October imports and liabilities checked", self.base / "closed")
        self.assertEqual(closed["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertEqual(closed["workflow"]["report"]["max_spendable"]["max_spendable"], "180")
        settings = yaml.safe_load(Path(closed["config"]).read_text())
        current = yaml.safe_load(Path(reviewed["config"]).read_text())
        self.assertEqual((Path(closed["config"]).parent / settings["ownership"]).resolve(),
                         (Path(reviewed["config"]).parent / current["ownership"]).resolve())

    def test_confirm_month_still_requires_all_other_report_evidence(self):
        self.ledger.write_text(self.ledger.read_text().replace(
            '  Assets:Cash -200 CNY\n    fund: "已收入资金"\n',
            '  Assets:Cash -200 CNY\n'), encoding="utf-8")
        self.create()
        self.approve(complete=False)
        prepared = compile_next_month(self.worksheet)
        result = confirm_month(Path(prepared["config"]), "month imports complete", self.base / "closed")
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["workflow"]["report"])
        with self.assertRaises(BudgetError):
            confirm_month(Path(prepared["config"]), "", self.base / "other")


if __name__ == "__main__":
    unittest.main()

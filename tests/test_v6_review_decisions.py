"""A monthly review worksheet saves explicit choices with exact source claims."""

from contextlib import redirect_stdout
from fractions import Fraction
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_init import _SetupDumper, create_setup, compile_setup
from beancount_budget_tool.tools.v6_review_decisions import create_review, apply_review, main
from beancount_budget_tool.v6.monthly_report import audit_month_workflow


class ReviewDecisionsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.ledger = self.base / "main.bean"
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Income:Salary\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-01 open Liabilities:Card\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Cash 100 CNY\n'
            '  Equity:Opening -100 CNY\n'
            '2026-09-02 * "bank" "mixed cash move"\n'
            '  Assets:Cash -20 CNY\n'
            '  Assets:Bank 20 CNY\n'
            '2026-09-03 * "food"\n'
            '  Assets:Cash -10 CNY\n'
            '  Expenses:Food 10 CNY\n'
            '2026-09-04 * "salary"\n'
            '  Assets:Cash 5 CNY\n'
            '  Income:Salary -5 CNY\n', encoding="utf-8")
        setup = Path(create_setup(self.ledger, "2026-09", self.base / "setup",
                                  income_purpose="自由资金", expense_purpose="生活费",
                                  expense_category="生活开销")["setup"])
        document = yaml.safe_load(setup.read_text())
        document["defaults"]["confirmed"] = True
        document["completeness_confirmed"] = True
        document["expense_targets"] = {"生活开销": "200"}
        document["allocation_targets"] = {name: "not_assessed" for name in document["allocation_targets"]}
        for account in document["accounts"]:
            account.update(semantics="H", reason="reviewed virtual cash")
            account["payment_scope"] = {"CNY": "payable"}
            for opening in account["opening"]:
                opening["owners"] = {"自由资金": "50", "生活费": "50"}
        self.save(setup, document)
        self.config = Path(compile_setup(setup)["config"])
        categories = self.config.parent / "classifications.yaml"
        document = yaml.safe_load(categories.read_text())
        del document["defaults"]
        self.save(categories, document)
        self.originals = {path: path.read_bytes() for path in [self.ledger, *self.config.parent.glob("*.yaml")]}
        self.export = self.base / "review"
        self.sheet = self.export / "decisions.yaml"

    def save(self, path, value):
        path.write_text(yaml.dump(value, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def create(self):
        return create_review(self.config, self.export)

    def approve(self, *, split=False):
        document = yaml.safe_load(self.sheet.read_text())
        for row in document["rows"]:
            row["approved"] = True
            row["reason"] = "reviewed virtual transaction"
            row["decision"] = (("自由资金10，生活费10" if split else "自由资金")
                               if row["kind"] == "asset_ownership" else
                               "生活开销" if row["account"].startswith("Expenses:") else "自由资金")
        self.save(self.sheet, document)
        return document

    def test_draft_explains_choices_and_never_approves_or_changes_inputs(self):
        result = self.create()
        self.assertEqual(result["editable_items"], 4)
        self.assertEqual(result["other_items"], 1)  # Transfer protocol waits for both owners.
        document = yaml.safe_load(self.sheet.read_text())
        self.assertTrue(all(row["approved"] is False and row["decision"] is None for row in document["rows"]))
        self.assertIn("mixed cash move", [row["narration"] for row in document["rows"]])
        self.assertTrue(all(row["source"].startswith(str(self.ledger) + ":") for row in document["rows"]))
        self.assertEqual(yaml.safe_load((self.export / "other-items.yaml").read_text())["items"][0]["kind"], "h_transfer")
        applied = apply_review(self.sheet)
        self.assertEqual(applied["status"], "NO_APPROVED_DECISIONS")
        self.assertIsNone(applied["config"])
        self.assertFalse((self.export / "reviewed").exists())
        self.assertTrue(all(path.read_bytes() == content for path, content in self.originals.items()))

    def test_full_review_reaches_monthly_report_without_copying_refs(self):
        self.create()
        self.approve()
        result = apply_review(self.sheet)
        self.assertEqual(result["status"], "DECISIONS_SAVED")
        self.assertEqual(len(result["accepted"]), 4)
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["workflow"]["report"]["max_spendable"]["max_spendable"], "40")
        output = Path(result["config"])
        self.assertEqual(audit_month_workflow(output)["status"], "CERTIFIED_SCOPED")
        self.assertTrue(all(path.read_bytes() == content for path, content in self.originals.items()))
        self.assertFalse(Path(yaml.safe_load(output.read_text())["ledger"]).is_absolute())

    def test_multi_purpose_decisions_keep_native_signs_and_exact_amounts(self):
        self.create()
        self.approve(split=True)
        result = apply_review(self.sheet)
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        saved = yaml.safe_load((Path(result["config"]).parent / "ownership.yaml").read_text())
        amounts = [sum((Fraction(p["signed_units"]) for p in row["slices"]), Fraction(0)) for row in saved["claims"]]
        self.assertEqual(sorted(amounts), [-20, 20])
        self.assertTrue(all(p["role"] == "hold" for row in saved["claims"] for p in row["slices"]))

    def test_partial_review_saves_only_approved_decisions_and_preserves_pending(self):
        self.create()
        document = yaml.safe_load(self.sheet.read_text())
        row = next(row for row in document["rows"] if row["kind"] == "classification")
        row.update(approved=True, decision="生活开销" if row["account"].startswith("Expenses:") else "自由资金",
                   reason="one decision at a time")
        self.save(self.sheet, document)
        result = apply_review(self.sheet)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["workflow"]["report"])
        self.assertEqual(sum(item["kind"] == "classification" for item in result["workflow"]["review_items"]), 1)
        next_draft = create_review(Path(result["config"]), self.base / "remaining")
        self.assertEqual(next_draft["editable_items"], 3)

    def test_approved_unknown_names_wrong_totals_and_missing_reasons_do_not_write(self):
        self.create()
        original = self.approve()
        for field, value, code in (("decision", "不存在", "REVIEW_DECISION_INVALID"),
                                   ("decision", "自由资金9，生活费9", "REVIEW_DECISION_INVALID"),
                                   ("reason", None, "REVIEW_REASON_REQUIRED"),
                                   ("approved", "yes", "REVIEW_WORKSHEET_INVALID")):
            with self.subTest(field=field, value=value):
                document = yaml.safe_load(yaml.safe_dump(original))
                row = next(row for row in document["rows"] if row["kind"] == "asset_ownership")
                row[field] = value
                self.save(self.sheet, document)
                with self.assertRaises(BudgetError) as caught:
                    apply_review(self.sheet)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.source.startswith(str(self.sheet) + ":"))
                self.assertFalse((self.export / "reviewed").exists())

    def test_changed_transaction_or_new_inline_claim_requires_fresh_review(self):
        self.create()
        self.approve()
        original = self.ledger.read_text()
        for changed in (original.replace('"mixed cash move"', '"corrected transfer"'),
                        original.replace('  Assets:Cash -20 CNY\n',
                                         '  Assets:Cash -20 CNY\n    fund: "自由资金"\n')):
            with self.subTest(changed=changed):
                self.ledger.write_text(changed, encoding="utf-8")
                with self.assertRaises(BudgetError) as caught:
                    apply_review(self.sheet)
                self.assertEqual(caught.exception.code, "REVIEW_SOURCE_CHANGED")
                self.assertFalse((self.export / "reviewed").exists())

    def test_unrelated_added_transaction_keeps_old_source_decisions_usable(self):
        self.create()
        self.approve()
        self.ledger.write_text(self.ledger.read_text() + '2026-09-05 * "new food"\n'
                              '  Assets:Cash -1 CNY\n  Expenses:Food 1 CNY\n', encoding="utf-8")
        result = apply_review(self.sheet)
        self.assertEqual(len(result["accepted"]), 4)
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")
        self.assertEqual(result["workflow"]["review_items"][0]["account"], "Expenses:Food")

    def test_budget_outside_does_not_erase_default_payer_funds(self):
        self.create()
        document = self.approve()
        next(row for row in document["rows"] if row["account"] == "Expenses:Food")["decision"] = "-"
        self.save(self.sheet, document)
        result = apply_review(self.sheet)
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["workflow"]["report"]["default_outflows"]["totals"][0]["expenses_paid"], "10")

    def test_duplicate_rows_or_changed_context_are_rejected(self):
        self.create()
        original = self.approve()
        original["rows"].append(original["rows"][0])
        self.save(self.sheet, original)
        with self.assertRaises(BudgetError) as caught:
            apply_review(self.sheet)
        self.assertEqual(caught.exception.code, "REVIEW_DUPLICATE")
        original["month"] = "2026-10"
        self.save(self.sheet, original)
        with self.assertRaises(BudgetError) as caught:
            apply_review(self.sheet)
        self.assertEqual(caught.exception.code, "REVIEW_CONTEXT_CHANGED")

    def test_new_output_is_required_and_cli_renders_report(self):
        self.create()
        self.approve()
        with redirect_stdout(StringIO()) as captured:
            self.assertEqual(main(["--apply", str(self.sheet)]), 0)
        self.assertIn("最多能花：40 CNY", captured.getvalue())
        with self.assertRaises(BudgetError) as caught:
            apply_review(self.sheet)
        self.assertEqual(caught.exception.code, "SETUP_OUTPUT_EXISTS")
        with self.assertRaises(BudgetError):
            create_review(self.config, self.export)

    def test_later_plan_error_reports_saved_configuration_and_real_source(self):
        self.create()
        self.approve()
        plan = self.config.parent / "expense_plans.yaml"
        document = yaml.safe_load(plan.read_text())
        document["months"]["2026-09"]["expense_subtree_targets"]["category_2"] = "bad"
        self.save(plan, document)
        result = apply_review(self.sheet)
        self.assertEqual(result["status"], "DECISIONS_SAVED")
        self.assertTrue(Path(result["config"]).is_file())
        self.assertIsNone(result["workflow"])
        self.assertTrue(result["report_error"]["source"].startswith(str(plan)))

    def test_labelled_debt_is_still_pending_separate_obligation_review(self):
        self.ledger.write_text(self.ledger.read_text() + '2026-09-05 * "card food"\n'
                              '  Expenses:Food 2 CNY\n  Liabilities:Card -2 CNY\n', encoding="utf-8")
        self.create()
        self.approve()
        result = apply_review(self.sheet)
        self.assertEqual(result["workflow"]["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["workflow"]["report"])
        self.assertTrue(any(item["kind"] == "booked_obligation" for item in result["workflow"]["review_items"]))

    def test_voluntary_purpose_overdraft_is_rejected_before_publishing(self):
        opening_path = self.config.parent / "opening.yaml"
        opening = yaml.safe_load(opening_path.read_text())
        owners = yaml.safe_load((self.config.parent / "ownership.yaml").read_text())
        free = next(row["id"] for row in owners["owners"] if row["name"] == "自由资金")
        for row in opening["h_holdings"]:
            row["units"] = "10" if row["owner"] == free else "90"
        self.save(opening_path, opening)
        self.create()
        self.approve()
        with self.assertRaises(BudgetError) as caught:
            apply_review(self.sheet)
        self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")
        self.assertFalse((self.export / "reviewed").exists())

    def test_next_month_reuses_saved_decisions_and_keeps_old_report_valid(self):
        self.create()
        self.approve()
        september = Path(apply_review(self.sheet)["config"])
        self.ledger.write_text(self.ledger.read_text() + '2026-10-03 * "October food"\n'
                              '  Assets:Cash -3 CNY\n  Expenses:Food 3 CNY\n', encoding="utf-8")
        october_folder = self.base / "october"
        october_folder.mkdir()
        settings = yaml.safe_load(september.read_text())
        for key, value in list(settings.items()):
            if key not in {"schema", "cutover", "month", "report_currency"}:
                settings[key] = str((september.parent / value).resolve())
        settings["month"] = "2026-10"
        for key in ("expense_plans", "purpose_plans", "completeness"):
            document = yaml.safe_load(Path(settings[key]).read_text())
            if key == "completeness":
                document["as_of"] = "2026-10-31"
            else:
                document["months"]["2026-10"] = document["months"]["2026-09"]
            path = october_folder / (key + ".yaml")
            self.save(path, document)
            settings[key] = str(path)
        october = october_folder / "month.yaml"
        self.save(october, settings)
        draft = create_review(october, october_folder / "review")
        self.assertEqual(draft["editable_items"], 1)
        self.assertEqual(draft["other_items"], 0)
        sheet = Path(draft["worksheet"])
        document = yaml.safe_load(sheet.read_text())
        self.assertEqual(document["rows"][0]["narration"], "October food")
        document["rows"][0].update(approved=True, decision="生活开销", reason="October food reviewed")
        self.save(sheet, document)
        result = apply_review(sheet)
        self.assertIsNotNone(result["workflow"], result.get("report_error"))
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["workflow"]["report"]["max_spendable"]["max_spendable"], "37")
        self.assertEqual(audit_month_workflow(september)["report"]["max_spendable"]["max_spendable"], "40")


if __name__ == "__main__":
    unittest.main()

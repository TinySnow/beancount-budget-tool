"""A first-month worksheet can reach a report without inventing approvals."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_init import create_setup, compile_setup, main
from beancount_budget_tool.v6.monthly_report import audit_month_workflow


class InitTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ledger = self.base / "main.bean"
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Income:Salary\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Cash 100 CNY\n'
            '  Equity:Opening -100 CNY\n'
            '2026-09-02 * "salary"\n'
            '  Assets:Cash 500 CNY\n'
            '  Income:Salary -500 CNY\n'
            '2026-09-03 * "allocate living"\n'
            '  Assets:Cash 200 CNY\n    fund: "生活费"\n'
            '  Assets:Cash -200 CNY\n'
            '2026-09-04 * "food"\n'
            '  Expenses:Food 10 CNY\n    budget: "生活开销.餐饮"\n'
            '  Assets:Cash -10 CNY\n'
            '2026-09-05 * "move"\n'
            '  Assets:Cash -20 CNY\n    fund: "生活费"\n'
            '  Assets:Bank 20 CNY\n    fund: "生活费"\n'
            '2026-10-02 * "future not migrated"\n'
            '  budget: "legacy transaction metadata"\n'
            '  Expenses:Food 1 CNY\n'
            '  Assets:Cash -1 CNY\n', encoding="utf-8")
        self.destination = self.base / "setup"
        self.initial_bytes = self.ledger.read_bytes()

    def create(self):
        result = create_setup(self.ledger, "2026-09", self.destination,
                              income_purpose="自由资金", expense_purpose="生活费",
                              expense_category="生活开销")
        self.setup = Path(result["setup"])
        return result

    def save(self, document):
        self.setup.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def approve(self):
        doc = yaml.safe_load(self.setup.read_text(encoding="utf-8"))
        self.assertFalse(Path(doc["ledger"]).is_absolute())
        doc["defaults"]["confirmed"] = True
        doc["completeness_confirmed"] = True
        for row in doc["accounts"]:
            row["semantics"] = "H"
            row["payment_scope"] = {commodity: "payable" for commodity in row["payment_scope"]}
            row["reason"] = "virtual directly payable cash"
            for coordinate in row["opening"]:
                coordinate["owners"] = {"自由资金": coordinate["signed_units"]}
        doc["expense_targets"] = {"生活开销": "200"}
        doc["allocation_targets"] = {name: "not_assessed" for name in doc["allocation_targets"]}
        self.save(doc)
        return doc

    def test_draft_never_approves_accounts_openings_or_completeness(self):
        created = self.create()
        self.assertEqual(created["status"], "DRAFT_CREATED")
        self.assertEqual(created["source_issues"], [])  # Future v6 syntax does not block.
        doc = yaml.safe_load(self.setup.read_text(encoding="utf-8"))
        self.assertEqual([row["account"] for row in doc["accounts"]], ["Assets:Bank", "Assets:Cash"])
        self.assertIsNone(doc["accounts"][0]["semantics"])
        self.assertEqual(doc["accounts"][1]["opening"][0]["owners"], {})
        outcome = compile_setup(self.setup)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["config"])
        self.assertFalse((self.destination / "compiled").exists())
        self.assertTrue(all(item["source"].startswith(str(self.setup) + ":") for item in outcome["review_items"]))
        self.assertEqual(self.ledger.read_bytes(), self.initial_bytes)

    def test_approved_worksheet_reaches_complete_report(self):
        self.create()
        self.approve()
        result = compile_setup(self.setup)
        self.assertEqual(result["status"], "CONFIG_CREATED")
        outcome = audit_month_workflow(Path(result["config"]))
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED")
        report = outcome["report"]
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "190")
        self.assertEqual(report["max_spendable"]["max_spendable"], "190")
        self.assertEqual(self.ledger.read_bytes(), self.initial_bytes)

    def test_opening_overallocation_and_incomplete_targets_stay_pending(self):
        self.create()
        doc = self.approve()
        doc["accounts"][1]["opening"][0]["owners"] = {"自由资金": "101"}
        doc["expense_targets"] = {}
        doc["allocation_targets"]["生活费"] = None
        self.save(doc)
        result = compile_setup(self.setup)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertGreaterEqual(len(result["review_items"]), 3)
        self.assertFalse((self.destination / "compiled").exists())

    def test_stale_opening_is_rejected_and_existing_output_is_preserved(self):
        self.create()
        doc = self.approve()
        doc["accounts"][1]["opening"][0]["signed_units"] = "101"
        self.save(doc)
        with self.assertRaises(BudgetError) as caught:
            compile_setup(self.setup)
        self.assertEqual(caught.exception.code, "SETUP_OPENING_CHANGED")
        with self.assertRaises(BudgetError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "SETUP_OUTPUT_EXISTS")
        self.approve()
        doc["accounts"][1]["opening"][0]["signed_units"] = "100"
        doc["accounts"][1]["opening"][0]["owners"] = {"自由资金": "100"}
        self.save(doc)
        result = compile_setup(self.setup)
        marker = Path(result["config"]).parent / "user.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            compile_setup(self.setup)
        self.assertEqual(caught.exception.code, "SETUP_OUTPUT_EXISTS")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_cli_json_draft_and_pending_apply(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--ledger", str(self.ledger), "--month", "2026-09",
                         "--output", str(self.destination), "--format", "json"])
        self.assertEqual(code, 0)
        import json
        self.assertEqual(json.loads(output.getvalue())["status"], "DRAFT_CREATED")
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--apply", str(self.destination / "setup.yaml"), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "NEEDS_REVIEW")

    def test_negative_k_opening_stays_book_flow_and_can_reach_report(self):
        source = self.ledger.read_text(encoding="utf-8")
        source = source.replace('2026-08-01 open Equity:Opening',
                                '2026-08-01 open Assets:FundBook\n2026-08-01 open Equity:Opening')
        source = source.replace('  Equity:Opening -100 CNY',
                                '  Assets:FundBook -25 CNY\n  Equity:Opening -75 CNY')
        self.ledger.write_text(source, encoding="utf-8")
        self.create()
        doc = self.approve()
        fund = next(row for row in doc["accounts"] if row["account"] == "Assets:FundBook")
        fund["semantics"] = "K"
        fund["payment_scope"] = {"CNY": "excluded"}
        fund["opening"][0]["owners"] = {}
        self.save(doc)
        result = compile_setup(self.setup)
        opening = yaml.safe_load((Path(result["config"]).parent / "opening.yaml").read_text(encoding="utf-8"))
        self.assertEqual(opening["k_openings"][0]["signed_units"], "-25")
        self.assertNotIn("Assets:FundBook", {row["account"] for row in opening["h_holdings"]})
        self.assertEqual(audit_month_workflow(Path(result["config"]))["status"], "CERTIFIED_SCOPED")

    def test_fractional_native_opening_and_outside_budget_ownership(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            '  Assets:Cash 100 CNY', '  Assets:Cash 0.5 CNY').replace(
            '  Equity:Opening -100 CNY', '  Equity:Opening -0.5 CNY').replace(
            '  Assets:Cash -200 CNY', '  Assets:Cash -200 CNY\n    fund: "自由资金"'), encoding="utf-8")
        self.create()
        doc = self.approve()
        self.assertEqual(doc["accounts"][1]["opening"][0]["signed_units"], "1/2")
        doc["accounts"][1]["opening"][0]["owners"] = {"-": "1/2"}
        self.save(doc)
        result = compile_setup(self.setup)
        outcome = audit_month_workflow(Path(result["config"]))
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED")

    def test_multiple_operating_currencies_require_explicit_selection(self):
        self.ledger.write_text('option "operating_currency" "USD"\n' +
                               self.ledger.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "REPORT_CURRENCY_REQUIRED")
        self.assertFalse(self.destination.exists())
        result = create_setup(self.ledger, "2026-09", self.destination, currency="CNY")
        doc = yaml.safe_load(Path(result["setup"]).read_text(encoding="utf-8"))
        self.assertEqual(doc["report_currency"], "CNY")

    def test_legacy_current_tags_stay_source_review_instead_of_becoming_approved_rules(self):
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            '2026-09-04 * "food"', '2026-09-04 * "food"\n  budget: "old category"'), encoding="utf-8")
        result = self.create()
        self.assertTrue(result["source_issues"])
        self.approve()
        pending = compile_setup(self.setup)
        self.assertEqual(pending["status"], "NEEDS_REVIEW")
        self.assertTrue(any(item["source"].startswith(str(self.ledger.resolve())) for item in pending["review_items"]))
        self.assertFalse((self.destination / "compiled").exists())

    def test_public_first_month_example_reaches_documented_report(self):
        example = Path(__file__).resolve().parents[1] / "examples/v6/first-month-setup.yaml"
        result = compile_setup(example, self.base / "public-example")
        outcome = audit_month_workflow(Path(result["config"]))
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(outcome["report"]["max_spendable"]["max_spendable"], "190")

    def test_invalid_manual_names_are_located_in_the_worksheet(self):
        self.create()
        original = self.approve()
        for field in ("defaults", "allocation_targets"):
            doc = yaml.safe_load(yaml.safe_dump(original, allow_unicode=True))
            if field == "defaults":
                doc["defaults"]["expense_purpose"] = "-"
            else:
                doc["allocation_targets"]["bad,name"] = "10"
            self.save(doc)
            with self.subTest(field=field), self.assertRaises(BudgetError) as caught:
                compile_setup(self.setup)
            self.assertTrue(caught.exception.source.startswith(str(self.setup) + ":"))


if __name__ == "__main__":
    unittest.main()

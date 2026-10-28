"""User-facing YAML and month-config errors point to source lines."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.yamlstrict import load_yaml
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, audit_reviewed_month
from beancount_budget_tool.v6.review_queue import audit_review_queue
from beancount_budget_tool.v6.targets import load_expense_targets
from beancount_budget_tool.v6.purpose_targets import load_purpose_targets
from fixture_inputs import create_setup, compile_setup
from beancount_budget_tool.v6.classification import _registry
from beancount_budget_tool.v6.ownership import _owners


class ConfigLocationTests(unittest.TestCase):
    def test_duplicate_and_malformed_yaml_have_file_line(self):
        with TemporaryDirectory() as folder:
            config = Path(folder) / "config.yaml"
            config.write_text("schema: 6\nschema: 7\n", encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                load_yaml(config)
            self.assertEqual((caught.exception.code, caught.exception.source),
                             ("YAML_DUPLICATE_KEY", f"{config.resolve()}:2"))
            config.write_text("schema: 6\nmonth: [\n", encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                load_yaml(config)
            self.assertEqual(caught.exception.code, "YAML_INVALID")
            self.assertTrue(caught.exception.source.startswith(f"{config.resolve()}:"))

    def test_invalid_month_and_cutover_identify_the_key(self):
        with TemporaryDirectory() as folder:
            config = Path(folder) / "month.yaml"
            config.write_text("schema: 6\nmonth: bad\ncutover: 2026-10-01\n",
                              encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                audit_month_workflow(config)
            self.assertEqual((caught.exception.code, caught.exception.source),
                             ("PLAN_AMBIGUOUS", f"{config.resolve()}:2"))
            config.write_text(
                "schema: 6\nledger: missing.bean\ncutover: bad\n"
                "accounts: accounts.yaml\nownership: ownership.yaml\n"
                "obligations: obligations.yaml\nclassifications: classifications.yaml\n"
                "purpose_pools: pool.yaml\n", encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 31))
            self.assertEqual((caught.exception.code, caught.exception.source),
                             ("TIME_INVALID", f"{config.resolve()}:3"))

    def test_missing_sidecar_identifies_its_config_line(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "main.bean").write_text('option "operating_currency" "CNY"\n',
                                             encoding="utf-8")
            names = ("accounts", "opening", "ownership", "obligations", "classifications",
                     "expense_plans", "purpose_plans", "h_transfers", "k_events",
                     "h_fx", "purpose_pools")
            for name in names:
                if name != "ownership":
                    (base / f"{name}.yaml").write_text("{}\n", encoding="utf-8")
            config = base / "month.yaml"
            lines = ["schema: 6", "ledger: main.bean", "cutover: '2026-10-01'",
                     "month: '2026-10'", "report_currency: CNY"] + [
                         f"{name}: {name}.yaml" for name in names]
            config.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                audit_reviewed_month(config)
            self.assertEqual((caught.exception.code, caught.exception.source),
                             ("MONTH_REPORT_CONFIG_INVALID",
                              f"{config.resolve()}:{lines.index('ownership: ownership.yaml') + 1}"))

    def test_nested_target_errors_identify_the_amount_or_unknown_id(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "plans.yaml"
            categories = [{"id": "food", "name": "餐饮", "parent": None, "aliases": []}]
            for ident, amount, code in (("food", "-1", "PLAN_AMOUNT_INVALID"),
                                        ("unknown", "'10'", "PLAN_CATEGORY_UNKNOWN")):
                path.write_text("schema: 6\nreport_currency: CNY\nmonths:\n"
                                f"  '2026-10':\n    expense_targets:\n      {ident}: {amount}\n",
                                encoding="utf-8")
                with self.assertRaises(BudgetError) as caught:
                    load_expense_targets(path, "2026-10", "CNY", categories)
                self.assertEqual((caught.exception.code, caught.exception.source),
                                 (code, f"{path.resolve()}:6"))
            path.write_text("schema: 6\nreport_currency: CNY\neffective_templates:\n"
                            "  '2026-10':\n    allocation_targets:\n      savings: -1\n",
                            encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                load_purpose_targets(path, "2026-10", "CNY", {"savings"})
            self.assertEqual((caught.exception.code, caught.exception.source),
                             ("PLAN_AMOUNT_INVALID", f"{path.resolve()}:6"))


class ReviewedSidecarLocationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.ledger = self.base / "main.bean"
        self.ledger.write_text('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Food\n2026-08-31 * "opening"\n'
            '  Assets:Cash 100 CNY\n  Equity:Opening -100 CNY\n'
            '2026-09-02 * "food"\n  Assets:Cash -10 CNY\n  Expenses:Food 10 CNY\n', encoding="utf-8")
        setup = Path(create_setup(self.ledger, "2026-09", self.base / "setup")["setup"])
        document = yaml.safe_load(setup.read_text(encoding="utf-8"))
        document["defaults"]["confirmed"] = document["completeness_confirmed"] = True
        document["expense_targets"] = {"待支出资金": "200"}
        document["allocation_targets"] = {name: "not_assessed" for name in document["allocation_targets"]}
        for account in document["accounts"]:
            account.update(semantics="H", reason="reviewed virtual cash", payment_scope={"CNY": "payable"})
            for opening in account["opening"]:
                opening["owners"] = {"待支出资金": opening["signed_units"]}
        setup.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.config = Path(compile_setup(setup)["config"])

    def fault(self, name, mutate):
        path = self.config.parent / (name + ".yaml")
        original = path.read_bytes()
        document = yaml.safe_load(original)
        mutate(document)
        text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        path.write_text(text, encoding="utf-8")
        try:
            with self.assertRaises(BudgetError) as caught:
                audit_month_workflow(self.config)
            return caught.exception, path, text.splitlines()
        finally:
            path.write_bytes(original)

    def test_unexpected_sidecar_field_identifies_the_actual_yaml_line(self):
        for name, code in (("obligations", "OBLIGATION_DECLARATION_INVALID"),
                           ("ownership", "OWNERSHIP_DECLARATION_INVALID"),
                           ("classifications", "CLASSIFICATION_DECLARATION_INVALID")):
            with self.subTest(name=name):
                error, path, lines = self.fault(name, lambda document: document.update(unexpected=True))
                self.assertEqual(error.code, code)
                self.assertEqual(error.source, f"{path}:{lines.index('unexpected: true') + 1}")

    def test_wrong_responsibility_metadata_identifies_its_own_field(self):
        for field, value in (("schema", 7), ("cutover", "2026-10-01"), ("ledger_snapshot", "bad"),
                             ("loader_version", "wrong"), ("events", {})):
            with self.subTest(field=field):
                error, path, lines = self.fault("obligations", lambda doc: doc.update({field: value}))
                line = next(index + 1 for index, text in enumerate(lines) if text.startswith(field + ":"))
                self.assertEqual(error.code, "OBLIGATION_DECLARATION_INVALID")
                self.assertEqual(error.source, f"{path}:{line}")

    def test_duplicate_category_or_owner_id_points_at_the_second_id(self):
        for name, field, code in (("classifications", "categories", "CATEGORY_ID_INVALID"),
                                  ("ownership", "owners", "OWNER_ID_INVALID"),
                                  ("obligations", "owners", "OWNER_ID_INVALID")):
            with self.subTest(name=name):
                def duplicate(doc):
                    doc[field].append(dict(doc[field][0]))
                error, path, lines = self.fault(name, duplicate)
                id_line = next(text for text in lines if text.startswith("- id:"))
                location = max(index + 1 for index, text in enumerate(lines) if text == id_line)
                self.assertEqual(error.code, code)
                self.assertEqual(error.source, f"{path}:{location}")

    def test_invalid_category_alias_and_parent_have_nested_positions(self):
        for field, value, code in (("aliases", ["illegal|alias"], "CATEGORY_NAME_INVALID"),
                                   ("parent", "missing", "CATEGORY_PARENT_INVALID")):
            with self.subTest(field=field):
                error, path, lines = self.fault("classifications", lambda doc: doc["categories"][0].update({field: value}))
                target = "  - illegal|alias" if field == "aliases" else "  parent: missing"
                line = lines.index(target) + 1
                self.assertEqual(error.code, code)
                self.assertEqual(error.source, f"{path}:{line}")

    def test_successful_registry_checks_do_not_reparse_yaml_to_locate_errors(self):
        with patch("beancount_budget_tool.yamlstrict.yaml_path_location", side_effect=AssertionError("eager location lookup")):
            self.assertEqual(_registry([{"id": "food", "name": "Food", "parent": None, "aliases": []}],
                                       source=self.base / "categories.yaml"), {"Food": "food"})
            self.assertEqual(_owners([{"id": "free", "name": "Free", "aliases": []}],
                                     source=self.base / "owners.yaml"), {"Free": "free"})


if __name__ == "__main__":
    unittest.main()

"""User-facing YAML and month-config errors point to source lines."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.yamlstrict import load_yaml
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, audit_reviewed_month
from beancount_budget_tool.v6.review_queue import audit_review_queue


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


if __name__ == "__main__":
    unittest.main()

"""One monthly command should stop at the first honest review boundary."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_review_migration_month import review_month, render_text


class ReviewMigrationMonthTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.before = base / "before" / "main.bean"
        self.after = base / "after" / "main.bean"
        self.before.parent.mkdir()
        self.after.parent.mkdir()
        self.ledger = (
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Income:Salary\n'
            '2026-08-02 * "old salary"\n'
            '  budget: "旧交易级标签"\n'
            '  Assets:Cash 50 CNY\n'
            '  Income:Salary -50 CNY\n'
            '2026-10-02 * "new salary"\n'
            '  Assets:Cash 100 CNY\n'
            '  Income:Salary -100 CNY\n')
        self.before.write_text(self.ledger, encoding="utf-8")
        self.after.write_text(self.ledger, encoding="utf-8")
        self.config = self.after.parent / "month.yaml"
        self.save_config()

    def save_config(self, *, month="2026-10", cutover="2026-09-01",
                    ledger="main.bean"):
        self.config.write_text(yaml.safe_dump({
            "schema": 6, "ledger": ledger, "month": month, "cutover": cutover},
            allow_unicode=True), encoding="utf-8")

    def test_pre_cutover_checks_structure_without_requiring_v6_tags(self):
        result = review_month(self.before, self.after, "2026-08", date(2026, 9, 1))
        self.assertEqual((result["status"], result["stage"]),
                         ("PRE_CUTOVER_CHECKED", "pre_cutover"))
        self.assertEqual(result["structural_check"]["month_source_audit"]["tag_syntax"],
                         "NOT_AUDITED")
        self.assertIn("早于 cutover", render_text(result))

    def test_cutover_collects_all_legacy_transaction_tags_with_locations(self):
        ledger = self.ledger.replace(
            '2026-10-02 * "new salary"\n',
            '2026-10-02 * "new salary"\n  budget: "old category"\n') + (
            '2026-10-03 * "bonus"\n  fund: "old owner"\n'
            '  Assets:Cash 1 CNY\n  Income:Salary -1 CNY\n')
        self.before.write_text(ledger, encoding="utf-8")
        self.after.write_text(ledger, encoding="utf-8")
        result = review_month(self.before, self.after, "2026-10", date(2026, 10, 1))
        self.assertEqual((result["status"], result["stage"]),
                         ("NEEDS_REVIEW", "source_preflight"))
        self.assertEqual(len(result["review"]["items"]), 2)
        self.assertTrue(all(row["reason_code"] == "TAG_LEVEL_FORBIDDEN"
                            for row in result["review"]["items"]))
        for row, line in zip(result["review"]["items"], (8, 12), strict=True):
            source_path, source_line = row["source"].rsplit(":", 1)
            self.assertTrue(Path(source_path).samefile(self.after))
            self.assertEqual(source_line, str(line))

    def test_post_cutover_needs_matching_config_then_runs_existing_workflow(self):
        missing = review_month(self.before, self.after, "2026-10", date(2026, 9, 1))
        self.assertEqual((missing["status"], missing["stage"]),
                         ("REVIEW_CONFIG_REQUIRED", "config"))
        outcome = {"status": "NEEDS_REVIEW", "stage": "metrics", "month": "2026-10",
                   "review_queue": {"items": []}, "report": None,
                   "pending_metrics": [{"metric": "实际可花", "status": "NEEDS_REVIEW",
                                        "reason_codes": ["EXTERNAL_SCOPE_PENDING"],
                                        "source": "completeness.yaml"}]}
        with patch("beancount_budget_tool.tools.v6_review_migration_month.audit_month_workflow",
                   return_value=outcome) as audited:
            result = review_month(self.before, self.after, "2026-10",
                                  date(2026, 9, 1), self.config)
        audited.assert_called_once_with(self.config.resolve())
        self.assertEqual((result["status"], result["stage"]),
                         ("NEEDS_REVIEW", "metrics"))
        self.assertIn("EXTERNAL_SCOPE_PENDING", render_text(result))

    def test_config_must_reference_this_month_and_after_ledger(self):
        for fields, code in (({"month": "2026-09"}, "MIGRATION_MONTH_CONFIG_MISMATCH"),
                             ({"cutover": "2026-10-01"}, "MIGRATION_CUTOVER_CONFIG_MISMATCH"),
                             ({"ledger": "../before/main.bean"},
                              "MIGRATION_LEDGER_CONFIG_MISMATCH")):
            self.save_config(**fields)
            result = review_month(self.before, self.after, "2026-10",
                                  date(2026, 9, 1), self.config)
            self.assertEqual((result["status"], result["error"]["code"]),
                             ("REVIEW_INPUT_BLOCKED", code))

    def test_review_input_error_is_reported_after_structural_success(self):
        with patch("beancount_budget_tool.tools.v6_review_migration_month.audit_month_workflow",
                   side_effect=BudgetError("CLAIM_REF_INVALID", "旧引用已失效", "claims.yaml:12")):
            result = review_month(self.before, self.after, "2026-10",
                                  date(2026, 9, 1), self.config)
        self.assertEqual(result["stage"], "review_input")
        self.assertTrue(result["structural_check"]["financial_facts_equal"])
        self.assertIn("claims.yaml:12", render_text(result))


if __name__ == "__main__":
    unittest.main()

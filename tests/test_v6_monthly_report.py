"""The readable report must preserve certified/pending boundaries."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.facts import frozen_fact_indices

from beancount_budget_tool.v6.monthly_report import (
    audit_month_workflow, render_text, render_workflow_text,
)


class MonthlyReportTextTests(unittest.TestCase):
    def test_exact_decimal_and_pending_metrics(self):
        report = {
            "month": "2026-09", "as_of": "2026-09-30", "report_currency": "CNY",
            "actual_spendable": {"status": "NEEDS_REVIEW", "actual_spendable": None,
                                 "reason_codes": ["EXTERNAL_SCOPE_PENDING"], "note": "仅限日末"},
            "expense_targets": {"categories": [
                {"name": "生活费", "subtree_target_kind": "explicit", "direct_target": None,
                 "subtree_remaining_status": "CERTIFIED", "subtree_target": "2000",
                 "subtree_spent": "116979/50", "subtree_remaining": "-16979/50"}]},
            "purpose_progress": {"owners": [
                {"owner_id": "savings", "status": "CERTIFIED", "target_q": "700",
                 "actual_g": "34247/50", "remaining_q_minus_g": "753/50"},
                {"owner_id": "emergency", "status": "TARGET_MISSING", "actual_g": "0"}]},
            "purpose_pools": {"pools": [
                {"id": "broker", "protected_label": "额外储蓄",
                 "protected_nominal_principal": "3000", "remainder_label": "待投资金",
                 "remainder_book_cost": "3669353/40", "account_purposes": [
                     {"account": "Assets:Broker:Cash", "protected_nominal": "110.88",
                      "remainder_book_cost": "0"}]}]},
            "k_book_purposes": {"rows": [
                {"account": "Assets:Fund:CCB", "commodity": "CNY",
                 "booked_opening": "40479.58", "booked_change": "2000",
                 "booked_end": "42479.58", "purpose_changes": [
                     {"owner_id": "insurance", "signed_change": "200"},
                     {"owner_id": "travel", "signed_change": "900"}]}]},
            "account_purposes": {"rows": [
                {"account": "Assets:Bank", "commodity": "CNY",
                 "owner_id": "living", "units": "-100"}]},
            "owner_names": {"living": "生活费", "insurance": "保险", "travel": "旅游"},
            "note": "历史快照"
        }
        rendered = render_text(report)
        self.assertIn("实际可花：待审（EXTERNAL_SCOPE_PENDING）", rendered)
        self.assertIn("实际 2339.58，差额 -339.58 CNY", rendered)
        self.assertIn("实际 684.94，差额 15.06 CNY", rendered)
        self.assertIn("待投资金 91733.825 CNY", rendered)
        self.assertIn("emergency：实际 0，无目标", rendered)
        self.assertIn("Assets:Bank：生活费 -100 CNY", rendered)
        self.assertIn("K 账面申赎及用途变动（非真实持仓；不可与 H 余额相加）", rendered)
        self.assertIn("Assets:Fund:CCB：账面期初 40479.58，本期变动 2000，期末 42479.58 CNY", rendered)
        self.assertIn("cutover 后已审 保险 申赎净变动 200 CNY", rendered)
        self.assertIn("cutover 后已审 旅游 申赎净变动 900 CNY", rendered)
        self.assertNotIn("实际可花：None", rendered)

    def test_workflow_keeps_pending_metric_out_of_signed_text(self):
        with TemporaryDirectory() as folder:
            config = Path(folder) / "month.yaml"
            config.write_text("month: '2026-10'\ncompleteness: completeness.yaml\n",
                              encoding="utf-8")
            review = {"items": [], "ledger_snapshot": "snapshot"}
            report = {"ledger_snapshot": "snapshot",
                      "actual_spendable": {"status": "NEEDS_REVIEW",
                                           "reason_codes": ["EXTERNAL_SCOPE_PENDING"]},
                      "expense_targets": {"status": "CERTIFIED"},
                      "purpose_progress": {"status": "CERTIFIED"},
                      "account_purposes": {"status": "CERTIFIED_H_OUTSIDE_POOLS"}}
            with patch("beancount_budget_tool.v6.monthly_report.audit_review_queue",
                       return_value=review), patch(
                       "beancount_budget_tool.v6.monthly_report.audit_reviewed_month",
                       return_value=report):
                outcome = audit_month_workflow(config)
            self.assertEqual((outcome["status"], outcome["stage"]),
                             ("NEEDS_REVIEW", "metrics"))
            self.assertIsNone(outcome["report"])
            self.assertEqual(outcome["pending_metrics"][0]["reason_codes"],
                             ["EXTERNAL_SCOPE_PENDING"])
            self.assertIn("EXTERNAL_SCOPE_PENDING", render_workflow_text(outcome))
            self.assertNotIn("v6 已审月报", render_workflow_text(outcome))
            report["actual_spendable"]["status"] = "CERTIFIED"
            with patch("beancount_budget_tool.v6.monthly_report.audit_review_queue",
                       return_value=review), patch(
                       "beancount_budget_tool.v6.monthly_report.audit_reviewed_month",
                       return_value=report):
                ready = audit_month_workflow(config)
            self.assertEqual((ready["status"], ready["stage"]),
                             ("CERTIFIED_SCOPED", "report"))
            self.assertIs(ready["report"], report)

    def test_budget_error_survives_generator_context_exit(self):
        with self.assertRaises(BudgetError) as caught:
            with frozen_fact_indices():
                raise BudgetError("EXAMPLE", "diagnostic", "example.bean:2")
        self.assertEqual(caught.exception.source, "example.bean:2")


if __name__ == "__main__":
    unittest.main()

"""Q−G exists only when target and actual are independently certified."""

import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.purpose_progress import compare_purpose_progress


def targets():
    return {"month": "2026-09", "report_currency": "CNY", "plan_sha256": "plan",
            "missing_target_ids": ["emergency"], "owners": [
                {"owner_id": "savings", "status": "TARGET", "target": "700"},
                {"owner_id": "investment", "status": "TARGET", "target": "2000"},
                {"owner_id": "travel", "status": "NOT_ASSESSED", "target": None},
                {"owner_id": "emergency", "status": "MISSING", "target": None}]}


def flows(actual=True):
    return {"month": "2026-09", "report_currency": "CNY", "ledger_snapshot": "source",
            "status": "CERTIFIED" if actual else "NEEDS_REVIEW",
            "net_allocations": {"savings": "34247/50", "investment": "3200",
                                "travel": "0", "emergency": "0"} if actual else None,
            "pending_events": [] if actual else [{"reason": "H_OWNER_PENDING"}]}


class PurposeProgressTests(unittest.TestCase):
    def test_signed_overshoot_missing_and_not_assessed_are_distinct(self):
        report = compare_purpose_progress(targets(), flows())
        rows = {row["owner_id"]: row for row in report["owners"]}
        self.assertEqual((rows["savings"]["status"], rows["savings"]["remaining_q_minus_g"]),
                         ("CERTIFIED", "753/50"))
        self.assertEqual(rows["investment"]["remaining_q_minus_g"], "-1200")
        self.assertEqual(rows["travel"]["status"], "NOT_ASSESSED")
        self.assertEqual(rows["emergency"]["status"], "TARGET_MISSING")
        self.assertEqual(report["status"], "NEEDS_REVIEW")

    def test_pending_actual_never_produces_remaining(self):
        report = compare_purpose_progress(targets(), flows(False))
        rows = {row["owner_id"]: row for row in report["owners"]}
        self.assertEqual(rows["savings"]["status"], "ACTUAL_PENDING")
        self.assertIsNone(rows["savings"]["remaining_q_minus_g"])
        self.assertEqual(report["pending_actual_events"], [{"reason": "H_OWNER_PENDING"}])

    def test_all_not_assessed_does_not_hide_pending_actual_flows(self):
        values = {**targets(), "missing_target_ids": [], "owners": [
            {"owner_id": owner, "status": "NOT_ASSESSED", "target": None}
            for owner in ("savings", "investment", "travel", "emergency")]}
        report = compare_purpose_progress(values, flows(False))
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertEqual(report["actual_status"], "NEEDS_REVIEW")

    def test_mismatched_period_or_owner_registry_rejected(self):
        bad = flows()
        bad["month"] = "2026-10"
        with self.assertRaises(BudgetError) as caught:
            compare_purpose_progress(targets(), bad)
        self.assertEqual(caught.exception.code, "PURPOSE_PROGRESS_MISMATCH")
        bad = flows()
        bad["net_allocations"].pop("travel")
        with self.assertRaises(BudgetError) as caught:
            compare_purpose_progress(targets(), bad)
        self.assertEqual(caught.exception.code, "PURPOSE_PROGRESS_MISMATCH")


if __name__ == "__main__":
    unittest.main()

"""Historical months can certify actuals without inventing forgotten targets."""

from datetime import date
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

import test_v6_native_flows as native_tests
from fixture_inputs import compile_setup, SetupDumper as _SetupDumper
from fixture_months import create_next_month, compile_next_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.period_report import audit_period, csv_rows, render_text as period_text
from beancount_budget_tool.v6.salary_cycle import audit_salary_cycle
from beancount_budget_tool.v6.targets import audit_monthly_expense_targets, load_expense_targets
from beancount_budget_tool.v6.tui import ReportBrowser
from beancount_budget_tool.yamlstrict import load_yaml


SOURCE = ('2026-09-02 * "food"\n  Expenses:Food 10 CNY\n'
          '    budget: "生活开销.餐饮"\n  Assets:Cash -10 CNY\n'
          '2026-10-03 * "next month"\n  Expenses:Food 20 CNY\n  Assets:Cash -20 CNY\n')


class UnassessedExpenseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def save(self, path, document):
        path.write_text(yaml.dump(document, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def configure(self, target="not_assessed", transactions=SOURCE):
        native_tests.NativeFlowTests.configure(self, transactions)
        self.setup = self.base / "initial/setup.yaml"
        doc = load_yaml(self.setup)
        doc["expense_targets"] = {"生活开销": target}
        self.save(self.setup, doc)
        self.created = compile_setup(self.setup, self.base / "historical")
        self.config = Path(self.created["config"]) if self.created["config"] else None
        if self.config:
            self.doc = load_yaml(self.config)
            self.classes = self.config.parent / self.doc["classifications"]
            self.plans = self.config.parent / self.doc["expense_plans"]
            self.categories = load_yaml(self.classes)["categories"]
            self.ids = {row["name"]: row["id"] for row in self.categories}
            self.root_id, self.child_id = self.ids["生活开销"], self.ids.get("生活开销.餐饮")
        return self.created

    def plan(self, direct=None, subtree=None):
        document = load_yaml(self.plans)
        document["months"]["2026-09"] = {"expense_targets": direct or {},
                                           "expense_subtree_targets": subtree or {}}
        self.save(self.plans, document)

    def audit_targets(self):
        return audit_monthly_expense_targets(self.ledger, date(2026, 9, 1), "2026-09",
                                             self.classes, self.plans, "CNY")

    def next_month(self, target="not_assessed", name="next"):
        directory = self.base / name
        create_next_month(self.config, directory)
        worksheet = directory / "next-month.yaml"
        doc = load_yaml(worksheet)
        self.assertEqual(doc["expense_subtree_targets"]["生活开销"],
                         load_expense_targets(self.plans, "2026-09", "CNY", self.categories)["subtree_targets"][self.root_id])
        doc["expense_subtree_targets"]["生活开销"] = target
        doc.update(targets_confirmed=True, completeness_confirmed=True,
                   completeness_reason="完整虚拟账本的明确截止日决定")
        self.save(worksheet, doc)
        return compile_next_month(worksheet)

    def test_setup_compiles_not_assessed_and_complete_month_keeps_actual_cash(self):
        result = self.configure()
        self.assertEqual(result["status"], "CONFIG_CREATED")
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        report = outcome["report"]
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "140")
        self.assertEqual(report["max_spendable"]["status"], "NOT_ASSESSED")
        self.assertIsNone(report["max_spendable"]["max_spendable"])
        root = next(row for row in report["expense_targets"]["categories"] if row["id"] == self.root_id)
        self.assertEqual((root["subtree_spent"], root["subtree_remaining_status"], root["subtree_target_kind"]),
                         ("10", "NOT_ASSESSED", "not_assessed"))
        self.assertIsNone(root["subtree_target"])
        self.assertIsNone(root["subtree_remaining"])
        text = render_workflow_text(outcome)
        self.assertIn("本月消费上限不考核", text)
        self.assertIn(["生活开销（含子类）", "-", "10", "-", "不考核"],
                      [line.split() for line in text.splitlines()])
        self.assertNotIn("待审", text)
        self.assertNotIn("目标 0", text)

    def test_null_still_needs_review_instead_of_becoming_zero_or_unassessed(self):
        result = self.configure(None)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["config"])
        issue = next(row for row in result["review_items"] if "not_assessed" in row["detail"])
        self.assertTrue(issue["source"].startswith(str(self.setup) + ":"))

    def test_zero_is_a_real_target_and_negative_difference_is_preserved(self):
        self.configure("0")
        report = audit_month_workflow(self.config)["report"]
        self.assertEqual(report["max_spendable"]["status"], "CERTIFIED")
        self.assertEqual(report["max_spendable"]["max_spendable"], "-10")

    def test_parent_unassessed_covers_children_but_does_not_erase_their_actuals(self):
        self.configure()
        report = self.audit_targets()
        self.assertEqual((report["status"], report["missing_target_ids"]), ("CERTIFIED", []))
        child = next(row for row in report["categories"] if row["id"] == self.child_id)
        self.assertEqual((child["direct_spent"], child["direct_remaining_status"]), ("10", "NOT_ASSESSED"))
        self.assertIsNone(child["direct_remaining"])

    def test_child_numeric_limit_can_be_assessed_under_unassessed_parent(self):
        self.configure()
        self.plan({self.child_id: "15"}, {self.root_id: "not_assessed"})
        report = self.audit_targets()
        self.assertEqual(report["status"], "CERTIFIED")
        rows = {row["id"]: row for row in report["categories"]}
        self.assertEqual(rows[self.child_id]["direct_remaining"], "5")
        self.assertEqual(rows[self.root_id]["subtree_remaining_status"], "NOT_ASSESSED")

    def test_explicit_numeric_parent_still_caps_child_marked_not_assessed(self):
        self.configure()
        self.plan({self.child_id: "not_assessed"}, {self.root_id: "50"})
        outcome = audit_month_workflow(self.config)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(outcome["report"]["max_spendable"]["max_spendable"], "40")
        child = next(row for row in outcome["report"]["expense_targets"]["categories"] if row["id"] == self.child_id)
        self.assertEqual(child["direct_remaining_status"], "NOT_ASSESSED")

    def test_direct_unassessed_does_not_silently_cover_unplanned_child(self):
        self.configure()
        self.plan({self.root_id: "not_assessed"})
        report = self.audit_targets()
        self.assertEqual((report["status"], report["missing_target_ids"]), ("NEEDS_REVIEW", [self.child_id]))

    def test_partial_numeric_child_goals_do_not_become_a_complete_parent_limit(self):
        self.configure()
        self.plan({self.root_id: "100", self.child_id: "not_assessed"})
        report = self.audit_targets()
        root = next(row for row in report["categories"] if row["id"] == self.root_id)
        self.assertEqual(root["direct_remaining"], "100")
        self.assertIsNone(root["subtree_target"])
        self.assertIsNone(root["subtree_remaining"])
        self.assertEqual(root["subtree_remaining_status"], "NOT_ASSESSED")

    def test_numeric_default_is_explicitly_replaced_and_later_reenabled(self):
        self.configure()
        self.save(self.plans, {"schema": 6, "report_currency": "CNY",
            "defaults": {"expense_subtree_targets": {self.root_id: "100"}},
            "effective_templates": {"2026-10": {"expense_subtree_targets": {self.root_id: "not_assessed"}}},
            "months": {"2026-11": {"expense_subtree_targets": {self.root_id: "50"}}}})
        values = [load_expense_targets(self.plans, month, "CNY", self.categories)["subtree_targets"][self.root_id]
                  for month in ("2026-09", "2026-10", "2026-11")]
        self.assertEqual(values, ["100", "not_assessed", "50"])

    def test_next_month_proposes_unassessed_and_carries_balances_without_reset(self):
        self.configure()
        result = self.next_month()
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED", result["workflow"]["review_items"])
        report = result["workflow"]["report"]
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "120")
        self.assertEqual(report["max_spendable"]["status"], "NOT_ASSESSED")
        self.assertEqual(audit_month_workflow(self.config)["report"]["actual_spendable"]["actual_spendable"], "140")

    def test_next_month_can_switch_back_to_numeric_limit(self):
        self.configure()
        report = self.next_month("40")["workflow"]["report"]
        self.assertEqual(report["max_spendable"]["max_spendable"], "20")

    def test_next_month_can_switch_numeric_limit_to_unassessed(self):
        self.configure("100")
        result = self.next_month()
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertIsNone(result["workflow"]["report"]["max_spendable"]["max_spendable"])

    def test_next_month_null_does_not_approve_a_missing_goal(self):
        self.configure()
        result = self.next_month(None)
        self.assertIsNone(result["config"])
        self.assertTrue(any("not_assessed" in row["detail"] for row in result["review_items"]))

    def test_missing_classification_is_not_waived_by_unassessed_goal(self):
        self.configure(transactions='2026-09-02 * "unknown"\n  Expenses:Food 10 CNY\n  Assets:Cash -10 CNY\n')
        doc = load_yaml(self.classes)
        doc.pop("defaults")
        self.save(self.classes, doc)
        result = audit_month_workflow(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["report"])

    def test_unpriced_actual_expense_still_requires_evidence(self):
        self.configure(transactions='2026-09-02 * "foreign"\n  Expenses:Food 10 USD\n  Liabilities:Card -10 USD\n')
        report = self.audit_targets()
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertIn("VALUATION_MISSING", report["reason_codes"])
        root = next(row for row in report["categories"] if row["id"] == self.root_id)
        self.assertIsNone(root["subtree_spent"])

    def test_period_and_csv_keep_actual_totals_and_explicit_no_comparison_status(self):
        self.configure()
        following = Path(self.next_month()["config"])
        result = audit_period([self.config, following])
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["expense_totals"]["direct_spent"], "30")
        self.assertIn("不考核", period_text(result))
        goals = [row for row in csv_rows(result) if row["section"] == "expenses" and row["metric"] == "subtree_target"
                 and row["name"].startswith("生活开销")]
        self.assertTrue(goals)
        self.assertTrue(all(row["status"] == "NOT_ASSESSED" and row["value"] == "" for row in goals))

    def test_salary_cycle_explicit_budget_is_independent_of_unassessed_months(self):
        self.configure()
        following = Path(self.next_month()["config"])
        result = audit_salary_cycle(following, "2026-09-02", "2026-10-04", "50")
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["cycle_budget"]["remaining"], "20")

    def test_tui_displays_certified_actuals_and_no_invented_target_difference(self):
        self.configure()
        browser = ReportBrowser([self.config], audit_month_workflow, render_workflow_text)
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertTrue(any("不考核" in line for line in browser.lines))
        self.assertEqual(browser.reviews(), [])


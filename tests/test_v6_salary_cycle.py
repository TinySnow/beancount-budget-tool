"""Explicit paydays partition expenses without resetting ownership or month goals."""

from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from fractions import Fraction
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

import test_v6_native_flows as native_tests
import test_v6_settlement_budget as settlement_tests
from beancount.core import data
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.classification import _key
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.period_report import main as report_main
from beancount_budget_tool.v6.project import create_project, load_project
from beancount_budget_tool.v6.salary_cycle import audit_salary_cycle, cycle_bounds, render_text, render_csv
from beancount_budget_tool.v6.tui import ReportBrowser, _select_salary_cycle, _report_browser
from beancount_budget_tool.v6.tui_export import write_export
from test_v6_tui import FakeScreen


TRANSACTIONS = ('2026-09-07 * "previous cycle"\n  Expenses:Food 9 CNY\n  Assets:Cash -9 CNY\n'
    '2026-09-08 * "payday food"\n  Expenses:Food 10 CNY\n    budget: "生活开销.餐饮"\n  Assets:Cash -10 CNY\n'
    '2026-09-30 * "month end food"\n  Expenses:Food 20 CNY\n  Assets:Cash -20 CNY\n'
    '2026-10-01 * "following month"\n  Expenses:Travel 3 CNY\n  Assets:Cash -3 CNY\n'
    '2026-10-02 * "next payday expense"\n  Expenses:Food 10 CNY\n  Assets:Cash -10 CNY\n')


class SalaryCycleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def configure(self, transactions=TRANSACTIONS):
        native_tests.NativeFlowTests.configure(self, transactions)
        self.september = self.config
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document["month"] = "2026-10"
        self.config = self.config.parent / "october.yaml"
        self.save(self.config, document)
        self.paths = {key: (self.config.parent / value).resolve() for key, value in document.items()
                      if key in ("classifications", "expense_plans", "ownership", "accounts", "obligations", "h_fx")}
        return self.config

    def save(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def audit(self, budget="50", **kwargs):
        return audit_salary_cycle(self.config, "2026-09-08", "2026-10-02", budget, **kwargs)

    def test_next_payday_is_excluded_and_subtree_is_not_double_counted(self):
        self.configure()
        result = self.audit()
        self.assertEqual((result["status"], result["through"]), ("CERTIFIED_SCOPED", "2026-10-01"))
        report = result["report"]
        self.assertEqual(report["cycle_budget"]["net_spent"], "33")
        self.assertEqual(report["cycle_budget"]["remaining"], "17")
        self.assertEqual(report["total_net_expense"], "33")
        self.assertEqual(len(report["events"]), 3)
        self.assertEqual(sum(Fraction(row["signed_units"]) for row in report["native_totals"]), 33)
        self.assertNotIn("actual_spendable", report)
        self.assertNotIn("max_spendable", report)

    def test_adjacent_cycles_partition_all_transactions_on_payday(self):
        self.configure(TRANSACTIONS + '2026-10-02 * "another same day expense"\n'
            '  Expenses:Food 2 CNY\n  Assets:Cash -2 CNY\n')
        first = self.audit()
        second = audit_salary_cycle(self.config, "2026-10-02", "2026-10-09", "50")
        self.assertEqual(second["report"]["cycle_budget"]["net_spent"], "12")
        self.assertTrue(set(json.dumps(row["ref"], sort_keys=True) for row in first["report"]["events"]).isdisjoint(
            json.dumps(row["ref"], sort_keys=True) for row in second["report"]["events"]))

    def test_month_budget_is_not_copied_prorated_or_added(self):
        self.configure()
        no_budget = self.audit(None)
        self.assertIsNone(no_budget["report"]["cycle_budget"]["target"])
        self.assertIsNone(no_budget["report"]["cycle_budget"]["remaining"])
        self.assertEqual(self.audit("0")["report"]["cycle_budget"]["remaining"], "-33")
        self.assertIn("警告", render_text(self.audit("0")))
        original = self.september.read_bytes()
        before = audit_month_workflow(self.september)
        self.audit("25.50")
        self.assertEqual(audit_month_workflow(self.september), before)
        self.assertEqual(self.september.read_bytes(), original)

    def test_exact_decimal_and_explicit_category_name(self):
        self.configure()
        report = self.audit("12.34", category="生活开销.餐饮")["report"]
        self.assertEqual(report["cycle_budget"]["net_spent"], "10")
        self.assertEqual(Fraction(report["cycle_budget"]["remaining"]), Fraction("2.34"))

    def test_credit_card_purchase_counts_now_repayment_does_not_count_again(self):
        self.configure('2026-09-09 * "card food"\n  Expenses:Food 30 CNY\n  Liabilities:Card -30 CNY\n'
            '2026-10-01 * "payment"\n  Assets:Cash -30 CNY\n  Liabilities:Card 30 CNY\n')
        result = self.audit()
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["cycle_budget"]["net_spent"], "30")
        self.assertEqual(len(result["report"]["events"]), 1)

    def test_refund_of_a_previous_cycle_is_a_receipt_in_this_cycle(self):
        self.configure('2026-09-04 * "old purchase"\n  Expenses:Food 20 CNY\n  Assets:Cash -20 CNY\n'
            '2026-09-10 * "refund"\n  Expenses:Food -20 CNY\n  Assets:Cash 20 CNY\n')
        cycle = self.audit()["report"]["cycle_budget"]
        self.assertEqual((cycle["gross_spent"], cycle["refunded"], cycle["remaining"]), ("0", "20", "70"))

    def test_income_and_asset_allocations_never_become_consumer_spending(self):
        self.configure('2026-09-08 * "salary"\n  Assets:Cash 500 CNY\n  Income:Salary -500 CNY\n'
            '2026-09-08 * "allocation"\n  Assets:Cash -200 CNY\n    fund: "自由资金"\n'
            '  Assets:Cash 200 CNY\n    fund: "生活费"\n')
        self.assertEqual(self.audit()["report"]["cycle_budget"]["net_spent"], "0")

    def test_missing_classification_inside_cycle_returns_no_partial_numbers(self):
        self.configure()
        doc = yaml.safe_load(self.paths["classifications"].read_text(encoding="utf-8"))
        doc.pop("defaults")
        self.save(self.paths["classifications"], doc)
        result = self.audit()
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["report"])
        self.assertTrue(all(row["source"].startswith(str(self.ledger)) for row in result["review_items"]))

    def test_unclassified_and_unpriced_expenses_outside_cycle_do_not_add_or_block(self):
        self.configure(TRANSACTIONS.replace('Expenses:Food 9 CNY\n  Assets:Cash -9 CNY',
            'Expenses:Food 9 USD\n  Liabilities:Card -9 USD'))
        doc = yaml.safe_load(self.paths["classifications"].read_text(encoding="utf-8"))
        # Only explicitly classified payday food lies inside this smaller query.
        doc.pop("defaults")
        self.save(self.paths["classifications"], doc)
        result = audit_salary_cycle(self.september, "2026-09-08", "2026-09-09", "50")
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["cycle_budget"]["net_spent"], "10")

    def test_unpriced_expense_inside_cycle_is_pending_and_retains_original_currency(self):
        self.configure('2026-09-09 * "foreign card"\n  Expenses:Food 10 USD\n  Liabilities:Card -10 USD\n')
        result = self.audit()
        self.assertIsNone(result["report"])
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertEqual(result["review_items"][0]["commodity"], "USD")

    def test_pre_cutover_reversed_dates_invalid_amount_and_unknown_category_rejected(self):
        self.configure()
        for start, end, budget in (("2026-09-08", "2026-09-08", "50"),
                                  ("2026-09-10", "2026-09-08", "50"),
                                  ("2026-02-30", "2026-03-01", "50"),
                                  ("20260908", "2026-10-02", "50"),
                                  ("2026-09-08", "2026-10-02", "-1"),
                                  ("2026-09-08", "2026-10-02", "1/3")):
            with self.subTest(start=start, end=end, budget=budget), self.assertRaises(BudgetError):
                cycle_bounds(start, end, budget)
        with self.assertRaises(BudgetError) as caught:
            audit_salary_cycle(self.config, "2026-08-31", "2026-10-02")
        self.assertEqual(caught.exception.code, "SALARY_CYCLE_BEFORE_CUTOVER")
        with self.assertRaises(BudgetError):
            self.audit(category="unknown")

    def test_next_payday_on_month_first_needs_only_prior_month_config(self):
        self.configure()
        result = audit_salary_cycle(self.september, "2026-09-08", "2026-10-01", "50")
        self.assertEqual(result["report"]["cycle_budget"]["net_spent"], "30")

    def test_same_payday_boundary_future_bad_tag_does_not_block_old_cycle(self):
        self.configure(TRANSACTIONS + '2026-10-02 * "legacy next cycle"\n  budget: "legacy"\n'
            '  Expenses:Food 1 CNY\n  Assets:Cash -1 CNY\n')
        self.assertEqual(self.audit()["status"], "CERTIFIED_SCOPED")

    def test_native_totals_keep_original_commodity_and_exact_slice_amount(self):
        self.configure('2026-09-09 * "priced foreign food"\n'
            '  Expenses:Food 10 USD @@ 70.50 CNY\n  Liabilities:Card -10 USD @@ 70.50 CNY\n')
        report = self.audit("100")["report"]
        self.assertEqual(report["native_totals"][0]["commodity"], "USD")
        self.assertEqual(report["native_totals"][0]["signed_units"], "10")
        self.assertEqual(Fraction(report["cycle_budget"]["net_spent"]), Fraction("70.50"))

    def test_cross_month_foreign_repayment_uses_actual_cost_without_rewriting_consumption(self):
        self.configure()
        fixture = settlement_tests.SettlementBudgetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ledger.write_text(fixture.ledger.read_text(encoding="utf-8").replace(
            "2026-09-28", "2026-10-01"), encoding="utf-8")
        fin = load_financial(fixture.ledger)
        facts = FactIndex(fin)
        new_refs = {(entry.narration, p.account, p.units.currency): facts.for_posting(p).as_dict()
                    for entry in fin.entries if isinstance(entry, data.Transaction) for p in entry.postings}
        replacements = {_key(ref): new_refs[key] for key, ref in fixture.refs.items()}
        def update_refs(value):
            if type(value) is dict:
                if _key(value) in replacements:
                    return replacements[_key(value)]
                return {key: update_refs(part) for key, part in value.items()}
            return [update_refs(part) for part in value] if type(value) is list else value
        overrides = {"ledger": fixture.ledger, "accounts": fixture.accounts, "ownership": fixture.ownership,
                     "obligations": fixture.obligations, "h_fx": fixture.fx,
                     "classifications": fixture.classes, "expense_plans": fixture.plans}
        for field, path in overrides.items():
            if field != "ledger":
                self.save(path, update_refs(yaml.safe_load(path.read_text(encoding="utf-8"))))
        document = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        document.update({key: str(path) for key, path in overrides.items()})
        self.save(self.config, document)
        result = self.audit("200", category="living")
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        report = result["report"]
        self.assertEqual(Fraction(report["cycle_budget"]["net_spent"]), Fraction("146.41"))
        self.assertEqual(Fraction(report["events"][0]["signed_units"]), Fraction("21.76"))
        self.assertEqual(report["events"][0]["commodity"], "USD")
        self.assertIn("实际清偿成本 146.41 CNY", render_text(result))
        csv = render_csv(result)
        self.assertIn("USD", csv)
        self.assertIn("2026-10-01 实际清偿成本", csv)
        self.assertIn("actual_settlement_budget_comparison", csv)
        # The next payday's settlement must never leak into the previous cycle.
        earlier = self.september
        document["month"] = "2026-09"
        self.save(earlier, document)
        pending = audit_salary_cycle(earlier, "2026-09-08", "2026-10-01", "200", "living")
        self.assertEqual(pending["status"], "NEEDS_REVIEW")
        self.assertIsNone(pending["report"])

    def test_leap_day_year_change_and_nonmonthly_payday_intervals(self):
        self.assertEqual(cycle_bounds("2024-02-29", "2024-03-07"), (date(2024, 2, 29), date(2024, 3, 6)))
        self.assertEqual(cycle_bounds("2026-12-20", "2027-01-03"), (date(2026, 12, 20), date(2027, 1, 2)))

    def test_project_cli_and_csv_exports_preserve_scope_and_boundary(self):
        self.configure()
        project = self.base / "project.yaml"
        create_project([self.september, self.config], project)
        output = StringIO()
        with redirect_stdout(output):
            code = report_main(["--project", str(project), "--payday", "2026-09-08", "--next-payday", "2026-10-02",
                                "--cycle-budget", "50", "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["report"]["cycle_budget"]["remaining"], "17")
        result = self.audit()
        for format in ("text", "json", "csv"):
            target = self.base / ("周期." + format)
            write_export(result, self.config, target, format, period=True)
            self.assertTrue(target.is_file())
            if format == "csv":
                self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))
                self.assertIn("cycle_remaining,17", target.read_text(encoding="utf-8-sig"))

    def test_cli_rejects_month_range_mixed_with_salary_or_incomplete_paydays(self):
        for args in (["--payday", "2026-09-08"], ["--cycle-budget", "50"],
                     ["--payday", "2026-09-08", "--next-payday", "2026-10-02", "--from", "2026-09", "--through", "2026-10"]):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                report_main(args)

    def test_input_changed_during_cycle_clears_result(self):
        self.configure()
        from beancount_budget_tool.v6 import salary_cycle
        original = salary_cycle._audit_spending_window
        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            self.paths["expense_plans"].write_text('# changed\n', encoding="utf-8")
            return result
        with patch.object(salary_cycle, "_audit_spending_window", side_effect=change), self.assertRaises(BudgetError) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")

    def test_project_identity_is_checked_against_the_exact_audited_config(self):
        self.configure()
        with self.assertRaises(BudgetError) as caught:
            audit_salary_cycle(self.config, "2026-09-08", "2026-10-02", "50",
                expected_identity=(self.ledger, date(2026, 10, 1), "CNY"))
        self.assertEqual(caught.exception.code, "PROJECT_CONTEXT_CHANGED")

    def test_tui_refresh_remembers_cycle_toggle_and_explicit_month_range_restores(self):
        self.configure()
        browser = ReportBrowser([self.september, self.config], audit_month_workflow, render_workflow_text)
        browser.select_salary_cycle("2026-09-08", "2026-10-02", "50")
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["report"]["cycle_budget"]["remaining"], "17")
        self.assertIn("工资周期", browser.title())
        browser.toggle_period()
        browser.refresh()
        self.assertFalse(browser.period)
        browser.toggle_period()
        browser.refresh()
        self.assertEqual(browser.outcome["mode"], "v6_salary_cycle_spending")
        browser.select_period("2026-09", "2026-09")
        browser.refresh()
        self.assertIsNone(browser.salary_cycle)
        self.assertEqual(browser.outcome["mode"], "v6_period_report")

    def test_tui_cancel_and_key_w_queries_exclusive_payday(self):
        self.configure()
        browser = ReportBrowser([self.september, self.config], audit_month_workflow, render_workflow_text)
        screen = FakeScreen()
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["2026-09-08", None]):
            _select_salary_cycle(screen, browser)
        self.assertFalse(browser.period)
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["2026-09-08", "2026-10-02", "50", ""]), \
                patch("beancount_budget_tool.v6.tui._read_key", side_effect=["w", "q"]), \
                patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertEqual(browser.outcome["report"]["cycle_budget"]["remaining"], "17")

    def test_tui_missing_end_month_or_bad_file_never_keeps_old_numbers(self):
        self.configure()
        browser = ReportBrowser([self.september, self.config], audit_month_workflow, render_workflow_text)
        browser.select_salary_cycle("2026-09-08", "2026-10-02", "50")
        browser.refresh()
        browser.configs.remove(self.config)
        browser.review_roots.pop()
        browser.refresh()
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.error.code, "SALARY_CYCLE_CONFIG_REQUIRED")

    def test_tui_recalculation_reads_saved_ledger_and_retains_cycle_target(self):
        self.configure()
        browser = ReportBrowser([self.september, self.config], audit_month_workflow, render_workflow_text)
        browser.select_salary_cycle("2026-09-08", "2026-10-02", "50")
        browser.refresh()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            'Expenses:Travel 3 CNY', 'Expenses:Travel 4 CNY').replace(
            'Assets:Cash -3 CNY', 'Assets:Cash -4 CNY'), encoding="utf-8")
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["report"]["cycle_budget"]["remaining"], "16")


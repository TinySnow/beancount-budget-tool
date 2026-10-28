"""The editable surface is config.yml + budgets.yml, not compiled sidecars."""

from copy import deepcopy
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool import financial
from beancount_budget_tool.yamlstrict import yaml_path_location
from beancount_budget_tool.v6.input_compiler import compile_inputs
from fixture_inputs import compile_setup, SetupDumper as _SetupDumper
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.simple_config import audit_simple, build_runtime, create_files, save_snapshot
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.monthly_report import main as monthly_main
from beancount_budget_tool.v6.review import prepare_review, save_review
from beancount_budget_tool.v6.tui import _next_month, _draw_browser, _report_browser, _edit_review, _confirm_month_end
from beancount_budget_tool.v6.tui_export import write_export
from test_v6_tui import FakeScreen


class SimpleConfigTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        examples = Path(__file__).resolve().parent / "fixtures"
        self.ledger = self.base / "main.bean"
        self.ledger.write_bytes((examples / "first-month.bean").read_bytes())
        setup = yaml.safe_load((examples / "first-month-setup.yaml").read_text(encoding="utf-8"))
        setup["ledger"] = "main.bean"
        self.write(self.base / "setup.yaml", setup)
        self.old = Path(compile_setup(self.base / "setup.yaml")["config"])
        self.original = {p: p.read_bytes() for p in self.old.parent.iterdir() if p.is_file()}

    def write(self, path, value):
        path.write_text(yaml.dump(value, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def migrate(self):
        result = create_files(self.base / "simple", old_config=self.old)
        self.config = Path(result["config"])
        self.budgets = Path(result["budgets"])
        return result

    def test_windows_relative_config_paths_produce_the_same_report_on_each_platform(self):
        self.migrate()
        expected = audit_simple(self.config)
        doc = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        doc["ledger"] = "..\\\\main.bean"
        doc["budgets"] = ".\\budgets.yml"
        self.write(self.config, doc)
        inputs = {path: path.read_bytes() for path in self.config.parent.rglob("*") if path.is_file()}
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"], expected["report"])
        self.assertTrue(all(path.read_bytes() == body for path, body in inputs.items()))

    @unittest.skipIf(sys.platform == "win32", "Foreign drive path rejection applies outside Windows")
    def test_foreign_windows_absolute_paths_point_to_the_config_field(self):
        self.migrate()
        for value in (r"C:\Users\name\main.bean", r"\\server\share\main.bean", r"C:main.bean"):
            with self.subTest(value=value):
                doc = yaml.safe_load(self.config.read_text(encoding="utf-8"))
                doc["ledger"] = value
                self.write(self.config, doc)
                with self.assertRaises(BudgetError) as caught:
                    audit_simple(self.config)
                self.assertEqual(caught.exception.code, "CONFIG_PATH_PLATFORM")
                self.assertEqual(caught.exception.source, yaml_path_location(self.config, ("ledger",)))

    def test_generated_yaml_paths_use_forward_slashes(self):
        from beancount_budget_tool.v6.input_compiler import relative_reference
        nested = self.base / "中文 空格" / "budget" / "simple"
        self.assertEqual(relative_reference(self.base / "中文 空格" / "main.bean", nested), "../../main.bean")

    def test_daily_month_indexes_only_visible_facts_even_with_future_card_transactions(self):
        from datetime import date
        from beancount.core import data
        from beancount_budget_tool.refs import ReferenceIndex
        self.migrate()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
            '\n2026-10-01 open Liabilities:Card\n'
            '2026-10-02 * "future card purchase"\n'
            '  Expenses:Food 3 CNY\n  Liabilities:Card -3 CNY\n', encoding="utf-8")
        captured = []
        def index(fin):
            captured.append(fin)
            return ReferenceIndex(fin)
        with patch("beancount_budget_tool.refs.ReferenceIndex", side_effect=index):
            result = audit_simple(self.config, include_transactions=True)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "190")
        self.assertTrue(captured)
        self.assertTrue(all(entry.date <= date(2026, 9, 30) for fin in captured for entry in fin.entries))
        self.assertTrue(any(isinstance(entry, data.Transaction) for fin in captured for entry in fin.entries))

    def test_daily_review_completes_without_importing_migration_commands(self):
        self.migrate()
        script = """
import json, sys
from pathlib import Path
from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6 import review, month_actions
result = audit_simple(Path(sys.argv[1]), include_transactions=True)
print(json.dumps({"status": result["status"], "commands": [name for name in sys.modules
                  if name.startswith("beancount_budget_tool.tools.")]}))
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.config)],
                                check=True, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(json.loads(result.stdout), {"status": "CERTIFIED_SCOPED", "commands": []})

    def test_daily_report_uses_runtime_documents_without_writing_sidecars(self):
        self.migrate()
        write = Path.write_text
        written = []
        def record(path, *args, **kwargs):
            written.append(path)
            return write(path, *args, **kwargs)
        with patch.object(Path, "write_text", record):
            result = audit_simple(self.config, include_transactions=True)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(written, [])
        self.assertNotIn("setup.yaml", {path.name for path in written})
        self.assertNotIn("START_HERE.md", {path.name for path in written})
        self.assertEqual(sorted(path.name for path in self.config.parent.iterdir() if path.is_file()),
                         ["budgets.yml", "config.yml"])

    def test_first_configuration_does_not_generate_a_legacy_setup_directory(self):
        write = Path.write_text
        written = []
        def record(path, *args, **kwargs):
            written.append(path.name)
            return write(path, *args, **kwargs)
        with patch.object(Path, "write_text", record):
            result = create_files(self.base / "fresh", ledger=self.ledger, month="2026-09")
        self.assertEqual(sorted(written), ["budgets.yml", "config.yml"])
        outcome = audit_simple(Path(result["config"]))
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertTrue(outcome["review_items"])  # Inspection never approves account declarations.

    def test_in_memory_compilation_neither_publishes_files_nor_mutates_input(self):
        source = self.base / "setup.yaml"
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
        original = deepcopy(document)
        output = self.base / "not-published"
        with patch.object(Path, "write_text", side_effect=AssertionError("unexpected publication")):
            documents, issues = compile_inputs(document, source, output)
        self.assertEqual(issues, [])
        self.assertIn("month.yaml", documents)
        self.assertEqual(document, original)
        self.assertFalse(output.exists())

    def test_compiler_pending_locations_use_public_keys_without_a_temporary_yaml(self):
        self.migrate()
        original = self.config.read_bytes()
        config = yaml.safe_load(original)
        config["accounts"]["Assets:Cash"]["payment_scope"]["CNY"] = None
        self.write(self.config, config)
        result = audit_simple(self.config)
        expected = yaml_path_location(self.config, ("accounts", "Assets:Cash", "payment_scope", "CNY"))
        self.assertTrue(any(item["source"] == expected and item["reason_code"] == "SETUP_DECISION_REQUIRED"
                            for item in result["review_items"]), result["review_items"])
        self.config.write_bytes(original)
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-09"]["expense_targets"]["日常开销"] = None
        self.write(self.budgets, plans)
        result = audit_simple(self.config)
        expected = yaml_path_location(self.budgets, ("months", "2026-09", "expense_targets", "日常开销"))
        self.assertTrue(any(item["source"] == expected and item["reason_code"] == "SETUP_DECISION_REQUIRED"
                            for item in result["review_items"]), result["review_items"])
        self.assertIsNone(result["report"])

    def test_migration_preserves_complete_report_and_does_not_touch_originals(self):
        previous = audit_month_workflow(self.old)
        self.migrate()
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        for key in ("actual_spendable", "max_spendable", "purpose_balances", "purpose_progress", "expense_targets"):
            before, after = deepcopy(previous["report"][key]), deepcopy(result["report"][key])
            # Plan file bytes differ after translating the public names.
            for row in (before, after):
                row.pop("plan_sha256", None)
            self.assertEqual(before, after, key)
        self.assertEqual({p: p.read_bytes() for p in self.original}, self.original)
        self.assertEqual(sorted(p.name for p in self.config.parent.iterdir() if p.is_file()), ["budgets.yml", "config.yml"])

    def test_inline_new_fund_is_registered_without_editing_an_owner_registry(self):
        self.migrate()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
            '\n2026-09-20 * "extra savings"\n'
            '  Assets:Cash -1 CNY\n    fund: "已收入资金"\n'
            '  Assets:Cash 1 CNY\n    fund: "额外储蓄"\n', encoding="utf-8")
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertIn("额外储蓄", result["report"]["owner_names"].values())

    def test_terminal_refresh_reuses_only_verified_source_parse(self):
        self.migrate()
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load:
            browser.refresh()
            self.assertIsNone(browser.error)
            self.assertEqual(load.call_count, 1)
            browser.refresh()
            self.assertIsNone(browser.error)
            self.assertEqual(load.call_count, 1)

    def test_terminal_final_source_failure_clears_the_previously_displayed_report(self):
        from beancount_budget_tool.v6.review_queue import audit_review_queue
        self.migrate()
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        def change_after_review(*args, **kwargs):
            result = audit_review_queue(*args, **kwargs)
            self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
                '\n2026-09-21 * "new expense"\n'
                '  Assets:Cash -1 CNY\n  Expenses:Food 1 CNY\n', encoding="utf-8")
            return result
        with patch("beancount_budget_tool.v6.monthly_report.audit_review_queue", side_effect=change_after_review):
            browser.refresh()
        self.assertEqual(browser.error.code, "INPUT_CHANGED")
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "189")

    def test_entire_simple_report_shares_one_capture_and_final_verification(self):
        self.migrate()
        with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load, \
             patch("beancount_budget_tool.v6.review_queue.Path", wraps=Path) as source_paths:
            first = audit_simple(self.config, include_transactions=True)
        source_paths.assert_not_called()  # Canonical source locations come from the frozen input.
        self.assertEqual(first["status"], "CERTIFIED_SCOPED")
        self.assertEqual(load.call_count, 1)
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
            '\n2026-09-21 * "new expense"\n'
            '  Assets:Cash -1 CNY\n  Expenses:Food 1 CNY\n', encoding="utf-8")
        second = audit_simple(self.config, include_transactions=True)
        self.assertEqual(second["status"], "CERTIFIED_SCOPED")
        self.assertEqual(second["report"]["max_spendable"]["max_spendable"], "189")
        self.assertNotEqual(first["report"]["ledger_snapshot"], second["report"]["ledger_snapshot"])

    def test_edit_between_preflight_and_report_cannot_publish_a_mixed_month(self):
        from beancount_budget_tool.v6.review_queue import audit_review_queue
        self.migrate()
        def change_after_review(*args, **kwargs):
            result = audit_review_queue(*args, **kwargs)
            self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
                '\n2026-09-21 * "new expense"\n'
                '  Assets:Cash -1 CNY\n  Expenses:Food 1 CNY\n', encoding="utf-8")
            return result
        with patch("beancount_budget_tool.v6.monthly_report.audit_review_queue", side_effect=change_after_review):
            with self.assertRaises(BudgetError) as error:
                audit_simple(self.config)
        self.assertEqual(error.exception.code, "INPUT_CHANGED")
        self.assertEqual(error.exception.source, str(self.ledger.resolve()))
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "189")

    def test_rereading_two_inputs_changes_target_without_manual_compilation(self):
        self.migrate()
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-09"]["expense_targets"]["日常开销"] = "50"
        self.write(self.budgets, plans)
        result = audit_simple(self.config)
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "40")

    def test_next_month_does_not_inherit_completeness_confirmation(self):
        self.migrate()
        result = audit_simple(self.config, "2026-10")
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(any(row["reason_code"] == "EXTERNAL_SCOPE_PENDING" for row in result["review_items"]), result["review_items"])

    def test_new_template_lists_unknown_account_settings_in_the_editable_config(self):
        result = create_files(self.base / "new", ledger=self.ledger, month="2026-09")
        config = Path(result["config"])
        result = audit_simple(config)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(result["review_items"])
        self.assertTrue(all(item["source"].startswith(str(config)) for item in result["review_items"]))

    def test_source_change_invalidates_a_prepared_runtime(self):
        self.migrate()
        runtime = build_runtime(self.config)
        self.budgets.write_text(self.budgets.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            runtime.check()
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")

    def test_second_migration_never_overwrites_existing_files(self):
        self.migrate()
        with self.assertRaises(BudgetError):
            create_files(self.config.parent, old_config=self.old)

    def test_public_monthly_entry_and_month_override_work_without_compile_command(self):
        self.migrate()
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(monthly_main(["--config", str(self.config), "--format", "json"]), 0)
        self.assertIn('"CERTIFIED_SCOPED"', output.getvalue())
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(monthly_main(["--config", str(self.config), "--month", "2026-10", "--format", "json"]), 0)
        self.assertIn('"2026-10"', output.getvalue())
        self.assertIn('"NEEDS_REVIEW"', output.getvalue())

    def test_browser_refresh_keeps_user_config_as_entry_and_rereads_budget_changes(self):
        self.migrate()
        browser = SimpleReportBrowser(self.config)
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.display_config, self.config)
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-09"]["expense_targets"]["日常开销"] = "30"
        self.write(self.budgets, plans)
        browser.refresh()
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "20")
        screen = FakeScreen(size=(24, 100))
        _draw_browser(screen, browser)
        self.assertIn("config.yml", screen.rows[1][2])
        self.assertNotIn(".budget-state", screen.rows[1][2])

    def test_next_month_and_jump_do_not_create_more_public_configurations(self):
        self.migrate()
        browser = SimpleReportBrowser(self.config)
        browser.refresh()
        _next_month(FakeScreen(), browser)
        self.assertEqual(browser.selected_month, "2026-10")
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "NEEDS_REVIEW")
        browser.jump_month("2026-09")
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(sorted(p.name for p in self.config.parent.iterdir() if p.is_file()), ["budgets.yml", "config.yml"])

    def test_simple_period_and_salary_cycle_use_current_public_inputs(self):
        self.migrate()
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-10"] = {"completeness_confirmed": True}
        self.write(self.budgets, plans)
        browser = SimpleReportBrowser(self.config)
        browser.select_period("2026-09", "2026-10")
        browser.refresh()
        self.assertIsNone(browser.error, browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED", browser.outcome["review_items"])
        browser.select_salary_cycle("2026-09-01", "2026-10-02", "100", "日常开销")
        browser.refresh()
        self.assertIsNone(browser.error, browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["report"]["cycle_budget"]["remaining"], "90")

    def test_saved_review_is_retained_after_refresh_and_public_target_edits(self):
        self.migrate()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            '2026-09-01 open Assets:Cash', '2026-09-01 open Assets:Bank\n2026-09-01 open Assets:Cash') +
            '\n2026-09-20 * "mixed transfer"\n  Assets:Cash -1 CNY\n  Assets:Bank 1 CNY\n', encoding="utf-8")
        doc = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        doc["accounts"]["Assets:Bank"] = {"semantics": "H", "payment_scope": {"CNY": "payable"}}
        self.write(self.config, doc)
        browser = SimpleReportBrowser(self.config)
        browser.refresh()
        item = next(item for item in browser.reviews() if item["reason_code"] == "FUND_MIXED_SOURCE")
        prepared = prepare_review(browser.config, item)
        saved = save_review(prepared, "已收入资金", "virtual explicit source choice", self.base / "approved", approved=True)
        browser.show_saved_month(saved)
        self.assertIsNone(browser.error, browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED", browser.outcome["review_items"])
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-09"]["expense_targets"]["日常开销"] = "40"
        self.write(self.budgets, plans)
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        restarted = SimpleReportBrowser(self.config)
        restarted.refresh()
        self.assertEqual(restarted.outcome["status"], "CERTIFIED_SCOPED")

    def test_actual_edit_key_path_saves_internal_state_without_retargeting_user_entry(self):
        self.migrate()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8").replace(
            '2026-09-01 open Assets:Cash', '2026-09-01 open Assets:Bank\n2026-09-01 open Assets:Cash') +
            '\n2026-09-20 * "mixed transfer"\n  Assets:Cash -1 CNY\n  Assets:Bank 1 CNY\n', encoding="utf-8")
        doc = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        doc["accounts"]["Assets:Bank"] = {"semantics": "H", "payment_scope": {"CNY": "payable"}}
        self.write(self.config, doc)
        browser = SimpleReportBrowser(self.config)
        browser.refresh()
        browser.selected = next(index for index, item in enumerate(browser.reviews()) if item["reason_code"] == "FUND_MIXED_SOURCE")
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["已收入资金", "virtual choice", "y"]), patch("beancount_budget_tool.v6.tui._message"):
            _edit_review(FakeScreen(size=(24, 100)), browser)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        restarted = SimpleReportBrowser(self.config)
        restarted.refresh()
        self.assertEqual(restarted.outcome["status"], "CERTIFIED_SCOPED")

    def test_actual_month_end_key_retains_confirmations_for_both_months_after_restart(self):
        self.migrate()
        browser = SimpleReportBrowser(self.config, "2026-10")
        browser.refresh()
        self.assertEqual(browser.outcome["status"], "NEEDS_REVIEW")
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["virtual month fully checked", "y"]), patch("beancount_budget_tool.v6.tui._message"):
            _confirm_month_end(FakeScreen(size=(30, 120)), browser)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        for month in ("2026-09", "2026-10"):
            restarted = SimpleReportBrowser(self.config, month)
            restarted.refresh()
            self.assertEqual(restarted.outcome["status"], "CERTIFIED_SCOPED", month)

    def test_snapshot_is_self_contained_after_runtime_files_are_removed(self):
        import shutil
        self.migrate()
        runtime = build_runtime(self.config)
        save_snapshot(self.config, runtime.config)
        shutil.rmtree(runtime.config.parent)
        self.assertEqual(audit_simple(self.config)["status"], "CERTIFIED_SCOPED")

    def test_invalid_public_configuration_erases_old_report_and_recovers(self):
        self.migrate()
        browser = SimpleReportBrowser(self.config)
        browser.refresh()
        before = self.budgets.read_bytes()
        self.budgets.write_text('schema: 1\nschema: 1\n', encoding="utf-8")
        browser.refresh()
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])
        self.assertEqual(browser.error.code, "YAML_DUPLICATE_KEY")
        self.budgets.write_bytes(before)
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")

    def test_null_target_is_pending_at_budget_line_instead_of_becoming_zero(self):
        self.migrate()
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-09"]["expense_targets"]["日常开销"] = None
        self.write(self.budgets, plans)
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["report"])
        self.assertTrue(any(item["source"].startswith(str(self.budgets) + ":") for item in result["review_items"]))

    def test_new_label_ids_are_stable_when_later_month_has_more_names(self):
        self.migrate()
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") +
            '\n2026-09-20 * "extra"\n  Assets:Cash -1 CNY\n    fund: "已收入资金"\n'
            '  Assets:Cash 1 CNY\n    fund: "额外储蓄"\n'
            '2026-10-20 * "another"\n  Assets:Cash -1 CNY\n    fund: "已收入资金"\n'
            '  Assets:Cash 1 CNY\n    fund: "新用途"\n', encoding="utf-8")
        september, october = build_runtime(self.config), build_runtime(self.config, "2026-10")
        def ids(runtime):
            return {row["name"]: row["id"] for row in yaml.safe_load((runtime.config.parent / "ownership.yaml").read_text(encoding="utf-8"))["owners"]}
        self.assertEqual(ids(september)["额外储蓄"], ids(october)["额外储蓄"])

    def test_explicit_false_revokes_imported_month_completeness(self):
        self.migrate()
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-09"]["completeness_confirmed"] = False
        self.write(self.budgets, plans)
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(any(item["reason_code"] == "EXTERNAL_SCOPE_PENDING" for item in result["review_items"]))

    def test_unresolved_pre_cutover_ownership_is_not_invented_as_outside_budget(self):
        self.ledger.write_text('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n2026-08-01 open Equity:Opening\n'
            '2026-08-30 * "opening"\n  Assets:Cash 100 CNY\n  Equity:Opening -100 CNY\n', encoding="utf-8")
        result = create_files(self.base / "new", ledger=self.ledger, month="2026-09")
        config = Path(result["config"])
        doc = yaml.safe_load(config.read_text(encoding="utf-8"))
        doc["accounts"]["Assets:Cash"]["semantics"] = "H"
        doc["accounts"]["Assets:Cash"]["payment_scope"] = {"CNY": "payable"}
        self.write(config, doc)
        runtime = build_runtime(config)
        opening = yaml.safe_load((runtime.config.parent / "opening.yaml").read_text(encoding="utf-8"))
        self.assertEqual(opening["h_holdings"], [])
        self.assertEqual(audit_simple(config)["status"], "NEEDS_REVIEW")

    def test_disabled_legacy_classification_defaults_stay_disabled(self):
        path = self.old.parent / "classifications.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc.pop("defaults")
        self.write(path, doc)
        self.migrate()
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertTrue(any(item["reason_code"] == "CLASSIFICATION_UNREVIEWED" for item in result["review_items"]))

    def test_missing_old_allocation_target_remains_pending_after_migration(self):
        path = self.old.parent / "purpose_plans.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["months"]["2026-09"]["allocation_targets"].pop(next(iter(doc["months"]["2026-09"]["allocation_targets"])))
        self.write(path, doc)
        self.migrate()
        self.assertEqual(audit_simple(self.config)["status"], "NEEDS_REVIEW")

    def test_cli_period_and_salary_keep_two_file_entry(self):
        from beancount_budget_tool.v6.period_report import main as period_main
        self.migrate()
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(period_main(["--config", str(self.config), "--from", "2026-09", "--through", "2026-09", "--format", "json"]), 0)
        self.assertIn('"CERTIFIED_SCOPED"', output.getvalue())
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(period_main(["--config", str(self.config), "--payday", "2026-09-01", "--next-payday", "2026-10-01", "--cycle-budget", "100", "--format", "json"]), 0)
        self.assertIn('"90"', output.getvalue())

    def test_current_directory_config_is_a_default_entry(self):
        self.migrate()
        with patch("beancount_budget_tool.v6.monthly_report.Path.cwd", return_value=self.config.parent), redirect_stdout(StringIO()) as output:
            self.assertEqual(monthly_main(["--format", "json"]), 0)
        self.assertIn('"CERTIFIED_SCOPED"', output.getvalue())

    def test_public_example_runs_with_only_the_two_configuration_files(self):
        source = Path(__file__).resolve().parents[1] / "examples/v6/simple"
        target = self.base / "example"
        target.mkdir()
        for path in source.iterdir():
            if path.is_file():
                (target / path.name).write_bytes(path.read_bytes())
        result = audit_month_workflow(target / "config.yml")
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "190")

    def test_migration_keeps_missing_old_expense_goal_pending(self):
        path = self.old.parent / "expense_plans.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["months"]["2026-09"] = {}
        self.write(path, doc)
        self.migrate()
        self.assertEqual(audit_simple(self.config)["status"], "NEEDS_REVIEW")

    def test_child_goals_preserve_derived_parent_without_inventing_a_cap(self):
        path = self.old.parent / "expense_plans.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        categories = yaml.safe_load((self.old.parent / "classifications.yaml").read_text(encoding="utf-8"))["categories"]
        child = next(row["id"] for row in categories if row["name"] == "日常开销.餐饮")
        doc.pop("spendable_cap_category_id")
        doc["months"]["2026-09"] = {"expense_targets": {child: "40"}}
        self.write(path, doc)
        before = audit_month_workflow(self.old)
        self.assertEqual(before["status"], "CERTIFIED_SCOPED")
        self.migrate()
        after = audit_simple(self.config)
        self.assertEqual(after["status"], "CERTIFIED_SCOPED", after["review_items"])
        self.assertEqual(after["report"]["max_spendable"]["status"], "NOT_CONFIGURED")
        first = before["report"]["expense_targets"]["categories"]
        second = after["report"]["expense_targets"]["categories"]
        self.assertEqual(first, second)

    def test_unquoted_decimal_goal_keeps_source_precision_without_float_rounding(self):
        self.migrate()
        text = self.budgets.read_text(encoding="utf-8")
        self.assertIn("'200'", text)
        self.budgets.write_text(text.replace("'200'", "200.000000000000000001", 1), encoding="utf-8")
        runtime = build_runtime(self.config)
        plan = yaml.safe_load((runtime.config.parent / "expense_plans.yaml").read_text(encoding="utf-8"))
        self.assertIn("200.000000000000000001", plan["months"]["2026-09"]["expense_subtree_targets"].values())

    def test_nonfinite_goal_is_reviewed_instead_of_becoming_spendable_money(self):
        self.migrate()
        text = self.budgets.read_text(encoding="utf-8")
        self.budgets.write_text(text.replace("'200'", ".inf", 1), encoding="utf-8")
        result = audit_simple(self.config)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["report"])


if __name__ == "__main__":
    unittest.main()

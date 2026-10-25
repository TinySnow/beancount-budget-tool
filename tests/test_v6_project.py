"""Project entry points persist navigation, never financial certification."""

from contextlib import redirect_stderr, redirect_stdout
from functools import partial
import io
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.launcher import main as launch
from beancount_budget_tool.tools.v6_init import _SetupDumper, compile_setup
from beancount_budget_tool.tools.v6_next_month import create_next_month, compile_next_month, confirm_month
from beancount_budget_tool.tools.v6_review_decisions import apply_review
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, main, render_workflow_text
from beancount_budget_tool.v6.period_report import audit_period, audit_project_period
from beancount_budget_tool.v6.project import create_project, load_project, _metadata, _validate
from beancount_budget_tool.v6.tui import ReportBrowser, _confirm_month_end, _draw_browser, _edit_review, _export_report, _jump_month, _next_month, _report_browser
from test_v6_tui import FakeScreen


def save(path, document):
    path.write_text(yaml.dump(document, Dumper=_SetupDumper, allow_unicode=True, sort_keys=False), encoding="utf-8")


class ProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.original = Path(temporary.name).resolve()
        examples = Path(__file__).resolve().parents[1] / "examples/v6"
        (cls.original / "main.bean").write_text((examples / "first-month.bean").read_text(encoding="utf-8") +
            '\n2026-10-05 * "公开虚构十月消费"\n  Expenses:Food 20 CNY\n  Assets:Cash -20 CNY\n', encoding="utf-8")
        sheet = yaml.safe_load((examples / "first-month-setup.yaml").read_text(encoding="utf-8"))
        sheet["ledger"] = "main.bean"
        save(cls.original / "setup.yaml", sheet)
        config = Path(compile_setup(cls.original / "setup.yaml")["config"])
        cls.relative_configs = [config.relative_to(cls.original)]
        for month in ("2026-10", "2026-11"):
            draft = create_next_month(config, cls.original / month)
            worksheet = Path(draft["worksheet"])
            document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
            document.update(targets_confirmed=True, completeness_confirmed=True, completeness_reason="公开虚构案例资料完整")
            save(worksheet, document)
            result = compile_next_month(worksheet)
            assert result["workflow"]["status"] == "CERTIFIED_SCOPED", result
            config = Path(result["config"])
            cls.relative_configs.append(config.relative_to(cls.original))

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve() / "项目 空格"
        self.base.mkdir()
        self.ledger_root = self.base / "ledger"
        shutil.copytree(self.original, self.ledger_root)
        self.configs = [self.ledger_root / relative for relative in self.relative_configs]
        self.path = self.base / "budget-project.yaml"

    def project(self, all_months=True):
        return create_project(self.configs if all_months else self.configs[:1], self.path)

    def browser(self, project):
        browser = ReportBrowser(list(project.months.values()), partial(audit_month_workflow, include_transactions=True),
                                render_workflow_text, project=project, initial_config=project.select())
        browser.refresh()
        return browser

    def invoke(self, function, args):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = function(args)
        return code, output.getvalue(), errors.getvalue()

    def test_registration_uses_relative_paths_and_does_not_modify_months(self):
        before = {p: p.read_bytes() for p in self.ledger_root.rglob("*") if p.is_file()}
        project = self.project()
        self.assertEqual(project.default_month, "2026-11")
        document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.assertTrue(all(not Path(value).is_absolute() for value in document["months"].values()))
        loaded = load_project(self.path)
        self.assertEqual(loaded.select("2026-09"), self.configs[0])
        self.assertEqual(before, {p: p.read_bytes() for p in self.ledger_root.rglob("*") if p.is_file()})
        with self.assertRaises(BudgetError) as caught:
            create_project(self.configs, self.path)
        self.assertEqual(caught.exception.code, "PROJECT_EXISTS")

    def test_creation_rejects_duplicate_months_and_mixed_ledger_context(self):
        for field, value in (("ledger", "other.bean"), ("cutover", "2026-08-01"), ("report_currency", "USD")):
            path = self.configs[1]
            original = path.read_bytes()
            try:
                document = yaml.safe_load(original.decode("utf-8"))
                if field == "ledger":
                    (path.parent / value).write_text("; another ledger\n", encoding="utf-8")
                document[field] = value
                save(path, document)
                with self.subTest(field=field), self.assertRaises(BudgetError) as caught:
                    create_project(self.configs, self.path)
                self.assertEqual(caught.exception.code, "PROJECT_CONTEXT_MISMATCH")
                self.assertFalse(self.path.exists())
            finally:
                path.write_bytes(original)
        with self.assertRaises(BudgetError) as caught:
            create_project([self.configs[0], self.configs[0]], self.path)
        self.assertEqual(caught.exception.code, "PROJECT_MONTH_DUPLICATE")

    def test_project_errors_have_file_and_line(self):
        self.project()
        original = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        for mutation, code in ((lambda doc: doc.update(default_month="2020-01"), "PROJECT_DEFAULT_INVALID"),
                               (lambda doc: doc["months"].update({"2026-09": "missing.yaml"}), "PROJECT_MONTH_MISSING"),
                               (lambda doc: doc["months"].update({"2026-09": doc["months"]["2026-10"]}), "PROJECT_MONTH_MISMATCH"),
                               (lambda doc: doc.update(unexpected=True), "PROJECT_INVALID")):
            document = yaml.safe_load(yaml.safe_dump(original))
            mutation(document)
            save(self.path, document)
            with self.subTest(code=code), self.assertRaises(BudgetError) as caught:
                load_project(self.path)
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.source.startswith(str(self.path) + ":"))

    def test_default_project_and_month_option_still_run_the_real_auditor(self):
        self.project()
        with patch("pathlib.Path.cwd", return_value=self.base):
            code, output, errors = self.invoke(main, ["--format", "json"])
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(json.loads(output)["month"], "2026-11")
            code, output, _ = self.invoke(main, ["--month", "2026-09", "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["report"]["max_spendable"]["max_spendable"], "190")
            ledger = self.ledger_root / "main.bean"
            ledger.write_text(ledger.read_text(encoding="utf-8").replace("Expenses:Food 10 CNY", "Expenses:Food 15 CNY")
                              .replace("Assets:Cash -10 CNY", "Assets:Cash -15 CNY"), encoding="utf-8")
            _, output, _ = self.invoke(main, ["--month", "2026-09", "--format", "json"])
            self.assertEqual(json.loads(output)["report"]["max_spendable"]["max_spendable"], "185")

    def test_no_argument_executable_launcher_opens_default_project(self):
        self.project(False)
        with patch("pathlib.Path.cwd", return_value=self.base):
            code, output, errors = self.invoke(launch, [])
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("最多能花：190 CNY", output)

    def test_unknown_month_returns_error_without_running_any_audit(self):
        self.project()
        with patch("beancount_budget_tool.v6.monthly_report.audit_month_workflow") as audit:
            code, output, errors = self.invoke(main, ["--project", str(self.path), "--month", "2026-12", "--format", "json"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors)["error"]["code"], "PROJECT_MONTH_MISSING")
        audit.assert_not_called()

    def test_project_cli_route_and_tui_selected_month(self):
        code, output, _ = self.invoke(launch, ["project", "--config", str(self.configs[0]), "--config", str(self.configs[1]),
                                               "--output", str(self.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "PROJECT_CREATED")
        with patch("beancount_budget_tool.v6.tui.run_report_tui") as run:
            self.assertEqual(main(["--project", str(self.path), "--month", "2026-09", "--format", "tui"]), 0)
        self.assertEqual(run.call_args.args[0], self.configs[:2])
        self.assertEqual(run.call_args.kwargs["initial_config"], self.configs[0])

    def test_m_key_jumps_to_a_registered_month_and_reaudits(self):
        browser = self.browser(self.project())
        self.assertEqual(browser.index, 2)
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"), \
             patch.object(browser, "loader", wraps=browser.loader) as load:
            _report_browser(FakeScreen(keys=["m", "2026-09", "\n", "q"], size=(30, 150)), browser)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(browser.config, self.configs[0])
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "190")

    def test_month_jump_cancel_or_unknown_month_keeps_current_config(self):
        browser = self.browser(self.project())
        for value in (None, "", "2026-12"):
            with self.subTest(value=value), patch("beancount_budget_tool.v6.tui._read_field", return_value=value), \
                 patch("beancount_budget_tool.v6.tui._message"):
                _jump_month(FakeScreen(), browser)
            self.assertEqual(browser.config, self.configs[-1])
        self.assertEqual(load_project(self.path).default_month, "2026-11")

    def test_review_save_updates_project_to_the_actual_latest_month_config(self):
        project = self.project(False)
        classifications = self.configs[0].parent / "classifications.yaml"
        document = yaml.safe_load(classifications.read_text(encoding="utf-8"))
        document.pop("defaults")
        save(classifications, document)
        browser = self.browser(project)
        self.assertEqual(len(browser.reviews()), 1)
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["已收入资金", "公开虚构工资收入", "y"]), \
             patch("beancount_budget_tool.v6.tui._message"):
            _edit_review(FakeScreen(size=(30, 150)), browser)
        self.assertNotEqual(browser.config, self.configs[0])
        self.assertIsNone(browser.project_error)
        reopened = load_project(self.path)
        self.assertEqual(reopened.select(), browser.config)
        self.assertEqual(self.browser(reopened).outcome["status"], "CERTIFIED_SCOPED")

    def test_next_month_is_registered_even_when_month_end_is_still_pending(self):
        project = self.project(False)
        browser = self.browser(project)
        draft = create_next_month(browser.config, self.base / "new-october")
        worksheet = Path(draft["worksheet"])
        document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        document["targets_confirmed"] = True
        save(worksheet, document)
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["2", str(worksheet)]), \
             patch("beancount_budget_tool.v6.tui._message"):
            _next_month(FakeScreen(size=(30, 150)), browser)
        reopened = load_project(self.path)
        self.assertEqual(reopened.default_month, "2026-10")
        self.assertEqual(reopened.select(), browser.config)
        outcome = self.browser(reopened).outcome
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["report"])
        self.assertIn("EXTERNAL_SCOPE_PENDING", {item["reason_code"] for item in outcome["review_items"]})

    def test_external_project_edit_is_preserved_and_new_saved_config_is_not_lost(self):
        browser = self.browser(self.project(False))
        document = yaml.safe_load(self.configs[0].read_text(encoding="utf-8"))
        document.pop("completeness")
        save(self.configs[0], document)
        browser.refresh()
        changed = self.path.read_bytes() + b"\n# edited externally\n"
        self.path.write_bytes(changed)
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=["公开虚构九月已完整核对", "y"]), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _confirm_month_end(FakeScreen(size=(30, 150)), browser)
        self.assertNotEqual(browser.config, self.configs[0])
        self.assertTrue(browser.config.is_file())
        self.assertEqual(browser.project_error.code, "PROJECT_CHANGED")
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertIn("项目入口尚未更新", "\n".join(message.call_args.args[2]))

    def test_atomic_project_replace_failure_does_not_destroy_existing_navigation(self):
        project = self.project(False)
        original = self.path.read_bytes()
        with patch("beancount_budget_tool.v6.project.os.replace", side_effect=PermissionError("readonly directory")):
            with self.assertRaises(OSError):
                project.remember(self.configs[1], self.configs[0])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(project.default_month, "2026-09")
        self.assertEqual(list(self.base.glob(".budget-project-*")), [])

    def test_cli_update_adds_next_month_and_replaces_reviewed_month_together(self):
        self.project(False)
        replacement = self.configs[0].with_name("latest-reviewed.yaml")
        replacement.write_bytes(self.configs[0].read_bytes())
        before = {p: p.read_bytes() for p in self.ledger_root.rglob("*") if p.is_file()}
        code, output, errors = self.invoke(launch, ["project", "--update", str(self.path),
            "--config", str(replacement), "--config", str(self.configs[1]), "--default-month", "2026-10", "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["status"], "PROJECT_UPDATED")
        project = load_project(self.path)
        self.assertEqual(project.months, {"2026-09": replacement, "2026-10": self.configs[1]})
        self.assertEqual(project.select(), self.configs[1])
        self.assertEqual(before, {p: p.read_bytes() for p in self.ledger_root.rglob("*") if p.is_file()})
        code, output, _ = self.invoke(main, ["--project", str(self.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["report"]["max_spendable"]["max_spendable"], "170")

    def test_update_preserves_other_months_and_default_unless_explicitly_changed(self):
        project = self.project()
        replacement = self.configs[0].with_name("reviewed.yaml")
        replacement.write_bytes(self.configs[0].read_bytes())
        project.register([replacement])
        reopened = load_project(self.path)
        self.assertEqual(reopened.default_month, "2026-11")
        self.assertEqual(reopened.months, {"2026-09": replacement, "2026-10": self.configs[1], "2026-11": self.configs[2]})
        code, output, errors = self.invoke(launch, ["project", "--update", str(self.path),
                                                  "--default-month", "2026-09", "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["default_month"], "2026-09")
        self.assertEqual(load_project(self.path).select(), replacement)

    def test_invalid_update_never_partially_registers_valid_configs(self):
        project = self.project(False)
        original = self.path.read_bytes()
        invalid = self.configs[2]
        document = yaml.safe_load(invalid.read_text(encoding="utf-8"))
        document["report_currency"] = "USD"
        save(invalid, document)
        for configs, default, expected in ((self.configs[1:], None, "PROJECT_CONTEXT_MISMATCH"),
                ([self.configs[1], self.configs[1]], None, "PROJECT_MONTH_DUPLICATE"),
                ([self.configs[1]], "2026-12", "PROJECT_DEFAULT_INVALID"),
                ([self.base / "absent.yaml"], None, "PROJECT_MONTH_MISSING")):
            with self.subTest(expected=expected), self.assertRaises(BudgetError) as caught:
                project.register(configs, default)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(project.months, {"2026-09": self.configs[0]})

    def test_registration_refuses_concurrent_project_or_config_edit(self):
        for edited in (self.path, self.configs[1]):
            if self.path.exists():
                self.path.unlink()
            project = self.project(False)
            original = self.path.read_bytes()
            def mutate_after_validation(months, source):
                result = _validate(months, source)
                edited.write_bytes(edited.read_bytes() + b"\n# external edit\n")
                return result
            with self.subTest(edited=edited), patch("beancount_budget_tool.v6.project._validate", side_effect=mutate_after_validation):
                with self.assertRaises(BudgetError) as caught:
                    project.register([self.configs[1]])
            self.assertEqual(caught.exception.code, "PROJECT_CHANGED")
            self.assertEqual(self.path.read_bytes(), original + (b"\n# external edit\n" if edited == self.path else b""))
            self.assertEqual(project.months, {"2026-09": self.configs[0]})
            self.assertEqual(list(self.base.glob(".budget-project-*")), [])

    def test_tui_save_preserves_header_guard_before_registration(self):
        project = self.project(False)
        original = self.path.read_bytes()
        reads = 0
        def mutate_after_first_header(path):
            nonlocal reads
            result = _metadata(path)
            reads += 1
            if reads == 1:
                path.write_bytes(path.read_bytes() + b"\n# saved config edited externally\n")
            return result
        with patch("beancount_budget_tool.v6.project._metadata", side_effect=mutate_after_first_header):
            with self.assertRaises(BudgetError) as caught:
                project.remember(self.configs[1], self.configs[0])
        self.assertEqual(caught.exception.code, "PROJECT_CHANGED")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(project.months, {"2026-09": self.configs[0]})

    def test_project_period_matches_explicit_configs_and_uses_closing_stock_once(self):
        self.project()
        result = audit_project_period(self.path)
        self.assertEqual(result, audit_period(self.configs))
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["expense_totals"]["direct_spent"], "30")
        self.assertEqual(result["report"]["closing"]["max_spendable"]["max_spendable"], "170")

    def test_project_range_ignores_other_month_sidecars_and_default_selection(self):
        self.project()
        document = yaml.safe_load(self.configs[0].read_text(encoding="utf-8"))
        document["purpose_targets"] = "not-available.yaml"
        save(self.configs[0], document)
        result = audit_project_period(self.path, "2026-10", "2026-11")
        self.assertEqual(result, audit_period(self.configs[1:]))
        self.assertEqual(result["report"]["expense_totals"]["direct_spent"], "20")

    def test_project_period_cli_uses_default_entry_and_saves_unicode_csv(self):
        self.project()
        with patch("pathlib.Path.cwd", return_value=self.base):
            code, output, errors = self.invoke(launch, ["report", "--from", "2026-09", "--through", "2026-10", "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        result = json.loads(output)
        self.assertEqual((result["from"], result["through"]), ("2026-09", "2026-10"))
        exported = self.base / "累计 报表.csv"
        code, _, errors = self.invoke(launch, ["report", "--project", str(self.path), "--from", "2026-09",
                                               "--through", "2026-09", "--format", "csv", "--output", str(exported)])
        self.assertEqual((code, errors), (0, ""))
        self.assertTrue(exported.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn("日常开销", exported.read_text(encoding="utf-8-sig"))

    def test_missing_project_month_has_location_and_does_not_run_audit(self):
        create_project([self.configs[0], self.configs[2]], self.path)
        with patch("beancount_budget_tool.v6.period_report.audit_period") as audit:
            with self.assertRaises(BudgetError) as caught:
                audit_project_period(self.path)
        self.assertEqual(caught.exception.code, "PERIOD_MONTHS_INCOMPLETE")
        self.assertIn("2026-10", caught.exception.message)
        self.assertTrue(caught.exception.source.startswith(str(self.path) + ":"))
        audit.assert_not_called()

    def test_invalid_project_range_never_runs_month_audits(self):
        self.project()
        for first, last in ((None, "2026-10"), ("2026-10", None), ("2026-11", "2026-09"),
                            ("2026-09", "2026-12"), ("2026-00", "2026-11")):
            with self.subTest(first=first, last=last), patch("beancount_budget_tool.v6.period_report.audit_period") as audit:
                with self.assertRaises(BudgetError):
                    audit_project_period(self.path, first, last)
                audit.assert_not_called()

    def test_project_period_pending_month_suppresses_interval_numbers(self):
        self.project()
        document = yaml.safe_load(self.configs[1].read_text(encoding="utf-8"))
        document.pop("completeness")
        save(self.configs[1], document)
        result = audit_project_period(self.path)
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["report"])
        self.assertIn("2026-10", {item["month"] for item in result["review_items"]})
        self.assertTrue(all(item["source"] for item in result["review_items"]))

    def test_project_edit_during_period_audit_does_not_publish_output(self):
        self.project()
        exported = self.base / "result.json"
        def audit_and_edit(*args):
            result = audit_period(*args)
            self.path.write_bytes(self.path.read_bytes() + b"\n# changed while calculating\n")
            return result
        with patch("beancount_budget_tool.v6.period_report.audit_period", side_effect=audit_and_edit):
            code, output, errors = self.invoke(launch, ["report", "--project", str(self.path),
                                                      "--format", "json", "--output", str(exported)])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("INPUT_CHANGED", errors)
        self.assertFalse(exported.exists())

    def test_project_period_reloads_ledger_instead_of_reusing_old_numbers(self):
        self.project()
        before = audit_project_period(self.path, "2026-09", "2026-09")
        ledger = self.ledger_root / "main.bean"
        ledger.write_text(ledger.read_text(encoding="utf-8").replace("Expenses:Food 10 CNY", "Expenses:Food 15 CNY")
                          .replace("Assets:Cash -10 CNY", "Assets:Cash -15 CNY"), encoding="utf-8")
        after = audit_project_period(self.path, "2026-09", "2026-09")
        self.assertEqual(before["report"]["closing"]["max_spendable"]["max_spendable"], "190")
        self.assertEqual(after["report"]["closing"]["max_spendable"]["max_spendable"], "185")

    def review_sheet(self, folder="review", *, approve=True):
        classifications = self.configs[0].parent / "classifications.yaml"
        document = yaml.safe_load(classifications.read_text(encoding="utf-8"))
        document.pop("defaults", None)
        save(classifications, document)
        code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--month", "2026-09",
                                                   "--output", str(self.base / folder), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        sheet = Path(json.loads(output)["worksheet"])
        document = yaml.safe_load(sheet.read_text(encoding="utf-8"))
        self.assertEqual(len(document["rows"]), 1)
        if approve:
            document["rows"][0].update(approved=True, reason="公开虚构工资分类确认", decision="已收入资金")
            save(sheet, document)
        return sheet

    def test_project_cli_lifecycle_reviews_prepares_and_closes_next_month(self):
        project = self.project(False)
        original = self.path.read_bytes()
        sheet = self.review_sheet()
        self.assertEqual(self.path.read_bytes(), original)
        instructions = sheet.with_name("START_HERE.md").read_text(encoding="utf-8")
        self.assertIn("--project", instructions)
        self.assertIn(str(self.path), instructions)
        code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--apply", str(sheet), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        reviewed = json.loads(output)
        self.assertEqual(reviewed["project_status"], "UPDATED")
        self.assertEqual(reviewed["workflow"]["status"], "CERTIFIED_SCOPED")
        september = Path(reviewed["config"])
        self.assertEqual(load_project(self.path).select(), september)
        code, output, errors = self.invoke(launch, ["next-month", "--project", str(self.path),
                                                   "--output", str(self.base / "october"), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        draft = json.loads(output)
        worksheet = Path(draft["worksheet"])
        document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        self.assertEqual((worksheet.parent / document["config"]).resolve(), september)
        self.assertFalse(document["completeness_confirmed"])
        document["targets_confirmed"] = True
        save(worksheet, document)
        code, output, errors = self.invoke(launch, ["next-month", "--project", str(self.path),
                                                   "--apply", str(worksheet), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        october = json.loads(output)
        self.assertEqual(october["project_status"], "UPDATED")
        self.assertEqual(october["workflow"]["status"], "NEEDS_REVIEW")
        self.assertIsNone(october["workflow"]["report"])
        self.assertEqual(load_project(self.path).select(), Path(october["config"]))
        code, output, errors = self.invoke(launch, ["review", "--project", str(self.path),
            "--output", str(self.base / "october-review"), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        october_sheet = Path(json.loads(output)["worksheet"])
        document = yaml.safe_load(october_sheet.read_text(encoding="utf-8"))
        self.assertEqual(len(document["rows"]), 1)
        document["rows"][0].update(approved=True, reason="公开虚构十月餐饮确认", decision="日常开销.餐饮")
        save(october_sheet, document)
        code, output, errors = self.invoke(launch, ["review", "--project", str(self.path),
            "--apply", str(october_sheet), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["workflow"]["status"], "NEEDS_REVIEW")  # Month-end still unconfirmed.
        code, output, errors = self.invoke(launch, ["next-month", "--project", str(self.path),
            "--confirm-completeness", "--reason", "公开虚构十月已完整核对", "--output", str(self.base / "closed"), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        closed = json.loads(output)
        self.assertEqual(closed["project_status"], "UPDATED")
        self.assertEqual(closed["workflow"]["status"], "CERTIFIED_SCOPED")
        reopened = load_project(self.path)
        self.assertEqual(reopened.months, {"2026-09": september, "2026-10": Path(closed["config"])})
        period = audit_project_period(self.path)
        self.assertEqual(period["report"]["expense_totals"]["direct_spent"], "30")
        self.assertEqual(period["report"]["closing"]["max_spendable"]["max_spendable"], "170")
        self.assertEqual(project.default_month, "2026-09")  # The original in-memory view is not reused.

    def test_unapproved_review_keeps_project_and_other_months_unchanged(self):
        self.project()
        original = self.path.read_bytes()
        sheet = self.review_sheet(approve=False)
        code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--apply", str(sheet), "--format", "json"])
        self.assertEqual((code, errors), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["status"], "NO_APPROVED_DECISIONS")
        self.assertEqual(result["project_status"], "UNCHANGED")
        self.assertIsNone(result["config"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(sheet.with_name("reviewed").exists())

    def test_stale_review_sheet_cannot_replace_a_more_recent_project_entry(self):
        project = self.project(False)
        sheet = self.review_sheet()
        replacement = self.configs[0].with_name("later-review.yaml")
        replacement.write_bytes(self.configs[0].read_bytes())
        project.register([replacement])
        original = self.path.read_bytes()
        code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--apply", str(sheet), "--format", "json"])
        self.assertEqual((code, output), (2, ""))
        error = json.loads(errors)["error"]
        self.assertEqual(error["code"], "PROJECT_MONTH_CHANGED")
        self.assertTrue(error["source"].startswith(str(sheet) + ":"))
        self.assertFalse(sheet.with_name("reviewed").exists())
        self.assertEqual(self.path.read_bytes(), original)

    def test_stale_next_month_sheet_requires_latest_base_config(self):
        project = self.project(False)
        draft = create_next_month(self.configs[0], self.base / "october")
        worksheet = Path(draft["worksheet"])
        replacement = self.configs[0].with_name("later-review.yaml")
        replacement.write_bytes(self.configs[0].read_bytes())
        project.register([replacement])
        code, output, errors = self.invoke(launch, ["next-month", "--project", str(self.path), "--apply", str(worksheet), "--format", "json"])
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(errors)["error"]["code"], "PROJECT_MONTH_CHANGED")
        self.assertFalse(worksheet.with_name("compiled").exists())

    def test_registered_successor_is_not_regenerated_from_the_previous_month(self):
        self.project()
        output_dir = self.base / "duplicate"
        code, output, errors = self.invoke(launch, ["next-month", "--project", str(self.path), "--month", "2026-09",
                                                   "--output", str(output_dir), "--format", "json"])
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(errors)["error"]["code"], "PROJECT_NEXT_MONTH_EXISTS")
        self.assertFalse(output_dir.exists())

    def test_project_edit_before_review_publication_leaves_no_saved_decisions(self):
        self.project(False)
        sheet = self.review_sheet()
        changed = self.path.read_bytes() + b"\n# concurrent edit\n"
        def change_before_apply(*args, **kwargs):
            self.path.write_bytes(changed)
            return apply_review(*args, **kwargs)
        with patch("beancount_budget_tool.tools.v6_review_decisions.apply_review", side_effect=change_before_apply):
            code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--apply", str(sheet), "--format", "json"])
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(errors)["error"]["code"], "INPUT_CHANGED")
        self.assertFalse(sheet.with_name("reviewed").exists())
        self.assertEqual(self.path.read_bytes(), changed)

    def test_project_update_failure_keeps_saved_review_path_and_external_edit(self):
        self.project(False)
        sheet = self.review_sheet()
        changed = self.path.read_bytes() + b"\n# concurrent edit after save\n"
        def fail_after_save(*args):
            self.path.write_bytes(changed)
            raise BudgetError("PROJECT_CHANGED", "外部已修改项目", str(self.path))
        with patch("beancount_budget_tool.v6.project.Project.remember", side_effect=fail_after_save):
            code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--apply", str(sheet), "--format", "json"])
        self.assertEqual((code, errors), (2, ""))
        result = json.loads(output)
        self.assertEqual(result["status"], "DECISIONS_SAVED")
        self.assertEqual(result["project_status"], "UPDATE_FAILED")
        self.assertTrue(Path(result["config"]).is_file())
        self.assertEqual(result["workflow"]["status"], "CERTIFIED_SCOPED")
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(load_project(self.path).select(), self.configs[0])

    def test_review_post_save_io_failure_keeps_and_registers_saved_config(self):
        self.project(False)
        sheet = self.review_sheet()
        calls = 0
        def fail_post_save(config):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("virtual post-save read failure")
            return audit_month_workflow(config)
        with patch("beancount_budget_tool.v6.review.audit_month_workflow", side_effect=fail_post_save):
            code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--apply", str(sheet), "--format", "json"])
        self.assertEqual((code, errors), (2, ""))
        result = json.loads(output)
        self.assertIsNone(result["workflow"])
        self.assertEqual(result["report_error"]["code"], "REVIEW_IO_ERROR")
        self.assertEqual(result["project_status"], "UPDATED")
        self.assertEqual(load_project(self.path).select(), Path(result["config"]))
        self.assertEqual(audit_month_workflow(Path(result["config"]))["status"], "CERTIFIED_SCOPED")

    def test_project_cli_closes_selected_month_and_retains_later_months(self):
        self.project()
        with patch("beancount_budget_tool.v6.month_actions.audit_month_workflow", side_effect=PermissionError("virtual failed read")):
            code, output, errors = self.invoke(launch, ["next-month", "--project", str(self.path), "--month", "2026-09",
                "--confirm-completeness", "--reason", "公开虚构九月完整", "--output", str(self.base / "closed"), "--format", "json"])
        self.assertEqual((code, errors), (2, ""))
        result = json.loads(output)
        self.assertEqual(result["project_status"], "UPDATED")
        self.assertIsNone(result["workflow"])
        reopened = load_project(self.path)
        self.assertEqual(reopened.select(), Path(result["config"]))
        self.assertEqual(reopened.months["2026-10"], self.configs[1])
        self.assertEqual(reopened.months["2026-11"], self.configs[2])

    def test_project_selection_and_explicit_config_must_agree(self):
        self.project()
        code, output, errors = self.invoke(launch, ["review", "--project", str(self.path), "--month", "2026-09",
            "--config", str(self.configs[1]), "--output", str(self.base / "wrong"), "--format", "json"])
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(errors)["error"]["code"], "PROJECT_MONTH_MISMATCH")
        self.assertFalse((self.base / "wrong").exists())

    def external_revision(self):
        """A virtual CLI save lowers September's target from 200 to 100."""
        config = self.configs[0]
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        plans = yaml.safe_load((config.parent / document["expense_plans"]).read_text(encoding="utf-8"))
        targets = plans["months"]["2026-09"]["expense_subtree_targets"]
        targets[next(iter(targets))] = "100"
        revised_plan = config.with_name("external-plans.yaml")
        save(revised_plan, plans)
        document["expense_plans"] = revised_plan.name
        revised = config.with_name("external-month.yaml")
        save(revised, document)
        return revised

    def test_tui_refresh_follows_external_save_and_new_months_without_jumping_default(self):
        browser = self.browser(self.project(False))
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "190")
        revised = self.external_revision()
        load_project(self.path).register([revised, *self.configs[1:]], default_month="2026-11")
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.config, revised)
        self.assertEqual(browser.project.default_month, "2026-11")
        self.assertEqual(browser.configs, [revised, *self.configs[1:]])
        self.assertEqual(browser.outcome["month"], "2026-09")
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "90")
        browser.jump_month("2026-11")
        browser.refresh()
        self.assertEqual(browser.outcome["month"], "2026-11")

    def test_actual_r_key_reloads_project_paths(self):
        browser = self.browser(self.project(False))
        revised = self.external_revision()
        keys = iter(("r", "q"))
        screen = FakeScreen(size=(30, 150))
        def get_key():
            key = next(keys)
            if key == "r":
                load_project(self.path).register([revised])
            return key
        with patch.object(screen, "get_wch", side_effect=get_key), patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertEqual(browser.config, revised)
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "90")

    def test_project_refresh_does_not_need_obsolete_config_file_to_still_exist(self):
        browser = self.browser(self.project(False))
        revised = self.external_revision()
        load_project(self.path).register([revised])
        self.configs[0].unlink()
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.config, revised)
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "90")

    def test_project_interval_refresh_uses_external_revised_month_config(self):
        browser = self.browser(self.project())
        browser.toggle_period()
        browser.refresh()
        revised = self.external_revision()
        load_project(self.path).register([revised])
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertTrue(browser.period)
        self.assertEqual(browser.config, self.configs[2])
        self.assertEqual(browser.outcome["months"][0]["config"], str(revised))
        first = browser.outcome["report"]["monthly_reports"][0]
        self.assertEqual(first["max_spendable"]["max_spendable"], "90")
        self.assertEqual(browser.outcome["report"]["closing"]["max_spendable"]["max_spendable"], "170")

    def test_tui_export_follows_project_save_during_export_form(self):
        browser = self.browser(self.project(False))
        revised = self.external_revision()
        exported = self.base / "最新月报.json"
        def form(_screen, title, *_args):
            if title == "导出报表":
                return "json"
            load_project(self.path).register([revised])
            return str(exported)
        with patch("beancount_budget_tool.v6.tui._read_field", side_effect=form), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _export_report(FakeScreen(), browser)
        self.assertEqual(message.call_args.args[1], "已导出")
        self.assertEqual(browser.config, revised)
        self.assertEqual(json.loads(exported.read_text(encoding="utf-8"))["report"]["max_spendable"]["max_spendable"], "90")

    def test_invalid_project_clears_old_numbers_and_recovers_after_repair(self):
        browser = self.browser(self.project(False))
        original = self.path.read_bytes()
        browser.toggle_transactions()
        browser.open_detail()
        self.path.write_text("months: [\n", encoding="utf-8")
        browser.refresh()
        self.assertEqual(browser.error.code, "YAML_INVALID")
        self.assertIsNone(browser.outcome)
        self.assertIsNone(browser.detail)
        self.assertEqual(browser.lines, [])
        screen = FakeScreen(size=(30, 150))
        _draw_browser(screen, browser)
        self.assertNotIn("最多能花：190", "\n".join(value for _, _, value in screen.rows))
        self.path.write_bytes(original)
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "190")

    def test_removed_current_month_is_reported_instead_of_silently_switching_month(self):
        browser = self.browser(self.project())
        browser.jump_month("2026-09")
        browser.refresh()
        project = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        del project["months"]["2026-09"]
        save(self.path, project)
        browser.refresh()
        self.assertEqual(browser.error.code, "PROJECT_MONTH_MISSING")
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.config, self.configs[0])

    def test_changing_project_ledger_requires_reopening_instead_of_changing_scope(self):
        browser = self.browser(self.project(False))
        other_ledger = self.ledger_root / "other.bean"
        other_ledger.write_bytes((self.ledger_root / "main.bean").read_bytes())
        document = yaml.safe_load(self.configs[0].read_text(encoding="utf-8"))
        document["ledger"] = str(other_ledger)
        save(self.configs[0], document)
        browser.refresh()
        self.assertEqual(browser.error.code, "PROJECT_CONTEXT_CHANGED")
        self.assertIsNone(browser.outcome)

    def test_project_edit_while_tui_audits_prevents_publishing_numbers_or_export(self):
        browser = self.browser(self.project(False))
        loader = browser.loader
        def calculate_and_edit(config):
            result = loader(config)
            self.path.write_bytes(self.path.read_bytes() + b"\n# project edited during audit\n")
            return result
        exported = self.base / "must-not-export.json"
        with patch.object(browser, "loader", side_effect=calculate_and_edit), \
             patch("beancount_budget_tool.v6.tui._read_field", side_effect=["json", str(exported)]), \
             patch("beancount_budget_tool.v6.tui._message") as message:
            _export_report(FakeScreen(), browser)
        self.assertEqual(message.call_args.args[1], "导出失败")
        self.assertEqual(browser.error.code, "INPUT_CHANGED")
        self.assertIsNone(browser.outcome)
        self.assertFalse(exported.exists())
        browser.refresh()
        self.assertIsNone(browser.error)

    def test_refresh_retains_unregistered_local_save_then_recovers_after_registration(self):
        browser = self.browser(self.project(False))
        result = confirm_month(browser.config, "公开虚构九月完整", self.base / "closed")
        with patch.object(browser.project, "remember", side_effect=PermissionError("virtual write failure")):
            browser.show_saved_month(result)
        local = Path(result["config"])
        self.assertEqual(browser.config, local)
        self.assertIsNotNone(browser.project_error)
        load_project(self.path).register([self.configs[1]], default_month="2026-10")
        browser.refresh()
        self.assertEqual(browser.config, local)
        self.assertIsNotNone(browser.project_error)
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "190")
        load_project(self.path).register([local])
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertIsNone(browser.project_error)
        self.assertEqual(browser.config, local)
        self.assertEqual(browser.configs, [local, self.configs[1]])


if __name__ == "__main__":
    unittest.main()

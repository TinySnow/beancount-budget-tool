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
from beancount_budget_tool.tools.v6_next_month import create_next_month, compile_next_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, main, render_workflow_text
from beancount_budget_tool.v6.period_report import audit_period, audit_project_period
from beancount_budget_tool.v6.project import create_project, load_project, _metadata, _validate
from beancount_budget_tool.v6.tui import ReportBrowser, _confirm_month_end, _edit_review, _jump_month, _next_month, _report_browser
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


if __name__ == "__main__":
    unittest.main()

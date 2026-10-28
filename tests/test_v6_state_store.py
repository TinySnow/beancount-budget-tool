"""Cache cleanup retains approved facts, monthly evidence and recovery data."""

from copy import deepcopy
import gzip
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.simple_config import (
    SCALARS, _write, audit_simple, build_runtime, save_snapshot, state_dir,
)
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6.state_store import clean, export, restore_previous, state_file


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        source = Path(__file__).resolve().parents[1] / "examples/v6/simple"
        for file in source.iterdir():
            if file.is_file():
                (self.base / file.name).write_bytes(file.read_bytes())
        self.config = self.base / "config.yml"
        self.budgets = self.base / "budgets.yml"

    def legacy_snapshot(self, month, ident):
        runtime = build_runtime(self.config, month)
        self.addCleanup(runtime.close)
        root = state_dir(self.config)
        target = root / "snapshots" / ident
        target.mkdir(parents=True)
        document = yaml.safe_load(runtime.config.read_text(encoding="utf-8"))
        for key in set(document) - SCALARS:
            (target / f"{key}.yaml").write_bytes((runtime.config.parent / document[key]).read_bytes())
            document[key] = f"{key}.yaml"
        _write(target / "month.yaml", document)
        manifest = root / "latest.yaml"
        previous = yaml.safe_load(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        relative = (target / "month.yaml").relative_to(root).as_posix()
        _write(manifest, {"snapshot": relative, "months": {**previous.get("months", {}), month: relative}})
        return target / "month.yaml"

    def data(self):
        return json.loads(gzip.decompress(state_file(self.config).read_bytes()))

    def test_legacy_cleanup_reduces_to_one_file_and_keeps_both_signed_months(self):
        plans = yaml.safe_load(self.budgets.read_text(encoding="utf-8"))
        plans["months"]["2026-10"] = {"completeness_confirmed": True}
        _write(self.budgets, plans)
        self.legacy_snapshot("2026-09", "first")
        self.legacy_snapshot("2026-10", "second")
        # Make the test rely on saved evidence instead of public booleans.
        for month in plans["months"].values():
            month.pop("completeness_confirmed", None)
        _write(self.budgets, plans)
        before = {month: audit_simple(self.config, month) for month in ("2026-09", "2026-10")}
        runtime = state_dir(self.config) / "runtime" / "unused"
        runtime.mkdir(parents=True)
        (runtime / "cache.yaml").write_text("rebuildable: true\n", encoding="utf-8")
        (self.base / "_internal").mkdir()
        dependency = self.base / "_internal" / "python.dll"
        dependency.write_bytes(b"required dependency")
        sources = {path: path.read_bytes() for path in (self.config, self.budgets, self.base / "main.bean", dependency)}
        result = clean(self.config)
        self.assertGreater(result["files_removed"], 20)
        self.assertEqual(sorted(path.name for path in state_dir(self.config).iterdir()), ["reviews.json.gz"])
        for month, previous in before.items():
            after = audit_simple(self.config, month)
            self.assertEqual(after["status"], "CERTIFIED_SCOPED")
            self.assertEqual(after["report"]["actual_spendable"], previous["report"]["actual_spendable"])
            self.assertEqual(after["report"]["max_spendable"], previous["report"]["max_spendable"])
        self.assertEqual({path: path.read_bytes() for path in sources}, sources)
        self.assertNotIn("runtime/unused/cache.yaml", self.data()["recovery_files"])
        self.assertIn("snapshots/first/month.yaml", self.data()["recovery_files"])

    def test_review_receipt_keeps_referenced_runtime_data_before_cache_deletion(self):
        reviewed = self.legacy_snapshot("2026-09", "first")
        root = state_dir(self.config)
        runtime = root / "runtime" / "referenced"
        shutil.copytree(reviewed.parent, runtime)
        receipt = root / "reviews" / "old-receipt"
        receipt.mkdir(parents=True)
        _write(receipt / "decisions.yaml", {"config": "../../runtime/referenced/month.yaml", "reason": "retained receipt"})
        clean(self.config)
        recovery = self.data()["recovery_files"]
        self.assertIn("reviews/old-receipt/decisions.yaml", recovery)
        self.assertIn("runtime/referenced/month.yaml", recovery)
        self.assertIn("runtime/referenced/ownership.yaml", recovery)
        self.assertFalse(runtime.exists())

    def test_dry_run_does_not_write_or_delete_any_state(self):
        self.legacy_snapshot("2026-09", "first")
        before = {p: p.read_bytes() for p in state_dir(self.config).rglob("*") if p.is_file()}
        result = clean(self.config, dry_run=True)
        self.assertGreater(result["files_removed"], 0)
        self.assertFalse(state_file(self.config).exists())
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_inline_only_runtime_creates_no_persistent_state(self):
        paths = []
        from tempfile import mkdtemp
        def record(*args, **kwargs):
            path = mkdtemp(*args, **kwargs)
            paths.append(Path(path))
            return path
        with patch("beancount_budget_tool.v6.simple_config.mkdtemp", side_effect=record):
            self.assertEqual(audit_simple(self.config)["status"], "CERTIFIED_SCOPED")
        self.assertEqual(paths, [])
        self.assertFalse(state_dir(self.config).exists())

    def test_opening_purpose_is_registered_without_a_redundant_goal_or_fund_tag(self):
        ledger = self.base / "main.bean"
        text = ledger.read_text(encoding="utf-8").replace("2026-09-01 open", "2026-08-01 open")
        text = text.replace('  Assets:Cash -200 CNY\n', '  Assets:Cash -200 CNY\n    fund: "自由资金"\n')
        text += '\n2026-08-01 open Equity:Opening\n\n2026-08-31 * "Opening"\n  Assets:Cash 300 CNY\n  Equity:Opening -300 CNY\n'
        ledger.write_text(text, encoding="utf-8")
        doc = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        doc["accounts"]["Assets:Cash"]["opening"] = {"CNY": {"自由资金": "100", "储蓄": "200"}}
        _write(self.config, doc)
        outcome = audit_simple(self.config)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        self.assertEqual(outcome["report"]["max_spendable"]["max_spendable"], "190")

    def test_refresh_releases_previous_temporary_inputs_and_close_releases_current(self):
        browser = SimpleReportBrowser(self.config)
        browser.refresh()
        old = Path(browser.runtime.temporary.name)
        self.assertTrue(old.exists())
        browser.refresh()
        current = Path(browser.runtime.temporary.name)
        self.assertFalse(old.exists())
        self.assertTrue(current.exists())
        browser.close()
        self.assertFalse(current.exists())

    def test_cli_failure_also_cleans_generated_files(self):
        paths = []
        from tempfile import mkdtemp
        def record(*args, **kwargs):
            path = mkdtemp(*args, **kwargs)
            paths.append(Path(path))
            return path
        with patch("beancount_budget_tool.v6.simple_config.mkdtemp", side_effect=record), patch(
                "beancount_budget_tool.v6.monthly_report.audit_month_workflow", side_effect=BudgetError("TEST_ERROR", "failure")):
            with self.assertRaises(BudgetError):
                audit_simple(self.config)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_failed_save_preserves_new_approved_receipt_for_recovery(self):
        browser = SimpleReportBrowser(self.config)
        browser.refresh()
        runtime = browser.runtime
        directory = Path(runtime.temporary.name)
        try:
            with patch("beancount_budget_tool.v6.simple_tui.save_snapshot", side_effect=OSError("read-only state directory")):
                with self.assertRaises(BudgetError) as caught:
                    browser.show_saved_month({"config": str(runtime.config)})
            self.assertEqual(caught.exception.code, "SIMPLE_STATE_SAVE_FAILED")
            browser.close()
            self.assertTrue(directory.exists())
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_corrupt_state_is_not_reset_and_cache_is_not_deleted(self):
        path = state_file(self.config)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"broken state")
        cache = path.parent / "runtime" / "keep"
        cache.mkdir(parents=True)
        (cache / "input.yaml").write_text("keep: true", encoding="utf-8")
        with self.assertRaises(BudgetError):
            clean(self.config)
        self.assertEqual(path.read_bytes(), b"broken state")
        self.assertTrue(cache.exists())

    def test_advanced_export_and_reimport_need_only_one_persistent_state_file(self):
        output = self.base / "advanced"
        exported = export(self.config, output)
        save_snapshot(self.config, Path(exported["config"]))
        self.assertEqual(audit_simple(self.config)["status"], "CERTIFIED_SCOPED")
        self.assertEqual([p.name for p in state_dir(self.config).iterdir()], ["reviews.json.gz"])

    def test_previous_approved_values_can_be_recovered_from_compact_history(self):
        first = export(self.config, self.base / "first")
        save_snapshot(self.config, Path(first["config"]))
        before = self.data()
        second = export(self.config, self.base / "second")
        path = Path(second["config"]).parent / "completeness.yaml"
        statement = yaml.safe_load(path.read_text(encoding="utf-8"))
        statement["reason"] = "new explicit reason"
        _write(path, statement)
        save_snapshot(self.config, Path(second["config"]))
        after = self.data()
        change = after["history"][-1]
        self.assertEqual(restore_previous(after["shared"], change["undo_shared"]), before["shared"])
        self.assertEqual(restore_previous(after["monthly"], change["undo_monthly"]), before["monthly"])

    def test_cleanup_refuses_to_follow_internal_symlinks(self):
        runtime = state_dir(self.config) / "runtime"
        runtime.mkdir(parents=True)
        outside = self.base / "outside.txt"
        outside.write_text("must keep", encoding="utf-8")
        try:
            (runtime / "link").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(BudgetError):
            clean(self.config)
        self.assertTrue((runtime / "link").is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "must keep")

    def test_cleanup_refuses_a_symlink_as_the_state_directory(self):
        outside = self.base / "outside-state"
        (outside / "runtime").mkdir(parents=True)
        sentinel = outside / "runtime" / "input.yaml"
        sentinel.write_text("keep: true", encoding="utf-8")
        root = state_dir(self.config)
        root.parent.mkdir()
        try:
            root.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(BudgetError):
            clean(self.config)
        self.assertTrue(sentinel.exists())


if __name__ == "__main__":
    unittest.main()

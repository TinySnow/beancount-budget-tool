"""A shared report load stays frozen and rejects source changes before output."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import date
import os
from unittest.mock import patch

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool import financial
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, frozen_financial, load_financial, reference_financial, shared_audit
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.facts import FactIndex, frozen_fact_indices, reference_fact_index
from beancount_budget_tool.v6.opening import audit_opening
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.source import scan_v6_source


class FrozenFinancialTests(unittest.TestCase):
    def test_nested_same_scope_reuses_load_and_index_but_different_cutoff_does_not(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text('2023-07-01 open Assets:Cash\n2023-08-01 open Assets:Future\n', encoding="utf-8")
            with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load:
                with booked_through(date(2023, 7, 31)), frozen_financial(root) as outer, frozen_fact_indices():
                    index = FactIndex(outer)
                    with frozen_financial(root) as inner, frozen_fact_indices():
                        self.assertIs(inner, outer)
                        self.assertIs(FactIndex(inner), index)
                        self.assertIs(reference_fact_index(inner), reference_fact_index(outer))
                    self.assertIs(FactIndex(outer), index)
                    self.assertEqual(load.call_count, 1)
                    with booked_through(date(2023, 8, 31)), frozen_financial(root) as later:
                        self.assertIsNot(later, outer)
                        self.assertEqual(len(later.entries), 2)
                    self.assertIs(load_financial(root), outer)
                    self.assertEqual(len(outer.entries), 1)
                self.assertEqual(load.call_count, 4)  # One enter/exit verification per distinct scope.

    def test_nested_different_root_does_not_reuse_the_outer_input(self):
        with TemporaryDirectory() as folder:
            root, other = (Path(folder) / name for name in ("first.bean", "second.bean"))
            root.write_text('2023-07-01 open Assets:First\n', encoding="utf-8")
            other.write_text('2023-07-01 open Assets:Second\n', encoding="utf-8")
            with frozen_financial(root) as first, frozen_fact_indices():
                first_index = FactIndex(first)
                with frozen_financial(other) as second, frozen_fact_indices():
                    self.assertEqual(load_financial(other).root, other.resolve())
                    self.assertIsNot(FactIndex(second), first_index)
                self.assertIs(load_financial(root), first)
                self.assertIs(FactIndex(first), first_index)

    def test_outer_verification_still_rejects_source_edits_in_reused_inner_scope(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text('2023-07-01 open Assets:Cash\n', encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                with frozen_financial(root):
                    with frozen_financial(root):
                        root.write_text('2023-07-01 open Assets:Bank\n', encoding="utf-8")
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")

    def test_complete_reference_index_is_shared_only_within_one_frozen_report(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text('2023-07-01 open Assets:Cash\n'
                              '2023-07-01 open Income:Salary\n'
                              '2023-08-01 * "future salary"\n'
                              '  Assets:Cash 10 CNY\n'
                              '  Income:Salary -10 CNY\n', encoding="utf-8")
            with booked_through(date(2023, 7, 31)):
                with frozen_financial(ledger), frozen_fact_indices():
                    bounded = load_financial(ledger)
                    first = reference_fact_index(bounded)
                    self.assertIs(reference_fact_index(bounded), first)
                    self.assertEqual(len(first._financial.entries), 3)
                    self.assertEqual(len(bounded.entries), 2)
            ledger.write_text(ledger.read_text(encoding="utf-8").replace('10 CNY', '20 CNY'),
                              encoding="utf-8")
            with booked_through(date(2023, 7, 31)):
                with frozen_financial(ledger), frozen_fact_indices():
                    second = reference_fact_index(load_financial(ledger))
                    self.assertIsNot(first, second)
                    self.assertNotEqual(first._financial.digest, second._financial.digest)

    def test_historical_scope_ignores_future_booked_entries_but_keeps_source(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text('2023-07-01 open Assets:Cash\n'
                              '2023-08-01 open Assets:Future\n', encoding="utf-8")
            complete = load_financial(ledger)
            with booked_through(date(2023, 7, 31)):
                with frozen_financial(ledger) as bounded:
                    self.assertEqual(len(bounded.entries), 1)
                    self.assertEqual(bounded.digest, complete.digest)
                    self.assertIs(load_financial(ledger), bounded)
                    reference = reference_financial(bounded)
                    self.assertEqual(reference.entries, complete.entries)
                    self.assertEqual(reference.digest, bounded.digest)
                    self.assertEqual(len(load_financial(ledger).entries), 1)
            self.assertEqual(len(load_financial(ledger).entries), 2)

    def test_loader_does_not_consult_existing_pickle_cache(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text('2026-09-01 open Assets:Bank\n', encoding="utf-8")
            with patch("beancount_budget_tool.financial.loader.load_file",
                       side_effect=AssertionError("cached loader used")):
                self.assertEqual(len(load_financial(ledger).entries), 1)

    def test_shared_source_and_end_of_report_validation(self):
        with TemporaryDirectory() as folder:
            ledger = Path(folder) / "main.bean"
            ledger.write_text('2026-09-01 open Assets:Bank\n', encoding="utf-8")
            with frozen_financial(ledger) as first:
                self.assertIs(load_financial(ledger), first)
                self.assertIs(load_financial(ledger), first)
                with frozen_fact_indices():
                    self.assertIs(FactIndex(first), FactIndex(first))
                self.assertIsNot(FactIndex(first), FactIndex(first))
            with self.assertRaises(BudgetError) as caught:
                with frozen_financial(ledger) as frozen:
                    ledger.write_text(ledger.read_text(encoding="utf-8") +
                                      '2026-10-01 open Expenses:Food\n', encoding="utf-8")
                    self.assertIs(load_financial(ledger), frozen)
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")
            self.assertNotEqual(load_financial(ledger).digest, first.digest)


class SharedBaseAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root, self.accounts, self.ownership = (self.base / name for name in (
            "main.bean", "accounts.yaml", "ownership.yaml"))
        self.cutover = date(2023, 7, 1)
        self.root.write_text('option "operating_currency" "CNY"\n'
                             '2023-07-01 open Assets:Cash\n2023-07-01 open Income:Salary\n'
                             '2023-07-02 * "salary"\n  Assets:Cash 100 CNY\n  Income:Salary -100 CNY\n', encoding="utf-8")
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": self.cutover.isoformat(), "report_currency": "CNY",
            "accounts": [{"account": "Assets:Cash", "semantics": "H", "balance_semantics": "real_inventory",
                          "payment_scope": {"CNY": "payable"}, "reason": "virtual"}],
        }), encoding="utf-8")
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": self.cutover.isoformat(), "ledger_snapshot": load_financial(self.root).digest,
            "loader_version": BEANCOUNT_VERSION, "claims": [],
            "owners": [{"id": "free", "name": "自由资金", "aliases": []}],
            "defaults": {"income_owner_id": "free", "expense_owner_id": "free"},
        }, allow_unicode=True), encoding="utf-8")

    def test_base_dependencies_run_once_and_results_are_not_shared_mutable_objects(self):
        with frozen_financial(self.root), frozen_fact_indices(), patch(
                "beancount_budget_tool.v6.accounts.FactIndex", wraps=FactIndex) as indices, patch(
                "beancount_budget_tool.v6.ownership.scan_v6_source", wraps=scan_v6_source) as scans:
            first = audit_accounts(self.root, self.cutover, self.accounts)
            first["accounts"].clear()
            owned = audit_ownership(self.root, self.cutover, self.accounts, self.ownership)
            owned["ownership"][0]["slices"][0]["owner_id"] = "caller annotation"
            again = audit_ownership(root=self.root, cutover=self.cutover,
                                    accounts=self.accounts, declarations=self.ownership, opening=None)
            audit_opening(self.root, self.cutover, self.accounts)
            self.assertEqual(len(audit_accounts(self.root, self.cutover, self.accounts)["accounts"]), 1)
            self.assertEqual(again["ownership"][0]["slices"][0]["owner_id"], "free")
            self.assertEqual(indices.call_count, 1)
            self.assertEqual(scans.call_count, 1)

    def test_same_size_same_timestamp_config_edit_invalidates_dependent_audits(self):
        with frozen_financial(self.root), frozen_fact_indices():
            before = audit_ownership(self.root, self.cutover, self.accounts, self.ownership)
            stamp = self.accounts.stat()
            text = self.accounts.read_text(encoding="utf-8")
            self.accounts.write_text(text.replace("payable", "pending"), encoding="utf-8")
            os.utime(self.accounts, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            after = audit_ownership(self.root, self.cutover, self.accounts, self.ownership)
            self.assertEqual(before["pending_payment_scope"], [])
            self.assertEqual(len(after["pending_payment_scope"]), 1)
            self.assertEqual(after["ledger_snapshot"], before["ledger_snapshot"])

    def test_failed_audits_are_retried_and_removed_files_keep_source_diagnostics(self):
        with frozen_financial(self.root), frozen_fact_indices():
            audit_accounts(self.root, self.cutover, self.accounts)
            self.accounts.write_text("schema: 6\nschema: 6\n", encoding="utf-8")
            for _ in range(2):
                with self.assertRaises(BudgetError) as caught:
                    audit_accounts(self.root, self.cutover, self.accounts)
                self.assertEqual(caught.exception.code, "YAML_DUPLICATE_KEY")
                self.assertEqual(caught.exception.source, f"{self.accounts.resolve()}:2")
            self.accounts.unlink()
            with self.assertRaises(BudgetError) as caught:
                audit_accounts(self.root, self.cutover, self.accounts)
            self.assertEqual(caught.exception.code, "YAML_INVALID")

    def test_a_reload_reaudits_and_standalone_calls_do_not_cache(self):
        with patch("beancount_budget_tool.v6.accounts.FactIndex", wraps=FactIndex) as indices:
            for _ in range(2):
                with frozen_financial(self.root), frozen_fact_indices():
                    audit_accounts(self.root, self.cutover, self.accounts)
                    audit_accounts(self.root, self.cutover, self.accounts)
            self.assertEqual(indices.call_count, 2)
            audit_accounts(self.root, self.cutover, self.accounts)
            audit_accounts(self.root, self.cutover, self.accounts)
            self.assertEqual(indices.call_count, 4)

    def test_audit_rejects_config_edits_during_computation(self):
        @shared_audit
        def changing_audit(root, declarations):
            declarations.write_text("changed: true\n", encoding="utf-8")
            return {"should_not_return": True}

        with frozen_financial(self.root):
            with self.assertRaises(BudgetError) as caught:
                changing_audit(self.root, self.accounts)
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")
        self.assertEqual(caught.exception.source, str(self.accounts.resolve()))


if __name__ == "__main__":
    unittest.main()

"""A shared report load stays frozen and rejects source changes before output."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import date
import os
from unittest.mock import patch

from beancount import __version__ as BEANCOUNT_VERSION
from beancount.core import data
from beancount_budget_tool.refs import ReferenceIndex
import yaml

from beancount_budget_tool import financial
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, frozen_financial, financial_session, load_financial, reference_financial, shared_audit
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.facts import FactIndex, frozen_fact_indices, reference_fact_index
from beancount_budget_tool.v6.opening import audit_opening
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.source import scan_v6_source


class FrozenFinancialTests(unittest.TestCase):
    def test_sequential_stages_share_input_and_index_until_the_report_returns(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text('2023-07-01 open Assets:Cash\n', encoding="utf-8")
            with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load:
                with financial_session(), frozen_fact_indices():
                    with frozen_financial(root) as first:
                        index = FactIndex(first)
                    with financial_session(), frozen_financial(root) as second:
                        self.assertIs(first, second)
                        self.assertIs(FactIndex(second), index)
                    self.assertIs(load_financial(root), first)
                    self.assertEqual(load.call_count, 1)
                self.assertEqual(load.call_count, 1)  # Final verification checks bytes, not booking again.
                with financial_session(), frozen_financial(root) as fresh:
                    self.assertIsNot(fresh, first)
                self.assertEqual(load.call_count, 2)

    def test_multiple_cutoffs_share_capture_and_raw_identities_without_future_leakage(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text('2023-07-01 open Assets:Cash\n2023-07-01 open Income:Salary\n'
                            '2023-07-02 * "July"\n  Assets:Cash 10 CNY\n  Income:Salary -10 CNY\n'
                            '2023-08-02 * "August"\n  Assets:Cash 20 CNY\n  Income:Salary -20 CNY\n', encoding="utf-8")
            with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load, \
                 patch("beancount_budget_tool.refs.ReferenceIndex", wraps=ReferenceIndex) as raw:
                with financial_session(), frozen_fact_indices():
                    with booked_through(date(2023, 7, 31)), frozen_financial(root) as july:
                        july_index = FactIndex(july)
                        self.assertEqual(raw.call_count, 1)  # Only the July view is indexed so far.
                        complete = reference_financial(july)
                        august_transaction = next(entry for entry in complete.entries if isinstance(entry, data.Transaction)
                                                  and entry.date.month == 8)
                        full_index = reference_fact_index(july)
                        future_ref = full_index.for_posting(august_transaction.postings[0])
                        with self.assertRaises(BudgetError) as error:
                            july_index.for_posting(august_transaction.postings[0])
                        self.assertEqual(error.exception.code, "SOURCE_REF_INVALID")
                        self.assertEqual(len(july_index.posting_refs), 2)
                    with booked_through(date(2023, 8, 31)), frozen_financial(root) as august:
                        august_index = FactIndex(august)
                        self.assertIs(reference_financial(august), complete)
                        self.assertIs(reference_fact_index(august), full_index)
                        self.assertEqual(august_index.for_posting(august_transaction.postings[0]), future_ref)
                        self.assertEqual(len(august_index.posting_refs), 4)
                    self.assertEqual(load.call_count, 1)
                    self.assertEqual(raw.call_count, 2)  # July plus the explicitly requested full table.
                    with self.assertRaises(BudgetError):
                        july_index.for_posting(august_transaction.postings[0])
                self.assertEqual(load.call_count, 1)

    def test_edit_between_cutoff_views_rejects_the_whole_query_and_reload_sees_it(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            initial = ('2023-07-01 open Assets:Cash\n2023-07-01 open Income:Salary\n'
                       '2023-07-02 * "salary"\n  Assets:Cash 10 CNY\n  Income:Salary -10 CNY\n')
            extra = '2023-08-02 * "new"\n  Assets:Cash 20 CNY\n  Income:Salary -20 CNY\n'
            root.write_text(initial, encoding="utf-8")
            with self.assertRaises(BudgetError) as error:
                with financial_session():
                    with booked_through(date(2023, 7, 31)), frozen_financial(root):
                        pass
                    root.write_text(initial + extra, encoding="utf-8")
                    with booked_through(date(2023, 8, 31)), frozen_financial(root) as august:
                        self.assertEqual(sum(isinstance(e, data.Transaction) for e in august.entries), 1)
            self.assertEqual(error.exception.code, "INPUT_CHANGED")
            with financial_session(), booked_through(date(2023, 8, 31)), frozen_financial(root) as fresh:
                self.assertEqual(sum(isinstance(e, data.Transaction) for e in fresh.entries), 2)

    def test_cutoff_audit_results_stay_separate_when_the_full_capture_is_shared(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text('2023-07-01 open Assets:Cash\n2023-08-01 open Assets:Future\n', encoding="utf-8")
            calls = []
            @shared_audit
            def count_entries(root):
                calls.append(len(load_financial(root).entries))
                return {"count": calls[-1]}
            with financial_session():
                for cutoff, expected in ((date(2023, 7, 31), 1), (date(2023, 8, 31), 2), (date(2023, 7, 31), 1)):
                    with booked_through(cutoff), frozen_financial(root):
                        self.assertEqual(count_entries(root)["count"], expected)
            self.assertEqual(calls, [1, 2])

    def test_session_separates_roots_and_report_cutoffs(self):
        with TemporaryDirectory() as folder:
            root, other = (Path(folder) / name for name in ("main.bean", "other.bean"))
            root.write_text('2023-07-01 open Assets:Cash\n2023-08-01 open Assets:Future\n', encoding="utf-8")
            other.write_text('2023-07-01 open Assets:Other\n', encoding="utf-8")
            with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load, \
                 financial_session(), frozen_fact_indices():
                with booked_through(date(2023, 7, 31)), frozen_financial(root) as july:
                    self.assertEqual(len(july.entries), 1)
                with booked_through(date(2023, 8, 31)), frozen_financial(root) as august:
                    self.assertEqual(len(august.entries), 2)
                    self.assertIsNot(july, august)
                    self.assertIsNot(FactIndex(july), FactIndex(august))
                with booked_through(date(2023, 7, 31)), frozen_financial(other) as separate:
                    self.assertEqual(separate.root, other.resolve())
                    self.assertNotEqual(separate.digest, july.digest)
                with booked_through(date(2023, 7, 31)), frozen_financial(root) as repeated:
                    self.assertIs(repeated, july)
            self.assertEqual(load.call_count, 2)  # Two independent roots, two cutoffs for the first.

    def test_session_rejects_same_size_source_edit_after_an_earlier_stage(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text('2023-07-01 open Assets:Cash\n', encoding="utf-8")
            original = root.stat()
            with self.assertRaises(BudgetError) as caught:
                with financial_session():
                    with frozen_financial(root) as first:
                        pass
                    root.write_text('2023-07-01 open Assets:Bank\n', encoding="utf-8")
                    os.utime(root, ns=(original.st_atime_ns, original.st_mtime_ns))
                    with frozen_financial(root) as second:
                        self.assertIs(second, first)
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")
            self.assertEqual(caught.exception.source, str(root.resolve()))
            with financial_session():
                self.assertEqual(load_financial(root).entries[0].account, "Assets:Bank")

    def test_session_final_verification_detects_new_glob_include_members(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            parts = Path(folder) / "parts"
            parts.mkdir()
            root.write_text('include "parts/*.bean"\n', encoding="utf-8")
            (parts / "one.bean").write_text('2023-07-01 open Assets:Cash\n', encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                with financial_session():
                    first = load_financial(root)
                    (parts / "two.bean").write_text('2023-07-01 open Income:Salary\n', encoding="utf-8")
                    self.assertIs(load_financial(root), first)
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")
            self.assertEqual(len(load_financial(root).entries), 2)

    def test_failed_report_restores_context_and_keeps_the_original_diagnostic(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text('2023-07-01 open Assets:Cash\n', encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                with financial_session():
                    first = load_financial(root)
                    raise BudgetError("EXAMPLE", "original diagnostic", "main.bean:1")
            self.assertEqual((caught.exception.code, caught.exception.source), ("EXAMPLE", "main.bean:1"))
            with financial_session():
                self.assertIsNot(load_financial(root), first)

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
                self.assertEqual(load.call_count, 2)  # One capture per distinct scope, byte checks at exit.

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

    def test_classification_and_responsibility_replay_are_shared_without_mutable_results(self):
        for module, audit in (("classification", audit_classification), ("obligations", audit_simple_obligations)):
            with self.subTest(module=module), financial_session(), frozen_fact_indices():
                with patch("beancount_budget_tool.v6." + module + ".FactIndex", wraps=FactIndex) as index:
                    with frozen_financial(self.root):
                        first = audit(self.root, self.cutover)
                    expected = first["cutover"]
                    first["cutover"] = "caller modification"
                    with frozen_financial(self.root):
                        second = audit(self.root, self.cutover)
                    self.assertEqual(second["cutover"], expected)
                    self.assertEqual(index.call_count, 1)

    def test_shared_classification_rechecks_its_declaration_and_error_location(self):
        declared = self.base / "categories.yaml"
        document = {"schema": 6, "cutover": self.cutover.isoformat(), "loader_version": BEANCOUNT_VERSION,
                    "categories": [{"id": "free", "name": "自由资金", "parent": None, "aliases": []}],
                    "classifications": [], "defaults": {"income_category_id": "free", "expense_category_id": "free"}}
        declared.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        with financial_session():
            with frozen_financial(self.root):
                first = audit_classification(self.root, self.cutover, declared)
            self.assertEqual(first["review_state"], "complete")
            declared.write_text("schema: 6\nschema: 6\n", encoding="utf-8")
            with frozen_financial(self.root):
                with self.assertRaises(BudgetError) as error:
                    audit_classification(self.root, self.cutover, declared)
            self.assertEqual(error.exception.code, "YAML_DUPLICATE_KEY")
            self.assertEqual(error.exception.source, str(declared.resolve()) + ":2")

    def test_sequential_stages_reuse_audits_but_recheck_changed_sidefiles(self):
        with financial_session(), frozen_fact_indices():
            with patch("beancount_budget_tool.v6.accounts.FactIndex", wraps=FactIndex) as indices:
                with frozen_financial(self.root):
                    first = audit_accounts(self.root, self.cutover, self.accounts)
                first["accounts"].clear()
                with frozen_financial(self.root):
                    second = audit_accounts(self.root, self.cutover, self.accounts)
                self.assertTrue(second["accounts"])
                self.assertEqual(indices.call_count, 1)
                document = yaml.safe_load(self.accounts.read_text(encoding="utf-8"))
                document["accounts"][0]["payment_scope"]["CNY"] = "excluded"
                self.accounts.write_text(yaml.safe_dump(document), encoding="utf-8")
                with frozen_financial(self.root):
                    changed = audit_accounts(self.root, self.cutover, self.accounts)
                self.assertEqual(changed["accounts"][0]["payment_scope"]["CNY"], "excluded")
                self.assertEqual(indices.call_count, 2)

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

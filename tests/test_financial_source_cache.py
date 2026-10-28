"""Browser parse reuse must retain the source and result isolation contracts."""

from datetime import date
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from beancount.core import data

from beancount_budget_tool import financial
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import (
    FinancialSourceCache, booked_through, financial_session, load_financial,
)
from beancount_budget_tool.v6.facts import FactIndex


class FinancialSourceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.root = self.folder / "main.bean"
        self.part = self.folder / "part-one.bean"
        self.root.write_text('include "part-*.bean"\n', encoding="utf-8")
        self.part.write_text('2026-01-01 open Assets:Cash\n', encoding="utf-8")
        self.cache = FinancialSourceCache()

    def load(self):
        with financial_session(source_cache=self.cache):
            return load_financial(self.root)

    def test_reuse_returns_fresh_metadata_options_and_audit_caches(self):
        with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load:
            first = self.load()
            first.entries[0].meta["caller"] = []
            first.options["operating_currency"].append("BAD")
            first.sources.clear()
            first._audit_results["caller"] = []
            second = self.load()
            self.assertEqual(load.call_count, 1)
            self.assertIsNot(first, second)
            self.assertNotIn("caller", second.entries[0].meta)
            self.assertNotIn("BAD", second.options["operating_currency"])
            self.assertEqual(second._audit_results, {})
            self.assertEqual(len(second.sources), 2)

    def test_same_size_same_mtime_edit_is_reparsed_and_changes_digest(self):
        before = self.load()
        stamp = self.part.stat()
        self.part.write_text('2026-01-01 open Assets:Bank\n', encoding="utf-8")
        os.utime(self.part, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        after = self.load()
        self.assertEqual(after.entries[0].account, "Assets:Bank")
        self.assertNotEqual(before.digest, after.digest)

    def test_added_and_removed_glob_members_are_reparsed(self):
        before = self.load()
        extra = self.folder / "part-two.bean"
        extra.write_text('2026-02-01 open Assets:Bank\n', encoding="utf-8")
        added = self.load()
        self.assertEqual(len(added.entries), 2)
        self.assertNotEqual(before.digest, added.digest)
        extra.unlink()
        removed = self.load()
        self.assertEqual(len(removed.entries), 1)
        self.assertEqual(before.digest, removed.digest)
        self.part.unlink()
        with self.assertRaises(BudgetError) as caught:
            self.load()
        self.assertEqual(caught.exception.code, "LOADER_ERROR")

    def test_version_changes_and_different_roots_do_not_hit(self):
        self.load()
        with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load:
            with patch.object(financial, "BEANCOUNT_VERSION", "different-version"):
                self.load()
            self.load()
            other = self.folder / "other.bean"
            other.write_text('2026-03-01 open Assets:Other\n', encoding="utf-8")
            with financial_session(source_cache=self.cache):
                self.assertEqual(load_financial(other).entries[0].account, "Assets:Other")
            self.assertEqual(load.call_count, 3)

    def test_cached_parse_is_rebounded_and_verified_before_publish(self):
        self.part.write_text('2026-01-01 open Assets:Cash\n2026-02-01 open Assets:Bank\n', encoding="utf-8")
        complete = self.load()
        with booked_through(date(2026, 1, 31)), financial_session(source_cache=self.cache):
            bounded = load_financial(self.root)
            self.assertEqual(len(bounded.entries), 1)
            self.assertEqual(bounded.reference_entries, complete.entries)
        with self.assertRaises(BudgetError) as caught:
            with financial_session(source_cache=self.cache):
                load_financial(self.root)
                self.part.write_text('2026-01-01 open Assets:New\n', encoding="utf-8")
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")
        self.assertEqual(self.load().entries[0].account, "Assets:New")

    def test_reuse_preserves_generated_pad_posting_identities(self):
        self.root.write_text('2026-01-01 open Assets:Cash CNY\n'
                             '2026-01-01 open Equity:Opening CNY\n'
                             '2026-01-02 pad Assets:Cash Equity:Opening\n'
                             '2026-01-03 balance Assets:Cash 100 CNY\n', encoding="utf-8")
        def references(fin):
            facts = FactIndex(fin)
            return [facts.for_posting(posting).as_dict() for entry in fin.entries
                    if isinstance(entry, data.Transaction) for posting in entry.postings]
        before = references(self.load())
        after = references(self.load())
        self.assertEqual(len(before), 2)
        self.assertEqual(before, after)
        self.assertTrue(all(ref["generated_kind"] == "pad" for ref in after))

    def test_default_sessions_still_parse_fresh_and_do_not_leak_reuse(self):
        self.load()
        with patch.object(financial, "_load_financial_uncached", wraps=financial._load_financial_uncached) as load:
            for _ in range(2):
                with financial_session():
                    load_financial(self.root)
            self.assertEqual(load.call_count, 2)

    @unittest.skipIf(os.name == "nt", "Symlink creation needs extra Windows privileges")
    def test_retargeted_source_alias_does_not_reuse_the_previous_parse(self):
        target = self.folder / "target-one.bean"
        self.part.rename(target)
        self.part.symlink_to(target)
        before = self.load()
        replacement = self.folder / "target-two.bean"
        replacement.write_text('2026-01-01 open Assets:Bank\n', encoding="utf-8")
        self.part.unlink()
        self.part.symlink_to(replacement)
        after = self.load()
        self.assertEqual(after.entries[0].account, "Assets:Bank")
        self.assertNotEqual(before.digest, after.digest)


if __name__ == "__main__":
    unittest.main()

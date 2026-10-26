"""Lexical reuse is limited to frozen source content, never a later review."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch

from beancount.core import data
from beancount.parser import parser

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.exact import Fraction
from beancount_budget_tool.refs import ReferenceIndex
from beancount_budget_tool.financial import (
    FinancialInput, SourceFile, frozen_financial, load_financial,
)


LEDGER = '''2026-09-01 open Assets:Bank
2026-09-01 open Assets:Dollars
2026-09-02 * "exchange"
  Assets:Dollars 2 USD @@ 14 CNY ; @ 99 CNY is only a comment
  Assets:Bank -14 CNY
'''


class FinancialLexicalCacheTests(unittest.TestCase):
    def test_canonical_reference_filenames_do_not_repeat_filesystem_resolution(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(LEDGER, encoding="utf-8")
            fin = load_financial(root)
            with patch("beancount_budget_tool.refs.Path", wraps=Path) as paths:
                index = ReferenceIndex(fin)
            paths.assert_not_called()
            postings = next(entry for entry in fin.entries if isinstance(entry, data.Transaction)).postings
            self.assertEqual(len(index.by_posting), len(postings))

    def test_noncanonical_reference_filename_resolves_once_and_keeps_the_same_refs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "main.bean"
            root.write_text(LEDGER, encoding="utf-8")
            fin = load_financial(root)
            expected = ReferenceIndex(fin)
            from copy import deepcopy
            entries = deepcopy(fin.entries)
            alias = str(root.parent) + "/./main.bean"
            for entry in entries:
                if isinstance(entry, data.Transaction):
                    entry.meta["filename"] = alias
                    for posting in entry.postings:
                        posting.meta["filename"] = alias
            aliased = replace(fin, entries=entries)
            with patch("beancount_budget_tool.refs.Path", wraps=Path) as paths:
                actual = ReferenceIndex(aliased)
            self.assertEqual(paths.call_count, 1)
            self.assertEqual(set(actual.by_ref), set(expected.by_ref))
            self.assertEqual(set(actual.by_txref), set(expected.by_txref))

    def test_raw_positions_are_parsed_once_for_bounded_and_complete_views(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(LEDGER.replace("exchange", "换汇").replace("\n", "\r\n"), encoding="utf-8")
            with patch("beancount_budget_tool.financial.parser.parse_string", wraps=parser.parse_string) as parse:
                fin = load_financial(root)
                ReferenceIndex(fin)
                ReferenceIndex(replace(fin, entries=fin.entries[:2], reference_entries=fin.entries))
            self.assertEqual(parse.call_count, len(fin.sources))
            raw, errors, _ = parser.parse_file(str(root))
            self.assertEqual(errors, [])
            expected = {entry.meta["lineno"]: tuple(posting.meta["lineno"] for posting in entry.postings)
                        for entry in raw if isinstance(entry, data.Transaction)}
            self.assertEqual(fin.sources[root.resolve()]._posting_lines, expected)

    def test_source_change_after_cached_raw_positions_still_blocks_the_report(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(LEDGER, encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                with frozen_financial(root) as fin:
                    original = set(ReferenceIndex(fin).by_ref)
                    root.write_text("; changed during the report\n" + LEDGER, encoding="utf-8")
                    self.assertEqual(set(ReferenceIndex(fin).by_ref), original)
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")

    def test_raw_position_cache_stays_frozen_and_a_reload_observes_new_lines(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(LEDGER, encoding="utf-8")
            first = load_financial(root)
            old_positions = first.sources[root.resolve()]._posting_lines
            expected = set(ReferenceIndex(first).by_ref)
            root.write_text("; inserted outside the transaction\n" + LEDGER, encoding="utf-8")
            second = load_financial(root)
            self.assertNotEqual(old_positions, second.sources[root.resolve()]._posting_lines)
            self.assertIs(first.sources[root.resolve()]._posting_lines, old_positions)
            self.assertEqual(set(ReferenceIndex(second).by_ref), expected)

    def test_repeated_source_reads_reuse_lines_and_canonical_filename(self):
        path = Path("/frozen/main.bean")
        source = SourceFile(path, LEDGER, "digest")
        fin = FinancialInput(path, (), {}, {path: source}, "digest", "test")
        with patch.object(Path, "resolve", side_effect=AssertionError("live path lookup")):
            self.assertIn("exchange", fin.transaction_text(str(path), 3))
            self.assertIn("Assets:Dollars", fin.line(str(path), 4))
            self.assertIs(source.lines, source.lines)

    def test_price_cache_validates_current_metadata_and_keeps_line_identity(self):
        path = Path("/frozen/main.bean")
        source = SourceFile(path, LEDGER, "digest")
        fin = FinancialInput(path, (), {}, {path: source}, "digest", "test")
        posting = SimpleNamespace(meta={"filename": str(path), "lineno": 4})
        with patch.object(FinancialInput, "line", wraps=fin.line) as read_line:
            self.assertEqual(fin.raw_price(posting), ("@@", Fraction(14), "CNY"))
            self.assertEqual(fin.raw_price(posting), ("@@", Fraction(14), "CNY"))
            self.assertEqual(read_line.call_count, 1)
            posting.meta["lineno"] = 5
            self.assertEqual(fin.raw_price(posting), ("none", Fraction(0), ""))
            posting.meta.pop("filename")
            with self.assertRaises(BudgetError) as caught:
                fin.raw_price(posting)
            self.assertEqual(caught.exception.code, "SOURCE_MISSING")

    def test_fresh_load_observes_same_length_same_timestamp_price_edit(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "main.bean"
            path.write_text(LEDGER, encoding="utf-8")
            first = load_financial(path)
            first_posting = next(e for e in first.entries if isinstance(e, data.Transaction)).postings[0]
            self.assertEqual(first.raw_price(first_posting), ("@@", Fraction(14), "CNY"))
            stat = path.stat()
            path.write_text(LEDGER.replace("14 CNY", "16 CNY"), encoding="utf-8")
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            second = load_financial(path)
            second_posting = next(e for e in second.entries if isinstance(e, data.Transaction)).postings[0]
            self.assertEqual(second.raw_price(second_posting), ("@@", Fraction(16), "CNY"))
            self.assertNotEqual(first.digest, second.digest)
            self.assertEqual(first.raw_price(first_posting), ("@@", Fraction(14), "CNY"))

    def test_include_edit_after_cached_price_still_rejects_report(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            included = Path(folder) / "exchange.bean"
            root.write_text('include "exchange.bean"\n', encoding="utf-8")
            included.write_text(LEDGER, encoding="utf-8")
            with self.assertRaises(BudgetError) as caught:
                with frozen_financial(root) as fin:
                    posting = next(e for e in fin.entries if isinstance(e, data.Transaction)).postings[0]
                    fin.raw_price(posting)
                    included.write_text(LEDGER.replace("14 CNY", "16 CNY"), encoding="utf-8")
                    self.assertEqual(fin.raw_price(posting), ("@@", Fraction(14), "CNY"))
            self.assertEqual(caught.exception.code, "INPUT_CHANGED")

    def test_missing_lines_and_price_conflicts_keep_source_locations(self):
        path = Path("/frozen/main.bean")
        source = SourceFile(path, "  Assets:Cash 1 USD @ 7 CNY @@ 7 CNY\n", "digest")
        fin = FinancialInput(path, (), {}, {path: source}, "digest", "test")
        for _ in range(2):
            with self.assertRaises(BudgetError) as conflict:
                fin.raw_price(SimpleNamespace(meta={"filename": str(path), "lineno": 1}))
            self.assertEqual(conflict.exception.code, "PRICE_SOURCE_CONFLICT")
            self.assertIn(f"{path}:1", str(conflict.exception))
            with self.assertRaises(BudgetError) as missing:
                fin.line(str(path), 2)
            self.assertEqual(missing.exception.code, "SOURCE_MISSING")
            self.assertIn(f"{path}:2", str(missing.exception))
        with self.assertRaises(BudgetError) as missing_file:
            fin.transaction_text("/frozen/missing.bean", 1)
        self.assertEqual(missing_file.exception.code, "SOURCE_MISSING")

    def test_replaced_snapshot_does_not_inherit_lexical_price_cache(self):
        path = Path("/frozen/main.bean")
        fin = FinancialInput(path, (), {}, {path: SourceFile(path, LEDGER, "first")}, "first", "test")
        posting = SimpleNamespace(meta={"filename": str(path), "lineno": 4})
        fin.raw_price(posting)
        changed = replace(fin, sources={path: SourceFile(path, LEDGER.replace("14 CNY", "16 CNY"), "second")}, digest="second")
        self.assertEqual(changed.raw_price(posting), ("@@", Fraction(16), "CNY"))
        self.assertEqual(fin.raw_price(posting), ("@@", Fraction(14), "CNY"))


if __name__ == "__main__":
    unittest.main()

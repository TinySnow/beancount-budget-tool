"""Lexical reuse is limited to frozen source content, never a later review."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch

from beancount.core import data

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.exact import Fraction
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

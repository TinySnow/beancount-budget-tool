"""One official booking remains bound to the captured bytes and include graph."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch

from beancount.core import data

from beancount_budget_tool import financial
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import financial_session, load_financial

HEADER = ('option "operating_currency" "CNY"\n'
          '2026-01-01 open Assets:Cash\n2026-01-01 open Income:Salary\n')
TX = '2026-01-02 * "income"\n  Assets:Cash 10 CNY\n  Income:Salary -10 CNY\n'


class FinancialCaptureTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.root = self.folder / "main.bean"
        self.parts = self.folder / "parts"
        self.parts.mkdir()
        self.child = self.parts / "one.bean"
        self.root.write_text(HEADER + 'include "parts/*.bean"\n', encoding="utf-8")
        self.child.write_text(TX, encoding="utf-8")

    def assert_input_changed(self, action):
        with self.assertRaises(BudgetError) as caught:
            action()
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")
        self.assertEqual(caught.exception.source, str(self.root))

    def test_one_official_booking_per_report_and_raw_positions_are_reused(self):
        with patch.object(financial.loader.booking, "book", wraps=financial.loader.booking.book) as book, \
             patch.object(financial.parser, "parse_string", wraps=financial.parser.parse_string) as syntax, \
             patch.object(financial, "_verify_financial", wraps=financial._verify_financial) as verify:
            with financial_session():
                fin = load_financial(self.root)
                for source in fin.sources.values():
                    source._posting_lines
                self.assertIs(load_financial(self.root), fin)
            self.assertEqual(book.call_count, 1)
            self.assertEqual(syntax.call_count, 2)
            self.assertEqual(verify.call_count, 2)  # After booking and before returning the report.

    def test_same_size_same_timestamp_edit_between_capture_and_booking_is_rejected(self):
        original = financial.book_snapshot
        stat = self.child.stat()

        def edit(*args, **kwargs):
            self.child.write_text(TX.replace("10 CNY", "20 CNY"), encoding="utf-8")
            os.utime(self.child, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return original(*args, **kwargs)

        with patch.object(financial, "book_snapshot", side_effect=edit):
            self.assert_input_changed(lambda: load_financial(self.root))

    def test_edit_after_booking_before_verification_is_rejected(self):
        original = financial.book_snapshot

        def edit(*args, **kwargs):
            result = original(*args, **kwargs)
            self.child.write_text(TX.replace("10 CNY", "20 CNY"), encoding="utf-8")
            return result

        with patch.object(financial, "book_snapshot", side_effect=edit):
            self.assert_input_changed(lambda: load_financial(self.root))

    def test_new_glob_member_during_booking_cannot_join_the_frozen_snapshot(self):
        original = financial.book_snapshot

        def add(*args, **kwargs):
            (self.parts / "two.bean").write_text('2026-01-01 open Assets:New\n', encoding="utf-8")
            return original(*args, **kwargs)

        with patch.object(financial, "book_snapshot", side_effect=add):
            self.assert_input_changed(lambda: load_financial(self.root))

    def test_deleted_glob_member_at_report_exit_is_rejected(self):
        def delete():
            with financial_session():
                load_financial(self.root)
                self.child.unlink()
        self.assert_input_changed(delete)

    def test_recursive_glob_change_is_detected_and_next_run_includes_it(self):
        self.root.write_text(HEADER + 'include "parts/**/*.bean"\n', encoding="utf-8")
        nested = self.parts / "nested"
        nested.mkdir()

        def add():
            with financial_session():
                load_financial(self.root)
                (nested / "two.bean").write_text('2026-01-01 open Assets:New\n', encoding="utf-8")
        self.assert_input_changed(add)
        self.assertEqual(len(load_financial(self.root).sources), 3)

    def test_official_syntax_finds_quoted_include_and_ignores_commented_include(self):
        renamed = self.parts / "中文 file.bean"
        self.child.rename(renamed)
        self.root.write_text(HEADER + '; include "missing.bean"\n'
                             'include "parts/中文 file.bean" ; real include\n', encoding="utf-8")
        with financial_session():
            fin = load_financial(self.root)
        self.assertEqual(set(fin.sources), {self.root, renamed})
        self.assertEqual(sum(isinstance(entry, data.Transaction) for entry in fin.entries), 1)

    def test_unmatched_and_duplicate_includes_remain_official_loader_errors(self):
        for include in ('include "missing/*.bean"\n',
                        'include "parts/one.bean"\ninclude "parts/one.bean"\n'):
            with self.subTest(include=include):
                self.root.write_text(HEADER + include, encoding="utf-8")
                with self.assertRaises(BudgetError) as caught:
                    load_financial(self.root)
                self.assertEqual(caught.exception.code, "LOADER_ERROR")

    def symlink(self, link, target):
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"File symlinks unavailable: {exc}")

    def test_relative_include_in_symlink_uses_the_loader_filename_directory(self):
        actual = self.folder / "actual"
        actual.mkdir()
        wrapper = actual / "wrapper.bean"
        wrapper.write_text('include "child.bean"\n', encoding="utf-8")
        (actual / "child.bean").write_text(TX.replace("10 CNY", "99 CNY"), encoding="utf-8")
        self.child.rename(self.parts / "child.bean")
        self.symlink(self.parts / "alias.bean", wrapper)
        self.root.write_text(HEADER + 'include "parts/alias.bean"\n', encoding="utf-8")
        with financial_session():
            fin = load_financial(self.root)
        tx = next(entry for entry in fin.entries if isinstance(entry, data.Transaction))
        self.assertEqual(str(tx.postings[0].units.number), "10")
        self.assertIn(self.parts / "child.bean", fin.sources)
        self.assertNotIn(actual / "child.bean", fin.sources)

    def test_include_symlink_retarget_with_identical_bytes_is_detected(self):
        alias = self.folder / "alias.bean"
        other = self.folder / "other.bean"
        other.write_text(TX, encoding="utf-8")
        self.symlink(alias, self.child)
        self.root.write_text(HEADER + 'include "alias.bean"\n', encoding="utf-8")

        def retarget():
            with financial_session():
                load_financial(self.root)
                alias.unlink()
                alias.symlink_to(other)
        self.assert_input_changed(retarget)

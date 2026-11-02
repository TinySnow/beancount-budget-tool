"""Exact-input reuse, invalidation and cancellation at public boundaries."""

import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool import financial
from beancount_budget_tool.recompute import RecomputeCache
from beancount_budget_tool.operation import checkpoint
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6 import simple_tui, tui
from test_v6_tui import FakeScreen


class RecomputeTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples/v6/simple', self.base, dirs_exist_ok=True)
        self.config = self.base/'config.yml'
        self.ledger = self.base/'main.bean'
        self.cache = RecomputeCache()

    def test_same_bytes_reuse_is_detached_and_parameters_are_distinct(self):
        calls = []
        def compute():
            calls.append(1)
            return {'status': 'NEEDS_REVIEW', 'report': None, 'items': [{'code': 'pending'}]}
        first = self.cache.run(self.config, ('month', '2026-09'), compute)
        first['items'].clear()
        second = self.cache.run(self.config, ('month', '2026-09'), compute)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second['items'], [{'code': 'pending'}])
        self.assertIsNone(second['report'])
        self.cache.run(self.config, ('month', '2026-10'), compute)
        self.assertEqual(len(calls), 2)

    def test_equal_size_mtime_edit_and_budget_change_invalidate(self):
        calls = []
        compute = lambda: calls.append(1) or {'ok': True}
        self.cache.run(self.config, (), compute)
        stamp = self.ledger.stat()
        old = self.ledger.read_text()
        self.assertIn('Food', old)
        self.ledger.write_text(old.replace('Food', 'Meal'))
        os.utime(self.ledger, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.cache.run(self.config, (), compute)
        budgets = self.base/'budgets.yml'
        budgets.write_text(budgets.read_text()+'\n# saved\n')
        self.cache.run(self.config, (), compute)
        self.assertEqual(len(calls), 3)

    def test_include_glob_addition_removal_and_deletion_are_not_cache_hits(self):
        self.ledger.write_text(self.ledger.read_text()+'\ninclude "part-*.bean"\n')
        first = self.base/'part-one.bean'
        first.write_text('; one\n')
        calls = []
        compute = lambda: calls.append(1) or {}
        self.cache.run(self.config, (), compute)
        second = self.base/'part-two.bean'
        second.write_text('; two\n')
        self.cache.run(self.config, (), compute)
        second.unlink()
        self.cache.run(self.config, (), compute)
        self.assertEqual(len(calls), 3)
        self.ledger.unlink()
        with self.assertRaises(BudgetError):
            self.cache.run(self.config, (), compute)

    def test_racing_save_discards_result_and_does_not_cache_failure(self):
        def race():
            self.ledger.write_text(self.ledger.read_text()+'\n; racing save\n')
            return {'stale': 190}
        with self.assertRaises(BudgetError) as caught:
            self.cache.run(self.config, (), race)
        self.assertEqual(caught.exception.code, 'INPUT_CHANGED')
        calls = []
        def fail():
            calls.append(1)
            raise BudgetError('TEST_FAILURE', 'failure')
        for _ in range(2):
            with self.assertRaises(BudgetError): self.cache.run(self.config, (), fail)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.cache._result)

    def test_browser_reuses_unchanged_result_but_saved_ambiguity_clears_cash(self):
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        with patch.object(simple_tui, 'build_runtime', wraps=simple_tui.build_runtime) as compile:
            browser.refresh()
            expected = browser.outcome
            browser.refresh()
            self.assertEqual(compile.call_count, 1)
            self.assertEqual(browser.outcome, expected)
            self.assertTrue(browser.recompute_cache.last_hit)
            self.ledger.write_text(self.ledger.read_text()+'''\n2026-09-07 * "ambiguous"
  Assets:Cash -1 CNY
  Expenses:Food 1 CNY
''')
            browser.refresh()
            self.assertEqual(compile.call_count, 2)
        self.assertEqual(browser.outcome['status'], 'NEEDS_REVIEW')
        self.assertIsNone(browser.outcome['report'])

    def test_browser_cache_hit_still_verifies_sources_at_return_and_clears_old_cash(self):
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        self.assertIsNotNone(browser.outcome['report'])
        verify = financial._verify_financial
        checks = []
        def change_at_final_verify(fin, *args, **kwargs):
            checks.append(1)
            if len(checks) == 2:
                self.ledger.write_text(self.ledger.read_text()+'\n; saved during verified hit\n')
            return verify(fin, *args, **kwargs)
        with patch.object(financial, '_verify_financial', side_effect=change_at_final_verify):
            browser.refresh()
        self.assertEqual(browser.error.code, 'INPUT_CHANGED')
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.lines, [])

    def test_busy_query_can_cancel_and_joins_worker_without_publishing(self):
        stopped = threading.Event()
        class CancelScreen(FakeScreen):
            def get_wch(self): return '\x1b'
            def timeout(self, delay): self.delays = getattr(self, 'delays', []) + [delay]
        screen = CancelScreen(size=(20, 100))
        def compute():
            try:
                while True:
                    checkpoint('核对历史来源')
                    time.sleep(0.002)
            finally:
                stopped.set()
        with self.assertRaises(BudgetError) as caught:
            tui._run_query(screen, '重算', compute)
        self.assertEqual(caught.exception.code, 'QUERY_CANCELLED')
        self.assertTrue(stopped.is_set())
        self.assertFalse(any(t.name=='budget-query' and t.is_alive() for t in threading.enumerate()))
        self.assertIn('旧数字已清除', '\n'.join(t for f in screen.frames for _,_,t in f))
        self.assertEqual(screen.delays, [100, 300])


if __name__ == '__main__': unittest.main()

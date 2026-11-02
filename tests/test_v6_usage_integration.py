"""Actual CLI/TUI entry points keep read-only guidance and comparisons separate."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import yaml

from beancount_budget_tool.launcher import main
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from beancount_budget_tool.v6 import tui
from test_v6_tui import FakeScreen


class UsageIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)/'with spaces 使用'
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples/v6/simple', self.base)
        self.config = self.base/'config.yml'
        budgets = self.base/'budgets.yml'
        plans = yaml.safe_load(budgets.read_text(encoding='utf-8'))
        plans['months']['2026-10'] = {'completeness_confirmed': True}
        budgets.write_text(yaml.safe_dump(plans, allow_unicode=True), encoding='utf-8')
        self.before = {p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([*args, '--config', str(self.config), '--format', 'json']), 0)
        return json.loads(output.getvalue())

    def test_guide_cli_does_not_create_or_rewrite_inputs(self):
        result = self.cli('guide')
        self.assertEqual(result['status'], 'DIAGNOSTIC_ONLY')
        self.assertIsNone(result['report'])
        self.assertEqual(self.before, {p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_trend_cli_keeps_audited_months_and_separate_exact_changes(self):
        result = self.cli('trend', '--since', '2026-09', '--through', '2026-10', '--owner', '生活费')
        self.assertIsNone(result['report'])
        self.assertTrue(all(row['report'] is not None for row in result['months']))
        self.assertEqual(len(result['comparisons']), 1)
        self.assertEqual(result['months'][0]['purpose']['available_cash'], '190')
        expense = self.cli('trend', '--since', '2026-09', '--through', '2026-10', '--category', '生活开销')
        self.assertEqual(len(expense['comparisons']), 1)
        self.assertEqual(self.before, {p:p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_tui_guide_and_trend_return_to_same_fresh_month(self):
        browser = SimpleReportBrowser(self.config, '2026-10')
        self.addCleanup(browser.close)
        browser.refresh()
        expected = browser.outcome
        guide = FakeScreen(keys=('\x1b',), size=(40,140))
        tui._usage_message(guide, browser, 'guide')
        self.assertIn('只读首次使用', '\n'.join(t for f in guide.frames for _,_,t in f))
        self.assertIn('首月预览 2026-09', '\n'.join(t for f in guide.frames for _,_,t in f))
        self.assertEqual(browser.outcome, expected)
        trend = FakeScreen(keys=('\x1b',), size=(40,140))
        with patch.object(tui, '_read_field', side_effect=['2026-09','2026-10','purpose','生活费']):
            tui._usage_message(trend, browser, 'trend')
        self.assertIn('月度趋势', '\n'.join(t for f in trend.frames for _,_,t in f))
        self.assertEqual(browser.outcome, expected)
        self.assertEqual(browser.selected_month, '2026-10')

    def test_repeated_diagnostic_reuse_does_not_erase_parent_report(self):
        browser = SimpleReportBrowser(self.config)
        self.addCleanup(browser.close)
        browser.refresh()
        expected = browser.outcome
        for _ in range(2):
            screen = FakeScreen(keys=('\x1b',), size=(30,120))
            tui._diagnostics_message(screen, browser, 'doctor')
            self.assertEqual(browser.outcome, expected)
        self.assertTrue(browser.diagnostic_cache.last_hit)


if __name__ == '__main__': unittest.main()

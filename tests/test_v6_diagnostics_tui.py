"""Read-only diagnostics remain fresh inside terminal popups."""

import curses
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from beancount_budget_tool.v6 import tui
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser
from test_v6_tui import FakeScreen


class DiagnosticsTuiTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        shutil.copytree(Path(__file__).resolve().parents[1]/'examples/v6/simple', self.base, dirs_exist_ok=True)
        self.config, self.ledger = self.base/'config.yml', self.base/'main.bean'
        self.browser = SimpleReportBrowser(self.config)
        self.addCleanup(self.browser.close)
        self.browser.refresh()

    def test_doctor_is_available_on_pending_month_without_signing(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8')+'''\n2026-09-07 * "ambiguous"
  Assets:Cash -1 CNY
  Expenses:Food 1 CNY
''', encoding='utf-8')
        screen = FakeScreen(keys=('\x1b',), size=(30,120))
        tui._diagnostics_message(screen, self.browser, 'doctor')
        shown = '\n'.join(text for frame in screen.frames for _,_,text in frame)
        self.assertIn('静态检查：通过', shown)
        self.assertIn('重放审计：待审', shown)
        self.assertIsNone(self.browser.outcome['report'])

    def test_workspace_filter_keeps_full_pending_count(self):
        self.ledger.write_text(self.ledger.read_text(encoding='utf-8')+'''\n2026-09-07 * "ambiguous"
  Assets:Cash -1 CNY
  Expenses:Food 1 CNY
''', encoding='utf-8')
        screen = FakeScreen(keys=('\x1b',), size=(45,160))
        with patch.object(tui, '_read_field', side_effect=['H_OWNER_PENDING','']):
            tui._diagnostics_message(screen, self.browser, 'attribution')
        shown = '\n'.join(text for frame in screen.frames for _,_,text in frame)
        self.assertIn('交易前用途余额', shown)
        self.assertIn('H_OWNER_PENDING', shown)
        self.assertIn('筛选前统计', shown)

    def test_explain_does_not_silently_use_a_month_inside_period(self):
        self.browser.period = True
        screen = FakeScreen(keys=('\x1b',), size=(12,120))
        with patch('beancount_budget_tool.v6.diagnostics.explain') as query:
            tui._diagnostics_message(screen, self.browser, 'explain')
        query.assert_not_called()
        self.assertIn('请按 Esc 返回月报', '\n'.join(t for f in screen.frames for _,_,t in f))

    def test_actual_save_during_explanation_watch_clears_previous_money(self):
        ledger = self.ledger
        class SaveScreen(FakeScreen):
            def get_wch(self):
                key = super().get_wch()
                if key == -1 and not hasattr(self, 'saved'):
                    self.saved = True
                    ledger.write_text(ledger.read_text(encoding='utf-8')+'''\n2026-09-07 * "ambiguous after display"
  Assets:Cash -1 CNY
  Expenses:Food 1 CNY
''', encoding='utf-8')
                return key
        screen = SaveScreen(keys=(-1,-1,'\x1b'), size=(30,140))
        self.browser.watch = True
        with patch.object(tui, '_read_field', return_value='cash'), \
             patch('beancount_budget_tool.v6.input_watch.time', Mock(monotonic=Mock(side_effect=[0,1,2,3,4,5]))):
            tui._diagnostics_message(screen, self.browser, 'explain')
        # Slow runners may show progress before the first complete number.
        first = next('\n'.join(t for _,_,t in frame) for frame in screen.frames
                     if '190 CNY' in '\n'.join(t for _,_,t in frame))
        last = '\n'.join(t for _,_,t in screen.frames[-1])
        self.assertIn('190 CNY', first)
        self.assertIn('当前未签发', last)
        self.assertNotIn('190 CNY', last)
        self.assertIsNone(self.browser.outcome['report'])

    def test_browser_dispatches_keys_and_enter_without_changing_navigation(self):
        screen = FakeScreen(keys=('D','l','E','\n','q'), size=(24,100))
        with patch.object(tui.curses, 'curs_set'), patch.object(tui, '_init_colors'), \
             patch.object(tui, '_diagnostics_message') as message:
            tui._report_browser(screen, self.browser)
        self.assertEqual([call.args[2] for call in message.call_args_list],
                         ['doctor','attribution','explain','explain'])


if __name__ == '__main__': unittest.main()

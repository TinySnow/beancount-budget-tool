"""Readable tables preserve exact values, status distinctions and CJK alignment."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock

from beancount_budget_tool.text_table import cell_width, table, wrap_cell
from beancount_budget_tool.v6.monthly_report import render_text, render_workflow_text
from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6.tui import ReportBrowser, _draw_browser
from test_v6_tui import FakeScreen


class ReportTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = Path(__file__).resolve().parents[1] / 'examples/v6/simple/config.yml'
        cls.outcome = audit_simple(cls.config)
        assert cls.outcome['status'] == 'CERTIFIED_SCOPED'

    def test_user_example_keeps_every_purpose_zero_and_signed_amount_in_aligned_columns(self):
        sample = [('电子产品', '900', '900'), ('生活费', None, '3818.53'),
                  ('额外储蓄', None, '0'), ('留存资金贷款', None, '0'), ('其他应付款', None, '0'),
                  ('投资', None, '2000'), ('同事差旅费', None, '0'), ('自由资金', None, '-8818.53'),
                  ('旅游', '900', '900'), ('储蓄', '1000', '1000'), ('保险', '200', '200')]
        report = deepcopy(self.outcome['report'])
        report['owner_names'] = {name: name for name, _, _ in sample}
        report['purpose_progress']['owners'] = [
            dict(owner_id=name, status='NOT_ASSESSED' if target is None else 'CERTIFIED',
                 target_q=target, actual_g=value, remaining_q_minus_g=None if target is None else '0')
            for name, target, value in sample]
        original = deepcopy(report)
        text = render_text(report)
        section = text.split('用途划拨', 1)[1].split('\n\n', 1)[0]
        rows = [line.split() for line in section.splitlines()]
        for name, target, value in sample:
            self.assertIn([name, target or '-', value, '-' if target is None else '0',
                           '不考核' if target is None else '已达成'], rows)
        self.assertTrue(all(cell_width(line) <= 76 for line in section.splitlines()))
        self.assertNotIn('本月不考核划拨目标', section)
        self.assertEqual(report, original)

    def test_numbers_right_align_by_screen_cells_instead_of_python_length(self):
        rows = [('电子产品', '900', '684.94'), ('e\u0301甲Ａ', '0', '-8818.53'), ('ABC', '1/3', '2000')]
        rendered = table(['用途', '目标', '实际'], rows, numeric=(1, 2))
        right_edges = []
        for line, row in zip(rendered[2:], rows):
            start = line.index(row[1], len(row[0]) + 2)
            right_edges.append(cell_width(line[:start + len(row[1])]))
        self.assertEqual(len(set(right_edges)), 1)
        self.assertEqual(cell_width('e\u0301甲Ａ'), 5)
        self.assertTrue(all('\t' not in line for line in rendered))
        self.assertIn('1/3', '\n'.join(rendered))

    def test_long_names_wrap_without_losing_characters_or_splitting_amounts(self):
        name = '留存资金贷款与同事差旅费超长预算' * 3 + 'LongAccountName-e\u0301'
        rendered = table(['用途', '目标 Q', '实际 G', '差额', '状态'],
                         [[name, '12345.6789', '-8818.53', '-1/3', '不考核']], numeric=(1, 2, 3), columns=76)
        first_width = len(rendered[1].strip().split('  ')[0])
        chunks = wrap_cell(name, first_width)
        self.assertEqual(''.join(chunks), name)
        self.assertEqual(len(rendered[2:]), len(chunks))
        self.assertTrue(all(line.startswith('  ' + chunk) for line, chunk in zip(rendered[2:], chunks)))
        self.assertTrue(all(cell_width(line) <= 76 for line in rendered))
        self.assertIn('12345.6789', rendered[2])
        self.assertIn('-8818.53', rendered[2])

    def test_narrow_fallback_preserves_zero_missing_and_full_exact_values(self):
        rendered = table(['用途', '目标 Q', '实际 G', '差额', '状态'],
                         [['留存资金贷款', '-', '-8818.53', '-', '不考核'],
                          ['待定用途', '0', '1/3', '-1/3', '无目标']], numeric=(1, 2, 3), columns=28)
        text = '\n'.join(rendered)
        for value in ('留存资金贷款', '-8818.53', '不考核', '待定用途', '1/3', '-1/3', '无目标', '目标 Q：0'):
            self.assertIn(value, text)
        self.assertTrue(all(cell_width(line) <= 28 for line in rendered))

    def test_refunds_unassessed_and_missing_targets_keep_separate_meanings(self):
        report = deepcopy(self.outcome['report'])
        category = report['expense_targets']['categories'][0]
        category.update(subtree_gross_spent='15', subtree_refunded='5')
        report['purpose_progress']['owners'].extend([
            dict(owner_id='missing', status='TARGET_MISSING', actual_g='0'),
            dict(owner_id='zero', status='CERTIFIED', target_q='0', actual_g='0', remaining_q_minus_g='0')])
        text = render_text(report)
        self.assertIn('支出 15，退款／冲回 5 CNY', text)
        self.assertIn(['missing', '-', '0', '-', '无目标'], [line.split() for line in text.splitlines()])
        self.assertIn(['zero', '0', '0', '0', '已达成'], [line.split() for line in text.splitlines()])
        category['subtree_remaining_status'] = 'NOT_ASSESSED'
        text = render_text(report)
        self.assertIn([category['name'] + '（含子类）', '-', '10', '-', '不考核'],
                      [line.split() for line in text.splitlines()])

    def test_tui_resize_rerenders_only_and_filter_keeps_table_headers(self):
        original = deepcopy(self.outcome)
        load = Mock(return_value=self.outcome)
        browser = ReportBrowser([self.config], load, render_workflow_text)
        browser.refresh()
        for width in (80, 120, 40):
            screen = FakeScreen(size=(24, width))
            _draw_browser(screen, browser)
            section = '\n'.join(browser.report_lines(columns=width - 1)).split('用途划拨', 1)[1].split('\n\n', 1)[0]
            self.assertTrue(all(cell_width(line) <= width - 1 for line in section.splitlines() if line.startswith('  ')))
            self.assertTrue(all(cell_width(line) <= width - 1 for _, _, line in screen.rows))
        self.assertEqual(load.call_count, 1)
        browser.filter('生活费')
        self.assertTrue(any('目标 Q' in line and '实际 G' in line for line in browser.report_lines(columns=79)))
        self.assertTrue(any('实际 G：200' in line for line in browser.report_lines(columns=28)))
        self.assertEqual(self.outcome, original)


if __name__ == '__main__':
    unittest.main()

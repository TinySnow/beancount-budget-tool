"""Registry-aware scalar labels must fail as located review, before compilation."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from functools import partial
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.launcher import main
from beancount_budget_tool.tools.v6_init import compile_setup
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.tui import ReportBrowser


class InlineReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        examples = Path(__file__).resolve().parents[1] / "examples/v6"
        self.ledger = self.base / "九月 流水.bean"
        self.original = (examples / "first-month.bean").read_text(encoding="utf-8")
        self.ledger.write_text(self.original, encoding="utf-8")
        self.root = self.base / "main.bean"
        self.root.write_text('include "九月 流水.bean"\n', encoding="utf-8")
        sheet = yaml.safe_load((examples / "first-month-setup.yaml").read_text(encoding="utf-8"))
        sheet["ledger"] = self.root.name
        self.write(self.base / "setup.yaml", sheet)
        self.config = Path(compile_setup(self.base / "setup.yaml")["config"])
        self.sidecars = {path: path.read_bytes() for path in self.config.parent.glob("*.yaml")}

    def write(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def rewrite(self, old, new):
        text = self.ledger.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.ledger.write_text(text.replace(old, new, 1), encoding="utf-8")

    def pending(self, count):
        result = audit_month_workflow(self.config)
        self.assertEqual((result["status"], result["stage"]), ("NEEDS_REVIEW", "source"))
        self.assertIsNone(result["report"])
        self.assertTrue(result["review_queue"]["preflight_only"])
        self.assertEqual(len(result["review_items"]), count)
        for item in result["review_items"]:
            self.assertEqual(item["reason_kind"], "data_review")
            filename, _, line = item["source"].rpartition(":")
            self.assertEqual(filename, str(self.ledger))
            self.assertIn(item["account"], self.ledger.read_text(encoding="utf-8").splitlines()[int(line) - 1])
            self.assertIn(item["source"], render_workflow_text(result))
            self.assertIn("CNY", item["detail"])
            self.assertIn("请核对", item["detail"])
        self.assertTrue(all(path.read_bytes() == content for path, content in self.sidecars.items()))
        return result

    def test_single_fund_amount_mismatch_is_located_without_internal_reference_error(self):
        self.rewrite('fund: "待支出资金"', 'fund: "待支出资金199"')
        result = self.pending(1)
        item = result["review_items"][0]
        self.assertEqual((item["kind"], item["reason_code"], item["signed_units"]),
                         ("fund_amount", "FUND_GRAMMAR", "200"))
        self.assertIn("199", item["detail"])
        self.assertIn("200 CNY", item["detail"])

    def test_all_budget_and_fund_errors_are_collected_together(self):
        self.rewrite('fund: "待支出资金"', 'fund: "待支出资金:199"')
        self.rewrite('budget: "日常开销.餐饮"', 'budget: "日常开销.餐饮：9元"')
        self.rewrite('  Income:Salary -500 CNY', '  Income:Salary -500 CNY\n    budget: "已收入资金499"')
        result = self.pending(3)
        self.assertEqual(result["review_queue"]["counts"], {"budget_amount": 2, "fund_amount": 1})
        self.assertEqual({item["signed_units"] for item in result["review_items"]}, {"-500", "200", "10"})
        self.assertIn("budget 金额", render_workflow_text(result))

    def test_registered_names_and_aliases_ending_in_digits_keep_whole_posting_meaning(self):
        for filename, field, spelling in (("ownership.yaml", "owners", "待支出资金199"),
                                           ("classifications.yaml", "categories", "日常开销.餐饮9")):
            path = self.config.parent / filename
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            original_name = "待支出资金" if field == "owners" else "日常开销.餐饮"
            row = next(row for row in document[field] if row["name"] == original_name)
            row["aliases"].append(spelling)
            self.write(path, document)
            self.rewrite(f'"{original_name}"', f'"{spelling}"')
        result = audit_month_workflow(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "190")

    def test_valid_single_amounts_keep_native_sign_for_income_refund_and_income_return(self):
        self.rewrite('fund: "待支出资金"', 'fund: "待支出资金:200元"')
        self.rewrite('budget: "日常开销.餐饮"', 'budget: "日常开销.餐饮 10"')
        self.rewrite('  Income:Salary -500 CNY', '  Income:Salary -500 CNY\n    budget: "已收入资金500"')
        extra = ('\n2026-09-04 * "消费退款"\n  Expenses:Food -2 CNY\n'
                 '    budget: "日常开销.餐饮2"\n  Assets:Cash 2 CNY\n    fund: "待支出资金2"\n'
                 '\n2026-09-05 * "退回收入"\n  Income:Salary 5 CNY\n'
                 '    budget: "已收入资金5"\n  Assets:Cash -5 CNY\n    fund: "已收入资金5"\n')
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + extra, encoding="utf-8")
        result = audit_month_workflow(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED", result["review_items"])
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "192")
        self.rewrite('budget: "日常开销.餐饮2"', 'budget: "日常开销.餐饮1"')
        self.rewrite('fund: "已收入资金5"', 'fund: "已收入资金4"')
        result = self.pending(2)
        self.assertEqual({item["signed_units"] for item in result["review_items"]}, {"-2", "-5"})

    def test_future_bad_single_amount_is_not_a_historical_month_problem(self):
        self.ledger.write_text(self.original + '\n2026-10-01 * "后月消费"\n'
                               '  Expenses:Food 1 CNY\n    budget: "日常开销.餐饮9"\n'
                               '  Assets:Cash -1 CNY\n    fund: "待支出资金9"\n', encoding="utf-8")
        result = audit_month_workflow(self.config)
        self.assertEqual(result["status"], "CERTIFIED_SCOPED")
        self.assertEqual(result["report"]["max_spendable"]["max_spendable"], "190")

    def test_cli_and_tui_show_review_then_recover_after_edit(self):
        self.rewrite('fund: "待支出资金"', 'fund: "待支出资金199"')
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["--config", str(self.config), "--format", "json"]), 0)
        self.assertEqual(errors.getvalue(), "")
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertIsNone(result["report"])
        browser = ReportBrowser([self.config], partial(audit_month_workflow, include_transactions=True),
                                render_workflow_text)
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "NEEDS_REVIEW")
        self.assertEqual(browser.outcome["review_items"][0]["source"], result["review_items"][0]["source"])
        self.ledger.write_text(self.original, encoding="utf-8")
        browser.refresh()
        self.assertIsNone(browser.error)
        self.assertEqual(browser.outcome["status"], "CERTIFIED_SCOPED")
        self.assertEqual(browser.outcome["report"]["max_spendable"]["max_spendable"], "190")

    def test_direct_compilers_keep_error_codes_but_report_actual_ledger_position(self):
        for tag, old, new, code in (("fund", "待支出资金", "待支出资金199", "OWNERSHIP_UNITS_MISMATCH"),
                                    ("budget", "日常开销.餐饮", "日常开销.餐饮9", "CLASSIFICATION_UNITS_MISMATCH")):
            self.ledger.write_text(self.original, encoding="utf-8")
            self.rewrite(f'{tag}: "{old}"', f'{tag}: "{new}"')
            with self.subTest(tag=tag), self.assertRaises(BudgetError) as caught:
                if tag == "fund":
                    audit_ownership(self.root, date(2026, 9, 1), self.config.parent / "accounts.yaml",
                                    self.config.parent / "ownership.yaml", self.config.parent / "opening.yaml")
                else:
                    audit_classification(self.root, date(2026, 9, 1), self.config.parent / "classifications.yaml")
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.source.startswith(str(self.ledger) + ":"))


if __name__ == "__main__":
    unittest.main()

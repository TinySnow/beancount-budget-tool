"""Interval flows, monthly targets and closing snapshots keep distinct meanings."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import csv
from io import StringIO
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.period_report import (
    _months, audit_period, csv_rows, main, render_csv, render_text,
)


class PeriodReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.base = Path(temporary.name).resolve()
        example = Path(__file__).resolve().parents[1] / "examples/v6/acceptance/run.py"
        cls.runner = runpy.run_path(str(example))
        cls.demo = cls.runner["build_demo"](cls.base / "demo")
        cls.configs = list(cls.demo["configs"].values())
        cls.originals = {path: path.read_bytes() for path in cls.base.rglob("*") if path.is_file()}
        cls.period = audit_period(cls.configs)

    def tearDown(self):
        for path, content in self.originals.items():
            path.write_bytes(content)

    def change_config(self, index, **fields):
        config = self.configs[index]
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        document.update(fields)
        self.runner["save"](config, document)

    def separate_sidecar(self, index, field):
        config = self.configs[index]
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
        document = yaml.safe_load((config.parent / settings[field]).read_text(encoding="utf-8"))
        path = self.base / f"separate-{index}-{field}.yaml"
        self.runner["save"](path, document)
        self.change_config(index, **{field: str(path)})
        return path, document

    def test_real_three_month_flow_totals_and_closing_capacity(self):
        self.assertEqual(self.period["status"], "CERTIFIED_SCOPED")
        report = self.period["report"]
        self.assertEqual(report["expense_totals"], {
            "direct_gross_spent": "2101", "direct_refunded": "760", "direct_spent": "1341"})
        self.assertEqual(report["closing"]["actual_spendable"]["actual_spendable"], "1160")
        self.assertEqual(report["closing"]["max_spendable"]["max_spendable"], "1160")
        self.assertEqual(report["closing"]["as_of"], "2026-12-31")
        self.assertEqual(report["closing"]["purpose_balances"],
                         self.demo["outcomes"]["2026-12"]["report"]["purpose_balances"])
        appliance = next(row for row in report["expenses"] if row["name"] == "家电")
        self.assertEqual((appliance["direct_gross_spent"], appliance["direct_refunded"], appliance["direct_spent"]),
                         ("800", "760", "40"))
        flow = report["default_outflows"][0]
        self.assertEqual((flow["debt_payments"], flow["advances_paid"], flow["advance_reimbursements"], flow["net_outflow"]),
                         ("120", "300", "300", "840"))
        tracking = next(row for row in report["tracking"] if row["topic"] == "大额支出")
        self.assertEqual(tracking["net"], "40")

    def test_single_month_export_keeps_unpaid_debt_and_receivable_as_stocks(self):
        result = audit_period(self.configs[:1])
        self.assertEqual(result["report"]["expense_totals"]["direct_spent"], "2081")
        self.assertEqual(result["report"]["closing"]["max_spendable"]["max_spendable"], "880")
        rows = csv_rows(result)
        debt = next(row for row in rows if row["section"] == "booked_debt")
        advance = next(row for row in rows if row["section"] == "clearing_receivable")
        self.assertEqual((debt["scope"], debt["month"], debt["value"]), ("closing", "2026-10-31", "120"))
        self.assertEqual((advance["scope"], advance["value"]), ("closing", "300"))
        self.assertIn("期末未还卡债 card：120 CNY", render_text(result))
        self.assertIn("期末待收单位代垫 employer：300 CNY", render_text(result))

    def test_comparison_preserves_each_month_targets_and_not_assessed(self):
        rows = csv_rows(self.period)
        living_targets = [row for row in rows if row["section"] == "allocations" and
                          row.get("name") == "生活费" and row["metric"] == "remaining_q_minus_g"]
        self.assertEqual([row["value"] for row in living_targets], ["0", "2000", "2000"])
        unassessed = next(row for row in rows if row.get("name") == "家电" and row["section"] == "allocations"
                          and row["metric"] == "target_q")
        self.assertEqual((unassessed["value"], unassessed["numerator"], unassessed["status"]), ("", "", "NOT_ASSESSED"))
        self.assertFalse(any(row["scope"] == "period" and row["metric"] in
                             {"target_q", "remaining_q_minus_g", "max_spendable", "actual_spendable"} for row in rows))
        text = render_text(self.period)
        self.assertIn("净额 1341 CNY", text)
        self.assertIn("2026-11 | 20 | 760 | -740 | 1160 | 1160", text)
        self.assertIn("不考核", text)
        self.assertIn("生活费 | Assets:Bank:Savings | H 原币余额：-700 CNY", text)
        self.assertTrue(all(warning["detail"] in text for warning in self.period["report"]["warnings"]))

    def test_unsorted_configs_are_ordered_and_explicit_range_is_checked(self):
        with patch("beancount_budget_tool.v6.period_report.audit_month_workflow",
                   side_effect=lambda path: self.demo["outcomes"][yaml.safe_load(path.read_text())["month"]]):
            result = audit_period(list(reversed(self.configs)), "2026-10", "2026-12")
        self.assertEqual([row["month"] for row in result["months"]], ["2026-10", "2026-11", "2026-12"])
        self.assertEqual(result["report"]["closing"]["actual_spendable"]["actual_spendable"], "1160")
        self.assertEqual(_months("2026-12", "2027-02"), ["2026-12", "2027-01", "2027-02"])
        self.assertEqual(_months("9999-12", "9999-12"), ["9999-12"])

    def test_missing_duplicate_months_or_invalid_range_cannot_look_like_zero(self):
        cases = [([], None, None, "PERIOD_CONFIG_REQUIRED"),
                 ([self.configs[0], self.configs[2]], None, None, "PERIOD_MONTHS_INCOMPLETE"),
                 ([self.configs[0]] * 2, None, None, "PERIOD_MONTH_DUPLICATE"),
                 (self.configs, "2026-09", "2026-12", "PERIOD_MONTHS_INCOMPLETE"),
                 (self.configs, "2026-10", None, "PERIOD_RANGE_INVALID"),
                 (self.configs, "2026-12", "2026-10", "PERIOD_MONTHS_INCOMPLETE"),
                 (self.configs, "2026-00", "2026-12", "PLAN_AMBIGUOUS")]
        for configs, first, last, code in cases:
            with self.subTest(code=code), self.assertRaises(BudgetError) as caught:
                audit_period(configs, first, last)
            self.assertEqual(caught.exception.code, code)

    def test_different_ledger_cutover_or_report_currency_is_rejected(self):
        original = self.configs[1].read_bytes()
        for fields in ({"cutover": "2026-09-01"}, {"report_currency": "USD"},
                       {"ledger": str(self.base / "other.bean")}):
            with self.subTest(fields=fields):
                (self.base / "other.bean").write_bytes(self.demo["ledger"].read_bytes())
                self.change_config(1, **fields)
                with self.assertRaises(BudgetError) as caught:
                    audit_period(self.configs)
                self.assertEqual(caught.exception.code, "PERIOD_SCOPE_MISMATCH")
                self.configs[1].write_bytes(original)

    def test_conflicting_category_or_owner_meaning_has_yaml_position(self):
        for field, key in (("ownership", "owners"), ("classifications", "categories")):
            with self.subTest(field=field):
                sidecar, document = self.separate_sidecar(1, field)
                document[key][0]["name"] += "改名"
                self.runner["save"](sidecar, document)
                with self.assertRaises(BudgetError) as caught:
                    audit_period(self.configs)
                self.assertEqual(caught.exception.code, "PERIOD_REGISTRY_MISMATCH")
                self.assertTrue(caught.exception.source.startswith(str(sidecar) + ":"))
                self.configs[1].write_bytes(self.originals[self.configs[1]])

    def test_default_cash_purpose_change_cannot_be_silently_merged(self):
        sidecar, document = self.separate_sidecar(1, "ownership")
        document["defaults"]["expense_owner_id"] = document["defaults"]["income_owner_id"]
        self.runner["save"](sidecar, document)
        with self.assertRaises(BudgetError) as caught:
            audit_period(self.configs)
        self.assertEqual(caught.exception.code, "PERIOD_DEFAULT_MISMATCH")

    def test_pending_middle_month_suppresses_every_interval_number(self):
        document = yaml.safe_load(self.configs[1].read_text(encoding="utf-8"))
        del document["completeness"]
        self.runner["save"](self.configs[1], document)
        outcome = audit_period(self.configs)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["report"])
        item = next(row for row in outcome["review_items"] if row["reason_code"] == "EXTERNAL_SCOPE_PENDING")
        self.assertEqual(item["month"], "2026-11")
        self.assertTrue(item["source"].startswith(str(self.configs[1])))
        self.assertTrue(all(row["value"] == "" and row["numerator"] == "" for row in csv_rows(outcome)))
        self.assertIn("未生成区间数字", render_text(outcome))
        self.assertNotIn("1160", render_text(outcome))

    def test_stale_decision_is_an_input_error_with_position_and_no_partial_totals(self):
        ledger = self.demo["ledger"]
        ledger.write_text(ledger.read_text(encoding="utf-8").replace('"跨月：单位代垫"', '"改过的单位代垫"'), encoding="utf-8")
        outcome = audit_period(self.configs)
        self.assertEqual(outcome["status"], "INPUT_ERROR")
        self.assertIsNone(outcome["report"])
        self.assertEqual(len(outcome["review_items"]), 3)
        self.assertTrue(all(row["reason_code"] == "OBLIGATION_REF_INVALID" for row in outcome["review_items"]))
        self.assertTrue(all("obligations.yaml:" in row["source"] for row in outcome["review_items"]))
        self.assertTrue(all(row["value"] == "" for row in csv_rows(outcome)))

    def test_input_changes_between_months_invalidate_whole_range(self):
        calls = []
        def audit(config):
            month = yaml.safe_load(config.read_text(encoding="utf-8"))["month"]
            calls.append(month)
            if len(calls) == 1:
                ledger = self.demo["ledger"]
                ledger.write_text(ledger.read_text(encoding="utf-8") + '\n; changed during interval\n', encoding="utf-8")
            return self.demo["outcomes"][month]
        with patch("beancount_budget_tool.v6.period_report.audit_month_workflow", side_effect=audit):
            with self.assertRaises(BudgetError) as caught:
                audit_period(self.configs)
        self.assertEqual(caught.exception.code, "INPUT_CHANGED")

    def test_unsupported_computation_remains_an_implementation_limit(self):
        with patch("beancount_budget_tool.v6.period_report.audit_month_workflow",
                   side_effect=BudgetError("OBLIGATION_EVENT_UNSUPPORTED", "复合责任协议尚未实现", "decisions.yaml:12")):
            result = audit_period(self.configs[:1])
        self.assertEqual(result["status"], "IMPLEMENTATION_BLOCKED")
        self.assertIsNone(result["report"])
        self.assertEqual(result["review_items"][0]["reason_kind"], "implementation_limit")
        self.assertEqual(result["review_items"][0]["source"], "decisions.yaml:12")

    def test_child_categories_are_not_double_counted_in_grand_total(self):
        outcomes = deepcopy(self.demo["outcomes"])
        for outcome in outcomes.values():
            categories = outcome["report"]["expense_targets"]["categories"]
            living = next(row for row in categories if row["name"] == "生活开销")
            child = deepcopy(living)
            child.update(id="child", name="生活开销.子类别", parent=living["id"])
            categories.append(child)
            for field in ("direct_gross_spent", "direct_refunded", "direct_spent"):
                living[field] = "0"
        with patch("beancount_budget_tool.v6.period_report.audit_month_workflow",
                   side_effect=lambda config: outcomes[yaml.safe_load(config.read_text())["month"]]):
            result = audit_period(self.configs)
        self.assertEqual(result["report"]["expense_totals"]["direct_spent"], "1341")

    def test_native_currencies_stay_separate_and_no_default_is_not_zero(self):
        outcomes = deepcopy(self.demo["outcomes"])
        for outcome in outcomes.values():
            report = outcome["report"]
            report["tracking"]["topics"].append({"topic": "同名专题", "commodity": "USD", "spent": "2", "returned": "0", "net": "2"})
            report["tracking"]["topics"].append({"topic": "同名专题", "commodity": "CNY", "spent": "7", "returned": "0", "net": "7"})
            report["default_outflows"] = {"status": "NOT_CONFIGURED", "totals": None}
        with patch("beancount_budget_tool.v6.period_report.audit_month_workflow",
                   side_effect=lambda config: outcomes[yaml.safe_load(config.read_text())["month"]]):
            result = audit_period(self.configs)
        self.assertEqual(result["report"]["default_outflows_status"], "NOT_CONFIGURED")
        self.assertIsNone(result["report"]["default_outflows"])
        self.assertEqual({row["commodity"]: row["net"] for row in result["report"]["tracking"] if row["topic"] == "同名专题"},
                         {"USD": "6", "CNY": "21"})
        self.assertTrue(all(row["value"] == "" for row in csv_rows(result) if row["section"] == "default_outflows"))

    def test_csv_exact_numbers_escaping_warning_and_closing_bases(self):
        outcome = deepcopy(self.period)
        topic = outcome["report"]["tracking"][0]
        topic.update(topic='=SUM(1,2)\n中文', spent="1/3", returned="1/40", net="-1/40")
        outcome["report"]["expenses"][0]["direct_gross_spent"] = "9007199254740993"
        rows = list(csv.DictReader(StringIO(render_csv(outcome))))
        rational = next(row for row in rows if row["scope"] == "period" and row["section"] == "tracking" and row["metric"] == "spent")
        self.assertEqual((rational["value"], rational["numerator"], rational["denominator"]), ("", "1", "3"))
        self.assertEqual(rational["name"], "'=SUM(1,2)\n中文")
        negative = next(row for row in rows if row["scope"] == "period" and row["section"] == "tracking" and row["metric"] == "net")
        self.assertEqual(negative["value"], "-0.025")
        self.assertTrue(any(row["value"] == "9007199254740993" for row in rows))
        self.assertTrue(any(row["section"] == "warning" and row["status"] == "WARNING" for row in rows))
        closing = [row for row in rows if row["section"] == "h_native_balances"]
        self.assertTrue(closing)
        self.assertTrue(all(row["scope"] == "closing" and row["month"] == "2026-12-31" for row in closing))

    def test_cli_saves_utf8_bom_csv_and_never_overwrites(self):
        output = self.base / "导出.csv"
        args = [argument for config in self.configs for argument in ("--config", str(config))]
        args.extend(["--format", "csv", "--output", str(output)])
        with patch("beancount_budget_tool.v6.period_report.audit_period", return_value=self.period):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(args), 0)
            content = output.read_bytes()
            self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
            self.assertIn("生活费", output.read_text(encoding="utf-8-sig"))
            with redirect_stderr(StringIO()) as errors:
                self.assertEqual(main(args), 2)
            self.assertIn("PERIOD_OUTPUT_EXISTS", errors.getvalue())
            self.assertEqual(output.read_bytes(), content)

    def test_cli_json_pending_and_input_error_exit_codes(self):
        args = ["--config", str(self.configs[0]), "--format", "json"]
        pending = {"status": "NEEDS_REVIEW", "report": None}
        for status, code in (("NEEDS_REVIEW", 0), ("INPUT_ERROR", 2)):
            pending["status"] = status
            with patch("beancount_budget_tool.v6.period_report.audit_period", return_value=pending):
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(args), code)
                self.assertIsNone(json.loads(output.getvalue())["report"])


if __name__ == "__main__":
    unittest.main()

"""Public requirement examples run through the same continuous monthly workflow."""

from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_review_decisions import create_review, apply_review
from beancount_budget_tool.tools.v6_review_source_refs import diagnose
from beancount_budget_tool.tools.v6_next_month import create_next_month, compile_next_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text


class PublicAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.base = Path(temporary.name).resolve()
        example = Path(__file__).resolve().parents[1] / "examples/v6/acceptance/run.py"
        cls.runner = runpy.run_path(str(example))
        cls.demo = cls.runner["build_demo"](cls.base / "demo")
        cls.ledger = cls.demo["ledger"]
        cls.configs = cls.demo["configs"]
        cls.originals = {path: path.read_bytes() for path in cls.ledger.parent.rglob("*")
                         if path.is_file()}

    def tearDown(self):
        for path, content in self.originals.items():
            path.write_bytes(content)

    def report(self, month="2026-10"):
        outcome = self.demo["outcomes"][month]
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        self.assertEqual(outcome["review_items"], [])
        return outcome["report"]

    def positions(self, month="2026-10", outcome=None):
        report = outcome["report"] if outcome else self.report(month)
        names = report["owner_names"]
        return {(row["account"], names[row["owner_id"]]): Fraction(row["units"])
                for row in report["account_purposes"]["rows"]}

    def rewrite(self, old, new):
        source = self.ledger.read_text(encoding="utf-8")
        self.assertIn(old, source)
        self.ledger.write_text(source.replace(old, new, 1), encoding="utf-8")

    def pending(self, code):
        outcome = audit_month_workflow(self.configs["2026-10"])
        self.assertEqual(outcome["status"], "NEEDS_REVIEW", outcome)
        self.assertIsNone(outcome["report"])
        items = [row for row in outcome["review_items"] if row["reason_code"] == code]
        self.assertTrue(items, outcome["review_items"])
        for item in items:
            self.assertTrue(item["source"].startswith(str(self.ledger) + ":"), item)
            line = int(item["source"].rpartition(":")[2])
            self.assertLessEqual(line, len(self.ledger.read_text(encoding="utf-8").splitlines()))
        return outcome

    def test_three_months_carry_balances_without_reseeding_or_recharging(self):
        for month, actual in (("2026-10", "880"), ("2026-11", "1160"), ("2026-12", "1160")):
            with self.subTest(month=month):
                report = self.report(month)
                self.assertEqual(report["actual_spendable"]["actual_spendable"], actual)
                self.assertEqual(report["max_spendable"]["max_spendable"], actual)
                settings = yaml.safe_load(self.configs[month].read_text(encoding="utf-8"))
                self.assertEqual(settings["cutover"], "2026-10-01")
                opening = (self.configs[month].parent / settings["opening"]).resolve()
                self.assertEqual(opening, self.configs["2026-10"].parent / "opening.yaml")
        self.assertEqual(self.positions("2026-11"), self.positions("2026-12"))
        december = self.report("2026-12")
        self.assertEqual(december["default_outflows"]["totals"][0]["net_outflow"], "0")
        self.assertEqual(december["tracking"]["topics"], [])

    def test_examples_1_to_3_default_purpose_and_negative_owner_warning(self):
        positions = self.positions()
        self.assertEqual(positions[("Assets:Bank:Daily", "自由资金")], 2300)
        self.assertEqual(positions[("Assets:Bank:Daily", "生活费")], 1700)
        self.assertEqual(positions[("Assets:Bank:Savings", "储蓄")], 700)
        self.assertEqual(positions[("Assets:Bank:Savings", "生活费")], -700)
        self.assertEqual(sum(amount for (account, _), amount in positions.items()
                             if account == "Assets:Bank:Savings"), 0)
        negative = self.report()["account_purposes"]["negative_purpose_positions"]
        self.assertEqual(len(negative), 1)
        text = render_workflow_text(self.demo["outcomes"]["2026-10"])
        self.assertIn("警告", text)
        self.assertIn("-700", text)

    def test_examples_4_and_5_preserve_mixed_vector_and_allow_virtual_reallocation(self):
        positions = self.positions()
        self.assertFalse(any(account == "Assets:Mixed:A" for account, _ in positions))
        self.assertEqual({name: amount for (account, name), amount in positions.items()
                          if account == "Assets:Mixed:B"},
                         {"储蓄": 300, "电子产品": 400, "旅游": 600})
        report = self.report()
        names = report["owner_names"]
        progress = {names[row["owner_id"]]: row for row in report["purpose_progress"]["owners"]}
        self.assertEqual(progress["生活费"]["actual_g"], "2000")
        self.assertEqual(progress["储蓄"]["actual_g"], "800")
        self.assertEqual(progress["自由资金"]["actual_g"], "-3400")
        # Spending, card responsibility and later settlements do not add allocation.
        for month in ("2026-11", "2026-12"):
            self.assertTrue(all(row["actual_g"] == "0" for row in self.report(month)["purpose_progress"]["owners"]))

    def test_example_5_alternative_mixed_virtual_adjustment(self):
        self.rewrite('  Assets:Mixed:B -100 CNY\n    fund: "旅游"\n'
                     '  Assets:Mixed:B 100 CNY\n    fund: "电子产品"',
                     '  Assets:Mixed:B -600 CNY\n    fund: "电子产品100，储蓄200，旅游300"\n'
                     '  Assets:Mixed:B 600 CNY\n    fund: "电子产品200，储蓄150，旅游250"')
        outcome = audit_month_workflow(self.configs["2026-10"])
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        positions = self.positions(outcome=outcome)
        self.assertEqual({name: amount for (account, name), amount in positions.items()
                          if account == "Assets:Mixed:B"},
                         {"储蓄": 250, "电子产品": 400, "旅游": 650})

    def test_examples_6_and_7_trip_remainder_and_tracking(self):
        positions = self.positions()
        self.assertEqual(positions[("Assets:Trip:Bank", "旅游")], 100)
        self.assertEqual(positions[("Assets:Trip:Fund", "旅游")], 39)
        topics = {row["topic"]: row["net"] for row in self.report()["tracking"]["topics"]}
        self.assertEqual(topics["第一次旅行"], "700")
        self.assertEqual(topics["第二次旅行"], "461")

    def test_example_8_refund_is_in_its_real_month_and_cumulative_net_is_40(self):
        categories = [{row["name"]: row for row in self.report(month)["expense_targets"]["categories"]}
                      for month in ("2026-10", "2026-11")]
        self.assertEqual(categories[0]["家电"]["direct_gross_spent"], "800")
        self.assertEqual(categories[0]["家电"]["direct_refunded"], "0")
        self.assertEqual(categories[1]["家电"]["direct_gross_spent"], "0")
        self.assertEqual(categories[1]["家电"]["direct_refunded"], "760")
        self.assertEqual(sum(Fraction(rows["家电"]["direct_spent"]) for rows in categories), 40)
        self.assertEqual(self.positions("2026-11")[("Assets:Appliance", "家电")], 760)
        self.assertEqual(self.report("2026-11")["tracking"]["topics"][0]["net"], "-760")
        self.assertEqual(audit_month_workflow(self.configs["2026-10"])["report"]["expense_targets"],
                         self.report()["expense_targets"])

    def test_example_8_original_same_month_refund_also_certifies(self):
        self.rewrite('2026-11-02 * "E8 家电退款"', '2026-10-14 * "E8 家电退款"')
        outcome = audit_month_workflow(self.configs["2026-10"])
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        category = next(row for row in outcome["report"]["expense_targets"]["categories"] if row["name"] == "家电")
        self.assertEqual((category["direct_gross_spent"], category["direct_refunded"], category["direct_spent"]),
                         ("800", "760", "40"))

    def test_example_9_category_exclusion_and_purpose_exclusion_are_independent(self):
        positions = self.positions()
        self.assertEqual(positions[("Assets:Outside:Cash", "储蓄")], 100)
        self.assertFalse(any(account == "Assets:Outside:Bank" for account, _ in positions))
        expenses = self.report()["expense_targets"]["categories"]
        self.assertEqual(sum(Fraction(row["direct_spent"]) for row in expenses), 2081)
        purposes = self.report()["purpose_balances"]["purposes"]
        savings = next(row for row in purposes if row["name"] == "储蓄")
        self.assertEqual(savings["h_native_balances"][0]["units"], "1100")

    def test_card_and_advance_cross_month_not_double_counted(self):
        october, november = self.report(), self.report("2026-11")
        self.assertEqual(october["actual_spendable"]["booked_debts"][0]["remaining_native"], "120")
        self.assertEqual(october["actual_spendable"]["clearing_receivables"][0]["remaining_native"], "300")
        self.assertEqual(november["actual_spendable"]["booked_debts"], [])
        self.assertEqual(november["actual_spendable"]["clearing_receivables"], [])
        totals = november["default_outflows"]["totals"][0]
        self.assertEqual((totals["expenses_paid"], totals["debt_payments"], totals["advance_reimbursements"]),
                         ("20", "120", "300"))
        living = next(row for row in november["expense_targets"]["categories"] if row["name"] == "生活开销")
        self.assertEqual(living["direct_spent"], "20")

    def test_example_4_bad_sum_returns_no_report_and_exact_ledger_line(self):
        self.rewrite('fund: "储蓄100，电子产品200，旅游400"', 'fund: "储蓄100，电子产品200，旅游399"')
        self.pending("FUND_GRAMMAR")

    def test_example_5_equal_total_but_different_vector_is_pending(self):
        self.rewrite('  Assets:Mixed:B 600 CNY\n    fund: "电子产品100，储蓄200，旅游300"',
                     '  Assets:Mixed:B 600 CNY\n    fund: "电子产品50，储蓄250，旅游300"')
        self.pending("H_TRANSFER_MIXED_REMAP_PENDING")

    def test_voluntary_reallocation_cannot_overdraw_a_named_purpose(self):
        self.rewrite('  Assets:Mixed:B -100 CNY\n    fund: "旅游"\n'
                     '  Assets:Mixed:B 100 CNY\n    fund: "电子产品"',
                     '  Assets:Mixed:B -701 CNY\n    fund: "旅游"\n'
                     '  Assets:Mixed:B 701 CNY\n    fund: "电子产品"')
        with self.assertRaises(BudgetError) as caught:
            audit_month_workflow(self.configs["2026-10"])
        self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")
        self.assertIsNotNone(caught.exception.source)

    def test_ambiguous_transfer_can_be_reviewed_then_old_source_change_is_detected(self):
        self.rewrite('  Assets:Bank:Daily -700 CNY\n    fund: "自由资金"',
                     '  Assets:Bank:Daily -700 CNY')
        pending = self.pending("FUND_MIXED_SOURCE")
        cause = next(row for row in pending["review_items"] if row["reason_code"] == "FUND_MIXED_SOURCE")
        self.assertIn("部分转出", cause["detail"])
        self.assertIn("fund", cause["detail"])
        draft = create_review(self.configs["2026-10"], self.base / "review")
        worksheet = Path(draft["worksheet"])
        document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        self.assertEqual(len(document["rows"]), 1)
        document["rows"][0].update(decision="自由资金", reason="虚构例 2 从自由资金正式划入储蓄", approved=True)
        self.runner["save"](worksheet, document)
        reviewed = apply_review(worksheet)
        self.assertEqual(reviewed["workflow"]["status"], "CERTIFIED_SCOPED", reviewed.get("report_error"))
        reviewed_config = Path(reviewed["config"])
        before = deepcopy(reviewed["workflow"]["report"]["account_purposes"])
        # Unrelated source edits must not expire an approval.
        self.ledger.write_text(self.ledger.read_text(encoding="utf-8") + '\n; 无关备注\n', encoding="utf-8")
        self.assertEqual(audit_month_workflow(reviewed_config)["report"]["account_purposes"], before)
        self.rewrite('"E2 首次划入储蓄"', '"E2 更正后的交易说明"')
        diagnostics = diagnose(reviewed_config)
        self.assertEqual(diagnostics["stale_reference_count"], 1)
        stale = next(row for row in diagnostics["sidecars"] if row["field"] == "ownership")["stale_references"][0]
        self.assertTrue(stale["review_source"].startswith(str(reviewed_config.parent / "ownership.yaml") + ":"))
        self.assertEqual(stale["candidate_count"], 1)
        self.assertTrue(stale["candidates_not_approved"][0]["source"].startswith(str(self.ledger) + ":"))
        with self.assertRaises(BudgetError) as caught:
            audit_month_workflow(reviewed_config)
        self.assertEqual(caught.exception.code, "OWNERSHIP_REF_INVALID")
        self.assertIsNotNone(caught.exception.source)
        # Human review explicitly retires the stale decision, then approves a
        # freshly exported row. No candidate is rebound merely because it matches.
        ownership = reviewed_config.parent / "ownership.yaml"
        document = yaml.safe_load(ownership.read_text(encoding="utf-8"))
        self.assertEqual(len(document["claims"]), 1)
        document["claims"] = []
        self.runner["save"](ownership, document)
        fresh = create_review(reviewed_config, self.base / "re-review")
        worksheet = Path(fresh["worksheet"])
        document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        self.assertEqual(document["rows"][0]["narration"], "E2 更正后的交易说明")
        document["rows"][0].update(decision="自由资金", reason="已重新核对改写后的虚构交易", approved=True)
        self.runner["save"](worksheet, document)
        corrected = apply_review(worksheet)
        self.assertEqual(corrected["workflow"]["status"], "CERTIFIED_SCOPED", corrected.get("report_error"))
        self.assertEqual(corrected["workflow"]["report"]["account_purposes"], before)
        corrected_config = Path(corrected["config"])
        create_next_month(corrected_config, self.base / "corrected-next")
        worksheet = self.base / "corrected-next" / "next-month.yaml"
        document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        document.update(targets_confirmed=True, completeness_confirmed=True,
                        completeness_reason="虚构下一月重新核对完毕")
        self.runner["save"](worksheet, document)
        following = compile_next_month(worksheet)
        self.assertEqual(following["workflow"]["status"], "CERTIFIED_SCOPED", following.get("report_error"))
        self.assertEqual(following["workflow"]["report"]["max_spendable"]["max_spendable"], "1160")
        self.assertEqual(audit_month_workflow(corrected_config)["report"]["max_spendable"]["max_spendable"], "880")

    def test_existing_output_is_never_overwritten(self):
        with self.assertRaises(BudgetError) as caught:
            self.runner["build_demo"](self.ledger.parent)
        self.assertEqual(caught.exception.code, "SETUP_OUTPUT_EXISTS")
        self.assertTrue(all(path.read_bytes() == content for path, content in self.originals.items()))


if __name__ == "__main__":
    unittest.main()

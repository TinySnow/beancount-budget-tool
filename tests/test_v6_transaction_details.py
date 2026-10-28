"""Signed-month provenance must not turn labels, debt, or K flows into holdings."""

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import date
from fractions import Fraction
from io import StringIO
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from beancount.core import data
import yaml

from beancount_budget_tool.financial import load_financial
from fixture_inputs import create_setup, compile_setup
from beancount_budget_tool.v6.review import create_review, apply_review
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text, main
from beancount_budget_tool.v6.purpose_pools import opening_fingerprint
from beancount_budget_tool.v6.transaction_details import render_transaction
from beancount_budget_tool.v6.tui import ReportBrowser, _draw_browser, _report_browser
from test_v6_tui import FakeScreen


class TransactionDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory(prefix="budget-e3-")
        cls.addClassCleanup(temporary.cleanup)
        cls.base = Path(temporary.name).resolve()
        cls.runner = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/v6/acceptance/run.py"))
        cls.demo = cls.runner["build_demo"](cls.base / "demo")
        cls.configs = cls.demo["configs"]
        cls.outcomes = {month: audit_month_workflow(config, include_transactions=True) for month, config in cls.configs.items()}
        cls.originals = {path: path.read_bytes() for path in cls.demo["ledger"].parent.rglob("*") if path.is_file()}

    def tearDown(self):
        for path, content in self.originals.items():
            path.write_bytes(content)

    def report(self, month="2026-10", outcome=None):
        outcome = outcome or self.outcomes[month]
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        return outcome["report"]

    def transaction(self, prefix, month="2026-10", outcome=None):
        return next(row for row in self.report(month, outcome)["transactions"]["rows"] if row["narration"].startswith(prefix))

    def test_opt_in_changes_no_existing_monthly_numbers_or_default_output(self):
        for month, outcome in self.outcomes.items():
            report = self.report(month)
            comparable = {key: value for key, value in report.items() if key != "transactions"}
            self.assertEqual(comparable, self.demo["outcomes"][month]["report"])
            self.assertNotIn("transactions", self.demo["outcomes"][month]["report"])
            self.assertEqual(report["transactions"]["count"], outcome["review_queue"]["booked_transactions_in_range"])
        self.assertEqual([self.report(month)["transactions"]["count"] for month in self.outcomes], [16, 4, 0])

    def test_exact_source_references_and_original_text_are_month_bounded(self):
        ledger = self.demo["ledger"]
        fin = load_financial(ledger)
        facts = FactIndex(fin)
        expected = [facts.for_transaction(entry).as_dict() for entry in fin.entries
                    if isinstance(entry, data.Transaction) and entry.date.strftime("%Y-%m") == "2026-10"]
        rows = self.report()["transactions"]["rows"]
        self.assertEqual([row["ref"] for row in rows], expected)
        lines = ledger.read_text(encoding="utf-8").splitlines()
        for row in rows:
            self.assertEqual(row["source_text"].splitlines()[0], lines[int(row["source"].rpartition(":")[2]) - 1])
            self.assertTrue(row["source"].startswith(str(ledger) + ":"))
            for part in row["postings"]:
                self.assertIn(part["account"], lines[int(part["source"].rpartition(":")[2]) - 1])
        self.assertEqual(self.report()["transactions"]["ledger_snapshot"], self.report()["ledger_snapshot"])

    def test_category_default_payer_and_tracking_are_independent(self):
        row = self.transaction("E3")
        expense, asset = row["postings"]
        self.assertEqual(expense["classification"]["slices"][0]["name"], "交通费用")
        self.assertEqual(expense["tracking"], ["第一次旅行"])
        self.assertEqual(asset["h_purpose"]["slices"][0]["name"], "生活费")
        self.assertEqual(asset["h_purpose"]["origin"], "inferred")
        self.assertEqual(row["net_allocations"], {})
        self.assertEqual(asset["default_outflows"][0]["amount"], "700")
        self.assertIn("默认支出资金收付：消费实付 700 CNY", "\n".join(render_transaction(row, self.report()["owner_names"])))

    def test_mixed_purpose_slices_and_per_transaction_g_reconcile_to_month(self):
        row = self.transaction("E4")
        self.assertEqual({part["name"]: part["signed_units"] for part in row["postings"][1]["h_purpose"]["slices"]},
                         {"储蓄": "100", "电子产品": "200", "旅游": "400"})
        report = self.report()
        for purpose in report["purpose_progress"]["owners"]:
            total = sum((Fraction(tx["net_allocations"].get(purpose["owner_id"], "0")) for tx in report["transactions"]["rows"]), Fraction(0))
            self.assertEqual(total, Fraction(purpose["actual_g"]))
        self.assertEqual(self.transaction("E5 混合用途搬家")["net_allocations"], {})

    def test_refund_and_closed_card_responsibility_remain_visible_in_next_month(self):
        refund = self.transaction("E8", "2026-11")
        self.assertEqual(refund["postings"][0]["signed_units"], "-760")
        self.assertEqual(refund["postings"][1]["h_purpose"]["slices"][0]["name"], "家电")
        payment = self.transaction("跨月：信用卡还款", "2026-11")
        self.assertEqual(payment["liabilities"][0]["responsibility_owner_name"], "生活费")
        self.assertEqual(payment["net_allocations"], {})
        self.assertEqual(self.report("2026-11")["actual_spendable"]["booked_debts"], [])
        clearing = self.transaction("跨月：单位报销", "2026-11")["liabilities"][0]
        self.assertEqual(clearing["clearing_id"], "employer")
        self.assertIsNone(clearing["responsibility_owner_name"])

    def test_repayment_payer_is_not_rewritten_to_responsibility_owner(self):
        ledger = self.demo["ledger"]
        source = ledger.read_text(encoding="utf-8").replace('  Assets:Bank:Daily -120 CNY\n    fund: "生活费"',
                                                          '  Assets:Bank:Daily -120 CNY\n    fund: "自由资金"')
        ledger.write_text(source, encoding="utf-8")
        fin = load_financial(ledger)
        facts = FactIndex(fin)
        entry = next(entry for entry in fin.entries if isinstance(entry, data.Transaction) and entry.narration == "跨月：信用卡还款")
        refs = [facts.for_posting(part).as_dict() for part in entry.postings]
        debts = self.configs["2026-10"].parent / "obligations.yaml"
        document = yaml.safe_load(debts.read_text(encoding="utf-8"))
        claim = next(row for row in document["events"] if row["kind"] == "payment")
        claim.update(liability_ref=refs[0], counterpart_ref=refs[1])
        self.runner["save"](debts, document)
        outcome = audit_month_workflow(self.configs["2026-11"], include_transactions=True)
        row = self.transaction("跨月：信用卡还款", "2026-11", outcome)
        self.assertEqual(row["postings"][1]["h_purpose"]["slices"][0]["name"], "自由资金")
        self.assertEqual(row["liabilities"][0]["responsibility_owner_name"], "生活费")
        self.assertEqual(row["postings"][1]["default_outflows"], [])

    def test_external_decision_is_shown_without_fabricating_inline_label(self):
        ledger = self.demo["ledger"]
        ledger.write_text(ledger.read_text(encoding="utf-8").replace('  Assets:Bank:Daily -700 CNY\n    fund: "自由资金"',
                                                                   '  Assets:Bank:Daily -700 CNY'), encoding="utf-8")
        draft = create_review(self.configs["2026-10"], self.base / "external-review")
        worksheet = Path(draft["worksheet"])
        document = yaml.safe_load(worksheet.read_text(encoding="utf-8"))
        document["rows"][0].update(approved=True, decision="自由资金", reason="虚构转账经人工审阅")
        self.runner["save"](worksheet, document)
        reviewed = apply_review(worksheet)
        outcome = audit_month_workflow(Path(reviewed["config"]), include_transactions=True)
        row = self.transaction("E2", outcome=outcome)
        part = row["postings"][0]
        self.assertEqual(part["h_purpose"]["origin"], "external")
        self.assertEqual(part["h_purpose"]["review_reason"], "虚构转账经人工审阅")
        self.assertNotIn("fund", part["labels"])

    def test_metric_pending_erases_all_transaction_details_and_tui_cannot_open_them(self):
        config = self.configs["2026-11"]
        document = yaml.safe_load(config.read_text(encoding="utf-8"))
        del document["completeness"]
        self.runner["save"](config, document)
        outcome = audit_month_workflow(config, include_transactions=True)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertIsNone(outcome["report"])
        browser = ReportBrowser([config], Mock(return_value=outcome), render_workflow_text)
        browser.refresh()
        browser.toggle_transactions()
        self.assertEqual(browser.view, "reviews")
        self.assertEqual(browser.transactions(), [])

    def test_tui_filters_transaction_source_and_keeps_filtered_selection(self):
        browser = ReportBrowser([self.configs["2026-10"]], Mock(return_value=self.outcomes["2026-10"]), render_workflow_text)
        browser.refresh()
        browser.toggle_transactions()
        browser.filter("第一次旅行")
        self.assertEqual(len(browser.transactions()), 1)
        browser.open_detail()
        self.assertTrue(browser.detail["narration"].startswith("E3"))
        screen = FakeScreen(size=(120, 40))
        _draw_browser(screen, browser)
        text = "".join(line for row, _, line in screen.rows if 3 <= row < 119)
        self.assertIn("交通费用", text)
        self.assertIn("生活费", text)
        self.assertIn("原始账本内容", text)
        browser.filter("不存在的文字")
        screen = FakeScreen(size=(12, 160))
        _draw_browser(screen, browser)
        text = "\n".join(line for _, _, line in screen.rows)
        self.assertIn("匹配 0 项", text)
        self.assertIn("交易总数 16", text)
        self.assertIn("月报警告 2 条", text)

    def test_short_transaction_queries_ignore_parent_paths_and_opaque_ids(self):
        outcome = deepcopy(self.outcomes["2026-10"])
        rows = outcome["report"]["transactions"]["rows"]
        for style in ("/private/tmp/budget-E3-random/", "C:\\Users\\name\\Temp\\budget-E3-random\\"):
            with self.subTest(style=style):
                for row in rows:
                    row["source"] = style + "acceptance.bean:11"
                    for posting in row["postings"]:
                        posting["source"] = style + "acceptance.bean:12"
                    for event in row["liabilities"]:
                        event["obligation_id"] = "opaque_E3_not_a_search_term"
                unchanged = deepcopy(outcome)
                browser = ReportBrowser([self.configs["2026-10"]], Mock(return_value=outcome), render_workflow_text)
                browser.refresh()
                browser.toggle_transactions()
                browser.filter("e3")
                self.assertEqual(len(browser.transactions()), 1)
                self.assertTrue(browser.transactions()[0]["narration"].startswith("E3"))
                browser.filter("opaque_E3_not_a_search_term")
                self.assertEqual(browser.transactions(), [])
                browser.filter("acceptance.bean:12")
                self.assertEqual(len(browser.transactions()), len(rows))
                for query in ("第一次旅行", "Expenses:Traffic", "700"):
                    browser.filter(query)
                    self.assertTrue(any(row["narration"].startswith("E3") for row in browser.transactions()))
                normal = "\n".join(render_transaction(rows[0], outcome["report"]["owner_names"]))
                self.assertIn(style + "acceptance.bean:11", normal)
                debt = next(row for row in rows if row["liabilities"])
                self.assertIn("opaque_E3_not_a_search_term", "\n".join(render_transaction(debt, outcome["report"]["owner_names"])))
                self.assertEqual(outcome, unchanged)

    def test_tui_keys_and_refresh_clear_details_before_pending_or_error(self):
        outcomes = [self.outcomes["2026-10"], self.outcomes["2026-11"]]
        browser = ReportBrowser(list(self.configs.values())[:2], Mock(side_effect=outcomes), render_workflow_text)
        screen = FakeScreen(keys=("a", "/", "旅", "行", "\n", "\n", "G", "\x1b", "]", "q"), size=(15, 120))
        with patch("beancount_budget_tool.v6.tui.curses.curs_set"):
            _report_browser(screen, browser)
        self.assertEqual(browser.outcome["month"], "2026-11")
        self.assertEqual(browser.view, "transactions")
        self.assertIsNone(browser.detail)
        self.assertTrue(any("交易来源详情" in line for frame in screen.frames for _, _, line in frame))
        browser.loader = Mock(side_effect=OSError("source temporarily unavailable"))
        browser.refresh()
        self.assertIsNone(browser.outcome)
        self.assertEqual(browser.transactions(), [])
        self.assertIsNone(browser.detail)

    def test_empty_signed_month_has_no_borrowed_transactions(self):
        browser = ReportBrowser([self.configs["2026-12"]], Mock(return_value=self.outcomes["2026-12"]), render_workflow_text)
        browser.refresh()
        browser.toggle_transactions()
        self.assertEqual(browser.view, "transactions")
        self.assertEqual(browser.transactions(), [])
        screen = FakeScreen(size=(10, 140))
        _draw_browser(screen, browser)
        self.assertTrue(any("交易总数 0" in line for _, _, line in screen.rows))

    def test_saved_source_change_reloads_details_and_bad_yaml_clears_them(self):
        config = self.configs["2026-10"]
        browser = ReportBrowser([config], lambda path: audit_month_workflow(path, include_transactions=True), render_workflow_text)
        browser.refresh()
        browser.toggle_transactions()
        browser.filter("E3")
        browser.open_detail()
        self.assertEqual(browser.detail["postings"][0]["signed_units"], "700")
        ledger = self.demo["ledger"]
        ledger.write_text(ledger.read_text(encoding="utf-8").replace("Expenses:Traffic 700 CNY", "Expenses:Traffic 690 CNY").replace(
            "Assets:Bank:Savings -700 CNY", "Assets:Bank:Savings -690 CNY"), encoding="utf-8")
        browser.refresh()
        self.assertEqual(browser.view, "transactions")
        self.assertIsNone(browser.detail)
        browser.open_detail()
        self.assertEqual(browser.detail["postings"][0]["signed_units"], "690")
        config.write_text(config.read_text(encoding="utf-8") + "\nschema: 6\n", encoding="utf-8")
        browser.refresh()
        self.assertEqual(browser.error.code, "YAML_DUPLICATE_KEY")
        self.assertIsNone(browser.detail)
        self.assertEqual(browser.transactions(), [])

    def test_cli_text_and_json_opt_in_preserve_provenance(self):
        for format_ in ("text", "json"):
            with patch("beancount_budget_tool.v6.monthly_report.audit_month_workflow", return_value=self.outcomes["2026-10"]) as audit:
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(["--config", str(self.configs["2026-10"]), "--format", format_, "--transactions"]), 0)
            self.assertEqual(audit.call_args.kwargs, {"include_transactions": True})
            if format_ == "json":
                self.assertEqual(json.loads(output.getvalue())["report"]["transactions"]["count"], 16)
            else:
                self.assertIn("原始账本内容", output.getvalue())
                self.assertIn("责任用途：生活费", output.getvalue())


class DetailProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.ledger = self.base / "main.bean"
        self.runner = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples/v6/acceptance/run.py"))

    def save(self, path, document):
        self.runner["save"](path, document)

    def configure(self, source, *, modes=None, openings=None, extra_owners=()):
        self.ledger.write_text(source, encoding="utf-8")
        setup = Path(create_setup(self.ledger, "2026-09", self.base / "setup",
            income_purpose="自由资金", expense_purpose="生活费", expense_category="生活开销")["setup"])
        worksheet = yaml.safe_load(setup.read_text(encoding="utf-8"))
        worksheet["defaults"]["confirmed"] = worksheet["completeness_confirmed"] = True
        worksheet["expense_targets"] = {"生活开销": "2000"}
        worksheet["allocation_targets"] = {name: "not_assessed" for name in
                                            set(worksheet["allocation_targets"]) | set(extra_owners)}
        for row in worksheet["accounts"]:
            mode = (modes or {}).get(row["account"], "H")
            row.update(semantics=mode, reason="已审虚构账户")
            row["payment_scope"] = {commodity: "payable" if commodity == "CNY" and not row["account"].startswith("Assets:Broker")
                                     and mode == "H" else "excluded" for commodity in row["payment_scope"]}
            for coordinate in row["opening"]:
                coordinate["owners"] = (openings or {}).get(row["account"], {"生活费": coordinate["signed_units"]}) if mode == "H" else {}
        self.save(setup, worksheet)
        compiled = compile_setup(setup)
        self.assertIsNotNone(compiled.get("config"), compiled)
        self.config = Path(compiled["config"])
        self.ownership = yaml.safe_load((self.config.parent / "ownership.yaml").read_text(encoding="utf-8"))
        self.owners = {row["name"]: row["id"] for row in self.ownership["owners"]}
        self.fin = load_financial(self.ledger)
        facts = FactIndex(self.fin)
        self.refs = {entry.narration: [facts.for_posting(posting).as_dict() for posting in entry.postings]
                     for entry in self.fin.entries if isinstance(entry, data.Transaction)}

    def audit(self):
        outcome = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        return outcome["report"]

    def test_k_slices_use_certified_subscription_and_never_become_h_holdings(self):
        self.configure('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n2026-08-01 open Assets:FundBook\n2026-08-01 open Income:Salary\n'
            '2026-09-01 * "income"\n  Assets:Cash 500 CNY\n  Income:Salary -500 CNY\n'
            '2026-09-02 * "allocate"\n  Assets:Cash -200 CNY\n    fund: "自由资金"\n'
            '  Assets:Cash 200 CNY\n    fund: "生活费"\n'
            '2026-09-03 * "subscription"\n  Assets:Cash -20 CNY\n    fund: "生活费"\n'
            '  Assets:FundBook 20 CNY\n    fund: "储蓄"\n', modes={"Assets:FundBook": "K"})
        report = self.audit()
        row = next(row for row in report["transactions"]["rows"] if row["narration"] == "subscription")
        k = row["postings"][1]
        self.assertIsNone(k["h_purpose"])
        self.assertEqual(k["k_purpose"]["slices"][0]["name"], "储蓄")
        self.assertEqual(k["k_purpose"]["slices"][0]["signed_units"], "20")
        self.assertEqual(row["net_allocations"][self.owners["储蓄"]], "20")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "180")
        self.assertIn("不是持仓、市值或可赎回金额", "\n".join(render_transaction(row, report["owner_names"])))

    def test_pad_generated_booked_source_stays_bound_to_original_directive(self):
        self.configure('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n2026-08-01 open Equity:Opening\n'
            '2026-09-02 pad Assets:Cash Equity:Opening\n2026-09-03 balance Assets:Cash 100 CNY\n')
        entry = next(entry for entry in self.fin.entries if isinstance(entry, data.Transaction))
        index = next(i for i, posting in enumerate(entry.postings) if posting.account == "Assets:Cash")
        self.ownership["claims"].append({"claim": self.refs[entry.narration][index], "reason": "虚构期初差额为预算外",
            "slices": [{"id": "generated_cash", "owner_id": "-", "role": "hold", "signed_units": "100"}]})
        self.save(self.config.parent / "ownership.yaml", self.ownership)
        row = self.audit()["transactions"]["rows"][0]
        self.assertTrue(row["generated"])
        self.assertEqual(row["ref"]["generated_kind"], "pad")
        self.assertEqual(row["source_text"], "2026-09-02 pad Assets:Cash Equity:Opening")
        self.assertEqual(row["postings"][index]["h_purpose"]["slices"][0]["name"], "预算外")

    def test_foreign_card_keeps_native_units_and_sourced_settlement_cost(self):
        self.configure('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Education\n2026-08-01 open Liabilities:Card\n'
            '2026-08-31 * "opening"\n  Assets:Bank 1000 CNY\n  Equity:Opening -1000 CNY\n'
            '2026-09-11 * "subscription"\n  Expenses:Education 21.76 USD\n  Liabilities:Card -21.76 USD\n'
            '2026-09-28 * "payment"\n  Liabilities:Card 21.76 USD\n  Assets:Bank -21.76 USD\n    fund: "生活费"\n'
            '2026-09-28 * "fx"\n  Assets:Bank 21.76 USD @@ 146.41 CNY\n    fund: "生活费"\n'
            '  Assets:Bank -146.41 CNY\n    fund: "生活费"\n')
        accounts = self.config.parent / "accounts.yaml"
        document = yaml.safe_load(accounts.read_text(encoding="utf-8"))
        document.update(credit_card_whitelist=["Liabilities:Card"], same_day_settlements=[{
            "asset_account": "Assets:Bank", "credit_card_account": "Liabilities:Card", "debit_ref": self.refs["payment"][1],
            "liability_ref": self.refs["payment"][0], "funding_ref": self.refs["fx"][0], "reason": "虚构同日还款清算"}])
        self.save(accounts, document)
        obligations = self.config.parent / "obligations.yaml"
        document = yaml.safe_load(obligations.read_text(encoding="utf-8"))
        document["events"] = [{"liability_ref": self.refs["subscription"][1], "counterpart_ref": self.refs["subscription"][0],
            "kind": "consumer_purchase", "obligation_id": "card", "owner_id": self.owners["生活费"], "reason": "虚构教育卡债"},
            {"liability_ref": self.refs["payment"][0], "counterpart_ref": self.refs["payment"][1],
             "kind": "payment", "obligation_id": "card", "reason": "虚构对应还款"}]
        self.save(obligations, document)
        fx = self.config.parent / "h_fx.yaml"
        document = yaml.safe_load(fx.read_text(encoding="utf-8"))
        document["events"] = [{"from_ref": self.refs["fx"][1], "to_ref": self.refs["fx"][0],
            "owner_id": self.owners["生活费"], "signed_from_units": "-146.41", "signed_to_units": "21.76",
            "boundary_role": "preserve", "reason": "虚构同日购汇，仅偿还已审卡债"}]
        self.save(fx, document)
        report = self.audit()
        row = next(row for row in report["transactions"]["rows"] if row["narration"] == "subscription")
        expense = row["postings"][0]
        self.assertEqual((expense["signed_units"], expense["commodity"]), ("544/25", "USD"))
        self.assertIsNone(expense["historical_valuation"])
        self.assertEqual(expense["settlement_comparison"]["budget_comparison_value"], "14641/100")
        self.assertEqual(expense["settlement_comparison"]["settlement_date"], "2026-09-28")
        text = "\n".join(render_transaction(row, report["owner_names"]))
        self.assertIn("21.76 USD", text)
        self.assertIn("2026-09-28 实际还款／购汇成本 146.41 CNY", text)
        self.assertIn("不是消费日价格", text)
        browser = ReportBrowser([self.config], Mock(return_value={"status": "CERTIFIED_SCOPED", "month": "2026-09",
            "report": report, "review_items": []}), lambda row: "v6 示例月报")
        browser.refresh()
        browser.toggle_transactions()
        browser.filter("146.41")
        self.assertTrue(any(row["narration"] == "subscription" for row in browser.transactions()))

    def test_unpriced_foreign_income_does_not_gain_a_fictitious_report_value(self):
        self.configure('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n2026-08-01 open Income:Salary\n'
            '2026-09-01 * "foreign income"\n  Assets:Cash 10 USD\n  Income:Salary -10 USD\n')
        report = self.audit()
        row = report["transactions"]["rows"][0]
        income = row["postings"][1]
        self.assertEqual((income["signed_units"], income["commodity"]), ("-10", "USD"))
        self.assertIsNone(income["historical_valuation"])
        self.assertIsNone(income["settlement_comparison"])
        self.assertIn("本项仅显示原币分类", "\n".join(render_transaction(row, report["owner_names"])))

    def test_pool_internal_lots_do_not_acquire_default_expense_owner_slices(self):
        self.configure('option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Broker:Cash\n2026-08-01 open Assets:Broker:Shares\n'
            '2026-08-01 open Equity:Opening\n2026-08-01 open Income:Interest\n'
            '2026-08-31 * "opening"\n  Assets:Broker:Cash 4000 CNY\n  Equity:Opening -4000 CNY\n'
            '2026-09-02 * "repo buy"\n  Assets:Broker:Shares 30 SEC {100 CNY}\n  Assets:Broker:Cash -3000 CNY\n'
            '2026-09-03 * "repo maturity"\n  Assets:Broker:Shares -30 SEC {100 CNY}\n'
            '  Assets:Broker:Cash 3001 CNY\n  Income:Interest -1 CNY\n',
            openings={"Assets:Broker:Cash": {"储蓄": "3000", "投资": "1000"}}, extra_owners=("储蓄", "投资"))
        pools = self.config.parent / "purpose_pools.yaml"
        accounts = ["Assets:Broker:Cash", "Assets:Broker:Shares"]
        self.save(pools, {"schema": 6, "cutover": "2026-09-01", "report_currency": "CNY", "pools": [{
            "id": "broker", "accounts": accounts, "reserve_order": accounts, "payment_scope": "excluded",
            "opening_fingerprint": opening_fingerprint(self.fin, date(2026, 9, 1), accounts),
            "protected_owner_id": self.owners["储蓄"], "protected_label": "额外储蓄", "protected_principal": "3000",
            "remainder_owner_id": self.owners["投资"], "remainder_label": "待投资金", "reason": "虚构本金持续保护"}]})
        benefits = self.config.parent / "benefit_groups.yaml"
        document = yaml.safe_load(benefits.read_text(encoding="utf-8"))
        document["approvals"] = [{"income_ref": self.refs["repo maturity"][2], "reason": "虚构到期本金和结息守恒"}]
        self.save(benefits, document)
        report = self.audit()
        rows = report["transactions"]["rows"]
        self.assertTrue(all(row["pool_internal"] for row in rows))
        for row in rows:
            self.assertEqual(row["net_allocations"], {})
            for posting in row["postings"]:
                if posting["account"].startswith("Assets:Broker"):
                    self.assertIsNone(posting["h_purpose"])
                    self.assertEqual(posting["pool"]["basis"], "pool_level_only_no_lot_attribution")
        self.assertEqual(report["purpose_pools"]["pools"][0]["protected_nominal_principal"], "3000")
        self.assertEqual(report["actual_spendable"]["actual_spendable"], "0")

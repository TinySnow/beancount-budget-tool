"""First pad balances bootstrap default ownership, not income or allocation G."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount.core import data
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.tools.v6_init import create_setup, compile_setup, _SetupDumper
from beancount_budget_tool.v6.initialization import initial_pad_postings
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.monthly_report import audit_month_workflow


HEADER = ('option "operating_currency" "CNY"\n'
          '2026-08-01 open Assets:Cash\n2026-08-01 open Assets:Bank\n'
          '2026-08-01 open Equity:Opening\n2026-08-01 open Expenses:Food\n')
PAD = ('2026-09-22 pad Assets:Cash Equity:Opening\n'
       '2026-09-23 balance Assets:Cash 100 CNY\n')
ACTIVITY = ('2026-09-24 * "allocate"\n'
            '  Assets:Cash 20 CNY\n    fund: "生活费"\n'
            '  Assets:Cash -20 CNY\n'
            '2026-09-25 * "food"\n'
            '  Expenses:Food 5 CNY\n'
            '  Assets:Cash -5 CNY\n'
            '2026-09-23 * "move"\n'
            '  Assets:Cash -10 CNY\n'
            '  Assets:Bank 10 CNY\n')


class InitialPadTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "ledger.bean"

    def compile(self, body):
        self.root.write_text(HEADER + body, encoding="utf-8")
        self.original = self.root.read_bytes()
        created = create_setup(self.root, "2026-09", self.base / "review",
                               income_purpose="自由资金", expense_purpose="生活费",
                               expense_category="生活开销")
        setup = Path(created["setup"])
        doc = yaml.safe_load(setup.read_text(encoding="utf-8"))
        doc["defaults"]["confirmed"] = True
        doc["completeness_confirmed"] = True
        doc["expense_targets"] = {"生活开销": "not_assessed"}
        doc["allocation_targets"] = {name: "not_assessed" for name in doc["allocation_targets"]}
        for row in doc["accounts"]:
            row["semantics"] = "H"
            row["reason"] = "virtual cash account"
            row["payment_scope"] = {currency: "payable" for currency in row["payment_scope"]}
            for coordinate in row["opening"]:
                coordinate["owners"] = {"自由资金": coordinate["signed_units"]}
        setup.write_text(yaml.dump(doc, Dumper=_SetupDumper, allow_unicode=True), encoding="utf-8")
        result = compile_setup(setup)
        self.assertEqual(result["status"], "CONFIG_CREATED", result)
        self.config = Path(result["config"])
        return audit_month_workflow(self.config, include_transactions=True)

    def test_midmonth_initial_pad_certifies_without_fund_and_allocates_only_later_transfer(self):
        outcome = self.compile(PAD + ACTIVITY)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        rows = outcome["report"]["transactions"]["rows"]
        initial = next(row for row in rows if row["generated"])
        asset = next(p for p in initial["postings"] if p["account"] == "Assets:Cash")
        self.assertEqual(asset["h_purpose"]["slices"][0]["name"], "自由资金")
        self.assertEqual(initial["net_allocations"], {})
        self.assertIsNone(asset["classification"])
        self.assertTrue(any(row["net_allocations"] for row in rows if row["narration"] == "allocate"))
        self.assertEqual(self.root.read_bytes(), self.original)

    def test_same_day_as_cutover_pad_is_not_counted_twice(self):
        outcome = self.compile(PAD.replace("09-22", "09-01").replace("09-23", "09-02"))
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        rows = outcome["report"]["transactions"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["postings"][0]["signed_units"], "100")
        self.assertEqual(rows[0]["net_allocations"], {})

    def test_explicit_reviewed_purpose_overrides_initial_default_without_creating_g(self):
        self.compile(PAD)
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        path = self.config.parent / config["ownership"]
        owners = yaml.safe_load(path.read_text(encoding="utf-8"))
        living = next(row["id"] for row in owners["owners"] if row["name"] == "生活费")
        fin = load_financial(self.root)
        facts = FactIndex(fin)
        posting = next(p for entry in fin.entries if isinstance(entry, data.Transaction)
                       for p in entry.postings if p.account == "Assets:Cash")
        owners["claims"].append({"claim": facts.for_posting(posting).as_dict(),
                                 "reason": "explicit virtual initial ownership",
                                 "slices": [{"id": "reviewed_initial", "owner_id": living,
                                             "role": "hold", "signed_units": "100"}]})
        path.write_text(yaml.safe_dump(owners, allow_unicode=True), encoding="utf-8")
        outcome = audit_month_workflow(self.config, include_transactions=True)
        self.assertEqual(outcome["status"], "CERTIFIED_SCOPED", outcome["review_items"])
        row = outcome["report"]["transactions"]["rows"][0]
        part = next(p for p in row["postings"] if p["account"] == "Assets:Cash")
        self.assertEqual(part["h_purpose"]["slices"][0]["name"], "生活费")
        self.assertEqual(row["net_allocations"], {})

    def test_later_balance_correction_stays_pending(self):
        correction = ('2026-09-27 pad Assets:Cash Equity:Opening\n'
                      '2026-09-28 balance Assets:Cash 90 CNY\n')
        outcome = self.compile(PAD + ACTIVITY + correction)
        self.assertEqual(outcome["status"], "NEEDS_REVIEW")
        self.assertTrue(any(row["reason_code"] == "PAD_RECONCILIATION_REVIEW"
                            for row in outcome["review_items"]), outcome["review_items"])

    def test_nonzero_and_zero_prior_history_both_prevent_reinitializing(self):
        for amount in ("0", "10"):
            with self.subTest(amount=amount):
                self.root.write_text(HEADER + f'2026-08-30 * "prior"\n  Assets:Cash {amount} CNY\n'
                                     f'  Equity:Opening -{amount} CNY\n' + PAD, encoding="utf-8")
                self.assertEqual(initial_pad_postings(load_financial(self.root), date(2026, 9, 1)), {})

    def test_raw_equity_transaction_is_not_mistaken_for_official_pad(self):
        self.root.write_text(HEADER + '2026-09-22 * "initial-looking"\n'
                             '  Assets:Cash 100 CNY\n  Equity:Opening -100 CNY\n', encoding="utf-8")
        self.assertEqual(initial_pad_postings(load_financial(self.root), date(2026, 9, 1)), {})

    def test_pad_against_another_asset_is_not_initial_free_money(self):
        self.root.write_text(HEADER + PAD.replace("pad Assets:Cash Equity:Opening", "pad Assets:Cash Assets:Bank"),
                             encoding="utf-8")
        self.assertEqual(initial_pad_postings(load_financial(self.root), date(2026, 9, 1)), {})

    def test_new_account_later_in_history_can_initialize_but_old_pad_is_not_replayed(self):
        self.root.write_text(HEADER + PAD.replace("2026-09", "2026-08") +
                             '2026-09-22 pad Assets:Bank Equity:Opening\n'
                             '2026-09-23 balance Assets:Bank 20 CNY\n', encoding="utf-8")
        fin = load_financial(self.root)
        initial = initial_pad_postings(fin, date(2026, 9, 1))
        accounts = [p.account for entry in fin.entries if isinstance(entry, data.Transaction)
                    for p in entry.postings if id(p) in initial]
        self.assertEqual(accounts, ["Assets:Bank"])


if __name__ == "__main__":
    unittest.main()

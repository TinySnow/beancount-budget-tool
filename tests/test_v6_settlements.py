"""Explicit card-repayment bridge cannot become a general negative-H waiver."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml
from beancount import __version__ as BEANCOUNT_VERSION
from beancount.core import data

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial, booked_through
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.ownership import audit_ownership


CUTOVER = date(2026, 10, 1)
BANK = "Assets:Bank"
CARD = "Liabilities:CreditCard"
LEDGER = (
    'option "operating_currency" "CNY"\n'
    '2026-09-01 open Assets:Bank\n'
    '2026-09-01 open Liabilities:CreditCard\n'
    '2026-09-01 open Equity:Opening\n'
    '2026-09-01 open Expenses:Education\n'
    '2026-09-02 * "cash opening"\n'
    '  Assets:Bank 1000 CNY\n'
    '  Equity:Opening -1000 CNY\n'
    '2026-09-11 * "foreign card purchase"\n'
    '  Expenses:Education 21.76 USD\n'
    '  Liabilities:CreditCard -21.76 USD\n'
    '2026-10-02 * "card repayment first"\n'
    '  Liabilities:CreditCard 21.76 USD\n'
    '  Assets:Bank -21.76 USD\n'
    '2026-10-02 * "FX funds repayment"\n'
    '  Assets:Bank 21.76 USD @@ 146.41 CNY\n'
    '  Assets:Bank -146.41 CNY\n'
)


class SameDaySettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main.bean"
        self.review = Path(self.temp.name) / "accounts.yaml"
        self.root.write_text(LEDGER, encoding="utf-8")

    def declaration(self):
        fin = load_financial(self.root)
        facts = FactIndex(fin)
        refs = {}
        for tx in fin.entries:
            if isinstance(tx, data.Transaction) and tx.date >= date(2026, 10, 2):
                for posting in tx.postings:
                    if tx.narration == "card repayment first":
                        role = "liability" if posting.account == CARD else "debit"
                    elif (tx.narration == "FX funds repayment" and
                          posting.account == BANK and posting.units.currency == "USD"):
                        role = "funding"
                    else:
                        continue
                    refs[role] = facts.for_posting(posting).as_dict()
        claim = {"asset_account": BANK, "credit_card_account": CARD,
                 "debit_ref": refs["debit"], "liability_ref": refs["liability"],
                 "funding_ref": refs["funding"], "reason": "先还信用卡，同日购汇精确结清"}
        return {"schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
                "ledger_snapshot": fin.digest,
                "accounts": [{"account": BANK, "semantics": "H",
                              "balance_semantics": "real_inventory",
                              "payment_scope": {"CNY": "payable", "USD": "excluded"},
                              "reason": "可核对银行原生余额"}],
                "credit_card_whitelist": [CARD], "same_day_settlements": [claim]}

    def audit(self, document):
        self.review.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return audit_accounts(self.root, CUTOVER, self.review)

    def test_current_settlement_uses_visible_sources_without_a_complete_index(self):
        from unittest.mock import patch
        document = self.declaration()
        self.root.write_text(LEDGER + '2026-11-02 * "future refund"\n'
                             '  Assets:Bank 1 CNY\n  Expenses:Education -1 CNY\n', encoding="utf-8")
        with booked_through(date(2026, 10, 31)), patch(
                "beancount_budget_tool.v6.settlements.reference_fact_index",
                side_effect=AssertionError("unneeded future index")):
            result = self.audit(document)
        self.assertEqual(len(result["same_day_settlements"]), 1)

    def test_future_settlement_is_checked_but_cannot_enable_a_current_exception(self):
        document = self.declaration()
        with booked_through(date(2026, 10, 1)):
            result = self.audit(document)
            self.assertEqual(result["same_day_settlements"], [])
            document["same_day_settlements"][0]["funding_ref"]["fact_sha256"] = "0" * 64
            with self.assertRaises(BudgetError) as caught:
                self.audit(document)
        self.assertEqual(caught.exception.code, "SETTLEMENT_SOURCE_INVALID")

    def test_exact_whitelisted_card_bridge_closes_at_day_end(self):
        result = self.audit(self.declaration())
        self.assertEqual(result["review_state"], "complete")
        self.assertEqual(len(result["same_day_settlements"]), 1)
        self.assertEqual(result["same_day_settlements"][0]["units"], "544/25")

    def test_owner_inventory_bridge_closes_in_same_owner(self):
        document = self.declaration()
        self.audit(document)
        opening = Path(self.temp.name) / "opening.yaml"
        ownership = Path(self.temp.name) / "ownership.yaml"
        opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": document["ledger_snapshot"],
            "h_holdings": [{"account": BANK, "commodity": "CNY", "lot": "CNY/no-cost",
                            "owner": "-", "units": "1000", "reason": "virtual bank opening"}],
            "k_openings": []}, allow_unicode=True), encoding="utf-8")
        pending = audit_ownership(self.root, CUTOVER, self.review)
        claims = [{"claim": row["ref"], "reason": "reviewed same-day bridge",
                   "slices": [{"id": f"bank_{i}", "owner_id": "-", "role": "hold",
                               "signed_units": row["signed_units"]}]}
                  for i, row in enumerate(pending["missing_h_ownership"])]
        ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": document["ledger_snapshot"],
            "loader_version": BEANCOUNT_VERSION, "owners": [], "claims": claims},
            allow_unicode=True), encoding="utf-8")
        report = audit_h_holdings(self.root, CUTOVER, date(2026, 10, 2),
                                  self.review, opening, ownership)
        self.assertEqual(report["review_state"], "complete")
        cny = next(row for row in report["positions"] if row["commodity"] == "CNY")
        self.assertEqual(cny["owner_units"], [{"owner_id": "-", "units": "85359/100"}])

    def test_negative_asset_without_explicit_claim_is_still_rejected(self):
        document = self.declaration()
        del document["same_day_settlements"]
        del document["credit_card_whitelist"]
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "H_NEGATIVE_INVENTORY")

    def test_card_whitelist_and_source_refs_are_mandatory(self):
        document = self.declaration()
        document["credit_card_whitelist"] = []
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "SETTLEMENT_CLAIM_INVALID")
        document = self.declaration()
        document["same_day_settlements"][0]["funding_ref"]["fact_sha256"] = "0" * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "SETTLEMENT_SOURCE_INVALID")

    def test_next_day_funding_cannot_clear_same_day_bridge(self):
        self.root.write_text(LEDGER.replace('2026-10-02 * "FX funds repayment"',
                                            '2026-10-03 * "FX funds repayment"'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "SETTLEMENT_SHAPE_INVALID")

    def test_unpriced_or_incomplete_funding_is_rejected(self):
        self.root.write_text(LEDGER.replace(
            '  Assets:Bank 21.76 USD @@ 146.41 CNY\n'
            '  Assets:Bank -146.41 CNY\n',
            '  Assets:Bank 21.76 USD\n'
            '  Equity:Opening -21.76 USD\n'
            '  Assets:Bank -146.41 CNY\n'
            '  Equity:Opening 146.41 CNY\n'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "SETTLEMENT_FX_INVALID")

    def test_unrelated_later_negative_usd_is_not_whitelisted(self):
        self.root.write_text(LEDGER +
                             '2026-10-03 * "another USD expense"\n'
                             '  Expenses:Education 1 USD\n'
                             '  Assets:Bank -1 USD\n', encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "H_NEGATIVE_INVENTORY")

    def test_an_intervening_usd_posting_breaks_the_bridge(self):
        self.root.write_text(LEDGER.replace(
            '2026-10-02 * "FX funds repayment"',
            '2026-10-02 * "unrelated USD inflow"\n'
            '  Assets:Bank 1 USD\n'
            '  Equity:Opening -1 USD\n'
            '2026-10-02 * "FX funds repayment"'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "SETTLEMENT_INTERRUPTED")

    def test_whitelisted_card_must_already_owe_the_amount(self):
        self.root.write_text(LEDGER.replace(
            '2026-09-11 * "foreign card purchase"\n'
            '  Expenses:Education 21.76 USD\n'
            '  Liabilities:CreditCard -21.76 USD\n', ''), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "SETTLEMENT_LIABILITY_INVALID")

    def test_partial_funding_cannot_use_whitelist(self):
        self.root.write_text(LEDGER.replace(
            '  Assets:Bank 21.76 USD @@ 146.41 CNY\n'
            '  Assets:Bank -146.41 CNY\n',
            '  Assets:Bank 20.76 USD @@ 140.00 CNY\n'
            '  Assets:Bank -140.00 CNY\n'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "SETTLEMENT_SHAPE_INVALID")


if __name__ == "__main__":
    unittest.main()

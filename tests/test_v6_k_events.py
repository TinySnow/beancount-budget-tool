"""K book-flow intentions require an exact H cash counterpart."""

from datetime import date
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.k_events import audit_k_events
from beancount_budget_tool.v6.k_book_purposes import audit_k_book_purposes
from beancount_budget_tool.v6.ownership import audit_ownership


CUTOVER = date(2026, 9, 1)


class KEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.accounts = folder / "accounts.yml"
        self.ownership = folder / "ownership.yml"
        self.events = folder / "k-events.yml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Assets:FundBook\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-31 * "cash opening"\n'
            '  Assets:Cash 500 CNY\n'
            '  Equity:Opening -500 CNY\n'
            '2026-09-02 * "fund subscription"\n'
            '  Assets:FundBook 200 CNY\n'
            '  Assets:Cash -200 CNY\n', encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": self.snapshot, "accounts": [
                {"account": "Assets:Cash", "semantics": "H",
                 "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                 "reason": "virtual cash"},
                {"account": "Assets:FundBook", "semantics": "K",
                 "balance_semantics": "signed_subscription_flow",
                 "payment_scope": {"CNY": "excluded"}, "reason": "virtual book flow"}]},
            allow_unicode=True), encoding="utf-8")
        missing = audit_ownership(self.root, CUTOVER, self.accounts)["missing_h_ownership"]
        self.cash_ref = missing[0]["ref"]
        self.write_ownership([{"claim": self.cash_ref, "reason": "cash previously protected",
                               "slices": [
                                   {"id": "cash_travel", "owner_id": "travel",
                                    "role": "hold", "signed_units": "-100"},
                                   {"id": "cash_insurance", "owner_id": "insurance",
                                    "role": "hold", "signed_units": "-100"}]}])
        candidate = audit_k_events(self.root, CUTOVER, self.accounts, self.ownership)
        self.k_ref = candidate["events"][0]["k_ref"]

    def write_ownership(self, claims):
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "travel", "name": "旅行", "aliases": []},
                       {"id": "insurance", "name": "保险", "aliases": []}],
            "claims": claims}, allow_unicode=True), encoding="utf-8")

    def declaration(self):
        return {"schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
                "events": [{"k_ref": deepcopy(self.k_ref), "cash_ref": deepcopy(self.cash_ref),
                            "kind": "subscription", "reason": "reviewed mixed purpose purchase",
                            "slices": [
                                {"id": "travel_part", "k_owner_id": "travel",
                                 "cash_owner_id": "travel", "signed_k_units": "100",
                                 "signed_cash_units": "-100", "boundary_role": "preserve"},
                                {"id": "insurance_part", "k_owner_id": "insurance",
                                 "cash_owner_id": "insurance", "signed_k_units": "100",
                                 "signed_cash_units": "-100", "boundary_role": "preserve"}]}]}

    def audit(self, document):
        self.events.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return audit_k_events(self.root, CUTOVER, self.accounts, self.ownership, self.events)

    def test_mixed_subscription_matches_two_h_cash_owner_slices(self):
        report = self.audit(self.declaration())
        self.assertEqual((report["review_state"], report["certified_events"],
                          report["pending_events"]), ("complete", 1, 0))
        self.assertEqual(report["events"][0]["net_allocations"], {})
        self.assertEqual(report["events"][0]["shape"], "simple_cash")

    def test_subscription_then_redemption_validate_each_events_own_direction(self):
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-09-03 * "partial fund redemption"\n'
                             '  Assets:FundBook -100 CNY\n  Assets:Cash 100 CNY\n', encoding="utf-8")
        ownership = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        candidate = audit_k_events(self.root, CUTOVER, self.accounts, self.ownership)["events"][1]
        ownership["claims"].append({"claim": candidate["cash_ref"], "reason": "returned travel cash",
                                    "slices": [{"id": "returned_cash", "owner_id": "travel",
                                                "role": "hold", "signed_units": "100"}]})
        self.write_ownership(ownership["claims"])
        document = self.declaration()
        document["events"].append({"k_ref": candidate["k_ref"], "cash_ref": candidate["cash_ref"],
                                    "kind": "redemption", "reason": "partial return of travel book flow",
                                    "slices": [{"id": "redeemed_travel", "k_owner_id": "travel",
                                                "cash_owner_id": "travel", "signed_k_units": "-100",
                                                "signed_cash_units": "100", "boundary_role": "preserve"}]})
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                if reverse:
                    document["events"].reverse()
                report = self.audit(document)
                self.assertEqual((report["review_state"], report["certified_events"]), ("complete", 2))
                self.assertEqual([row["kind"] for row in report["events"]], ["subscription", "redemption"])
                self.assertTrue(all(row["net_allocations"] == {} for row in report["events"]))

    def test_k_book_projection_reconciles_without_claiming_real_holdings(self):
        self.audit(self.declaration())
        opening = Path(self.temp.name) / "opening.yml"
        opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "h_holdings": [],
            "k_openings": [{"account": "Assets:FundBook", "commodity": "CNY",
                            "lot": "CNY/no-cost", "signed_units": "0",
                            "reason": "reviewed virtual signed book opening"}]},
            allow_unicode=True), encoding="utf-8")
        result = audit_k_book_purposes(self.root, CUTOVER, date(2026, 9, 30),
                                       self.accounts, opening, self.ownership,
                                       self.events)
        self.assertEqual(result["status"], "CERTIFIED")
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual((row["booked_opening"], row["booked_change"],
                          row["booked_end"], row["unreviewed_change"]),
                         ("0", "200", "200", "0"))
        self.assertEqual({part["owner_id"]: part["signed_change"]
                          for part in row["purpose_changes"]},
                         {"insurance": "100", "travel": "100"})
        pending = audit_k_book_purposes(self.root, CUTOVER, date(2026, 9, 30),
                                        self.accounts, opening, self.ownership)
        self.assertEqual(pending["status"], "NEEDS_REVIEW")
        self.assertEqual((pending["rows"][0]["unreviewed_change"],
                          pending["rows"][0]["pending_event_count"]), ("200", 1))

    def test_inline_fund_allocates_from_named_cash_purpose(self):
        source = self.root.read_text(encoding="utf-8").replace(
            '  Assets:FundBook 200 CNY\n  Assets:Cash -200 CNY\n',
            '  Assets:FundBook 200 CNY\n    fund: "保险"\n'
            '  Assets:Cash -200 CNY\n    fund: "旅行"\n')
        self.root.write_text(source, encoding="utf-8")
        snapshot = load_financial(self.root).digest
        account_doc = yaml.safe_load(self.accounts.read_text(encoding="utf-8"))
        account_doc["ledger_snapshot"] = snapshot
        self.accounts.write_text(yaml.safe_dump(account_doc, allow_unicode=True), encoding="utf-8")
        self.snapshot = snapshot
        self.write_ownership([])
        report = audit_k_events(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual(report["events"][0]["status"], "CERTIFIED")
        self.assertEqual(report["events"][0]["net_allocations"],
                         {"insurance": "200", "travel": "-200"})
        self.k_ref = report["events"][0]["k_ref"]
        self.cash_ref = report["events"][0]["cash_ref"]
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "K_EVENT_DOUBLE_CLAIM")

    def test_inline_fund_preserves_mixed_cash_purposes(self):
        source = self.root.read_text(encoding="utf-8").replace(
            '  Assets:FundBook 200 CNY\n  Assets:Cash -200 CNY\n',
            '  Assets:FundBook 200 CNY\n    fund: "旅行100，保险100"\n'
            '  Assets:Cash -200 CNY\n    fund: "旅行100，保险100"\n')
        self.root.write_text(source, encoding="utf-8")
        snapshot = load_financial(self.root).digest
        account_doc = yaml.safe_load(self.accounts.read_text(encoding="utf-8"))
        account_doc["ledger_snapshot"] = snapshot
        self.accounts.write_text(yaml.safe_dump(account_doc, allow_unicode=True), encoding="utf-8")
        self.snapshot = snapshot
        self.write_ownership([])
        report = audit_k_events(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual(report["events"][0]["status"], "CERTIFIED")
        self.assertEqual(report["events"][0]["net_allocations"], {})

    def test_missing_cash_owner_claim_stays_pending(self):
        self.write_ownership([])
        report = self.audit(self.declaration())
        self.assertEqual(report["events"][0]["status"], "NEEDS_REVIEW")
        self.assertIsNone(report["events"][0]["net_allocations"])
        self.assertEqual(report["events"][0]["reason_codes"], ["K_CASH_OWNER_PENDING"])

    def test_wrong_cash_source_or_owner_is_rejected(self):
        document = self.declaration()
        document["events"][0]["cash_ref"]["fact_sha256"] = "0" * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "K_EVENT_CASH_REF_INVALID")
        document = self.declaration()
        document["events"][0]["slices"][0]["cash_owner_id"] = "insurance"
        document["events"][0]["slices"][0]["k_owner_id"] = "insurance"
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "K_EVENT_CASH_OWNER_MISMATCH")

    def test_cash_leg_cannot_claim_allocation_twice(self):
        owner_doc = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        owner_doc["claims"][0]["slices"][0]["role"] = "allocation"
        self.ownership.write_text(yaml.safe_dump(owner_doc, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "K_EVENT_CASH_ROLE_INVALID")

    def test_unreviewed_event_is_not_auto_approved(self):
        report = audit_k_events(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual((report["certified_events"], report["pending_events"]), (0, 1))

    def test_first_allocation_is_explicit_and_counted_once(self):
        self.write_ownership([{"claim": self.cash_ref, "reason": "free cash used",
                               "slices": [{"id": "cash_free", "owner_id": "-",
                                           "role": "hold", "signed_units": "-200"}]}])
        document = self.declaration()
        document["events"][0]["slices"] = [
            {"id": "travel_allocation", "k_owner_id": "travel",
             "cash_owner_id": "-", "signed_k_units": "200",
             "signed_cash_units": "-200", "boundary_role": "allocate"}]
        report = self.audit(document)
        self.assertEqual(report["events"][0]["net_allocations"], {"travel": "200"})

    def test_fee_event_cannot_use_simple_cash_template(self):
        source = self.root.read_text(encoding="utf-8")
        source = source.replace('2026-08-01 open Equity:Opening\n',
                                '2026-08-01 open Equity:Opening\n'
                                '2026-08-01 open Expenses:Fees\n')
        source = source.replace('  Assets:Cash -200 CNY\n',
                                '  Expenses:Fees 5 CNY\n  Assets:Cash -205 CNY\n')
        self.root.write_text(source, encoding="utf-8")
        new_snapshot = load_financial(self.root).digest
        for path in (self.accounts, self.ownership):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["ledger_snapshot"] = new_snapshot
            if path == self.ownership:
                document["claims"] = []
            path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        report = audit_k_events(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual((report["unsupported_events"], report["events"][0]["reason_code"]),
                         (1, "K_EVENT_SHAPE_UNSUPPORTED"))

    def test_reviewed_redemption_releases_purpose_to_free_cash(self):
        source = self.root.read_text(encoding="utf-8")
        source = source.replace('2026-09-02 * "fund subscription"\n'
                                '  Assets:FundBook 200 CNY\n'
                                '  Assets:Cash -200 CNY\n',
                                '2026-09-02 * "fund redemption"\n'
                                '  Assets:FundBook -100 CNY\n'
                                '  Assets:Cash 100 CNY\n')
        self.root.write_text(source, encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        account_doc = yaml.safe_load(self.accounts.read_text(encoding="utf-8"))
        account_doc["ledger_snapshot"] = self.snapshot
        self.accounts.write_text(yaml.safe_dump(account_doc, allow_unicode=True), encoding="utf-8")
        self.write_ownership([])
        self.cash_ref = audit_ownership(self.root, CUTOVER, self.accounts)["missing_h_ownership"][0]["ref"]
        self.write_ownership([{"claim": self.cash_ref, "reason": "reviewed release",
                               "slices": [{"id": "released_cash", "owner_id": "-",
                                           "role": "hold", "signed_units": "100"}]}])
        self.k_ref = audit_k_events(self.root, CUTOVER, self.accounts,
                                    self.ownership)["events"][0]["k_ref"]
        document = self.declaration()
        event = document["events"][0]
        event["kind"] = "redemption"
        event["slices"] = [{"id": "travel_release", "k_owner_id": "travel",
                            "cash_owner_id": "-", "signed_k_units": "-100",
                            "signed_cash_units": "100", "boundary_role": "release"}]
        report = self.audit(document)
        self.assertEqual(report["events"][0]["net_allocations"], {"travel": "-100"})
        opening = Path(self.temp.name) / "opening.yml"
        opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "h_holdings": [],
            "k_openings": [{"account": "Assets:FundBook", "commodity": "CNY",
                            "lot": "CNY/no-cost", "signed_units": "0",
                            "reason": "reviewed virtual signed book opening"}]},
            allow_unicode=True), encoding="utf-8")
        book = audit_k_book_purposes(self.root, CUTOVER, date(2026, 9, 30),
                                     self.accounts, opening, self.ownership,
                                     self.events)
        self.assertEqual((book["rows"][0]["booked_end"],
                          book["rows"][0]["purpose_changes"]),
                         ("-100", [{"owner_id": "travel", "signed_change": "-100"}]))


if __name__ == "__main__":
    unittest.main()

"""A first allocation followed by an investment move counts once."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.cli import main
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.h_transfers import audit_h_transfers
from beancount_budget_tool.v6.k_events import audit_k_events
from beancount_budget_tool.v6.purpose_flows import audit_monthly_purpose_flows
from beancount_budget_tool.v6.purpose_pools import opening_fingerprint


CUTOVER = date(2026, 9, 1)


class PurposeFlowTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.ledger = folder / "main.bean"
        self.accounts = folder / "accounts.yml"
        self.opening = folder / "opening.yml"
        self.ownership = folder / "ownership.yml"
        self.transfers = folder / "transfers.yml"
        self.books = folder / "books.yml"
        self.ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Assets:Wallet\n'
            '2026-08-01 open Assets:FundBook\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 500 CNY\n'
            '  Equity:Opening -500 CNY\n'
            '2026-09-02 * "first allocate"\n'
            '  Assets:Bank -200 CNY\n'
            '  Assets:Wallet 200 CNY\n'
            '2026-09-03 * "move savings to fund book"\n'
            '  Assets:FundBook 200 CNY\n'
            '  Assets:Wallet -200 CNY\n', encoding="utf-8")
        snapshot = load_financial(self.ledger).digest
        self.header = {"schema": 6, "cutover": "2026-09-01",
                       "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION}
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "ledger_snapshot": snapshot,
            "report_currency": "CNY", "accounts": [
                {"account": account, "semantics": semantics,
                 "balance_semantics": balance,
                 "payment_scope": {"CNY": scope}, "reason": "virtual review"}
                for account, semantics, balance, scope in (
                    ("Assets:Bank", "H", "real_inventory", "payable"),
                    ("Assets:Wallet", "H", "real_inventory", "payable"),
                    ("Assets:FundBook", "K", "signed_subscription_flow", "excluded"))]},
            allow_unicode=True), encoding="utf-8")
        self.opening.write_text(yaml.safe_dump({
            "schema": 6, "cutover": "2026-09-01", "ledger_snapshot": snapshot,
            "h_holdings": [{"account": "Assets:Bank", "commodity": "CNY",
                            "lot": "CNY/no-cost", "owner": "-", "units": "500",
                            "reason": "reviewed virtual opening"}],
            "k_openings": []}, allow_unicode=True), encoding="utf-8")
        self.write_ownership([])
        transfer = audit_h_transfers(self.ledger, CUTOVER, self.accounts,
                                     self.ownership)["events"][0]
        book = audit_k_events(self.ledger, CUTOVER, self.accounts,
                              self.ownership)["events"][0]
        self.write_ownership([
            self.claim(transfer["from_ref"], "-", "-200", "out"),
            self.claim(transfer["to_ref"], "savings", "200", "in"),
            self.claim(book["cash_ref"], "savings", "-200", "purchase")])
        self.transfers.write_text(yaml.safe_dump({**self.header, "events": [{
            "from_ref": transfer["from_ref"], "to_ref": transfer["to_ref"],
            "reason": "first formal savings allocation", "slices": [{
                "id": "first", "from_owner_id": "-", "to_owner_id": "savings",
                "signed_from_units": "-200", "signed_to_units": "200",
                "boundary_role": "allocate"}]}]}, allow_unicode=True), encoding="utf-8")
        self.books.write_text(yaml.safe_dump({**self.header, "events": [{
            "k_ref": book["k_ref"], "cash_ref": book["cash_ref"],
            "kind": "subscription", "reason": "savings only changes location",
            "slices": [{"id": "preserve", "k_owner_id": "savings",
                        "cash_owner_id": "savings", "signed_k_units": "200",
                        "signed_cash_units": "-200", "boundary_role": "preserve"}]}]},
            allow_unicode=True), encoding="utf-8")

    def claim(self, ref, owner, amount, ident):
        return {"claim": ref, "reason": "reviewed virtual owner",
                "slices": [{"id": ident, "owner_id": owner,
                            "role": "hold", "signed_units": amount}]}

    def write_ownership(self, claims):
        self.ownership.write_text(yaml.safe_dump({**self.header,
            "owners": [{"id": "savings", "name": "储蓄", "aliases": []}],
            "claims": claims}, allow_unicode=True), encoding="utf-8")

    def audit(self, books=True, *, month="2026-09", include_evidence=False):
        return audit_monthly_purpose_flows(
            self.ledger, CUTOVER, month, self.accounts, self.opening,
            self.ownership,
            self.transfers, self.books if books else None, include_evidence=include_evidence)

    def test_monthly_projection_does_not_index_preceding_month_postings(self):
        indices = []

        def index(fin):
            result = Mock(wraps=FactIndex(fin))
            indices.append((fin, result))
            return result

        with patch("beancount_budget_tool.v6.purpose_flows.FactIndex", side_effect=index):
            report = self.audit(include_evidence=True)
        self.assertEqual(report["status"], "CERTIFIED")
        self.assertEqual(report["net_allocations"], {"savings": "200"})
        self.assertEqual(len(indices), 1)
        fin, facts = indices[0]
        prior = {id(posting) for entry in fin.entries if entry.date < CUTOVER
                 for posting in getattr(entry, "postings", [])}
        self.assertTrue(prior)
        self.assertTrue(facts.for_posting.call_args_list)
        self.assertFalse(any(id(call.args[0]) in prior for call in facts.for_posting.call_args_list))

    def test_empty_next_month_keeps_history_validation_without_borrowing_allocations(self):
        report = self.audit(month="2026-10", include_evidence=True)
        self.assertEqual(report["status"], "CERTIFIED")
        self.assertEqual(report["net_allocations"], {"savings": "0"})
        self.assertEqual(report["known_events"], [])
        self.assertEqual(report["_transaction_evidence"]["ownership"], [])
        document = yaml.safe_load(self.opening.read_text(encoding="utf-8"))
        document["h_holdings"] = []
        self.opening.write_text(yaml.safe_dump(document), encoding="utf-8")
        pending = self.audit(month="2026-10", include_evidence=True)
        self.assertEqual(pending["status"], "NEEDS_REVIEW")
        self.assertIsNone(pending["net_allocations"])
        self.assertIn("H_HOLDINGS_PENDING", [row["reason"] for row in pending["pending_events"]])

    def test_first_allocation_and_following_k_move_count_once(self):
        report = self.audit()
        self.assertEqual(report["status"], "CERTIFIED", report["pending_events"])
        self.assertEqual(report["net_allocations"], {"savings": "200"})
        self.assertEqual(len(report["known_events"]), 2)

    def test_unreviewed_k_move_blocks_total_but_keeps_known_source(self):
        report = self.audit(books=False)
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertIsNone(report["net_allocations"])
        self.assertEqual(report["known_event_allocations"], {"savings": "200"})
        self.assertTrue(report["pending_events"])

    def test_missing_opening_blocks_total_even_when_events_match(self):
        document = yaml.safe_load(self.opening.read_text(encoding="utf-8"))
        document["h_holdings"] = []
        self.opening.write_text(yaml.safe_dump(document), encoding="utf-8")
        report = self.audit()
        self.assertEqual(report["status"], "NEEDS_REVIEW")
        self.assertIsNone(report["net_allocations"])
        self.assertIn("H_HOLDINGS_PENDING",
                      [row["reason"] for row in report["pending_events"]])

    def test_virtual_cli_reports_certified_net_allocation(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--ledger", str(self.ledger), "--cutover", "2026-09-01",
                         "--purpose-flow-audit", "--accounts", str(self.accounts),
                         "--opening", str(self.opening),
                         "--ownership", str(self.ownership),
                         "--h-transfers", str(self.transfers),
                         "--k-events", str(self.books), "--month", "2026-09"])
        report = json.loads(output.getvalue())
        self.assertEqual((code, report["status"], report["net_allocations"]),
                         (0, "CERTIFIED", {"savings": "200"}))


class PooledPurposeFlowTests(unittest.TestCase):
    def test_broker_imported_repo_rollover_needs_no_lot_claims(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        ledger = folder / "main.bean"
        accounts = folder / "accounts.yml"
        opening = folder / "opening.yml"
        ownership = folder / "ownership.yml"
        transfers = folder / "transfers.yml"
        pools = folder / "pools.yml"
        ledger.write_text('option "operating_currency" "CNY"\n'
                          '2026-08-01 open Assets:Bank\n'
                          '2026-08-01 open Assets:Broker:Cash\n'
                          '2026-08-01 open Assets:Broker:Security\n'
                          '2026-08-01 open Equity:Opening\n'
                          '2026-08-01 open Income:Interest\n'
                          '2026-08-31 * "opening"\n'
                          '  Assets:Bank 1000 CNY\n'
                          '  Assets:Broker:Cash 4000 CNY\n'
                          '  Equity:Opening -5000 CNY\n'
                          '2026-09-02 * "first invest"\n'
                          '  Assets:Bank -200 CNY\n'
                          '  Assets:Broker:Cash 200 CNY\n'
                          '2026-09-03 * "repo buy"\n'
                          '  Assets:Broker:Security 30 SEC {100 CNY}\n'
                          '  Assets:Broker:Cash -3000 CNY\n'
                          '2026-09-04 * "repo maturity"\n'
                          '  Assets:Broker:Security -30 SEC {100 CNY}\n'
                          '  Assets:Broker:Cash 3001 CNY\n'
                          '  Income:Interest -1 CNY\n', encoding="utf-8")
        fin = load_financial(ledger)
        snapshot = fin.digest
        account_rows = []
        for name, commodity, scope in (
                ("Assets:Bank", "CNY", "payable"),
                ("Assets:Broker:Cash", "CNY", "excluded"),
                ("Assets:Broker:Security", "SEC", "excluded")):
            account_rows.append({"account": name, "semantics": "H",
                                 "balance_semantics": "real_inventory",
                                 "payment_scope": {commodity: scope},
                                 "reason": "reviewed virtual account"})
        accounts.write_text(yaml.safe_dump({"schema": 6, "cutover": "2026-09-01",
            "ledger_snapshot": snapshot, "report_currency": "CNY", "accounts": account_rows}),
            encoding="utf-8")
        opening.write_text(yaml.safe_dump({"schema": 6, "cutover": "2026-09-01",
            "ledger_snapshot": snapshot,
            "h_holdings": [{"account": "Assets:Bank", "commodity": "CNY",
                            "lot": "CNY/no-cost", "owner": "-", "units": "1000",
                            "reason": "reviewed bank opening"}], "k_openings": []}), encoding="utf-8")
        header = {"schema": 6, "cutover": "2026-09-01",
                  "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION}
        ownership.write_text(yaml.safe_dump({**header,
            "owners": [{"id": "savings", "name": "储蓄", "aliases": []},
                       {"id": "investment", "name": "投资", "aliases": []}],
            "claims": []}, allow_unicode=True), encoding="utf-8")
        transfer = audit_h_transfers(ledger, CUTOVER, accounts, ownership)["events"][0]
        claims = [
            {"claim": transfer["from_ref"], "reason": "bank free funds",
             "slices": [{"id": "bank_out", "owner_id": "-", "role": "hold",
                         "signed_units": "-200"}]},
            {"claim": transfer["to_ref"], "reason": "first investment allocation",
             "slices": [{"id": "broker_in", "owner_id": "investment",
                         "role": "hold", "signed_units": "200"}]}]
        ownership.write_text(yaml.safe_dump({**header,
            "owners": [{"id": "savings", "name": "储蓄", "aliases": []},
                       {"id": "investment", "name": "投资", "aliases": []}],
            "claims": claims}, allow_unicode=True), encoding="utf-8")
        transfers.write_text(yaml.safe_dump({**header, "events": [{
            "from_ref": transfer["from_ref"], "to_ref": transfer["to_ref"],
            "reason": "formal allocation on bank to broker transfer",
            "slices": [{"id": "allocate", "from_owner_id": "-",
                        "to_owner_id": "investment", "signed_from_units": "-200",
                        "signed_to_units": "200", "boundary_role": "allocate"}]}]},
            allow_unicode=True), encoding="utf-8")
        pool_accounts = ["Assets:Broker:Cash", "Assets:Broker:Security"]
        pools.write_text(yaml.safe_dump({"schema": 6, "cutover": "2026-09-01",
            "report_currency": "CNY", "pools": [{
                "id": "broker", "accounts": pool_accounts, "reserve_order": pool_accounts,
                "payment_scope": "excluded",
                "opening_fingerprint": opening_fingerprint(fin, CUTOVER, pool_accounts),
                "protected_owner_id": "savings", "protected_label": "额外储蓄",
                "protected_principal": "3000", "remainder_owner_id": "investment",
                "remainder_label": "待投资金", "reason": "reviewed pool rule"}]},
            allow_unicode=True), encoding="utf-8")
        report = audit_monthly_purpose_flows(
            ledger, CUTOVER, "2026-09", accounts, opening, ownership,
            h_transfers=transfers, purpose_pools=pools)
        self.assertEqual(report["status"], "CERTIFIED", report["pending_events"])
        self.assertEqual(report["net_allocations"], {"savings": "0", "investment": "200"})
        self.assertEqual(report["pool_internal_events"], 2)
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--ledger", str(ledger), "--cutover", "2026-09-01",
                         "--purpose-flow-audit", "--accounts", str(accounts),
                         "--opening", str(opening), "--ownership", str(ownership),
                         "--h-transfers", str(transfers), "--purpose-pools", str(pools),
                         "--month", "2026-09"])
        self.assertEqual((code, json.loads(output.getvalue())["status"]), (0, "CERTIFIED"))
        plans = folder / "purpose-targets.yml"
        plans.write_text('schema: 6\nreport_currency: CNY\n'
                         'months:\n  "2026-09":\n    allocation_targets:\n'
                         '      savings: "300"\n      investment: "100"\n',
                         encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            code = main(["--ledger", str(ledger), "--cutover", "2026-09-01",
                         "--purpose-progress-audit", "--accounts", str(accounts),
                         "--opening", str(opening), "--ownership", str(ownership),
                         "--h-transfers", str(transfers), "--purpose-pools", str(pools),
                         "--plans", str(plans), "--report-currency", "CNY",
                         "--month", "2026-09"])
        progress = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(progress["status"], "CERTIFIED")
        self.assertEqual({row["owner_id"]: row["remaining_q_minus_g"]
                          for row in progress["owners"]},
                         {"savings": "300", "investment": "-100"})
        ledger.write_text(ledger.read_text(encoding="utf-8") +
                          '2026-09-05 * "unreviewed withdrawal"\n'
                          '  Assets:Broker:Cash -100 CNY\n'
                          '  Assets:Bank 100 CNY\n', encoding="utf-8")
        new_snapshot = load_financial(ledger).digest
        for sidecar in (accounts, opening, ownership, transfers):
            document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            document["ledger_snapshot"] = new_snapshot
            sidecar.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        pending = audit_monthly_purpose_flows(
            ledger, CUTOVER, "2026-09", accounts, opening, ownership,
            h_transfers=transfers, purpose_pools=pools)
        self.assertEqual(pending["status"], "NEEDS_REVIEW")
        self.assertIsNone(pending["net_allocations"])
        self.assertIn("POOL_BOUNDARY_PENDING",
                      [row["reason"] for row in pending["pending_events"]])


if __name__ == "__main__":
    unittest.main()

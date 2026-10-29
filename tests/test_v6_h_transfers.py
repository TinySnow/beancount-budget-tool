"""Simple H transfers need two booked legs and matching owner evidence."""

from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial, booked_through
from beancount_budget_tool.v6.h_transfers import audit_h_transfers


CUTOVER = date(2026, 9, 1)


class HTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.accounts = folder / "accounts.yml"
        self.ownership = folder / "ownership.yml"
        self.events = folder / "transfers.yml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Assets:Wallet\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 500 CNY\n'
            '  Equity:Opening -500 CNY\n'
            '2026-09-02 * "transfer"\n'
            '  Assets:Bank -200 CNY\n'
            '  Assets:Wallet 200 CNY\n', encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": self.snapshot, "accounts": [
                {"account": account, "semantics": "H",
                 "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                 "reason": "reviewed virtual cash"}
                for account in ("Assets:Bank", "Assets:Wallet")]},
            allow_unicode=True), encoding="utf-8")
        self.write_ownership([])
        candidate = audit_h_transfers(self.root, CUTOVER, self.accounts, self.ownership)["events"][0]
        self.from_ref = candidate["from_ref"]
        self.to_ref = candidate["to_ref"]

    def write_ownership(self, claims, owners=None):
        if owners is None:
            owners = [{"id": "savings", "name": "储蓄", "aliases": []}]
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
            "owners": owners, "claims": claims}, allow_unicode=True), encoding="utf-8")

    def set_owner_legs(self, from_owner, to_owner, from_role="hold", to_role="hold"):
        self.write_ownership([
            {"claim": deepcopy(self.from_ref), "reason": "reviewed source",
             "slices": [{"id": "source", "owner_id": from_owner,
                         "role": from_role, "signed_units": "-200"}]},
            {"claim": deepcopy(self.to_ref), "reason": "reviewed destination",
             "slices": [{"id": "destination", "owner_id": to_owner,
                         "role": to_role, "signed_units": "200"}]},
        ])

    def declaration(self, source="-", target="-", role="preserve"):
        return {"schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
                "events": [{"from_ref": deepcopy(self.from_ref), "to_ref": deepcopy(self.to_ref),
                            "reason": "reviewed virtual transfer", "slices": [
                                {"id": "move", "from_owner_id": source,
                                 "to_owner_id": target, "signed_from_units": "-200",
                                 "signed_to_units": "200", "boundary_role": role}]}]}

    def audit(self, document):
        self.events.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return audit_h_transfers(self.root, CUTOVER, self.accounts,
                                 self.ownership, self.events)

    def test_future_decision_keeps_exact_counterpart_validation(self):
        self.set_owner_legs("-", "-")
        document = self.declaration()
        with booked_through(date(2026, 9, 1)):
            result = self.audit(document)
            self.assertEqual(result["events"], [])
            document["events"][0]["to_ref"]["fact_sha256"] = "0" * 64
            with self.assertRaises(BudgetError) as caught:
                self.audit(document)
        self.assertEqual(caught.exception.code, "H_TRANSFER_TO_REF_INVALID")

    def test_free_transfer_preserves_purpose_and_no_allocation(self):
        self.set_owner_legs("-", "-")
        report = self.audit(self.declaration())
        self.assertEqual((report["review_state"], report["certified_events"]), ("complete", 1))
        self.assertEqual(report["events"][0]["net_allocations"], {})

    def test_first_savings_allocation_and_release_have_opposite_g(self):
        self.set_owner_legs("-", "savings")
        report = self.audit(self.declaration("-", "savings", "allocate"))
        self.assertEqual(report["events"][0]["net_allocations"], {"savings": "200"})
        self.set_owner_legs("savings", "-")
        report = self.audit(self.declaration("savings", "-", "release"))
        self.assertEqual(report["events"][0]["net_allocations"], {"savings": "-200"})

    def test_unreviewed_event_and_missing_h_claim_stay_pending(self):
        report = audit_h_transfers(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual((report["pending_events"], report["certified_events"]), (1, 0))
        report = self.audit(self.declaration())
        self.assertEqual(report["events"][0]["reason_codes"], ["H_TRANSFER_OWNER_PENDING"])
        self.assertIsNone(report["events"][0]["net_allocations"])

    def foreign_legs(self, source='储蓄', target='储蓄', price='', commodity='JPY'):
        text = ('option "operating_currency" "CNY"\n2026-08-01 open Assets:Bank\n'
                '2026-08-01 open Assets:Wallet\n2026-08-01 open Equity:Opening\n'
                '2026-08-31 * "opening"\n  Assets:Bank 500 JPY\n  Equity:Opening -500 JPY\n'
                f'2026-09-02 * "foreign move"\n  Assets:Bank -200 JPY{price}\n' +
                (f'    fund: "{source}"\n' if source is not None else '') +
                f'  Assets:Wallet 200 {commodity}{price}\n' +
                (f'    fund: "{target}"\n' if target is not None else ''))
        self.root.write_text(text, encoding='utf-8')
        snapshot = load_financial(self.root).digest
        accounts = yaml.safe_load(self.accounts.read_text(encoding='utf-8'))
        accounts['ledger_snapshot'] = snapshot
        for row in accounts['accounts']:
            row['payment_scope'] = {'JPY' if row['account']=='Assets:Bank' else commodity: 'excluded'}
        self.accounts.write_text(yaml.safe_dump(accounts, allow_unicode=True), encoding='utf-8')
        self.snapshot = snapshot
        self.write_ownership([], owners=[{'id':'savings','name':'储蓄','aliases':[]},
                                         {'id':'travel','name':'旅游','aliases':[]}])

    def test_foreign_single_inline_fund_preserves_native_units_with_zero_g(self):
        for fund in ('储蓄', '-'):
            with self.subTest(fund=fund):
                self.foreign_legs(fund, fund)
                before = {p:p.read_bytes() for p in (self.root,self.accounts,self.ownership)}
                result = audit_h_transfers(self.root, CUTOVER, self.accounts, self.ownership)
                event = result['events'][0]
                self.assertEqual((event['shape'],event['status'],event['commodity']), ('foreign_preserve','CERTIFIED','JPY'))
                self.assertEqual(event['net_allocations'], {})
                self.assertIsNone(result['review_sha256'])
                self.assertEqual(event['signed_from_units'], '-200')
                self.assertEqual(event['signed_to_units'], '200')
                self.assertTrue(all(p.read_bytes()==body for p,body in before.items()))
                self.assertFalse(self.events.exists())

    def test_foreign_missing_mixed_or_remapped_purposes_are_not_cny_allocations(self):
        for source,target in [(None,'储蓄'),('储蓄',None),('储蓄','旅游'),
                              ('储蓄100，旅游100','储蓄100，旅游100')]:
            with self.subTest(source=source,target=target):
                self.foreign_legs(source,target)
                event = audit_h_transfers(self.root,CUTOVER,self.accounts,self.ownership)['events'][0]
                self.assertNotEqual(event['status'],'CERTIFIED')
                self.assertIsNone(event['net_allocations'])
                self.assertEqual(event['reason_codes'],['H_TRANSFER_FOREIGN_PURPOSE_UNSUPPORTED'])

    def test_foreign_price_annotation_is_not_a_plain_native_move(self):
        self.foreign_legs(price=' @@ 10 CNY')
        event = audit_h_transfers(self.root,CUTOVER,self.accounts,self.ownership)['events'][0]
        self.assertEqual(event['reason_codes'],['H_TRANSFER_SHAPE_UNSUPPORTED'])
        self.assertNotEqual(event['status'],'CERTIFIED')

    def test_foreign_external_event_cannot_authorize_native_g_as_report_g(self):
        self.foreign_legs(None,None)
        candidate = audit_h_transfers(self.root,CUTOVER,self.accounts,self.ownership)['events'][0]
        self.from_ref,self.to_ref = candidate['from_ref'],candidate['to_ref']
        self.set_owner_legs('savings','savings')
        self.assertEqual(audit_h_transfers(self.root,CUTOVER,self.accounts,self.ownership)['certified_events'], 0)
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration('savings','travel','reallocate'))
        self.assertEqual(caught.exception.code,'H_TRANSFER_UNSUPPORTED')

    def test_wrong_source_owner_or_boundary_role_is_rejected(self):
        self.set_owner_legs("-", "-")
        document = self.declaration()
        document["events"][0]["to_ref"]["fact_sha256"] = "0" * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "H_TRANSFER_TO_REF_INVALID")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration("-", "savings", "allocate"))
        self.assertEqual(caught.exception.code, "H_TRANSFER_OWNER_MISMATCH")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration("savings", "-", "preserve"))
        self.assertEqual(caught.exception.code, "H_TRANSFER_ROLE_INVALID")

    def test_h_leg_cannot_claim_allocation_again(self):
        self.set_owner_legs("-", "savings", to_role="allocation")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration("-", "savings", "allocate"))
        self.assertEqual(caught.exception.code, "H_TRANSFER_OWNER_ROLE_INVALID")

    def test_foreign_exchange_is_unsupported(self):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Assets:Dollars\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 500 CNY\n'
            '  Equity:Opening -500 CNY\n'
            '2026-09-02 * "exchange"\n'
            '  Assets:Bank -140 CNY\n'
            '  Assets:Dollars 20 USD @ 7 CNY\n', encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        document = yaml.safe_load(self.accounts.read_text(encoding="utf-8"))
        document["ledger_snapshot"] = self.snapshot
        document["accounts"][1]["account"] = "Assets:Dollars"
        document["accounts"][1]["payment_scope"] = {"USD": "excluded"}
        self.accounts.write_text(yaml.safe_dump(document), encoding="utf-8")
        self.write_ownership([])
        report = audit_h_transfers(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual((report["unsupported_events"], report["events"][0]["shape"]),
                         (1, "unsupported"))


if __name__ == "__main__":
    unittest.main()

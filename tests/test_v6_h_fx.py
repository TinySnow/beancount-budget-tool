"""A simple FX cash move needs an original total and unchanged purpose."""

from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial, booked_through
from beancount_budget_tool.v6.h_fx import audit_h_fx


CUTOVER = date(2026, 9, 1)


class HFXTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.accounts = folder / "accounts.yml"
        self.ownership = folder / "ownership.yml"
        self.events = folder / "fx.yml"
        self.write_ledger("@@ 140 CNY")

    def write_ledger(self, price):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:CNY\n'
            '2026-08-01 open Assets:USD\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-31 * "opening"\n'
            '  Assets:CNY 500 CNY\n'
            '  Equity:Opening -500 CNY\n'
            '2026-09-02 * "buy dollars"\n'
            f'  Assets:USD 20 USD {price}\n'
            '  Assets:CNY -140 CNY\n', encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": self.snapshot, "accounts": [
                {"account": name, "semantics": "H", "balance_semantics": "real_inventory",
                 "payment_scope": {commodity: "payable" if commodity == "CNY" else "excluded"},
                 "reason": "reviewed virtual cash"}
                for name, commodity in (("Assets:CNY", "CNY"), ("Assets:USD", "USD"))]},
            allow_unicode=True), encoding="utf-8")
        self.write_ownership([])
        candidate = audit_h_fx(self.root, CUTOVER, self.accounts, self.ownership)["events"][0]
        self.from_ref = candidate["from_ref"]
        self.to_ref = candidate["to_ref"]

    def write_ownership(self, claims):
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "travel", "name": "旅游", "aliases": []}],
            "claims": claims}, allow_unicode=True), encoding="utf-8")

    def set_legs(self, from_owner="-", to_owner="-"):
        self.write_ownership([
            {"claim": deepcopy(self.from_ref), "reason": "reviewed FX source",
             "slices": [{"id": "fx_from", "owner_id": from_owner,
                         "role": "hold", "signed_units": "-140"}]},
            {"claim": deepcopy(self.to_ref), "reason": "reviewed FX destination",
             "slices": [{"id": "fx_to", "owner_id": to_owner,
                         "role": "hold", "signed_units": "20"}]},
        ])

    def declaration(self, owner="-"):
        return {"schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
                "events": [{"from_ref": deepcopy(self.from_ref), "to_ref": deepcopy(self.to_ref),
                            "owner_id": owner, "signed_from_units": "-140",
                            "signed_to_units": "20", "boundary_role": "preserve",
                            "reason": "reviewed exact-total currency purchase"}]}

    def audit(self, document):
        self.events.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return audit_h_fx(self.root, CUTOVER, self.accounts, self.ownership, self.events)

    def test_future_decision_keeps_exact_counterpart_validation(self):
        self.set_legs()
        document = self.declaration()
        with booked_through(date(2026, 9, 1)):
            result = self.audit(document)
            self.assertEqual(result["events"], [])
            document["events"][0]["to_ref"]["fact_sha256"] = "0" * 64
            with self.assertRaises(BudgetError) as caught:
                self.audit(document)
        self.assertEqual(caught.exception.code, "H_FX_TO_REF_INVALID")

    def test_exact_total_free_cash_purchase_is_certified_without_g(self):
        self.set_legs()
        report = self.audit(self.declaration())
        self.assertEqual((report["review_state"], report["certified_events"]), ("complete", 1))
        self.assertEqual(report["events"][0]["quoted_total"], "140")
        self.assertEqual(report["events"][0]["net_allocations"], {})

    def test_missing_or_conflicting_ownership_cannot_certify(self):
        report = self.audit(self.declaration())
        self.assertEqual(report["events"][0]["reason_code"], "H_FX_OWNER_PENDING")
        self.set_legs("-", "travel")
        with self.assertRaises(BudgetError) as caught:
            self.audit(self.declaration())
        self.assertEqual(caught.exception.code, "H_FX_OWNER_MISMATCH")

    def test_wrong_source_or_amount_is_rejected(self):
        self.set_legs()
        document = self.declaration()
        document["events"][0]["to_ref"]["fact_sha256"] = "0" * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "H_FX_TO_REF_INVALID")
        document = self.declaration()
        document["events"][0]["signed_from_units"] = "-139"
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "H_FX_UNITS_MISMATCH")

    def test_unit_price_does_not_satisfy_exact_total_protocol(self):
        self.write_ledger("@ 7 CNY")
        report = audit_h_fx(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual((report["unsupported_events"], report["events"][0]["shape"]),
                         (1, "unsupported"))


if __name__ == "__main__":
    unittest.main()

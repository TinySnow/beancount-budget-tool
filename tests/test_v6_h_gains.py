"""An H gain must have a booked Income source of the same amount."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.h_gains import audit_h_income_gains
from beancount_budget_tool.v6.ownership import audit_ownership


CUTOVER = date(2026, 9, 1)


class HGainTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.accounts = folder / "accounts.yml"
        self.ownership = folder / "ownership.yml"
        self.write_ledger("Income:Interest", "-1")

    def write_ledger(self, counterpart, counterpart_units):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Reserve\n'
            '2026-08-01 open Equity:Opening\n'
            f'2026-08-01 open {counterpart}\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Reserve 100 CNY\n'
            '  Equity:Opening -100 CNY\n'
            '2026-09-02 * "reported yield"\n'
            '  Assets:Reserve 1 CNY\n'
            f'  {counterpart} {counterpart_units} CNY\n', encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        self.accounts.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "ledger_snapshot": self.snapshot, "accounts": [
                {"account": "Assets:Reserve", "semantics": "H",
                 "balance_semantics": "real_inventory", "payment_scope": {"CNY": "excluded"},
                 "reason": "reviewed virtual reserve"}]},
            allow_unicode=True), encoding="utf-8")
        self.write_ownership()

    def write_ownership(self, slices=None):
        missing = audit_ownership(self.root, CUTOVER, self.accounts)["missing_h_ownership"]
        assert len(missing) == 1
        self.ownership.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(),
            "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "emergency", "name": "应急", "aliases": []}],
            "claims": [{"claim": missing[0]["ref"], "reason": "reviewed interest",
                        "slices": slices if slices is not None else [
                            {"id": "interest", "owner_id": "emergency",
                             "role": "gain", "signed_units": "1"}]}]},
            allow_unicode=True), encoding="utf-8")

    def test_income_gain_is_certified_without_allocation(self):
        report = audit_h_income_gains(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual((report["review_state"], report["certified_events"],
                          report["unsupported_events"]), ("complete", 1, 0))
        self.assertIsNotNone(report["events"][0]["income_ref"])
        self.assertEqual(report["events"][0]["owner_id"], "emergency")

    def test_expense_refund_cannot_masquerade_as_income_gain(self):
        self.write_ledger("Expenses:Refund", "-1")
        report = audit_h_income_gains(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual((report["review_state"], report["unsupported_events"]),
                         ("pending", 1))
        self.assertIsNone(report["events"][0]["income_ref"])

    def test_mixed_hold_and_gain_slices_need_separate_protocol(self):
        self.write_ownership([
            {"id": "part_gain", "owner_id": "emergency", "role": "gain",
             "signed_units": "1/2"},
            {"id": "part_hold", "owner_id": "emergency", "role": "hold",
             "signed_units": "1/2"},
        ])
        report = audit_h_income_gains(self.root, CUTOVER, self.accounts, self.ownership)
        self.assertEqual(report["events"][0]["reason_code"], "H_GAIN_SHAPE_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()

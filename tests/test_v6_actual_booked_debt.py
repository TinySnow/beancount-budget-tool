"""A reviewed CNY card balance reduces actual spendable exactly once."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.actual_snapshot import audit_actual_snapshot
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.purpose_flows import audit_monthly_purpose_flows


CUTOVER = date(2026, 9, 1)


class BookedDebtSnapshotTests(unittest.TestCase):
    def _fixture(self, base: Path, *, currency="CNY", owner="free", payment=True):
        ledger = base / "main.bean"
        ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank\n'
            '2026-08-01 open Liabilities:Card\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Bank 1000 CNY\n'
            '  Equity:Opening -1000 CNY\n'
            '2026-09-02 * "card purchase"\n'
            f'  Expenses:Food 300 {currency}\n'
            f'  Liabilities:Card -300 {currency}\n' +
            ('2026-09-20 * "card payment"\n'
             '  Assets:Bank -100 CNY\n'
             '  Liabilities:Card 100 CNY\n' if payment and currency == "CNY" else ''),
            encoding="utf-8")
        snapshot = load_financial(ledger).digest

        def save(name, value):
            path = base / name
            path.write_text(yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")
            return path

        accounts = save("accounts.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "report_currency": "CNY", "accounts": [{
                "account": "Assets:Bank", "semantics": "H",
                "balance_semantics": "real_inventory", "payment_scope": {"CNY": "payable"},
                "reason": "reviewed bank"}]})
        opening = save("opening.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "h_holdings": [{"account": "Assets:Bank", "commodity": "CNY",
                            "lot": "CNY/no-cost", "owner": "free", "units": "1000",
                            "reason": "reviewed opening"}], "k_openings": []})
        missing = audit_ownership(ledger, CUTOVER, accounts)["missing_h_ownership"]
        ownership = save("ownership.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "free", "name": "自由资金", "aliases": []},
                       {"id": "savings", "name": "储蓄", "aliases": []}],
            "defaults": {"income_owner_id": "free", "expense_owner_id": "free"},
            "claims": [{"claim": row["ref"], "reason": "reviewed card payment",
                        "slices": [{"id": "card-payment", "owner_id": "free",
                                    "role": "hold", "signed_units": "-100"}]}
                       for row in missing]})
        events = audit_simple_obligations(ledger, CUTOVER)["events"]
        obligations = save("obligations.yaml", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": snapshot,
            "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": "free", "name": "自由资金", "aliases": []},
                       {"id": "savings", "name": "储蓄", "aliases": []}],
            "events": [{"liability_ref": row["liability_ref"],
                        "counterpart_ref": row["counterpart_ref"],
                        "kind": "consumer_purchase" if row["shape"] == "consumer_purchase"
                        else "payment", "obligation_id": "card_purchase",
                        **({"owner_id": owner} if row["shape"] == "consumer_purchase" else {}),
                        "reason": "reviewed card transaction"}
                       for row in events]})
        return ledger, accounts, opening, ownership, obligations, snapshot

    def _audit(self, base: Path, paths: tuple, as_of: date):
        ledger, accounts, opening, ownership, obligations, snapshot = paths
        completeness = base / "completeness.yaml"
        completeness.write_text(yaml.safe_dump({
            "schema": 6, "cutover": CUTOVER.isoformat(), "as_of": as_of.isoformat(),
            "ledger_snapshot": snapshot, "unbooked_obligations": "none",
            "inflight_payments": "none", "reason": "reviewed date end"}), encoding="utf-8")
        return audit_actual_snapshot(ledger, CUTOVER, as_of, accounts, opening,
                                     ownership, obligations, completeness)

    def test_purchase_and_partial_payment_keep_same_actual(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            paths = self._fixture(base)
            on_purchase = self._audit(base, paths, date(2026, 9, 2))
            self.assertEqual((on_purchase["status"], on_purchase["actual_spendable"]),
                             ("CERTIFIED", "700"))
            self.assertEqual(on_purchase["booked_debts"][0]["remaining_native"], "300")
            after_payment = self._audit(base, paths, date(2026, 9, 30))
            self.assertEqual((after_payment["status"], after_payment["actual_spendable"]),
                             ("CERTIFIED", "700"))
            self.assertEqual(after_payment["booked_debts"][0]["remaining_native"], "200")

    def test_foreign_debt_and_protected_debt_stay_pending(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            foreign = self._audit(base, self._fixture(base, currency="USD", payment=False),
                                  date(2026, 9, 30))
            self.assertEqual(foreign["status"], "NEEDS_REVIEW")
            self.assertIn("SETTLEMENT_VALUE_MISSING", foreign["reason_codes"])
            protected = self._audit(base, self._fixture(base, owner="savings"),
                                    date(2026, 9, 30))
            self.assertEqual(protected["status"], "NEEDS_REVIEW")
            self.assertIn("DEBT_COVERAGE_PENDING", protected["reason_codes"])

    def test_reviewed_card_payment_is_not_a_new_purpose_allocation(self):
        with TemporaryDirectory() as folder:
            ledger, accounts, opening, ownership, obligations, _ = self._fixture(Path(folder))
            without_review = audit_monthly_purpose_flows(
                ledger, CUTOVER, "2026-09", accounts, opening, ownership)
            self.assertEqual(without_review["status"], "NEEDS_REVIEW")
            self.assertIn("NAMED_H_EVENT_UNSUPPORTED",
                          [row["reason"] for row in without_review["pending_events"]])
            self.assertTrue(any(row["source"].startswith(str(ledger.resolve()) + ":")
                                for row in without_review["pending_events"]))

            reviewed = audit_monthly_purpose_flows(
                ledger, CUTOVER, "2026-09", accounts, opening, ownership,
                obligations=obligations)
            self.assertEqual(reviewed["status"], "CERTIFIED", reviewed["pending_events"])
            self.assertEqual(reviewed["net_allocations"], {"free": "0", "savings": "0"})
            self.assertEqual([row["kind"] for row in reviewed["known_events"]],
                             ["liability_payment"])


if __name__ == "__main__":
    unittest.main()

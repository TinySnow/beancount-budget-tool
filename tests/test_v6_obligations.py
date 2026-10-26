"""Simple booked credit responsibilities must be individually source bound."""

from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import load_financial, booked_through
from beancount_budget_tool.v6.obligations import audit_simple_obligations


CUTOVER = date(2026, 9, 1)


class SimpleObligationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        self.root = folder / "main.bean"
        self.declarations = folder / "obligations.yml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Liabilities:Card\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-09-02 * "lunch on card"\n'
            '  Expenses:Food 300 CNY\n'
            '  Liabilities:Card -300 CNY\n'
            '2026-09-03 * "partial repayment"\n'
            '  Liabilities:Card 100 CNY\n'
            '  Assets:Cash -100 CNY\n'
            '2026-09-04 * "last repayment"\n'
            '  Liabilities:Card 200 CNY\n'
            '  Assets:Cash -200 CNY\n', encoding="utf-8")
        self.snapshot = load_financial(self.root).digest
        self.candidates = audit_simple_obligations(self.root, CUTOVER)["events"]

    def document(self):
        events = []
        for row in self.candidates:
            claim = {"liability_ref": deepcopy(row["liability_ref"]),
                     "counterpart_ref": deepcopy(row["counterpart_ref"]),
                     "kind": ("consumer_purchase" if row["shape"] == "consumer_purchase" else "payment"),
                     "obligation_id": "lunch",
                     "reason": "reviewed purchase and settlement"}
            if row["shape"] == "consumer_purchase":
                claim["owner_id"] = "living"
            events.append(claim)
        return {"schema": 6, "cutover": CUTOVER.isoformat(),
                "ledger_snapshot": self.snapshot, "loader_version": BEANCOUNT_VERSION,
                "owners": [{"id": "living", "name": "生活费", "aliases": []}],
                "events": events}

    def audit(self, document):
        self.declarations.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        return audit_simple_obligations(self.root, CUTOVER, self.declarations)

    def test_future_payment_counterpart_is_checked_with_and_without_bounded_facts(self):
        from contextlib import nullcontext
        for bounded in (False, True):
            with self.subTest(bounded=bounded), (booked_through(date(2026, 9, 2)) if bounded else nullcontext()):
                document = self.document()
                self.declarations.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
                result = audit_simple_obligations(self.root, CUTOVER, self.declarations, date(2026, 9, 2))
                self.assertEqual(result["obligations"][0]["remaining_native"], "300")
                document["events"][1]["counterpart_ref"] = document["events"][2]["counterpart_ref"]
                self.declarations.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
                with self.assertRaises(BudgetError) as caught:
                    audit_simple_obligations(self.root, CUTOVER, self.declarations, date(2026, 9, 2))
                self.assertEqual(caught.exception.code, "OBLIGATION_COUNTERPART_INVALID")

    def test_purchase_then_partial_and_full_payment_replay(self):
        report = self.audit(self.document())
        self.assertEqual((report["review_state"], report["certified_events"],
                          report["pending_events"]), ("complete", 3, 0))
        self.assertEqual(report["obligations"][0]["remaining_native"], "0")
        self.assertEqual(report["obligations"][0]["owner_id"], "living")

    def test_as_of_replays_only_events_booked_by_that_day(self):
        self.audit(self.document())
        on_purchase = audit_simple_obligations(self.root, CUTOVER, self.declarations,
                                               date(2026, 9, 2))
        after_partial = audit_simple_obligations(self.root, CUTOVER, self.declarations,
                                                 date(2026, 9, 3))
        self.assertEqual((len(on_purchase["events"]),
                          on_purchase["obligations"][0]["remaining_native"]), (1, "300"))
        self.assertEqual((len(after_partial["events"]),
                          after_partial["obligations"][0]["remaining_native"]), (2, "200"))

    def test_later_import_does_not_expire_reviewed_card_events(self):
        self.audit(self.document())
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-10-02 * "new card purchase"\n'
                             '  Expenses:Food 5 CNY\n'
                             '  Liabilities:Card -5 CNY\n', encoding="utf-8")
        old = audit_simple_obligations(self.root, CUTOVER, self.declarations,
                                       date(2026, 9, 30))
        self.assertEqual((old["review_state"], old["certified_events"]),
                         ("complete", 3))
        current = audit_simple_obligations(self.root, CUTOVER, self.declarations)
        self.assertEqual((current["review_state"], current["pending_events"]),
                         ("pending", 1))

    def test_changed_reviewed_card_posting_is_rejected_after_append(self):
        self.audit(self.document())
        source = self.root.read_text(encoding="utf-8")
        self.root.write_text(source.replace("Expenses:Food 300 CNY", "Expenses:Food 301 CNY")
                             .replace("Liabilities:Card -300 CNY", "Liabilities:Card -301 CNY"),
                             encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            audit_simple_obligations(self.root, CUTOVER, self.declarations)
        self.assertEqual(caught.exception.code, "OBLIGATION_REF_INVALID")

    def test_unclaimed_payment_keeps_booked_domain_pending(self):
        document = self.document()
        document["events"].pop()
        report = self.audit(document)
        self.assertEqual((report["review_state"], report["pending_events"]), ("pending", 1))
        self.assertEqual(report["obligations"][0]["remaining_native"], "200")

    def test_wrong_counterpart_and_duplicate_claim_rejected(self):
        document = self.document()
        document["events"][0]["counterpart_ref"]["fact_sha256"] = "0" * 64
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "OBLIGATION_COUNTERPART_INVALID")
        document = self.document()
        document["events"].append(deepcopy(document["events"][0]))
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "DUPLICATE_OBLIGATION_CLAIM")

    def test_payment_cannot_exceed_or_precede_its_obligation(self):
        document = self.document()
        document["events"][1]["obligation_id"] = "other"
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "OBLIGATION_SETTLEMENT_UNKNOWN")
        document = self.document()
        document["events"].pop(1)
        document["events"][1]["obligation_id"] = "lunch"
        # 200 is still within the original 300; make the payment exceed it.
        self.root.write_text(self.root.read_text(encoding="utf-8").replace(
            'Liabilities:Card 200 CNY', 'Liabilities:Card 400 CNY').replace(
            'Assets:Cash -200 CNY', 'Assets:Cash -400 CNY'), encoding="utf-8")
        report = audit_simple_obligations(self.root, CUTOVER)
        document["ledger_snapshot"] = load_financial(self.root).digest
        document["events"][0]["liability_ref"] = report["events"][0]["liability_ref"]
        document["events"][0]["counterpart_ref"] = report["events"][0]["counterpart_ref"]
        document["events"][1]["liability_ref"] = report["events"][2]["liability_ref"]
        document["events"][1]["counterpart_ref"] = report["events"][2]["counterpart_ref"]
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "OBLIGATION_SETTLEMENT_EXCESS")

    def test_nonzero_opening_stays_pending(self):
        source = self.root.read_text(encoding="utf-8")
        source = source.replace('2026-09-02 *',
            '2026-08-31 * "old purchase"\n'
            '  Expenses:Food 20 CNY\n'
            '  Liabilities:Card -20 CNY\n'
            '2026-09-02 *')
        self.root.write_text(source, encoding="utf-8")
        report = audit_simple_obligations(self.root, CUTOVER)
        self.assertEqual(report["opening_pending"][0]["signed_units"], "-20")
        self.assertEqual(report["review_state"], "pending")

    def test_reviewed_third_party_advance_is_not_personal_debt(self):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Liabilities:Clearing\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-09-08 * "pay for employer"\n'
            '  Liabilities:Clearing 500 CNY\n'
            '  Assets:Cash -500 CNY\n'
            '2026-09-20 * "employer reimburses"\n'
            '  Assets:Cash 500 CNY\n'
            '  Liabilities:Clearing -500 CNY\n', encoding="utf-8")
        candidates = audit_simple_obligations(self.root, CUTOVER)["events"]
        document = {"schema": 6, "cutover": CUTOVER.isoformat(),
                    "ledger_snapshot": load_financial(self.root).digest,
                    "loader_version": BEANCOUNT_VERSION, "owners": [],
                    "events": [{"liability_ref": row["liability_ref"],
                                "counterpart_ref": row["counterpart_ref"],
                                "kind": ("advance_outflow" if row["shape"] == "asset_outflow"
                                         else "advance_reimbursement"),
                                "clearing_id": "employer_phone", "reason": "reviewed employer advance"}
                               for row in candidates]}
        on_outflow = self.audit(document)
        self.assertEqual((on_outflow["review_state"], on_outflow["certified_events"],
                          on_outflow["obligations"]), ("complete", 2, []))
        self.assertEqual(on_outflow["clearing_advances"][0]["remaining_native"], "0")
        at_midmonth = audit_simple_obligations(self.root, CUTOVER, self.declarations,
                                                date(2026, 9, 15))
        self.assertEqual(at_midmonth["clearing_advances"][0]["remaining_native"], "500")
        self.assertEqual(at_midmonth["obligations"], [])

    def test_advance_reimbursement_cannot_exceed_earlier_outflow(self):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Liabilities:Clearing\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-09-08 * "advance"\n'
            '  Liabilities:Clearing 500 CNY\n'
            '  Assets:Cash -500 CNY\n'
            '2026-09-20 * "reimbursement"\n'
            '  Liabilities:Clearing -600 CNY\n'
            '  Assets:Cash 600 CNY\n', encoding="utf-8")
        candidates = audit_simple_obligations(self.root, CUTOVER)["events"]
        document = {"schema": 6, "cutover": CUTOVER.isoformat(),
                    "ledger_snapshot": load_financial(self.root).digest,
                    "loader_version": BEANCOUNT_VERSION, "owners": [],
                    "events": [{"liability_ref": row["liability_ref"],
                                "counterpart_ref": row["counterpart_ref"],
                                "kind": ("advance_outflow" if row["shape"] == "asset_outflow"
                                         else "advance_reimbursement"),
                                "clearing_id": "advance", "reason": "reviewed"}
                               for row in candidates]}
        with self.assertRaises(BudgetError) as caught:
            self.audit(document)
        self.assertEqual(caught.exception.code, "CLEARING_REIMBURSEMENT_EXCESS")


if __name__ == "__main__":
    unittest.main()

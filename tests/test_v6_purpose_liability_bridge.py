"""Reviewed liability settlements close H purpose coverage without creating G."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.obligations import audit_simple_obligations
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.purpose_flows import audit_monthly_purpose_flows


class PurposeLiabilityBridgeTests(unittest.TestCase):
    def test_reviewed_advance_and_reimbursement_have_zero_g(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            ledger = base / "main.bean"
            ledger.write_text('''option "operating_currency" "CNY"
2026-08-01 open Assets:Bank
2026-08-01 open Liabilities:Advance
2026-08-01 open Equity:Opening
2026-08-31 * "opening"
  Assets:Bank 1000 CNY
  Equity:Opening -1000 CNY
2026-09-02 * "advance for employer"
  Liabilities:Advance 100 CNY
  Assets:Bank -100 CNY
2026-09-03 * "employer reimbursement"
  Assets:Bank 100 CNY
  Liabilities:Advance -100 CNY
''', encoding="utf-8")
            snapshot = load_financial(ledger).digest

            def save(name: str, document: dict) -> Path:
                path = base / name
                path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
                return path

            header = {"schema": 6, "cutover": "2026-09-01",
                      "ledger_snapshot": snapshot, "loader_version": BEANCOUNT_VERSION}
            accounts = save("accounts.yaml", {
                "schema": 6, "cutover": "2026-09-01", "ledger_snapshot": snapshot,
                "report_currency": "CNY", "accounts": [{
                    "account": "Assets:Bank", "semantics": "H",
                    "balance_semantics": "real_inventory",
                    "payment_scope": {"CNY": "payable"}, "reason": "reviewed bank"}]})
            opening = save("opening.yaml", {
                "schema": 6, "cutover": "2026-09-01", "ledger_snapshot": snapshot,
                "h_holdings": [{"account": "Assets:Bank", "commodity": "CNY",
                                "lot": "CNY/no-cost", "owner": "free", "units": "1000",
                                "reason": "reviewed free opening"}], "k_openings": []})
            missing = audit_ownership(ledger, date(2026, 9, 1), accounts)["missing_h_ownership"]
            ownership = save("ownership.yaml", {**header,
                "owners": [{"id": "free", "name": "自由资金", "aliases": []}],
                "claims": [{"claim": row["ref"], "reason": "reviewed advance flow",
                            "slices": [{"id": f"leg_{index}", "owner_id": "free",
                                        "role": "hold", "signed_units": row["signed_units"]}]}
                           for index, row in enumerate(missing)]})
            events = audit_simple_obligations(ledger, date(2026, 9, 1))["events"]
            obligations = save("obligations.yaml", {**header, "owners": [],
                "events": [{"liability_ref": row["liability_ref"],
                            "counterpart_ref": row["counterpart_ref"],
                            "kind": "advance_outflow" if row["shape"] == "asset_outflow"
                            else "advance_reimbursement", "clearing_id": "employer",
                            "reason": "reviewed third-party advance"} for row in events]})

            pending = audit_monthly_purpose_flows(
                ledger, date(2026, 9, 1), "2026-09", accounts, opening, ownership)
            self.assertEqual(pending["status"], "NEEDS_REVIEW")
            self.assertEqual(sum(item["reason"] == "NAMED_H_EVENT_UNSUPPORTED"
                                 for item in pending["pending_events"]), 2)

            reviewed = audit_monthly_purpose_flows(
                ledger, date(2026, 9, 1), "2026-09", accounts, opening,
                ownership, obligations=obligations)
            self.assertEqual(reviewed["status"], "CERTIFIED", reviewed["pending_events"])
            self.assertEqual(reviewed["net_allocations"], {"free": "0"})
            self.assertEqual([row["kind"] for row in reviewed["known_events"]],
                             ["liability_advance_outflow", "liability_advance_reimbursement"])


if __name__ == "__main__":
    unittest.main()

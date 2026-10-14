"""Later account/event approvals must not change a signed historical month."""

from copy import deepcopy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount.core import data
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, load_financial
from beancount_budget_tool.v6.accounts import audit_accounts
from beancount_budget_tool.v6.accounts import _lot
from beancount_budget_tool.v6.benefits import audit_economic_benefits
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.h_fx import audit_h_fx
from beancount_budget_tool.v6.h_transfers import audit_h_transfers
from beancount_budget_tool.v6.k_events import audit_k_events
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.ownership import audit_ownership
from beancount_budget_tool.v6.obligations import audit_simple_obligations


CUTOVER = date(2026, 9, 1)
SEPTEMBER_END = date(2026, 9, 30)
OCTOBER_END = date(2026, 10, 31)


class SharedEventReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "main.bean"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Cash\n'
            '2026-08-01 open Assets:Shares\n'
            '2026-08-01 open Assets:NextBank\n'
            '2026-08-01 open Assets:Foreign\n'
            '2026-08-01 open Assets:FundBook\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Income:Salary\n'
            '2026-08-01 open Income:Gain\n'
            '2026-08-01 open Expenses:Food\n'
            '2026-08-01 open Liabilities:Card\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Cash 1000 CNY\n'
            '  Assets:Shares 5 FUND {100 CNY}\n'
            '  Equity:Opening -1500 CNY\n'
            '2026-09-02 * "salary"\n'
            '  Assets:Cash 100 CNY\n'
            '  Income:Salary -100 CNY\n'
            '2026-09-03 * "food"\n'
            '  Expenses:Food 10 CNY\n'
            '  Assets:Cash -10 CNY\n'
            '2026-10-01 * "move to new bank"\n'
            '  Assets:Cash -20 CNY\n    fund: "自由资金"\n'
            '  Assets:NextBank 20 CNY\n    fund: "自由资金"\n'
            '2026-10-02 * "foreign purchase"\n'
            '  Assets:Foreign 20 USD @@ 140 CNY\n    fund: "自由资金"\n'
            '  Assets:Cash -140 CNY\n    fund: "自由资金"\n'
            '2026-10-03 * "subscription"\n'
            '  Assets:FundBook 20 CNY\n'
            '  Assets:Cash -20 CNY\n    fund: "自由资金"\n'
            '2026-10-04 * "sale"\n'
            '  Assets:Shares -1 FUND {100 CNY}\n    fund: "自由资金"\n'
            '  Assets:Cash 110 CNY\n    fund: "自由资金"\n'
            '  Income:Gain -10 CNY\n'
            '2026-10-05 * "card purchase"\n'
            '  Expenses:Food 10 USD\n'
            '  Liabilities:Card -10 USD\n'
            '2026-10-06 * "card repayment"\n'
            '  Liabilities:Card 10 USD\n'
            '  Assets:Cash -10 USD\n    fund: "自由资金"\n'
            '2026-10-06 * "bridge funding"\n'
            '  Assets:Cash 10 USD @@ 70 CNY\n    fund: "自由资金"\n'
            '  Assets:Cash -70 CNY\n    fund: "自由资金"\n', encoding="utf-8")
        fin = load_financial(self.root)
        facts = FactIndex(fin)
        self.refs = {entry.narration: [facts.for_posting(p).as_dict() for p in entry.postings]
                     for entry in fin.entries if isinstance(entry, data.Transaction)}
        header = {"schema": 6, "cutover": CUTOVER.isoformat(),
                  "ledger_snapshot": fin.digest, "loader_version": fin.loader_version}
        owners = [{"id": ident, "name": name, "aliases": []}
                  for ident, name in (("free", "自由资金"), ("living", "生活费"))]
        self.save("accounts", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": fin.digest,
            "report_currency": "CNY", "accounts": [
                {"account": name, "semantics": mode,
                 "balance_semantics": "real_inventory" if mode == "H" else "signed_subscription_flow",
                 "payment_scope": scope, "reason": "reviewed virtual account"}
                for name, mode, scope in (
                    ("Assets:Cash", "H", {"CNY": "payable", "USD": "excluded"}),
                    ("Assets:Shares", "H", {"FUND": "excluded"}),
                    ("Assets:NextBank", "H", {"CNY": "payable"}),
                    ("Assets:Foreign", "H", {"USD": "excluded"}),
                    ("Assets:FundBook", "K", {"CNY": "excluded"}))],
            "credit_card_whitelist": ["Liabilities:Card"], "same_day_settlements": [{
                "asset_account": "Assets:Cash", "credit_card_account": "Liabilities:Card",
                "debit_ref": self.refs["card repayment"][1],
                "liability_ref": self.refs["card repayment"][0],
                "funding_ref": self.refs["bridge funding"][0], "reason": "same-day bridge"}]})
        self.save("ownership", {**header, "owners": owners,
                               "defaults": {"income_owner_id": "free", "expense_owner_id": "living"},
                               "claims": []})
        self.save("opening", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": fin.digest,
            "h_holdings": [{"account": name, "commodity": commodity, "lot": lot,
                            "owner": "free", "units": amount, "reason": "reviewed opening"}
                           for name, commodity, lot, amount in (
                               ("Assets:Cash", "CNY", "CNY/no-cost", "1000"),
                               ("Assets:Shares", "FUND", None, "5"))], "k_openings": []})
        # Cost lot spelling comes from booked facts, never from a hand-made identifier.
        opening = self.read("opening")
        shares = next(p for e in fin.entries if isinstance(e, data.Transaction)
                      and e.narration == "opening" for p in e.postings if p.account == "Assets:Shares")
        opening["h_holdings"][1]["lot"] = _lot(shares)
        self.save("opening", opening)
        self.save("classifications", {
            "schema": 6, "cutover": CUTOVER.isoformat(), "loader_version": fin.loader_version,
            "categories": [{"id": ident, "name": name, "parent": None, "aliases": []}
                           for ident, name in (("income", "收入"), ("food", "生活开销"))],
            "defaults": {"income_category_id": "income", "expense_category_id": "food"},
            "classifications": []})
        self.save("obligations", {**header, "owners": owners, "events": [{
            "liability_ref": self.refs["card purchase"][1],
            "counterpart_ref": self.refs["card purchase"][0], "kind": "consumer_purchase",
            "obligation_id": "card", "owner_id": "free", "reason": "future purchase"}, {
            "liability_ref": self.refs["card repayment"][0],
            "counterpart_ref": self.refs["card repayment"][1], "kind": "payment",
            "obligation_id": "card", "reason": "future repayment"}]})
        self.save("h_transfers", {**header, "events": [{
            "from_ref": self.refs["move to new bank"][0], "to_ref": self.refs["move to new bank"][1],
            "reason": "preserve", "slices": [{"id": "move", "from_owner_id": "free",
                "to_owner_id": "free", "signed_from_units": "-20", "signed_to_units": "20",
                "boundary_role": "preserve"}]}]})
        self.save("h_fx", {**header, "events": [{
            "from_ref": self.refs[name][1], "to_ref": self.refs[name][0], "owner_id": owner,
            "signed_from_units": debit, "signed_to_units": credit,
            "boundary_role": "preserve", "reason": "reviewed purchase"}
            for name, owner, debit, credit in (("foreign purchase", "free", "-140", "20"),
                                               ("bridge funding", "free", "-70", "10"))]})
        self.save("k_events", {**header, "events": [{
            "k_ref": self.refs["subscription"][0], "cash_ref": self.refs["subscription"][1],
            "kind": "subscription", "reason": "reviewed book flow", "slices": [{
                "id": "subscription", "k_owner_id": "free", "cash_owner_id": "free",
                "signed_k_units": "20", "signed_cash_units": "-20", "boundary_role": "preserve"}]}]})
        self.save("benefit_groups", {**header, "report_currency": "CNY", "approvals": [{
            "income_ref": self.refs["sale"][2], "reason": "reviewed costed sale"}]})
        self.save("purpose_pools", {"schema": 6, "cutover": CUTOVER.isoformat(),
                                    "report_currency": "CNY", "pools": []})
        self.save("expense_plans", {"schema": 6, "report_currency": "CNY",
                                    "spendable_cap_category_id": "food",
                                    "defaults": {"expense_subtree_targets": {"food": "100"}}})
        self.save("purpose_plans", {"schema": 6, "report_currency": "CNY",
                                    "defaults": {"allocation_targets": {
                                        "free": "not_assessed", "living": "not_assessed"}}})
        self.save("completeness", {"schema": 6, "cutover": CUTOVER.isoformat(),
            "as_of": SEPTEMBER_END.isoformat(), "ledger_snapshot": fin.digest,
            "unbooked_obligations": "none", "inflight_payments": "none", "reason": "virtual"})
        keys = ("accounts", "opening", "ownership", "obligations", "classifications",
                "completeness", "h_transfers", "h_fx", "k_events", "benefit_groups",
                "purpose_pools", "expense_plans", "purpose_plans")
        self.save("month", {"schema": 6, "cutover": CUTOVER.isoformat(), "month": "2026-09",
                            "ledger": "main.bean", "report_currency": "CNY",
                            **{key: key + ".yaml" for key in keys}})

    def path(self, name):
        return self.base / (name + ".yaml")

    def save(self, name, document):
        self.path(name).write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                                   encoding="utf-8")

    def read(self, name):
        return yaml.safe_load(self.path(name).read_text(encoding="utf-8"))

    def auditors(self):
        return {
            "h_transfers": lambda: audit_h_transfers(self.root, CUTOVER, self.path("accounts"),
                self.path("ownership"), self.path("h_transfers"), self.path("opening")),
            "h_fx": lambda: audit_h_fx(self.root, CUTOVER, self.path("accounts"),
                self.path("ownership"), self.path("h_fx")),
            "k_events": lambda: audit_k_events(self.root, CUTOVER, self.path("accounts"),
                self.path("ownership"), self.path("k_events")),
        }

    def test_old_month_ignores_future_accounts_currencies_events_and_bridge(self):
        with booked_through(SEPTEMBER_END):
            accounts = audit_accounts(self.root, CUTOVER, self.path("accounts"))
            self.assertEqual(accounts["review_state"], "complete")
            self.assertEqual([row["account"] for row in accounts["accounts"]],
                             ["Assets:Cash", "Assets:Shares"])
            self.assertEqual(accounts["accounts"][0]["payment_scope"], {"CNY": "payable"})
            self.assertEqual(accounts["same_day_settlements"], [])
            debt = audit_simple_obligations(self.root, CUTOVER, self.path("obligations"))
            self.assertEqual(debt["events"], [])
            self.assertEqual(debt["obligations"], [])
            owned = audit_ownership(self.root, CUTOVER, self.path("accounts"), self.path("ownership"))
            self.assertEqual(owned["claimed_h_postings"], 2)
            for name, audit in self.auditors().items():
                with self.subTest(name=name):
                    result = audit()
                    self.assertEqual(result["review_state"], "complete")
                    self.assertEqual(result["events"], [])
            benefit = audit_economic_benefits(self.root, CUTOVER, self.path("classifications"),
                                             self.path("benefit_groups"))
            self.assertEqual(benefit["proved_benefits"], 1)  # September salary only.
        report = audit_month_workflow(self.path("month"))
        self.assertEqual(report["status"], "CERTIFIED_SCOPED")
        self.assertEqual(report["review_items"], [])
        self.assertEqual(report["report"]["actual_spendable"]["actual_spendable"], "-10")
        self.assertEqual(report["report"]["max_spendable"]["max_spendable"], "-10")
        self.assertEqual(report["report"]["account_purposes"]["rows"], [
            {"account": "Assets:Cash", "commodity": "CNY", "owner_id": "free", "units": "1100"},
            {"account": "Assets:Cash", "commodity": "CNY", "owner_id": "living", "units": "-10"},
            {"account": "Assets:Shares", "commodity": "FUND", "owner_id": "free", "units": "5"}])

    def test_future_events_are_replayed_when_their_month_is_in_scope(self):
        with booked_through(OCTOBER_END):
            accounts = audit_accounts(self.root, CUTOVER, self.path("accounts"))
            self.assertEqual(accounts["review_state"], "complete")
            self.assertEqual(len(accounts["same_day_settlements"]), 1)
            debt = audit_simple_obligations(self.root, CUTOVER, self.path("obligations"))
            self.assertEqual(debt["certified_events"], 2)
            self.assertEqual(debt["obligations"][0]["remaining_native"], "0")
            expected = {"h_transfers": 1, "h_fx": 2, "k_events": 1}
            for name, audit in self.auditors().items():
                with self.subTest(name=name):
                    result = audit()
                    self.assertEqual(result["certified_events"], expected[name])
            benefit = audit_economic_benefits(self.root, CUTOVER, self.path("classifications"),
                                             self.path("benefit_groups"))
            self.assertEqual(benefit["review_state"], "complete")
            self.assertEqual(benefit["proved_benefits"], 2)

    def test_future_counterpart_cannot_be_an_unrelated_live_source(self):
        for name, field, code in (("h_transfers", "to_ref", "H_TRANSFER_TO_REF_INVALID"),
                                  ("h_fx", "to_ref", "H_FX_TO_REF_INVALID"),
                                  ("k_events", "cash_ref", "K_EVENT_CASH_REF_INVALID")):
            original = self.read(name)
            changed = deepcopy(original)
            changed["events"][0][field] = self.refs["food"][1]
            self.save(name, changed)
            with self.subTest(name=name), booked_through(SEPTEMBER_END):
                with self.assertRaises(BudgetError) as caught:
                    self.auditors()[name]()
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.source.startswith(str(self.path(name).resolve()) + ":"))
            self.save(name, original)

    def test_stale_future_sources_are_not_silently_ignored(self):
        # Remove the bridge declaration so this case reaches individual event validation.
        accounts = self.read("accounts")
        accounts["same_day_settlements"] = []
        self.save("accounts", accounts)
        original = self.root.read_text(encoding="utf-8")
        for name, narration, code in (("h_transfers", "move to new bank", "H_TRANSFER_REF_INVALID"),
                                      ("h_fx", "foreign purchase", "H_FX_REF_INVALID"),
                                      ("k_events", "subscription", "K_EVENT_REF_INVALID")):
            self.root.write_text(original.replace(narration, narration + " corrected"), encoding="utf-8")
            with self.subTest(name=name), booked_through(SEPTEMBER_END):
                with self.assertRaises(BudgetError) as caught:
                    self.auditors()[name]()
                self.assertEqual(caught.exception.code, code)

    def test_unknown_future_account_currency_and_duplicate_declaration_reject(self):
        original = self.read("accounts")
        for variant, code in (("account", "ACCOUNT_UNKNOWN"), ("currency", "PAYMENT_SCOPE_INVALID"),
                               ("duplicate", "ACCOUNT_DUPLICATE")):
            document = deepcopy(original)
            if variant == "account":
                document["accounts"][-1]["account"] = "Assets:NeverPosted"
            elif variant == "currency":
                document["accounts"][0]["payment_scope"]["EUR"] = "excluded"
            else:
                document["accounts"].append(deepcopy(document["accounts"][-1]))
            self.save("accounts", document)
            with self.subTest(variant=variant), booked_through(SEPTEMBER_END):
                with self.assertRaises(BudgetError) as caught:
                    audit_accounts(self.root, CUTOVER, self.path("accounts"))
                self.assertEqual(caught.exception.code, code)

    def test_growing_shared_sidecars_preserves_complete_monthly_report(self):
        shared = {name: self.read(name) for name in (
            "accounts", "h_transfers", "h_fx", "k_events", "benefit_groups")}
        accounts = deepcopy(shared["accounts"])
        accounts["accounts"] = accounts["accounts"][:2]
        accounts["accounts"][0]["payment_scope"] = {"CNY": "payable"}
        accounts["same_day_settlements"] = []
        self.save("accounts", accounts)
        for name in ("h_transfers", "h_fx", "k_events", "benefit_groups"):
            document = deepcopy(shared[name])
            document["approvals" if name == "benefit_groups" else "events"] = []
            self.save(name, document)
        before = audit_month_workflow(self.path("month"))
        for name, document in shared.items():
            self.save(name, document)
        after = audit_month_workflow(self.path("month"))
        self.assertEqual((before["status"], after["status"]),
                         ("CERTIFIED_SCOPED", "CERTIFIED_SCOPED"))
        for section in ("account_purposes", "purpose_balances", "max_spendable"):
            self.assertEqual(before["report"][section], after["report"][section])

    def test_future_group_and_bridge_stale_sources_are_rejected(self):
        groups = self.read("benefit_groups")
        groups["approvals"][0]["income_ref"]["fact_sha256"] = "0" * 64
        self.save("benefit_groups", groups)
        with booked_through(SEPTEMBER_END):
            with self.assertRaises(BudgetError) as caught:
                audit_economic_benefits(self.root, CUTOVER, self.path("classifications"),
                                        self.path("benefit_groups"))
            self.assertEqual(caught.exception.code, "BENEFIT_GROUP_REF_INVALID")
        accounts = self.read("accounts")
        accounts["same_day_settlements"][0]["funding_ref"]["fact_sha256"] = "0" * 64
        self.save("accounts", accounts)
        with booked_through(SEPTEMBER_END):
            with self.assertRaises(BudgetError) as caught:
                audit_accounts(self.root, CUTOVER, self.path("accounts"))
            self.assertEqual(caught.exception.code, "SETTLEMENT_SOURCE_INVALID")

    def test_future_decisions_are_validated_again_when_the_month_arrives(self):
        for name, code in (("h_transfers", "H_TRANSFER_UNITS_MISMATCH"),
                           ("h_fx", "H_FX_UNITS_MISMATCH"),
                           ("k_events", "K_EVENT_UNITS_MISMATCH")):
            original = self.read(name)
            changed = deepcopy(original)
            row = changed["events"][0]
            if name == "h_fx":
                row["signed_from_units"] = "-139"
            else:
                part = row["slices"][0]
                part["signed_from_units" if name == "h_transfers" else "signed_k_units"] = "21"
            self.save(name, changed)
            with self.subTest(name=name):
                with booked_through(SEPTEMBER_END):
                    self.assertEqual(self.auditors()[name]()["events"], [])
                with booked_through(OCTOBER_END):
                    with self.assertRaises(BudgetError) as caught:
                        self.auditors()[name]()
                    self.assertEqual(caught.exception.code, code)
            self.save(name, original)

    def test_future_payment_scope_pending_does_not_block_the_old_month(self):
        accounts = self.read("accounts")
        accounts["accounts"][2]["payment_scope"]["CNY"] = "pending"
        self.save("accounts", accounts)
        with booked_through(SEPTEMBER_END):
            current = audit_accounts(self.root, CUTOVER, self.path("accounts"))
            self.assertEqual(current["review_state"], "complete")
            self.assertEqual(current["pending_payment_scope"], [])
        with booked_through(OCTOBER_END):
            later = audit_accounts(self.root, CUTOVER, self.path("accounts"))
            self.assertEqual(later["review_state"], "pending")
            self.assertEqual(later["pending_payment_scope"], ["Assets:NextBank/CNY"])

    def test_future_obligation_requires_its_actual_counterpart(self):
        document = self.read("obligations")
        document["events"][0]["counterpart_ref"] = self.refs["food"][0]
        self.save("obligations", document)
        for bounded in (True, False):
            with self.subTest(bounded=bounded):
                if bounded:
                    with booked_through(SEPTEMBER_END), self.assertRaises(BudgetError) as caught:
                        audit_simple_obligations(self.root, CUTOVER, self.path("obligations"))
                else:
                    with self.assertRaises(BudgetError) as caught:
                        audit_simple_obligations(self.root, CUTOVER, self.path("obligations"),
                                                 as_of=SEPTEMBER_END)
                self.assertEqual(caught.exception.code, "OBLIGATION_COUNTERPART_INVALID")


if __name__ == "__main__":
    unittest.main()

"""Optional owner orders resolve mixed transfers without changing public defaults."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, load_financial
from beancount_budget_tool.v6.h_transfers import audit_h_transfers
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.ownership import audit_ownership


CUTOVER = date(2026, 9, 1)


class TransferOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "main.bean"
        self.accounts = self.base / "accounts.yaml"
        self.opening = self.base / "opening.yaml"
        self.ownership = self.base / "ownership.yaml"

    def save(self, path, value):
        path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def case(self, amount="150", source_fund=None, ordered=True, opening=True):
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Mixed\n'
            '2026-08-01 open Assets:Destination\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Expenses:Loss\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Mixed 400 CNY\n'
            '  Equity:Opening -400 CNY\n'
            '2026-09-02 * "partial transfer"\n'
            f'  Assets:Mixed -{amount} CNY\n' +
            (f'    fund: "{source_fund}"\n' if source_fund else "") +
            f'  Assets:Destination {amount} CNY\n', encoding="utf-8")
        digest = load_financial(self.root).digest
        self.save(self.accounts, {
            "schema": 6, "cutover": CUTOVER.isoformat(), "report_currency": "CNY",
            "accounts": [{"account": name, "semantics": "H", "balance_semantics": "real_inventory",
                          "payment_scope": {"CNY": "payable"}, "reason": "virtual account"}
                         for name in ("Assets:Mixed", "Assets:Destination")]})
        self.save(self.opening, {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": digest,
            "h_holdings": [{"account": "Assets:Mixed", "commodity": "CNY", "lot": "CNY/no-cost",
                            "owner": owner, "units": value, "reason": "reviewed opening"}
                           for owner, value in (("investment", "100"), ("savings", "300"))] if opening else [],
            "k_openings": []})
        document = {
            "schema": 6, "cutover": CUTOVER.isoformat(), "ledger_snapshot": digest,
            "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": ident, "name": name, "aliases": []}
                       for ident, name in (("investment", "投资"), ("savings", "储蓄"))],
            "defaults": {"income_owner_id": "investment", "expense_owner_id": "investment"},
            "claims": []}
        if ordered:
            document["transfer_orders"] = [{"account": "Assets:Mixed", "commodity": "CNY",
                                            "owner_ids": ["investment", "savings"],
                                            "reason": "investment first, savings second"}]
        self.save(self.ownership, document)

    def owned(self):
        return audit_ownership(self.root, CUTOVER, self.accounts, self.ownership, self.opening)

    def test_order_uses_first_owner_then_next_and_preserves_both_at_destination(self):
        for amount, expected in (("50", [("investment", "-50")]),
                                 ("150", [("investment", "-100"), ("savings", "-50")])):
            with self.subTest(amount=amount):
                self.case(amount)
                rows = self.owned()["ownership"]
                self.assertEqual([(p["owner_id"], p["signed_units"]) for p in rows[0]["slices"]], expected)
                self.assertIn("已审转出顺序", rows[0]["review_reason"])
                events = audit_h_transfers(self.root, CUTOVER, self.accounts, self.ownership,
                                           opening=self.opening)
                self.assertEqual(events["events"][0]["status"], "CERTIFIED")
                self.assertEqual(events["events"][0]["net_allocations"], {})
                holdings = audit_h_holdings(self.root, CUTOVER, date(2026, 9, 30),
                                            self.accounts, self.opening, self.ownership)
                destination = next(row for row in holdings["positions"]
                                   if row["account"] == "Assets:Destination")
                self.assertEqual(destination["owner_units"],
                                 [{"owner_id": owner, "units": str(-int(value))} for owner, value in expected])

    def test_explicit_source_fund_overrides_the_order(self):
        self.case("50", "储蓄")
        rows = self.owned()["ownership"]
        self.assertEqual(rows[0]["origin"], "inline")
        self.assertEqual(rows[1]["slices"][0]["owner_id"], "savings")

    def test_overdraft_names_the_purpose_and_does_not_expose_internal_maps(self):
        self.case("1200", "储蓄别名")
        self.root.write_text(self.root.read_text(encoding="utf-8").replace("400 CNY", "2000 CNY"),
                             encoding="utf-8")
        opening = yaml.safe_load(self.opening.read_text(encoding="utf-8"))
        opening["h_holdings"][1]["units"] = "1900"
        self.save(self.opening, opening)
        document = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        document["owners"].append({"id": "fund_35cacd0163c1c4625679",
                                   "name": "额外储蓄", "aliases": ["储蓄别名"]})
        self.save(self.ownership, document)
        with self.assertRaises(BudgetError) as caught:
            self.owned()
        error = caught.exception
        self.assertEqual(error.code, "H_OWNER_NEGATIVE")
        self.assertIn("账户：Assets:Mixed", error.message)
        self.assertIn("资金用途：额外储蓄", error.message)
        self.assertIn("划拨前余额：0 CNY（可划出 0 CNY）", error.message)
        self.assertIn("本次净划出：1200 CNY", error.message)
        self.assertIn("本次缺口：1200 CNY", error.message)
        self.assertIn("划拨后余额：-1200 CNY", error.message)
        for internal in ("fund_", "Fraction", "before=", "after=", "储蓄别名"):
            self.assertNotIn(internal, str(error))
        line = self.root.read_text(encoding="utf-8").splitlines().index('  Assets:Mixed -1200 CNY') + 1
        self.assertEqual(error.source, f"{self.root.resolve()}:{line}")

    def test_overdraft_shows_exact_decimal_balance_and_only_the_missing_amount(self):
        self.case("150.5", "投资")
        document = yaml.safe_load(self.opening.read_text(encoding="utf-8"))
        document["h_holdings"][0]["units"] = "100.25"
        document["h_holdings"][1]["units"] = "299.75"
        self.save(self.opening, document)
        with self.assertRaises(BudgetError) as caught:
            self.owned()
        text = caught.exception.message
        self.assertIn("资金用途：投资", text)
        self.assertIn("划拨前余额：100.25 CNY（可划出 100.25 CNY）", text)
        self.assertIn("本次净划出：150.5 CNY", text)
        self.assertIn("本次缺口：50.25 CNY", text)
        self.assertIn("划拨后余额：-50.25 CNY", text)
        self.assertNotIn("资金用途：储蓄", text)

    def test_split_overdraft_lists_every_insufficient_purpose(self):
        self.case("200", "投资:150,额外储蓄:50")
        document = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        document["owners"].append({"id": "extra_savings", "name": "额外储蓄", "aliases": []})
        self.save(self.ownership, document)
        with self.assertRaises(BudgetError) as caught:
            self.owned()
        text = caught.exception.message
        self.assertIn("资金用途：投资", text)
        self.assertIn("划拨前余额：100 CNY", text)
        self.assertIn("资金用途：额外储蓄", text)
        self.assertIn("划拨前余额：0 CNY", text)
        self.assertNotIn("资金用途：储蓄", text)
        self.assertEqual(text.count("本次缺口：50 CNY"), 2)

    def test_public_default_still_requires_a_choice(self):
        self.case(ordered=False)
        self.assertEqual({row["reason_code"] for row in self.owned()["missing_h_ownership"]},
                         {"FUND_MIXED_SOURCE", "FUND_SOURCE_UNRESOLVED"})

    def test_unknown_opening_is_not_replaced_by_a_priority(self):
        self.case(opening=False)
        self.assertEqual(self.owned()["missing_h_ownership"][0]["reason_code"],
                         "FUND_SOURCE_OPENING_PENDING")

    def test_unlisted_owners_are_not_silently_used(self):
        self.case()
        document = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        document["transfer_orders"][0]["owner_ids"] = ["investment"]
        self.save(self.ownership, document)
        self.assertEqual(self.owned()["missing_h_ownership"][0]["reason_code"],
                         "FUND_SOURCE_INSUFFICIENT")

    def test_loss_can_make_investment_negative_but_transfer_cannot_worsen_it(self):
        self.case("150")
        self.root.write_text(self.root.read_text(encoding="utf-8").replace(
            '2026-09-02 * "partial transfer"',
            '2026-09-01 * "investment loss"\n'
            '  Assets:Mixed -120 CNY\n'
            '  Expenses:Loss 120 CNY\n'
            '2026-09-02 * "partial transfer"'), encoding="utf-8")
        report = self.owned()
        source = report["ownership"][1]
        self.assertEqual([(p["owner_id"], p["signed_units"]) for p in source["slices"]],
                         [("savings", "-150")])
        holdings = audit_h_holdings(self.root, CUTOVER, date(2026, 9, 30),
                                    self.accounts, self.opening, self.ownership)
        mixed = next(row for row in holdings["positions"] if row["account"] == "Assets:Mixed")
        self.assertEqual(mixed["owner_units"], [{"owner_id": "investment", "units": "-20"},
                                                {"owner_id": "savings", "units": "150"}])
        # An explicit voluntary overdraft is still rejected, even under this policy.
        self.root.write_text(self.root.read_text(encoding="utf-8").replace(
            '  Assets:Mixed -150 CNY\n', '  Assets:Mixed -150 CNY\n    fund: "投资"\n'), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            self.owned()
        self.assertEqual(caught.exception.code, "H_OWNER_NEGATIVE")
        self.assertIn("划拨前余额：-20 CNY（可划出 0 CNY）", caught.exception.message)
        self.assertIn("本次净划出：150 CNY", caught.exception.message)
        self.assertIn("本次缺口：150 CNY", caught.exception.message)
        self.assertIn("划拨后余额：-170 CNY", caught.exception.message)

    def test_invalid_and_duplicate_rules_have_yaml_locations(self):
        self.case()
        original = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        for field, value in (("owner_ids", ["investment", "investment"]),
                             ("owner_ids", ["unknown"]), ("account", "Assets:Unknown"),
                             ("reason", ""), ("commodity", 3), ("commodity", "TYPO")):
            with self.subTest(field=field, value=value):
                document = yaml.safe_load(yaml.safe_dump(original))
                document["transfer_orders"][0][field] = value
                self.save(self.ownership, document)
                with self.assertRaises(BudgetError) as caught:
                    self.owned()
                self.assertEqual(caught.exception.code, "FUND_TRANSFER_ORDER_INVALID")
                self.assertTrue(caught.exception.source.startswith(str(self.ownership.resolve()) + ":"))
        original["transfer_orders"] *= 2
        self.save(self.ownership, original)
        with self.assertRaises(BudgetError) as caught:
            self.owned()
        self.assertEqual(caught.exception.code, "FUND_TRANSFER_ORDER_INVALID")

    def test_order_applies_only_to_its_account(self):
        self.case()
        document = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        document["transfer_orders"][0]["account"] = "Assets:Destination"
        self.save(self.ownership, document)
        self.assertEqual(self.owned()["missing_h_ownership"][0]["reason_code"], "FUND_MIXED_SOURCE")

    def test_order_requires_replay_defaults(self):
        self.case()
        document = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        del document["defaults"]
        self.save(self.ownership, document)
        with self.assertRaises(BudgetError) as caught:
            self.owned()
        self.assertEqual(caught.exception.code, "FUND_TRANSFER_ORDER_INVALID")

    def test_future_account_rule_can_be_shared_without_changing_old_month(self):
        self.case()
        with booked_through(date(2026, 9, 30)):
            original = self.owned()["ownership"]
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-08-01 open Assets:Future\n'
                             '2026-08-01 open Income:Pay\n'
                             '2026-10-02 * "future income"\n'
                             '  Assets:Future 20 CNY\n'
                             '  Income:Pay -20 CNY\n', encoding="utf-8")
        accounts = yaml.safe_load(self.accounts.read_text(encoding="utf-8"))
        accounts["accounts"].append({"account": "Assets:Future", "semantics": "H",
                                     "balance_semantics": "real_inventory",
                                     "payment_scope": {"CNY": "payable"}, "reason": "future review"})
        self.save(self.accounts, accounts)
        owners = yaml.safe_load(self.ownership.read_text(encoding="utf-8"))
        owners["transfer_orders"].append({"account": "Assets:Future", "commodity": "CNY",
                                          "owner_ids": ["investment", "savings"], "reason": "future order"})
        self.save(self.ownership, owners)
        with booked_through(date(2026, 9, 30)):
            historical = self.owned()
        self.assertEqual(historical["review_state"], "complete")
        self.assertEqual(historical["ownership"], original)


if __name__ == "__main__":
    unittest.main()

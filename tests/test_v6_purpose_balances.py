"""Purpose totals preserve currencies, balances and separate book-flow bases."""

from copy import deepcopy
import unittest

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.purpose_balances import summarize_purpose_balances


class PurposeBalancesTests(unittest.TestCase):
    def setUp(self):
        self.owners = {"owners": [
            {"id": ident, "name": name, "aliases": []}
            for ident, name in (("free", "自由资金"), ("living", "生活费"),
                                ("savings", "储蓄"), ("investment", "待投资金"),
                                ("travel", "旅游"), ("insurance", "保险"),
                                ("empty", "电子产品"))],
            "defaults": {"income_owner_id": "free", "expense_owner_id": "living"}}
        self.h = {"status": "CERTIFIED_H_OUTSIDE_POOLS", "rows": [
            {"account": account, "commodity": currency, "owner_id": owner, "units": value}
            for account, currency, owner, value in (
                ("Assets:A", "CNY", "free", "3000"),
                ("Assets:A", "CNY", "living", "100"),
                ("Assets:B", "CNY", "living", "-20"),
                ("Assets:B", "CNY", "travel", "39"),
                ("Assets:C", "CNY", "travel", "100"),
                ("Assets:D", "USD", "travel", "10"),
                ("Assets:A", "CNY", "-", "4"))]}
        self.pools = {"report_currency": "CNY", "pools": [{
            "id": "broker", "status": "CERTIFIED_NOMINAL_BOOK_COVERAGE",
            "protected_owner_id": "savings", "protected_label": "额外储蓄",
            "remainder_owner_id": "investment", "remainder_label": "待投资金",
            "account_purposes": [{"account": "Assets:Broker", "protected_nominal": "3000",
                                  "remainder_book_cost": "5000"}]}]}
        self.k = {"status": "CERTIFIED", "rows": [{
            "account": "Assets:Fund", "commodity": "CNY", "purpose_changes": [
                {"owner_id": "insurance", "signed_change": "200"},
                {"owner_id": "travel", "signed_change": "900"}]}]}

    def audit(self):
        return summarize_purpose_balances(self.h, self.pools, self.k, self.owners)

    def test_all_registered_purposes_and_defaults_are_visible(self):
        report = self.audit()
        rows = {row["owner_id"]: row for row in report["purposes"]}
        self.assertEqual(set(rows), {row["id"] for row in self.owners["owners"]})
        self.assertEqual(rows["free"]["default_roles"], ["income_default"])
        self.assertEqual(rows["living"]["default_roles"], ["expense_default"])
        self.assertEqual(rows["free"]["h_native_balances"][0]["units"], "3000")
        self.assertEqual(rows["empty"]["h_native_balances"], [])
        self.assertEqual(report["outside_budget"]["h_native_balances"][0]["units"], "4")
        self.assertNotIn("-", rows)

    def test_native_balances_group_accounts_without_converting_currencies(self):
        rows = {row["owner_id"]: row for row in self.audit()["purposes"]}
        travel = rows["travel"]
        self.assertEqual(travel["h_native_balances"], [
            {"commodity": "CNY", "units": "139", "accounts": [
                {"account": "Assets:B", "units": "39"},
                {"account": "Assets:C", "units": "100"}]},
            {"commodity": "USD", "units": "10", "accounts": [
                {"account": "Assets:D", "units": "10"}]}])
        self.assertEqual(rows["living"]["h_native_balances"][0]["units"], "80")
        self.assertTrue(rows["living"]["has_negative_h_position"])

    def test_pool_cost_and_k_changes_do_not_inflate_native_balances(self):
        rows = {row["owner_id"]: row for row in self.audit()["purposes"]}
        self.assertEqual(rows["savings"]["h_native_balances"], [])
        self.assertEqual(rows["savings"]["pool_book_cost_balances"], [
            {"commodity": "CNY", "units": "3000", "accounts": [
                {"account": "Assets:Broker", "pool_id": "broker", "label": "额外储蓄",
                 "units": "3000"}]}])
        self.assertEqual(rows["travel"]["k_book_purpose_changes"][0]["signed_change"], "900")
        self.assertEqual(rows["travel"]["h_native_balances"][0]["units"], "139")
        self.assertEqual(rows["insurance"]["h_native_balances"], [])

    def test_offsetting_positions_keep_zero_total_and_deficit_visible(self):
        self.h["rows"][1]["units"] = "20"
        living = next(row for row in self.audit()["purposes"] if row["owner_id"] == "living")
        self.assertEqual(living["h_native_balances"][0]["units"], "0")
        self.assertEqual(len(living["h_native_balances"][0]["accounts"]), 2)
        self.assertTrue(living["has_negative_h_position"])

    def test_missing_attribution_cannot_be_displayed_as_zero(self):
        for component, status, code in ((self.h, "NEEDS_REVIEW", "H_HOLDINGS_PENDING"),
                                        (self.k, "NEEDS_REVIEW", "K_BOOK_PURPOSE_PENDING")):
            original = component["status"]
            component["status"] = status
            report = self.audit()
            self.assertIsNone(report["purposes"])
            self.assertEqual(report["reason_codes"], [code])
            component["status"] = original

    def test_unknown_purpose_is_rejected_instead_of_disappearing(self):
        row = deepcopy(self.h["rows"][0])
        row["owner_id"] = "unknown"
        self.h["rows"].append(row)
        with self.assertRaises(BudgetError) as caught:
            self.audit()
        self.assertEqual(caught.exception.code, "MONTH_REPORT_MISMATCH")

    def test_unreviewed_pool_prevents_purpose_totals(self):
        self.pools["pools"][0]["status"] = "NEEDS_REVIEW"
        report = self.audit()
        self.assertEqual(report["reason_codes"], ["PURPOSE_POOL_PENDING"])
        self.assertIsNone(report["purposes"])


if __name__ == "__main__":
    unittest.main()

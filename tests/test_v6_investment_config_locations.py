"""Investment review errors point to the field a user can actually repair."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from fixture_benefits import propose_groups as propose
from beancount_budget_tool.v6.benefits import audit_economic_benefits
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.cli import main
from beancount_budget_tool.v6.purpose_pools import audit_purpose_pools


class InvestmentConfigLocationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve() / "投资配置 空格"
        self.base.mkdir()
        self.root = self.base / "main.bean"
        self.root.write_text('''option "operating_currency" "CNY"
2026-08-01 open Assets:Cash
2026-08-01 open Assets:Security
2026-08-01 open Equity:Opening
2026-08-01 open Income:Gain
2026-08-01 open Expenses:Fee
2026-08-31 * "virtual opening"
  Assets:Cash 100 CNY
  Equity:Opening -100 CNY
2026-09-01 * "virtual buy"
  Assets:Security 10 SH {2 CNY}
  Assets:Cash -20 CNY
2026-09-02 * "virtual sell"
  Assets:Security -10 SH {2 CNY} @ 3 CNY
  Assets:Cash 29 CNY
  Expenses:Fee 1 CNY
  Income:Gain -10 CNY
''', encoding="utf-8")
        self.cutover = date(2026, 9, 1)
        self.classes = self.base / "classifications.yaml"
        missing = audit_classification(self.root, self.cutover)["missing_classifications"]
        self.write(self.classes, {"schema": 6, "cutover": "2026-09-01", "loader_version": BEANCOUNT_VERSION,
            "categories": [{"id": "generic", "name": "通用", "parent": None, "aliases": []}],
            "classifications": [{"claim": row["ref"], "reason": "公开虚构分类",
                "slices": [{"id": f"slice_{i}", "category_id": "generic", "signed_units": row["signed_units"]}]}
                for i, row in enumerate(missing)]})
        proposal = propose(self.root, self.cutover, self.classes, "CNY")
        self.groups = self.base / "benefits.yaml"
        self.group_doc = {"schema": 6, "cutover": "2026-09-01", "ledger_snapshot": proposal["ledger_snapshot"],
            "loader_version": BEANCOUNT_VERSION, "report_currency": "CNY", "approvals": [
                {"income_ref": proposal["proposals"][0]["income_ref"], "reason": "公开虚构收益等式确认"}]}
        self.pools = self.base / "pools.yaml"
        self.pool_doc = {"schema": 6, "cutover": "2026-09-01", "report_currency": "CNY", "pools": [{
            "id": "broker", "accounts": ["Assets:Cash", "Assets:Security"],
            "reserve_order": ["Assets:Cash", "Assets:Security"], "payment_scope": "excluded",
            "protected_owner_id": "savings", "protected_label": "储蓄", "protected_principal": "50",
            "remainder_owner_id": "investment", "remainder_label": "投资", "reason": "公开虚构本金保护"}]}
        self.write(self.groups, self.group_doc)
        self.write(self.pools, self.pool_doc)

    def write(self, path, document):
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def benefits(self):
        return audit_economic_benefits(self.root, self.cutover, self.classes, self.groups)

    def pool(self):
        return audit_purpose_pools(self.root, self.cutover, date(2026, 9, 30), self.pools)

    def assert_field(self, action, path, marker, code, occurrence=0):
        lines = [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if line.strip().removeprefix("- ").startswith(marker)]
        self.assertGreater(len(lines), occurrence, marker)
        with self.assertRaises(BudgetError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.source, f"{path}:{lines[occurrence]}")

    def test_valid_equation_and_nominal_balances_remain_exact_and_read_only(self):
        before = {path: path.read_bytes() for path in self.base.iterdir()}
        benefits = self.benefits()
        self.assertEqual(benefits["review_state"], "complete")
        self.assertEqual(benefits["proved"][0]["income_report_value"], "10")
        pool = self.pool()["pools"][0]
        self.assertEqual(pool["protected_nominal_principal"], "50")
        self.assertEqual(pool["remainder_book_cost"], "59")
        self.assertEqual(before, {path: path.read_bytes() for path in self.base.iterdir()})

    def test_exact_costed_sale_proves_the_source_without_a_redundant_manual_equation_review(self):
        pending = audit_economic_benefits(self.root, self.cutover, self.classes)
        self.assertEqual(pending["review_state"], "complete")
        self.assertEqual(pending["proved_benefits"], 1)
        self.assertEqual(pending["pending_benefits"], [])
        self.assertEqual(pending["proved"][0]["proof"], "exact_costed_sale_group_v1")
        proposed = propose(self.root, self.cutover, self.classes, "CNY")
        self.assertEqual(proposed["proposed_groups"], 1)
        self.assertEqual(proposed["unsupported_complex_benefits"], 0)

    def test_benefit_header_errors_have_the_specific_field_line(self):
        for field, value in (("extra", True), ("schema", True), ("cutover", "2026-08-01"),
                ("ledger_snapshot", "invalid"), ("loader_version", "other"), ("report_currency", "USD"), ("approvals", {})):
            document = deepcopy(self.group_doc)
            document[field] = value
            self.write(self.groups, document)
            with self.subTest(field=field):
                self.assert_field(self.benefits, self.groups, field + ":", "BENEFIT_GROUP_INVALID")

    def test_benefit_row_and_malformed_reference_errors_do_not_escape_as_python_errors(self):
        for field, value, code in (("extra", True, "BENEFIT_GROUP_INVALID"),
                ("reason", "", "BENEFIT_GROUP_INVALID"), ("income_ref", [], "BENEFIT_GROUP_INVALID"),
                ("income_ref", {"date": date(2026, 9, 1)}, "BENEFIT_GROUP_REF_INVALID"),
                ("income_ref", {"x": 1, 1: "mixed key types"}, "BENEFIT_GROUP_REF_INVALID"),
                ("income_ref", {}, "BENEFIT_GROUP_REF_INVALID")):
            document = deepcopy(self.group_doc)
            document["approvals"][0][field] = value
            self.write(self.groups, document)
            with self.subTest(field=field, value=value):
                self.assert_field(self.benefits, self.groups, field + ":", code)

    def test_duplicate_benefit_points_to_second_claim_instead_of_serialized_ref(self):
        document = deepcopy(self.group_doc)
        document["approvals"].append(deepcopy(document["approvals"][0]))
        self.write(self.groups, document)
        self.assert_field(self.benefits, self.groups, "income_ref:", "BENEFIT_GROUP_DUPLICATE", 1)

    def test_pool_header_errors_have_the_specific_field_line(self):
        for field, value in (("extra", True), ("schema", True), ("cutover", "2026-08-01"),
                             ("report_currency", "USD"), ("pools", {})):
            document = deepcopy(self.pool_doc)
            document[field] = value
            self.write(self.pools, document)
            with self.subTest(field=field):
                self.assert_field(self.pool, self.pools, field + ":", "POOL_DECLARATION_INVALID")

    def test_pool_nested_fields_including_unhashable_order_are_located(self):
        for field, value, code in (("extra", True, "POOL_DECLARATION_INVALID"),
                ("id", "", "POOL_DECLARATION_INVALID"), ("accounts", ["Assets:Cash", "Assets:Cash"], "POOL_DECLARATION_INVALID"),
                ("reserve_order", [{"not": "an account"}], "POOL_RESERVE_ORDER_INVALID"),
                ("reserve_order", ["Assets:Cash"], "POOL_RESERVE_ORDER_INVALID"),
                ("payment_scope", "payable", "POOL_PAYMENT_SCOPE_INVALID"),
                ("reason", "", "POOL_REASON_MISSING"), ("protected_owner_id", "invalid id", "POOL_OWNER_INVALID"),
                ("remainder_owner_id", "savings", "POOL_OWNER_INVALID"), ("protected_label", " ", "POOL_LABEL_INVALID"),
                ("protected_principal", "0", "POOL_PRINCIPAL_INVALID"),
                ("remainder_policy", [], "POOL_REMAINDER_POLICY_INVALID")):
            document = deepcopy(self.pool_doc)
            document["pools"][0][field] = value
            self.write(self.pools, document)
            with self.subTest(field=field, value=value):
                self.assert_field(self.pool, self.pools, field + ":", code)

    def test_duplicate_pool_id_and_shared_account_point_to_the_second_pool(self):
        document = deepcopy(self.pool_doc)
        document["pools"].append(deepcopy(document["pools"][0]))
        self.write(self.pools, document)
        self.assert_field(self.pool, self.pools, "id:", "POOL_DECLARATION_INVALID", 1)
        document["pools"][1]["id"] = "other"
        self.write(self.pools, document)
        self.assert_field(self.pool, self.pools, "Assets:Cash", "POOL_ACCOUNT_INVALID", 2)

    def test_pool_inventory_error_points_to_the_ledger_transaction(self):
        self.root.write_text(self.root.read_text(encoding="utf-8") +
            '\n2026-09-03 * "virtual overspend"\n  Assets:Cash -150 CNY\n  Expenses:Fee 150 CNY\n', encoding="utf-8")
        self.assert_field(self.pool, self.root, '2026-09-03 *', "POOL_NEGATIVE_LOT")

    def test_cli_outputs_located_error_without_partial_report(self):
        document = deepcopy(self.pool_doc)
        document["pools"][0]["reserve_order"] = [{"not": "an account"}]
        self.write(self.pools, document)
        with redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
            code = main(["--ledger", str(self.root), "--cutover", "2026-09-01", "--as-of", "2026-09-30",
                         "--purpose-pool-audit", "--purpose-pools", str(self.pools)])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        error = json.loads(errors.getvalue())["error"]
        self.assertEqual(error["code"], "POOL_RESERVE_ORDER_INVALID")
        self.assertTrue(error["source"].startswith(str(self.pools) + ":"))


if __name__ == "__main__":
    unittest.main()

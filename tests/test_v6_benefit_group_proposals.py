"""A candidate group records full refs and the booked cost equation."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.v6.benefits import audit_economic_benefits
from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.tools.v6_propose_benefit_groups import propose


class BenefitGroupProposalTests(unittest.TestCase):
    def test_costed_sale_candidate_is_not_approved(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            review = Path(folder) / "classes.yml"
            root.write_text(
                'option "operating_currency" "CNY"\n'
                '2026-09-01 open Assets:Cash\n'
                '2026-09-01 open Assets:Security\n'
                '2026-09-01 open Income:Gain\n'
                '2026-09-01 open Expenses:Fee\n'
                '2026-09-01 * "buy"\n'
                '  Assets:Security 10 SH {2 CNY}\n'
                '  Assets:Cash -20 CNY\n'
                '2026-09-02 * "sell"\n'
                '  Assets:Security -10 SH {2 CNY} @ 3 CNY\n'
                '  Assets:Cash 29 CNY\n'
                '  Expenses:Fee 1 CNY\n'
                '  Income:Gain -10 CNY\n', encoding="utf-8")
            candidates = audit_classification(root, date(2026, 9, 1))["missing_classifications"]
            review.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-09-01", "loader_version": BEANCOUNT_VERSION,
                "categories": [{"id": "generic", "name": "通用", "parent": None, "aliases": []}],
                "classifications": [{"claim": row["ref"], "reason": "虚拟账本",
                                     "slices": [{"id": f"slice_{i}", "category_id": "generic",
                                                 "signed_units": row["signed_units"]}]}
                                    for i, row in enumerate(candidates)],
            }, allow_unicode=True), encoding="utf-8")
            result = propose(root, date(2026, 9, 1), review, "CNY")
            self.assertEqual(result["pending_complex_benefits"], 1)
            self.assertEqual(result["proposed_groups"], 1)
            group = result["proposals"][0]
            self.assertEqual(group["status"], "proposed")
            self.assertEqual((group["principal_report_value"], group["income_report_value"],
                              group["fee_report_value"], group["cash_receipt_report_value"]),
                             ("20", "10", "1", "29"))
            self.assertEqual(len(group["fee_refs"]), 1)
            approval = Path(folder) / "benefits.yml"
            approval.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-09-01",
                "ledger_snapshot": result["ledger_snapshot"],
                "loader_version": BEANCOUNT_VERSION, "report_currency": "CNY",
                "approvals": [{"income_ref": group["income_ref"],
                               "reason": "虚拟账本逐项核对成本与收益"}],
            }, allow_unicode=True), encoding="utf-8")
            benefit_audit = audit_economic_benefits(root, date(2026, 9, 1), review, approval)
            self.assertEqual(benefit_audit["review_state"], "complete")
            self.assertEqual(benefit_audit["proved_benefits"], 1)
            self.assertEqual(benefit_audit["proved"][0]["proof"],
                             "approved_costed_sale_equation")
            stale = yaml.safe_load(approval.read_text())
            stale["ledger_snapshot"] = "stale"
            approval.write_text(yaml.safe_dump(stale, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(BudgetError) as error:
                audit_economic_benefits(root, date(2026, 9, 1), review, approval)
            self.assertEqual(error.exception.code, "BENEFIT_GROUP_INVALID")


if __name__ == "__main__":
    unittest.main()

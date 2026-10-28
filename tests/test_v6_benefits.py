"""Benefit source claims must identify a booked receipt, not a matching amount."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.v6.benefits import audit_economic_benefits
from beancount_budget_tool.v6.classification import audit_classification


class BenefitAuditTests(unittest.TestCase):
    def test_simple_refund_and_income_have_distinct_source_refs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-01 open Income:Reward\n'
                '2026-10-02 * "refund"\n'
                '  Assets:Cash 10 CNY\n'
                '  Expenses:Food -10 CNY\n'
                '2026-10-03 * "independent reward"\n'
                '  Assets:Cash 10 CNY\n'
                '  Income:Reward -10 CNY\n', encoding="utf-8")
            candidates = audit_classification(root, date(2026, 10, 1))["missing_classifications"]
            sidecar = Path(folder) / "classes.yml"
            sidecar.write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01", "loader_version": BEANCOUNT_VERSION,
                "categories": [
                    {"id": "food", "name": "餐饮", "parent": None, "aliases": []},
                    {"id": "living", "name": "生活费", "parent": None, "aliases": []}],
                "classifications": [
                    {"claim": candidate["ref"], "reason": "虚拟账本", "slices": [
                        {"id": f"slice_{i}", "category_id": "food" if i == 0 else "living",
                         "signed_units": "-10"}]}
                    for i, candidate in enumerate(candidates)],
            }, allow_unicode=True), encoding="utf-8")
            audit = audit_economic_benefits(root, date(2026, 10, 1), sidecar)
            self.assertEqual(audit["review_state"], "complete")
            self.assertEqual(audit["proved_benefits"], 2)
            self.assertNotEqual(audit["proved"][0]["evidence_ref"],
                                audit["proved"][1]["evidence_ref"])

    def test_shared_counterpart_requires_group(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "main.bean"
            root.write_text(
                '2026-10-01 open Assets:Cash\n'
                '2026-10-01 open Expenses:Food\n'
                '2026-10-01 open Income:Reward\n'
                '2026-10-02 * "both benefits"\n'
                '  Assets:Cash 20 CNY\n'
                '  Expenses:Food -10 CNY\n'
                '  Income:Reward -10 CNY\n', encoding="utf-8")
            audit = audit_economic_benefits(root, date(2026, 10, 1))
            self.assertEqual(audit["proved_benefits"], 0)
            self.assertEqual(len(audit["pending_benefits"]), 2)


if __name__ == "__main__":
    unittest.main()

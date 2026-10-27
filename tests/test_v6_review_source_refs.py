"""A stale review is reported completely without changing approved decisions."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.reference_checks import diagnose
from beancount_budget_tool.v6.facts import FactIndex


class ReviewSourceRefTests(unittest.TestCase):
    def test_lists_all_stale_refs_and_keeps_review_untouched(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            ledger = base / "main.bean"
            ledger.write_text('''2026-09-01 open Assets:Cash CNY
2026-09-01 open Expenses:Food CNY
2026-09-02 * "meal"
  Expenses:Food 10 CNY
  Assets:Cash -10 CNY
''', encoding="utf-8")
            fin = load_financial(ledger)
            facts = FactIndex(fin)
            transaction = next(entry for entry in fin.entries if hasattr(entry, "postings"))
            valid = facts.for_posting(transaction.postings[0]).as_dict()
            first = {**valid, "transaction_sha256": "0" * 64}
            second = {**valid, "transaction_sha256": "1" * 64}
            review = base / "review.yaml"
            review.write_text(yaml.safe_dump({
                "loader_version": "old-loader", "claims": [
                    {"claim": valid}, {"claim": first}, {"claim": second}]
            }), encoding="utf-8")
            original = review.read_bytes()
            config = base / "config.yaml"
            config.write_text('ledger: main.bean\nclassifications: review.yaml\n',
                              encoding="utf-8")

            result = diagnose(config)
            self.assertEqual(result["status"], "NEEDS_REVIEW")
            self.assertEqual(result["stale_reference_count"], 2)
            self.assertEqual(result["loader_mismatch_count"], 1)
            stale = result["sidecars"][0]["stale_references"]
            self.assertEqual([row["document_path"] for row in stale],
                             ["$.claims[1].claim", "$.claims[2].claim"])
            self.assertEqual([row["candidate_count"] for row in stale], [1, 1])
            self.assertTrue(all(row["review_source"].startswith(str(review.resolve()) + ":")
                                for row in stale))
            self.assertEqual(review.read_bytes(), original)

    def test_current_ref_is_only_a_diagnostic_not_a_signature(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            ledger = base / "main.bean"
            ledger.write_text('''2026-09-01 open Assets:Cash CNY
2026-09-01 open Income:Salary CNY
2026-09-02 * "salary"
  Assets:Cash 10 CNY
  Income:Salary -10 CNY
''', encoding="utf-8")
            fin = load_financial(ledger)
            transaction = next(entry for entry in fin.entries if hasattr(entry, "postings"))
            ref = FactIndex(fin).for_posting(transaction.postings[0]).as_dict()
            (base / "review.yaml").write_text(yaml.safe_dump({
                "loader_version": fin.loader_version, "claim": ref}), encoding="utf-8")
            config = base / "config.yaml"
            config.write_text('ledger: main.bean\nownership: review.yaml\n', encoding="utf-8")

            result = diagnose(config)
            self.assertEqual(result["status"], "REFERENCES_CURRENT")
            self.assertEqual(result["stale_reference_count"], 0)
            self.assertIn("正式审计", result["note"])

    def test_lists_inline_and_external_double_claims_in_one_pass(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            ledger = base / "main.bean"
            ledger.write_text('''2026-09-01 open Assets:Cash CNY
2026-09-01 open Assets:Fund CNY
2026-09-01 open Expenses:Food CNY
2026-09-02 * "buy and spend"
  Assets:Fund 50 CNY
    fund: "储蓄"
  Expenses:Food 10 CNY
    budget: "生活费"
  Assets:Cash -60 CNY
    fund: "自由资金"
''', encoding="utf-8")
            fin = load_financial(ledger)
            transaction = next(entry for entry in fin.entries if hasattr(entry, "postings"))
            facts = FactIndex(fin)
            fund, expense, cash = [facts.for_posting(posting).as_dict()
                                   for posting in transaction.postings]
            for name, content in (
                ("ownership.yaml", {"claims": [{"claim": cash}]}),
                ("classifications.yaml",
                 {"classifications": [{"claim": expense}]}),
                ("k.yaml", {"events": [{"k_ref": fund}]}),
            ):
                (base / name).write_text(yaml.safe_dump(content), encoding="utf-8")
            config = base / "config.yaml"
            config.write_text('ledger: main.bean\nownership: ownership.yaml\n'
                              'classifications: classifications.yaml\nk_events: k.yaml\n',
                              encoding="utf-8")

            result = diagnose(config)
            self.assertEqual(result["status"], "NEEDS_REVIEW")
            self.assertEqual(result["stale_reference_count"], 0)
            self.assertEqual(result["inline_conflict_count"], 3)
            by_field = {row["field"]: row for row in result["sidecars"]}
            for field in ("ownership", "classifications", "k_events"):
                conflict = by_field[field]["inline_conflicts"][0]
                self.assertTrue(conflict["review_source"].startswith(by_field[field]["file"] + ":"))
                self.assertFalse(conflict["matched_by_candidate"])


if __name__ == "__main__":
    unittest.main()

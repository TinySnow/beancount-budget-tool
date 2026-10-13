"""Private account rules expand only matching booked postings."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.classification import audit_classification
from beancount_budget_tool.tools.v6_expand_classification_rules import expand


class RuleExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "main.bean"
        self.rules = Path(self.temp.name) / "rules.yaml"
        self.root.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-10-01 open Assets:Cash\n'
            '2026-10-01 open Expenses:Fee\n'
            '2026-10-01 open Income:Interest\n'
            '2026-10-02 * "fee"\n'
            '  Expenses:Fee 1.25 CNY\n'
            '  Assets:Cash -1.25 CNY\n'
            '2026-10-03 * "interest"\n'
            '  Assets:Cash 2 CNY\n'
            '  Income:Interest -2 CNY\n', encoding="utf-8")

    def write_rules(self, extra=None, stale=False, expense_default=False,
                    income_default=False):
        digest = audit_classification(self.root, date(2026, 10, 1))["ledger_snapshot"]
        rules = [{"id": "fee_living", "account": "Expenses:Fee",
                  "first_date": "2026-10-01", "last_date": "2026-10-31",
                  "category_id": "living", "reason": "本次审阅批准"},
                 {"id": "interest_investment", "account": "Income:Interest",
                  "first_date": "2026-10-01", "last_date": "2026-10-31",
                  "category_id": "investment", "reason": "本次审阅批准"}]
        if extra:
            rules.append(extra)
        document = {
            "schema": 6, "cutover": "2026-10-01",
            "ledger_snapshot": "stale" if stale else digest,
            "loader_version": BEANCOUNT_VERSION,
            "categories": [
                {"id": "living", "name": "生活费", "parent": None, "aliases": []},
                {"id": "investment", "name": "投资", "parent": None, "aliases": []}],
            "rules": rules,
        }
        if expense_default:
            document["expense_default"] = {
                "first_date": "2026-10-01", "last_date": "2026-10-31",
                "category_id": "living", "reason": "已批准的私人费用默认规则"}
        if income_default:
            document["income_default"] = {
                "first_date": "2026-10-01", "last_date": "2026-10-31",
                "category_id": "living", "reason": "已批准的私人收入默认规则"}
        self.rules.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

    def test_generic_rules_expand_to_exact_posting_claims(self):
        self.write_rules()
        output, summary = expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(summary["matched_rules"], {"fee_living": 1, "interest_investment": 1})
        self.assertEqual(summary["still_unreviewed"], 0)
        path = Path(self.temp.name) / "classifications.yaml"
        path.write_text(yaml.safe_dump(output, allow_unicode=True), encoding="utf-8")
        checked = audit_classification(self.root, date(2026, 10, 1), path)
        self.assertEqual(checked["review_state"], "complete")
        self.assertEqual([row["slices"][0]["signed_units"] for row in checked["classifications"]],
                         ["5/4", "-2"])

    def test_overlap_and_stale_snapshot_reject(self):
        self.write_rules(extra={"id": "another_fee", "account": "Expenses:Fee",
                                "first_date": "2026-10-02", "last_date": "2026-10-02",
                                "category_id": "living", "reason": "重复"})
        with self.assertRaises(BudgetError) as caught:
            expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(caught.exception.code, "PRIVATE_RULE_OVERLAP")
        self.write_rules(stale=True)
        with self.assertRaises(BudgetError) as caught:
            expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(caught.exception.code, "PRIVATE_RULE_STALE")

    def test_expense_default_is_visible_and_does_not_claim_income_or_inline(self):
        text = self.root.read_text(encoding="utf-8")
        text = text.replace('2026-10-01 open Income:Interest\n',
                            '2026-10-01 open Income:Interest\n'
                            '2026-10-01 open Income:Gift\n'
                            '2026-10-01 open Expenses:Other\n')
        text += ('2026-10-04 * "other fee"\n'
                 '  Expenses:Other 3 CNY\n'
                 '  Assets:Cash -3 CNY\n'
                 '2026-10-05 * "explicit outside"\n'
                 '  Expenses:Other 1 CNY\n'
                 '    budget: "-"\n'
                 '  Assets:Cash -1 CNY\n'
                 '2026-10-06 * "unreviewed income"\n'
                 '  Assets:Cash 4 CNY\n'
                 '  Income:Gift -4 CNY\n')
        self.root.write_text(text, encoding="utf-8")
        self.write_rules(expense_default=True)
        output, summary = expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(summary["expense_default_matches"], 1)
        self.assertEqual(summary["generated_claims"], 3)
        self.assertEqual(summary["still_unreviewed"], 1)
        path = Path(self.temp.name) / "classifications.yaml"
        path.write_text(yaml.safe_dump(output, allow_unicode=True), encoding="utf-8")
        checked = audit_classification(self.root, date(2026, 10, 1), path)
        self.assertEqual(checked["claimed_postings"], 4)
        self.assertEqual(len(checked["missing_classifications"]), 1)

    def test_income_default_only_claims_unlabeled_income_after_specific_rules(self):
        source = self.root.read_text(encoding="utf-8")
        source = source.replace('2026-10-01 open Income:Interest\n',
                                '2026-10-01 open Income:Interest\n'
                                '2026-10-01 open Income:Gift\n')
        source += ('2026-10-06 * "gift"\n'
                   '  Assets:Cash 4 CNY\n'
                   '  Income:Gift -4 CNY\n'
                   '2026-10-07 * "explicit outside"\n'
                   '  Assets:Cash 1 CNY\n'
                   '  Income:Gift -1 CNY\n'
                   '    budget: "-"\n')
        self.root.write_text(source, encoding="utf-8")
        self.write_rules(income_default=True)
        output, summary = expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(summary["income_default_matches"], 1)
        self.assertEqual(summary["expense_default_matches"], 0)
        self.assertEqual(summary["matched_rules"]["interest_investment"], 1)
        path = Path(self.temp.name) / "classifications.yaml"
        path.write_text(yaml.safe_dump(output, allow_unicode=True), encoding="utf-8")
        checked = audit_classification(self.root, date(2026, 10, 1), path)
        self.assertEqual(checked["review_state"], "complete")
        interest = next(x for x in checked["classifications"] if x["account"] == "Income:Interest")
        gift = next(x for x in checked["classifications"] if x["account"] == "Income:Gift" and
                    x["slices"][0]["signed_units"] == "-4")
        self.assertEqual(interest["slices"][0]["category_id"], "investment")
        self.assertEqual(gift["slices"][0]["category_id"], "living")

    def test_income_default_can_leave_ambiguous_account_for_review(self):
        source = self.root.read_text(encoding="utf-8")
        source = source.replace('2026-10-01 open Income:Interest\n',
                                '2026-10-01 open Income:Interest\n'
                                '2026-10-01 open Income:Gift\n'
                                '2026-10-01 open Income:Wages\n')
        source += ('2026-10-06 * "ambiguous gift"\n'
                   '  Assets:Cash 4 CNY\n'
                   '  Income:Gift -4 CNY\n'
                   '2026-10-07 * "wages"\n'
                   '  Assets:Cash 5 CNY\n'
                   '  Income:Wages -5 CNY\n')
        self.root.write_text(source, encoding="utf-8")
        self.write_rules(income_default=True)
        document = yaml.safe_load(self.rules.read_text(encoding="utf-8"))
        document["income_default"]["exclude_accounts"] = ["Income:Gift"]
        self.rules.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        output, summary = expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual((summary["income_default_matches"], summary["still_unreviewed"]),
                         (1, 1))
        self.assertEqual(next(row for row in output["classifications"]
                              if row["slices"][0]["signed_units"] == "-5")
                         ["slices"][0]["category_id"], "living")
        document["income_default"]["exclude_accounts"] *= 2
        self.rules.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(caught.exception.code, "PRIVATE_RULE_INVALID")

    def test_income_default_prefix_exclusion_keeps_new_finance_accounts_pending(self):
        source = self.root.read_text(encoding="utf-8")
        source = source.replace('2026-10-01 open Income:Interest\n',
                                '2026-10-01 open Income:Interest\n'
                                '2026-10-01 open Income:Finance:Unknown\n'
                                '2026-10-01 open Income:Finance:Known\n'
                                '2026-10-01 open Income:Wages\n')
        source += ('2026-10-06 * "new finance income"\n'
                   '  Assets:Cash 4 CNY\n'
                   '  Income:Finance:Unknown -4 CNY\n'
                   '2026-10-06 * "known finance income"\n'
                   '  Assets:Cash 3 CNY\n'
                   '  Income:Finance:Known -3 CNY\n'
                   '2026-10-07 * "wages"\n'
                   '  Assets:Cash 5 CNY\n'
                   '  Income:Wages -5 CNY\n')
        self.root.write_text(source, encoding="utf-8")
        self.write_rules(income_default=True, extra={
            "id": "known_finance", "account": "Income:Finance:Known",
            "first_date": "2026-10-01", "last_date": "2026-10-31",
            "category_id": "investment", "reason": "已审金融收入"})
        document = yaml.safe_load(self.rules.read_text(encoding="utf-8"))
        document["income_default"]["exclude_account_prefixes"] = ["Income:Finance"]
        self.rules.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        output, summary = expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(summary["matched_rules"]["interest_investment"], 1)
        self.assertEqual(summary["matched_rules"]["known_finance"], 1)
        self.assertEqual((summary["income_default_matches"], summary["still_unreviewed"]),
                         (1, 1))
        path = Path(self.temp.name) / "prefix.yaml"
        path.write_text(yaml.safe_dump(output, allow_unicode=True), encoding="utf-8")
        checked = audit_classification(self.root, date(2026, 10, 1), path)
        self.assertEqual([row["account"] for row in checked["missing_classifications"]],
                         ["Income:Finance:Unknown"])
        document["income_default"]["exclude_account_prefixes"] *= 2
        self.rules.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(caught.exception.code, "PRIVATE_RULE_INVALID")

    def test_append_import_keeps_reviewed_exception_and_applies_old_rule(self):
        self.write_rules()
        original, _ = expand(self.root, date(2026, 10, 1), self.rules)
        fee = next(row for row in original["classifications"]
                   if row["slices"][0]["signed_units"] == "5/4")
        fee["slices"][0]["category_id"] = "investment"
        fee["reason"] = "manually reviewed exception"
        existing = Path(self.temp.name) / "existing.yaml"
        existing.write_text(yaml.safe_dump(original, allow_unicode=True), encoding="utf-8")
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-10-04 * "new imported fee"\n'
                             '  Expenses:Fee 3 CNY\n'
                             '  Assets:Cash -3 CNY\n', encoding="utf-8")
        refreshed, summary = expand(self.root, date(2026, 10, 1), self.rules, existing)
        self.assertEqual((summary["preserved_claims"], summary["generated_claims"],
                          summary["still_unreviewed"]), (2, 1, 0))
        self.assertEqual(next(row for row in refreshed["classifications"]
                              if row["slices"][0]["signed_units"] == "5/4")
                         ["slices"][0]["category_id"], "investment")
        output = Path(self.temp.name) / "refreshed.yaml"
        output.write_text(yaml.safe_dump(refreshed, allow_unicode=True), encoding="utf-8")
        self.assertEqual(audit_classification(self.root, date(2026, 10, 1), output)
                         ["review_state"], "complete")

    def test_changed_reviewed_source_does_not_silently_refresh(self):
        self.write_rules()
        original, _ = expand(self.root, date(2026, 10, 1), self.rules)
        existing = Path(self.temp.name) / "existing.yaml"
        existing.write_text(yaml.safe_dump(original, allow_unicode=True), encoding="utf-8")
        source = self.root.read_text(encoding="utf-8")
        self.root.write_text(source.replace("1.25 CNY", "1.30 CNY")
                             .replace("-1.25 CNY", "-1.30 CNY"), encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            expand(self.root, date(2026, 10, 1), self.rules, existing)
        self.assertEqual(caught.exception.code, "CLAIM_REF_INVALID")

    def test_named_inline_budget_is_kept_above_private_default(self):
        self.write_rules(expense_default=True)
        self.root.write_text(self.root.read_text(encoding="utf-8") +
                             '2026-10-04 * "reviewed inline"\n'
                             '  Expenses:Fee 3 CNY\n'
                             '    budget: "投资"\n'
                             '  Assets:Cash -3 CNY\n', encoding="utf-8")
        output, summary = expand(self.root, date(2026, 10, 1), self.rules)
        self.assertEqual(summary["generated_claims"], 2)
        path = Path(self.temp.name) / "inline.yaml"
        path.write_text(yaml.safe_dump(output, allow_unicode=True), encoding="utf-8")
        checked = audit_classification(self.root, date(2026, 10, 1), path)
        self.assertEqual(checked["review_state"], "complete")
        self.assertEqual(next(row for row in checked["classifications"]
                              if row["origin"] == "inline")["slices"][0]["category_id"],
                         "investment")


if __name__ == "__main__":
    unittest.main()

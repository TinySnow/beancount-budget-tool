"""A month's source review exposes fund errors at their booked lines."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from beancount import __version__ as BEANCOUNT_VERSION
import yaml

from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.h_fx import audit_h_fx
from beancount_budget_tool.v6.monthly_report import audit_month_workflow
from beancount_budget_tool.v6.provenance import asset_opening_fingerprint
from beancount_budget_tool.v6.purpose_pools import opening_fingerprint
from beancount_budget_tool.v6.review_queue import audit_review_queue, render_text


class FundReviewWorkflowTests(unittest.TestCase):
    def _config(self, base: Path, from_fund: str, to_fund: str,
                *, infer_defaults: bool = True, include_k: bool = False,
                include_fx: bool = False) -> tuple[Path, Path]:
        ledger = base / "main.bean"
        ledger.write_text(
            'option "operating_currency" "CNY"\n'
            '2026-08-01 open Assets:Bank:A\n'
            '2026-08-01 open Assets:Bank:B\n'
            '2026-08-01 open Assets:Broker:Cash\n'
            '2026-08-01 open Equity:Opening\n'
            '2026-08-01 open Income:Salary\n'
            '2026-08-31 * "opening"\n'
            '  Assets:Broker:Cash 4000 CNY\n'
            '  Equity:Opening -4000 CNY\n'
            '2026-10-02 * "income"\n'
            '  Assets:Bank:A 700 CNY\n'
            '    fund: "自由资金100，储蓄300，旅游300"\n'
            '  Income:Salary -700 CNY\n'
            '2026-10-03 * "move"\n'
            '  Assets:Bank:A -600 CNY\n'
            f'    fund: "{from_fund}"\n'
            '  Assets:Bank:B 600 CNY\n'
            f'    fund: "{to_fund}"\n' +
            ('2026-10-04 open Assets:FundBook\n'
             '2026-10-05 * "fund subscription"\n'
             '  Assets:FundBook 50 CNY\n'
             '  Assets:Bank:A -50 CNY\n'
             '    fund: "储蓄"\n' if include_k else '') +
            ('2026-10-04 open Assets:Bank:USD\n'
             '2026-10-06 * "buy dollars"\n'
             '  Assets:Bank:A -70 CNY\n'
             '    fund: "储蓄"\n'
             '  Assets:Bank:USD 10 USD @@ 70 CNY\n'
             '    fund: "储蓄"\n' if include_fx else ''), encoding="utf-8")
        fin = load_financial(ledger)
        snapshot = fin.digest
        cutover = "2026-10-01"

        def save(name: str, document: dict) -> None:
            (base / name).write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")

        save("accounts.yaml", {
            "schema": 6, "cutover": cutover, "ledger_snapshot": snapshot,
            "opening_fingerprint": asset_opening_fingerprint(fin, date(2026, 10, 1)),
            "report_currency": "CNY", "accounts": [
                {"account": account, "semantics": "H", "balance_semantics": "real_inventory",
                 "payment_scope": {"CNY": "payable" if account != "Assets:Broker:Cash"
                                   else "excluded"}, "reason": "virtual account"}
                for account in ("Assets:Bank:A", "Assets:Bank:B", "Assets:Broker:Cash")]
                + ([{"account": "Assets:FundBook", "semantics": "K",
                     "balance_semantics": "signed_subscription_flow",
                     "payment_scope": {"CNY": "excluded"}, "reason": "virtual fund book"}]
                   if include_k else [])
                + ([{"account": "Assets:Bank:USD", "semantics": "H",
                     "balance_semantics": "real_inventory",
                     "payment_scope": {"USD": "excluded"}, "reason": "virtual FX wallet"}]
                   if include_fx else [])})
        ownership_doc = {
            "schema": 6, "cutover": cutover, "ledger_snapshot": snapshot,
            "loader_version": BEANCOUNT_VERSION,
            "owners": [{"id": ident, "name": name, "aliases": []}
                       for ident, name in (("free", "自由资金"), ("savings", "储蓄"),
                                           ("travel", "旅游"))],
            "claims": []}
        if infer_defaults:
            ownership_doc["defaults"] = {"income_owner_id": "free",
                                         "expense_owner_id": "free"}
        save("ownership.yaml", ownership_doc)
        save("obligations.yaml", {"schema": 6, "cutover": cutover,
                                  "ledger_snapshot": snapshot,
                                  "loader_version": BEANCOUNT_VERSION,
                                  "owners": [], "events": []})
        save("classifications.yaml", {"schema": 6, "cutover": cutover,
                                       "loader_version": BEANCOUNT_VERSION,
                                       "categories": [{"id": "living", "name": "生活费",
                                                       "parent": None, "aliases": []}],
                                       "classifications": []})
        pool_accounts = ["Assets:Broker:Cash"]
        save("pool.yaml", {"schema": 6, "cutover": cutover, "report_currency": "CNY",
                           "pools": [{"id": "broker", "accounts": pool_accounts,
                                      "reserve_order": pool_accounts,
                                      "opening_fingerprint": opening_fingerprint(
                                          fin, date(2026, 10, 1), pool_accounts),
                                      "payment_scope": "excluded", "protected_owner_id": "savings",
                                      "protected_label": "额外储蓄",
                                      "protected_principal": "3000",
                                      "remainder_owner_id": "investment",
                                      "remainder_label": "待投资金", "reason": "virtual pool"}]})
        config = base / "report.yaml"
        save("report.yaml", {"schema": 6, "ledger": "main.bean", "cutover": cutover,
                             "month": "2026-10", "accounts": "accounts.yaml",
                             "ownership": "ownership.yaml", "obligations": "obligations.yaml",
                             "classifications": "classifications.yaml",
                             "purpose_pools": "pool.yaml"})
        return config, ledger

    def test_bad_fund_total_becomes_source_review_not_a_cli_error(self):
        with TemporaryDirectory() as folder:
            config, ledger = self._config(Path(folder),
                "自由资金100，储蓄200，旅游300", "自由资金100，储蓄200，旅游200")
            result = audit_month_workflow(config)
            self.assertEqual((result["status"], result["stage"]), ("NEEDS_REVIEW", "source"))
            review = result["review_queue"]
            self.assertEqual(review["counts"], {"fund_amount": 1})
            self.assertEqual(review["items"][0]["source"], f"{ledger.resolve()}:17")
            self.assertIn("FUND_GRAMMAR", render_text(review))

    def test_cross_account_mixed_remap_is_reviewed_with_both_locations(self):
        with TemporaryDirectory() as folder:
            config, ledger = self._config(Path(folder),
                "自由资金50，储蓄250，旅游300", "自由资金100，储蓄200，旅游300")
            review = audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 31))
            move = next(row for row in review["items"] if row["kind"] == "h_transfer")
            self.assertEqual(move["reason_code"], "H_TRANSFER_MIXED_REMAP_PENDING")
            self.assertEqual((move["source"], move["to_source"]),
                             (f"{ledger.resolve()}:15", f"{ledger.resolve()}:17"))
            self.assertEqual((audit_month_workflow(config)["status"]), "NEEDS_REVIEW")

    def test_matching_mixed_transfer_has_no_transfer_review(self):
        with TemporaryDirectory() as folder:
            config, _ = self._config(Path(folder),
                "自由资金100，储蓄200，旅游300", "自由资金100，储蓄200，旅游300")
            review = audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 31))
            self.assertFalse(any(row["kind"] in ("fund_amount", "h_transfer")
                                 for row in review["items"]))

    def test_owned_transfer_without_event_claim_has_both_source_lines(self):
        with TemporaryDirectory() as folder:
            config, ledger = self._config(Path(folder),
                "自由资金100，储蓄200，旅游300", "自由资金100，储蓄200，旅游300",
                infer_defaults=False)
            review = audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 31))
            pending = [row for row in review["items"] if row["kind"] == "h_transfer"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["reason_code"], "H_TRANSFER_PENDING")
            self.assertEqual((pending[0]["source"], pending[0]["to_source"]),
                             (f"{ledger.resolve()}:15", f"{ledger.resolve()}:17"))
            self.assertIn(f"{ledger.resolve()}:17", render_text(review))

    def test_unreviewed_k_subscription_names_cash_and_k_sources(self):
        with TemporaryDirectory() as folder:
            config, ledger = self._config(Path(folder),
                "自由资金100，储蓄200，旅游300", "自由资金100，储蓄200，旅游300",
                include_k=True)
            review = audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 31))
            pending = [row for row in review["items"] if row["kind"] == "k_event"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["reason_code"], "K_EVENT_PENDING")
            self.assertEqual((pending[0]["source"], pending[0]["to_source"]),
                             (f"{ledger.resolve()}:21", f"{ledger.resolve()}:22"))
            self.assertIn(f"{ledger.resolve()}:22", render_text(review))

    def test_unreviewed_fx_names_both_currencies_and_source_lines(self):
        with TemporaryDirectory() as folder:
            config, ledger = self._config(Path(folder),
                "自由资金100，储蓄200，旅游300", "自由资金100，储蓄200，旅游300",
                include_fx=True)
            # An unproven FX still exposes both sources. Complete same-fund
            # inline evidence is now sufficient without a separate event file.
            ledger.write_text(ledger.read_text(encoding="utf-8").replace(
                '  Assets:Bank:USD 10 USD @@ 70 CNY\n    fund: "储蓄"\n',
                '  Assets:Bank:USD 10 USD @@ 70 CNY\n'), encoding="utf-8")
            review = audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 31))
            pending = [row for row in review["items"] if row["kind"] == "h_fx"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["reason_code"], "H_FX_PENDING")
            self.assertEqual((pending[0]["commodity"], pending[0]["to_commodity"]),
                             ("CNY", "USD"))
            self.assertEqual((pending[0]["source"], pending[0]["to_source"]),
                             (f"{ledger.resolve()}:21", f"{ledger.resolve()}:23"))

    def test_reviewed_fx_is_not_listed_as_a_pending_transfer(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            config, ledger = self._config(base,
                "自由资金100，储蓄200，旅游300", "自由资金100，储蓄200，旅游300",
                include_fx=True)
            candidate = audit_h_fx(ledger, date(2026, 10, 1),
                                   base / "accounts.yaml", base / "ownership.yaml")["events"][0]
            (base / "fx.yaml").write_text(yaml.safe_dump({
                "schema": 6, "cutover": "2026-10-01",
                "ledger_snapshot": load_financial(ledger).digest,
                "loader_version": BEANCOUNT_VERSION,
                "events": [{"from_ref": candidate["from_ref"],
                            "to_ref": candidate["to_ref"], "owner_id": "savings",
                            "signed_from_units": "-70", "signed_to_units": "10",
                            "boundary_role": "preserve", "reason": "reviewed virtual FX"}]},
                allow_unicode=True), encoding="utf-8")
            document = yaml.safe_load(config.read_text(encoding="utf-8"))
            document["h_fx"] = "fx.yaml"
            config.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
            review = audit_review_queue(config, date(2026, 10, 1), date(2026, 10, 31))
            self.assertFalse(any(row["kind"] in ("h_fx", "h_transfer")
                                 for row in review["items"]))


if __name__ == "__main__":
    unittest.main()

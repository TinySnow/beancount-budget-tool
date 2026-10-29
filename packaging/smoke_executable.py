"""Exercise the frozen app from a Unicode directory outside the source checkout."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

import yaml


def check(executable: Path, *, onedir: bool = False) -> None:
    source = Path(__file__).resolve().parents[1] / "examples" / "v6"
    with TemporaryDirectory(prefix="budget-exe-") as temporary:
        directory = (Path(temporary) / "独立运行 空格").resolve()
        directory.mkdir()
        binary = directory / executable.name
        if onedir:
            if not (executable.resolve().parent / "_internal").is_dir():
                raise AssertionError("Folder bundle must include _internal beside the executable")
            shutil.copytree(executable.resolve().parent, directory, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(executable.resolve(), binary)
        environment = os.environ.copy()
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        # No interpreter, venv Scripts directory, or build dependency DLLs in PATH.
        # os.environ is case insensitive on Windows; a copied dict is not.
        environment["PATH"] = (str(Path(os.environ["SystemRoot"]) / "System32")
                               if os.name == "nt" else "/usr/bin:/bin")

        def run(*args, code=0, env=None):
            print(f"Standalone check: {args[0] if args else 'default config'}", flush=True)
            result = subprocess.run([str(binary), *map(str, args)], cwd=directory,
                                    env=environment if env is None else env, capture_output=True,
                                    encoding="utf-8", timeout=90)
            if result.returncode != code:
                raise AssertionError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
            return result.stdout, result.stderr

        def document(*args):
            return json.loads(run(*args, "--format", "json")[0])

        run("--version")
        multicurrency = directory / "多币种收支"
        shutil.copytree(source / "multicurrency", multicurrency)
        multi_config = multicurrency / "config.yml"
        multi = document("--config", multi_config, "--transactions")
        assert multi["status"] == "CERTIFIED_NATIVE_SCOPED", multi
        view = multi["report"]["multicurrency"]
        assert view["native_expenses"][0]["currency"] == "JPY", view
        assert view["native_expenses"][0]["net"] == "550", view
        assert 'report_value' not in view["native_expenses"][0], view
        assert view["native_income"][0]["net"] == "25", view
        assert 'report_value' not in view["native_income"][0], view
        assert multi["report"]["actual_spendable"]["actual_spendable"] == "1000", multi
        assert multi["report"]["max_spendable"]["max_spendable"] == "100", multi
        multi_period = document("report", "--config", multi_config, "--from", "2025-01", "--through", "2025-02")
        assert multi_period["status"] == "CERTIFIED_NATIVE_SCOPED", multi_period
        native = multi_period["report"]["multicurrency"]["native_expenses"]
        assert {(row["currency"], row["net"]) for row in native} == {("JPY", "550"), ("CNY", "5")}, native
        native_text = run("--config", multi_config, "--format", "text")[0]
        assert "550" in native_text and "JPY" in native_text and '缺价' not in native_text, native_text
        equity_dir = directory / "已有本金权益入账"
        shutil.copytree(source / "simple", equity_dir)
        equity_ledger = equity_dir / "main.bean"
        equity_source = equity_ledger.read_text(encoding="utf-8") + (
            '\n2026-09-01 open Equity:Init\n2026-09-09 * "old principal received"\n'
            '  Assets:Cash 7894.17 CNY\n    fund: "额外储蓄"\n'
            '  Equity:Init -7894.17 CNY\n')
        equity_ledger.write_text(equity_source, encoding="utf-8")
        received = document("--config", equity_dir / "config.yml", "--transactions")
        assert received["status"] == "CERTIFIED_SCOPED", received
        report = received["report"]
        savings = next(key for key, name in report["owner_names"].items() if name == "额外储蓄")
        assert next(row for row in report["purpose_progress"]["owners"] if row["owner_id"] == savings)["actual_g"] == "0", received
        assert next(row for row in report["account_purposes"]["rows"] if row["owner_id"] == savings)["units"] == "789417/100", received
        assert report["max_spendable"]["max_spendable"] == "190", received
        event = next(row for row in report["transactions"]["rows"] if row["narration"] == "old principal received")
        assert event["purpose_events"][0]["protocol"]["protocol"] == "explicit_equity_receipt_v1", event
        assert event["net_allocations"] == {}, event
        equity_ledger.write_text(equity_source.replace('    fund: "额外储蓄"\n', ''), encoding="utf-8")
        missing_equity_fund = document("--config", equity_dir / "config.yml")
        assert missing_equity_fund["report"] is None, missing_equity_fund
        assert any(row["reason_code"] == "OWNERSHIP_UNREVIEWED" for row in missing_equity_fund["review_items"]), missing_equity_fund
        liabilities = directory / "负债自动清算"
        shutil.copytree(source / "liabilities", liabilities)
        automatic = document("--config", liabilities / "config.yml", "--transactions")
        assert automatic["status"] == "CERTIFIED_SCOPED", automatic
        liability_report = automatic["report"]
        assert liability_report["actual_spendable"]["actual_spendable"] == "2150", automatic
        assert liability_report["max_spendable"]["max_spendable"] == "410", automatic
        assert liability_report["default_outflows"]["totals"][0]["expenses_paid"] == "10", automatic
        assert liability_report["default_outflows"]["totals"][0]["debt_payments"] == "320", automatic
        assert not (liabilities / ".budget-state").exists(), "Ordinary rules must not require saved per-event approvals"
        liability_ledger = liabilities / "main.bean"
        liability_ledger.write_text(liability_ledger.read_text(encoding="utf-8") +
            '\n2026-09-07 * "prepay"\n  Assets:Bank -50 CNY\n  Liabilities:Card 50 CNY\n'
            '2026-09-08 * "return credit"\n  Assets:Bank 50 CNY\n  Liabilities:Card -50 CNY\n', encoding="utf-8")
        credit = document("--config", liabilities / "config.yml")
        assert credit["report"]["actual_spendable"]["actual_spendable"] == "2150", credit
        assert credit["report"]["max_spendable"]["max_spendable"] == "410", credit
        assert credit["report"]["default_outflows"]["totals"][0]["credit_returns"] == "50", credit
        assert credit["report"]["default_outflows"]["totals"][0]["net_outflow"] == "330", credit
        h_bundle = directory / "组合转账与手续费"
        shutil.copytree(liabilities, h_bundle)
        profile = yaml.safe_load((h_bundle / "config.yml").read_text(encoding="utf-8"))
        profile["accounts"]["Assets:Wallet"] = {"semantics": "H", "payment_scope": {"CNY": "excluded"},
                                                "opening": {"CNY": {"额外储蓄": "20"}}}
        (h_bundle / "config.yml").write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        bundle_ledger = h_bundle / "main.bean"
        bundle_ledger.write_text('2026-08-01 open Assets:Wallet\n'
            '2026-08-31 * "wallet opening"\n  Assets:Wallet 20 CNY\n  Equity:Opening -20 CNY\n' +
            bundle_ledger.read_text(encoding="utf-8") +
            '\n2026-09-07 * "withdraw and fee"\n  Assets:Bank 10 CNY\n    fund: "额外储蓄"\n'
            '  Assets:Wallet -10 CNY\n    fund: "额外储蓄"\n  Expenses:Food 0.1 CNY\n'
            '  Assets:Bank -0.1 CNY\n    fund: "生活费"\n'
            '2026-09-08 * "multi leg inherited transfer"\n  Assets:Wallet -2 CNY\n'
            '  Assets:Bank 1 CNY\n  Assets:Wallet 1 CNY\n', encoding="utf-8")
        joint = document("--config", h_bundle / "config.yml", "--transactions")
        assert joint["status"] == "CERTIFIED_SCOPED", joint
        assert joint["report"]["default_outflows"]["totals"][0]["expenses_paid"] == "101/10", joint
        group = [e for tx in joint["report"]["transactions"]["rows"] for e in tx["purpose_events"] if e["kind"] == "h_bundle"]
        assert len(group) == 2 and all(e["net_allocations"] == {} for e in group), group
        assert group[0]["protocol"]["asset_total"] == "-1/10" and group[0]["protocol"]["expense_total"] == "1/10", group
        redemptions = directory / "H理财本金及收益"
        shutil.copytree(liabilities, redemptions)
        profile = yaml.safe_load((redemptions / "config.yml").read_text(encoding="utf-8"))
        profile["accounts"]["Assets:Wealth"] = {"semantics": "H", "payment_scope": {"CNY": "excluded"},
                                                "opening": {"CNY": {"额外储蓄": "2200"}}}
        (redemptions / "config.yml").write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        redemption_ledger = redemptions / "main.bean"
        redemption_ledger.write_text(
            '2026-08-01 open Assets:Wealth\n2026-08-01 open Income:Gain\n'
            '2026-08-31 * "wealth opening"\n  Assets:Wealth 2200 CNY\n  Equity:Opening -2200 CNY\n' +
            redemption_ledger.read_text(encoding="utf-8") +
            '\n2026-09-09 * "H redemption 1"\n  Assets:Bank 1010.71 CNY\n    fund: "额外储蓄"\n'
            '  Assets:Wealth -1000 CNY\n    fund: "额外储蓄"\n  Income:Gain -10.71 CNY\n'
            '2026-09-10 * "H redemption 2"\n  Assets:Bank 1216.11 CNY\n    fund: "额外储蓄"\n'
            '  Assets:Wealth -1200 CNY\n    fund: "额外储蓄"\n  Income:Gain -16.11 CNY\n', encoding="utf-8")
        redeemed = document("--config", redemptions / "config.yml", "--transactions")
        assert redeemed["status"] == "CERTIFIED_SCOPED", redeemed
        assert redeemed["report"]["actual_spendable"]["actual_spendable"] == "2150", redeemed
        assert redeemed["report"]["max_spendable"]["max_spendable"] == "410", redeemed
        rows = [row for row in redeemed["report"]["transactions"]["rows"]
                if any(event["kind"] == "h_redemption" for event in row["purpose_events"])]
        assert len(rows) == 2 and all(row["net_allocations"] == {} for row in rows), redeemed
        gains = [row["purpose_events"][0]["protocol"]["income_native"] for row in rows]
        assert gains == ["1071/100", "1611/100"], gains
        distribution = document("snapshot", "--config", redemptions / "config.yml", "--as-of", "2026-09-09")
        assert distribution["status"] == "CERTIFIED_SCOPED", distribution
        wealth = [r for r in distribution["report"]["positions"] if r["account"] == "Assets:Wealth"]
        assert len(wealth) == 1 and wealth[0]["amount"] == "1200" and wealth[0]["basis"] == "H", wealth
        history = document("history", "--config", redemptions / "config.yml", "--as-of", "2026-09-30",
                           "--account", "Assets:Wealth", "--purpose", "额外储蓄")
        assert history["report"]["closing_positions"] == [], history
        assert [r["delta"] for r in history["report"]["events"]] == ["-1000", "-1200"], history
        assert all("main.bean:" in r["source"] for r in history["report"]["events"]), history
        query_export = directory / "科目用途流水.csv"
        run("history", "--config", redemptions / "config.yml", "--as-of", "2026-09-30",
            "--account", "Assets:Wealth", "--format", "csv", "--output", query_export)
        assert query_export.read_bytes().startswith(b"\xef\xbb\xbf")
        posting_budgets = directory / "逐笔负债用途"
        shutil.copytree(source / "liabilities", posting_budgets)
        profile = yaml.safe_load((posting_budgets / "config.yml").read_text(encoding="utf-8"))
        profile.pop("liabilities", None)
        profile["liability_budget_mode"] = "purpose"
        (posting_budgets / "config.yml").write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        posting_ledger = posting_budgets / "main.bean"
        body = posting_ledger.read_text(encoding="utf-8")
        for old, new in (("卡债", "生活费"), ("偿债", "生活费"), ("代收代付", "同事差旅费")):
            body = body.replace(f'budget: "{old}"', f'budget: "{new}"')
        body += '\n2026-09-09 * "mixed purposes"\n  Expenses:Food 100 CNY\n'
        body += '  Liabilities:Card -100 CNY\n    budget: "生活费60,旅游40"\n'
        body += '2026-09-10 * "full payment"\n  Assets:Bank -100 CNY\n  Liabilities:Card 100 CNY\n'
        posting_ledger.write_text(body, encoding="utf-8")
        tagged = document("--config", posting_budgets / "config.yml", "--transactions")
        assert tagged["status"] == "CERTIFIED_SCOPED", tagged
        assert tagged["report"]["actual_spendable"]["actual_spendable"] == "2050", tagged
        assert tagged["report"]["max_spendable"]["max_spendable"] == "310", tagged
        assert not tagged["report"]["actual_spendable"]["booked_debts"], tagged
        assert not (posting_budgets / ".budget-state").exists(), "Posting budgets must not require saved approvals"
        mixed_receipts = directory / "个人收入及预付收回"
        shutil.copytree(posting_budgets, mixed_receipts)
        mixed_ledger = mixed_receipts / "main.bean"
        mixed_ledger.write_text(mixed_ledger.read_text(encoding="utf-8").replace(
            '2026-09-06 * "转交同事款项', '2026-09-04 * "转交同事款项'), encoding="utf-8")
        mixed = document("--config", mixed_receipts / "config.yml", "--transactions")
        assert mixed["status"] == "CERTIFIED_SCOPED", mixed
        assert mixed["report"]["actual_spendable"]["actual_spendable"] == "2050", mixed
        assert mixed["report"]["max_spendable"]["max_spendable"] == "310", mixed
        assert mixed["report"]["default_outflows"]["totals"][0]["credit_returns"] == "240", mixed
        assert mixed["report"]["default_outflows"]["totals"][0]["net_outflow"] == "190", mixed
        receipts = [row["credit_income_receipt"] for row in mixed["report"]["transactions"]["rows"]
                    if "credit_income_receipt" in row]
        assert len(receipts) == 1 and receipts[0]["income_native"] == "240", mixed
        if os.name == "nt" and not onedir:
            # The bootloader must use its own per-user runtime base, even if
            # the caller's system TEMP settings cannot contain a directory.
            unavailable = directory / "not-a-temporary-directory"
            unavailable.write_text("not a directory", encoding="utf-8")
            restricted = {**environment, "TMP": str(unavailable), "TEMP": str(unavailable)}
            run("--version", env=restricted)
            print("Windows one-file startup passed with unusable TEMP/TMP.", flush=True)
        if onedir:
            # A file cannot contain a new _MEI directory, even for an admin.
            # Folder startup must not rely on the one-file extraction path.
            unavailable = directory / "not-a-temporary-directory"
            unavailable.write_text("not a directory", encoding="utf-8")
            restricted = {**environment, "TMP": str(unavailable), "TEMP": str(unavailable),
                          "TMPDIR": str(unavailable)}
            run("--version", env=restricted)
            print("Folder startup passed with unusable TEMP/TMP/TMPDIR.", flush=True)
        for command in ("monthly", "configure", "report", "audit", "history", "snapshot"):
            run(command, "--help")
        for retired in ("init", "review", "project", "next-month"):
            _, error = run(retired, "--help", code=2)
            assert json.loads(error)["error"]["code"] == "COMMAND_RETIRED", error
        for filename in ("main.bean", "config.yml", "budgets.yml"):
            shutil.copy2(source / "simple" / filename, directory / filename)
        ledger, config = directory / "main.bean", directory / "config.yml"
        ledger.write_text(ledger.read_text(encoding="utf-8") + "\n2026-09-04 balance Assets:Cash 490 CNY\n", encoding="utf-8")
        # The default launcher discovers config.yml; no registration or compile.
        direct = document()
        assert direct["status"] == "CERTIFIED_SCOPED" and not direct["review_items"], direct
        assert direct["report"]["max_spendable"]["max_spendable"] == "190", direct
        assert "190 CNY" in run()[0]
        created = json.loads(run("configure", "--ledger", ledger, "--month", "2026-09", "--output", "created")[0])
        assert {p.name for p in (directory / "created").iterdir()} == {"config.yml", "budgets.yml"}
        assert created["config"].endswith("config.yml"), created
        generated = document("--config", created["config"])
        assert generated["report"] is None and generated["review_items"], generated
        # First foreign-currency posting in a grouped account: ask for its
        # payment scope, never invent CNY or approve JPY as direct cash.
        fx_dir = directory / "首次外币"
        shutil.copytree(source / "simple", fx_dir)
        fx_config = fx_dir / "config.yml"
        fx_profile = yaml.safe_load(fx_config.read_text(encoding="utf-8"))
        fx_profile.pop("accounts")
        fx_profile["account_groups"] = {"H": {"payable": ["Assets:Cash", "Assets:Foreign"]}}
        fx_config.write_text(yaml.safe_dump(fx_profile, allow_unicode=True), encoding="utf-8")
        fx_ledger = fx_dir / "main.bean"
        fx_ledger.write_text(fx_ledger.read_text(encoding="utf-8") +
            '\n2026-09-01 open Assets:Foreign\n2026-09-04 price JPY 0.05 CNY\n'
            '2026-09-04 * "foreign income"\n  Assets:Foreign 100 JPY\n  Income:Salary -100 JPY\n', encoding="utf-8")
        pending_fx = document("--config", fx_config)
        assert pending_fx["status"] == "NEEDS_REVIEW" and pending_fx["report"] is None, pending_fx
        assert any("Assets:Foreign / JPY" in row["detail"] for row in pending_fx["review_items"]), pending_fx
        fx_profile["accounts"] = {"Assets:Foreign": {"payment_scope": {"JPY": "excluded"}}}
        fx_config.write_text(yaml.safe_dump(fx_profile, allow_unicode=True), encoding="utf-8")
        fx_inputs = {p: p.read_bytes() for p in fx_dir.iterdir() if p.is_file()}
        approved_fx = document("--config", fx_config)
        assert approved_fx["status"] == "CERTIFIED_SCOPED", approved_fx
        assert all(p.read_bytes() == body for p, body in fx_inputs.items())
        assert not (fx_dir / ".budget-state").exists()
        # An explicitly excluded H account covers all real commodities,
        # including subsequently added security codes, with no code list.
        fx_profile.pop("accounts")
        fx_profile["account_groups"]["H"] = {"payable": ["Assets:Cash"], "excluded": ["Assets:Foreign"]}
        fx_config.write_text(yaml.safe_dump(fx_profile, allow_unicode=True), encoding="utf-8")
        original_fx_config = fx_config.read_bytes()
        for code in ("SEC_000001", "SEC_000002"):
            fx_ledger.write_text(fx_ledger.read_text(encoding="utf-8") +
                f'\n2026-09-05 price {code} 10 CNY\n2026-09-05 * "security income"\n'
                f'  Assets:Foreign 1 {code}\n  Income:Salary -1 {code}\n', encoding="utf-8")
            securities = document("--config", fx_config)
            assert securities["status"] == "CERTIFIED_SCOPED", securities
            assert fx_config.read_bytes() == original_fx_config
        fx_text = fx_ledger.read_text(encoding="utf-8") + (
            '\n2026-09-06 * "exact yen purchase"\n'
            '  Assets:Foreign 550 JPY @@ 25.70 CNY\n    fund: "生活费"\n'
            '  Assets:Cash -25.70 CNY\n    fund: "生活费"\n')
        fx_profile["account_groups"]["H"]["excluded"].append("Assets:YenBank")
        fx_config.write_text(yaml.safe_dump(fx_profile, allow_unicode=True), encoding="utf-8")
        original_fx_config = fx_config.read_bytes()
        fx_text += ('\n2026-09-01 open Assets:YenBank\n2026-09-07 * "same-purpose yen move"\n'
                    '  Assets:Foreign -550 JPY\n    fund: "生活费"\n'
                    '  Assets:YenBank 550 JPY\n    fund: "生活费"\n')
        fx_ledger.write_text(fx_text, encoding="utf-8")
        exact_fx = document("--config", fx_config)
        assert exact_fx["status"] == "CERTIFIED_SCOPED", exact_fx
        living = next(row for row in exact_fx["report"]["purpose_progress"]["owners"]
                      if exact_fx["report"]["owner_names"][row["owner_id"]] == "生活费")
        assert living["actual_g"] == "200", exact_fx
        fx_ledger.write_text(fx_text.replace('  Assets:Foreign 550 JPY @@ 25.70 CNY\n    fund: "生活费"\n',
                                           '  Assets:Foreign 550 JPY @@ 25.70 CNY\n'), encoding="utf-8")
        missing_fund = document("--config", fx_config)
        assert missing_fund["report"] is None, missing_fund
        assert any(row["reason_code"] == "H_FX_PENDING" and "在 h_fx 中" not in row["detail"]
                   for row in missing_fund["review_items"]), missing_fund
        fx_ledger.write_text(fx_text + (
            '\n2026-09-01 open Liabilities:YenCard\n2026-09-08 * "prepay yen card"\n'
            '  Assets:YenBank -550 JPY\n    fund: "生活费"\n'
            '  Liabilities:YenCard 550 JPY\n    budget: "生活费"\n'
            '2026-09-09 * "unvalued yen card expense"\n  Expenses:Food 550 JPY\n'
            '  Liabilities:YenCard -550 JPY\n    budget: "生活费"\n'), encoding="utf-8")
        unvalued = document("--config", fx_config)
        assert unvalued["status"] == "CERTIFIED_NATIVE_SCOPED" and unvalued["report"] is not None, unvalued
        assert next(row for row in unvalued['report']['multicurrency']['native_expenses']
                    if row['currency'] == 'JPY')['net'] == '550', unvalued
        assert not any(row["reason_code"] == "VALUATION_MISSING" for row in unvalued["review_items"]), unvalued
        assert not any(row["reason_code"] in ("UNSUPPORTED", "NAMED_H_EVENT_UNSUPPORTED")
                       for row in unvalued["review_items"]), unvalued
        assert fx_config.read_bytes() == original_fx_config
        assert not (fx_dir / ".budget-state").exists()
        # Same source, portable path spelling, same complete audited report.
        profile = yaml.safe_load(config.read_text(encoding="utf-8"))
        profile["ledger"], profile["budgets"] = ".\\main.bean", ".\\budgets.yml"
        config.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        assert document("--config", config)["report"] == direct["report"]
        # The two public files retain the existing schema, including rejection
        # of the withdrawn declaration block even when it is empty.
        original_config = config.read_bytes()
        profile['declarations'] = {}
        config.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding='utf-8')
        _, error = run('--config', config, '--format', 'json', code=2)
        assert json.loads(error)['error']['code'] == 'SIMPLE_CONFIG_INVALID', error
        config.write_bytes(original_config)
        budget_path = directory / 'budgets.yml'
        original_budgets = budget_path.read_bytes()
        plans = yaml.safe_load(original_budgets)
        plans['months']['2026-09']['declarations'] = {}
        budget_path.write_text(yaml.safe_dump(plans, allow_unicode=True), encoding='utf-8')
        _, error = run('--config', config, '--format', 'json', code=2)
        assert json.loads(error)['error']['code'] == 'SIMPLE_BUDGETS_INVALID', error
        budget_path.write_bytes(original_budgets)
        # The executable has no hidden review-state input or save workflow.
        assert not (directory / '.budget-state').exists()
        for command in ('state', 'clean', 'review'):
            _, stderr = run(command, '--config', config, code=2)
            assert 'COMMAND_RETIRED' in stderr, stderr
        assert not (directory / '.budget-state').exists()
        legacy = directory / '.budget-state/config/reviews.json.gz'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'poisoned old review data: must never be read')
        old_bytes = legacy.read_bytes()
        assert document()['report'] == direct['report']
        assert legacy.read_bytes() == old_bytes
        salary = document("report", "--config", config, "--payday", "2026-09-01",
                          "--next-payday", "2026-10-01", "--cycle-budget", "200")
        assert salary["report"]["cycle_budget"]["remaining"] == "190", salary
        payday_dir = directory / "payday"
        shutil.copytree(source / "simple", payday_dir)
        payday_config = payday_dir / "config.yml"
        profile = yaml.safe_load(payday_config.read_text(encoding="utf-8"))
        profile["expense_budget_basis"] = "salary_cycle"
        payday_config.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        plans_path = payday_dir / "budgets.yml"
        plans = yaml.safe_load(plans_path.read_text(encoding="utf-8"))
        plans["defaults"].pop("expense_targets")
        plans["salary_cycles"] = {day: "100" for day in ("2026-09-02", "2026-10-02")}
        plans_path.write_text(yaml.safe_dump(plans, allow_unicode=True), encoding="utf-8")
        payday_month = document("--config", payday_config)
        assert payday_month["report"]["max_spendable"]["max_spendable"] == "90", payday_month
        saved_cycle = document("report", "--config", payday_config, "--cycle", "2026-09-02")
        assert saved_cycle["report"]["cycle_budget"]["remaining"] == "90", saved_cycle
        plans["salary_cycles"].pop("2026-09-02")
        plans_path.write_text(yaml.safe_dump(plans, allow_unicode=True), encoding="utf-8")
        before_first = document("--config", payday_config)
        assert before_first["status"] == "CERTIFIED_SCOPED", before_first
        assert before_first["report"]["salary_cycle"]["status"] == "NOT_STARTED", before_first
        assert before_first["report"]["max_spendable"]["max_spendable"] is None, before_first
        # Included importer files can put a real recharge behind a payment by
        # line number. Only an explicit cash-order declaration resolves it.
        (directory / "order-main.bean").write_text(
            'option "operating_currency" "CNY"\n2026-09-01 open Assets:Wallet\n'
            '2026-09-01 open Income:Pay\n2026-09-01 open Expenses:Food\n'
            '2026-09-01 * "opening"\n  Assets:Wallet 46.32 CNY\n  Income:Pay -46.32 CNY\n'
            'include "order-payment.bean"\ninclude "order-recharge.bean"\n', encoding="utf-8")
        (directory / "order-payment.bean").write_text(
            '2026-09-02 * "payment"\n  Expenses:Food 199 CNY\n  Assets:Wallet -199 CNY\n', encoding="utf-8")
        recharge = '\n' * 40 + '2026-09-02 * "recharge"\n  Assets:Wallet 200 CNY\n  Income:Pay -200 CNY\n'
        (directory / "order-recharge.bean").write_text(recharge, encoding="utf-8")
        (directory / "order-config.yml").write_text(
            'schema: 1\nledger: order-main.bean\nbudgets: order-budgets.yml\n'
            'cutover: "2026-09-01"\nmonth: "2026-09"\nreport_currency: CNY\n'
            'defaults: {income_purpose: Living, expense_purpose: Living, expense_category: Daily}\n'
            'accounts:\n  Assets:Wallet:\n    semantics: H\n    payment_scope: {CNY: payable}\n', encoding="utf-8")
        (directory / "order-budgets.yml").write_text(
            'schema: 1\ndefaults:\n  expense_targets: {Daily: 2000}\n'
            'months:\n  "2026-09":\n    completeness_confirmed: true\n', encoding="utf-8")
        unordered = document("--config", "order-config.yml")
        assert any(row["reason_code"] == "H_NEGATIVE_INVENTORY" for row in unordered["review_items"]), unordered
        (directory / "order-recharge.bean").write_text(
            recharge.replace('* "recharge"\n', '* "recharge"\n  budget-order: -1\n'), encoding="utf-8")
        ordered = document("--config", "order-config.yml", "--transactions")
        assert ordered["status"] == "CERTIFIED_SCOPED", ordered
        assert [row["narration"] for row in ordered["report"]["transactions"]["rows"]] == ["opening", "recharge", "payment"]
        plans_path = directory / "budgets.yml"
        plans = yaml.safe_load(plans_path.read_text(encoding="utf-8"))
        pending = document("--month", "2026-10")
        assert pending["report"] is None and any(r["reason_code"] == "EXTERNAL_SCOPE_PENDING" for r in pending["review_items"]), pending
        plans["months"]["2026-10"] = {"completeness_confirmed": True}
        plans_path.write_text(yaml.safe_dump(plans, allow_unicode=True), encoding="utf-8")
        assert document("--month", "2026-10")["status"] == "CERTIFIED_SCOPED"
        cumulative = document("report", "--config", config, "--from", "2026-09", "--through", "2026-10")
        assert cumulative["status"] == "CERTIFIED_SCOPED", cumulative
        assert cumulative["report"]["closing"]["max_spendable"]["max_spendable"] == "190", cumulative
        run("report", "--config", config, "--from", "2026-09", "--through", "2026-10", "--format", "csv", "--output", "report.csv")
        assert (directory / "report.csv").read_bytes().startswith(b"\xef\xbb\xbf")
        _, collision = run("report", "--config", config, "--format", "csv", "--output", "report.csv", code=2)
        assert "PERIOD_OUTPUT_EXISTS" in collision, collision
        plans["defaults"]["expense_targets"]["生活开销"] = "not_assessed"
        plans_path.write_text(yaml.safe_dump(plans, allow_unicode=True), encoding="utf-8")
        historical = document()
        assert historical["status"] == "CERTIFIED_SCOPED", historical
        assert historical["report"]["actual_spendable"]["actual_spendable"] == "190", historical
        assert historical["report"]["max_spendable"]["max_spendable"] is None, historical
        # Import curses and its native extension, then stop at the expected
        # noninteractive-terminal guard. A pipe cannot validate keyboard/display.
        _, stderr = run("--config", config, "--format", "tui", code=2)
        assert json.loads(stderr)["error"]["code"] == "TUI_TERMINAL_REQUIRED", stderr
        original = ledger.read_text(encoding="utf-8")
        for old, new in (('fund: "生活费"', 'fund: "生活费199,自由资金0"'),
                         ('fund: "生活费"', 'fund: "生活费100,自由资金99"'),
                         ('budget: "生活开销.餐饮"', 'budget: "生活开销.餐饮9,生活开销0"')):
            assert old in original, old
            ledger.write_text(original.replace(old, new), encoding="utf-8")
            pending = document("--config", config)
            assert pending["report"] is None and pending["review_items"], pending
            assert any(str(ledger) + ":" in row["source"] for row in pending["review_items"]), pending
        print("Standalone smoke passed: two-file creation/default launch, explicit-input/ignored-legacy-state checks, "
              "monthly/salary/range, CSV/no overwrite, liabilities, H bundles/redemptions, "
              "history/snapshot, portable paths, curses import and located review queue.")



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--onedir", action="store_true", help="Copy and test the entire folder bundle")
    args = parser.parse_args()
    check(args.executable, onedir=args.onedir)

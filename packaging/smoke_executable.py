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
            print(f"Standalone check: {args[0] if args else 'default project'}", flush=True)
            result = subprocess.run([str(binary), *map(str, args)], cwd=directory,
                                    env=environment if env is None else env, capture_output=True,
                                    encoding="utf-8", timeout=90)
            if result.returncode != code:
                raise AssertionError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
            return result.stdout, result.stderr

        def document(*args):
            return json.loads(run(*args, "--format", "json")[0])

        run("--version")
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
        for command in ("monthly", "configure", "clean", "state", "init", "review-month", "review", "review-refs",
                        "next-month", "report", "audit", "classification-proposals", "project"):
            run(command, "--help")
        ledger = directory / "账本 2026.bean"
        text = (source / "first-month.bean").read_text(encoding="utf-8")
        ledger.write_text(text + "\n2026-09-04 balance Assets:Cash 490 CNY\n", encoding="utf-8")
        (directory / "main.bean").write_text('include "账本 2026.bean"\n', encoding="utf-8")
        draft = document("init", "--ledger", "main.bean", "--month", "2026-09", "--output", "draft")
        assert draft["status"] == "DRAFT_CREATED", draft
        instructions = (directory / "draft" / "START_HERE.md").read_text(encoding="utf-8")
        assert str(binary) in instructions, instructions
        sheet = (source / "first-month-setup.yaml").read_text(encoding="utf-8")
        (directory / "setup.yaml").write_text(sheet.replace("ledger: first-month.bean", "ledger: main.bean"),
                                                encoding="utf-8")
        created = document("init", "--apply", "setup.yaml", "--output", "approved")
        assert created["status"] == "CONFIG_CREATED", created
        config = Path(created["config"])
        outcome = document("--config", config, "--transactions")
        assert outcome["status"] == "CERTIFIED_SCOPED", outcome
        assert outcome["report"]["max_spendable"]["max_spendable"] == "190", outcome
        assert outcome["review_items"] == [], outcome
        simple = json.loads(run("configure", "--from-config", config, "--output", "two-files")[0])
        direct = document("--config", simple["config"], "--month", "2026-09")
        simple_path = Path(simple["config"])
        original_profile = simple_path.read_bytes()
        profile = yaml.safe_load(original_profile)
        profile["ledger"] = profile["ledger"].replace("/", "\\")
        profile["budgets"] = ".\\budgets.yml"
        simple_path.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        assert document("--config", simple_path, "--month", "2026-09")["report"] == direct["report"]
        simple_path.write_bytes(original_profile)
        assert direct["status"] == "CERTIFIED_SCOPED", direct
        assert direct["report"]["max_spendable"]["max_spendable"] == "190", direct
        state_root = Path(simple["config"]).parent / ".budget-state" / "config"
        cache = state_root / "runtime" / "old-cache"
        cache.mkdir(parents=True)
        (cache / "input.yaml").write_text("rebuildable: true", encoding="utf-8")
        preview = json.loads(run("clean", "--config", simple["config"], "--dry-run")[0])
        assert preview["files_removed"] == 1 and cache.exists(), preview
        cleaned = json.loads(run("clean", "--config", simple["config"])[0])
        assert cleaned["files_removed"] == 1 and not cache.exists(), cleaned
        assert [p.name for p in state_root.iterdir()] == ["reviews.json.gz"]
        assert document("--config", simple["config"])["report"] == direct["report"]
        exported = json.loads(run("state", "--config", simple["config"], "--export", "advanced-copy")[0])
        run("state", "--config", simple["config"], "--import-reviewed", exported["config"])
        assert document("--config", simple["config"])["report"] == direct["report"]
        direct_salary = document("report", "--config", simple["config"], "--payday", "2026-09-01",
                                 "--next-payday", "2026-10-01", "--cycle-budget", "200")
        assert direct_salary["report"]["cycle_budget"]["remaining"] == "190", direct_salary
        payday_dir = directory / "payday"
        shutil.copytree(Path(simple["config"]).parent, payday_dir)
        payday_config = payday_dir / "config.yml"
        profile = yaml.safe_load(payday_config.read_text(encoding="utf-8"))
        profile["expense_budget_basis"] = "salary_cycle"
        payday_config.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
        plans_path = payday_dir / "budgets.yml"
        plans = yaml.safe_load(plans_path.read_text(encoding="utf-8"))
        layers = [plans.get("defaults", {}), *plans.get("months", {}).values(), *plans.get("effective_templates", {}).values()]
        for layer in layers:
            layer.pop("expense_targets", None)
            layer.pop("expense_direct_targets", None)
        category = profile["defaults"]["expense_category"]
        plans["salary_cycles"] = {day: {"expense_targets": {category: "100"}}
                                  for day in ("2026-09-02", "2026-10-02")}
        plans_path.write_text(yaml.safe_dump(plans, allow_unicode=True), encoding="utf-8")
        payday_month = document("--config", payday_config)
        assert payday_month["status"] == "CERTIFIED_SCOPED", payday_month
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
        project = document("project", "--config", config, "--output", "budget-project.yaml")
        assert project["status"] == "PROJECT_CREATED", project
        assert document("--month", "2026-09")["report"]["max_spendable"]["max_spendable"] == "190"
        salary = document("report", "--payday", "2026-09-01", "--next-payday", "2026-10-01",
                          "--cycle-budget", "200")
        assert salary["status"] == "CERTIFIED_SCOPED" and salary["through"] == "2026-09-30", salary
        assert salary["report"]["cycle_budget"]["remaining"] == "190", salary
        # Forgotten historical targets must not be replaced with a made-up zero.
        unknown_sheet = (directory / "setup.yaml").read_text(encoding="utf-8")
        unassessed_sheet = unknown_sheet.replace("日常开销: '200'", "日常开销: not_assessed")
        assert unknown_sheet != unassessed_sheet
        (directory / "unassessed-setup.yaml").write_text(unassessed_sheet, encoding="utf-8")
        historical_config = document("init", "--apply", "unassessed-setup.yaml", "--output", "unassessed")["config"]
        historical = document("--config", historical_config)
        assert historical["status"] == "CERTIFIED_SCOPED", historical
        assert historical["report"]["actual_spendable"]["actual_spendable"] == "190", historical
        assert historical["report"]["max_spendable"]["status"] == "NOT_ASSESSED", historical
        assert historical["report"]["max_spendable"]["max_spendable"] is None, historical
        assert "190 CNY" in run()[0]
        run("report", "--format", "csv", "--output", "report.csv")
        assert (directory / "report.csv").read_bytes().startswith(b"\xef\xbb\xbf")
        review = document("review", "--project", "budget-project.yaml", "--output", "review")
        assert review["editable_items"] == 0, review
        untouched = document("review", "--project", "budget-project.yaml", "--apply", review["worksheet"])
        assert untouched["status"] == "NO_APPROVED_DECISIONS" and untouched["project_status"] == "UNCHANGED", untouched
        refs = document("review-refs", "--config", config)
        assert refs["status"] == "REFERENCES_CURRENT", refs
        following = document("next-month", "--project", "budget-project.yaml", "--output", "next")
        assert following["status"] == "DRAFT_CREATED", following
        worksheet = Path(following["worksheet"])
        # Confirm only the proposed targets of this public virtual fixture.
        # Deliberately leave October's month-end declaration unconfirmed.
        worksheet.write_text(worksheet.read_text(encoding="utf-8").replace(
            "targets_confirmed: false", "targets_confirmed: true"), encoding="utf-8")
        next_config = document("next-month", "--project", "budget-project.yaml", "--apply", worksheet)
        assert next_config["status"] == "MONTH_PREPARED" and next_config["project_status"] == "UPDATED", next_config
        updated = document("project", "--update", "budget-project.yaml", "--config", next_config["config"],
                           "--default-month", "2026-10")
        assert updated["status"] == "PROJECT_UPDATED" and updated["months"] == ["2026-09", "2026-10"], updated
        assert document()["report"] is None  # Default October still needs review.
        pending_period = document("report", "--project", "budget-project.yaml")
        assert pending_period["status"] == "NEEDS_REVIEW" and pending_period["report"] is None, pending_period
        september = document("report", "--from", "2026-09", "--through", "2026-09")
        assert september["report"]["closing"]["max_spendable"]["max_spendable"] == "190", september
        closed = document("next-month", "--project", "budget-project.yaml", "--confirm-completeness",
                          "--reason", "Public virtual October is complete", "--output", "october-closed")
        assert closed["project_status"] == "UPDATED" and closed["workflow"]["status"] == "CERTIFIED_SCOPED", closed
        assert document()["report"]["max_spendable"]["max_spendable"] == "190"
        # Import curses and its native extension, then stop at the expected
        # noninteractive-terminal guard. A pipe cannot validate keyboard/display.
        _, stderr = run("--config", config, "--format", "tui", code=2)
        assert json.loads(stderr)["error"]["code"] == "TUI_TERMINAL_REQUIRED", stderr
        original = ledger.read_text(encoding="utf-8")
        for old, new in (('fund: "待支出资金"', 'fund: "待支出资金199"'),
                         ('fund: "待支出资金"', 'fund: "待支出资金100,已收入资金99"'),
                         ('budget: "日常开销.餐饮"', 'budget: "日常开销.餐饮9"')):
            ledger.write_text(original.replace(old, new), encoding="utf-8")
            pending = document("--config", config)
            assert pending["report"] is None and pending["review_items"], pending
            assert any(str(ledger) + ":" in row["source"] for row in pending["review_items"]), pending
        print("Standalone smoke passed: init, 190 CNY certified report, Unicode/includes/balance plugin, "
              "project registration/update/range, CSV, review, references, next-month, curses import, located review queue.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--onedir", action="store_true", help="Copy and test the entire folder bundle")
    args = parser.parse_args()
    check(args.executable, onedir=args.onedir)

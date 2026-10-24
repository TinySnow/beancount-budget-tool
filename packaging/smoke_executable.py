"""Exercise the frozen app from a Unicode directory outside the source checkout."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory


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
        if onedir:
            # A file cannot contain a new _MEI directory, even for an admin.
            # Folder startup must not rely on the one-file extraction path.
            unavailable = directory / "not-a-temporary-directory"
            unavailable.write_text("not a directory", encoding="utf-8")
            restricted = {**environment, "TMP": str(unavailable), "TEMP": str(unavailable),
                          "TMPDIR": str(unavailable)}
            run("--version", env=restricted)
            print("Folder startup passed with unusable TEMP/TMP/TMPDIR.", flush=True)
        for command in ("monthly", "init", "review-month", "review", "review-refs",
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
        project = document("project", "--config", config, "--output", "budget-project.yaml")
        assert project["status"] == "PROJECT_CREATED", project
        assert document("--month", "2026-09")["report"]["max_spendable"]["max_spendable"] == "190"
        salary = document("report", "--payday", "2026-09-01", "--next-payday", "2026-10-01",
                          "--cycle-budget", "200")
        assert salary["status"] == "CERTIFIED_SCOPED" and salary["through"] == "2026-09-30", salary
        assert salary["report"]["cycle_budget"]["remaining"] == "190", salary
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

"""Exercise the frozen app from a Unicode directory outside the source checkout."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory


def check(executable: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "examples" / "v6"
    with TemporaryDirectory(prefix="budget-exe-") as temporary:
        directory = (Path(temporary) / "独立运行 空格").resolve()
        directory.mkdir()
        binary = directory / executable.name
        shutil.copy2(executable.resolve(), binary)
        environment = os.environ.copy()
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        # No interpreter, venv Scripts directory, or build dependency DLLs in PATH.
        # os.environ is case insensitive on Windows; a copied dict is not.
        environment["PATH"] = (str(Path(os.environ["SystemRoot"]) / "System32")
                               if os.name == "nt" else "/usr/bin:/bin")

        def run(*args, code=0):
            print(f"Standalone check: {args[0]}", flush=True)
            result = subprocess.run([str(binary), *map(str, args)], cwd=directory,
                                    env=environment, capture_output=True,
                                    encoding="utf-8", timeout=90)
            if result.returncode != code:
                raise AssertionError(f"{args}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
            return result.stdout, result.stderr

        def document(*args):
            return json.loads(run(*args, "--format", "json")[0])

        run("--version")
        for command in ("monthly", "init", "review-month", "review", "review-refs",
                        "next-month", "report", "audit", "classification-proposals"):
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
        run("report", "--config", config, "--format", "csv", "--output", "report.csv")
        assert (directory / "report.csv").read_bytes().startswith(b"\xef\xbb\xbf")
        review = document("review", "--config", config, "--output", "review")
        assert review["editable_items"] == 0, review
        refs = document("review-refs", "--config", config)
        assert refs["status"] == "REFERENCES_CURRENT", refs
        following = document("next-month", "--config", config, "--output", "next")
        assert following["status"] == "DRAFT_CREATED", following
        # Import curses and its native extension, then stop at the expected
        # noninteractive-terminal guard. A pipe cannot validate keyboard/display.
        _, stderr = run("--config", config, "--format", "tui", code=2)
        assert json.loads(stderr)["error"]["code"] == "TUI_TERMINAL_REQUIRED", stderr
        ledger.write_text(ledger.read_text(encoding="utf-8").replace('fund: "待支出资金"',
                                                                   'fund: "待支出资金100,已收入资金99"'), encoding="utf-8")
        pending = document("--config", config)
        assert pending["report"] is None and pending["review_items"], pending
        assert any(str(ledger) + ":" in row["source"] for row in pending["review_items"]), pending
        print("Standalone smoke passed: init, 190 CNY certified report, Unicode/includes/balance plugin, "
              "CSV, review, references, next-month, curses import, located review queue.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    check(parser.parse_args().executable)

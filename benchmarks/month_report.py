"""Measure source loading and complete report work without changing user inputs."""

import argparse
import cProfile
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import statistics
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from beancount_budget_tool.v6.simple_config import audit_simple
from beancount_budget_tool.v6.simple_tui import SimpleReportBrowser


def fingerprint(outcome):
    body = json.dumps(outcome, ensure_ascii=False, sort_keys=True)
    # Diagnostics may contain the fresh runtime directory. Keep every other
    # field, including financial values, provenance and review status, intact.
    body = re.sub(r'[^" ]*(?:/|\\\\)budget-runtime-[^/\\"]+', '<runtime>', body)
    return sha256(body.encode()).hexdigest()


def measure(config, month, repeats, details, profile):
    samples, fingerprints = [], []
    for _ in range(repeats):
        start = perf_counter()
        result = audit_simple(config, month, include_transactions=details)
        samples.append(perf_counter() - start)
        fingerprints.append(fingerprint(result))
    if len(set(fingerprints)) != 1:
        raise RuntimeError("Report output changed between measurements")
    print(json.dumps({"mode": "audit", "month": month, "seconds": samples,
                      "median": statistics.median(samples), "status": result["status"],
                      "output_sha256": fingerprints[0]}))
    if profile:
        cProfile.runctx("audit_simple(config, month, include_transactions=details)",
                        globals(), locals(), str(profile))
    browser = SimpleReportBrowser(config, month)
    try:
        samples, fingerprints = [], []
        for _ in range(repeats + 1):
            start = perf_counter()
            browser.refresh()
            samples.append(perf_counter() - start)
            if browser.error:
                raise browser.error
            fingerprints.append(fingerprint(browser.outcome))
        if len(set(fingerprints)) != 1:
            raise RuntimeError("Browser output changed between refreshes")
        print(json.dumps({"mode": "browser", "month": month, "first_seconds": samples[0],
                          "refresh_seconds": samples[1:], "refresh_median": statistics.median(samples[1:]),
                          "status": browser.outcome["status"], "output_sha256": fingerprints[0]}))
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Existing config: read only; otherwise use synthetic data")
    parser.add_argument("--month", default="2026-09")
    parser.add_argument("--transactions", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--details", action="store_true", help="Include transaction details in audit measurements")
    parser.add_argument("--profile", type=Path, help="Write cProfile data for one additional audit")
    args = parser.parse_args()
    if args.repeats < 1 or args.transactions < 0:
        parser.error("repeats must be positive and transactions must be nonnegative")
    if args.config:
        measure(args.config.resolve(), args.month, args.repeats, args.details, args.profile)
        return
    if args.month != "2026-09":
        parser.error("Synthetic fixture uses 2026-09; supply --config for another month")
    with TemporaryDirectory(prefix="budget-benchmark-") as temporary:
        folder = Path(temporary)
        for name in ("main.bean", "config.yml", "budgets.yml"):
            shutil.copyfile(REPO / "examples/v6/simple" / name, folder / name)
        with (folder / "main.bean").open("a", encoding="utf-8") as ledger:
            for index in range(args.transactions):
                ledger.write(f'\n2026-09-20 * "Benchmark {index}"\n'
                             '  Expenses:Food 0.01 CNY\n  Assets:Cash -0.01 CNY\n')
        measure(folder / "config.yml", args.month, args.repeats, args.details, args.profile)


if __name__ == "__main__":
    main()

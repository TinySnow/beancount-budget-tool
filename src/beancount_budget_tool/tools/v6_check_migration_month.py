"""Read-only structural gate for one month of a v6 ledger migration.

Both inputs must be full, independently loadable Beancount roots. By default
this gate preserves all booked facts. An explicit correction mode permits
booked changes only within the selected month and shows each before/after
fact for human review. It does not certify budget classification, ownership,
transfer events, plans, or a monthly report.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import sys

from beancount.core import data

from beancount_budget_tool.errors import BudgetError, require
from beancount_budget_tool.financial import load_financial, month_bounds as _month
from beancount_budget_tool.v6.source import collect_v6_source_issues, scan_v6_source

from .v6_make_review_copy import _financial_facts


TRANSACTION = re.compile(r"^\d{4}-\d{2}-\d{2}\s+[*!?]\s")


def _source_layout(fin) -> set[str]:
    base = fin.root.parent
    require(all(path.is_relative_to(base) for path in fin.sources),
            "MIGRATION_SOURCE_LAYOUT", "include 必须位于各自根账本目录内")
    return {path.relative_to(base).as_posix() for path in fin.sources}


def _options(fin) -> dict:
    # Loader paths and source hash naturally differ between independent copies.
    # Every other option can change how later v6 audits interpret the ledger.
    volatile = {"filename", "include", "input_hash", "dcontext", "documents"}
    return {key: value for key, value in fin.options.items() if key not in volatile}


def _outside_month_source(fin, first: date, last: date) -> dict[str, tuple[bool, str]]:
    """Mask only target-month transaction blocks; preserve every other byte."""
    result = {}
    for path, source in fin.sources.items():
        selected = []
        in_target = False
        for line in source.text.splitlines(keepends=True):
            if line and not line[0].isspace():
                match = TRANSACTION.match(line)
                in_target = bool(match and first <= date.fromisoformat(line[:10]) <= last)
            if not in_target:
                selected.append(line)
        relative = path.relative_to(fin.root.parent).as_posix()
        result[relative] = (path.read_bytes().startswith(b"\xef\xbb\xbf"), "".join(selected))
    return result


def _metadata(meta) -> dict:
    return {key: value for key, value in (meta or {}).items()
            if key not in ("filename", "lineno")}


def _entry_metadata(entry) -> tuple:
    own = _metadata(entry.meta)
    if isinstance(entry, data.Transaction):
        return own, tuple(_metadata(posting.meta) for posting in entry.postings)
    return own, ()


def _financial_change_rows(before, after, first: date, last: date) -> list[dict]:
    """Show exact booked edits in the selected month, with both source locations."""
    def rows(fin):
        return [(entry, repr(_financial_facts((entry,))[0])) for entry in fin.entries
                if first <= entry.date <= last]

    old, new = rows(before), rows(after)
    matcher = SequenceMatcher(None, [fact for _, fact in old],
                              [fact for _, fact in new], autojunk=False)

    def describe(fin, selected):
        result = []
        for entry, fact in selected:
            meta = entry.meta or {}
            filename = meta.get("filename")
            if filename:
                path = Path(filename)
                try:
                    filename = path.relative_to(fin.root.parent).as_posix()
                except ValueError:
                    filename = str(path)
            item = {"date": entry.date.isoformat(),
                    "source": f"{filename or '?'}:{meta.get('lineno', '?')}",
                    "booked_fact": fact}
            if isinstance(entry, data.Transaction):
                item["payee"] = entry.payee
                item["narration"] = entry.narration
                item["postings"] = [
                    {"account": posting.account, "units": str(posting.units),
                     "cost": repr(posting.cost) if posting.cost is not None else None,
                     "price": str(posting.price) if posting.price is not None else None}
                    for posting in entry.postings]
            result.append(item)
        return result

    return [{"change": kind, "before": describe(before, old[i:j]),
             "after": describe(after, new[k:l])}
            for kind, i, j, k, l in matcher.get_opcodes() if kind != "equal"]


def check_month(before_root: Path, after_root: Path, month: str,
                allow_financial_corrections: bool = False,
                cutover: date | None = None,
                source_issues_as_review: bool = False) -> dict:
    first, last = _month(month)
    require(cutover is None or isinstance(cutover, date) and cutover.day == 1,
            "CUTOVER_INVALID", "cutover 必须是自然月第一天")
    require(before_root.resolve() != after_root.resolve(),
            "MIGRATION_SAME_INPUT", "前后账本必须是两个独立副本")
    before = load_financial(before_root)
    after = load_financial(after_root)
    require(_source_layout(before) == _source_layout(after),
            "MIGRATION_SOURCE_LAYOUT", "前后账本的 include 文件集合不同")
    require(_options(before) == _options(after), "MIGRATION_OPTIONS_CHANGE",
            "迁移前后 Beancount 选项不同；按月修改时须保留选项设置")
    require(_outside_month_source(before, first, last) ==
            _outside_month_source(after, first, last),
            "MIGRATION_OUTSIDE_MONTH_SOURCE_CHANGE",
            "指定月份交易块之外的账本原文有变化；每次只改目标月交易")
    original = _financial_facts(before.entries)
    changed = _financial_facts(after.entries)
    changes = []
    if original != changed:
        require(allow_financial_corrections, "MIGRATION_FINANCIAL_CHANGE",
                "迁移前后 booked 事实不一致；若要纠正目标月账务，请显式启用 --allow-financial-corrections")
        require(_financial_facts(tuple(entry for entry in before.entries
                                       if not first <= entry.date <= last)) ==
                _financial_facts(tuple(entry for entry in after.entries
                                       if not first <= entry.date <= last)),
                "MIGRATION_OUTSIDE_MONTH_FINANCIAL_CHANGE",
                "目标月份之外的 booked 事实发生变化")
        changes = _financial_change_rows(before, after, first, last)
        require(bool(changes), "MIGRATION_FINANCIAL_CHANGE",
                "booked 事实不同，但无法定位目标月更正")
    # Target-month corrections can add/remove booked entries. Compare only the
    # unaffected subsequences; zipping the complete ledgers would misalign all
    # later entries and falsely report their unchanged metadata as edited.
    outside_before = [entry for entry in before.entries if not first <= entry.date <= last]
    outside_after = [entry for entry in after.entries if not first <= entry.date <= last]
    require(len(outside_before) == len(outside_after), "MIGRATION_OUTSIDE_MONTH_CHANGE",
            "指定月份之外的指令数量发生变化")
    for ordinal, (old, new) in enumerate(zip(outside_before, outside_after), 1):
        require(_entry_metadata(old) == _entry_metadata(new),
                "MIGRATION_OUTSIDE_MONTH_CHANGE",
                "指定月份之外的指令或分录 metadata 有变化", f"月外第 {ordinal} 条：{old.date}")
    if cutover is not None and first < cutover:
        count = sum(isinstance(entry, data.Transaction) and first <= entry.date <= last
                    for entry in after.entries)
        require(count > 0, "MIGRATION_MONTH_EMPTY",
                "指定月份没有已导入交易；先核对 importer 覆盖范围", month)
        source_audit = {"status": "NOT_APPLICABLE_BEFORE_CUTOVER",
                        "transactions": count, "tag_syntax": "NOT_AUDITED"}
        source_files = len(after.sources)
    else:
        issues = (collect_v6_source_issues(after_root, first, last)
                  if source_issues_as_review else [])
        if issues:
            count = sum(isinstance(entry, data.Transaction) and first <= entry.date <= last
                        for entry in after.entries)
            source_audit = {"status": "NEEDS_REVIEW", "transactions": count,
                            "tag_syntax": "NEEDS_REVIEW", "issues": issues}
            source_files = len(after.sources)
        else:
            audit = scan_v6_source(after_root, first, last)
            source_audit = asdict(audit)
            source_files = audit.source_files
        require(source_audit["transactions"] > 0, "MIGRATION_MONTH_EMPTY",
                "指定月份没有已导入交易；先核对 importer 覆盖范围", month)
    return {"month": month, "before": str(before.root), "after": str(after.root),
            "source_files": source_files, "booked_entries": len(before.entries),
            "financial_facts_equal": not changes,
            "financial_corrections_allowed": allow_financial_corrections,
            "financial_changes": changes, "outside_month_source_equal": True,
            "outside_month_metadata_equal": True,
            "month_source_audit": source_audit,
            "budget_semantics": "NOT_AUDITED",
            "note": ("目标月 booked 更正已逐项列出，尚须人工核对差异；"
                     if changes else "全账 booked 事实不变；") +
                    ("目标月交易块外原文不变；本月早于 cutover，不审 v6 标签或月报"
                     if cutover is not None and first < cutover else
                     "目标月交易块外原文不变，本月 v6 标签语法合法；预算分类、用途、划拨和月报仍须独立审阅")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=Path, help="上一个已保存的完整账本副本入口")
    parser.add_argument("--after", required=True, type=Path, help="本月修改后的完整账本副本入口")
    parser.add_argument("--month", required=True, help="本次只修改的 YYYY-MM 月份")
    parser.add_argument("--allow-financial-corrections", action="store_true",
                        help="允许更正目标月 booked 财务事实，并输出逐项前后对照")
    args = parser.parse_args(argv)
    try:
        result = check_month(args.before, args.after, args.month,
                             args.allow_financial_corrections)
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message,
                                    "source": exc.source}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

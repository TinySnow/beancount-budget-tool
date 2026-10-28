"""List stale source references and duplicate inline claims without changes.

Use after comparing old and new loader bookings, or after editing the ledger.
Matching booked facts are suggestions for human review, never replacements.
Formal sidecar audits and the monthly report must still pass independently.
"""

from __future__ import annotations

from ..runtime_inputs import input_text

from collections import defaultdict
import json
from pathlib import Path

from beancount.core import data
import yaml

from beancount_budget_tool.errors import require
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.yamlstrict import StrictLoader, load_yaml


REVIEW_FIELDS = (
    "accounts", "opening", "ownership", "obligations", "completeness",
    "classifications", "benefit_groups", "expense_plans", "purpose_plans",
    "h_transfers", "k_events", "h_fx", "purpose_pools", "debt_coverage", "debt_settlements",
)
RAW_TRANSACTION = frozenset(("path", "transaction_sha256", "occurrence"))
RAW_POSTING = RAW_TRANSACTION | frozenset((
    "posting_ordinal", "booked_ordinal", "lot", "fact_sha256"))
GENERATED_TRANSACTION = frozenset((
    "path", "directive_sha256", "occurrence", "generated_kind",
    "generated_ordinal", "loader_version"))
GENERATED_POSTING = GENERATED_TRANSACTION | frozenset((
    "posting_ordinal", "lot", "fact_sha256"))
REFERENCE_SHAPES = {RAW_TRANSACTION, RAW_POSTING,
                    GENERATED_TRANSACTION, GENERATED_POSTING}


def _key(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _references(value: object, path: str = "$"):
    if isinstance(value, dict):
        if frozenset(value) in REFERENCE_SHAPES:
            yield path, value
        else:
            for name, child in value.items():
                yield from _references(child, f"{path}.{name}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _references(child, f"{path}[{index}]")


def _reference_lines(path: Path) -> dict[str, int]:
    """Map a nested review reference to its YAML line, including list rows."""
    node = yaml.compose(input_text(path), Loader=StrictLoader)
    lines = {}

    def visit(part, location: str):
        if isinstance(part, yaml.MappingNode):
            if frozenset(key.value for key, _ in part.value) in REFERENCE_SHAPES:
                lines[location] = part.start_mark.line + 1
            else:
                for key, child in part.value:
                    visit(child, f"{location}.{key.value}")
        elif isinstance(part, yaml.SequenceNode):
            for index, child in enumerate(part.value):
                visit(child, f"{location}[{index}]")

    if node is not None:
        visit(node, "$")
    return lines


def diagnose(config: Path) -> dict:
    config = config.resolve()
    document = load_yaml(config)
    require(type(document) is dict and type(document.get("ledger")) is str,
            "REVIEW_CONFIG_INVALID", "配置须明确 ledger 路径", str(config))
    root = (config.parent / document["ledger"]).resolve()
    fin = load_financial(root)
    facts = FactIndex(fin)

    known = set()
    candidates = defaultdict(list)
    postings = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        txn_ref = facts.for_transaction(entry).as_dict()
        known.add(_key(txn_ref))
        for posting in entry.postings:
            ref = facts.for_posting(posting).as_dict()
            key = _key(ref)
            known.add(key)
            postings[key] = posting
            meta = posting.meta or {}
            location = f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"
            if "fact_sha256" in ref:
                candidates[(ref["path"], ref["fact_sha256"], ref["lot"])].append(
                    {"ref": ref, "source": location})

    reports = []
    for field in REVIEW_FIELDS:
        filename = document.get(field)
        if filename is None:
            continue
        require(type(filename) is str and bool(filename),
                "REVIEW_CONFIG_INVALID", "审阅文件路径须为非空字符串", field)
        path = (config.parent / filename).resolve()
        sidecar = load_yaml(path)
        require(type(sidecar) is dict, "REVIEW_DECLARATION_INVALID",
                "审阅文件根节点必须是映射", str(path))
        ref_lines = _reference_lines(path)
        stale = []
        inline_conflicts = []
        total = 0
        for doc_path, ref in _references(sidecar):
            total += 1
            key = _key(ref)
            hints = []
            if key not in known:
                require(doc_path in ref_lines, "REVIEW_SOURCE_LOCATION_INVALID",
                        "无法定位过期引用在审阅文件中的行号", f"{path} {doc_path}")
                hints = candidates.get((ref.get("path"), ref.get("fact_sha256"),
                                        ref.get("lot")), [])
                stale.append({"document_path": doc_path,
                              "review_source": f"{path}:{ref_lines[doc_path]}",
                              "ref": ref,
                              "candidate_count": len(hints),
                              "candidates_not_approved": hints[:3]})
            metadata = None
            if field == "ownership" and doc_path.startswith("$.claims[") and doc_path.endswith("].claim"):
                metadata = "fund"
            elif field == "classifications" and doc_path.startswith("$.classifications[") and doc_path.endswith("].claim"):
                metadata = "budget"
            elif field == "k_events" and doc_path.startswith("$.events[") and doc_path.endswith("].k_ref"):
                metadata = "fund"
            if metadata is None:
                continue
            current_key = key if key in known else _key(hints[0]["ref"]) if len(hints) == 1 else None
            posting = postings.get(current_key)
            if posting is None or metadata not in (posting.meta or {}):
                continue
            require(doc_path in ref_lines, "REVIEW_SOURCE_LOCATION_INVALID",
                    "无法定位重复认领在审阅文件中的行号", f"{path} {doc_path}")
            meta = posting.meta or {}
            inline_conflicts.append({"document_path": doc_path,
                                     "review_source": f"{path}:{ref_lines[doc_path]}",
                                     "ledger_source": f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}",
                                     "metadata": metadata, "inline_value": meta[metadata],
                                     "matched_by_candidate": key not in known})
        loader = sidecar.get("loader_version")
        reports.append({"field": field, "file": str(path),
                        "declared_loader": loader,
                        "loader_matches_current": loader is None or loader == fin.loader_version,
                        "declared_snapshot_matches_current": (
                            sidecar.get("ledger_snapshot") == fin.digest
                            if "ledger_snapshot" in sidecar else None),
                        "references": total, "stale_references": stale,
                        "inline_conflicts": inline_conflicts})

    count = sum(len(row["stale_references"]) for row in reports)
    conflicts = sum(len(row["inline_conflicts"]) for row in reports)
    loader_mismatches = sum(not row["loader_matches_current"] for row in reports)
    return {"mode": "v6_review_source_refs", "ledger": str(root),
            "loader_version": fin.loader_version, "ledger_snapshot": fin.digest,
            "status": "NEEDS_REVIEW" if count or loader_mismatches or conflicts else "REFERENCES_CURRENT",
            "stale_reference_count": count, "loader_mismatch_count": loader_mismatches,
            "inline_conflict_count": conflicts,
            "sidecars": reports,
            "note": "仅盘点引用与重复认领；候选不可自动替换。账本版本迁移仍须独立 booked 比对和正式审计。"}

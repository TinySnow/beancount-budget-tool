"""Source-bound review metadata and stable booked-state fingerprints."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path
import json
import re

from beancount.core import data

from ..exact import exact, exact_text
from ..financial import FinancialInput
from ..refs import _lot
from ..yamlstrict import require_yaml
from .facts import FactIndex


SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def valid_digest(value: object) -> bool:
    return type(value) is str and bool(SHA256.fullmatch(value))


def validate_event_declaration(document: object, path: Path, cutover: date,
                               loader_version: str, code: str) -> None:
    """Check the common evidence header for transfer, FX and K review files."""
    required = {"schema", "cutover", "ledger_snapshot", "loader_version", "events"}
    unknown = set(document) - required if type(document) is dict else set()
    require_yaml(type(document) is dict and set(document) == required, code,
                 "事件侧文件字段不完整或含未知字段", path,
                 (sorted(unknown, key=str)[0],) if unknown else ())
    for field, valid in (
        ("schema", type(document["schema"]) is int and document["schema"] == 6),
        ("cutover", type(document["cutover"]) is str and document["cutover"] == cutover.isoformat()),
        ("ledger_snapshot", valid_digest(document["ledger_snapshot"])),
        ("loader_version", document["loader_version"] == loader_version),
        ("events", type(document["events"]) is list),
    ):
        require_yaml(valid, code, "事件版本、cutover、原审快照或事件列表格式不匹配", path, (field,))


def asset_opening_fingerprint(fin: FinancialInput, cutover: date) -> str:
    """Pin every pre-cutover asset source, leaving later imports independent."""
    facts = FactIndex(fin)
    assets = fin.options["name_assets"]
    rows = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date >= cutover:
            continue
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] != assets:
                continue
            rows.append([facts.for_posting(posting).as_dict(), posting.account,
                         exact_text(exact(posting.units.number)), posting.units.currency,
                         _lot(posting)])
    return sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def as_of_booked_fingerprint(fin: FinancialInput, as_of: date) -> str:
    """Pin every booked transaction through a report date, excluding later imports.

    This is an evidence identity, not a claim that an importer covered every
    real-world transaction. Existing reviewed sidecars still validate their
    own source references and the actual snapshot still rechecks balances.
    """
    facts = FactIndex(fin)
    rows = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date > as_of:
            continue
        rows.append([entry.date.isoformat(), facts.for_transaction(entry).as_dict(),
                     [facts.for_posting(posting).as_dict() for posting in entry.postings]])
    payload = [fin.loader_version, as_of.isoformat(), rows]
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()

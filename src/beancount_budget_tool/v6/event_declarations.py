"""Shared metadata checks for source-bound transfer, FX and K event files."""

from datetime import date
from pathlib import Path

from ..yamlstrict import require_yaml
from .provenance import valid_digest


def validate_event_declaration(document: object, path: Path, cutover: date,
                               loader_version: str, code: str) -> None:
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

"""Reviewed cutover debt and advance snapshots, bound to historical sources."""

from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import re

from ..errors import require
from ..exact import exact, exact_text
from ..refs import _lot
from ..yamlstrict import yaml_path_location
from .classification import _key


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
AMOUNT = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")


def opening_source(entry, posting, fin, facts) -> dict:
    """Recognize original consumer/advance or explicit accounting carry-in legs.

    This describes an origin, not the amount still outstanding at cutover.
    That residual is a reviewed snapshot reconciled with all historic postings.
    """
    amount = exact(posting.units.number)
    counterpart = next((p for p in entry.postings if p is not posting), None) if len(entry.postings) == 2 else None
    kind = "unsupported"
    if counterpart is not None and amount and (
            posting.cost is None and posting.price is None and
            counterpart.cost is None and counterpart.price is None and
            counterpart.units.currency == posting.units.currency and
            exact(counterpart.units.number) == -amount and
            fin.raw_price(posting)[0] == fin.raw_price(counterpart)[0] == "none"):
        other_root = counterpart.account.split(":", 1)[0]
        if amount < 0 and other_root in (fin.options["name_expenses"], fin.options["name_equity"]):
            kind = "consumer_debt"
        elif amount > 0 and other_root in (fin.options["name_assets"], fin.options["name_equity"]):
            kind = "advance_receivable"
    meta = posting.meta or entry.meta
    return {"liability_ref": facts.for_posting(posting).as_dict(),
            "account": posting.account, "commodity": posting.units.currency,
            "lot": _lot(posting), "signed_units": exact_text(amount),
            "date": entry.date.isoformat(), "kind": kind,
            "source": f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"}


def compile_obligation_opening(booked: dict, sources: dict, rows: object,
                               owners: set[str], declarations: Path | None) -> dict:
    """Do not recreate old flows: seed residual IDs once, then replay new events."""
    location = lambda *keys: yaml_path_location(declarations, ("opening", *keys)) if declarations else None
    require(type(rows) is list, "OBLIGATION_OPENING_INVALID", "opening 须为期初责任列表", location())
    debts, advances, reviewed = {}, {}, defaultdict(Fraction)
    used_sources = set()
    declared = []
    coordinate_locations = {}
    for index, row in enumerate(rows):
        where = location(index)
        common = {"liability_ref", "kind", "remaining_native", "reason"}
        require(type(row) is dict and set(row) in (
            common | {"obligation_id", "owner_id"}, common | {"clearing_id"}),
            "OBLIGATION_OPENING_INVALID", "期初项须含来源、类型、稳定 ID、未结金额和理由；债务另需 owner_id", where)
        ref = row["liability_ref"]
        require(type(ref) is dict and all(type(value) in (str, int) for value in ref.values()),
                "OBLIGATION_OPENING_REF_INVALID", "期初来源须为完整 booked 引用", location(index, "liability_ref"))
        key = _key(ref)
        require(key in sources, "OBLIGATION_OPENING_REF_INVALID", "期初来源不存在、已过期或并非 cutover 前负债分录",
                location(index, "liability_ref"))
        origin = sources[key]
        require(set(ref) == set(origin["liability_ref"]) and
                all(type(value) is type(origin["liability_ref"][field]) for field, value in ref.items()),
                "OBLIGATION_OPENING_REF_INVALID", "期初来源字段类型不符", location(index, "liability_ref"))
        require(origin["kind"] != "unsupported", "OBLIGATION_OPENING_SOURCE_UNSUPPORTED",
                "期初来源须是简单同币消费、代垫或 Equity 结转；借款及复合责任仍需专门协议", where)
        kind = row["kind"]
        require(type(kind) is str and kind == origin["kind"], "OBLIGATION_OPENING_KIND_INVALID",
                "期初类型与来源方向或交易形状不符", location(index, "kind"))
        debt = kind == "consumer_debt"
        require(set(row) == common | ({"obligation_id", "owner_id"} if debt else {"clearing_id"}),
                "OBLIGATION_OPENING_INVALID", "债务须用 obligation_id，代垫须用 clearing_id", where)
        ident = row["obligation_id"] if debt else row["clearing_id"]
        require(type(ident) is str and ID.fullmatch(ident) is not None, "OBLIGATION_OPENING_ID_INVALID",
                "期初稳定 ID 无效", where)
        registry = debts if debt else advances
        require(ident not in registry and key not in used_sources, "DUPLICATE_OBLIGATION_OPENING",
                "期初责任 ID 或来源被重复认领", where)
        if debt:
            require(type(row["owner_id"]) is str and row["owner_id"] in owners,
                    "OBLIGATION_OWNER_INVALID", "期初消费债务须指定已登记用途", location(index, "owner_id"))
        raw = row["remaining_native"]
        require(type(raw) is str and AMOUNT.fullmatch(raw) is not None and Fraction(raw) > 0,
                "OBLIGATION_OPENING_AMOUNT_INVALID", "期初未结金额须为正的精确十进制字符串",
                location(index, "remaining_native"))
        amount = Fraction(raw)
        require(amount <= abs(Fraction(origin["signed_units"])), "OBLIGATION_OPENING_EXCESS",
                f"期初未结 {exact_text(amount)} 超过所绑定原分录金额 {origin['signed_units']}",
                location(index, "remaining_native"))
        require(type(row["reason"]) is str and bool(row["reason"].strip()), "OBLIGATION_OPENING_INVALID",
                "期初未结金额及归属须留下审阅理由", location(index, "reason"))
        coordinate = (origin["account"], origin["commodity"], origin["lot"])
        reviewed[coordinate] += -amount if debt else amount
        coordinate_locations[coordinate] = where
        used_sources.add(key)
        registry[ident] = {"account": coordinate, "remaining": amount, "origin": "opening",
                           **({"owner_id": row["owner_id"], "purchase_ref": ref} if debt else {"outflow_ref": ref})}
        declared.append({**row, "account": coordinate[0], "commodity": coordinate[1],
                         "lot": coordinate[2], "source": origin["source"], "review_source": where})
    for coordinate, value in reviewed.items():
        expected = booked.get(coordinate, Fraction(0))
        require(value == expected, "OBLIGATION_OPENING_MISMATCH",
                f"{coordinate[0]} {coordinate[1]}：期初应收减债务 {exact_text(value)} "
                f"不等于 cutover 前账面余额 {exact_text(expected)}；核对该坐标全部未结 ID 与金额",
                coordinate_locations[coordinate])
    origins_by_coordinate = defaultdict(list)
    for row in sources.values():
        origins_by_coordinate[(row["account"], row["commodity"], row["lot"])].append(row)
    pending = []
    for (account, commodity, lot), value in sorted(booked.items()):
        if not value or (account, commodity, lot) in reviewed:
            continue
        origins = origins_by_coordinate[(account, commodity, lot)]
        candidates = [row for row in origins if row["kind"] != "unsupported"]
        pending.append({"account": account, "commodity": commodity, "lot": lot,
                        "signed_units": exact_text(value), "origin_source": origins[-1]["source"],
                        "source_candidates": candidates,
                        "reason_code": "LIABILITY_OPENING_PENDING" if candidates else "OBLIGATION_OPENING_SOURCE_UNSUPPORTED"})
    return {"debts": debts, "advances": advances, "pending": pending, "declarations": declared}

"""Read-only activity views for K book flows and tracking topics.

K activity is book flow, not fund shares, market value or cash.  Tracking
activity is a presentation of the already-audited Expenses topic events.
"""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
import re

from ..errors import require
from ..exact import amount_text, exact_text
from ..text_table import table


MONTH = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])\Z")
DIRECTIONS = {"subscription": "申购", "redemption": "赎回", "zero_record": "零金额"}
DISPLAY_STATUS = {"CERTIFIED": "已审", "CERTIFIED_BOOK_FLOW": "已审",
                  "NEEDS_REVIEW": "待审", "UNSUPPORTED": "待审"}


def _direction(event: dict) -> str:
    if event.get("shape") == "zero_record":
        return "zero_record"
    return "subscription" if Fraction(event["signed_k_units"]) > 0 else "redemption"


def k_flows(k_book: dict, month: str | None = None) -> list[dict]:
    """Project reviewed K event facts, optionally for one YYYY-MM."""
    if month is not None:
        require(type(month) is str and bool(MONTH.fullmatch(month)),
                "K_FLOW_MONTH_INVALID", "K 流水月份须为 YYYY-MM", str(month))
    rows = []
    for event in k_book.get("events", []):
        if month is not None and event["date"][:7] != month:
            continue
        rows.append({**event, "direction": _direction(event),
                     "source": event.get("source") or ""})
    # Stable by date only: same-day events keep their source-ledger order,
    # rather than being reordered by account or line-number strings.
    return sorted(rows, key=lambda row: row["date"])


def _purpose_text(event: dict, owner_names: dict) -> str:
    parts = event.get("slices") or []
    if not parts:
        return "待审"
    return "，".join(
        f"{owner_names.get(part['k_owner_id'], '预算外' if part['k_owner_id'] == '-' else part['k_owner_id'])} "
        f"{amount_text(part['signed_k_units'])}" for part in parts)


def _cash_text(event: dict) -> str:
    account = event.get("cash_account")
    if not account:
        return ""
    units = event.get("signed_cash_units")
    if units is None:
        units = exact_text(-Fraction(event["signed_k_units"]))
    return f"{account} {amount_text(units)} {event['commodity']}"


def render_k_summary(k_book: dict, owner_names: dict, month: str | None = None,
                     columns: int = 76, show_all: bool = False) -> list[str]:
    """Compact K account summary; never calls book flow shares or cash."""
    if month is not None and not show_all:
        flows = k_flows(k_book, month)
        groups = defaultdict(lambda: {"net": Fraction(0), "count": 0,
                                      "pending": False, "owners": set()})
        for event in flows:
            key = (event["k_account"], event["commodity"])
            group = groups[key]
            group["net"] += Fraction(event["signed_k_units"])
            group["count"] += 1
            if event["status"] != "CERTIFIED":
                group["pending"] = True
            for part in event.get("slices") or []:
                group["owners"].add(part["k_owner_id"])
        rows = []
        for (account, commodity), group in sorted(groups.items()):
            # Any event in the month is visible, including subscriptions
            # offset by redemptions and zero-record events.
            owners = "，".join(owner_names.get(owner, "预算外" if owner == "-" else owner)
                              for owner in sorted(group["owners"])) or "待审"
            rows.append([account, f"{amount_text(exact_text(group['net']))} {commodity}",
                         str(group["count"]), owners,
                         DISPLAY_STATUS["NEEDS_REVIEW" if group["pending"] else "CERTIFIED_BOOK_FLOW"]])
        if not rows:
            return []
        return ["K 本月账面申赎（账面变动，不是份额或现金）",
                *table(["K账户", "本月账面变动", "笔数", "用途", "状态"], rows,
                       numeric=(1, 2), columns=columns)]

    rows = []
    for row in k_book.get("rows", []):
        changed = Fraction(row["booked_change"]) != 0 or Fraction(row["unreviewed_change"]) != 0
        if not show_all and not changed and not row["pending_event_count"]:
            continue
        owners = "，".join(
            f"{owner_names.get(part['owner_id'], '预算外' if part['owner_id'] == '-' else part['owner_id'])} "
            f"{amount_text(part['signed_change'])}"
            for part in row.get("purpose_changes") or []) or "—"
        rows.append([row["account"], f"{amount_text(row['booked_opening'])} {row['commodity']}",
                     f"{amount_text(row['booked_change'])} {row['commodity']}",
                     f"{amount_text(row['booked_end'])} {row['commodity']}", owners,
                     DISPLAY_STATUS.get(row["status"], "待审")])
    if not rows:
        return []
    return ["K 账面申赎汇总（账面数，不是份额、市值或现金）",
            *table(["K账户", "期初账面", "本期账面变动", "期末账面", "已审用途变动", "状态"],
                   rows, numeric=(1, 2, 3), columns=columns)]


def render_k_flows(events: list[dict], owner_names: dict, columns: int = 76) -> list[str]:
    if not events:
        return []
    rows = []
    for event in events:
        rows.append([event["date"], DIRECTIONS[event.get("direction", _direction(event))],
                     event["k_account"],
                     f"{amount_text(event['signed_k_units'])} {event['commodity']}",
                     _cash_text(event), _purpose_text(event, owner_names),
                     DISPLAY_STATUS.get(event['status'], "待审"), event.get('source', '')])
    return ["K 申赎流水（K 账面变动，不代表份额、市值或可赎回现金）",
            *table(["日期", "方向", "K账户", "账面变动", "现金对手", "用途", "状态", "来源"],
                   rows, numeric=(3,), columns=columns)]


def render_tracking_flows(events: list[dict], columns: int = 76) -> list[str]:
    if not events:
        return []
    rows = [[event["date"], event["topic"], event["account"],
             f"{amount_text(event['signed_units'])} {event['commodity']}",
             event.get("source", "")] for event in events]
    return ["专题流水（原币；同一支出可属于多个专题，不改变预算）",
            *table(["日期", "专题", "账户", "原币金额", "来源"], rows,
                   numeric=(3,), columns=columns)]

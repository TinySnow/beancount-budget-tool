"""Join reviewed monthly purpose targets Q with certified allocations G."""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path

from ..errors import require
from ..exact import exact_text
from ..yamlstrict import load_yaml
from .ownership import _owners
from .purpose_flows import audit_monthly_purpose_flows
from .purpose_targets import load_purpose_targets


def compare_purpose_progress(targets: dict, flows: dict) -> dict:
    """Report Q−G per owner only when both sides are independently usable."""
    require(targets["month"] == flows["month"] and
            targets["report_currency"] == flows["report_currency"],
            "PURPOSE_PROGRESS_MISMATCH", "用途目标与实际净划拨的月份或货币不一致")
    actual = flows["net_allocations"]
    require((flows["status"] == "CERTIFIED") == (actual is not None),
            "PURPOSE_PROGRESS_MISMATCH", "实际净划拨状态与金额完整性不一致")
    if actual is not None:
        require(set(actual) == {row["owner_id"] for row in targets["owners"]},
                "PURPOSE_PROGRESS_MISMATCH", "目标用途名册与实际净划拨用途名册不一致")
    rows = []
    for target in targets["owners"]:
        owner = target["owner_id"]
        g = actual[owner] if actual is not None else None
        if target["status"] == "MISSING":
            status = "TARGET_MISSING"
        elif target["status"] == "NOT_ASSESSED":
            status = "NOT_ASSESSED"
        elif g is None:
            status = "ACTUAL_PENDING"
        else:
            status = "CERTIFIED"
        rows.append({"owner_id": owner, "target_status": target["status"],
                     "target_q": target["target"], "actual_g": g,
                     "remaining_q_minus_g": exact_text(Fraction(target["target"]) - Fraction(g))
                     if status == "CERTIFIED" else None,
                     "status": status})
    evidence = ({"_transaction_evidence": flows["_transaction_evidence"]} if "_transaction_evidence" in flows else {})
    return {**evidence, "mode": "v6_monthly_purpose_progress", "month": targets["month"],
            "report_currency": targets["report_currency"],
            "ledger_snapshot": flows["ledger_snapshot"],
            "plan_sha256": targets["plan_sha256"],
            "status": "CERTIFIED" if flows["status"] == "CERTIFIED" and all(row["status"] in
                       ("CERTIFIED", "NOT_ASSESSED") for row in rows) else "NEEDS_REVIEW",
            "actual_status": flows["status"],
            "pending_actual_events": flows["pending_events"],
            "missing_target_ids": targets["missing_target_ids"],
            "owners": rows,
            "note": "Q−G 是月度用途划拨目标差，不代表可花现金、投资市值或计划收入到账"}


def audit_monthly_purpose_progress(root: Path, cutover: date, month: str,
                                   accounts: Path, opening: Path, ownership: Path,
                                   plans: Path, report_currency: str,
                                   h_transfers: Path | None = None,
                                   k_events: Path | None = None,
                                   h_fx: Path | None = None,
                                   purpose_pools: Path | None = None,
                                   obligations: Path | None = None, *, include_evidence: bool = False) -> dict:
    flows = audit_monthly_purpose_flows(root, cutover, month, accounts, opening,
                                        ownership, h_transfers, k_events, h_fx,
                                        purpose_pools, obligations, include_evidence=include_evidence)
    require(report_currency == flows["report_currency"],
            "REPORT_CURRENCY_INVALID", "用途进度货币须与账户声明一致")
    owners = set(_owners(load_yaml(ownership)["owners"]).values())
    targets = load_purpose_targets(plans, month, report_currency, owners)
    return compare_purpose_progress(targets, flows)

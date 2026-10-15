"""Turn monthly metric blockers into concrete review items, without values."""

from pathlib import Path

from ..yamlstrict import yaml_key_location, yaml_path_location
from .review_diagnostics import merge_metric_items, review_item


SOURCES = {"实际可花": ("completeness", "as_of"),
           "消费目标": ("expense_plans", "months"),
           "用途划拨": ("purpose_plans", "months"),
           "最多能花": ("expense_plans", "spendable_cap_category_id"),
           "资产账户用途": ("ownership", "claims"),
           "K 账面申赎用途": ("k_events", "events"),
           "默认支出资金收付": ("ownership", "claims"),
           "预算用途余额": ("purpose_pools", "pools")}


def enrich_metric_reviews(config: Path, document: dict, report: dict,
                           pending: list[dict]) -> list[dict]:
    """Attach file lines and merge a shared cause across affected metrics."""
    category_names = {row["id"]: row["name"]
                      for row in report["expense_targets"].get("categories", [])}
    owner_names = report.get("owner_names", {})
    for metric in pending:
        key, field = SOURCES[metric["metric"]]
        sidecar = (config.parent / document[key]).resolve() if key in document else None
        fallback = (yaml_key_location(sidecar, field) if sidecar and sidecar.is_file()
                    else yaml_key_location(config, key))
        if field == "months" and sidecar and sidecar.is_file():
            fallback = yaml_path_location(sidecar, (field, document["month"]))
        metric["source"] = fallback
        items = [{**item, "source": item.get("source") or fallback}
                 for item in metric.get("review_items", [])]
        for ident in metric.get("missing_target_ids", []):
            if any(item.get("target_id") == ident for item in items):
                continue
            purpose = metric["metric"] == "用途划拨"
            name = (owner_names if purpose else category_names).get(ident, ident)
            items.append(review_item(
                "PURPOSE_TARGET_MISSING" if purpose else "PLAN_MISSING", fallback,
                target_id=ident, detail=f"本月{'用途' if purpose else '消费类别'}「{name}」缺少目标；"
                + ("填写划拨金额或明确写 not_assessed。" if purpose else
                   "填写该类别目标或覆盖它的父类别子树目标。")))
        for event in metric.get("pending_events", []):
            items.append(review_item(event["reason"], event.get("source") or fallback,
                                     event_kind=event["kind"],
                                     **{key: event[key] for key in ("ref", "account", "commodity")
                                        if key in event}))
        for position in metric.get("pending_positions", []):
            for reason in position["reasons"]:
                for source in reason.get("sources", []) or [fallback]:
                    items.append(review_item(
                        reason["code"], source, account=position["account"],
                        commodity=position["commodity"], lot=position["lot"],
                        blocking_reason_code="H_HOLDINGS_PENDING"))
        for account in metric.get("pending_accounts", []):
            if not any(item.get("account") == account for item in items):
                items.append(review_item("K_BOOK_PURPOSE_PENDING", fallback, account=account))
        if metric["metric"] == "最多能花":
            # Reuse debt/valuation source causes rather than inventing a
            # second issue at the expense-plan file for the same obstacle.
            for parent in pending:
                if parent["metric"] not in ("实际可花", "消费目标"):
                    continue
                for item in parent.get("review_items", []):
                    if item.get("blocking_reason_code", item["reason_code"]) in metric["reason_codes"]:
                        items.append(item.copy())
        covered = {item.get("blocking_reason_code", item["reason_code"]) for item in items}
        for code in metric["reason_codes"]:
            if code not in covered:
                items.append(review_item(code, fallback))
        if not items:
            items.append(review_item("METRIC_PENDING", fallback))
        metric["review_items"] = items
    return merge_metric_items(pending)

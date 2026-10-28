"""Actionable source review queue after an importer run.

This is not a budget report. Every item is a currently unreviewed booked fact;
an empty queue does not certify valuation, plans or current spendable cash.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sys

from beancount.core import data

from ..runtime_inputs import input_bytes, input_is_file
from ..errors import BudgetError, require
from ..exact import exact, exact_text
from ..financial import frozen_financial, load_financial
from ..yamlstrict import load_yaml, yaml_key_location
from .accounts import _lot, audit_accounts
from .classification import _key, _registry, audit_classification
from .facts import FactIndex, frozen_fact_indices
from .h_fx import audit_h_fx
from .h_transfers import audit_h_transfers
from .inline_amounts import parse_inline_amounts
from .k_events import audit_k_events
from .obligations import audit_simple_obligations
from .ownership import _owners, audit_ownership
from .purpose_pools import audit_purpose_pools
from .review_diagnostics import annotate_source_items


NEEDED = {"schema", "ledger", "cutover", "accounts", "ownership",
          "obligations", "classifications", "purpose_pools"}
LABELS = {"account_semantics": "新资产账户语义", "payment_scope": "账户支付范围",
          "classification": "费用／收入分类", "asset_ownership": "资产用途",
          "pool_boundary": "证券池对外边界", "booked_obligation": "已记负债事件",
          "liability_opening": "期初负债", "fund_amount": "fund 金额",
          "budget_amount": "budget 金额",
          "inventory": "真实库存顺序与余额",
          "h_transfer": "资产用途转账", "k_event": "基金申赎用途事件",
          "h_fx": "购汇用途事件"}


def audit_review_queue(config: Path, since: date, through: date) -> dict:
    config = config.resolve()
    doc = load_yaml(config)
    require(type(doc) is dict and NEEDED <= set(doc),
            "REVIEW_QUEUE_CONFIG_INVALID", "待审清单配置缺少必要字段",
            f"{config}:1")
    require(type(doc["schema"]) is int and doc["schema"] == 6,
            "REVIEW_QUEUE_CONFIG_INVALID", "待审清单配置版本无效",
            yaml_key_location(config, "schema"))
    try:
        cutover = date.fromisoformat(doc["cutover"])
    except (TypeError, ValueError) as exc:
        raise BudgetError("TIME_INVALID", "切换点必须是有效日期",
                          yaml_key_location(config, "cutover")) from exc
    require(cutover.day == 1 and cutover <= since <= through,
            "TIME_INVALID", "待审区间须在 v6 切换点之后且起止有序",
            yaml_key_location(config, "cutover"))

    def path(key: str) -> Path:
        value = doc[key]
        require(type(value) is str and bool(value.strip()),
                "REVIEW_QUEUE_CONFIG_INVALID", "配置路径不能为空",
                yaml_key_location(config, key))
        return (config.parent / value).resolve()

    root = path("ledger")
    require(input_is_file(root), "REVIEW_QUEUE_CONFIG_INVALID",
            "待审清单引用了不存在的根账本", yaml_key_location(config, "ledger"))
    sidecars = [config, *(path(key) for key in NEEDED - {"schema", "ledger", "cutover"})]
    for key in ("opening", "h_transfers", "k_events", "h_fx"):
        if key in doc:
            sidecars.append(path(key))
    optional_sidecars = {key for key in ("opening", "h_transfers", "k_events", "h_fx") if key in doc}
    for key in NEEDED - {"schema", "ledger", "cutover"} | optional_sidecars:
        require(input_is_file(path(key)), "REVIEW_QUEUE_CONFIG_INVALID",
                "待审清单引用了不存在的审阅文件", yaml_key_location(config, key))
    before = {item: sha256(input_bytes(item)).hexdigest() for item in sidecars}
    with frozen_financial(root), frozen_fact_indices():
        fin = load_financial(root)
        facts = FactIndex(fin)
        dated = {}
        first_asset_date = {}
        first_asset_source = {}
        liability_opening_sources = {}
        source_by_ref = {}
        posting_by_ref = {}
        transaction_by_ref = {}
        booked_dates = []
        asset_root = fin.options["name_assets"]
        liability_root = fin.options["name_liabilities"]
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction):
                continue
            if entry.date <= through:
                booked_dates.append(entry.date)
            for posting in entry.postings:
                ref = facts.for_posting(posting).as_dict()
                key = _key(ref)
                dated[key] = entry.date
                transaction_by_ref[key] = entry
                posting_by_ref[key] = posting
                meta = posting.meta or entry.meta
                filename = meta.get("filename")
                lineno = meta.get("lineno")
                source = (f"{fin._source(filename).path}:{lineno}"
                          if isinstance(filename, str) and isinstance(lineno, int)
                          else None)
                source_by_ref[key] = source
                if posting.account.split(":", 1)[0] == liability_root and entry.date < cutover:
                    liability_opening_sources[(posting.account, posting.units.currency, _lot(posting))] = source
                if posting.account.split(":", 1)[0] == asset_root:
                    previous = first_asset_date.get(posting.account)
                    if previous is None or entry.date < previous:
                        first_asset_date[posting.account] = entry.date
                        first_asset_source[posting.account] = source

        # A bare name can end in digits. Only the same registries used by the
        # compilers can distinguish that name from a single explicit amount.
        # Collect all invalid vectors before either compiler stops at one.
        ownership_doc = load_yaml(path("ownership"))
        classification_doc = load_yaml(path("classifications"))
        names = {
            "fund": set(_owners(ownership_doc.get("owners") if type(ownership_doc) is dict else None,
                                source=path("ownership"))),
            "budget": set(_registry(classification_doc.get("categories") if type(classification_doc) is dict else None,
                                    source=path("classifications"))),
        }
        tag_errors = []
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction) or not cutover <= entry.date <= through:
                continue
            for posting in entry.postings:
                ref = facts.for_posting(posting).as_dict()
                kind = posting.account.split(":", 1)[0]
                for tag in ("fund", "budget"):
                    if tag not in (posting.meta or {}) or (tag == "fund" and kind != asset_root) or (
                            tag == "budget" and kind not in (fin.options["name_income"], fin.options["name_expenses"], liability_root)):
                        continue  # Misplaced metadata is rejected by the source gate.
                    code = "FUND_GRAMMAR" if tag == "fund" else "TAG_SYNTAX_INVALID"
                    raw = posting.meta[tag]
                    try:
                        parse_inline_amounts(raw, exact(posting.units.number),
                                             source_by_ref[_key(ref)] or str(ref),
                                             known_names=names[tag], code=code)
                    except BudgetError as exc:
                        if exc.code != code:
                            raise
                        tag_errors.append({"kind": tag + "_amount", "date": entry.date.isoformat(),
                                           "account": posting.account,
                                           "commodity": posting.units.currency,
                                           "signed_units": exact_text(exact(posting.units.number)),
                                           "ref": ref, "source": source_by_ref[_key(ref)],
                                           "reason_code": exc.code,
                                           "detail": f'{tag}: {raw!r}；{exc.message}。'
                                                     f'请核对标签金额与分录 {exact_text(exact(posting.units.number))} '
                                                     f'{posting.units.currency}；标签金额使用正数，'
                                                     '不拆分时也可只写已登记的名称。'})
        if tag_errors:
            require(all(input_is_file(item) and sha256(input_bytes(item)).hexdigest() == digest
                        for item, digest in before.items()),
                    "INPUT_CHANGED", "待审清单运行期间配置或审阅文件发生变化", str(config))
            return {"mode": "v6_import_review_queue", "since": since.isoformat(),
                    "through": through.isoformat(), "ledger_snapshot": fin.digest,
                    "latest_booked_transaction_date": max(booked_dates).isoformat() if booked_dates else None,
                    "booked_transactions_in_range": sum(since <= day <= through for day in booked_dates),
                    "source_review_state": "NEEDS_REVIEW",
                    "counts": dict(sorted(Counter(item["kind"] for item in tag_errors).items())), "items": tag_errors,
                    "suppressed_pool_internal_h_postings": None, "preflight_only": True,
                    "note": "先修正上述 fund／budget 标签并重跑；本次尚未检查其他待审来源。"}

        try:
            accounts = audit_accounts(root, cutover, path("accounts"))
        except BudgetError as exc:
            if exc.code != "H_NEGATIVE_INVENTORY" or exc.source is None:
                raise
            matches = [key for key, source in source_by_ref.items()
                       if source == exc.source and since <= dated[key] <= through]
            if not matches:
                raise
            key = matches[0]
            posting = posting_by_ref[key]
            item = {"kind": "inventory", "date": dated[key].isoformat(),
                    "account": posting.account, "commodity": posting.units.currency,
                    "signed_units": exact_text(exact(posting.units.number)),
                    "ref": facts.for_posting(posting).as_dict(), "source": exc.source,
                    "reason_code": exc.code, "detail": exc.message}
            require(all(input_is_file(item) and sha256(input_bytes(item)).hexdigest() == digest
                        for item, digest in before.items()),
                    "INPUT_CHANGED", "待审清单运行期间配置或审阅文件发生变化", str(config))
            return {"mode": "v6_import_review_queue", "since": since.isoformat(),
                    "through": through.isoformat(), "ledger_snapshot": fin.digest,
                    "latest_booked_transaction_date": max(booked_dates).isoformat() if booked_dates else None,
                    "booked_transactions_in_range": sum(since <= day <= through for day in booked_dates),
                    "source_review_state": "NEEDS_REVIEW", "counts": {"inventory": 1},
                    "items": [item], "suppressed_pool_internal_h_postings": None,
                    "preflight_only": True,
                    "note": "先核对这笔真实库存负值的账本顺序、期初余额或账户语义并重跑；其余来源尚未检查。"}
        categories = audit_classification(root, cutover, path("classifications"))
        opening = path("opening") if "opening" in doc else None
        ownership = audit_ownership(root, cutover, path("accounts"), path("ownership"), opening)
        obligations = audit_simple_obligations(root, cutover, path("obligations"), through)
        pool = audit_purpose_pools(root, cutover, through, path("purpose_pools"))
        transfers = audit_h_transfers(root, cutover, path("accounts"), path("ownership"),
                                      path("h_transfers") if "h_transfers" in doc else None,
                                      opening)
        k_events = audit_k_events(root, cutover, path("accounts"), path("ownership"),
                                  path("k_events") if "k_events" in doc else None)
        exchange = audit_h_fx(root, cutover, path("accounts"), path("ownership"),
                              path("h_fx") if "h_fx" in doc else None)
        pool_accounts = {name for row in pool["pools"] for name in row["accounts"]}
        require(len({fin.digest, accounts["ledger_snapshot"],
                     categories["ledger_snapshot"], ownership["ledger_snapshot"],
                     obligations["ledger_snapshot"], pool["ledger_snapshot"],
                     transfers["ledger_snapshot"], k_events["ledger_snapshot"],
                     exchange["ledger_snapshot"]}) == 1,
                "REVIEW_QUEUE_MISMATCH", "待审来源不属于同一账本快照")
        items = []

        def in_range(day: date | None) -> bool:
            return day is not None and since <= day <= through

        for account in accounts["missing_declarations"]:
            day = first_asset_date.get(account)
            if in_range(day):
                items.append({"kind": "account_semantics", "date": day.isoformat(),
                              "account": account, "source": first_asset_source.get(account),
                              "reason_code": "ACCOUNT_UNREVIEWED"})
        for coordinate in accounts["pending_payment_scope"]:
            account = coordinate.rsplit("/", 1)[0]
            day = first_asset_date.get(account)
            if day is not None and day <= through:
                items.append({"kind": "payment_scope", "date": max(day, since).isoformat(),
                              "account": account, "coordinate": coordinate,
                              "source": first_asset_source.get(account),
                              "reason_code": "PAYMENT_SCOPE_PENDING"})
        for row in categories["missing_classifications"]:
            day = dated[_key(row["ref"])]
            if in_range(day):
                items.append({"kind": "classification", "date": day.isoformat(),
                              "account": row["account"], "commodity": row["commodity"],
                              "signed_units": row["signed_units"], "ref": row["ref"],
                              "source": source_by_ref[_key(row["ref"])],
                              "reason_code": "CLASSIFICATION_UNREVIEWED"})
        for row in ownership["missing_h_ownership"]:
            key = _key(row["ref"])
            day = dated[key]
            if not in_range(day):
                continue
            kind = "asset_ownership"
            if row["account"] in pool_accounts:
                tx = transaction_by_ref[key]
                crosses = any(
                    posting.account.split(":", 1)[0] == liability_root or
                    posting.account.split(":", 1)[0] == asset_root and
                    posting.account not in pool_accounts
                    for posting in tx.postings)
                if not crosses:
                    continue
                kind = "pool_boundary"
            items.append({"kind": kind, "date": day.isoformat(),
                          "account": row["account"], "commodity": row["commodity"],
                          "signed_units": row["signed_units"], "ref": row["ref"],
                          "source": source_by_ref[key],
                          "reason_code": "POOL_BOUNDARY_PENDING" if kind == "pool_boundary"
                                         else row.get("reason_code", "OWNERSHIP_UNREVIEWED")})
        for row in obligations["events"]:
            day = date.fromisoformat(row["date"])
            dependency = row.get("responsibility_protocol") == "posting_liability_budget_v1" and day <= through
            if row["status"] == "CERTIFIED" or not (in_range(day) or dependency):
                continue
            items.append({"kind": "booked_obligation", "date": row["date"],
                          **({"detail": row["detail"]} if "detail" in row else {}),
                          "account": row["account"], "commodity": row["commodity"],
                          "signed_units": row["signed_units"],
                          "ref": row["liability_ref"], "shape": row["shape"],
                          **{key: row[key] for key in ("counterpart_refs", "expense_refs", "asset_refs", "income_refs")
                             if key in row},
                          **({key: row[key] for key in ("proof", "purchase_group_ref", "liability_refs",
                                                        "expense_total", "cash_paid", "debt_total")}
                             if row.get("proof") == "native_joint_card_purchase_v1" else {}),
                          **({key: row[key] for key in ("proof", "payment_group_ref", "liability_refs", "cash_paid")}
                             if row.get("proof") == "native_group_payment_v1" else {}),
                          "source": source_by_ref[_key(row["liability_ref"])],
                          "reason_code": row["reason_codes"][0]})
        for row in transfers["events"]:
            day = date.fromisoformat(row["date"])
            if (not in_range(day) or row["status"] == "CERTIFIED" or
                    row["shape"] != "simple_cash" or
                    {row["from_account"], row["to_account"]} <= pool_accounts):
                continue
            items.append({"kind": "h_transfer", "date": row["date"],
                          "account": row["from_account"], "to_account": row["to_account"],
                          "commodity": row["commodity"],
                          "signed_units": row["signed_from_units"],
                          "ref": row["from_ref"], "to_ref": row["to_ref"],
                          "source": source_by_ref[_key(row["from_ref"])],
                          "to_source": source_by_ref[_key(row["to_ref"])],
                          "reason_code": row["reason_codes"][0]})
        for row in k_events["events"]:
            day = date.fromisoformat(row["date"])
            if not in_range(day) or row["status"] == "CERTIFIED":
                continue
            cash_ref = row["cash_ref"]
            items.append({"kind": "k_event", "date": row["date"],
                          "account": row["k_account"], "cash_account": row["cash_account"],
                          "commodity": row["commodity"],
                          "signed_units": row["signed_k_units"],
                          "ref": row["k_ref"], "cash_ref": cash_ref,
                          "source": source_by_ref[_key(row["k_ref"])],
                          "to_source": source_by_ref[_key(cash_ref)] if cash_ref else None,
                          "reason_code": row["reason_codes"][0]})
        for row in exchange["events"]:
            day = date.fromisoformat(row["date"])
            acquired = posting_by_ref[_key(row["to_ref"])]
            if (not in_range(day) or row["status"] == "CERTIFIED" or
                    {row["from_account"], row["to_account"]} <= pool_accounts or
                    acquired.cost is not None):
                continue
            items.append({"kind": "h_fx", "date": row["date"],
                          "account": row["from_account"], "to_account": row["to_account"],
                          "commodity": row["from_commodity"],
                          "to_commodity": row["to_commodity"],
                          "signed_units": row["signed_from_units"],
                          "signed_to_units": row["signed_to_units"],
                          "ref": row["from_ref"], "to_ref": row["to_ref"],
                          "source": source_by_ref[_key(row["from_ref"])],
                          "to_source": source_by_ref[_key(row["to_ref"])],
                          "reason_code": row["reason_code"]})
        if since == cutover:
            for row in obligations["opening_pending"]:
                items.append({"kind": "liability_opening", "date": cutover.isoformat(),
                              **row, "source": liability_opening_sources.get(
                                  (row["account"], row["commodity"], row["lot"])),
                              "reason_code": row["reason_code"]})
        require(all(input_is_file(item) and sha256(input_bytes(item)).hexdigest() == digest
                    for item, digest in before.items()),
                "INPUT_CHANGED", "待审清单运行期间配置或审阅文件发生变化", str(config))

    items.sort(key=lambda row: (row["date"], row["kind"], row["account"],
                                json.dumps(row.get("ref", {}), sort_keys=True)))
    items = annotate_source_items(items)
    return {"mode": "v6_import_review_queue", "since": since.isoformat(),
            "through": through.isoformat(), "ledger_snapshot": fin.digest,
            "latest_booked_transaction_date": max(booked_dates).isoformat() if booked_dates else None,
            "booked_transactions_in_range": sum(since <= day <= through for day in booked_dates),
            "source_review_state": "NEEDS_REVIEW" if items else "NO_SOURCE_ITEMS",
            "counts": dict(sorted(Counter(row["kind"] for row in items).items())),
            "items": items,
            "suppressed_pool_internal_h_postings": sum(
                1 for row in ownership["missing_h_ownership"]
                if in_range(dated[_key(row["ref"])]) and
                row["account"] in pool_accounts and not any(
                    posting.account.split(":", 1)[0] == liability_root or
                    posting.account.split(":", 1)[0] == asset_root and
                    posting.account not in pool_accounts
                    for posting in transaction_by_ref[_key(row["ref"])].postings)),
            "note": "这里只列未审来源；无待办不证明目标、估值、用途库存或首页已认证。"}


def render_text(report: dict) -> str:
    lines = [f"v6 导入后待审：{report['since']} 至 {report['through']}"]
    if report.get("preflight_only"):
        lines.append(f"来源预检待审 {len(report['items'])} 项；其余来源尚未检查")
    else:
        lines.append(f"待审 {len(report['items'])} 项；证券池内部免逐腿审阅 "
                     f"{report['suppressed_pool_internal_h_postings']} 条")
    if not report["booked_transactions_in_range"]:
        latest = report["latest_booked_transaction_date"] or "无"
        lines.append(f"查询区间没有已记交易；最近已记交易日 {latest}。请核对 importer 是否已覆盖查询区间。")
    for item in report["items"]:
        description = LABELS[item["kind"]]
        if item.get("reason_kind") == "implementation_limit":
            description += "／程序实现缺口"
        amount = (f" {item['signed_units']} {item['commodity']}"
                  if "signed_units" in item and "commodity" in item else "")
        if item["kind"] == "h_fx":
            amount += f" → {item['signed_to_units']} {item['to_commodity']}"
        account = item["account"]
        if item.get("to_account"):
            account += f" → {item['to_account']}"
        elif item.get("cash_account"):
            account += f" ↔ {item['cash_account']}"
        location = item.get("source") or (item["ref"]["path"] if "ref" in item else None)
        source = f" [{location}]" if location else ""
        if item.get("to_source"):
            source += f" → [{item['to_source']}]"
        lines.append(f"  {item['date']} {description}（{item['reason_code']}）："
                     f"{account}{amount}{source}")
        if item.get("detail"):
            lines.append(f"    原因：{item['detail']}")
    lines.append(report["note"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="汇总 importer 后的 v6 来源待审项")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--since", required=True, type=date.fromisoformat,
                        help="待审区间起日，包含该日")
    parser.add_argument("--through", required=True, type=date.fromisoformat,
                        help="待审区间止日，包含该日")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = audit_review_queue(args.config, args.since, args.through)
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message,
                                    "source": exc.source}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(render_text(result) if args.format == "text" else
          json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

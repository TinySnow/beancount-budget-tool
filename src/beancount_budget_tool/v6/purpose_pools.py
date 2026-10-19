"""Source-bound nominal protection across an excluded H portfolio.

This is a book-cost purpose overlay, not per-lot ownership or a market value.
It lets a user protect a fixed principal in a rolling securities portfolio
without pretending that particular shares are permanently earmarked.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import json
import re

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import FinancialInput, load_financial
from ..yamlstrict import load_yaml, require_yaml, yaml_path_location
from .accounts import _lot
from .facts import FactIndex
from .ownership import ID


AMOUNT = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?\Z")


def opening_fingerprint(fin: FinancialInput, cutover: date,
                        accounts: list[str]) -> str:
    """Pin the pre-cutover holdings without expiring on later imports."""
    facts = FactIndex(fin)
    rows = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date >= cutover:
            continue
        for posting in entry.postings:
            if posting.account in accounts:
                rows.append([facts.for_posting(posting).as_dict(), posting.account,
                             exact_text(exact(posting.units.number)), posting.units.currency,
                             _lot(posting)])
    return sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def _book_weight(posting: data.Posting, currency: str) -> Fraction:
    source = f"{(posting.meta or {}).get('filename', '?')}:{(posting.meta or {}).get('lineno', '?')}"
    if posting.cost is None:
        require(posting.units.currency == currency, "POOL_COST_MISSING",
                "资金池的非报告货币资产缺少 booked 成本", source)
        return exact(posting.units.number)
    require(posting.cost.currency == currency and posting.cost.number is not None,
            "POOL_COST_CURRENCY", "资金池成本必须使用报告货币", source)
    require(exact(posting.cost.number) > 0, "POOL_COST_INVALID",
            "资金池 booked 单位成本必须为正数", source)
    return exact(posting.units.number) * exact(posting.cost.number)


def audit_purpose_pools(root: Path, cutover: date, as_of: date,
                        declarations: Path) -> dict:
    """Audit nominal purpose accounting at every booked transaction boundary.

    An overlay covers *all* direct postings in its declared accounts; no
    source leg is inferred to be a particular owner's lot. A declared account
    priority allocates the protected amount for the account-by-purpose view.
    That display allocation is recalculated from booked balances and does not
    itself create a purpose transfer or monthly G.
    """
    require(cutover.day == 1 and as_of >= cutover, "TIME_INVALID",
            "资金池需要自然月 cutover 和不早于它的日末日期")
    fin = load_financial(root)
    document = load_yaml(declarations)
    fields = {"schema", "cutover", "report_currency", "pools"}
    invalid = sorted(set(document) - fields, key=str) if type(document) is dict else []
    require_yaml(type(document) is dict and set(document) == fields,
                 "POOL_DECLARATION_INVALID", "资金池声明字段不完整或含未知字段", declarations,
                 (invalid[0],) if invalid else ())
    currency = document["report_currency"]
    for field, valid in (
        ("schema", type(document["schema"]) is int and document["schema"] == 6),
        ("cutover", type(document["cutover"]) is str and document["cutover"] == cutover.isoformat()),
        ("report_currency", type(currency) is str and currency in fin.options.get("operating_currency", [])),
        ("pools", type(document["pools"]) is list),
    ):
        require_yaml(valid, "POOL_DECLARATION_INVALID", "资金池版本、报告货币或列表无效", declarations, (field,))

    used_accounts: set[str] = set()
    used_ids: set[str] = set()
    reports = []
    for row_index, row in enumerate(document["pools"]):
        keys = ("pools", row_index)
        source = yaml_path_location(declarations, keys)

        def check(condition, code, message, *fields):
            require_yaml(condition, code, message, declarations, (*keys, *fields))

        required = {
            "id", "accounts", "reserve_order", "payment_scope",
            "protected_owner_id", "protected_label", "protected_principal",
            "remainder_owner_id", "remainder_label", "reason"}
        allowed = required | {"opening_fingerprint", "remainder_policy"}
        invalid = sorted(set(row) - allowed, key=str) if type(row) is dict else []
        check(type(row) is dict and required <= set(row) <= allowed,
              "POOL_DECLARATION_INVALID", "每个资金池须明确账户、本金、用途和理由", *invalid[:1])
        policy = row.get("remainder_policy", "require_coverage")
        check(type(policy) is str and policy in {"require_coverage", "allow_negative"},
              "POOL_REMAINDER_POLICY_INVALID", "资金池余款规则须为 require_coverage 或 allow_negative", "remainder_policy")
        ident = row["id"]
        names = row["accounts"]
        check(type(ident) is str and bool(ident) and ident not in used_ids,
              "POOL_DECLARATION_INVALID", "资金池 ID 重复／无效", "id")
        check(type(names) is list and bool(names) and
              all(type(name) is str for name in names) and len(names) == len(set(names)),
              "POOL_DECLARATION_INVALID", "资金池账户列表重复／无效", "accounts")
        used_ids.add(ident)
        check(row["payment_scope"] == "excluded",
              "POOL_PAYMENT_SCOPE_INVALID", "资金池账户必须由用户声明排除直接支付", "payment_scope")
        order = row["reserve_order"]
        check(type(order) is list and bool(order) and all(type(name) is str for name in order) and
              order == list(dict.fromkeys(order)) and set(order) == set(names), "POOL_RESERVE_ORDER_INVALID",
              "账户展示的保护额分配顺序须恰好覆盖资金池账户", "reserve_order")
        check(type(row["reason"]) is str and bool(row["reason"].strip()),
              "POOL_REASON_MISSING", "资金池保护需要人工审阅理由", "reason")
        protected, remainder = row["protected_owner_id"], row["remainder_owner_id"]
        for field in ("protected_owner_id", "remainder_owner_id"):
            check(type(row[field]) is str and ID.fullmatch(row[field]) is not None,
                  "POOL_OWNER_INVALID", "资金池须声明不同的稳定用途 ID", field)
        check(protected != remainder, "POOL_OWNER_INVALID", "资金池须声明不同的稳定用途 ID", "remainder_owner_id")
        for field in ("protected_label", "remainder_label"):
            check(type(row[field]) is str and bool(row[field].strip()) and row[field] == row[field].strip(),
                  "POOL_LABEL_INVALID", "资金池用途展示名称不能为空", field)
        raw_amount = row["protected_principal"]
        check(type(raw_amount) is str and AMOUNT.fullmatch(raw_amount) is not None and
              Fraction(raw_amount) > 0, "POOL_PRINCIPAL_INVALID",
              "受保护本金须为正的精确金额字符串", "protected_principal")
        principal = Fraction(raw_amount)
        for index, name in enumerate(names):
            check(name.startswith(fin.options["name_assets"] + ":") and name not in used_accounts,
                  "POOL_ACCOUNT_INVALID", "资金池只能包含不重复的完整资产账户", "accounts", index)
            used_accounts.add(name)
        balance = Fraction(0)
        per_account = {name: Fraction(0) for name in names}
        lots: dict[tuple[str, str], Fraction] = {}
        opening = None
        minimum = None
        tx_count = 0
        posting_count = 0
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction):
                continue
            if opening is None and entry.date >= cutover:
                opening = balance
                minimum = opening
                require(all(units >= 0 for units in lots.values()),
                        "POOL_NEGATIVE_OPENING", "资金池期初 booked lot 为负", source)
            if entry.date > as_of:
                break
            relevant = [posting for posting in entry.postings if posting.account in names]
            if not relevant:
                continue
            before_balance = balance
            changes: dict[tuple[str, str], Fraction] = {}
            for posting in relevant:
                delta = _book_weight(posting, currency)
                balance += delta
                per_account[posting.account] += delta
                key = (posting.account, _lot(posting))
                changes[key] = changes.get(key, Fraction(0)) + exact(posting.units.number)
            for key, delta in changes.items():
                lots[key] = lots.get(key, Fraction(0)) + delta
                if entry.date >= cutover:
                    require(lots[key] >= 0, "POOL_NEGATIVE_LOT",
                            "资金池证券或现金 booked 库存为负",
                            f"{entry.meta.get('filename')}:{entry.meta.get('lineno')}")
            if entry.date >= cutover:
                if policy == "allow_negative" and balance < min(principal, before_balance):
                    # A realised loss can overdraw a purpose, but moving money
                    # out voluntarily cannot borrow from the fixed principal.
                    outside = [p for p in entry.postings if p.account not in names]
                    require(bool(outside) and all(p.account.split(":", 1)[0] in {
                        fin.options["name_income"], fin.options["name_expenses"]} for p in outside),
                        "POOL_VOLUNTARY_OVERDRAFT", "池外主动划拨不能产生或扩大名义用途缺口",
                        f"{entry.meta.get('filename')}:{entry.meta.get('lineno')}")
                tx_count += 1
                posting_count += len(relevant)
                if minimum is None or balance < minimum:
                    minimum = balance
        if opening is None:
            opening = balance
            require(all(units >= 0 for units in lots.values()),
                    "POOL_NEGATIVE_OPENING", "资金池期初 booked lot 为负", source)
        if minimum is None:
            minimum = opening
        require(policy == "allow_negative" or opening >= principal and minimum >= principal,
                "POOL_PRINCIPAL_UNCOVERED",
                "资金池 booked 成本曾不足覆盖受保护本金", source)
        require(all(value >= 0 for value in per_account.values()),
                "POOL_NEGATIVE_ACCOUNT_BOOK", "资金池账户日末 booked 成本为负", source)
        remaining = principal
        assignments = {}
        for name in order:
            assigned = min(remaining, per_account[name])
            assignments[name] = assigned
            remaining -= assigned
        # When losses have exhausted the remainder, retain the full nominal
        # principal and show its uncovered part on the first reserve account.
        # This signed display is neither a physical lot claim nor a guarantee.
        if remaining and policy == "allow_negative":
            assignments[order[0]] += remaining
            remaining = Fraction(0)
        by_account = []
        for name in order:
            assigned = assignments[name]
            by_account.append({"account": name,
                               "book_cost": exact_text(per_account[name]),
                               "protected_owner_id": protected,
                               "protected_label": row["protected_label"],
                               "protected_nominal": exact_text(assigned),
                               "remainder_owner_id": remainder,
                               "remainder_label": row["remainder_label"],
                               "remainder_book_cost": exact_text(per_account[name] - assigned)})
        require(remaining == 0, "POOL_PRINCIPAL_UNCOVERED",
                "资金池账户分配后没有足额保护本金", source)
        reports.append({"id": ident, "accounts": names,
                        "reserve_order": order,
                        "remainder_policy": policy,
                        "protected_owner_id": protected,
                        "protected_label": row["protected_label"],
                        "protected_nominal_principal": exact_text(principal),
                        "remainder_owner_id": remainder,
                        "remainder_label": row["remainder_label"],
                        "opening_book_cost": exact_text(opening),
                        "minimum_book_cost_since_cutover": exact_text(minimum),
                        "closing_book_cost": exact_text(balance),
                        "remainder_book_cost": exact_text(balance - principal),
                        "principal_book_shortfall": exact_text(max(Fraction(0), principal - balance)),
                        "maximum_principal_book_shortfall": exact_text(max(Fraction(0), principal - minimum)),
                        "account_purposes": by_account,
                        "post_cutover_transactions": tx_count,
                        "post_cutover_postings": posting_count,
                        "market_value": None,
                        "market_value_status": "UNKNOWN_REQUIRES_AS_OF_PRICES",
                        "status": ("CERTIFIED_NOMINAL_PURPOSE_BALANCES" if policy == "allow_negative"
                                   else "CERTIFIED_NOMINAL_BOOK_COVERAGE")})
    return {"mode": "v6_purpose_pool_audit_only", "ledger_snapshot": fin.digest,
            "cutover": cutover.isoformat(), "as_of": as_of.isoformat(),
            "report_currency": currency, "pools": reports,
            "note": "名义本金与余款合计等于账面成本；余款为负表示本金存在账面缺口，不证明本金保全、证券市值、可变现额、逐 lot 归属或完整月度 G"}

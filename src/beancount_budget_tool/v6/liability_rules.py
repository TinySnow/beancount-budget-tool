"""Configured, homogeneous liability accounts; no purchase-by-purchase guesses.

A rule declares that one account/currency is one settlement pool with one
responsibility purpose. Signed credit balances remain receivables, never cash.
Expenses alone recognize consumption; liability budget labels only classify
the debt movement. Unconfigured or structurally mixed events keep their review.
"""

from collections import defaultdict
from datetime import date
from fractions import Fraction
from hashlib import sha256

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..refs import _lot
from ..yamlstrict import require_yaml
from .classification import _key, _name
from .inline_amounts import parse_inline_amounts
from .source import _source


ROLES = {"credit_card", "payable", "receivable", "loan"}


def validate_rules(rules, owners, source, *, public=False, liability_root="Liabilities"):
    """Use the same contract for public names and compiled stable owner IDs."""
    prefix = "liabilities" if public else "account_rules"
    require_yaml(type(rules) is dict, "LIABILITY_RULE_INVALID", "负债账户规则须为账户映射", source, (prefix,))
    field = "purpose" if public else "owner_id"
    for account, rule in rules.items():
        keys = (prefix, account)
        require_yaml(type(account) is str and account.startswith(liability_root + ":") and
                     type(rule) is dict and {"role", field} <= set(rule) <= {"role", field, "due_on"},
                     "LIABILITY_RULE_INVALID", "规则须含 role、purpose；仅简单到期贷款另填 due_on", source, keys)
        require_yaml(type(rule["role"]) is str and rule["role"] in ROLES,
                     "LIABILITY_RULE_INVALID", "role 须为 credit_card、payable、receivable 或 loan", source, (*keys, "role"))
        require_yaml(type(rule[field]) is str and rule[field] in owners,
                     "LIABILITY_RULE_INVALID", "责任用途须为已登记的单一资金用途", source, (*keys, field))
        if public:
            name = rule[field]
            require_yaml(bool(name) and name == name.strip() and name != "-" and not any(c in name for c in "|｜,，;；"),
                         "LIABILITY_RULE_INVALID", "purpose 须为一个规范的资金用途名称", source, (*keys, field))
        require_yaml((rule["role"] == "loan") == ("due_on" in rule),
                     "LIABILITY_RULE_INVALID", "loan 须填全部剩余本金的到期日 due_on；分期贷款须另行拆分到期份额", source, keys)
        if "due_on" in rule:
            try:
                valid = type(rule["due_on"]) is str and date.fromisoformat(rule["due_on"]).isoformat() == rule["due_on"]
            except ValueError:
                valid = False
            require_yaml(valid, "LIABILITY_RULE_INVALID", "due_on 须为带引号的 YYYY-MM-DD 日期", source, (*keys, "due_on"))
    return rules


def native_rule_flow(fin, entry, rules, cash_accounts):
    """Recognize a complete exact native equation, never individual leg pairs."""
    if not isinstance(entry, data.Transaction):
        return None
    roots = {kind: fin.options["name_" + kind] for kind in ("assets", "liabilities", "expenses", "income")}
    parts = {kind: tuple(p for p in entry.postings if p.account.split(":", 1)[0] == root)
             for kind, root in roots.items()}
    debts, assets, expenses, incomes = (parts[k] for k in ("liabilities", "assets", "expenses", "income"))
    if (not debts or any(p.account not in rules for p in debts) or
            sum(len(v) for v in parts.values()) != len(entry.postings) or
            any(p.account not in cash_accounts for p in assets)):
        return None
    currency = debts[0].units.currency
    if (any(p.units.currency != currency or p.cost is not None or p.price is not None or
            fin.raw_price(p)[0] != "none" for p in entry.postings) or
            sum((exact(p.units.number) for p in entry.postings), Fraction(0)) != 0):
        return None
    positive = lambda rows: all(exact(p.units.number) >= 0 for p in rows)
    negative = lambda rows: all(exact(p.units.number) <= 0 for p in rows)
    roles = {rules[p.account]["role"] for p in debts}
    if expenses and not incomes and negative(debts) and negative(assets) and positive(expenses):
        return "consumer_purchase" if roles <= {"credit_card", "payable"} else None
    if expenses and not incomes and positive(debts) and positive(assets) and negative(expenses):
        return "consumer_refund" if roles <= {"credit_card", "payable"} else None
    if assets and positive(debts) and negative(assets) and positive(expenses) and not incomes:
        return "payment"
    if assets and negative(debts) and positive(assets) and negative(incomes) and not expenses:
        return "receipt"
    if (not assets and not expenses and not incomes and
            len({rules[p.account]["owner_id"] for p in debts}) == 1 and
            (roles <= {"credit_card", "payable"} or roles == {"loan"})):
        return "refinance"
    return None


def cash_owner_parts(fin, entry, flow, rules, default_owner):
    """Use public cash defaults independently of the debt's responsibility.

    Income and borrowing receipts retain the public income default. Receivable
    reimbursement restores its declared original purpose. Mixed cash purposes
    require explicit asset fund slices rather than a proportional guess.
    """
    debts = [p for p in entry.postings if p.account in rules]
    if flow != "receipt" or all(rules[p.account]["role"] != "receivable" for p in debts):
        return default_owner
    owners = {rules[p.account]["owner_id"] for p in debts}
    return next(iter(owners)) if all(rules[p.account]["role"] == "receivable" for p in debts) and len(owners) == 1 else None


def liability_budget(posting, names=None):
    raw = (posting.meta or {}).get("budget")
    if raw is None:
        return None
    source = _source(posting.meta or {})
    parts = parse_inline_amounts(raw, exact(posting.units.number), source,
                                known_names=set(names) if names is not None else None,
                                code="CLASSIFICATION_UNITS_MISMATCH")
    slices = []
    for name, units in parts:
        if name != "-":
            name = _name(name)
            require(names is None or name in names, "CATEGORY_UNKNOWN", f"负债 budget 类别未登记：{name}", source)
        slices.append({"name": name, "signed_units": exact_text(units)})
    return slices


def audit_rule_accounts(fin, facts, cutover, as_of, rules, cash_accounts, *, before_ref=None,
                        budget_names=None, owner_names=None, default_expense_owner=None):
    """Replay declared settlement pools, retaining every source and original order."""
    signed = defaultdict(Fraction)
    origins, latest, pools, events = {}, {}, {}, []
    sources, source_refs = {}, defaultdict(list)
    transactions = [e for e in fin.entries if isinstance(e, data.Transaction)]
    rules_by_ref = {_key(facts.for_posting(p).as_dict()): rules[p.account]
                    for e in transactions for p in e.postings if p.account in rules}
    before_entry = next((e for e in transactions for p in e.postings
                         if before_ref == _key(facts.for_posting(p).as_dict())), None) if before_ref else None
    for entry in transactions:
        if as_of is not None and entry.date > as_of:
            continue
        if entry is before_entry:
            break
        managed = [p for p in entry.postings if p.account in rules]
        if not managed:
            continue
        flow = native_rule_flow(fin, entry, rules, cash_accounts) if entry.date >= cutover else None
        for posting in managed:
            ref = facts.for_posting(posting).as_dict()
            key = _key(ref)
            coordinate = (posting.account, posting.units.currency, _lot(posting))
            rule = rules[posting.account]
            pool_id = "account_" + sha256(_key({"coordinate": coordinate, **rule}).encode()).hexdigest()
            pools[coordinate] = (pool_id, rule)
            origins.setdefault(coordinate, (ref, entry.date.isoformat()))
            latest[coordinate] = ref
            sources[coordinate] = _source(posting.meta or entry.meta)
            source_refs[coordinate].append(ref)
            signed[coordinate] += exact(posting.units.number)
            if entry.date < cutover:
                continue
            kind = flow
            if flow == "payment" and rule["role"] == "receivable":
                kind = "advance_outflow"
            elif flow == "receipt":
                kind = "advance_reimbursement" if rule["role"] == "receivable" else "payable_receipt"
            asset_refs = [facts.for_posting(p).as_dict() for p in entry.postings
                          if p.account.split(":", 1)[0] == fin.options["name_assets"]]
            event = {"liability_ref": ref, "counterpart_ref": asset_refs[0] if len(asset_refs) == 1 else None,
                     "counterpart_refs": [facts.for_posting(p).as_dict() for p in entry.postings if p is not posting],
                     "asset_refs": asset_refs, "liability_refs": [facts.for_posting(p).as_dict() for p in managed],
                     "expense_refs": [facts.for_posting(p).as_dict() for p in entry.postings
                                      if p.account.split(":", 1)[0] == fin.options["name_expenses"]],
                     "account": posting.account, "commodity": posting.units.currency, "lot": coordinate[2],
                     "date": entry.date.isoformat(), "signed_units": exact_text(exact(posting.units.number)),
                     "shape": flow or "unsupported", "kind": kind,
                     "status": "CERTIFIED" if flow is not None else "UNSUPPORTED",
                     "reason_codes": [] if flow is not None else ["OBLIGATION_EVENT_UNSUPPORTED"],
                     "obligation_id": pool_id if rule["role"] != "receivable" else None,
                     "clearing_id": pool_id if rule["role"] == "receivable" else None,
                     "owner_id": rule["owner_id"], "claim_origin": "account_rule",
                     "proof": "configured_liability_account_v1", "account_role": rule["role"],
                     "transaction_ref": facts.for_transaction(entry).as_dict(),
                     "review_reason": "config 账户规则：同一账户／币种为单一责任用途的清算池；保留原顺序，消费仅取 Expenses"}
            if flow is not None and rule["role"] == "receivable":
                # A reimbursement may restore the original purpose only when
                # this account really represents that purpose's advance.
                for cash in (p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_assets"]):
                    raw = (cash.meta or {}).get("fund")
                    parts = parse_inline_amounts(raw, exact(cash.units.number), _source(cash.meta or {}),
                                                 known_names=set(owner_names or {})) if raw is not None else None
                    cash_owners = ({(owner_names or {}).get(name) for name, _ in parts} if parts is not None else
                                   {rule["owner_id"] if flow == "receipt" else default_expense_owner})
                    if cash_owners != {rule["owner_id"]}:
                        event.update(status="NEEDS_REVIEW", reason_codes=["LIABILITY_RULE_PURPOSE_MISMATCH"])
            labels = liability_budget(posting, budget_names)
            if labels is not None:
                event["budget_slices"] = labels
            events.append(event)
    debts, advances, pending, opening = [], [], [], []
    for coordinate, balance in sorted(signed.items()):
        ident, rule = pools[coordinate]
        ref, originated = origins[coordinate]
        account, commodity, lot = coordinate
        common = {"account": account, "commodity": commodity, "lot": lot,
                  "origin": "account_rule", "account_role": rule["role"]}
        if balance < 0:
            debts.append({**common, "obligation_id": ident, "owner_id": rule["owner_id"],
                          "remaining_native": exact_text(-balance), "purchase_ref": latest[coordinate],
                          "purchase_date": None, "source_refs": source_refs[coordinate],
                          **({"debt_kind": "nonconsumer", "due_on": rule["due_on"]} if rule["role"] == "loan" else {})})
        elif balance > 0:
            advances.append({**common, "clearing_id": ident, "remaining_native": exact_text(balance),
                             "outflow_ref": latest[coordinate]})
        if rule["role"] == "receivable" and balance < 0 or lot != commodity + "/no-cost":
            pending.append({**common, "signed_booked_units": exact_text(balance),
                            "reason_code": "LIABILITY_RULE_BALANCE_INVALID", "origin_source": sources[coordinate]})
        if originated < cutover.isoformat():
            opening.append({**common, "liability_ref": ref, "kind": "account_rule",
                            "reason": "配置声明单一用途清算池，期初余额直接取 cutover 前已记事实；不重算历史消费"})
    return {"events": events, "obligations": debts, "clearing_advances": advances,
            "opening_pending": pending, "opening_declarations": opening, "keys": set(rules_by_ref),
            "rules_by_ref": rules_by_ref,
            "before_entry": before_entry}


def activity_report(audit, month):
    """Optional report section, separate from Expenses and their targets."""
    events = [row for row in audit["events"] if row["date"][:7] == month and
              (row.get("claim_origin") == "account_rule" or "budget_slices" in row)]
    if not events:
        return None
    totals = defaultdict(Fraction)
    for row in events:
        for part in row.get("budget_slices", []):
            totals[(row["kind"], part["name"], row["commodity"])] += Fraction(part["signed_units"])
    return {"events": events, "budget_totals": [
        {"kind": kind, "category": name, "commodity": currency, "signed_liability_units": exact_text(units)}
        for (kind, name, currency), units in sorted(totals.items(), key=lambda item: str(item[0]))],
        "note": "负债 budget 只分类债务变动，不增加消费；正数减少负债／增加预付，负数增加负债／减少应收。利息费用取 Expenses。"}

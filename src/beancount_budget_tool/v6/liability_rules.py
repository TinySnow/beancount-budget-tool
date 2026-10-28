"""Native liability transaction proofs and cash-credit purpose tracking.

One responsibility replay is keyed by account, commodity and posting budget.
Account metadata carries roles and due dates, never a second purpose mode.
Signed credits remain receivables, never cash; Expenses recognize consumption.
Unsupported transaction shapes retain their review rather than guessed splits.
"""

from collections import defaultdict
from datetime import date
from fractions import Fraction
from types import SimpleNamespace

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text, amount_text
from ..refs import _lot
from ..yamlstrict import require_yaml
from .classification import _key, _name
from .inline_amounts import parse_inline_amounts
from .native_flows import native_payable_receipt
from .source import _source


ROLES = {"credit_card", "payable", "receivable", "loan"}


def certify_credit_income_receipt(fin, entry, group):
    """Extend only a proved full credit-return leg, never new borrowing.

    Reuse the complete one-cash/one-liability native equation. Purpose-source
    restoration is checked separately by CreditOwners against actual cash.
    """
    native = native_payable_receipt(fin, entry)
    if (native is None or len(group) != 1 or group[0]["status"] != "CERTIFIED" or
            group[0]["kind"] != "credit_return" or
            Fraction(group[0]["credit_return_native"]) != native.payable):
        return False
    group[0].update(receipt_protocol="native_credit_income_receipt_v1",
                    receipt_credit_native=exact_text(native.payable),
                    receipt_income_native=exact_text(native.income))
    return True


def validate_rules(rules, owners, source, *, public=False, liability_root="Liabilities"):
    """Use the same contract for public names and compiled stable owner IDs."""
    prefix = "liabilities" if public else "account_rules"
    require_yaml(type(rules) is dict, "LIABILITY_RULE_INVALID", "负债账户规则须为账户映射", source, (prefix,))
    field = "owner_id"
    for account, rule in rules.items():
        keys = (prefix, account)
        if public and type(rule) is dict:
            require_yaml('purpose' not in rule, 'LIABILITY_MODE_REMOVED',
                         '整户 purpose 已取消；普通应付款可删除整项，责任用途写在 Liabilities 分录的 budget。贷款／应收只保留 role 和必要的 due_on。',
                         source, (*keys, 'purpose'))
        required = {'role'} if public else {'role', field}
        allowed = required | {'due_on'}
        require_yaml(type(account) is str and account.startswith(liability_root + ':') and
                     type(rule) is dict and required <= set(rule) <= allowed,
                     'LIABILITY_RULE_INVALID', '仅保留财务性质 role 和贷款到期日 due_on；用途由逐笔 budget 决定', source, keys)
        require_yaml(type(rule["role"]) is str and rule["role"] in ROLES,
                     "LIABILITY_RULE_INVALID", "role 须为 credit_card、payable、receivable 或 loan", source, (*keys, "role"))
        if not public:
            require_yaml(type(rule[field]) is str and rule[field] in owners,
                         'LIABILITY_RULE_INVALID', '默认责任用途须引用已登记用途', source, (*keys, field))
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
            (roles <= {"credit_card", "payable"} or roles == {"loan"})):
        return "refinance"
    return None


def cash_owner_parts(fin, entry, flow, rules, default_owner, posting, responsibilities):
    """Use public cash defaults independently of the debt's responsibility.

    Income and borrowing receipts retain the public income default. Receivable
    reimbursement restores its declared original purpose. Mixed cash purposes
    require explicit asset fund slices rather than a proportional guess.
    """
    debts = [p for p in entry.postings if p.account in rules]
    units = exact(posting.units.number)
    if flow != "receipt" or all(rules[p.account]["role"] != "receivable" for p in debts):
        return [(default_owner, units)]
    parts = defaultdict(Fraction)
    for debt in debts:
        if rules[debt.account]["role"] == "receivable":
            for owner, amount in responsibilities.get(id(debt), []):
                parts[owner] -= amount
            continue
        parts[default_owner] -= exact(debt.units.number)
    for income in entry.postings:
        if income.account.split(":", 1)[0] == fin.options["name_income"]:
            parts[default_owner] -= exact(income.units.number)
    cash = [p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_assets"]]
    if len(cash) == 1 and sum(parts.values(), Fraction(0)) == units:
        return list(parts.items())
    return [(next(iter(parts)), units)] if len(parts) == 1 else None


def receivable_cash_matches(fin, entry, rules, owned, responsibilities):
    """Prove the combined owner vector, without assigning whole cash to each debt."""
    needed, cash = defaultdict(Fraction), defaultdict(Fraction)
    debts = [p for p in entry.postings if p.account in rules and rules[p.account]["role"] == "receivable"]
    for debt in debts:
        for owner, units in responsibilities.get(id(debt), []):
            needed[owner] += abs(units)
    for posting in entry.postings:
        if posting.account.split(":", 1)[0] != fin.options["name_assets"]:
            continue
        parts = owned.get(id(posting))
        if parts is None:
            return None
        for part in parts:
            cash[part["owner_id"]] += abs(Fraction(part["signed_units"]))
    return all(cash[owner] >= units for owner, units in needed.items())


class CreditOwners:
    """Track proved prepayment purposes, separately from responsibility owners.

    A single-purpose payment proves its excess owner's purpose; mixed cash
    cannot prove which part paid debt versus became credit. Partial spending
    of mixed credit likewise loses the allocation unless explicitly observed.
    None means unknown, never an automatic choice of income/default purpose.
    """

    def __init__(self, fin, cutover, rules, defaults, owner_names, responsibilities):
        self.fin, self.rules, self.defaults = fin, rules, defaults
        self.owner_names = owner_names or {}
        self.responsibilities = responsibilities
        self.last_income_receipt = None
        self.balances, self.credits = defaultdict(Fraction), {}
        self.credit_caps, self.joint_pending = {}, set()
        for entry in fin.entries:
            if isinstance(entry, data.Transaction) and entry.date < cutover:
                for p in self.movements(entry):
                    self.balances[self.coordinate(p)] += exact(p.units.number)

    @staticmethod
    def coordinate(posting):
        coordinate = (posting.account, posting.units.currency, _lot(posting))
        owner = getattr(posting, "responsibility_owner", None)
        return (*coordinate, owner) if owner is not None else coordinate

    def movements(self, entry):
        # Local exact sums only: the original booked postings and source refs
        # remain untouched. Shared cash must not be reused for duplicate legs.
        totals, first = defaultdict(Fraction), {}
        for posting in entry.postings:
            if posting.account in self.rules:
                parts = self.responsibilities.get(id(posting), [])
                for owner, units in parts:
                    proxy = SimpleNamespace(account=posting.account, cost=posting.cost, price=posting.price,
                        meta=posting.meta, responsibility_owner=owner,
                        units=SimpleNamespace(number=units, currency=posting.units.currency))
                    coordinate = self.coordinate(proxy)
                    first.setdefault(coordinate, proxy)
                    totals[coordinate] += units
        return [SimpleNamespace(**{**vars(posting), "units":
                    SimpleNamespace(number=totals[coordinate], currency=posting.units.currency)})
                for coordinate, posting in first.items()]

    def returning(self, entry):
        return [p for p in self.movements(entry) if
                self.rules[p.account]["role"] != "receivable" and exact(p.units.number) < 0 and
                self.balances[self.coordinate(p)] > 0]

    def take(self, posting, amount, *, total=None):
        coordinate = self.coordinate(posting)
        parts = self.credits.get(coordinate)
        if parts is None:
            caps = {owner: units for owner, units in (self.credit_caps.get(coordinate) or {}).items() if units}
            total = self.balances[coordinate] if total is None else total
            if caps and sum(caps.values(), Fraction(0)) == total:
                parts = caps
            elif len(caps) == 1 and next(iter(caps.values())) >= total:
                parts = {next(iter(caps)): total}
            if parts is not None:
                self.credits[coordinate] = parts
                self.credit_caps.pop(coordinate, None)
        if parts is None:
            return None
        if amount == sum(parts.values(), Fraction(0)):
            return parts.copy()
        return {next(iter(parts)): amount} if len(parts) == 1 else None

    def pure_return(self, entry, *, allow_income=False):
        moving = [p for p in self.movements(entry) if exact(p.units.number) != 0]
        returning = self.returning(entry)
        return (bool(returning) and len(moving) == len(returning) and
                all(-exact(p.units.number) <= self.balances[self.coordinate(p)] for p in returning) and
                (allow_income or not any(p.account.split(":", 1)[0] == self.fin.options["name_income"] and exact(p.units.number)
                                        for p in entry.postings)))

    def income_return(self, entry):
        return native_payable_receipt(self.fin, entry) is not None and self.pure_return(entry, allow_income=True)

    def returned_parts(self, entry):
        result = defaultdict(Fraction)
        for debt in self.returning(entry):
            parts = self.take(debt, -exact(debt.units.number))
            if parts is None:
                return None
            for owner, units in parts.items():
                result[owner] += units
        return dict(result)

    def receipt_parts(self, entry, posting):
        cash = [p for p in entry.postings if p.account.split(":", 1)[0] == self.fin.options["name_assets"]]
        mixed = self.income_return(entry)
        if not (mixed or self.pure_return(entry)):
            return None
        result = self.returned_parts(entry)
        if result is None:
            return None
        if mixed:
            native = native_payable_receipt(self.fin, entry)
            owner = self.defaults["income_owner_id"]
            result[owner] = result.get(owner, Fraction(0)) + native.income
        units = exact(posting.units.number)
        if len(cash) == 1 and sum(result.values(), Fraction(0)) == units:
            return list(result.items())
        return [(next(iter(result)), units)] if len(result) == 1 else None

    def check_return(self, requested, available, assets, entry):
        exceeded = [owner for owner, units in requested.items() if units > available.get(owner, Fraction(0))]
        if not exceeded:
            return
        currency = assets[0].units.currency
        lines = ["预付／溢缴退回的 fund 超过已证明的用途余额或上限"]
        for owner in exceeded:
            units, cap = requested[owner], available.get(owner, Fraction(0))
            lines.extend([f"资金用途：{self.owner_names.get(owner, owner)}",
                          f"本次取回：{amount_text(units)} {currency}",
                          f"已证明可取回上限：{amount_text(cap)} {currency}",
                          f"本次缺口：{amount_text(units - cap)} {currency}"])
        lines.append("核对原付款用途和收款 fund；改用途须另记明确调拨。")
        require(False, "LIABILITY_CREDIT_OWNER_EXCEEDED", "\n".join(lines), _source(assets[0].meta or entry.meta))

    def advance(self, entry, flow, owned):
        issue = None
        self.last_income_receipt = None
        assets = [p for p in entry.postings if p.account.split(":", 1)[0] == self.fin.options["name_assets"]]
        cash = defaultdict(Fraction)
        for asset in assets:
            if id(asset) not in owned:
                cash = None
                break
            for part in owned[id(asset)]:
                cash[part["owner_id"]] += abs(Fraction(part["signed_units"]))
        if cash is not None:
            cash = {owner: units for owner, units in cash.items() if units}
        debts = self.movements(entry)
        returning = self.returning(entry) if flow == "receipt" else []
        moving = [p for p in debts if exact(p.units.number) != 0]
        if flow == "receipt" and self.income_return(entry):
            restored = self.returned_parts(entry)
            if any(self.coordinate(p) in self.joint_pending for p in returning):
                issue = "LIABILITY_CREDIT_GROUP_UNSUPPORTED"
            elif restored is None or cash is None:
                issue = "LIABILITY_CREDIT_PURPOSE_PENDING"
            else:
                missing = {owner: units - cash.get(owner, Fraction(0)) for owner, units in restored.items()
                           if units > cash.get(owner, Fraction(0))}
                if missing:
                    details = [f"资金用途：{self.owner_names.get(owner, owner)}；应收回 "
                               f"{amount_text(restored[owner])}，实际收到 {amount_text(cash.get(owner, 0))}，"
                               f"缺口 {amount_text(units)} {assets[0].units.currency}"
                               for owner, units in missing.items()]
                    require(False, "LIABILITY_CREDIT_RECEIPT_PURPOSE_MISMATCH",
                            "混合到账须先恢复原预付用途，个人收入不能掩盖本金改用途：\n" + "\n".join(details),
                            _source(assets[0].meta or entry.meta))
                self.last_income_receipt = {"credit_returns": restored,
                    "personal_income": {owner: units - restored.get(owner, Fraction(0))
                                        for owner, units in cash.items() if units > restored.get(owner, Fraction(0))}}
        if self.pure_return(entry) and any(self.coordinate(p) in self.joint_pending for p in returning):
            issue = "LIABILITY_CREDIT_GROUP_UNSUPPORTED"
        if issue is None and cash and self.pure_return(entry) and len(returning) == len(moving) == 1:
            coordinate = self.coordinate(returning[0])
            existing = self.credits.get(coordinate) or self.credit_caps.get(coordinate)
            if existing is not None and sum(cash.values(), Fraction(0)) == -exact(returning[0].units.number):
                self.check_return(cash, existing, assets, entry)
        elif issue is None and cash and self.pure_return(entry) and len(returning) > 1:
            proved = defaultdict(Fraction)
            for debt in returning:
                parts = self.take(debt, -exact(debt.units.number))
                if parts is None:
                    issue = "LIABILITY_CREDIT_GROUP_UNSUPPORTED"
                    self.joint_pending.update(self.coordinate(p) for p in returning)
                    break
                for owner, units in parts.items():
                    proved[owner] += units
            if issue is None:
                self.check_return(cash, proved, assets, entry)
        moved_credit = None
        growing = {self.coordinate(p) for p in moving if exact(p.units.number) > 0 and
                   self.balances[self.coordinate(p)] + exact(p.units.number) > 0}
        if flow == "refinance":
            outgoing = [p for p in moving if exact(p.units.number) < 0]
            incoming = [p for p in moving if exact(p.units.number) > 0]
            if len(incoming) == 1 and outgoing and all(-exact(p.units.number) <= self.balances[self.coordinate(p)] for p in outgoing):
                parts = [self.take(p, -exact(p.units.number)) for p in outgoing]
                if all(part is not None for part in parts):
                    moved_credit = defaultdict(Fraction)
                    for part in parts:
                        for owner, units in part.items():
                            moved_credit[owner] += units
        for debt in debts:
            coordinate = self.coordinate(debt)
            before = self.balances[coordinate]
            after = before + exact(debt.units.number)
            self.balances[coordinate] = after
            if self.rules[debt.account]["role"] == "receivable":
                continue
            if after <= 0:
                self.credits.pop(coordinate, None)
                self.credit_caps.pop(coordinate, None)
                self.joint_pending.discard(coordinate)
            elif after > max(before, Fraction(0)):
                increase = after - max(before, Fraction(0))
                addition = None
                if flow == "consumer_refund":
                    addition = {self.defaults["expense_owner_id"]: increase}
                elif flow == "payment" and cash:
                    if len(cash) == 1:
                        addition = {next(iter(cash)): increase}
                    elif len(debts) == 1 and sum(cash.values(), Fraction(0)) == increase:
                        addition = dict(cash)
                elif flow == "refinance" and moved_credit:
                    if len(moved_credit) == 1:
                        addition = {next(iter(moved_credit)): increase}
                    elif sum(moved_credit.values(), Fraction(0)) == increase:
                        addition = dict(moved_credit)
                previous = self.credits.get(coordinate, {} if before <= 0 else None)
                if previous is None or addition is None:
                    self.credits[coordinate] = None
                    caps = self.credit_caps.get(coordinate, previous)
                    new_caps = (addition if addition is not None else dict(cash) if flow == "payment" and cash else
                                dict(moved_credit) if moved_credit is not None else None)
                    if caps is not None and new_caps is not None:
                        self.credit_caps[coordinate] = {owner: min(after, caps.get(owner, Fraction(0)) + new_caps.get(owner, Fraction(0)))
                                                        for owner in set(caps) | set(new_caps)}
                    else:
                        self.credit_caps.pop(coordinate, None)
                    if flow == "payment" and len(growing) > 1 or flow == "refinance" and moved_credit is None:
                        self.joint_pending.add(coordinate)
                else:
                    for owner, units in addition.items():
                        previous[owner] = previous.get(owner, Fraction(0)) + units
                    self.credits[coordinate] = previous
                    self.credit_caps.pop(coordinate, None)
            elif after < before:
                consumed = before - after
                parts = self.take(debt, consumed, total=before)
                if flow == "receipt" and len(debts) == 1 and cash and sum(cash.values(), Fraction(0)) == consumed:
                    existing = self.credits.get(coordinate)
                    if existing is not None and all(units <= existing.get(owner, Fraction(0)) for owner, units in cash.items()):
                        parts = dict(cash)
                if parts is None:
                    caps = self.credits.get(coordinate) or self.credit_caps.get(coordinate)
                    if caps is not None:
                        self.credit_caps[coordinate] = {owner: min(after, units) for owner, units in caps.items()}
                        if flow == "receipt" and len(moving) == 1 and cash:
                            self.credit_caps[coordinate] = {owner: min(after, units - cash.get(owner, Fraction(0)))
                                                            for owner, units in caps.items() if units > cash.get(owner, Fraction(0))}
                    self.credits[coordinate] = None
                else:
                    previous = self.credits[coordinate]
                    self.credits[coordinate] = {owner: units - parts.get(owner, Fraction(0))
                                               for owner, units in previous.items() if units > parts.get(owner, Fraction(0))}
                if flow == "refinance" and moved_credit is None:
                    self.joint_pending.add(coordinate)
        return issue


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
                        budget_owners):
    """Build source-bound events, then replay responsibility vectors exactly once."""
    from .liability_budgets import apply_budget_responsibilities
    transactions = [e for e in fin.entries if isinstance(e, data.Transaction)]
    rules_by_ref = {facts.for_posting(p).as_key(): rules[p.account]
                    for e in transactions for p in e.postings if p.account in rules}
    before_entry = next((e for e in transactions for p in e.postings
                         if before_ref == facts.for_posting(p).as_key()), None) if before_ref else None
    events, opening, seen = [], [], set()
    for entry in transactions:
        if as_of is not None and entry.date > as_of:
            continue
        if entry is before_entry:
            break
        managed = [p for p in entry.postings if p.account in rules]
        if not managed:
            continue
        flow = native_rule_flow(fin, entry, rules, cash_accounts) if entry.date >= cutover else None
        asset_refs = [facts.for_posting(p).as_dict() for p in entry.postings
                      if p.account.split(':', 1)[0] == fin.options['name_assets']]
        for posting in managed:
            ref = facts.for_posting(posting).as_dict()
            coordinate = (posting.account, posting.units.currency, _lot(posting))
            rule = rules[posting.account]
            if entry.date < cutover:
                if coordinate not in seen:
                    opening.append({'account': posting.account, 'commodity': posting.units.currency,
                        'lot': coordinate[2], 'origin': 'account_rule', 'account_role': rule['role'],
                        'liability_ref': ref, 'kind': 'account_rule',
                        'reason': '配置声明单一用途清算池，期初余额直接取 cutover 前已记事实；不重算历史消费'})
                    seen.add(coordinate)
                continue
            event = {'liability_ref': ref, 'counterpart_ref': asset_refs[0] if len(asset_refs)==1 else None,
                'counterpart_refs': [facts.for_posting(p).as_dict() for p in entry.postings if p is not posting],
                'asset_refs': asset_refs, 'liability_refs': [facts.for_posting(p).as_dict() for p in managed],
                'expense_refs': [facts.for_posting(p).as_dict() for p in entry.postings
                                 if p.account.split(':', 1)[0]==fin.options['name_expenses']],
                'account': posting.account, 'commodity': posting.units.currency, 'lot': coordinate[2],
                'date': entry.date.isoformat(), 'signed_units': exact_text(exact(posting.units.number)),
                'shape': flow or 'unsupported', 'kind': flow,
                'status': 'CERTIFIED' if flow is not None else 'UNSUPPORTED',
                'reason_codes': [] if flow is not None else ['OBLIGATION_EVENT_UNSUPPORTED'],
                'obligation_id': None, 'clearing_id': None, 'owner_id': rule['owner_id'],
                'claim_origin': 'account_rule', 'proof': 'configured_liability_account_v1',
                'account_role': rule['role'], 'transaction_ref': facts.for_transaction(entry).as_dict()}
            labels = liability_budget(posting, budget_owners)
            if labels is not None:
                event['budget_slices'] = labels
            events.append(event)
    opening.sort(key=lambda row: (row['account'], row['commodity'], row['lot']))
    result = {'events': events, 'opening_declarations': opening, 'keys': set(rules_by_ref),
              'rules_by_ref': rules_by_ref, 'before_entry': before_entry}
    return apply_budget_responsibilities(fin, facts, cutover, rules, budget_owners, result)


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
        "note": "负债 budget 按逐笔用途分配责任，不增加消费；" +
                "正数减少负债／增加预付，负数增加负债／减少应收。利息费用取 Expenses。"}

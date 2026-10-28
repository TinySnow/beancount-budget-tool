"""Source-bound ownership-intent audit for real H asset postings.

This stage validates each posting's declared owner and intent kind. It does
not replay owner inventory or infer a budget transfer from account names.
K book-flow postings are deliberately outside this claim domain.
"""

from __future__ import annotations

from datetime import date
from collections import defaultdict
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re

from beancount.core import data

from ..errors import BudgetError, require
from ..exact import amount_text, exact, exact_text
from ..financial import load_financial, shared_audit
from ..yamlstrict import load_yaml, yaml_path_location, require_yaml
from .accounts import _lot, audit_accounts
from .classification import _key
from .facts import FactIndex, GeneratedPostingRef, future_posting_refs, future_transactions
from .inline_amounts import parse_inline_amounts
from .liability_rules import validate_rules, native_rule_flow, cash_owner_parts, CreditOwners, receivable_cash_matches, audit_rule_accounts
from .initialization import initial_pad_postings
from .native_flows import native_asset_flow, native_card_purchase, native_payable_receipt, native_group_payment, native_book_redemption, native_h_bundle, compile_h_bundle
from .opening import audit_opening
from .provenance import valid_digest
from .source import _source, scan_v6_source


ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
NUMBER = re.compile(r"-?(?:\d+(?:\.\d+)?|\d+/[1-9]\d*)\Z")
ROLES = {"hold", "allocation", "gain"}


def _owners(rows: object, source: Path | None = None) -> dict[str, str]:
    def check(condition, code, message, keys=(), fallback=None):
        require_yaml(condition, code, message, source, ("owners", *keys), fallback)

    check(type(rows) is list, "OWNER_REGISTRY_INVALID", "owners 必须是列表")
    names = {}
    identifiers = set()
    for index, row in enumerate(rows):
        check(type(row) is dict and set(row) == {"id", "name", "aliases"},
                "OWNER_REGISTRY_INVALID", "用途须有稳定 id/name/aliases", (index,))
        ident = row["id"]
        check(type(ident) is str and bool(ID.fullmatch(ident)) and ident not in identifiers,
                "OWNER_ID_INVALID", "用途 ID 无效或重复", (index, "id"), str(ident))
        identifiers.add(ident)
        check(type(row["aliases"]) is list, "OWNER_REGISTRY_INVALID", "aliases 必须是列表", (index, "aliases"))
        for alias_index, name in enumerate([row["name"], *row["aliases"]]):
            keys = (index, "name") if alias_index == 0 else (index, "aliases", alias_index - 1)
            check(type(name) is str and bool(name) and name == name.strip() and
                    name != "-" and not any(char in name for char in "|｜,，;；") and
                    name not in names,
                    "OWNER_NAME_INVALID", "用途名／别名非法或重复", keys, str(name))
            names[name] = ident
    return names


def _signed_units(value: object, source: str) -> Fraction:
    require(type(value) is str and bool(NUMBER.fullmatch(value)),
            "OWNERSHIP_UNITS_INVALID", "用途切片数量须为精确数字字符串", source)
    return Fraction(value)


def _require_transfer_balance(coordinate: tuple[str, str, str],
                              before: dict[str, Fraction], after: dict[str, Fraction],
                              owner_names: dict[str, str], source: str, diagnostic=None) -> None:
    # Consumption may leave a negative purpose balance; a voluntary transfer
    # may neither create a deficit nor deepen an existing one.
    overdrawn = [owner for owner, amount in after.items()
                 if amount < 0 and amount < before.get(owner, Fraction(0))]
    if not overdrawn:
        return
    account, commodity, lot = coordinate
    lines = ["主动资产用途划拨不能透支交易前余额", f"账户：{account}"]
    if lot != f"{commodity}/no-cost":
        lines.append(f"库存批次：{lot}")
    for owner in overdrawn:
        previous = before.get(owner, Fraction(0))
        available = max(previous, Fraction(0))
        outgoing = previous - after[owner]
        lines.extend([
            f"资金用途：{owner_names[owner]}",
            f"划拨前余额：{amount_text(previous)} {commodity}（可划出 {amount_text(available)} {commodity}）",
            f"本次净划出：{amount_text(outgoing)} {commodity}",
            f"本次缺口：{amount_text(outgoing - available)} {commodity}",
            f"划拨后余额：{amount_text(after[owner])} {commodity}",
        ])
    lines.append("该账户本次划拨前的全部用途余额：")
    for owner in sorted(set(before) | set(after), key=lambda ident: owner_names.get(ident, ident)):
        balance = before.get(owner, Fraction(0))
        name = owner_names.get(owner, "预算外" if owner == "-" else owner)
        lines.append(f"  {name}：{amount_text(balance)} {commodity}（可划出 {amount_text(max(balance, Fraction(0)))} {commodity}）")
    lines.append("以上为该交易前、该账户及币种／批次的余额，不是月末余额。请核对 fund；改用途须明确记虚拟调拨，不会自动挪用其他用途。")
    raise BudgetError("H_OWNER_NEGATIVE", "\n".join(lines), source, diagnostic)


def _transfer_source_parts(before, required, rule):
    """Reuse the same reviewed inventory rule for two-leg and joint transfers."""
    available = [(owner, amount) for owner, amount in before.items() if amount > 0]
    nonzero = [owner for owner, amount in before.items() if amount]
    if rule is not None:
        remaining, parts = required, []
        for owner in rule['owner_ids']:
            amount = min(remaining, max(Fraction(0), before.get(owner, Fraction(0))))
            if amount:
                parts.append((owner, -amount)); remaining -= amount
        return (None, 'FUND_SOURCE_INSUFFICIENT') if remaining else (parts, '已审转出顺序：'+rule['reason'])
    if len(nonzero)==len(available)==1 and available[0][1] >= required:
        return [(available[0][0], -required)], '交易前单一用途余额继承'
    if len(nonzero)==len(available) and available and sum((amount for _,amount in available), Fraction(0))==required:
        return [(owner,-amount) for owner,amount in available], '交易前全部用途余额搬移'
    return None, 'FUND_MIXED_SOURCE' if len(available)>1 else 'FUND_SOURCE_INSUFFICIENT'


@shared_audit
def audit_ownership(root: Path, cutover: date, accounts: Path,
                    declarations: Path | None = None,
                    opening: Path | None = None) -> dict:
    """Validate H ownership slices and identify unknown H/K/account domains."""
    scan_v6_source(root, cutover)
    account_audit = audit_accounts(root, cutover, accounts)
    semantics = {row["account"]: row.get("semantics") for row in account_audit["accounts"]}
    fin = load_financial(root)
    facts = FactIndex(fin)
    assets = fin.options["name_assets"]
    future_keys = None
    known_coordinates = {(row["account"], commodity) for row in account_audit["accounts"]
                         for commodity in row["commodities"]}
    known_coordinates.update((posting.account, posting.units.currency)
                             for entry in future_transactions(fin, cutover) for posting in entry.postings)
    h_candidates = {}
    unknown = []
    k_postings = []
    order = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry.date < cutover:
            continue
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] != assets:
                continue
            ref = facts.for_posting(posting).as_dict()
            record = {"ref": ref, "account": posting.account,
                      "signed_units": exact_text(exact(posting.units.number)),
                      "commodity": posting.units.currency}
            mode = semantics.get(posting.account)
            if mode == "K":
                # K is a signed subscription flow, not a holding. Its inline
                # fund is validated together with the H cash counterpart by
                # the K event audit; it does not enter H ownership inventory.
                k_postings.append(record)
            elif mode == "H":
                key = _key(ref)
                require(key not in h_candidates, "SOURCE_REF_AMBIGUOUS",
                        "H 资产腿来源身份重复", key)
                h_candidates[key] = (posting, record)
                order.append(key)
            else:
                unknown.append(record)

    names = {}
    owner_names = {"-": "未指定用途"}
    claims = {}
    defaults = None
    transfer_orders = {}
    liability_rules = {}
    budget_owners = None
    responsibilities = None
    credit_income_receipts = []
    h_bundles = []
    if declarations is not None:
        document = load_yaml(declarations)
        required = {"schema", "cutover", "ledger_snapshot", "loader_version", "owners", "claims"}
        unknown_fields = set(document) - required - {"defaults", "transfer_orders", "account_rules", "account_rule_assets", "budget_owners"} if type(document) is dict else set()
        require_yaml(type(document) is dict and required <= set(document) and not unknown_fields,
            "OWNERSHIP_DECLARATION_INVALID", "归属声明字段不完整或含未知字段", declarations,
            (sorted(unknown_fields, key=str)[0],) if unknown_fields else ())
        for field, valid in (
            ("schema", type(document["schema"]) is int and document["schema"] == 6),
            ("cutover", document["cutover"] == cutover.isoformat()),
            ("ledger_snapshot", valid_digest(document["ledger_snapshot"])),
            ("loader_version", document["loader_version"] == fin.loader_version),
            ("claims", type(document["claims"]) is list),
        ):
            require_yaml(valid, "OWNERSHIP_DECLARATION_INVALID", "归属声明的版本或原审快照格式不匹配",
                         declarations, (field,))
        names = _owners(document["owners"], source=declarations)
        owner_names.update({row["id"]: row["name"] for row in document["owners"]})
        ids = set(names.values())
        budget_owners = document.get("budget_owners")
        require(budget_owners is None or type(budget_owners) is dict and all(
                    type(name) is str and type(owner) is str and owner in ids for name, owner in budget_owners.items()),
                "OWNERSHIP_DECLARATION_INVALID", "budget_owners 须为名称到已登记用途的映射", str(declarations))
        liability_rules = validate_rules(document.get("account_rules", {}), ids, declarations,
                                         liability_root=fin.options["name_liabilities"])
        if "defaults" in document:
            defaults = document["defaults"]
            require(type(defaults) is dict and set(defaults) == {
                "income_owner_id", "expense_owner_id"} and
                all(type(owner) is str and owner in ids
                    for owner in defaults.values()),
                "OWNERSHIP_DEFAULT_INVALID", "默认收入／支出用途须引用已登记的命名用途",
                yaml_path_location(declarations, ("defaults",)))
        require(not liability_rules or budget_owners is not None, 'LIABILITY_MODE_REMOVED',
                '自动负债只保留逐笔用途；旧整户用途配置须先迁移', str(declarations))
        rules = document.get("transfer_orders", [])
        require(type(rules) is list and (not rules or defaults is not None),
                "FUND_TRANSFER_ORDER_INVALID", "转出顺序须为列表，并同时配置默认用途以启用库存重放",
                yaml_path_location(declarations, ("transfer_orders",)))
        for index, rule in enumerate(rules):
            source = yaml_path_location(declarations, ("transfer_orders", index))
            require(type(rule) is dict and set(rule) == {
                "account", "commodity", "owner_ids", "reason"},
                "FUND_TRANSFER_ORDER_INVALID", "转出顺序须明确账户、币种、用途顺序和审阅理由", source)
            account, commodity, owners = rule["account"], rule["commodity"], rule["owner_ids"]
            require(type(account) is str and
                    account_audit["declared_semantics"].get(account) == "H" and
                    type(commodity) is str and bool(commodity) and commodity == commodity.strip() and
                    (account, commodity) in known_coordinates and
                    type(owners) is list and bool(owners) and
                    all(type(owner) is str and (owner in ids or owner == "-") for owner in owners) and
                    len(owners) == len(set(owners)) and
                    type(rule["reason"]) is str and bool(rule["reason"].strip()),
                    "FUND_TRANSFER_ORDER_INVALID", "转出顺序须引用已审 H 账户及不同的已登记用途", source)
            coordinate = (account, commodity)
            require(coordinate not in transfer_orders, "FUND_TRANSFER_ORDER_INVALID",
                    "同一账户和币种不能重复声明转出顺序", source)
            transfer_orders[coordinate] = rule
        seen_slices = set()
        for row_index, row in enumerate(document["claims"]):
            require(type(row) is dict and set(row) == {"claim", "reason", "slices"} and
                    type(row["claim"]) is dict and type(row["reason"]) is str and
                    bool(row["reason"].strip()) and type(row["slices"]) is list and row["slices"],
                    "OWNERSHIP_CLAIM_INVALID", "归属 claim 须有完整引用、理由和切片")
            key = _key(row["claim"])
            if key not in h_candidates:
                if future_keys is None:
                    future_keys = {_key(ref) for posting, ref in future_posting_refs(fin, cutover)
                                   if posting.account.split(":", 1)[0] == assets
                                   and account_audit["declared_semantics"].get(posting.account) == "H"}
                if key in future_keys:
                    continue  # No future slices or balances enter a historical report.
            require(key in h_candidates, "OWNERSHIP_REF_INVALID",
                    "归属引用不存在、过期或不是已审 H 资产腿",
                    yaml_path_location(declarations, ("claims", row_index, "claim")))
            require(key not in claims, "OWNERSHIP_DOUBLE_CLAIM", "H 资产腿被重复认领", key)
            posting, record = h_candidates[key]
            require("fund" not in (posting.meta or {}), "OWNERSHIP_DOUBLE_CLAIM",
                    "行内 fund 与外部归属不能同时认领", key)
            original = exact(posting.units.number)
            total = Fraction(0)
            parts = []
            for part in row["slices"]:
                require(type(part) is dict and set(part) == {
                    "id", "owner_id", "role", "signed_units"},
                    "OWNERSHIP_CLAIM_INVALID", "用途切片须含 id/owner_id/role/signed_units", key)
                ident, owner, role = part["id"], part["owner_id"], part["role"]
                require(type(ident) is str and bool(ID.fullmatch(ident)) and
                        ident not in seen_slices,
                        "OWNERSHIP_SLICE_ID_INVALID", "用途切片 ID 无效或重复", key)
                require(type(owner) is str and (owner == "-" or owner in ids) and
                        role in ROLES and (owner != "-" or role == "hold"),
                        "OWNERSHIP_ROLE_INVALID", "用途或意图角色非法", key)
                seen_slices.add(ident)
                amount = _signed_units(part["signed_units"], key)
                require((amount == 0 if original == 0 else amount * original > 0),
                        "OWNERSHIP_UNITS_MISMATCH", "切片与资产腿符号不一致", key)
                total += amount
                parts.append({"id": ident, "owner_id": owner, "role": role,
                              "signed_units": exact_text(amount)})
            require(total == original, "OWNERSHIP_UNITS_MISMATCH",
                    "用途切片没有精确加回 H 资产腿", key)
            claims[key] = {**record, "origin": "external", "slices": parts,
                           "review_reason": row["reason"]}

    compiled_by_key = {}
    for key in order:
        posting, record = h_candidates[key]
        if key in claims:
            compiled_by_key[key] = claims[key]
            continue
        raw = (posting.meta or {}).get("fund")
        if raw is None:
            continue
        source = _source(posting.meta or {})
        parts = parse_inline_amounts(raw, exact(posting.units.number), source,
                                     known_names=set(names), code="OWNERSHIP_UNITS_MISMATCH")
        slice_id = "inline:" + sha256(key.encode()).hexdigest()
        slices = []
        for index, (name, amount) in enumerate(parts):
            owner = "-" if name == "-" else names.get(name)
            require(owner is not None, "OWNER_UNKNOWN", f"行内 fund 用途未登记：{name}", source)
            slices.append({"id": slice_id if len(parts) == 1 else f"{slice_id}:{index}",
                           "owner_id": owner, "role": "hold",
                           "signed_units": exact_text(amount)})
        compiled_by_key[key] = {**record, "origin": "inline", "slices": slices}

    pending_reasons: dict[str, str] = {}
    if defaults is not None:
        initial_pads = initial_pad_postings(fin, cutover)
        # A nonzero cutover balance can serve as an inferred transfer source
        # only when its entire native coordinate has reviewed opening owners.
        nonzero_opening = {
            (row["account"], coordinate["commodity"], coordinate["lot"])
            for row in account_audit["accounts"] if row.get("semantics") == "H"
            for coordinate in row["opening_coordinates"]
            if Fraction(coordinate["signed_units"]) != 0
        }
        running: dict[tuple[str, str, str], dict[str, Fraction]] = {}
        unknown_coordinates = set(nonzero_opening)
        if opening is not None:
            opening_audit = audit_opening(root, cutover, accounts, opening)
            require(opening_audit["ledger_snapshot"] == fin.digest,
                    "OWNERSHIP_OPENING_MISMATCH", "期初归属与资产用途不属于同一账本快照")
            incomplete = {(row["account"], row["commodity"], row["lot"])
                          for row in opening_audit["missing_h_ownership"]}
            reviewed_opening: dict[tuple[str, str, str], dict[str, Fraction]] = defaultdict(dict)
            for row in load_yaml(opening)["h_holdings"]:
                owner = row["owner"]
                require(owner == "-" or owner in ids, "OPENING_OWNER_UNKNOWN",
                        "H 期初 owner 未登记于归属名册", str(row))
                coordinate = (row["account"], row["commodity"], row["lot"])
                reviewed_opening[coordinate][owner] = Fraction(row["units"])
            for coordinate in nonzero_opening - incomplete:
                running[coordinate] = reviewed_opening[coordinate]
                unknown_coordinates.remove(coordinate)
        cash_accounts = {account for account, mode in semantics.items() if mode == "H"}
        if liability_rules:
            responsibilities = audit_rule_accounts(fin, facts, cutover, None, liability_rules, cash_accounts,
                budget_owners=budget_owners)["responsibility_parts"]
        credit_owners = CreditOwners(fin, cutover, liability_rules, defaults, owner_names, responsibilities) if liability_rules else None
        # Unknown opening ownership limits inference only from that coordinate;
        # explicit post-cutover labels on other coordinates remain valid.
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction) or entry.date < cutover:
                continue
            rule_flow = native_rule_flow(fin, entry, liability_rules, cash_accounts) if liability_rules else None
            h_legs = [posting for posting in entry.postings
                      if semantics.get(posting.account) == "H"]
            if not h_legs:
                if credit_owners is not None:
                    credit_owners.advance(entry, rule_flow, {})
                continue
            leg_keys = {_key(facts.for_posting(p).as_dict()): p for p in h_legs}
            coordinates = {key: (p.account, p.units.currency, _lot(p))
                           for key, p in leg_keys.items()}
            missing_keys = [key for key in leg_keys if key not in compiled_by_key]

            def inferred(key: str, owner_parts: list[tuple[str, Fraction]], reason: str) -> None:
                posting, record = h_candidates[key]
                stem = "inferred:" + sha256(key.encode()).hexdigest()
                compiled_by_key[key] = {**record, "origin": "inferred",
                                        "review_reason": reason,
                                        "slices": [
                                            {"id": f"{stem}:{index}", "owner_id": owner,
                                             "role": "hold", "signed_units": exact_text(amount)}
                                            for index, (owner, amount) in enumerate(owner_parts)]}

            roots = [p.account.split(":", 1)[0] for p in entry.postings]
            for key in missing_keys:
                if id(leg_keys[key]) in initial_pads:
                    inferred(key, [(defaults["income_owner_id"], exact(leg_keys[key].units.number))],
                             "首次 pad 初始化余额归配置的默认收入用途；不计收入或正式划拨")
                elif isinstance(facts.for_posting(leg_keys[key]), GeneratedPostingRef):
                    pending_reasons[key] = "PAD_RECONCILIATION_REVIEW"
            native = native_asset_flow(fin, entry, semantics) if len(entry.postings) > 2 else None
            card_purchase = native_card_purchase(fin, entry, semantics)
            receipt = native_payable_receipt(fin, entry, semantics)
            payment = native_group_payment(fin, entry, semantics)
            bundle = native_h_bundle(fin, entry, semantics)
            h_redemption = native_book_redemption(fin, entry, semantics, source_mode="H")
            if rule_flow is not None:
                default = defaults["income_owner_id"] if rule_flow == "receipt" else defaults["expense_owner_id"]
                returning = rule_flow == "receipt" and credit_owners.returning(entry)
                for key in missing_keys:
                    parts = credit_owners.receipt_parts(entry, leg_keys[key]) if returning else cash_owner_parts(
                        fin, entry, rule_flow, liability_rules, default, leg_keys[key], responsibilities)
                    if parts is not None:
                        inferred(key, parts, "退回已证明的预付资金用途" if returning else
                                 "config 负债账户规则的现金用途；借入资金沿默认收入用途")
                    elif returning:
                        pending_reasons[key] = ("LIABILITY_CREDIT_PURPOSE_PENDING" if credit_owners.pure_return(entry) else
                                                "LIABILITY_CREDIT_MIXED_UNSUPPORTED")
            elif receipt is not None or native is not None or card_purchase is not None or payment is not None or len(h_legs) == 1 and len(entry.postings) == 2:
                # Defaults select each asset's owner, not an allocation of
                # payments to individual expense/income counterparts.
                for key in missing_keys:
                    signed = exact(leg_keys[key].units.number)
                    if fin.options["name_income"] in roots:
                        inferred(key, [(defaults["income_owner_id"], signed)], "默认收入用途")
                    elif fin.options["name_expenses"] in roots:
                        inferred(key, [(defaults["expense_owner_id"], signed)], "默认支出用途")
                    elif (payment is not None or len(entry.postings) == 2 and signed < 0 and
                          fin.options["name_liabilities"] in roots and all(
                              p.cost is None and p.price is None and fin.raw_price(p)[0] == "none" and
                              p.units.currency == leg_keys[key].units.currency for p in entry.postings) and
                          sum((exact(p.units.number) for p in entry.postings), Fraction(0)) == 0):
                        inferred(key, [(defaults["expense_owner_id"], signed)], "负债付款使用默认支出用途；责任另审")
            elif (len(entry.postings) == len(h_legs) == 2 and
                  all(p.account.split(":", 1)[0] == assets for p in h_legs)):
                positive = [p for p in h_legs if exact(p.units.number) > 0]
                negative = [p for p in h_legs if exact(p.units.number) < 0]
                if (len(positive) == len(negative) == 1 and
                        positive[0].units.currency == negative[0].units.currency and
                        exact(positive[0].units.number) == -exact(negative[0].units.number)):
                    source_key = _key(facts.for_posting(negative[0]).as_dict())
                    target_key = _key(facts.for_posting(positive[0]).as_dict())
                    if source_key not in compiled_by_key:
                        coordinate = coordinates[source_key]
                        before = running.get(coordinate, {}) if coordinate not in unknown_coordinates else {}
                        if coordinate in unknown_coordinates:
                            pending_reasons[source_key] = 'FUND_SOURCE_OPENING_PENDING'
                        else:
                            parts, reason = _transfer_source_parts(before, -exact(negative[0].units.number), transfer_orders.get(coordinate[:2]))
                            if parts is None:
                                pending_reasons[source_key] = reason
                            else:
                                inferred(source_key, parts, reason)
                    if target_key not in compiled_by_key and source_key in compiled_by_key:
                        source_slices = compiled_by_key[source_key]["slices"]
                        inferred(target_key, [(part["owner_id"], -Fraction(part["signed_units"]))
                                              for part in source_slices], "同用途资产搬移")
                    elif target_key not in compiled_by_key:
                        pending_reasons[target_key] = "FUND_SOURCE_UNRESOLVED"

            if bundle is not None and not bundle.expense_total and sum((exact(p.units.number) for p in bundle.assets), Fraction(0))==0:
                # Reserve declared outflows first. Inherited source slices must
                # not reuse funds already consumed by another leg in this group.
                reserved = {coordinate: dict(running.get(coordinate, {})) for coordinate in coordinates.values()}
                for key,p in leg_keys.items():
                    if p.units.number < 0 and key in compiled_by_key:
                        for part in compiled_by_key[key]['slices']:
                            owner = part['owner_id']; bucket = reserved[coordinates[key]]
                            bucket[owner] = bucket.get(owner, Fraction(0)) + Fraction(part['signed_units'])
                for key,p in leg_keys.items():
                    if p.units.number >= 0 or key in compiled_by_key:
                        continue
                    coordinate = coordinates[key]
                    if coordinate in unknown_coordinates:
                        pending_reasons[key] = 'FUND_SOURCE_OPENING_PENDING'
                        continue
                    parts, reason = _transfer_source_parts(reserved[coordinate], -exact(p.units.number), transfer_orders.get(coordinate[:2]))
                    if parts is None:
                        pending_reasons[key] = reason
                    else:
                        inferred(key, parts, reason)
                        for owner,amount in parts:
                            reserved[coordinate][owner] = reserved[coordinate].get(owner, Fraction(0)) + amount
                targets = [key for key,p in leg_keys.items() if p.units.number > 0 and key not in compiled_by_key]
                if all(key in compiled_by_key for key,p in leg_keys.items() if p.units.number < 0):
                    remainder = defaultdict(Fraction)
                    for key in leg_keys:
                        for part in compiled_by_key.get(key, {}).get('slices', []):
                            remainder[part['owner_id']] -= Fraction(part['signed_units'])
                    remainder = {owner:amount for owner,amount in remainder.items() if amount}
                    if remainder and all(amount>0 for amount in remainder.values()) and sum(remainder.values(), Fraction(0)) == sum((exact(leg_keys[key].units.number) for key in targets), Fraction(0)):
                        if len(targets)==1:
                            inferred(targets[0], list(remainder.items()), '组合转账唯一未认领收款腿继承已审来源用途')
                        elif len(remainder)==1:
                            owner = next(iter(remainder))
                            for key in targets:
                                inferred(key, [(owner,exact(leg_keys[key].units.number))], '组合转账多户收款继承唯一来源用途')
                for key in targets:
                    if key not in compiled_by_key:
                        pending_reasons[key] = 'FUND_SOURCE_UNRESOLVED'
            if bundle is not None and bundle.expense_total:
                # If declared H movement already cancels by purpose, remaining
                # one-direction missing legs are ordinary default expenses or
                # refunds. Never default a missing transfer destination.
                missing = [key for key in leg_keys if key not in compiled_by_key]
                declared = defaultdict(Fraction)
                for key in leg_keys:
                    for part in compiled_by_key.get(key, {}).get('slices', []):
                        declared[part['owner_id']] += Fraction(part['signed_units'])
                if (missing and not any(declared.values()) and
                        all(exact(leg_keys[key].units.number) * bundle.expense_total < 0 for key in missing) and
                        sum((exact(leg_keys[key].units.number) for key in missing), Fraction(0)) == -bundle.expense_total):
                    for key in missing:
                        inferred(key, [(defaults['expense_owner_id'], exact(leg_keys[key].units.number))],
                                 '同用途转账已逐用途守恒；剩余消费／退款沿默认支出用途')
            bundle_event = compile_h_bundle(bundle, entry, facts, compiled_by_key, account_audit['report_currency']) if bundle else None
            if bundle_event is not None:
                h_bundles.append(bundle_event)

            if credit_owners is not None:
                credit_issue = credit_owners.advance(entry, rule_flow, {id(posting): compiled_by_key[key]["slices"]
                    for key, posting in leg_keys.items() if key in compiled_by_key})
                if credit_issue is not None:
                    for key in leg_keys:
                        compiled_by_key.pop(key, None)
                        pending_reasons[key] = credit_issue
                elif credit_owners.last_income_receipt is not None:
                    credit_income_receipts.append({"protocol": "native_credit_income_receipt_v1",
                        "transaction_ref": facts.for_transaction(entry).as_dict(),
                        "asset_ref": facts.for_posting(receipt.cash).as_dict(),
                        "liability_ref": facts.for_posting(receipt.liability).as_dict(),
                        "income_refs": [facts.for_posting(p).as_dict() for p in receipt.incomes],
                        "commodity": receipt.cash.units.currency,
                        "receipt_native": exact_text(receipt.received), "credit_native": exact_text(receipt.payable),
                        "income_native": exact_text(receipt.income),
                        **{kind: [{"owner_id": owner, "units": exact_text(units)} for owner, units in sorted(parts.items())]
                           for kind, parts in credit_owners.last_income_receipt.items()}})
            if any(key not in compiled_by_key for key in leg_keys):
                unknown_coordinates.update(coordinates.values())
                continue
            changes: dict[tuple[str, str, str], dict[str, Fraction]] = defaultdict(
                lambda: defaultdict(Fraction))
            for key in leg_keys:
                for part in compiled_by_key[key]["slices"]:
                    changes[coordinates[key]][part["owner_id"]] += Fraction(part["signed_units"])
            if bundle_event is not None and bundle_event['status'] == 'CERTIFIED' and bundle.expense_total:
                # Prove that all same-purpose transfers can be funded before
                # consumption. Fees may cause a deficit; transfers may not.
                for owner in {owner for parts in changes.values() for owner in parts}:
                    movements = {coordinate: parts.get(owner, Fraction(0)) for coordinate, parts in changes.items()}
                    sources = {coordinate: -delta for coordinate, delta in movements.items() if delta < 0}
                    if any(coordinate in unknown_coordinates for coordinate in sources):
                        continue
                    charge = Fraction(bundle_event['expense_by_owner'].get(owner, '0'))
                    needed = max(Fraction(0), sum(sources.values(), Fraction(0)) - max(charge, Fraction(0)))
                    available = sum((min(amount, max(Fraction(0), running.get(coordinate, {}).get(owner, Fraction(0))))
                                     for coordinate, amount in sources.items()), Fraction(0))
                    if needed > available:
                        lines = ['组合转账不能透支交易前用途余额', f'资金用途：{owner_names[owner]}',
                                 f'本次主动划拨：{amount_text(needed)} {bundle.currency}',
                                 f'来源合计可划出：{amount_text(available)} {bundle.currency}',
                                 f'本次缺口：{amount_text(needed-available)} {bundle.currency}',
                                 '转出账户的全部交易前用途余额：']
                        for coordinate in sources:
                            lines.append(f'  {coordinate[0]}：')
                            for ident, units in sorted(running.get(coordinate, {}).items(), key=lambda row: owner_names[row[0]]):
                                lines.append(f'    {owner_names[ident]}：{amount_text(units)} {bundle.currency}（可划出 {amount_text(max(units, Fraction(0)))} {bundle.currency}）')
                        source = next((p.meta for key,p in leg_keys.items() if coordinates[key] in sources and exact(p.units.number)<0), entry.meta)
                        raise BudgetError('H_OWNER_NEGATIVE', '\n'.join(lines), _source(source),
                            {'account': next(coordinate[0] for coordinate in sources),
                             'transaction_ref': facts.for_transaction(entry).as_dict(),
                             'ledger_snapshot': fin.digest})
            for coordinate, deltas in changes.items():
                if coordinate in unknown_coordinates:
                    continue
                before = running.setdefault(coordinate, {})
                after = {owner: before.get(owner, Fraction(0)) + amount
                         for owner, amount in deltas.items()}
                after.update({owner: amount for owner, amount in before.items() if owner not in after})
                # K subscriptions also spend real H cash voluntarily; report
                # their source deficit here rather than a raw holdings ref.
                if (bundle_event is not None and bundle_event['status'] == 'CERTIFIED' and not bundle.expense_total or
                        len(entry.postings) == 2 and all(p.account.split(":", 1)[0] == assets for p in entry.postings) or
                        h_redemption is not None and coordinate == (h_redemption.book.account,
                            h_redemption.book.units.currency, _lot(h_redemption.book))):
                    outgoing = next((posting for key, posting in leg_keys.items()
                                     if coordinates[key] == coordinate and posting.units.number < 0),
                                    next(posting for key, posting in leg_keys.items()
                                         if coordinates[key] == coordinate))
                    _require_transfer_balance(coordinate, before, after, owner_names,
                                              _source(outgoing.meta or entry.meta),
                                              {'account': coordinate[0],
                                               'transaction_ref': facts.for_transaction(entry).as_dict(),
                                               'ledger_snapshot': fin.digest})
                running[coordinate] = after

    if liability_rules:
        # Verify the actual compiled cash vector, including existing external
        # approvals; raw tags/defaults alone would reject valid reviewed funds.
        cash_accounts = {account for account, mode in semantics.items() if mode == "H"}
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction) or entry.date < cutover or not any(
                    p.account in liability_rules and liability_rules[p.account]["role"] == "receivable" for p in entry.postings):
                continue
            if native_rule_flow(fin, entry, liability_rules, cash_accounts) is None:
                continue
            cash_keys = {_key(facts.for_posting(p).as_dict()): p for p in entry.postings if p.account in cash_accounts}
            owned = {id(p): compiled_by_key[key]["slices"] for key, p in cash_keys.items() if key in compiled_by_key}
            if receivable_cash_matches(fin, entry, liability_rules, owned, responsibilities) is False:
                for key in cash_keys:
                    compiled_by_key.pop(key, None)
                    pending_reasons[key] = "LIABILITY_RULE_PURPOSE_MISMATCH"
    compiled = [compiled_by_key[key] for key in order if key in compiled_by_key]
    missing = [{**h_candidates[key][1],
                **({"reason_code": pending_reasons[key]} if key in pending_reasons else {})}
               for key in order if key not in compiled_by_key]
    return {"mode": "v6_ownership_audit_only", "cutover": cutover.isoformat(),
            **({"credit_income_receipts": credit_income_receipts} if credit_income_receipts else {}),
            **({"h_bundles": h_bundles} if h_bundles else {}),
            "ledger_snapshot": fin.digest,
            "review_state": "complete" if not missing and not unknown else "pending",
            "h_postings": len(order), "claimed_h_postings": len(compiled),
            "missing_h_ownership": missing, "unknown_account_semantics": unknown,
            "k_book_flow_postings": k_postings,
            "pending_payment_scope": account_audit["pending_payment_scope"],
            "ownership": compiled,
            "note": "归属意图切片不证明逐事件库存守恒、K 申赎、净划拨或首页现金"}

"""Booked payments from the default spending purpose, independent of expenses/G."""

from calendar import monthrange
from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import booked_through, frozen_financial, load_financial
from ..yamlstrict import load_yaml, yaml_key_location
from .accounts import audit_accounts
from .classification import _key
from .facts import FactIndex, frozen_fact_indices
from .obligations import audit_simple_obligations
from .native_flows import native_asset_flow, native_book_redemption, native_payable_receipt
from .ownership import audit_ownership
from .purpose_pools import audit_purpose_pools
from .review_diagnostics import posting_sources, review_item
from .targets import _month


FIELDS = ("expenses_paid", "expense_refunds", "debt_payments", "advances_paid", "advance_reimbursements")
KINDS = {"payment": "debt_payments", "advance_outflow": "advances_paid",
         "advance_reimbursement": "advance_reimbursements"}


def _totals(amounts):
    result = {key: exact_text(amounts[key]) for key in FIELDS}
    if amounts["credit_returns"]:
        result["credit_returns"] = exact_text(amounts["credit_returns"])
    outflow = sum((amounts[key] for key in ("expenses_paid", "debt_payments", "advances_paid")), Fraction(0))
    inflow = amounts["expense_refunds"] + amounts["advance_reimbursements"] + amounts["credit_returns"]
    return {**result, "gross_outflow": exact_text(outflow), "gross_inflow": exact_text(inflow),
            "net_outflow": exact_text(outflow - inflow)}


def audit_default_outflows(root: Path, cutover: date, month: str, accounts: Path,
                           ownership: Path, opening: Path | None = None,
                           obligations: Path | None = None,
                           purpose_pools: Path | None = None) -> dict:
    """Count source-bound default-purpose cash movements by original commodity.

    Joint cash/card purchases count only their actual asset payments here;
    later repayments use the actual paying purpose.
    Transfers and FX never create payments in this projection. Unsupported
    mixed flow cannot be replaced by a guessed share or an incomplete zero.
    """
    _month(month)
    first = date.fromisoformat(month + "-01")
    require(first >= cutover, "MONTH_INVALID", "资金收付月份不得早于 cutover", month)
    last = date(first.year, first.month, monthrange(first.year, first.month)[1])
    with booked_through(last), frozen_financial(root), frozen_fact_indices():
        return _audit(root, cutover, first, last, accounts, ownership, opening, obligations, purpose_pools)


def _audit(root, cutover, first, last, accounts, ownership, opening, obligations, purpose_pools):
    fin = load_financial(root)
    facts = FactIndex(fin)
    account_audit = audit_accounts(root, cutover, accounts)
    owned = audit_ownership(root, cutover, accounts, ownership, opening)
    document = load_yaml(ownership)
    owner = document.get("defaults", {}).get("expense_owner_id")
    result = {"mode": "v6_default_spending_purpose_cash_movements", "month": first.strftime("%Y-%m"),
              "as_of": last.isoformat(), "ledger_snapshot": fin.digest, "owner_id": owner,
              "basis": "booked_native_asset_units", "events": [], "review_items": [],
              "excluded_pools": [],
              "reason_codes": [], "totals": None,
              "note": "按默认支出用途的实记资产数量分币种汇总；消费分类与用途净划拨另算。消费现金部分当次计入，刷卡部分在还款时计入；资产搬移、购汇和申赎不计入。"}
    if owner is None:
        return {**result, "status": "NOT_CONFIGURED"}
    nondefault_pool_accounts = set()
    pool_accounts = set()
    if purpose_pools is not None:
        pools = audit_purpose_pools(root, cutover, last, purpose_pools)
        require(pools["ledger_snapshot"] == fin.digest and
                pools["report_currency"] == account_audit["report_currency"],
                "PURPOSE_POOL_MISMATCH", "资金池与资金收付汇总的来源或报告货币不一致")
        registered = {row["id"] for row in document["owners"]}
        for pool in pools["pools"]:
            pool_accounts.update(pool["accounts"])
            members = {pool["protected_owner_id"], pool["remainder_owner_id"]}
            if not members <= registered:
                item = review_item("PURPOSE_POOL_PENDING", yaml_key_location(purpose_pools, "pools"),
                                   detail="资金池用途 ID 未完整登记，不能确认默认用途是否参与。")
                return {**result, "status": "NEEDS_REVIEW", "review_items": [item],
                        "reason_codes": [item["reason_code"]]}
            if owner not in members:
                nondefault_pool_accounts.update(pool["accounts"])
                result["excluded_pools"].append({"id": pool["id"], "accounts": pool["accounts"],
                                                 "owner_ids": sorted(members)})
    modes = {row["account"]: row.get("semantics") for row in account_audit["accounts"]}
    claims = {_key(row["ref"]): row for row in owned["ownership"]}
    bundles = {_key(row['transaction_ref']): row for row in owned.get('h_bundles', []) if not set(row['asset_accounts']) & pool_accounts}
    mixed_receipts = {_key(row["asset_ref"]): row for row in owned.get("credit_income_receipts", [])}
    # Even without a declaration file, recognize supported shapes as data
    # awaiting review rather than labelling their cash share unsupported.
    debt = audit_simple_obligations(root, cutover, obligations, last)
    configured = defaultdict(list)
    for row in debt["events"]:
        if row.get("claim_origin") == "account_rule":
            configured[_key(row["transaction_ref"])].append(row)
    payments = defaultdict(list)
    for row in debt["events"]:
        if row["counterpart_ref"] is not None:
            payments[_key(row["counterpart_ref"])].append(row)
    purchases = defaultdict(list)
    for row in debt["events"]:
        if row["shape"] == "consumer_purchase":
            for ref in row.get("asset_refs", []):
                purchases[_key(ref)].append(row)
    sources = posting_sources(fin)
    by_currency = defaultdict(lambda: defaultdict(Fraction))
    by_account = defaultdict(lambda: defaultdict(Fraction))
    by_currency[account_audit["report_currency"]]  # Explicit zero when there are no payments.

    def pending(code, ref, account, detail=None):
        result["review_items"].append(review_item(
            code, sources[_key(ref)], detail=detail, ref=ref, account=account))

    expense_root, liability_root = fin.options["name_expenses"], fin.options["name_liabilities"]
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or not first <= entry.date <= last:
            continue
        expense_legs = [p for p in entry.postings if p.account.split(":", 1)[0] == expense_root]
        liability_legs = [p for p in entry.postings if p.account.split(":", 1)[0] == liability_root]
        if not expense_legs and not liability_legs:
            continue  # Includes all virtual allocations, transfers, K and FX.
        bundle = bundles.get(facts.for_transaction(entry).as_key())
        if bundle is not None:
            if bundle['status'] != 'CERTIFIED':
                pending(bundle['reason_code'], bundle['ref'], bundle['account'])
                continue
            for payment in bundle['cash_expenses']:
                if payment['owner_id'] != owner:
                    continue
                amount = Fraction(payment['signed_expense_units'])
                kind = 'expenses_paid' if amount > 0 else 'expense_refunds'
                members = payment['accounts']
                key = members[0] if len(members) == 1 else tuple(members)
                by_currency[bundle['commodity']][kind] += abs(amount)
                by_account[(bundle['commodity'], key)][kind] += abs(amount)
                ref = facts.for_posting(next(p for p in entry.postings if p.account == members[0] and
                    any(part['owner_id']==owner and Fraction(part['signed_units'])*amount < 0
                        for part in claims.get(facts.for_posting(p).as_key(), {}).get('slices', [])))).as_dict()
                result['events'].append({'date': entry.date.isoformat(), 'narration': entry.narration,
                    'account': members[0] if len(members)==1 else None, 'commodity': bundle['commodity'], 'owner_id': owner,
                    'kind': kind, 'amount': exact_text(abs(amount)), 'signed_owner_units': exact_text(-amount),
                    'asset_ref': ref, 'asset_refs': bundle['asset_refs'], 'counterpart_refs': bundle['expense_refs'],
                    'source': sources[_key(ref)], 'proof': 'native_h_bundle_v1',
                    **({'source_accounts': members, 'basis': 'joint_source_no_account_allocation'} if len(members)>1 else {})})
            continue
        native = native_asset_flow(fin, entry, modes)
        receipt = native_payable_receipt(fin, entry, modes)
        if receipt is not None and facts.for_posting(receipt.cash).as_key() not in mixed_receipts:
            continue  # A receipt is not a default-purpose payment or expense refund.
        if native_book_redemption(fin, entry, modes) is not None:
            continue  # Expenses netted from a K receipt are not another H cash debit.
        for posting in entry.postings:
            if posting.account.split(":", 1)[0] == fin.options["name_assets"] and modes.get(posting.account) is None:
                pending("ACCOUNT_UNREVIEWED", facts.for_posting(posting).as_dict(),
                        posting.account, "先确认该资产账户是 H 真实库存还是 K 累计申赎。")
        assets = [p for p in entry.postings if modes.get(p.account) == "H"]
        for posting in assets:
            ref = facts.for_posting(posting).as_dict()
            claim = claims.get(_key(ref))
            if posting.account in nondefault_pool_accounts:
                if claim is not None and claim["origin"] in {"inline", "external"} and any(
                        part["owner_id"] == owner and Fraction(part["signed_units"]) != 0
                        for part in claim["slices"]):
                    pending("PURPOSE_POOL_PENDING", ref, posting.account,
                            "该分录显式使用默认支出用途，但资金池用途集合未包含它；先审定冲突。")
                continue  # Approved pool owners exclude this default; no per-lot guesses.
            if claim is None:
                pending("OWNERSHIP_PENDING", ref, posting.account)
                continue
            units = sum((Fraction(part["signed_units"]) for part in claim["slices"]
                         if part["owner_id"] == owner), Fraction(0))
            if units == 0:
                continue
            # A net income receipt with fees has no observed asset payment.
            # Its fee classification does not manufacture a cash debit.
            if (len(assets) == 1 and exact(posting.units.number) > 0 and expense_legs and not liability_legs and
                    any(p.account.split(":", 1)[0] == fin.options["name_income"] for p in entry.postings) and all(
                    exact(p.units.number) >= 0 for p in expense_legs)):
                continue
            kind, event, components = None, None, None
            ruled = configured.get(facts.for_transaction(entry).as_key())
            if ruled:
                if any(row["status"] != "CERTIFIED" for row in ruled):
                    reasons = [code for row in ruled for code in row["reason_codes"]]
                    pending(next(iter(reasons), "OBLIGATION_EVENT_UNSUPPORTED"), ref, posting.account)
                    continue
                event = ruled[0]
                moving = [row for row in ruled if Fraction(row["signed_units"]) != 0]
                kinds = {row["kind"] for row in moving or ruled}
                counterparts = [row["liability_ref"] for row in ruled]
                if kinds <= {"payable_receipt"}:
                    continue
                if kinds == {"consumer_purchase"}:
                    kind = "expenses_paid"
                elif kinds == {"consumer_refund"}:
                    kind = "expense_refunds"
                elif kinds == {"advance_reimbursement"}:
                    kind = "advance_reimbursements"
                elif kinds == {"credit_return"}:
                    kind = "credit_returns"
                    if _key(ref) in mixed_receipts:
                        components = {kind: sum((Fraction(part["units"]) for part in mixed_receipts[_key(ref)][kind]
                                                 if part["owner_id"] == owner), Fraction(0))}
                elif kinds <= {"payment", "advance_outflow"}:
                    fees = sum((exact(p.units.number) for p in expense_legs), Fraction(0))
                    if len(kinds) == 1 and not fees:
                        kind = KINDS[next(iter(kinds))]
                    elif len(assets) == 1:
                        components = defaultdict(Fraction)
                        advances = defaultdict(Fraction)
                        principal = Fraction(0)
                        for row in ruled:
                            if row["kind"] == "advance_outflow":
                                for part in row.get("responsibility_slices", [{"owner_id": row["owner_id"], "signed_units": row["signed_units"]}]):
                                    advances[part["owner_id"]] += Fraction(part["signed_units"])
                            else:
                                principal += Fraction(row["signed_units"])
                        available = abs(units) - advances[owner]
                        remaining = abs(exact(posting.units.number)) - sum(advances.values(), Fraction(0))
                        if available < 0 or available > remaining or fees and principal and 0 < available < remaining:
                            pending("DEFAULT_CASH_SPLIT_AMBIGUOUS", ref, posting.account,
                                    "本金／利息的多用途现金分摊无法唯一确定；应收部分已按明确责任用途单独核对。")
                            continue
                        components["advances_paid"] = advances[owner]
                        components["expenses_paid"] = min(available, fees) if not principal or available == remaining else Fraction(0)
                        components["debt_payments"] = available - components["expenses_paid"]
                    else:
                        pending("DEFAULT_CASH_SPLIT_AMBIGUOUS" if len(assets) == 1 else "DEFAULT_OUTFLOW_EVENT_UNSUPPORTED",
                                ref, posting.account,
                                "同笔本金／利息使用多用途资金，尚不能确定各用途承担的部分。" if len(assets) == 1 else
                                "同笔多个现金账户支付本金／利息的联合分类汇总尚未实现；不能用逐笔审阅代替程序支持。")
                        continue
                else:
                    pending("DEFAULT_OUTFLOW_EVENT_UNSUPPORTED", ref, posting.account)
                    continue
            elif native is not None and native.kind == "expense":
                kind = "expenses_paid" if units < 0 else "expense_refunds"
                counterparts = [facts.for_posting(p).as_dict() for p in native.counterparts]
            elif (len(assets) == 1 and len(entry.postings) == 1 + len(expense_legs) and expense_legs
                    and all(p.cost is None and p.units.currency == posting.units.currency
                            for p in entry.postings)
                    and sum((exact(p.units.number) for p in entry.postings), Fraction(0)) == 0):
                kind = "expenses_paid" if units < 0 else "expense_refunds"
                counterparts = [facts.for_posting(p).as_dict() for p in expense_legs]
            elif _key(ref) in purchases:
                members = purchases[_key(ref)]
                event = members[0]
                if any(row["status"] != "CERTIFIED" for row in members):
                    pending("OBLIGATION_UNCLAIMED", ref, posting.account)
                    continue
                kind = "expenses_paid"
                counterparts = event["expense_refs"]
                if len(members) > 1:
                    event = {**event, "obligation_ids": [row["obligation_id"] for row in members]}
            elif liability_legs and (len(entry.postings) == 2 or any(
                    row.get("proof") == "native_group_payment_v1" for row in payments.get(_key(ref), []))):
                members = payments.get(_key(ref), [])
                event = members[0] if members else None
                if not members or any(row["status"] != "CERTIFIED" or row["kind"] not in KINDS for row in members):
                    # Certified receipts add cash and a separate obligation;
                    # they are neither personal income nor expense refunds.
                    if len(members) == 1 and event["status"] == "CERTIFIED" and event["kind"] == "payable_receipt":
                        continue
                    code = ("OBLIGATION_EVENT_UNSUPPORTED" if any(row["status"] == "UNSUPPORTED" for row in members)
                            else "OBLIGATION_UNCLAIMED")
                    pending(code, ref, posting.account)
                    continue
                kind = KINDS[event["kind"]]
                counterparts = [row["liability_ref"] for row in members]
                if len(members) > 1:
                    event = {**event, "obligation_ids": [row["obligation_id"] for row in members]}
            else:
                pending("DEFAULT_OUTFLOW_EVENT_UNSUPPORTED", ref, posting.account)
                continue
            for component, amount in (components or {kind: abs(units)}).items():
                if not amount:
                    continue
                by_currency[posting.units.currency][component] += amount
                by_account[(posting.units.currency, posting.account)][component] += amount
                result["events"].append({"date": entry.date.isoformat(), "narration": entry.narration,
                    "account": posting.account, "commodity": posting.units.currency, "owner_id": owner,
                    "kind": component, "amount": exact_text(amount),
                    "signed_owner_units": exact_text(-amount if units < 0 else amount),
                    "asset_ref": ref, "counterpart_refs": counterparts, "source": sources[_key(ref)],
                    **({"obligation_ids": event["obligation_ids"]} if event and "obligation_ids" in event else
                       {"obligation_id": event["obligation_id"], "clearing_id": event["clearing_id"]}
                       if event else {})})
    if result["review_items"]:
        return {**result, "status": "NEEDS_REVIEW", "events": [],
                "reason_codes": sorted({item["reason_code"] for item in result["review_items"]})}
    totals = [{"commodity": commodity, **_totals(amounts), "accounts": [
        {**({'account': None, 'source_accounts': list(account), 'basis': 'joint_source_no_account_allocation'}
            if isinstance(account, tuple) else {'account': account}), **_totals(parts)}
        for (currency, account), parts in sorted(by_account.items(), key=lambda row: str(row[0]))
        if currency == commodity]} for commodity, amounts in sorted(by_currency.items())]
    return {**result, "status": "CERTIFIED", "totals": totals}

"""Exact native cash flows and card purchases, without pairwise guesses."""

from dataclasses import dataclass
from fractions import Fraction

from beancount.core import data

from ..exact import exact


@dataclass(frozen=True)
class NativeFlow:
    kind: str
    assets: tuple
    counterparts: tuple
    asset_total: Fraction


@dataclass(frozen=True)
class NativeCardPurchase:
    liabilities: tuple
    assets: tuple
    expenses: tuple


def native_group_payment(fin, entry, modes: dict | None = None) -> NativeFlow | None:
    """One observed cash payment exactly clears several positive debt legs."""
    if not isinstance(entry, data.Transaction):
        return None
    cash = tuple(p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_assets"])
    debts = tuple(p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_liabilities"])
    if (len(cash) != 1 or len(debts) < 2 or len(entry.postings) != 1 + len(debts) or
            modes is not None and modes.get(cash[0].account) != "H"):
        return None
    asset = cash[0]
    if (exact(asset.units.number) >= 0 or any(exact(p.units.number) <= 0 for p in debts) or
            any(p.cost is not None or p.price is not None or fin.raw_price(p)[0] != "none" or
                p.units.currency != asset.units.currency for p in entry.postings) or
            sum((exact(p.units.number) for p in entry.postings), Fraction(0)) != 0):
        return None
    return NativeFlow("payment", cash, debts, exact(asset.units.number))


@dataclass(frozen=True)
class NativeBookRedemption:
    book: data.Posting
    cash: data.Posting
    income: data.Posting | None
    principal: Fraction
    receipt: Fraction
    gain: Fraction
    fees: tuple
    fee_total: Fraction


def zero_native_record(fin, entry, currency: str) -> bool:
    """Prove only the absence of recorded monetary movement, never share value."""
    return (isinstance(entry, data.Transaction) and bool(entry.postings) and all(
        exact(p.units.number) == 0 and p.units.currency == currency and
        p.cost is None and p.price is None and fin.raw_price(p)[0] == "none"
        for p in entry.postings))


def native_book_redemption(fin, entry, modes: dict | None = None, *, source_mode="K") -> NativeBookRedemption | None:
    """Prove receipt = book-flow principal + signed income - expenses.

    This is a complete native equation, not a claim about fund shares,
    cost lots or market value. Callers explicitly select K book flow or H
    native inventory for the source; the benefit projection uses no modes.
    """
    if not isinstance(entry, data.Transaction) or len(entry.postings) < 3:
        return None
    assets = [p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_assets"]]
    incomes = [p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_income"]]
    fees = tuple(p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_expenses"])
    if (len(assets) != 2 or len(incomes) > 1 or not (incomes or fees) or
            len(entry.postings) != 2 + len(incomes) + len(fees) or
            any(exact(p.units.number) < 0 for p in fees)):
        return None
    # H cash out + cash in + Expenses alone can also be a purchase with
    # change. It is not evidence of a redemption; keep that separate protocol.
    if source_mode == "H" and not incomes:
        return None
    outgoing = [p for p in assets if exact(p.units.number) < 0]
    incoming = [p for p in assets if exact(p.units.number) > 0]
    if len(outgoing) != 1 or len(incoming) != 1:
        return None
    book, cash = outgoing[0], incoming[0]
    if modes is not None and (modes.get(book.account) != source_mode or modes.get(cash.account) != "H"):
        return None
    if source_mode == "H" and book.account == cash.account:
        return None
    if any(p.cost is not None or p.price is not None or fin.raw_price(p)[0] != "none" or
           p.units.currency != cash.units.currency for p in entry.postings):
        return None
    income = incomes[0] if incomes else None
    principal, receipt = -exact(book.units.number), exact(cash.units.number)
    gain = -exact(income.units.number) if income is not None else Fraction(0)
    fee_total = sum((exact(p.units.number) for p in fees), Fraction(0))
    if receipt != principal + gain - fee_total:
        return None
    return NativeBookRedemption(book, cash, income, principal, receipt, gain, fees, fee_total)


def native_card_purchase(fin, entry, modes: dict | None = None) -> NativeCardPurchase | None:
    """Prove expenses = cash paid + all new card debts in native units.

    The complete group is evidence; it assigns no expense to a particular
    payer and says nothing about the debt's reviewed responsibility owner.
    Refunds, change, lots and FX need their own protocols.
    """
    if not isinstance(entry, data.Transaction):
        return None
    roots = {fin.options['name_assets'], fin.options['name_expenses'], fin.options['name_liabilities']}
    if any(p.account.split(':', 1)[0] not in roots for p in entry.postings):
        return None
    assets = tuple(p for p in entry.postings if p.account.split(':', 1)[0] == fin.options['name_assets'])
    expenses = tuple(p for p in entry.postings if p.account.split(':', 1)[0] == fin.options['name_expenses'])
    cards = [p for p in entry.postings if p.account.split(':', 1)[0] == fin.options['name_liabilities']]
    if not cards or not expenses or modes is not None and any(modes.get(p.account) != 'H' for p in assets):
        return None
    card = cards[0]
    if any(p.cost is not None or p.price is not None or fin.raw_price(p)[0] != 'none' or
           p.units.currency != card.units.currency for p in entry.postings):
        return None
    if (any(exact(p.units.number) >= 0 for p in cards) or any(exact(p.units.number) >= 0 for p in assets) or
            any(exact(p.units.number) <= 0 for p in expenses) or
            sum((exact(p.units.number) for p in entry.postings), Fraction(0)) != 0):
        return None
    return NativeCardPurchase(tuple(cards), assets, expenses)


def native_asset_flow(fin, entry, modes: dict | None = None) -> NativeFlow | None:
    """Recognize a whole, one-direction, same-commodity booked flow.

    Expense and Income may not mix, nor may assets move in both directions.
    No costs, prices, liabilities, equity or unrecognized accounts are allowed.
    This proves only the joint native equation, never a cash/category pairing
    or the payment capability of an asset. Callers auditing H pass its modes.
    """
    if not isinstance(entry, data.Transaction):
        return None
    assets = tuple(p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_assets"])
    if not assets or modes is not None and any(modes.get(p.account) != "H" for p in assets):
        return None
    remaining = tuple(p for p in entry.postings if p.account.split(":", 1)[0] != fin.options["name_assets"])
    roots = {p.account.split(":", 1)[0] for p in remaining}
    if roots == {fin.options["name_expenses"]}:
        kind = "expense"
    elif roots == {fin.options["name_income"]}:
        kind = "income"
    else:
        return None
    currency = assets[0].units.currency
    if any(p.cost is not None or p.price is not None or fin.raw_price(p)[0] != "none" or
           p.units.currency != currency for p in entry.postings):
        return None
    total = sum((exact(p.units.number) for p in assets), Fraction(0))
    if sum((exact(p.units.number) for p in remaining), total) != 0:
        return None
    if total == 0:
        if any(exact(p.units.number) != 0 for p in entry.postings):
            return None
    elif (any(exact(p.units.number) * total < 0 for p in assets) or
          any(exact(p.units.number) * total > 0 for p in remaining)):
        return None
    return NativeFlow(kind, assets, remaining, total)


@dataclass(frozen=True)
class NativePayableReceipt:
    cash: data.Posting
    liability: data.Posting
    incomes: tuple
    received: Fraction
    income: Fraction
    payable: Fraction


def native_payable_receipt(fin, entry, modes: dict | None = None) -> NativePayableReceipt | None:
    """Prove cash = income + negative liability leg, counting only Income.

    Liability replay decides whether that leg creates debt or returns credit.
    """
    if not isinstance(entry, data.Transaction):
        return None
    grouped = {kind: tuple(p for p in entry.postings
                          if p.account.split(":", 1)[0] == fin.options["name_" + kind])
               for kind in ("assets", "liabilities", "income")}
    cash, liabilities, incomes = (grouped[kind] for kind in ("assets", "liabilities", "income"))
    if (len(cash) != 1 or len(liabilities) != 1 or not incomes or
            len(entry.postings) != 2 + len(incomes) or
            modes is not None and modes.get(cash[0].account) != "H"):
        return None
    asset, liability = cash[0], liabilities[0]
    if any(p.cost is not None or p.price is not None or fin.raw_price(p)[0] != "none" or
           p.units.currency != asset.units.currency for p in entry.postings):
        return None
    received, payable = exact(asset.units.number), -exact(liability.units.number)
    income = -sum((exact(p.units.number) for p in incomes), Fraction(0))
    if (received <= 0 or payable <= 0 or income <= 0 or
            any(exact(p.units.number) > 0 for p in incomes) or received != income + payable):
        return None
    return NativePayableReceipt(asset, liability, incomes, received, income, payable)


@dataclass(frozen=True)
class NativeHBundle:
    """An arbitrary-size H transfer, optionally accompanied by native expenses."""
    assets: tuple
    expenses: tuple
    currency: str
    expense_total: Fraction


def native_h_bundle(fin, entry, modes) -> NativeHBundle | None:
    # Existing one-direction cash and two-leg transfer protocols stay intact.
    # This adapter covers their composition, independent of posting count/order.
    if not isinstance(entry, data.Transaction) or len(entry.postings) <= 2:
        return None
    assets = tuple(p for p in entry.postings if p.account.split(':', 1)[0] == fin.options['name_assets'])
    expenses = tuple(p for p in entry.postings if p.account.split(':', 1)[0] == fin.options['name_expenses'])
    if (not assets or len(assets) + len(expenses) != len(entry.postings) or
            any(modes.get(p.account) != 'H' for p in assets) or
            not any(exact(p.units.number) > 0 for p in assets) or
            not any(exact(p.units.number) < 0 for p in assets)):
        return None
    currency = assets[0].units.currency
    if any(p.units.currency != currency or p.cost is not None or p.price is not None or
           fin.raw_price(p)[0] != 'none' for p in entry.postings):
        return None
    if any(exact(p.units.number) > 0 for p in expenses) and any(exact(p.units.number) < 0 for p in expenses):
        return None  # Mixed gross benefits still need their economic-source proof.
    return NativeHBundle(assets, expenses, currency,
                         sum((exact(p.units.number) for p in expenses), Fraction(0)))


def compile_h_bundle(native, entry, facts, claims, report_currency):
    """Cancel same-purpose H movement as a group, never invent payer pairings.

    A pure transfer's net purpose vector is G. With expense/refund counterparts,
    residuals have their direction or a single paying/receiving purpose proves
    the expense share. Mixed-purpose remaps require a separate declaration.
    """
    from collections import defaultdict
    from ..exact import exact_text
    from .classification import _key
    refs = [facts.for_posting(p).as_dict() for p in native.assets]
    primary = next(i for i,p in enumerate(native.assets) if p.units.number < 0)
    total = sum((exact(p.units.number) for p in native.assets), Fraction(0))
    event = {'protocol': 'native_h_bundle_v1', 'date': entry.date.isoformat(),
             'transaction_ref': facts.for_transaction(entry).as_dict(),
             'asset_refs': refs, 'asset_accounts': [p.account for p in native.assets], 'expense_refs': [facts.for_posting(p).as_dict() for p in native.expenses],
             'ref': refs[primary], 'account': native.assets[primary].account,
             'commodity': native.currency, 'signed_units': exact_text(exact(native.assets[primary].units.number)),
             'asset_total': exact_text(total), 'expense_total': exact_text(native.expense_total),
             'status': 'NEEDS_REVIEW', 'reason_code': None}
    if total + native.expense_total:
        return {**event, 'reason_code': 'H_BUNDLE_UNBALANCED'}
    if any(_key(ref) not in claims or any(part['role'] != 'hold' for part in claims[_key(ref)]['slices']) for ref in refs):
        return {**event, 'reason_code': 'H_BUNDLE_OWNERSHIP_PENDING'}
    by_owner, by_account = defaultdict(Fraction), defaultdict(lambda: defaultdict(Fraction))
    for posting, ref in zip(native.assets, refs):
        for part in claims[_key(ref)]['slices']:
            owner, amount = part['owner_id'], Fraction(part['signed_units'])
            by_owner[owner] += amount
            by_account[owner][posting.account] += amount
    expense = native.expense_total
    charges = {owner: -amount for owner, amount in by_owner.items() if amount} if expense else {}
    if expense and any(amount * expense > 0 for amount in by_owner.values()):
        # A single paying purpose determines all consumption; symmetrically a
        # single receiving purpose determines a refund. A mixed source remap
        # has no such evidence and must remain data review.
        side = {part['owner_id'] for p,ref in zip(native.assets, refs) if exact(p.units.number)*expense < 0
                for part in claims[_key(ref)]['slices'] if Fraction(part['signed_units'])}
        if len(side) != 1:
            return {**event, 'reason_code': 'H_BUNDLE_PURPOSE_AMBIGUOUS'}
        charges = {next(iter(side)): expense}
    net = {owner: amount + charges.get(owner, Fraction(0)) for owner, amount in by_owner.items()}
    allocations = {owner: exact_text(amount) for owner, amount in net.items() if amount and owner != '-'}
    outgoing = {part['owner_id'] for p,ref in zip(native.assets, refs) if p.units.number < 0
                for part in claims[_key(ref)]['slices'] if Fraction(part['signed_units'])}
    incoming = {part['owner_id'] for p,ref in zip(native.assets, refs) if p.units.number > 0
                for part in claims[_key(ref)]['slices'] if Fraction(part['signed_units'])}
    # Pure many-to-one merges are as explicit as one-to-many splits. Keep
    # mixed remaps with fees/refunds behind their existing attribution gate.
    if allocations and len(outgoing)>1 and (expense or len(incoming)>1) and len(set(event['asset_accounts']))>1:
        return {**event, 'reason_code': 'H_TRANSFER_MIXED_REMAP_PENDING'}
    if allocations and native.currency != report_currency:
        return {**event, 'reason_code': 'H_BUNDLE_ALLOCATION_CURRENCY_UNSUPPORTED'}
    payments = []
    if expense:
        for owner, charge in sorted(charges.items()):
            if not charge:
                continue
            sources = {account: value for account, value in by_account[owner].items() if value * charge < 0}
            opposite = any(value * charge > 0 for value in by_account[owner].values())
            exact_split = not opposite and -sum(sources.values(), Fraction(0)) == charge
            if exact_split or len(sources) == 1:
                payments.extend({'owner_id': owner, 'accounts': [account],
                                 'signed_expense_units': exact_text(-value if exact_split else charge)}
                                for account, value in sorted(sources.items()))
            else:
                payments.append({'owner_id': owner, 'accounts': sorted(sources),
                                 'signed_expense_units': exact_text(charge)})
    return {**event, 'status': 'CERTIFIED', 'reason_code': None,
            'purpose_residuals': {owner: exact_text(amount) for owner, amount in sorted(by_owner.items()) if amount},
            'net_allocations': allocations, 'cash_expenses': payments,
            'expense_by_owner': {owner: exact_text(amount) for owner, amount in charges.items() if amount},
            'basis': 'joint_native_equation_same_purpose_cancellation_no_pairwise_allocation'}

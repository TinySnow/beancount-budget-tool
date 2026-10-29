"""Recognize initial pad balances without hiding later reconciliation changes."""

from datetime import date
from fractions import Fraction

from beancount.core import data

from ..financial import FinancialInput
from ..exact import exact, exact_text
from .accounts import _lot
from .facts import FactIndex, GeneratedTransactionRef


def explicit_equity_receipt(fin, entry, facts, modes, claims):
    """Prove a declared equity receipt, without inventing income or old assets.

    This is a current booked increase, not an opening balance reset or a
    transfer from an inferred term-deposit account. Its complete inline fund
    is required even when defaults exist. Generated pad facts keep their
    separate first-coordinate/reconciliation rules.
    """
    if (not isinstance(entry, data.Transaction) or len(entry.postings) != 2 or
            isinstance(facts.for_transaction(entry), GeneratedTransactionRef)):
        return None
    assets = [p for p in entry.postings
              if p.account.split(":", 1)[0] == fin.options["name_assets"]]
    if len(assets) != 1 or modes.get(assets[0].account) != "H":
        return None
    asset = assets[0]
    counterpart = next(p for p in entry.postings if p is not asset)
    if (counterpart.account.split(":", 1)[0] != fin.options["name_equity"] or
            exact(asset.units.number) <= 0 or
            counterpart.units.currency != asset.units.currency or
            exact(counterpart.units.number) != -exact(asset.units.number) or
            any(p.cost is not None or p.price is not None or
                fin.raw_price(p)[0] != "none" for p in entry.postings)):
        return None
    claim = claims.get(facts.for_posting(asset).as_key(), {})
    parts = claim.get("slices", [])
    if (claim.get("origin") != "inline" or len(parts) != 1 or
            parts[0]["role"] != "hold" or
            Fraction(parts[0]["signed_units"]) != exact(asset.units.number)):
        return None
    return {"protocol": "explicit_equity_receipt_v1", "date": entry.date.isoformat(),
            "asset_ref": facts.for_posting(asset).as_dict(),
            "equity_ref": facts.for_posting(counterpart).as_dict(),
            "account": asset.account,
            "commodity": asset.units.currency,
            "signed_units": exact_text(exact(asset.units.number)),
            "owner_id": parts[0]["owner_id"]}


def initial_pad_postings(fin: FinancialInput, cutover: date) -> dict[int, data.Transaction]:
    """Return first positive, cost-free Asset/Equity pad facts after cutover.

    The configured default income owner is also the initial money's default
    purpose. This is neither actual income nor a purpose allocation. Once a
    coordinate has any booked history, a later pad is a reconciliation change
    and must not silently reset its ownership. File/account names are irrelevant.
    """
    facts = FactIndex(fin)
    seen = set()
    result = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        assets = [p for p in entry.postings
                  if p.account.split(":", 1)[0] == fin.options["name_assets"]]
        identity = facts.for_transaction(entry)
        if (entry.date >= cutover and len(entry.postings) == 2 and len(assets) == 1 and
                isinstance(identity, GeneratedTransactionRef) and identity.generated_kind == "pad"):
            asset = assets[0]
            counterpart = next(p for p in entry.postings if p is not asset)
            coordinate = (asset.account, asset.units.currency, _lot(asset))
            if (coordinate not in seen and asset.units.number > 0 and
                    asset.cost is None and asset.price is None and
                    counterpart.account.split(":", 1)[0] == fin.options["name_equity"] and
                    counterpart.cost is None and counterpart.price is None and
                    counterpart.units.currency == asset.units.currency and
                    counterpart.units.number == -asset.units.number):
                result[id(asset)] = entry
        seen.update((p.account, p.units.currency, _lot(p)) for p in assets)
    return result

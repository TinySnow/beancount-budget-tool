"""Exact arithmetic shared by inventory, valuation and projections."""

from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Union

from .errors import BudgetError

Number = Union[str, int, Decimal, Fraction]


def exact(value: Number) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise BudgetError("NUMBER_INVALID", "布尔值不是金额")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise BudgetError("NUMBER_INVALID", f"无效数值：{value}") from exc
    if not decimal.is_finite():
        raise BudgetError("NUMBER_INVALID", "金额必须为有限数")
    return Fraction(decimal)


def exact_text(value: Fraction) -> str:
    """Emit a lossless rational string; decimal formatting belongs to the UI."""
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def amount_text(raw: Number) -> str:
    """Format a finite decimal exactly, otherwise retain its rational value."""
    value = Fraction(raw)
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return str(value)
    places = max(twos, fives)
    scaled = abs(value.numerator) * (10 ** places) // value.denominator
    whole, fractional = divmod(scaled, 10 ** places)
    tail = f"{fractional:0{places}d}".rstrip("0") if places else ""
    return ("-" if value < 0 else "") + str(whole) + ("." + tail if tail else "")

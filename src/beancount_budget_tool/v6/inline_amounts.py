"""Parse the user-facing, posting-level budget and fund amount syntax.

Parsing is exact. A bare registered name covers the whole posting; a list
must give an amount for every name and add back to the booked posting.
"""

from __future__ import annotations

from fractions import Fraction
import re

from ..errors import require


SEPARATOR = re.compile(r"[,，|｜;；]")
AMOUNT = re.compile(r"^(?P<name>.+?)(?:\s*[：:]\s*|\s*)(?P<amount>\d+(?:\.\d+)?)(?:\s*元)?$")


def parse_inline_amounts(raw: str, signed_units: Fraction, source: str,
                         *, known_names: set[str] | None = None,
                         code: str = "TAG_SYNTAX_INVALID") -> list[tuple[str, Fraction]]:
    """Return exact, signed (name, amount) slices for one booked posting."""
    require(isinstance(raw, str) and bool(raw) and raw == raw.strip(), code,
            "用途／类别标签须为非空、无首尾空白的字符串", source)
    if raw == "-":
        return [(raw, signed_units)]
    if known_names is not None and raw in known_names:
        return [(raw, signed_units)]

    pieces = SEPARATOR.split(raw)
    listed = len(pieces) > 1
    if not listed and known_names is None:
        # The source gate has no registry yet: a name may itself end in digits.
        return [(raw, signed_units)]
    if listed and pieces[-1] == "":
        pieces.pop()  # The documented inline example ends with a semicolon.
    require(all(piece.strip() for piece in pieces), code,
            "多用途／类别标签不能含空项", source)
    parsed: list[tuple[str, Fraction]] = []
    for piece in pieces:
        piece = piece.strip()
        match = AMOUNT.fullmatch(piece)
        if match is None:
            require(not listed and piece != "-", code,
                    "多用途／类别标签每项都须写正数金额", source)
            return [(piece, signed_units)]
        name = match.group("name").strip()
        require(bool(name) and name != "-" and not name.endswith(("-", "－", "—", "–")),
                code, "用途／类别名称无效或金额带负号", source)
        amount = Fraction(match.group("amount"))
        require(amount > 0, code, "用途／类别切片金额须大于零", source)
        parsed.append((name, amount if signed_units >= 0 else -amount))
    require(sum((amount for _, amount in parsed), Fraction(0)) == signed_units,
            code, "用途／类别切片金额没有精确加回分录金额", source)
    return parsed

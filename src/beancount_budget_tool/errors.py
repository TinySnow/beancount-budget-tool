"""Stable, source-oriented diagnostics. Errors never carry partial reports."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BudgetError(Exception):
    code: str
    message: str
    source: Optional[str] = None
    # A replay anchor, never a partial report or cached balance.
    diagnostic: Optional[dict] = field(default=None, repr=False, compare=False)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" + (f" ({self.source})" if self.source else "")


def require(condition: bool, code: str, message: str, source: Optional[str] = None) -> None:
    if not condition:
        raise BudgetError(code, message, source)

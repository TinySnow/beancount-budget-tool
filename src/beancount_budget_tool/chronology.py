"""Explicit same-day ordering for cost-free transactions, never inferred funding.

Beancount uses source line numbers across included files. An inline
budget-order overrides that incidental order for cash replay only. Booked
amounts and lots stay intact; crossing a cost transaction or generated pad is
rejected rather than changing booking or initialization semantics.
"""

from collections import defaultdict
from decimal import Decimal

from beancount.core import data

from .errors import require


KEY = "budget-order"


def order_entries(entries):
    priorities = {}
    days = defaultdict(list)
    for index, entry in enumerate(entries):
        meta = entry.meta or {}
        source = f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}"
        if KEY in meta:
            require(isinstance(entry, data.Transaction), "TRANSACTION_ORDER_INVALID",
                    "budget-order 只能写在交易头下面，不能写在指令或分录上", source)
            value = meta[KEY]
            require(type(value) is Decimal and value.is_finite() and value == value.to_integral_value(),
                    "TRANSACTION_ORDER_INVALID", "budget-order 须为不加引号的整数；越小越先，未填按 0", source)
            require(not _protected(entry), "TRANSACTION_ORDER_UNSUPPORTED",
                    "budget-order 目前只支持无成本普通交易；不能重排证券成本批次或自动 pad", source)
            priorities[id(entry)] = value
        if isinstance(entry, data.Transaction):
            days[entry.date].append(index)
            for posting in entry.postings:
                pmeta = posting.meta or {}
                require(KEY not in pmeta, "TRANSACTION_ORDER_INVALID",
                        "budget-order 应写在交易头下面、第一条分录前面",
                        f"{pmeta.get('filename', '?')}:{pmeta.get('lineno', '?')}")
    if not priorities:
        return entries
    result = list(entries)
    for indices in days.values():
        if not any(id(entries[index]) in priorities for index in indices):
            continue
        original = [entries[index] for index in indices]
        ordered = sorted(enumerate(original), key=lambda pair: priorities.get(id(pair[1]), Decimal(0)))
        barriers = [position for position, entry in enumerate(original) if _protected(entry)]
        for position, (old_position, entry) in enumerate(ordered):
            require(position == old_position or not any(min(position, old_position) <= barrier <= max(position, old_position)
                                                       for barrier in barriers),
                    "TRANSACTION_ORDER_UNSUPPORTED",
                    "声明的同日顺序会跨越成本交易或自动 pad；请在源账本核对顺序，工具不重做成本 booking",
                    f"{entry.meta.get('filename', '?')}:{entry.meta.get('lineno', '?')}")
            result[indices[position]] = entry
    return result


def _protected(entry):
    return entry.flag == "P" or any(posting.cost is not None for posting in entry.postings)

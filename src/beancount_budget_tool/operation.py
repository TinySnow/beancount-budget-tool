"""Cooperative cancellation and coarse progress, with no accounting decisions."""

from contextlib import contextmanager
from contextvars import ContextVar

from .errors import BudgetError

_CURRENT = ContextVar('query_operation', default=None)


def checkpoint(stage=None):
    current = _CURRENT.get()
    if current is None:
        return
    cancel, progress = current
    if cancel.is_set():
        raise BudgetError('QUERY_CANCELLED', '本次查询已取消；未发布部分结果。')
    if stage and progress:
        progress(stage)


@contextmanager
def operation(cancel, progress=None):
    token = _CURRENT.set((cancel, progress))
    try:
        checkpoint('准备输入')
        yield
        checkpoint()
    finally:
        _CURRENT.reset(token)

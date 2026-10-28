"""Canonical external identities with one serialization per query session.

    Keys remain byte-for-byte compatible with saved claims. Flat payloads are
    indexed by their complete typed values, never by mutable dict identity.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
from .projection import FrozenDict

_KEYS = ContextVar('identity_keys', default=None)


@contextmanager
def identity_scope():
    if _KEYS.get() is not None:
        yield
        return
    token = _KEYS.set({})
    try:
        yield
    finally:
        _KEYS.reset(token)


def canonical_key(value):
    if isinstance(value, FrozenDict):
        return value.canonical_key
    cache = _KEYS.get()
    signature = None
    if cache is not None and type(value) is dict:
        fields = []
        for key, item in value.items():
            if type(key) is not str or type(item) not in (str, int, bool, type(None)):
                break
            fields.append((key, type(item), item))
        else:
            signature = tuple(fields)
            previous = cache.get(signature)
            if previous is not None:
                return previous
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if signature is not None:
        cache[signature] = result
    return result

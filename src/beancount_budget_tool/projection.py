"""Read-only internal projection records; mutable copies at public boundaries."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import cached_property
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
import json

_GRAPH = ContextVar('projection_graph', default=None)
_ATOMS = {str, int, float, bool, bytes, type(None), date, datetime, Decimal, Fraction}


class ProjectionGraph:
    """Query-owned proof nodes, isolated from reusable source snapshots."""
    def __init__(self):
        self.inputs = {}

    def for_input(self, financial):
        identity = id(financial)
        if identity not in self.inputs:
            # Retain the view as well as its nodes, preventing identity reuse
            # and keeping each cutoff's decisions strictly separate.
            self.inputs[identity] = financial, {}
        return self.inputs[identity][1]

    def clear(self):
        self.inputs.clear()


def _reject(*args, **kwargs):
    raise TypeError('Projection records are read only; copy before annotation')


class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _reject

    @cached_property
    def canonical_key(self):
        return json.dumps(self, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def __deepcopy__(self, memo):
        result = {}
        memo[id(self)] = result
        for key, value in self.items():
            result[deepcopy(key, memo)] = deepcopy(value, memo)
        return result


class FrozenList(list):
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = sort = reverse = __iadd__ = __imul__ = _reject

    def __deepcopy__(self, memo):
        result = []
        memo[id(self)] = result
        result.extend(deepcopy(value, memo) for value in self)
        return result


def freeze(value, memo=None):
    if type(value) in _ATOMS:
        return value
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if is_dataclass(value) and value.__dataclass_params__.frozen:
        return replace(value, **{item.name: freeze(getattr(value, item.name)) for item in fields(value)})
    if type(value) is frozenset:
        return frozenset(freeze(item) for item in value)
    if type(value) not in (dict, list, tuple, set):
        raise TypeError('Unsupported mutable projection value: ' + type(value).__name__)
    if memo is None:
        memo = {}
    if id(value) in memo:
        return memo[id(value)]
    if type(value) is tuple:
        items = [freeze(item, memo) for item in value]
        if id(value) in memo:
            return memo[id(value)]
        result = tuple(items)
        memo[id(value)] = result
        return result
    if type(value) is set:
        result = frozenset(freeze(item, memo) for item in value)
        memo[id(value)] = result
        return result
    if type(value) is dict:
        result = FrozenDict()
        memo[id(value)] = result
        for key, item in value.items():
            dict.__setitem__(result, key, freeze(item, memo))
    else:
        result = FrozenList()
        memo[id(value)] = result
        list.extend(result, (freeze(item, memo) for item in value))
    return result


def readonly_projections():
    return _GRAPH.get() is not None


def projection_cache(financial):
    graph = _GRAPH.get()
    return graph.for_input(financial) if graph is not None else financial._audit_results


@contextmanager
def projection_scope():
    existing = _GRAPH.get()
    if existing is not None:
        yield existing
        return
    graph = ProjectionGraph()
    token = _GRAPH.set(graph)
    try:
        yield graph
    finally:
        _GRAPH.reset(token)
        graph.clear()

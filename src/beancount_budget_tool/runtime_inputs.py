"""Query-owned generated documents, separate from physical user inputs.

Internal proofs consume documents directly. YAML bytes are generated lazily
only for existing evidence hashes and diagnostics. Physical sidecars remain a
supported adapter for review editing, import/export and older callers.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import cached_property

import yaml
from .projection import freeze

_DOCUMENTS = ContextVar('runtime_documents', default=None)


@dataclass(frozen=True)
class RuntimeDocument:
    value: object
    dumper: type

    @cached_property
    def content(self):
        return yaml.dump(deepcopy(self.value), Dumper=self.dumper, allow_unicode=True,
                         sort_keys=False).encode('utf-8')


@contextmanager
def document_scope():
    if _DOCUMENTS.get() is not None:
        yield
        return
    token = _DOCUMENTS.set({})
    try:
        yield
    finally:
        _DOCUMENTS.reset(token)


def register_documents(directory, documents, dumper):
    store = _DOCUMENTS.get()
    if store is None:
        raise RuntimeError('RuntimeInputs requires an active query session')
    for name, value in documents.items():
        path = (directory / name).resolve()
        if path in store:
            raise RuntimeError('RuntimeInputs cannot overwrite an existing document')
        store[path] = RuntimeDocument(freeze(value), dumper)


def document(path):
    store = _DOCUMENTS.get()
    return store.get(path) if store is not None else None


def input_bytes(path):
    item = document(path)
    return item.content if item is not None else path.read_bytes()


def input_text(path, encoding='utf-8-sig'):
    return input_bytes(path).decode(encoding)


def input_is_file(path):
    return document(path) is not None or path.is_file()


def input_value(path):
    item = document(path)
    return (True, deepcopy(item.value)) if item is not None else (False, None)

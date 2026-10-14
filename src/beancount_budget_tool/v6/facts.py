"""Stable references for every booked v6 financial fact.

This is a source adapter, not a budget projection.  Ordinary transactions use
the existing raw-to-booked reference contract.  Beancount ``pad`` creates real
booked transactions without raw posting lines, so those facts are anchored to
the originating pad directive and the pinned loader version instead.
"""

from dataclasses import dataclass
from datetime import date
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path
from typing import Dict, Iterator, Optional, Union

from beancount.core import data

from ..errors import BudgetError, require
from ..financial import FinancialInput, reference_financial
from ..refs import BookedRef, ReferenceIndex, TransactionRef, _fact, _lot


@dataclass(frozen=True)
class GeneratedTransactionRef:
    path: str
    directive_sha256: str
    occurrence: int
    generated_kind: str
    generated_ordinal: int
    loader_version: str

    def as_dict(self) -> dict:
        return vars(self).copy()


@dataclass(frozen=True)
class GeneratedPostingRef:
    transaction: GeneratedTransactionRef
    posting_ordinal: int
    lot: str
    fact_sha256: str

    def as_dict(self) -> dict:
        return {**self.transaction.as_dict(), "posting_ordinal": self.posting_ordinal,
                "lot": self.lot, "fact_sha256": self.fact_sha256}


TransactionIdentity = Union[TransactionRef, GeneratedTransactionRef]
PostingIdentity = Union[BookedRef, GeneratedPostingRef]


def future_transactions(fin: FinancialInput, cutover: date) -> Iterator[data.Transaction]:
    """Identify existing future facts without including them in report totals.

    Only a report-bounded financial input has invisible entries. Consumers
    still check their claim domain; a missing or changed reference is never
    accepted merely because the reviewer calls it a future transaction.
    """
    if fin.reference_entries is None:
        return
    visible = {id(entry) for entry in fin.entries}
    for entry in fin.reference_entries:
        if isinstance(entry, data.Transaction) and entry.date >= cutover and id(entry) not in visible:
            yield entry


def future_transaction_refs(fin: FinancialInput, cutover: date) -> Iterator[tuple[data.Transaction, list[dict]]]:
    facts = None
    for entry in future_transactions(fin, cutover):
        if facts is None:
            facts = reference_fact_index(fin)
        yield entry, [facts.for_posting(posting).as_dict() for posting in entry.postings]


def future_posting_refs(fin: FinancialInput, cutover: date) -> Iterator[tuple[data.Posting, dict]]:
    for entry, refs in future_transaction_refs(fin, cutover):
        yield from zip(entry.postings, refs)


_FACT_INDEX_CACHE: ContextVar[Optional[Dict[int, "FactIndex"]]] = ContextVar(
    "fact_index_cache", default=None)
_REFERENCE_INDEX_CACHE: ContextVar[Optional[Dict[int, tuple[FinancialInput, "FactIndex"]]]] = ContextVar(
    "reference_fact_index_cache", default=None)


def reference_fact_index(fin: FinancialInput) -> "FactIndex":
    """Reuse a complete-source index within one frozen report, never across edits."""
    if fin.reference_entries is None:
        return FactIndex(fin)
    cache = _REFERENCE_INDEX_CACHE.get()
    if cache is not None and id(fin) in cache:
        return cache[id(fin)][1]
    facts = FactIndex(reference_financial(fin))
    if cache is not None:
        cache[id(fin)] = (fin, facts)
    return facts


@contextmanager
def frozen_fact_indices():
    """Reuse source-reference work only within one frozen report."""
    token = _FACT_INDEX_CACHE.set({})
    reference_token = _REFERENCE_INDEX_CACHE.set({})
    try:
        yield
    finally:
        _REFERENCE_INDEX_CACHE.reset(reference_token)
        _FACT_INDEX_CACHE.reset(token)


class FactIndex:
    """Map all booked transactions and postings to auditable source identities.

    Any generated transaction other than an official ``pad`` result is
    rejected until it has an explicit origin protocol.  In particular, a
    missing raw reference never falls back to date, amount or source line.
    """

    def __new__(cls, fin: FinancialInput):
        cache = _FACT_INDEX_CACHE.get()
        if cache is not None and id(fin) in cache:
            return cache[id(fin)]
        return super().__new__(cls)

    def __init__(self, fin: FinancialInput) -> None:
        if getattr(self, "_initialized", False):
            return
        raw = ReferenceIndex(fin)
        self.transaction_refs: Dict[int, TransactionIdentity] = dict(raw.by_transaction)
        self.posting_refs: Dict[int, PostingIdentity] = dict(raw.by_posting)
        self.generated_transactions = 0
        self.generated_postings = 0

        pads = {}
        occurrences = {}
        for entry in fin.entries:
            if not isinstance(entry, data.Pad):
                continue
            filename, lineno = entry.meta.get("filename"), entry.meta.get("lineno")
            require(isinstance(filename, str) and isinstance(lineno, int),
                    "SOURCE_REF_INVALID", "pad 指令缺少原文位置")
            path = Path(filename).resolve()
            require(path in fin.sources, "SOURCE_REF_INVALID", "pad 来源未进入冻结快照",
                    f"{filename}:{lineno}")
            relative = (path.relative_to(fin.root.parent).as_posix()
                        if path.is_relative_to(fin.root.parent) else path.as_posix())
            digest = sha256(fin.transaction_text(filename, lineno).encode()).hexdigest()
            group = (relative, digest)
            occurrences[group] = occurrences.get(group, 0) + 1
            key = (path, lineno)
            require(key not in pads, "SOURCE_REF_AMBIGUOUS", "同一来源位置有多个 pad 指令",
                    f"{filename}:{lineno}")
            pads[key] = (relative, digest, occurrences[group])

        generated_ordinals = {}
        for entry in fin.entries:
            if not isinstance(entry, data.Transaction):
                continue
            if id(entry) in self.transaction_refs:
                for posting in entry.postings:
                    require(id(posting) in self.posting_refs, "SOURCE_REF_INVALID",
                            "原始交易的 booked posting 无法唯一映射到原文")
                continue
            meta = entry.meta or {}
            filename, lineno = meta.get("filename"), meta.get("lineno")
            require(isinstance(filename, str) and isinstance(lineno, int),
                    "GENERATED_REF_UNSUPPORTED", "生成交易缺少来源位置")
            key = (Path(filename).resolve(), lineno)
            require(key in pads and entry.flag == "P", "GENERATED_REF_UNSUPPORTED",
                    "仅支持由源 pad 指令生成的 booked 交易", f"{filename}:{lineno}")
            generated_ordinals[key] = generated_ordinals.get(key, 0) + 1
            relative, digest, occurrence = pads[key]
            txref = GeneratedTransactionRef(relative, digest, occurrence, "pad",
                                            generated_ordinals[key], fin.loader_version)
            self.transaction_refs[id(entry)] = txref
            self.generated_transactions += 1
            for ordinal, posting in enumerate(entry.postings, 1):
                require(id(posting) not in self.posting_refs, "SOURCE_REF_AMBIGUOUS",
                        "生成 posting 与原文 posting 引用冲突")
                self.posting_refs[id(posting)] = GeneratedPostingRef(
                    txref, ordinal, _lot(posting), _fact(posting))
                self.generated_postings += 1
        self._initialized = True
        # Keep the identity alive for the report-local cache keyed by id(fin).
        # Reference-only views may otherwise be collected between audits.
        self._financial = fin
        cache = _FACT_INDEX_CACHE.get()
        if cache is not None:
            cache[id(fin)] = self

    def for_transaction(self, transaction: data.Transaction) -> TransactionIdentity:
        try:
            return self.transaction_refs[id(transaction)]
        except KeyError as exc:
            raise BudgetError("SOURCE_REF_INVALID", "交易没有冻结的来源身份") from exc

    def for_posting(self, posting: data.Posting) -> PostingIdentity:
        try:
            return self.posting_refs[id(posting)]
        except KeyError as exc:
            raise BudgetError("SOURCE_REF_INVALID", "posting 没有冻结的来源身份") from exc

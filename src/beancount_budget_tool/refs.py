"""Versioned source-to-booked references for external v4 claims.

Line numbers are diagnostics only. A claim is identified by source transaction
content, its occurrence among identical transactions, the raw posting ordinal,
the booked child ordinal, the lot and the booked fact fingerprint.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from beancount.core import data
from beancount.parser import parser

from .errors import BudgetError, require
from .financial import FinancialInput


@dataclass(frozen=True)
class TransactionRef:
    path: str
    transaction_sha256: str
    occurrence: int

    def as_dict(self) -> dict:
        return {"path": self.path, "transaction_sha256": self.transaction_sha256,
                "occurrence": self.occurrence}


def parse_txref(value: object) -> TransactionRef:
    require(isinstance(value, dict) and set(value) == {"path", "transaction_sha256", "occurrence"},
            "CLAIM_REF_INVALID", "after 必须是结构化 RawTxnRef")
    require(isinstance(value["path"], str) and isinstance(value["transaction_sha256"], str) and
            len(value["transaction_sha256"]) == 64 and type(value["occurrence"]) is int and value["occurrence"] >= 1,
            "CLAIM_REF_INVALID", "after 的 RawTxnRef 字段非法")
    return TransactionRef(**value)


@dataclass(frozen=True)
class BookedRef:
    path: str
    transaction_sha256: str
    occurrence: int
    posting_ordinal: int
    booked_ordinal: int
    lot: str
    fact_sha256: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "transaction_sha256": self.transaction_sha256,
            "occurrence": self.occurrence,
            "posting_ordinal": self.posting_ordinal,
            "booked_ordinal": self.booked_ordinal,
            "lot": self.lot,
            "fact_sha256": self.fact_sha256,
        }


def parse_ref(value: object) -> BookedRef:
    require(isinstance(value, dict), "CLAIM_REF_INVALID", "claim 必须是结构化 BookedRef")
    expected = {"path", "transaction_sha256", "occurrence", "posting_ordinal", "booked_ordinal", "lot", "fact_sha256"}
    require(set(value) == expected, "CLAIM_REF_INVALID", "BookedRef 字段不完整或含未知字段")
    require(all(isinstance(value[k], str) for k in ("path", "transaction_sha256", "lot", "fact_sha256")),
            "CLAIM_REF_INVALID", "BookedRef 字符串字段非法")
    require(all(type(value[k]) is int and value[k] >= 1 for k in ("occurrence", "posting_ordinal", "booked_ordinal")),
            "CLAIM_REF_INVALID", "BookedRef 序号必须从 1 开始")
    require(all(len(value[k]) == 64 and all(c in "0123456789abcdef" for c in value[k])
                for k in ("transaction_sha256", "fact_sha256")), "CLAIM_REF_INVALID", "BookedRef 哈希非法")
    return BookedRef(**value)


def _lot(posting: data.Posting) -> str:
    cost = posting.cost
    if cost is None:
        return f"{posting.units.currency}/no-cost"
    return json.dumps([posting.units.currency, str(cost.number), cost.currency,
                       cost.date.isoformat() if cost.date else None, cost.label], ensure_ascii=False)


def _fact(posting: data.Posting) -> str:
    payload = {
        "account": posting.account,
        "units": [str(posting.units.number), posting.units.currency],
        "lot": _lot(posting),
        "price": [str(posting.price.number), posting.price.currency] if posting.price else None,
        "flag": posting.flag,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ReferenceIndex:
    def __init__(self, fin: FinancialInput) -> None:
        self.fin = fin
        self.by_transaction: Dict[int, TransactionRef] = {}
        self.by_txref: Dict[TransactionRef, data.Transaction] = {}
        self.by_posting: Dict[int, BookedRef] = {}
        self.by_ref: Dict[BookedRef, data.Posting] = {}
        raw_postings: Dict[Tuple[Path, int], Tuple[int, ...]] = {}
        for path in fin.sources:
            raw_entries, errors, _ = parser.parse_file(str(path))
            require(not errors, "CLAIM_REF_INVALID", "来源文件无法由官方 parser 唯一识别", str(path))
            for raw in raw_entries:
                if not isinstance(raw, data.Transaction):
                    continue
                raw_postings[(path, raw.meta["lineno"])] = tuple(p.meta["lineno"] for p in raw.postings)
        transactions = [entry for entry in fin.entries if isinstance(entry, data.Transaction)]
        transactions.sort(key=lambda tx: (str(tx.meta.get("filename", "")), tx.meta.get("lineno", 0)))
        occurrence: Dict[Tuple[str, str], int] = {}
        for tx in transactions:
            filename, lineno = tx.meta.get("filename"), tx.meta.get("lineno")
            if not isinstance(filename, str) or not isinstance(lineno, int):
                continue  # A generated transaction requires GeneratedRef, not a guessed RawTxnRef.
            path = Path(filename).resolve()
            if path not in fin.sources:
                continue
            raw_text = fin.transaction_text(filename, lineno)
            tx_hash = sha256(raw_text.encode()).hexdigest()
            relative = str(path.relative_to(fin.root.parent)) if path.is_relative_to(fin.root.parent) else str(path)
            group = (relative, tx_hash)
            occurrence[group] = occurrence.get(group, 0) + 1
            txref = TransactionRef(relative, tx_hash, occurrence[group])
            raw_lines = raw_postings.get((path, lineno))
            if raw_lines is None:
                continue  # e.g. a generated pad entry: not a raw transaction.
            require(txref not in self.by_txref, "CLAIM_REF_AMBIGUOUS", "原始交易引用重复")
            self.by_transaction[id(tx)] = txref
            self.by_txref[txref] = tx
            child_ordinals: Dict[int, int] = {}
            for posting in tx.postings:
                meta = posting.meta or {}
                line = meta.get("lineno")
                if not isinstance(meta.get("filename"), str) or Path(meta["filename"]).resolve() != path or line not in raw_lines:
                    continue  # Refuse during claim lookup; never synthesize an identity.
                child_ordinals[line] = child_ordinals.get(line, 0) + 1
                ref = BookedRef(relative, tx_hash, occurrence[group], raw_lines.index(line) + 1,
                                child_ordinals[line], _lot(posting), _fact(posting))
                require(ref not in self.by_ref, "CLAIM_REF_AMBIGUOUS", "两个 booked 事实生成了同一个引用")
                self.by_posting[id(posting)] = ref
                self.by_ref[ref] = posting

    def for_posting(self, posting: data.Posting) -> BookedRef:
        try:
            return self.by_posting[id(posting)]
        except KeyError as exc:
            raise BudgetError("CLAIM_REF_INVALID", "posting 无法唯一映射到原始交易与 booked 子腿") from exc

    def for_transaction(self, transaction: data.Transaction) -> TransactionRef:
        try:
            return self.by_transaction[id(transaction)]
        except KeyError as exc:
            raise BudgetError("CLAIM_REF_INVALID", "交易无法唯一映射到原始财务事实") from exc

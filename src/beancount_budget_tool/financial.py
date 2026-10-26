"""Official Beancount adapter plus a frozen, read-only source manifest.

This module does not infer accounting facts from source text. It only retains
the lexical evidence that Beancount's booked AST may normalize away.
"""

from calendar import monthrange
from collections import deque
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import date
from functools import cached_property, wraps
from hashlib import sha256
from inspect import signature
import json
import glob
import os
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from beancount import __version__ as BEANCOUNT_VERSION, loader
from beancount.core import data
from beancount.parser import parser

from .errors import BudgetError, require
from .exact import Fraction, exact

PRICE = re.compile(r"(?<!@)@@?\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([A-Za-z][A-Za-z0-9_]*)")
RESERVED_PUSHMETA = re.compile(r"^\s*pushmeta\s+(?:budget|fund|tracking|budget-order)\s*:", re.IGNORECASE)


def month_bounds(value: str) -> tuple[date, date]:
    """Validate a calendar month and return its inclusive ledger cutoff bounds."""
    try:
        first = date.fromisoformat(value + "-01")
        require(first.strftime("%Y-%m") == value, "MONTH_INVALID", "月份须为 YYYY-MM")
    except ValueError as exc:
        raise BudgetError("MONTH_INVALID", "月份须为有效的 YYYY-MM") from exc
    return first, date(first.year, first.month, monthrange(first.year, first.month)[1])


@dataclass(frozen=True)
class SourceFile:
    path: Path
    text: str
    sha256: str

    @cached_property
    def lines(self) -> Tuple[str, ...]:
        return tuple(self.text.splitlines())

    @cached_property
    def _syntax(self) -> tuple:
        """Keep only official positions/include directives, not another full AST."""
        entries, errors, options = parser.parse_string(self.text, report_filename=str(self.path))
        positions = {entry.meta["lineno"]: tuple(posting.meta["lineno"] for posting in entry.postings)
                     for entry in entries if isinstance(entry, data.Transaction)}
        return positions, tuple(options["include"]), errors

    @cached_property
    def _posting_lines(self) -> Dict[int, Tuple[int, ...]]:
        """Official raw syntax positions shared by views of this frozen text."""
        positions, _, errors = self._syntax
        require(not errors, "CLAIM_REF_INVALID", "来源文件无法由官方 parser 唯一识别", str(self.path))
        return positions


@dataclass(frozen=True)
class FinancialInput:
    root: Path
    entries: Tuple[Any, ...]
    options: Mapping[str, Any]
    sources: Mapping[Path, SourceFile]
    digest: str
    loader_version: str
    reference_entries: Optional[Tuple[Any, ...]] = None
    source_names: Mapping[str, Path] = field(default_factory=dict)
    include_globs: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)

    @cached_property
    def _sources_by_filename(self) -> Mapping[str, SourceFile]:
        return {str(path): source for path, source in self.sources.items()}

    @cached_property
    def _raw_prices(self) -> Dict[Tuple[str, int], Tuple[str, Fraction, str]]:
        # Lexical evidence belongs to this frozen source, never to a filename
        # across reloads. A fresh load/replace starts with an empty price cache.
        return {}

    @cached_property
    def _audit_results(self) -> dict:
        return {}

    @cached_property
    def _reference_tables(self) -> tuple[dict, dict]:
        """Raw identities for this view; full views are indexed only on demand."""
        from .refs import ReferenceIndex
        index = ReferenceIndex(self)
        return index.by_transaction, index.by_posting

    def _source(self, filename: str) -> SourceFile:
        # Booked metadata normally already has the canonical absolute name.
        # Match that frozen name without consulting the live filesystem again.
        source = self._sources_by_filename.get(filename)
        if source is not None:
            return source
        path = Path(filename).resolve()
        require(path in self.sources, "SOURCE_MISSING", "booked 事实引用了未冻结的来源", str(path))
        return self.sources[path]

    def line(self, filename: str, lineno: int) -> str:
        source = self._source(filename)
        lines = source.lines
        require(1 <= lineno <= len(lines), "SOURCE_MISSING", "来源行超出范围", f"{source.path}:{lineno}")
        return lines[lineno - 1]

    def raw_price(self, posting: Any) -> Tuple[str, Fraction, str]:
        """Return notation, unsigned raw price/total, currency or reject ambiguity."""
        meta = posting.meta or {}
        source = f"{meta.get('filename')}:{meta.get('lineno')}"
        require(isinstance(meta.get("filename"), str) and isinstance(meta.get("lineno"), int), "SOURCE_MISSING", "posting 缺少原文位置", source)
        key = (meta["filename"], meta["lineno"])
        if key in self._raw_prices:
            return self._raw_prices[key]
        line = self.line(meta["filename"], meta["lineno"]).split(";", 1)[0]
        matches = list(PRICE.finditer(line))
        require(len(matches) <= 1, "PRICE_SOURCE_CONFLICT", "一条 posting 有多个原文价格", source)
        if not matches:
            result = ("none", Fraction(0), "")
        else:
            match = matches[0]
            notation = "@@" if line[match.start():match.start() + 2] == "@@" else "@"
            result = (notation, abs(exact(match.group(1))), match.group(2))
        self._raw_prices[key] = result
        return result

    def transaction_text(self, filename: str, lineno: int) -> str:
        lines = self._source(filename).lines
        require(1 <= lineno <= len(lines), "SOURCE_MISSING", "交易来源行无效")
        selected = [lines[lineno - 1]]
        for line in lines[lineno:]:
            if line and not line[0].isspace():
                break
            selected.append(line)
        return "\n".join(selected).rstrip()


def _glob_matches(pattern: str) -> Tuple[str, ...]:
    # Match the official loader's recursive glob and relative-path semantics.
    return tuple(os.path.normpath(name) for name in glob.glob(pattern, recursive=True))


def _check_loader_errors(errors, root: Path) -> None:
    if errors:
        code = "DUPLICATE_METADATA_KEY" if "Duplicate" in str(errors[0]) and "metadata field" in str(errors[0]) else "LOADER_ERROR"
        raise BudgetError(code, f"Beancount 加载失败：{errors[0]}", str(root))


def _capture_sources(root: Path) -> tuple[dict, dict, dict]:
    sources, names, includes = {}, {}, {}
    pending = deque([str(root)])
    while pending:
        name = pending.popleft()
        if name in names:
            continue  # The official loader still diagnoses duplicate includes.
        path = Path(name).resolve()
        names[name] = path
        if path not in sources:
            try:
                raw = Path(name).read_bytes()
                sources[path] = SourceFile(path, raw.decode("utf-8-sig"), sha256(raw).hexdigest())
            except (OSError, UnicodeError) as exc:
                raise BudgetError("SOURCE_MISSING", str(exc), name) from exc
        source = sources[path]
        _, patterns, errors = source._syntax
        _check_loader_errors(errors, root)
        for pattern in patterns:
            search = pattern if os.path.isabs(pattern) else os.path.join(os.path.dirname(name), pattern)
            matches = _glob_matches(search)
            require(bool(matches), "LOADER_ERROR", f'Beancount include 没有匹配文件：{pattern}', name)
            includes[search] = matches
            pending.extend(matches)
    return sources, names, includes


def _verify_financial(fin: FinancialInput, message: str = "报表运行期间根账本或 include 来源发生变化") -> None:
    """Verify frozen bytes, aliases and glob membership without redoing booking."""
    try:
        unchanged = (all(Path(name).resolve() == path for name, path in fin.source_names.items()) and
                     all(sha256(path.read_bytes()).hexdigest() == source.sha256
                         for path, source in fin.sources.items()) and
                     all(_glob_matches(pattern) == matches for pattern, matches in fin.include_globs.items()))
    except OSError:
        unchanged = False
    require(unchanged, "INPUT_CHANGED", message, str(fin.root))


def _digest(sources: Mapping[Path, SourceFile], root: Path) -> str:
    manifest = [(path.relative_to(root.parent).as_posix()
                 if path.is_relative_to(root.parent) else path.as_posix(), source.sha256)
                for path, source in sorted(sources.items())]
    return sha256(json.dumps([BEANCOUNT_VERSION, manifest], ensure_ascii=False).encode()).hexdigest()


_FROZEN_FINANCIAL: ContextVar[Optional[FinancialInput]] = ContextVar("frozen_financial", default=None)
_FROZEN_THROUGH: ContextVar[Optional[date]] = ContextVar("frozen_through", default=None)
_BOOKED_THROUGH: ContextVar[Optional[date]] = ContextVar("booked_through", default=None)
_REPORT_INPUTS: ContextVar[Optional[Dict[Tuple[Path, Optional[date]], FinancialInput]]] = ContextVar(
    "report_inputs", default=None)


def _bounded(fin: FinancialInput) -> FinancialInput:
    through = _BOOKED_THROUGH.get()
    if through is None:
        return fin
    entries = fin.reference_entries if fin.reference_entries is not None else fin.entries
    return replace(fin, entries=tuple(entry for entry in entries if entry.date <= through),
                   reference_entries=entries)


def reference_financial(fin: FinancialInput) -> FinancialInput:
    """Restore frozen booked facts for reference lookup, never for report totals.

    Historical reports may share a review file containing later transactions.
    Retain those source identities so a known future claim is distinguishable
    from a stale reference, without replaying future cash or responsibility.
    """
    if fin.reference_entries is None:
        return fin
    inputs = _REPORT_INPUTS.get()
    complete = inputs.get((fin.root, None)) if inputs is not None else None
    if complete is not None and fin.reference_entries is complete.entries:
        return complete
    return replace(fin, entries=fin.reference_entries)


@contextmanager
def booked_through(day: date):
    """Audit a historical month without requiring future transactions to be migrated.

    Keep the complete source manifest and original file locations for stable
    booked references; only the booked directive stream is time bounded.
    """
    token = _BOOKED_THROUGH.set(day)
    try:
        yield
    finally:
        _BOOKED_THROUGH.reset(token)


def _load_financial_uncached(root: Path) -> FinancialInput:
    root = root.resolve()
    require(root.is_file(), "SOURCE_MISSING", "根账本文件不存在", str(root))
    # Beancount's public load_file may reuse a pickle cache created by another
    # version of the loader. Its timestamp check is not a source digest check,
    # and a stale cache can return different booked options for identical
    # independent copies. v6 must always parse the frozen source itself.
    sources, names, includes = _capture_sources(root)
    entries, errors, options = loader._uncached_load_file(
        str(root), None, None, None)
    _check_loader_errors(errors, root)
    require(not options.get("plugin"), "PLUGIN_UNSUPPORTED", "首个实现阶段暂不认证外部插件代码", str(root))
    require(set(options["include"]) == set(names), "INPUT_CHANGED",
            "加载期间 include 来源集合发生变化", str(root))
    for path, source in sources.items():
        for lineno, line in enumerate(source.lines, 1):
            require(not RESERVED_PUSHMETA.match(line), "META_INJECTION", "禁止用 pushmeta 注入预算保留键", f"{path}:{lineno}")
    from .chronology import order_entries
    fin = FinancialInput(root, tuple(order_entries(entries)), options, sources, _digest(sources, root),
                         BEANCOUNT_VERSION, source_names=names, include_globs=includes)
    _verify_financial(fin, "加载期间根账本或 include 来源发生变化")
    return fin


@contextmanager
def financial_session():
    """Share source inputs across the sequential stages of one report.

    Each root is captured once, with independent cutoff views and base-audit
    results. Nested stages join the owner, which verifies each source at exit.
    Nothing survives a reload, and standalone audits keep their existing checks.
    """
    if _REPORT_INPUTS.get() is not None:
        yield
        return
    inputs = {}
    token = _REPORT_INPUTS.set(inputs)
    try:
        yield
    finally:
        _REPORT_INPUTS.reset(token)
        for (root, through), fin in inputs.items():
            if through is not None:
                continue  # These views all derive from the same complete capture.
            _verify_financial(fin)


def _session_input(root: Path) -> FinancialInput:
    inputs = _REPORT_INPUTS.get()
    key = (root.resolve(), _BOOKED_THROUGH.get())
    if inputs is None:
        return _bounded(_load_financial_uncached(key[0]))
    complete_key = (key[0], None)
    if complete_key not in inputs:
        inputs[complete_key] = _load_financial_uncached(key[0])
    if key not in inputs:
        inputs[key] = _bounded(inputs[complete_key])
    return inputs[key]


def load_financial(root: Path) -> FinancialInput:
    """Read this report's source input; standalone calls always load afresh."""
    resolved = root.resolve()
    frozen = _FROZEN_FINANCIAL.get()
    if frozen is not None and frozen.root == resolved:
        return frozen
    return _session_input(resolved)


def shared_audit(audit):
    """Reuse a pure base audit within one frozen input, never across reloads.

    Only apply to audits whose dependencies are the frozen ledger and their
    explicit Path arguments. File contents are checked on every call; return
    independent copies so report annotations cannot mutate another consumer.
    Exceptions are not cached, and standalone calls keep their original path.
    """
    parameters = signature(audit)

    @wraps(audit)
    def run(*args, **kwargs):
        frozen = _FROZEN_FINANCIAL.get()
        if frozen is None:
            return audit(*args, **kwargs)
        arguments = parameters.bind(*args, **kwargs)
        arguments.apply_defaults()
        root = arguments.arguments["root"]
        if not isinstance(root, Path) or root.resolve() != frozen.root:
            return audit(*args, **kwargs)
        try:
            watched = {value.resolve(): sha256(value.read_bytes()).digest()
                       for name, value in arguments.arguments.items()
                       if name != "root" and isinstance(value, Path)}
            key = (audit, _BOOKED_THROUGH.get(),
                   tuple((name, value.resolve() if isinstance(value, Path) else value)
                         for name, value in arguments.arguments.items()), tuple(watched.items()))
            hash(key)
        except (OSError, TypeError):
            return audit(*args, **kwargs)  # Preserve the audit's own input diagnostics.
        if key in frozen._audit_results:
            return deepcopy(frozen._audit_results[key])
        result = audit(*args, **kwargs)
        for path, digest in watched.items():
            try:
                unchanged = sha256(path.read_bytes()).digest() == digest
            except OSError:
                unchanged = False
            require(unchanged, "INPUT_CHANGED", "基础审计期间配置或审阅文件发生变化", str(path))
        frozen._audit_results[key] = deepcopy(result)
        return result

    return run


@contextmanager
def frozen_financial(root: Path):
    """Share one booked source within a report, then verify it did not change.

    Content and include-graph checks at exit detect edited sources and newly
    matched files. Within a report session its owner performs that final check.
    """
    frozen = _FROZEN_FINANCIAL.get()
    if (frozen is not None and frozen.root == root.resolve() and
            _FROZEN_THROUGH.get() == _BOOKED_THROUGH.get()):
        # The outer owner still verifies the complete source at exit. A
        # different root or report cutoff must establish its own snapshot.
        yield frozen
        return
    fin = _session_input(root)
    token = _FROZEN_FINANCIAL.set(fin)
    through_token = _FROZEN_THROUGH.set(_BOOKED_THROUGH.get())
    try:
        yield fin
    finally:
        _FROZEN_THROUGH.reset(through_token)
        _FROZEN_FINANCIAL.reset(token)
        if _REPORT_INPUTS.get() is None:
            _verify_financial(fin)

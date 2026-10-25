"""Official Beancount adapter plus a frozen, read-only source manifest.

This module does not infer accounting facts from source text. It only retains
the lexical evidence that Beancount's booked AST may normalize away.
"""

from calendar import monthrange
from dataclasses import dataclass, replace
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import date
from functools import cached_property, wraps
from hashlib import sha256
from inspect import signature
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from beancount import __version__ as BEANCOUNT_VERSION, loader

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


@dataclass(frozen=True)
class FinancialInput:
    root: Path
    entries: Tuple[Any, ...]
    options: Mapping[str, Any]
    sources: Mapping[Path, SourceFile]
    digest: str
    loader_version: str
    reference_entries: Optional[Tuple[Any, ...]] = None

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


def _sources(root: Path, options: Mapping[str, Any]) -> Dict[Path, SourceFile]:
    names = {root.resolve()}
    names.update(Path(name).resolve() for name in options.get("include", []))
    result = {}
    for path in sorted(names):
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise BudgetError("SOURCE_MISSING", str(exc), str(path)) from exc
        result[path] = SourceFile(path, content, sha256(raw).hexdigest())
    return result


def _digest(sources: Mapping[Path, SourceFile], root: Path) -> str:
    manifest = [(path.relative_to(root.parent).as_posix()
                 if path.is_relative_to(root.parent) else path.as_posix(), source.sha256)
                for path, source in sorted(sources.items())]
    return sha256(json.dumps([BEANCOUNT_VERSION, manifest], ensure_ascii=False).encode()).hexdigest()


_FROZEN_FINANCIAL: ContextVar[Optional[FinancialInput]] = ContextVar("frozen_financial", default=None)
_FROZEN_THROUGH: ContextVar[Optional[date]] = ContextVar("frozen_through", default=None)
_BOOKED_THROUGH: ContextVar[Optional[date]] = ContextVar("booked_through", default=None)


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
    return replace(fin, entries=fin.reference_entries) if fin.reference_entries is not None else fin


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
    first_entries, first_errors, first_options = loader._uncached_load_file(
        str(root), None, None, None)
    if first_errors:
        code = "DUPLICATE_METADATA_KEY" if "Duplicate" in str(first_errors[0]) and "metadata field" in str(first_errors[0]) else "LOADER_ERROR"
        raise BudgetError(code, f"Beancount 加载失败：{first_errors[0]}", str(root))
    require(not first_options.get("plugin"), "PLUGIN_UNSUPPORTED", "首个实现阶段暂不认证外部插件代码", str(root))
    before = _sources(root, first_options)
    for path, source in before.items():
        for lineno, line in enumerate(source.lines, 1):
            require(not RESERVED_PUSHMETA.match(line), "META_INJECTION", "禁止用 pushmeta 注入预算保留键", f"{path}:{lineno}")
    second_entries, second_errors, second_options = loader._uncached_load_file(
        str(root), None, None, None)
    if second_errors:
        raise BudgetError("LOADER_ERROR", f"Beancount 再次加载失败：{second_errors[0]}", str(root))
    after = _sources(root, second_options)
    require({p: s.sha256 for p, s in before.items()} == {p: s.sha256 for p, s in after.items()},
            "INPUT_CHANGED", "加载期间根账本或 include 文件发生变化", str(root))
    require(len(first_entries) == len(second_entries), "INPUT_CHANGED", "加载期间财务指令集合发生变化", str(root))
    from .chronology import order_entries
    return FinancialInput(root, tuple(order_entries(second_entries)), second_options, after, _digest(after, root), BEANCOUNT_VERSION)


def load_financial(root: Path) -> FinancialInput:
    """Load independently, unless one report has explicitly frozen this root."""
    resolved = root.resolve()
    frozen = _FROZEN_FINANCIAL.get()
    if frozen is not None and frozen.root == resolved:
        return frozen
    return _bounded(_load_financial_uncached(resolved))


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

    Re-loading at exit detects edited source files, newly matched includes and
    loader changes before any report can be returned to the caller.
    """
    frozen = _FROZEN_FINANCIAL.get()
    if (frozen is not None and frozen.root == root.resolve() and
            _FROZEN_THROUGH.get() == _BOOKED_THROUGH.get()):
        # The outer owner still verifies the complete source at exit. A
        # different root or report cutoff must establish its own snapshot.
        yield frozen
        return
    fin = _bounded(_load_financial_uncached(root))
    token = _FROZEN_FINANCIAL.set(fin)
    through_token = _FROZEN_THROUGH.set(_BOOKED_THROUGH.get())
    try:
        yield fin
    finally:
        _FROZEN_THROUGH.reset(through_token)
        _FROZEN_FINANCIAL.reset(token)
        after = _load_financial_uncached(root)
        require(after.digest == fin.digest, "INPUT_CHANGED",
                "报表运行期间根账本或 include 来源发生变化", str(root))

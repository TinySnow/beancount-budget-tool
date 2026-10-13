"""Official Beancount adapter plus a frozen, read-only source manifest.

This module does not infer accounting facts from source text. It only retains
the lexical evidence that Beancount's booked AST may normalize away.
"""

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from beancount import __version__ as BEANCOUNT_VERSION, loader

from .errors import BudgetError, require
from .exact import Fraction, exact

PRICE = re.compile(r"(?<!@)@@?\s+([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+([A-Za-z][A-Za-z0-9_]*)")
RESERVED_PUSHMETA = re.compile(r"^\s*pushmeta\s+(?:budget|fund|tracking)\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    text: str
    sha256: str

    @property
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

    def line(self, filename: str, lineno: int) -> str:
        path = Path(filename).resolve()
        require(path in self.sources, "SOURCE_MISSING", "booked 事实引用了未冻结的来源", str(path))
        lines = self.sources[path].lines
        require(1 <= lineno <= len(lines), "SOURCE_MISSING", "来源行超出范围", f"{path}:{lineno}")
        return lines[lineno - 1]

    def raw_price(self, posting: Any) -> Tuple[str, Fraction, str]:
        """Return notation, unsigned raw price/total, currency or reject ambiguity."""
        meta = posting.meta or {}
        source = f"{meta.get('filename')}:{meta.get('lineno')}"
        require(isinstance(meta.get("filename"), str) and isinstance(meta.get("lineno"), int), "SOURCE_MISSING", "posting 缺少原文位置", source)
        line = self.line(meta["filename"], meta["lineno"]).split(";", 1)[0]
        matches = list(PRICE.finditer(line))
        require(len(matches) <= 1, "PRICE_SOURCE_CONFLICT", "一条 posting 有多个原文价格", source)
        if not matches:
            return ("none", Fraction(0), "")
        match = matches[0]
        notation = "@@" if line[match.start():match.start() + 2] == "@@" else "@"
        return (notation, abs(exact(match.group(1))), match.group(2))

    def transaction_text(self, filename: str, lineno: int) -> str:
        lines = self.sources[Path(filename).resolve()].lines
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
    manifest = [(str(path.relative_to(root.parent)) if path.is_relative_to(root.parent) else str(path), source.sha256)
                for path, source in sorted(sources.items())]
    return sha256(json.dumps([BEANCOUNT_VERSION, manifest], ensure_ascii=False).encode()).hexdigest()


_FROZEN_FINANCIAL: ContextVar[Optional[FinancialInput]] = ContextVar("frozen_financial", default=None)


def _load_financial_uncached(root: Path) -> FinancialInput:
    root = root.resolve()
    require(root.is_file(), "SOURCE_MISSING", "根账本文件不存在", str(root))
    first_entries, first_errors, first_options = loader.load_file(str(root))
    if first_errors:
        code = "DUPLICATE_METADATA_KEY" if "Duplicate" in str(first_errors[0]) and "metadata field" in str(first_errors[0]) else "LOADER_ERROR"
        raise BudgetError(code, f"Beancount 加载失败：{first_errors[0]}", str(root))
    require(not first_options.get("plugin"), "PLUGIN_UNSUPPORTED", "首个实现阶段暂不认证外部插件代码", str(root))
    before = _sources(root, first_options)
    for path, source in before.items():
        for lineno, line in enumerate(source.lines, 1):
            require(not RESERVED_PUSHMETA.match(line), "META_INJECTION", "禁止用 pushmeta 注入预算保留键", f"{path}:{lineno}")
    second_entries, second_errors, second_options = loader.load_file(str(root))
    if second_errors:
        raise BudgetError("LOADER_ERROR", f"Beancount 再次加载失败：{second_errors[0]}", str(root))
    after = _sources(root, second_options)
    require({p: s.sha256 for p, s in before.items()} == {p: s.sha256 for p, s in after.items()},
            "INPUT_CHANGED", "加载期间根账本或 include 文件发生变化", str(root))
    require(len(first_entries) == len(second_entries), "INPUT_CHANGED", "加载期间财务指令集合发生变化", str(root))
    return FinancialInput(root, tuple(second_entries), second_options, after, _digest(after, root), BEANCOUNT_VERSION)


def load_financial(root: Path) -> FinancialInput:
    """Load independently, unless one report has explicitly frozen this root."""
    resolved = root.resolve()
    frozen = _FROZEN_FINANCIAL.get()
    if frozen is not None and frozen.root == resolved:
        return frozen
    return _load_financial_uncached(resolved)


@contextmanager
def frozen_financial(root: Path):
    """Share one booked source within a report, then verify it did not change.

    Re-loading at exit detects edited source files, newly matched includes and
    loader changes before any report can be returned to the caller.
    """
    fin = _load_financial_uncached(root)
    token = _FROZEN_FINANCIAL.set(fin)
    try:
        yield fin
    finally:
        _FROZEN_FINANCIAL.reset(token)
        after = _load_financial_uncached(root)
        require(after.digest == fin.digest, "INPUT_CHANGED",
                "报表运行期间根账本或 include 来源发生变化", str(root))

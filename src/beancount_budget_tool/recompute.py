"""One caller's in-memory result, verified against exact inputs on every use."""

from copy import deepcopy
from pathlib import Path

from .financial import FinancialSourceCache, load_financial
from .query import QuerySession
from .operation import checkpoint
from .yamlstrict import config_path
from .v6.simple_config import _documents, _unchanged


class RecomputeCache:
    """Keep one complete public result, never a runtime or persistent approval.

    SourceCache verifies bytes and include-glob membership, including atomic
    replacements and equal-size/mtime edits. The complete ledger is captured
    before computing; the query session verifies it again before publishing.
    """
    def __init__(self, source_cache=None):
        self.source_cache = source_cache or FinancialSourceCache()
        self.clear()
        self.hits = self.misses = 0
        self.last_hit = False

    def clear(self):
        self._key = self._result = None
        self.last_hit = False

    def run(self, config, request, compute):
        config = Path(config).resolve()
        self.last_hit = False
        try:
            checkpoint('校验输入内容与 include')
            with QuerySession(source_cache=self.source_cache):
                inputs = {}
                document, _, _ = _documents(config, inputs)
                financial = load_financial(config_path(config, document['ledger'], 'ledger'))
                key = (str(config), request, financial.digest, financial.loader_version,
                       tuple((str(path), content) for path, content in inputs.items()))
                # Request keys are immutable and explicit, never callables or
                # object identity; query parameters cannot alias each other.
                hash(key)
                hit = key == self._key
                checkpoint('复用同一输入的完整结果' if hit else '重新审计')
                result = deepcopy(self._result) if hit else compute()
                _unchanged(inputs)
            # Session exit above validates source bytes/glob members again.
            # An exception, cancellation or racing save never publishes/cache.
            checkpoint()
            if hit:
                self.hits += 1
            else:
                self.misses += 1
                self._result = deepcopy(result)
                self._key = key
            self.last_hit = hit
            return result
        except BaseException:
            self.clear()
            raise

"""Cheap input-change detection; every detected change runs a fresh full audit."""

import glob
import os
from pathlib import Path
import time

from beancount.parser import parser

from ..errors import BudgetError
from ..yamlstrict import config_path, load_yaml


class InputWatch:
    def __init__(self, configs):
        self.configs = [Path(path).resolve() for path in configs]
        self.cache = {}
        self.roots = set()
        self.previous = self.snapshot()
        self.pending = None
        self.changed_at = 0

    def snapshot(self):
        files, roots = set(self.configs), set(self.roots)
        for config in self.configs:
            try:
                document = load_yaml(config)
                if not isinstance(document, dict):
                    continue
                for key, value in document.items():
                    if key in {"ledger", "budgets"} or isinstance(value, str) and value.endswith((".yaml", ".yml")):
                        if isinstance(value, str):
                            path = config_path(config, value, key)
                            files.add(path)
                            if key == "ledger":
                                roots.add(path)
            except (BudgetError, OSError, ValueError, TypeError):
                pass
        self.roots = roots
        queue = list(roots)
        seen = set()
        while queue:
            path = queue.pop()
            if path in seen:
                continue
            seen.add(path)
            files.add(path)
            stamp = self.stamp(path)
            cached = self.cache.get(path)
            if cached is None or cached[0] != stamp:
                try:
                    _, _, options = parser.parse_file(str(path))
                    includes = tuple(options.get("include", ()))
                except (OSError, ValueError):
                    includes = cached[1] if cached else ()
                self.cache[path] = stamp, includes
            for pattern in self.cache[path][1]:
                absolute = str(path.parent / pattern)
                matches = glob.glob(absolute, recursive=True)
                queue.extend(Path(os.path.abspath(name)) for name in matches)
                if not matches and not glob.has_magic(absolute):
                    files.add(Path(os.path.abspath(absolute)))
        # Pick up atomic saves and new configs without watching
        # generated temporary runtimes or a whole unrelated parent directory.
        for config in self.configs:
            files.update(config.parent.glob("*.y*ml"))
        return {str(path): self.stamp(path) for path in files}

    @staticmethod
    def stamp(path):
        try:
            value = path.stat()
            return value.st_mtime_ns, value.st_ctime_ns, value.st_size, os.path.realpath(path)
        except OSError:
            return None

    def changed(self):
        """Debounce saves so an editor's intermediate file is not published."""
        current = self.snapshot()
        if current == self.previous:
            self.pending = None
            return False
        if current != self.pending:
            self.pending, self.changed_at = current, time.monotonic()
            return False
        if time.monotonic() - self.changed_at < 0.25:
            return False
        self.previous, self.pending = current, None
        return True

    def accept(self):
        self.previous, self.pending = self.snapshot(), None

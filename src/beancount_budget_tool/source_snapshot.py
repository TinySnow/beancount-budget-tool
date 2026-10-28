"""Parse frozen source bytes once, then use the official accounting pipeline.

The source manifest and raw posting positions come from the same parser pass
that feeds booking. No disk pickle, text accounting parser or patched loader
global is involved. Include traversal follows the pinned loader's FIFO order,
filename aliases and recursive glob semantics.
"""

from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import glob
import os
from pathlib import Path
import sys

from beancount import loader
from beancount.core import data
from beancount.parser import parser

from .errors import BudgetError, require


@dataclass
class ParsedSnapshot:
    sources: dict
    names: dict
    includes: dict
    entries: list
    errors: list
    options: dict


def capture(root: Path, source_type) -> ParsedSnapshot:
    sources, names, includes = {}, {}, {}
    entries, errors, option_maps = [], [], []
    pending = deque([str(root)])
    while pending:
        name = os.path.normpath(pending.popleft())
        if name in names:
            errors.append(loader.LoadError(data.new_metadata('<load>', 0),
                                          f'Duplicate filename parsed: "{name}"'))
            continue
        path = Path(name).resolve()
        names[name] = path
        if path not in sources:
            try:
                raw = path.read_bytes()
                sources[path] = source_type(path, raw.decode('utf-8-sig'), sha256(raw).hexdigest())
            except (OSError, UnicodeError) as exc:
                raise BudgetError('SOURCE_MISSING', str(exc), name) from exc
        source = sources[path]
        parsed, parse_errors, options = parser.parse_string(source.text, report_filename=name)
        entries.extend(parsed)
        errors.extend(parse_errors)
        option_maps.append(options)
        # Read-only lexical evidence survives booking without retaining a
        # second raw AST. SourceFile's fallback parser is for standalone text.
        source.__dict__.setdefault('_syntax', (
            {entry.meta['lineno']: tuple(p.meta['lineno'] for p in entry.postings)
             for entry in parsed if isinstance(entry, data.Transaction)},
            tuple(options['include']), tuple(parse_errors)))
        for pattern in options['include']:
            search = pattern if os.path.isabs(pattern) else os.path.join(os.path.dirname(name), pattern)
            matches = tuple(os.path.normpath(p) for p in glob.glob(search, recursive=True))
            includes[search] = matches
            if not matches:
                errors.append(loader.LoadError(data.new_metadata('<load>', 0),
                                              f'File glob "{pattern}" does not match any files'))
            pending.extend(matches)
    options = option_maps[0]
    options['include'] = sorted(names)
    options = loader.aggregate_options_map(options, option_maps[1:])
    return ParsedSnapshot(sources, names, includes, entries, errors, options)


def book(snapshot: ParsedSnapshot):
    """Apply the exact pinned loader stages to already captured parser output."""
    options = snapshot.options
    require(not options.get('plugin'), 'PLUGIN_UNSUPPORTED',
            '首个实现阶段暂不认证外部插件代码', next(iter(snapshot.names)))
    entries = sorted(snapshot.entries, key=data.entry_sortkey)
    entries, booking_errors = loader.booking.book(entries, options)
    parse_errors = [*snapshot.errors, *booking_errors]
    saved_pythonpath = list(sys.path)
    try:
        if 'pythonpath' in options:
            sys.path[0:0] = options['pythonpath']
        entries, errors = loader.run_transformations(entries, parse_errors, options, None)
    finally:
        sys.path[:] = saved_pythonpath
    errors.extend(loader.validation.validate(entries, options, None, None))
    options['input_hash'] = loader.compute_input_hash(options['include'])
    return entries, errors, options

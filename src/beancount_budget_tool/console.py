"""Keep redirected Windows command output readable for UTF-8 ledger reviews."""

import sys


def configure_output() -> None:
    """Use UTF-8 for pipes/files; leave interactive console handling intact."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

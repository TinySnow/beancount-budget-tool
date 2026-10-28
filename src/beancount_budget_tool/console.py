"""UTF-8 console output and runnable instructions for installed/frozen tools."""

import sys


def command(name: str) -> str:
    """Render runnable worksheet instructions for installed or frozen tools."""
    if not getattr(sys, "frozen", False):
        return name
    action = "" if name == "beancount-budget-tool" else name.removeprefix("beancount-budget-")
    prefix = "& " if sys.platform == "win32" else ""
    return f'{prefix}"{sys.executable}"' + (f" {action}" if action else "")


def configure_output() -> None:
    """Use UTF-8 for pipes/files; leave interactive console handling intact."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

"""Backend log feed for the dashboard's "BACKEND" pane.

Keeps the last few hundred server-side lines in memory: every HTTP request
(method, path, status, time taken) plus Python ``logging`` warnings/errors
from Sara's modules. The UI polls ``/logs/backend?after=<id>``.
Secrets are never logged here: query strings are dropped from request lines.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

_MAX = 600


@dataclass
class Entry:
    id: int
    ts: float
    level: str      # INFO | WARNING | ERROR
    source: str     # "http" or the logger name
    message: str


_lock = threading.Lock()
_entries: deque[Entry] = deque(maxlen=_MAX)
_next_id = 1


def add(level: str, source: str, message: str) -> None:
    global _next_id
    with _lock:
        _entries.append(Entry(_next_id, time.time(), level, source, message[:500]))
        _next_id += 1


def since(after_id: int = 0, limit: int = 300) -> list[dict]:
    with _lock:
        out = [asdict(e) for e in _entries if e.id > after_id]
    return out[-limit:]


def request(method: str, path: str, status: int, ms: float) -> None:
    level = "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO"
    add(level, "http", f"{method} {path} {status} {ms:.0f}ms")


# Chatty third-party loggers: only their warnings and errors are interesting.
_NOISY = ("httpx", "httpcore", "urllib3", "asyncio", "multipart", "PIL", "matplotlib", "websockets", "aiohttp")


class _Handler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and record.name.startswith(_NOISY):
            return
        if record.name == "uvicorn.access":     # requests are logged by the middleware (with timing)
            return
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                e = record.exc_info[1]
                msg += f" | {type(e).__name__}: {e}"
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        level = "ERROR" if record.levelno >= logging.ERROR else "WARNING" if record.levelno >= logging.WARNING else "INFO"
        add(level, record.name, msg)


_installed = False


def install() -> None:
    """Capture Python logging (root + uvicorn) into the feed. Safe to call twice."""
    global _installed
    if _installed:
        return
    h = _Handler(level=logging.INFO)
    logging.getLogger().addHandler(h)          # warnings/errors from every library
    pkg = logging.getLogger(__package__ or "my_python_project")
    if pkg.level == logging.NOTSET:
        pkg.setLevel(logging.INFO)             # plus Sara's own INFO lines
    # uvicorn's own loggers don't propagate to root.
    logging.getLogger("uvicorn.error").addHandler(h)
    _installed = True
    add("INFO", "server", "Backend log started")

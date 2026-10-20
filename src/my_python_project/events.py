from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import time


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    kind: str
    message: str


_lock = Lock()
_events: list[Event] = []
_next_id = 1


def emit(kind: str, message: str) -> Event:
    global _next_id
    with _lock:
        ev = Event(id=_next_id, ts=time(), kind=kind, message=message)
        _next_id += 1
        _events.append(ev)
        # keep last 500 events
        if len(_events) > 500:
            del _events[: len(_events) - 500]
        return ev


def list_events(after_id: int = 0) -> list[Event]:
    with _lock:
        if after_id <= 0:
            return list(_events)
        return [e for e in _events if e.id > after_id]


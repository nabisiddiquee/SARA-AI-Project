"""Tiny thread-safe TTL cache (per process). Swap for Redis when scaling out."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Hashable


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 2000) -> None:
        self.ttl, self.max_items = ttl, max_items
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if not hit:
                return None
            if time.monotonic() - hit[0] > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return hit[1]

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)

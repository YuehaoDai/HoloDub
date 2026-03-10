from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class ModelRegistry:
    """A tiny lazy-loading cache for heavy model objects."""

    def __init__(self) -> None:
        self._items: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get_or_load(self, key: str, loader: Callable[[], Any]) -> Any:
        with self._lock:
            if key not in self._items:
                self._items[key] = loader()
            return self._items[key]

    def status(self) -> list[str]:
        with self._lock:
            return sorted(self._items.keys())

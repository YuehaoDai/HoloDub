"""LRU-based model registry for the ml-service.

Heavy ML models (Whisper, Pyannote, IndexTTS2 components) are expensive to
load (multi-GB downloads, multi-second initialisation, multi-GB GPU
allocation). The previous implementation was a single ``dict`` that
permanently retained every loaded model, which caused VRAM growth without
bound when multiple backends or model variants were used in one process.

This rewrite keeps the same simple API but:

  * tracks insertion / access order so we can evict the least-recently-used
    model when an explicit ``max_models`` budget is hit;
  * lets operators force-unload a specific model via :meth:`unload` (used
    by an admin endpoint exposed in the future);
  * runs each loader inside a per-key ``threading.Lock`` so concurrent
    requests for the same model do not duplicate the load (the previous
    implementation already did this for the whole registry, here we make
    it per-key for finer parallelism).
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Lazy-loading cache for heavy model objects with LRU eviction."""

    def __init__(self, max_models: int | None = None) -> None:
        """:param max_models: optional cap on the number of resident models.

        ``None`` (the default) disables LRU eviction so behaviour matches
        the historical no-eviction registry. Pass an integer to enforce a
        ceiling: when adding a new model would exceed it, the
        least-recently-accessed entry is unloaded first (and the loader's
        result is logged for traceability).
        """
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._key_locks: dict[str, threading.Lock] = {}
        self._index_lock = threading.Lock()
        self.max_models = max_models

    # ------------------------------------------------------------------
    # Loading

    def get_or_load(self, key: str, loader: Callable[[], Any]) -> Any:
        """Return the cached value for ``key`` or call ``loader`` to build it.

        Concurrent calls for the same ``key`` are serialised behind a
        per-key ``threading.Lock`` so the loader fires at most once.
        Concurrent calls for *different* keys proceed in parallel.
        """
        # Cheap fast-path: already loaded.
        with self._index_lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
            lock = self._key_locks.setdefault(key, threading.Lock())

        with lock:
            with self._index_lock:
                if key in self._items:
                    self._items.move_to_end(key)
                    return self._items[key]
            value = loader()
            with self._index_lock:
                self._items[key] = value
                self._items.move_to_end(key)
                self._evict_if_needed()
            logger.info("model_registry: loaded key=%s", key)
            return value

    def _evict_if_needed(self) -> None:
        """Caller MUST hold ``self._index_lock``."""
        if self.max_models is None or len(self._items) <= self.max_models:
            return
        while len(self._items) > self.max_models:
            evict_key, _ = self._items.popitem(last=False)
            self._key_locks.pop(evict_key, None)
            logger.info("model_registry: evicted key=%s due to max_models", evict_key)

    # ------------------------------------------------------------------
    # Introspection / mutation

    def status(self) -> list[str]:
        with self._index_lock:
            return list(self._items.keys())

    def unload(self, key: str) -> bool:
        """Forcibly evict ``key``. Returns True if it was loaded.

        The actual GPU memory release depends on whether the loaded value
        held the only reference to its tensors. For PyTorch modules
        callers typically follow up with ``torch.cuda.empty_cache()``,
        but doing so unconditionally hurts throughput so we leave it to
        the operator-facing endpoint.
        """
        with self._index_lock:
            if key not in self._items:
                return False
            del self._items[key]
            self._key_locks.pop(key, None)
            logger.info("model_registry: unloaded key=%s on request", key)
            return True

    def clear(self) -> int:
        """Drop every cached model. Returns the number of evicted entries."""
        with self._index_lock:
            count = len(self._items)
            self._items.clear()
            self._key_locks.clear()
            return count

from __future__ import annotations

import copy
import hashlib
import pickle
import threading
import time
from collections import OrderedDict
from typing import Callable, Generic, Hashable, TypeVar

from .redis_runtime import redis_available, redis_del, redis_get_pickle, redis_set_pickle, redis_setnx


T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: float, maxsize: int = 128, namespace: str = "ttl") -> None:
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self.maxsize = max(int(maxsize), 1)
        self.namespace = namespace
        self._lock = threading.Lock()
        self._items: OrderedDict[Hashable, tuple[float, T]] = OrderedDict()
        self._inflight: dict[Hashable, threading.Event] = {}
        self._refreshing: dict[Hashable, threading.Event] = {}

    def get_or_set(self, key: Hashable, factory: Callable[[], T]) -> T:
        redis_key = self._redis_key(key)
        cached = self._get_fresh(key, redis_key)
        if cached is not None:
            return cached
        leader = False
        event: threading.Event | None = None
        with self._lock:
            cached = self._get_fresh_local_locked(key)
            if cached is not None:
                return cached
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                leader = True
        if not leader and event is not None:
            event.wait(timeout=self._singleflight_wait_seconds())
            cached = self._get_fresh(key, redis_key)
            if cached is not None:
                return cached
            return copy.deepcopy(factory())
        lock_key = f"{redis_key}:lock"
        redis_leader = False
        if redis_available():
            redis_leader = redis_setnx(lock_key, "1", self._lock_ttl_seconds())
            if not redis_leader:
                cached = self._wait_for_redis_value(key, redis_key)
                if cached is not None:
                    self._finish_inflight(key, event)
                    return cached
        try:
            value = factory()
            self._store(key, redis_key, value)
            return copy.deepcopy(value)
        finally:
            if redis_leader:
                redis_del(lock_key)
            self._finish_inflight(key, event)

    def get_stale(self, key: Hashable) -> T | None:
        redis_key = self._redis_key(key)
        if redis_available():
            cached = redis_get_pickle(redis_key)
            if cached is not None:
                return copy.deepcopy(cached)
        with self._lock:
            cached = self._items.get(key)
            if not cached:
                return None
            self._items.move_to_end(key)
            return copy.deepcopy(cached[1])

    def get_or_set_stale_while_revalidate(self, key: Hashable, factory: Callable[[], T]) -> T:
        redis_key = self._redis_key(key)
        cached = self._get_fresh(key, redis_key)
        if cached is not None:
            return cached
        stale = self._get_stale_local(key)
        if stale is not None:
            self._refresh_in_background(key, redis_key, factory)
            return stale
        return self.get_or_set(key, factory)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _get_fresh(self, key: Hashable, redis_key: str) -> T | None:
        if redis_available():
            cached = redis_get_pickle(redis_key)
            if cached is not None:
                return copy.deepcopy(cached)
        with self._lock:
            return self._get_fresh_local_locked(key)

    def _get_fresh_local_locked(self, key: Hashable) -> T | None:
        cached = self._items.get(key)
        if not cached:
            return None
        if time.monotonic() - cached[0] > self.ttl_seconds:
            return None
        self._items.move_to_end(key)
        return copy.deepcopy(cached[1])

    def _store(self, key: Hashable, redis_key: str, value: T) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), copy.deepcopy(value))
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)
        if redis_available():
            redis_set_pickle(redis_key, value, max(int(self.ttl_seconds), 1))

    def _get_stale_local(self, key: Hashable) -> T | None:
        with self._lock:
            cached = self._items.get(key)
            if not cached:
                return None
            self._items.move_to_end(key)
            return copy.deepcopy(cached[1])

    def _refresh_in_background(self, key: Hashable, redis_key: str, factory: Callable[[], T]) -> None:
        with self._lock:
            if key in self._refreshing:
                return
            event = threading.Event()
            self._refreshing[key] = event

        def refresh() -> None:
            redis_leader = False
            lock_key = f"{redis_key}:lock"
            try:
                if redis_available():
                    redis_leader = redis_setnx(lock_key, "1", self._lock_ttl_seconds())
                    if not redis_leader:
                        return
                value = factory()
                self._store(key, redis_key, value)
            except Exception:
                pass
            finally:
                if redis_leader:
                    redis_del(lock_key)
                with self._lock:
                    self._refreshing.pop(key, None)
                    event.set()

        threading.Thread(target=refresh, name=f"{self.namespace}-stale-refresh", daemon=True).start()

    def _wait_for_redis_value(self, key: Hashable, redis_key: str) -> T | None:
        deadline = time.monotonic() + self._singleflight_wait_seconds()
        while time.monotonic() < deadline:
            time.sleep(0.05)
            cached = self._get_fresh(key, redis_key)
            if cached is not None:
                return cached
        return None

    def _finish_inflight(self, key: Hashable, event: threading.Event | None) -> None:
        if event is None:
            return
        with self._lock:
            self._inflight.pop(key, None)
            event.set()

    def _singleflight_wait_seconds(self) -> float:
        return min(max(self.ttl_seconds, 0.5), 8.0)

    def _lock_ttl_seconds(self) -> int:
        return max(int(self._singleflight_wait_seconds() * 2), 2)

    def _redis_key(self, key: Hashable) -> str:
        digest = hashlib.sha1(pickle.dumps(key)).hexdigest()
        return f"ai-option:ttl:{self.namespace}:{digest}"

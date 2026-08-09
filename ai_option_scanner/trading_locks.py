"""Per-run advisory lock shared by the order monitor and user-triggered
trade-instance actions (flatten / cancel / reset).

The monitor and the API run in different threads (and, in production, different
nodes). Both load a run, mutate its orders/instance JSON, and write the whole
blob back with a last-writer-wins UPDATE. Without a shared lock keyed on the
run, a manual flatten and a monitor cycle can clobber each other — re-selling
the same contract, or erasing freshly-armed protection. This lock serializes
all broker-affecting work for one run_id.

Redis-backed when available (cross-node); falls back to a process-local lock
registry otherwise (still serializes within a single worker).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from .redis_runtime import redis_available, redis_del, redis_setnx

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_GUARD = threading.Lock()
_LOCK_TTL_SECONDS = 120


def _run_lock_key(run_id: str) -> str:
    return f"ai-option:trading-run-action:{run_id}"


def _local_lock(run_id: str) -> threading.Lock:
    with _REGISTRY_GUARD:
        lock = _LOCAL_LOCKS.get(run_id)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[run_id] = lock
        return lock


@contextmanager
def run_action_lock(run_id: str, *, timeout_seconds: float = 15.0) -> Iterator[bool]:
    """Acquire the per-run action lock. Yields True if acquired, False otherwise.

    Always yields (never blocks forever); callers decide what to do when the
    lock could not be taken. The process-local lock is always taken first so two
    threads in the same worker can never interleave; Redis adds cross-node
    exclusion when present.
    """
    if not run_id:
        yield True
        return
    local = _local_lock(run_id)
    acquired_local = local.acquire(timeout=max(0.1, timeout_seconds))
    redis_key = _run_lock_key(run_id)
    acquired_redis = False
    try:
        if acquired_local and redis_available():
            acquired_redis = bool(redis_setnx(redis_key, "1", _LOCK_TTL_SECONDS))
            if not acquired_redis:
                # Another node holds it.
                yield False
                return
        yield acquired_local
    finally:
        if acquired_redis:
            try:
                redis_del(redis_key)
            except Exception:
                pass
        if acquired_local:
            local.release()

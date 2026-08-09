from __future__ import annotations

import os
import socket
import time

from .redis_runtime import redis_available, redis_blpop, redis_del, redis_lpop, redis_rpush, redis_setnx


SCAN_QUEUE_KEY = os.getenv("AI_OPTION_REDIS_SCAN_QUEUE", "ai-option:scan-queue")
SCAN_REQUEUE_LOCK = "ai-option:scan-requeue-lock"


def redis_queue_enabled() -> bool:
    return redis_available()


def enqueue_scan(scan_id: str) -> bool:
    if not redis_queue_enabled():
        return False
    redis_rpush(SCAN_QUEUE_KEY, scan_id)
    return True


def pop_scan(timeout: int = 5) -> str | None:
    if not redis_queue_enabled():
        return None
    # Use blocking BLPOP — waits server-side, eliminates 500ms polling loop
    return redis_blpop(SCAN_QUEUE_KEY, timeout=max(int(timeout), 1))


def acquire_requeue_lock(ttl_seconds: int = 30) -> bool:
    holder = f"{socket.gethostname()}:{os.getpid()}:{int(time.time())}"
    return redis_setnx(SCAN_REQUEUE_LOCK, holder, ttl_seconds)


def release_requeue_lock() -> None:
    redis_del(SCAN_REQUEUE_LOCK)

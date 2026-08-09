from __future__ import annotations

import json
import os
import pickle
import threading
import time
from functools import lru_cache
from typing import Any

from .concurrency import env_int

# Cache for redis_available() — ping once per 2s instead of on every call
_redis_available_ts: float = 0.0
_redis_available_result: bool = False
_redis_available_lock = threading.Lock()


def redis_url() -> str | None:
    return os.getenv("AI_OPTION_REDIS_URL") or os.getenv("REDIS_URL")


def redis_enabled() -> bool:
    return bool(redis_url())


def redis_available() -> bool:
    global _redis_available_ts, _redis_available_result
    now = time.monotonic()
    # Fast path: return cached result within 2-second window
    if now - _redis_available_ts < 2.0:
        return _redis_available_result
    with _redis_available_lock:
        # Re-check after acquiring lock (another thread may have updated)
        if now - _redis_available_ts < 2.0:
            return _redis_available_result
        client = redis_client()
        if client is None:
            if redis_enabled():
                _reset_redis_client()
            result = False
        else:
            try:
                client.ping()
                result = True
            except Exception:
                _reset_redis_client()
                result = False
        _redis_available_ts = time.monotonic()
        _redis_available_result = result
        return result


def process_role() -> str:
    return (os.getenv("AI_OPTION_PROCESS_ROLE") or "all").strip().lower() or "all"


def worker_enabled() -> bool:
    return process_role() in {"all", "worker"}


def web_enabled() -> bool:
    return process_role() in {"all", "web"}


@lru_cache(maxsize=1)
def redis_client() -> Any | None:
    url = redis_url()
    if not url:
        return None
    try:
        import redis
    except ImportError:
        return None
    client = redis.Redis.from_url(
        url,
        decode_responses=False,
        health_check_interval=30,
        retry_on_timeout=True,
        socket_connect_timeout=3,
        socket_timeout=15,
        max_connections=env_int("AI_OPTION_REDIS_MAX_CONNECTIONS", 16, 1, 256),
    )
    try:
        client.ping()
    except Exception:
        return None
    return client


def redis_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    client = redis_client()
    if client is None:
        return
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    try:
        client.setex(key, max(int(ttl_seconds), 1), payload)
    except Exception:
        _reset_redis_client()


def redis_get_json(key: str) -> Any | None:
    client = redis_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception:
        _reset_redis_client()
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        return None


def redis_set_pickle(key: str, value: Any, ttl_seconds: int) -> None:
    client = redis_client()
    if client is None:
        return
    try:
        client.setex(key, max(int(ttl_seconds), 1), pickle.dumps(value))
    except Exception:
        _reset_redis_client()


def redis_get_pickle(key: str) -> Any | None:
    client = redis_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception:
        _reset_redis_client()
        return None
    if raw is None:
        return None
    try:
        return pickle.loads(raw)
    except Exception:
        return None


def redis_rpush(key: str, value: str) -> None:
    client = redis_client()
    if client is None:
        return
    try:
        client.rpush(key, value)
    except Exception:
        _reset_redis_client()


def redis_brpop(key: str, timeout: int = 5) -> str | None:
    client = redis_client()
    if client is None:
        return None
    try:
        result = client.brpop(key, timeout=timeout)
    except Exception:
        _reset_redis_client()
        return None
    if not result:
        return None
    _, raw = result
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def redis_lpop(key: str) -> str | None:
    client = redis_client()
    if client is None:
        return None
    try:
        raw = client.lpop(key)
    except Exception:
        _reset_redis_client()
        return None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def redis_llen(key: str) -> int | None:
    client = redis_client()
    if client is None:
        return None
    try:
        return int(client.llen(key))
    except Exception:
        _reset_redis_client()
        return None


def redis_setnx(name: str, value: str, ttl_seconds: int) -> bool:
    client = redis_client()
    if client is None:
        return False
    try:
        return bool(client.set(name, value, nx=True, ex=max(int(ttl_seconds), 1)))
    except Exception:
        _reset_redis_client()
        return False


def redis_eval(script: str, keys: list[str], args: list[str]) -> Any | None:
    client = redis_client()
    if client is None:
        return None
    try:
        return client.eval(script, len(keys), *(keys + args))
    except Exception:
        _reset_redis_client()
        return None


def redis_del(name: str) -> None:
    client = redis_client()
    if client is None:
        return
    try:
        client.delete(name)
    except Exception:
        _reset_redis_client()


def redis_pool_snapshot() -> dict[str, Any]:
    client = redis_client()
    if client is None:
        return {"enabled": False, "max_connections": 0, "in_use": 0, "available": 0}
    pool = getattr(client, "connection_pool", None)
    in_use = 0
    available = 0
    max_connections = 0
    if pool is not None:
        try:
            in_use = len(getattr(pool, "_in_use_connections", []) or [])
            available = len(getattr(pool, "_available_connections", []) or [])
            max_connections = int(getattr(pool, "max_connections", 0) or 0)
        except Exception:
            pass
    return {
        "enabled": True,
        "max_connections": max_connections,
        "in_use": in_use,
        "available": available,
    }


def redis_sadd(name: str, *values: str) -> int:
    client = redis_client()
    if client is None:
        return 0
    try:
        return int(client.sadd(name, *values))
    except Exception:
        _reset_redis_client()
        return 0


def redis_smembers(name: str) -> set[str]:
    client = redis_client()
    if client is None:
        return set()
    try:
        values = client.smembers(name)
    except Exception:
        _reset_redis_client()
        return set()
    return {value.decode("utf-8") if isinstance(value, bytes) else str(value) for value in values}


def redis_blpop(key: str, timeout: int = 5) -> str | None:
    """Blocking left-pop — FIFO counterpart to redis_rpush for the scan queue."""
    client = redis_client()
    if client is None:
        return None
    try:
        result = client.blpop(key, timeout=timeout)
    except Exception:
        _reset_redis_client()
        return None
    if not result:
        return None
    _, raw = result
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def redis_publish(channel: str, message: str) -> int:
    """Publish a message to a pub/sub channel. Best-effort: returns the number
    of subscribers that received it, or 0 if Redis is unavailable. Never raises
    so a failed publish can't break the caller's primary work (status writes)."""
    client = redis_client()
    if client is None:
        return 0
    try:
        return int(client.publish(channel, message.encode("utf-8")))
    except Exception:
        _reset_redis_client()
        return 0


def redis_subscribe(channel: str, *, timeout: float = 1.0):
    """Subscribe to a channel and yield decoded message strings as they arrive.

    Yields None on each idle `timeout` tick (no message) so the caller can do
    periodic work — emit keep-alive frames, check for client disconnect, honor
    a deadline. Ends the generator if Redis is unavailable or errors, so the
    caller can fall back to polling. The pubsub connection is always closed."""
    client = redis_client()
    if client is None:
        return
    pubsub = None
    try:
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel)
        while True:
            message = pubsub.get_message(timeout=timeout)
            if message is None:
                yield None
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                yield data.decode("utf-8")
            elif data is not None:
                yield str(data)
    except GeneratorExit:
        raise
    except Exception:
        _reset_redis_client()
        return
    finally:
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                pass


def _reset_redis_client() -> None:
    global _redis_available_ts
    redis_client.cache_clear()
    # Force re-ping on next redis_available() call after a connection reset
    _redis_available_ts = 0.0
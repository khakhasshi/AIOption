"""Scheduler thread for the fully-automatic LLM trading feature.

Polls every ~30s for due auto-trade instances (status=active AND
next_run_at<=now) and runs one cycle each. A Redis leader-lock makes only one
node fire per tick (so the 3-node fleet doesn't run duplicate cycles); the
cycle engine additionally advances next_run_at first so a crash can't hot-loop.
Gated by AI_OPTION_ENABLE_AUTO_TRADE_SCHEDULER (worker) + the feature-wide
AI_OPTION_AUTO_TRADE_ENABLED kill-switch (checked inside the cycle).
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from typing import Any

from .account_store import utc_now
from .auto_trade_loop import auto_trade_enabled, run_auto_trade_cycle
from .auto_trade_store import list_due_auto_trade_instances
from .redis_runtime import redis_available, redis_eval, redis_setnx

_LOCK_KEY = "ai-option:auto-trade-scheduler-lock"
# Renew the leader lock if-and-only-if we still own it (token match), and release
# the same way, so a node whose lock already lapsed cannot extend or delete a lock
# another node has since acquired.
_RENEW_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_LOCK_TTL_SECONDS = 45
_started = False
_start_lock = threading.Lock()
_runtime_lock = threading.Lock()
_runtime: dict[str, Any] = {"started": False, "started_at": None, "status": "idle", "last_tick_at": None, "last_error": None, "last_checked": 0}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(os.getenv(name) or default), hi))
    except (TypeError, ValueError):
        return default


def start_auto_trade_scheduler() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        _runtime["started"] = True
        _runtime["started_at"] = utc_now()
    threading.Thread(target=_loop, name="auto-trade-scheduler", daemon=True).start()


def _loop() -> None:
    interval = _env_int("AI_OPTION_AUTO_TRADE_SCHEDULER_SECONDS", 30, 5, 300)
    while True:
        _tick(limit=20)
        time.sleep(interval)


def _tick(limit: int = 20) -> dict[str, Any]:
    if not auto_trade_enabled():
        with _runtime_lock:
            _runtime.update({"status": "disabled", "last_tick_at": utc_now()})
        return {"skipped": "disabled"}
    has_redis = redis_available()
    # Leader-lock: only one node fires per tick. A unique token lets us safely
    # renew the TTL between instances (a batch of slow LLM cycles can exceed the
    # base TTL) and release only our own lock — without a token, an expired-then-
    # reacquired lock could be deleted by the wrong node, letting two nodes run the
    # same due instances and double-submit. Each instance's next_run_at is the
    # backstop idempotency.
    lock_token = uuid.uuid4().hex
    if has_redis and not redis_setnx(_LOCK_KEY, lock_token, _LOCK_TTL_SECONDS):
        return {"skipped": "not_leader"}
    processed: list[dict[str, Any]] = []
    try:
        for instance in list_due_auto_trade_instances(limit=limit):
            try:
                summary = run_auto_trade_cycle(instance)
                processed.append({"id": instance.get("id"), "summary": summary})
            except Exception as exc:  # noqa: BLE001 - one bad instance must not kill the tick.
                traceback.print_exc(limit=6)
                processed.append({"id": instance.get("id"), "error": str(exc)[:200]})
            # Renew our leadership after each (potentially slow) cycle so the lock
            # doesn't lapse mid-batch and let another node start the same instances.
            if has_redis:
                try:
                    redis_eval(_RENEW_LUA, [_LOCK_KEY], [lock_token, str(_LOCK_TTL_SECONDS)])
                except Exception:  # noqa: BLE001
                    pass
        with _runtime_lock:
            _runtime.update({"status": "ok", "last_tick_at": utc_now(), "last_error": None, "last_checked": len(processed)})
        return {"checked_count": len(processed), "processed": processed}
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(limit=6)
        with _runtime_lock:
            _runtime.update({"status": "error", "last_tick_at": utc_now(), "last_error": str(exc)[:200]})
        return {"error": str(exc)}
    finally:
        if has_redis:
            try:
                # Release only if we still own it (token match).
                redis_eval(_RELEASE_LUA, [_LOCK_KEY], [lock_token])
            except Exception:
                pass


def auto_trade_scheduler_runtime_snapshot() -> dict[str, Any]:
    with _runtime_lock:
        return dict(_runtime)

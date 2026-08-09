from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any

from .observation_store import process_notification_events, prune_observation_history
from .longbridge_sdk_client import refresh_all_sdk_account_tokens


_started = False
_lock = threading.Lock()


def start_notification_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_notification_loop, name="notification-worker-loop", daemon=True).start()


def run_notification_worker_once(limit: int | None = None, retry_after_seconds: int | None = None, max_attempts: int | None = None) -> dict[str, Any]:
    return process_notification_events(
        limit=limit or _env_int("AI_OPTION_NOTIFICATION_WORKER_BATCH_SIZE", 50, 1, 500),
        retry_after_seconds=retry_after_seconds if retry_after_seconds is not None else _env_int("AI_OPTION_NOTIFICATION_RETRY_AFTER_SECONDS", 300, 30, 86400),
        max_attempts=max_attempts if max_attempts is not None else _env_int("AI_OPTION_NOTIFICATION_MAX_ATTEMPTS", 3, 1, 20),
    )


def _notification_loop() -> None:
    interval = _env_int("AI_OPTION_NOTIFICATION_WORKER_SECONDS", 30, 5, 600)
    prune_interval = _env_int("AI_OPTION_OBSERVATION_PRUNE_SECONDS", 3600, 300, 86400)
    # Proactively refresh LB tokens far before their ~90d expiry so they never
    # reach the dead zone where even the refresh call is rejected (401003).
    token_refresh_interval = _env_int("AI_OPTION_LONGBRIDGE_TOKEN_REFRESH_SECONDS", 43200, 3600, 604800)
    token_refresh_enabled = (os.getenv("AI_OPTION_LONGBRIDGE_PROACTIVE_REFRESH", "true") or "").strip().lower() in {"1", "true", "yes", "on"}
    next_prune = time.monotonic()
    # First proactive refresh one interval out (avoid a thundering refresh on
    # every worker restart); reactive refresh still covers a mid-window expiry.
    next_token_refresh = time.monotonic() + token_refresh_interval
    while True:
        try:
            run_notification_worker_once()
        except Exception:
            traceback.print_exc(limit=6)
        # Piggyback periodic history pruning on this already-running loop so the
        # append-only event/log tables don't grow without bound. Throttled so it
        # runs ~hourly, not every notification cycle.
        if time.monotonic() >= next_prune:
            try:
                prune_observation_history()
            except Exception:
                traceback.print_exc(limit=6)
            next_prune = time.monotonic() + prune_interval
        # Proactive LB token refresh (~twice daily by default).
        if token_refresh_enabled and time.monotonic() >= next_token_refresh:
            try:
                refresh_all_sdk_account_tokens()
            except Exception:
                traceback.print_exc(limit=6)
            next_token_refresh = time.monotonic() + token_refresh_interval
        time.sleep(interval)


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    return max(low, min(value, high))

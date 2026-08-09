from __future__ import annotations

import json
from typing import Any, Iterator

from .redis_runtime import redis_publish, redis_subscribe

# Per-scan pub/sub channel. A web-process SSE endpoint subscribes to one
# channel; the worker process publishes to it as the scan advances. Keeping
# one channel per scan id means a subscriber only receives its own scan's
# events with no client-side filtering.
_CHANNEL_PREFIX = "scan:events:"


def scan_channel(scan_id: str) -> str:
    return f"{_CHANNEL_PREFIX}{scan_id}"


def publish_scan_event(scan_id: str, event: dict[str, Any]) -> None:
    """Best-effort publish of a scan status change. Called from the worker's
    mark_scan_* writes. Never raises — a Redis hiccup must not break the status
    write or the scan itself; subscribers simply fall back to polling."""
    if not scan_id:
        return
    payload = {"scan_id": scan_id, **event}
    try:
        redis_publish(scan_channel(scan_id), json.dumps(payload, ensure_ascii=False))
    except Exception:
        # Defensive: redis_publish already swallows its own errors, but the
        # JSON encode or anything else must not propagate into a status write.
        pass


def iter_scan_events(scan_id: str, *, timeout: float = 1.0) -> Iterator[dict[str, Any] | None]:
    """Yield decoded scan events for a scan id. Yields None on each idle tick so
    the SSE endpoint can send keep-alives and check for client disconnect.
    Ends (returns) if Redis is unavailable, so the caller can degrade to
    polling."""
    for raw in redis_subscribe(scan_channel(scan_id), timeout=timeout):
        if raw is None:
            yield None
            continue
        try:
            yield json.loads(raw)
        except (ValueError, TypeError):
            continue

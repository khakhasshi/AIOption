from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from .observation_store import (
    build_scan_trigger_quote_snapshot,
    check_scan_trigger,
    list_due_scan_loop_instances,
    list_due_scan_triggers,
    process_due_opportunity_followups,
    run_due_scan_loop_instance,
)


_started = False
_lock = threading.Lock()
_runtime_lock = threading.Lock()
_runtime: dict[str, Any] = {
    "started": False,
    "started_at": None,
    "loops": {
        "scan_loop_scheduler": {"status": "idle"},
        "trigger_monitor": {"status": "idle"},
        "opportunity_followup": {"status": "idle"},
    },
}


def start_observation_scheduler() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
        _runtime["started"] = True
        _runtime["started_at"] = _utc_now()
    threading.Thread(target=_scan_loop, name="observation-scan-loop", daemon=True).start()
    threading.Thread(target=_trigger_loop, name="observation-trigger-loop", daemon=True).start()
    threading.Thread(target=_opportunity_followup_loop, name="opportunity-followup-loop", daemon=True).start()


def observation_scheduler_runtime_snapshot() -> dict[str, Any]:
    with _runtime_lock:
        snapshot = {
            "started": bool(_runtime.get("started")),
            "started_at": _runtime.get("started_at"),
            "generated_at": _utc_now(),
            "loops": {name: dict(value) for name, value in (_runtime.get("loops") or {}).items()},
        }
    for loop in snapshot["loops"].values():
        last_tick = _parse_epoch(loop.get("last_tick_epoch"))
        interval = int(loop.get("interval_seconds") or 0)
        loop["online"] = bool(last_tick and interval and time.time() - last_tick <= max(interval * 3, interval + 30))
        loop.pop("last_tick_epoch", None)
    snapshot["online"] = any(loop.get("online") for loop in snapshot["loops"].values())
    return snapshot


def run_observation_due_cycle(owner_id: str | None = None, *, scan_limit: int = 5, trigger_limit: int = 20, opportunity_limit: int = 20) -> dict[str, Any]:
    started = time.monotonic()
    result = {
        "scan_loops": _run_scan_loop_once(owner_id=owner_id, limit=scan_limit, manual=True),
        "triggers": _run_trigger_loop_once(owner_id=owner_id, limit=trigger_limit, manual=True),
        "opportunities": _run_opportunity_followup_once(owner_id=owner_id, limit=opportunity_limit, manual=True),
    }
    result["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    result["ran_at"] = _utc_now()
    return result


def _scan_loop() -> None:
    interval = _env_int("AI_OPTION_OBSERVATION_SCHEDULER_SECONDS", 30, 5, 600)
    while True:
        _run_scan_loop_once(limit=10, interval=interval)
        time.sleep(interval)


def _trigger_loop() -> None:
    interval = _env_int("AI_OPTION_TRIGGER_MONITOR_SECONDS", 30, 5, 600)
    while True:
        _run_trigger_loop_once(limit=20, interval=interval)
        time.sleep(interval)


def _opportunity_followup_loop() -> None:
    interval = _env_int("AI_OPTION_OPPORTUNITY_FOLLOWUP_SECONDS", 120, 30, 1800)
    while True:
        _run_opportunity_followup_once(limit=20, interval=interval)
        time.sleep(interval)


def _run_scan_loop_once(owner_id: str | None = None, *, limit: int = 10, interval: int | None = None, manual: bool = False) -> dict[str, Any]:
    loop_name = "scan_loop_scheduler"
    _mark_loop_start(loop_name, interval=interval, manual=manual)
    processed: list[dict[str, Any]] = []
    try:
        for instance in list_due_scan_loop_instances(owner_id, limit=limit):
            run = run_due_scan_loop_instance(instance, submit_scans=True)
            processed.append({"id": instance.get("id"), "name": instance.get("name"), "run_id": run.get("id"), "status": run.get("status")})
        payload = {"checked_count": len(processed), "processed": processed}
        _mark_loop_success(loop_name, payload)
        return payload
    except Exception as exc:  # noqa: BLE001 - scheduler health should retain user-facing failure context.
        traceback.print_exc(limit=6)
        _mark_loop_error(loop_name, exc)
        return {"checked_count": len(processed), "processed": processed, "error": str(exc)}


def _run_trigger_loop_once(owner_id: str | None = None, *, limit: int = 20, interval: int | None = None, manual: bool = False) -> dict[str, Any]:
    loop_name = "trigger_monitor"
    _mark_loop_start(loop_name, interval=interval, manual=manual)
    processed: list[dict[str, Any]] = []
    try:
        for trigger in list_due_scan_triggers(owner_id, limit=limit):
            condition = trigger.get("condition") or {}
            snapshot = None
            if condition.get("type") in {"technical_indicator", "option_quote"}:
                snapshot = build_scan_trigger_quote_snapshot(trigger)
            result = check_scan_trigger(trigger["owner_id"], trigger["id"], quote_snapshot=snapshot)
            processed.append({"id": trigger.get("id"), "symbol": trigger.get("symbol"), "matched": bool(result.get("matched")), "reason": result.get("reason")})
        payload = {"checked_count": len(processed), "processed": processed}
        _mark_loop_success(loop_name, payload)
        return payload
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(limit=6)
        _mark_loop_error(loop_name, exc)
        return {"checked_count": len(processed), "processed": processed, "error": str(exc)}


def _run_opportunity_followup_once(owner_id: str | None = None, *, limit: int = 20, interval: int | None = None, manual: bool = False) -> dict[str, Any]:
    loop_name = "opportunity_followup"
    _mark_loop_start(loop_name, interval=interval, manual=manual)
    try:
        result = process_due_opportunity_followups(owner_id, limit=limit)
        _mark_loop_success(loop_name, result)
        return result
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(limit=6)
        _mark_loop_error(loop_name, exc)
        return {"checked_count": 0, "processed_count": 0, "error": str(exc)}


def _mark_loop_start(name: str, *, interval: int | None = None, manual: bool = False) -> None:
    with _runtime_lock:
        loop = dict((_runtime.get("loops") or {}).get(name) or {})
        loop.update(
            {
                "status": "running",
                "last_tick_at": _utc_now(),
                "last_tick_epoch": time.time(),
                "interval_seconds": interval or loop.get("interval_seconds"),
                "manual": manual,
            }
        )
        _runtime["loops"][name] = loop


def _mark_loop_success(name: str, payload: dict[str, Any]) -> None:
    with _runtime_lock:
        loop = dict((_runtime.get("loops") or {}).get(name) or {})
        loop.update(
            {
                "status": "ok",
                "last_success_at": _utc_now(),
                "last_error": None,
                "last_result": _compact_result(payload),
            }
        )
        _runtime["loops"][name] = loop


def _mark_loop_error(name: str, exc: Exception) -> None:
    with _runtime_lock:
        loop = dict((_runtime.get("loops") or {}).get(name) or {})
        loop.update({"status": "error", "last_error": str(exc), "last_error_at": _utc_now()})
        _runtime["loops"][name] = loop


def _compact_result(payload: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in payload.items() if key.endswith("_count") or key in {"market_state", "error"}}
    for key in ("processed", "skipped"):
        values = payload.get(key)
        if isinstance(values, list):
            compact[key] = values[:5]
    return compact


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_epoch(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    return max(low, min(value, high))

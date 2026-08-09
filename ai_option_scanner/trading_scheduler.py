from __future__ import annotations

import traceback
import threading
import time
import uuid

from .redis_runtime import redis_available, redis_setnx, redis_eval
from .market_calendar import is_nyse_trading_day, next_nyse_trading_day
from .time_utils import now_et
from .trading_agent import get_schedule_slots, start_scheduled_trading_slot, start_trading_run
from .trading_store import list_enabled_configs, set_last_run_date


# Release the leader lock only if we still own it (token match), so a tick that
# already overran its TTL — letting another node acquire the key — cannot delete
# the new owner's lock. An unconditional redis_del here would let a slow tick
# erase a peer's lock, cascading into concurrent ticks that double-submit a
# single-instance config (whose only other dedup is last_run_date_et==today).
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_LOCK_TTL_SECONDS = 55
_started = False
_lock = threading.Lock()
_state_lock = threading.Lock()
_scheduler_lock_key = "ai-option:trading-scheduler-lock"
_state = {
    "started": False,
    "last_tick_at_et": None,
    "last_error": None,
    "last_trading_day_reason": None,
    "last_triggered_owner_id": None,
    "last_triggered_run_time_et": None,
    "last_triggered_slot_id": None,
}


def start_trading_scheduler() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    with _state_lock:
        _state["started"] = True
    threading.Thread(target=_loop, name="trading-scheduler", daemon=True).start()


def _loop() -> None:
    while True:
        try:
            _tick()
            _set_last_error(None)
        except Exception:
            _set_last_error(traceback.format_exc(limit=8))
        time.sleep(30)


def _tick() -> None:
    has_redis = redis_available()
    lock_token = uuid.uuid4().hex
    if has_redis and not redis_setnx(_scheduler_lock_key, lock_token, _LOCK_TTL_SECONDS):
        return
    try:
        now = now_et()
        is_trading_day, reason = is_nyse_trading_day(now.date())
        with _state_lock:
            _state["last_tick_at_et"] = now.isoformat()
            _state["last_trading_day_reason"] = reason
        if not is_trading_day:
            return
        today = now.date().isoformat()
        current_minutes = now.hour * 60 + now.minute
        for config in list_enabled_configs():
            try:
                _tick_one_config(config, today, current_minutes)
            except Exception:  # noqa: BLE001 - one bad config must not kill the tick.
                # A single config that raises (e.g. TradingRunBlockedError on a
                # failed readiness/entry gate) must not skip every config ordered
                # after it. Log and continue; the loop-level handler records the
                # last error for status.
                traceback.print_exc(limit=6)
    finally:
        if has_redis:
            try:
                # Release only if we still own it (token match).
                redis_eval(_RELEASE_LUA, [_scheduler_lock_key], [lock_token])
            except Exception:  # noqa: BLE001
                pass


def _tick_one_config(config: dict, today: str, current_minutes: int) -> None:
    owner_id = str(config.get("owner_id"))
    if config.get("multi_instance_enabled"):
        slots = [slot for slot in get_schedule_slots(config) if slot.get("enabled", True)]
        for slot in slots:
            slot_time = str(slot.get("time_et") or "10:30")
            hour, minute = [int(part) for part in slot_time.split(":", 1)]
            if current_minutes < hour * 60 + minute:
                continue
            run = start_scheduled_trading_slot(
                owner_id,
                config,
                trade_date_et=today,
                profile_id=str(config.get("schedule_profile") or "default"),
                slot=slot,
            )
            if run:
                with _state_lock:
                    _state["last_triggered_owner_id"] = owner_id
                    _state["last_triggered_run_time_et"] = f"{today}T{slot_time}:00"
                    _state["last_triggered_slot_id"] = str(slot.get("slot_id") or "")
        return
    if not config.get("single_instance_enabled", True):
        return
    if config.get("last_run_date_et") == today:
        return
    run_time = str(config.get("run_time_et") or "10:30")
    hour, minute = [int(part) for part in run_time.split(":", 1)]
    if current_minutes < hour * 60 + minute:
        return
    # set_last_run_date runs only AFTER a run is actually submitted: a blocked/
    # not-ready config raises before any run is created, so leaving the date unset
    # lets it retry on a later tick (cheap gate checks, no run created) instead of
    # losing the whole day to a transient blocker. The per-config try/except in
    # _tick keeps that retry from blocking other configs. The narrow crash window
    # between submit and this line is covered by the order-journal idempotency.
    start_trading_run(owner_id, config, trigger_source="scheduler")
    set_last_run_date(owner_id, today)
    with _state_lock:
        _state["last_triggered_owner_id"] = owner_id
        _state["last_triggered_run_time_et"] = f"{today}T{run_time}:00"
        _state["last_triggered_slot_id"] = None


def scheduler_status() -> dict[str, str | None | bool]:
    with _state_lock:
        return dict(_state)


def next_run_preview(run_time_et: str) -> dict[str, str | dict[str, str] | None]:
    hour, minute = _parse_time_et(run_time_et)
    now = now_et()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        from datetime import timedelta

        target = target + timedelta(days=1)
    target_date = next_nyse_trading_day(target.date())
    if target_date != target.date():
        target = target.replace(year=target_date.year, month=target_date.month, day=target_date.day)
    return {
        "next_run_at_et": target.isoformat(),
        "next_run_mode": "single_run",
        "next_run_slot": None,
    }


def next_config_run_preview(config: dict) -> dict[str, str | dict[str, str] | None]:
    if not config.get("multi_instance_enabled"):
        if not config.get("single_instance_enabled", True):
            return {
                "next_run_at_et": None,
                "next_run_mode": "single_run_disabled",
                "next_run_slot": None,
            }
        return next_run_preview(str(config.get("run_time_et") or "10:30"))
    slots = [slot for slot in get_schedule_slots(config) if slot.get("enabled", True)]
    if not slots:
        return {**next_run_preview(str(config.get("run_time_et") or "10:30")), "next_run_mode": "multi_slot_empty"}

    now = now_et()
    today = now.date()
    current_minutes = now.hour * 60 + now.minute
    normalized_slots = sorted(slots, key=lambda item: _time_minutes(str(item.get("time_et") or "10:30")))
    target_date = today
    target_slot = None
    for slot in normalized_slots:
        if _time_minutes(str(slot.get("time_et") or "10:30")) > current_minutes:
            target_slot = slot
            break
    if target_slot is None:
        from datetime import timedelta

        target_date = today + timedelta(days=1)
        target_slot = normalized_slots[0]
    trading_day = next_nyse_trading_day(target_date)
    if trading_day != target_date:
        target_date = trading_day
        target_slot = normalized_slots[0]
    hour, minute = _parse_time_et(str(target_slot.get("time_et") or "10:30"))
    target = now.replace(year=target_date.year, month=target_date.month, day=target_date.day, hour=hour, minute=minute, second=0, microsecond=0)
    return {
        "next_run_at_et": target.isoformat(),
        "next_run_mode": "multi_slot",
        "next_run_slot": {
            "slot_id": str(target_slot.get("slot_id") or ""),
            "label": str(target_slot.get("label") or target_slot.get("slot_id") or ""),
            "time_et": f"{hour:02d}:{minute:02d}",
            "action": str(target_slot.get("action") or ""),
            "gate_profile": str(target_slot.get("gate_profile") or ""),
        },
    }


def _parse_time_et(value: str) -> tuple[int, int]:
    try:
        hour, minute = [int(part) for part in str(value or "10:30").split(":", 1)]
    except (TypeError, ValueError):
        return 10, 30
    return max(0, min(hour, 23)), max(0, min(minute, 59))


def _time_minutes(value: str) -> int:
    hour, minute = _parse_time_et(value)
    return hour * 60 + minute


def _set_last_error(message: str | None) -> None:
    with _state_lock:
        _state["last_error"] = message

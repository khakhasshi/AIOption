from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .account_store import LOCAL_OWNER_ID, normalize_owner_id, utc_now
from .db import connect, ensure_column, run_db_init_once
from .strategy_structures import normalize_strategy_modes
from . import adaptive_pricing
from .trading_instance import (
    INSTANCE_VERSION,
    append_instance_event,
    build_protection_status,
    create_trade_instance,
    hydrate_trade_instance,
    lifecycle_from_orders,
    sanitize_instance_orders,
)
from .time_utils import to_et_iso

# ---------------------------------------------------------------------------
# Lightweight in-process TTL caches — avoid redundant DB hits on hot read paths
# ---------------------------------------------------------------------------

class _TTLCache:
    """Simple thread-safe per-key TTL cache."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry and time.monotonic() - entry[0] < self._ttl:
                return True, entry[1]
            return False, None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_config_cache = _TTLCache(ttl=10.0)          # get_trading_config — stale by at most 10 s
_runtime_counts_cache = _TTLCache(ttl=30.0)  # trading_runtime_counts — refreshed every 30 s
_schedule_snapshot_cache = _TTLCache(ttl=30.0)  # schedule_runtime_snapshot — refreshed every 30 s


ET = ZoneInfo("America/New_York")
DEFAULT_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "QQQ", "SPY"]
DEFAULT_TRADING_PROMPT = (
    "扫描{symbol}最近的日K线，今天的分时线，相关领域的新闻，"
    "你帮我选一个具有大额度盈利潜力的单腿期权"
)
DEFAULT_TRADING_CONFIG = {
    "live_enabled": False,
    "total_capital": 10000.0,
    "run_time_et": "10:30",
    "single_instance_enabled": True,
    "multi_instance_enabled": False,
    "schedule_profile": "single_run",
    "schedule_slots": [],
    "schedule_session_id": None,
    "schedule_slot_id": None,
    "schedule_slot_label": None,
    "schedule_slot_time_et": None,
    "schedule_slot_action": None,
    "schedule_slot_gate_profile": None,
    "schedule_slot_allow_new_positions": True,
    "schedule_slot_force_no_overnight": False,
    "trade_date_et": None,
    "universe": DEFAULT_UNIVERSE,
    "prompt_template": DEFAULT_TRADING_PROMPT,
    "dry_run": False,
    "top_n": 5,
    "max_per_symbol": 1,
    "default_stop_loss_pct": 25.0,
    "default_take_profit_pct": 30.0,
    "tiered_take_profit_enabled": False,
    "default_take_profit_1_pct": 20.0,
    "default_take_profit_2_pct": 35.0,
    "use_ai": True,
    "council": True,
    "ai_adjust_allocation": False,
    "ai_adjust_stop_loss": True,
    "ai_adjust_take_profit": False,
    # Auto-trade safety net: force the deterministic pre-close flatten regardless
    # of the LLM's allow_overnight. Cap any single LLM-sized position's share of
    # total_capital (0 = uncapped). Both default off/uncapped for manual runs.
    "force_no_overnight": False,
    "max_allocation_pct_per_trade": 0.0,
    # Optional extra instruction injected into the decision payload (auto-trade
    # uses it to make the LLM session-aware: budget, smart exits, no overnight).
    "decision_directive": "",
    "software_stop_enabled": True,
    "software_take_profit_enabled": True,
    "risk_max_daily_runs": 3,
    "risk_max_consecutive_failures": 2,
    "risk_max_unprotected_quantity": 0,
    "risk_max_single_stop_loss_pct": 45.0,
    "risk_require_protection_for_market_order": True,
    "low_gate_enabled": False,
    "ai_provider": "deepseek",
    "broker": "longbridge",
    "broker_account": None,
    "longbridge_account": None,
    "market_data_source": "thetadata",
    "analysis_modules": {
        "intraday": True,
        "greeks": True,
        "gex": True,
        "execution": True,
        "volatility": True,
        "strategy": True,
        "scenario": True,
        "risk": True,
    },
    "strategy_modes": ["single_leg"],
    "strategy_auto_execute_enabled": False,
    "strategy_unwind_on_failure": True,
    "wait_for_fill_seconds": 8,
    "entry_order_type": "market",
    "exit_order_type": "market",
    "trigger_source": "manual",
}


DEFAULT_SCHEDULE_SLOTS = [
    {
        "slot_id": "open_confirmation",
        "label": "开盘确认",
        "time_et": "09:45",
        "action": "scan_open",
        "strategy_modes": ["single_leg", "spread"],
        "capital_pct": 0.25,
        "gate_profile": "strict_momentum",
        "allow_new_positions": True,
        "force_no_overnight": False,
        "enabled": True,
    },
    {
        "slot_id": "midday_structure",
        "label": "中盘结构",
        "time_et": "12:45",
        "action": "open_or_adjust",
        "strategy_modes": ["calendar", "iron_condor", "strangle", "butterfly"],
        "capital_pct": 0.35,
        "gate_profile": "structure_specific",
        "allow_new_positions": True,
        "force_no_overnight": False,
        "enabled": True,
    },
    {
        "slot_id": "power_hour_risk",
        "label": "尾盘风控",
        "time_et": "15:10",
        "action": "reduce_or_exit",
        "strategy_modes": ["single_leg", "spread"],
        "capital_pct": 0.15,
        "gate_profile": "no_overnight",
        "allow_new_positions": False,
        "force_no_overnight": True,
        "enabled": True,
    },
]


def init_trading_db() -> None:
    run_db_init_once("trading_store", _init_trading_db)


def _init_trading_db() -> None:
    with _connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_configs (
                owner_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_run_date_et TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_runs (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                stage TEXT,
                progress INTEGER,
                config_json TEXT NOT NULL,
                scan_results_json TEXT,
                council_json TEXT,
                selections_json TEXT,
                orders_json TEXT,
                instance_json TEXT,
                instance_version INTEGER NOT NULL DEFAULT 1,
                lifecycle_state TEXT,
                protection_state TEXT,
                instance_updated_at TEXT,
                error TEXT
            )
            """
        )
        ensure_column(db, "trading_runs", "instance_json", "TEXT")
        ensure_column(db, "trading_runs", "instance_version", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "trading_runs", "lifecycle_state", "TEXT")
        ensure_column(db, "trading_runs", "protection_state", "TEXT")
        ensure_column(db, "trading_runs", "instance_updated_at", "TEXT")
        ensure_column(db, "trading_runs", "locator_id", "TEXT")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_capital_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                snapshot_date_et TEXT NOT NULL,
                created_at TEXT NOT NULL,
                account_name TEXT NOT NULL,
                total_capital REAL NOT NULL,
                net_assets REAL,
                total_cash REAL,
                buy_power REAL,
                risk_level TEXT,
                assets_json TEXT NOT NULL,
                executions_json TEXT NOT NULL,
                UNIQUE(owner_id, snapshot_date_et)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_schedule_sessions (
                session_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                trade_date_et TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                status TEXT NOT NULL,
                total_capital REAL NOT NULL,
                allocated_capital REAL NOT NULL DEFAULT 0,
                remaining_capital REAL NOT NULL DEFAULT 0,
                slot_count INTEGER NOT NULL DEFAULT 0,
                fired_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                config_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                context_json TEXT NOT NULL,
                UNIQUE(owner_id, trade_date_et, profile_id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_schedule_fires (
                owner_id TEXT NOT NULL,
                trade_date_et TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                slot_id TEXT NOT NULL,
                session_id TEXT,
                scheduled_time_et TEXT NOT NULL,
                action TEXT,
                gate_profile TEXT,
                status TEXT NOT NULL,
                run_id TEXT,
                allocated_capital REAL NOT NULL DEFAULT 0,
                gate_result_json TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_replay_at TEXT,
                claimed_at TEXT NOT NULL,
                fired_at TEXT,
                error TEXT,
                PRIMARY KEY (owner_id, trade_date_et, profile_id, slot_id)
            )
            """
        )
        ensure_column(db, "trading_schedule_sessions", "config_hash", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "trading_schedule_fires", "session_id", "TEXT")
        ensure_column(db, "trading_schedule_fires", "action", "TEXT")
        ensure_column(db, "trading_schedule_fires", "gate_profile", "TEXT")
        ensure_column(db, "trading_schedule_fires", "allocated_capital", "REAL NOT NULL DEFAULT 0")
        ensure_column(db, "trading_schedule_fires", "gate_result_json", "TEXT")
        ensure_column(db, "trading_schedule_fires", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "trading_schedule_fires", "last_replay_at", "TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_trading_runs_owner_created ON trading_runs(owner_id, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_trading_runs_status ON trading_runs(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_trading_runs_owner_lifecycle ON trading_runs(owner_id, lifecycle_state, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_trading_runs_protection ON trading_runs(protection_state)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trading_runs_locator_id ON trading_runs(locator_id)")
        # Compound index for list_monitorable_trading_runs: filters by status + lifecycle_state, orders by created_at
        db.execute("CREATE INDEX IF NOT EXISTS idx_trading_runs_monitor ON trading_runs(status, lifecycle_state, created_at DESC)")
        # Compound index for trading_runtime_counts GROUP BY: covers status, lifecycle_state, protection_state
        db.execute("CREATE INDEX IF NOT EXISTS idx_trading_runs_status_lifecycle_protection ON trading_runs(status, lifecycle_state, protection_state)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_capital_snapshots_owner_date ON trading_capital_snapshots(owner_id, snapshot_date_et DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_schedule_sessions_owner_date ON trading_schedule_sessions(owner_id, trade_date_et DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_schedule_fires_date ON trading_schedule_fires(trade_date_et, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_schedule_fires_claimed ON trading_schedule_fires(status, claimed_at)")
        # Append-only journal of every real broker order action (submit/cancel/
        # fill). Written before AND after each broker call so a crash mid-submit
        # leaves a durable record to reconcile against, and so a stable
        # client_order_key lets a retry detect an in-flight/succeeded submit
        # instead of duplicating a live order.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_order_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                run_id TEXT,
                client_order_key TEXT,
                action TEXT NOT NULL,
                phase TEXT NOT NULL,
                broker TEXT,
                account_ref TEXT,
                symbol TEXT,
                side TEXT,
                quantity REAL,
                price REAL,
                order_id TEXT,
                status TEXT,
                detail_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_order_journal_key ON trading_order_journal(client_order_key, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_order_journal_run ON trading_order_journal(run_id, created_at)")
        _backfill_trading_locator_ids(db)
        _backfill_instance_columns(db)


def get_trading_config(owner_id: str = LOCAL_OWNER_ID) -> dict[str, Any]:
    init_trading_db()
    owner_id = normalize_owner_id(owner_id)
    hit, cached = _config_cache.get(owner_id)
    if hit:
        return cached  # type: ignore[return-value]
    with _connect() as db:
        row = db.execute("SELECT config_json, last_run_date_et FROM trading_configs WHERE owner_id = ?", (owner_id,)).fetchone()
    config = normalize_trading_config(_loads(row["config_json"]) if row else {})
    config["owner_id"] = owner_id
    config["last_run_date_et"] = row["last_run_date_et"] if row else None
    _config_cache.set(owner_id, config)
    return config


def save_trading_config(owner_id: str, config: dict[str, Any]) -> dict[str, Any]:
    init_trading_db()
    owner_id = normalize_owner_id(owner_id)
    normalized = normalize_trading_config(config)
    now = utc_now()
    last_run_date_et = _saved_last_run_date_for_config(owner_id, normalized)
    with _connect() as db:
        db.execute(
            """
            INSERT INTO trading_configs (owner_id, config_json, updated_at, last_run_date_et)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(owner_id) DO UPDATE SET
                config_json = excluded.config_json,
                updated_at = excluded.updated_at,
                last_run_date_et = excluded.last_run_date_et
            """,
            (owner_id, json.dumps(normalized, ensure_ascii=False), now, last_run_date_et),
        )
    _config_cache.invalidate(owner_id)  # Flush cache so next read reflects the save
    return get_trading_config(owner_id)


def set_last_run_date(owner_id: str, run_date_et: str) -> None:
    config = get_trading_config(owner_id)
    with _connect() as db:
        db.execute(
            """
            INSERT INTO trading_configs (owner_id, config_json, updated_at, last_run_date_et)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(owner_id) DO UPDATE SET
                last_run_date_et = excluded.last_run_date_et,
                updated_at = excluded.updated_at
            """,
            (normalize_owner_id(owner_id), json.dumps(normalize_trading_config(config), ensure_ascii=False), utc_now(), run_date_et),
        )


def get_or_create_schedule_session(owner_id: str, trade_date_et: str, profile_id: str, config: dict[str, Any]) -> dict[str, Any]:
    init_trading_db()
    owner_id = normalize_owner_id(owner_id)
    profile_id = _normalize_profile_id(profile_id)
    total_capital = max(float(config.get("total_capital") or 0), 0.0)
    slots = _normalize_schedule_slots(config.get("schedule_slots"))
    config_hash = schedule_config_hash(config)
    context = {
        "profile_id": profile_id,
        "config_hash": config_hash,
        "slot_count": len([slot for slot in slots if slot.get("enabled", True)]),
        "slots": slots,
    }
    now = utc_now()
    session_id = _stable_locator_id("SES", f"{owner_id}-{trade_date_et}-{profile_id}")
    with _connect() as db:
        existing = db.execute(
            """
            SELECT session_id, config_hash, context_json
            FROM trading_schedule_sessions
            WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ?
            """,
            (owner_id, trade_date_et, profile_id),
        ).fetchone()
        if existing and existing["config_hash"] and existing["config_hash"] != config_hash:
            existing_context = _loads(existing["context_json"]) or {}
            existing_context["config_drift"] = {
                "expected_config_hash": existing["config_hash"],
                "current_config_hash": config_hash,
                "detected_at": now,
            }
            db.execute(
                """
                UPDATE trading_schedule_sessions
                SET status = 'degraded', context_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (json.dumps(existing_context, ensure_ascii=False), now, existing["session_id"]),
            )
            return _row_to_schedule_session(db.execute("SELECT * FROM trading_schedule_sessions WHERE session_id = ?", (existing["session_id"],)).fetchone())
        db.execute(
            """
            INSERT INTO trading_schedule_sessions
                (session_id, owner_id, trade_date_et, profile_id, status, total_capital,
                 allocated_capital, remaining_capital, slot_count, config_hash, created_at, updated_at, context_json)
            VALUES (?, ?, ?, ?, 'open', ?, 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, trade_date_et, profile_id) DO UPDATE SET
                total_capital = excluded.total_capital,
                slot_count = excluded.slot_count,
                config_hash = excluded.config_hash,
                context_json = excluded.context_json,
                updated_at = excluded.updated_at
            """,
            (session_id, owner_id, trade_date_et, profile_id, total_capital, total_capital, context["slot_count"], config_hash, now, now, json.dumps(context, ensure_ascii=False)),
        )
    return recalc_schedule_session(owner_id, trade_date_et, profile_id)


def recalc_schedule_session(owner_id: str, trade_date_et: str, profile_id: str) -> dict[str, Any]:
    init_trading_db()
    owner_id = normalize_owner_id(owner_id)
    profile_id = _normalize_profile_id(profile_id)
    with _connect() as db:
        session = db.execute(
            """
            SELECT *
            FROM trading_schedule_sessions
            WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ?
            """,
            (owner_id, trade_date_et, profile_id),
        ).fetchone()
        if session is None:
            raise ValueError("schedule session not found")
        # Use SQL aggregation instead of fetching all rows and looping in Python
        agg = db.execute(
            """
            SELECT
                SUM(CASE WHEN status IN ('claimed', 'fired') THEN COALESCE(allocated_capital, 0) ELSE 0 END) AS allocated,
                SUM(CASE WHEN status = 'fired'    THEN 1 ELSE 0 END) AS fired,
                SUM(CASE WHEN status = 'skipped'  THEN 1 ELSE 0 END) AS skipped,
                SUM(CASE WHEN status = 'failed'   THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'retrying' THEN 1 ELSE 0 END) AS retrying
            FROM trading_schedule_fires
            WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ?
            """,
            (owner_id, trade_date_et, profile_id),
        ).fetchone()
        allocated = float(agg["allocated"] or 0)
        fired = int(agg["fired"] or 0)
        skipped = int(agg["skipped"] or 0)
        failed = int(agg["failed"] or 0)
        retrying = int(agg["retrying"] or 0)
        total = float(session["total_capital"] or 0)
        slot_count = int(session["slot_count"] or 0)
        terminal = fired + skipped + failed >= slot_count if slot_count > 0 else False
        context = _loads(session["context_json"]) or {}
        if context.get("config_drift"):
            status = "degraded"
        elif retrying:
            status = "retrying"
        elif terminal and failed == 0:
            status = "completed"
        elif terminal and failed:
            status = "attention"
        elif fired or skipped or failed:
            status = "partial"
        else:
            status = "open"
        db.execute(
            """
            UPDATE trading_schedule_sessions
            SET status = ?, allocated_capital = ?, remaining_capital = ?, fired_count = ?,
                skipped_count = ?, failed_count = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (status, round(allocated, 2), round(max(total - allocated, 0.0), 2), fired, skipped, failed, utc_now(), session["session_id"]),
        )
        row = db.execute("SELECT * FROM trading_schedule_sessions WHERE session_id = ?", (session["session_id"],)).fetchone()
    return _row_to_schedule_session(row)


def get_schedule_session(owner_id: str, trade_date_et: str, profile_id: str) -> dict[str, Any] | None:
    init_trading_db()
    with _connect() as db:
        row = db.execute(
            """
            SELECT *
            FROM trading_schedule_sessions
            WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ?
            """,
            (normalize_owner_id(owner_id), trade_date_et, _normalize_profile_id(profile_id)),
        ).fetchone()
    return _row_to_schedule_session(row) if row else None


def claim_schedule_slot(
    owner_id: str,
    trade_date_et: str,
    profile_id: str,
    slot_id: str,
    scheduled_time_et: str,
    *,
    session_id: str | None = None,
    action: str | None = None,
    gate_profile: str | None = None,
    allocated_capital: float = 0.0,
    gate_result: dict[str, Any] | None = None,
) -> bool:
    init_trading_db()
    try:
        with _connect() as db:
            existing = db.execute(
                """
                SELECT status
                FROM trading_schedule_fires
                WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ? AND slot_id = ?
                """,
                (normalize_owner_id(owner_id), trade_date_et, _normalize_profile_id(profile_id), slot_id),
            ).fetchone()
            if existing and str(existing["status"] or "") == "retrying":
                cursor = db.execute(
                    """
                    UPDATE trading_schedule_fires
                    SET session_id = ?, scheduled_time_et = ?, action = ?, gate_profile = ?, status = 'claimed',
                        run_id = NULL, allocated_capital = ?, gate_result_json = ?, retry_count = retry_count + 1,
                        last_replay_at = ?, claimed_at = ?, fired_at = NULL, error = NULL
                    WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ? AND slot_id = ? AND status = 'retrying'
                    """,
                    (
                        session_id,
                        scheduled_time_et,
                        action,
                        gate_profile,
                        round(max(float(allocated_capital or 0), 0.0), 2),
                        json.dumps(gate_result or {}, ensure_ascii=False),
                        utc_now(),
                        utc_now(),
                        normalize_owner_id(owner_id),
                        trade_date_et,
                        _normalize_profile_id(profile_id),
                        slot_id,
                    ),
                )
                inserted = bool(getattr(cursor, "rowcount", 0))
            else:
                cursor = db.execute(
                    """
                    INSERT INTO trading_schedule_fires
                        (owner_id, trade_date_et, profile_id, slot_id, session_id, scheduled_time_et,
                         action, gate_profile, status, run_id, allocated_capital, gate_result_json, retry_count, last_replay_at, claimed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed', NULL, ?, ?, 0, NULL, ?)
                    ON CONFLICT(owner_id, trade_date_et, profile_id, slot_id) DO NOTHING
                    """,
                    (
                        normalize_owner_id(owner_id),
                        trade_date_et,
                        _normalize_profile_id(profile_id),
                        slot_id,
                        session_id,
                        scheduled_time_et,
                        action,
                        gate_profile,
                        round(max(float(allocated_capital or 0), 0.0), 2),
                        json.dumps(gate_result or {}, ensure_ascii=False),
                        utc_now(),
                    ),
                )
                inserted = bool(getattr(cursor, "rowcount", 0))
        if inserted:
            recalc_schedule_session(owner_id, trade_date_et, profile_id)
        return inserted
    except Exception:
        return False


def recover_stale_schedule_slots(
    owner_id: str,
    trade_date_et: str,
    profile_id: str,
    *,
    stale_after_minutes: int = 30,
) -> int:
    init_trading_db()
    owner_id = normalize_owner_id(owner_id)
    profile_id = _normalize_profile_id(profile_id)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, int(stale_after_minutes)))
    recovered = 0
    with _connect() as db:
        rows = db.execute(
            """
            SELECT slot_id
            FROM trading_schedule_fires
            WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ? AND status = 'claimed'
              AND claimed_at < ?
            """,
            (owner_id, trade_date_et, profile_id, cutoff.isoformat()),
        ).fetchall()
        for row in rows:
            cursor = db.execute(
                """
                UPDATE trading_schedule_fires
                SET status = 'retrying', error = COALESCE(error, 'stale claimed slot recovered'), last_replay_at = ?, claimed_at = ?
                WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ? AND slot_id = ?
                """,
                (utc_now(), utc_now(), owner_id, trade_date_et, profile_id, row["slot_id"]),
            )
            recovered += int(getattr(cursor, "rowcount", 0) or 0)
    if recovered:
        recalc_schedule_session(owner_id, trade_date_et, profile_id)
    return recovered


def skip_schedule_slot(
    owner_id: str,
    trade_date_et: str,
    profile_id: str,
    slot_id: str,
    scheduled_time_et: str,
    *,
    session_id: str | None = None,
    action: str | None = None,
    gate_profile: str | None = None,
    gate_result: dict[str, Any] | None = None,
    reason: str | None = None,
) -> bool:
    init_trading_db()
    with _connect() as db:
        cursor = db.execute(
            """
            INSERT INTO trading_schedule_fires
                (owner_id, trade_date_et, profile_id, slot_id, session_id, scheduled_time_et,
                 action, gate_profile, status, run_id, allocated_capital, gate_result_json, retry_count, last_replay_at, claimed_at, fired_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'skipped', NULL, 0, ?, 0, NULL, ?, ?, ?)
            ON CONFLICT(owner_id, trade_date_et, profile_id, slot_id) DO NOTHING
            """,
            (
                normalize_owner_id(owner_id),
                trade_date_et,
                _normalize_profile_id(profile_id),
                slot_id,
                session_id,
                scheduled_time_et,
                action,
                gate_profile,
                json.dumps(gate_result or {}, ensure_ascii=False),
                utc_now(),
                utc_now(),
                reason,
            ),
        )
        inserted = bool(getattr(cursor, "rowcount", 0))
    if inserted:
        recalc_schedule_session(owner_id, trade_date_et, profile_id)
    return inserted


def mark_schedule_slot_fired(
    owner_id: str,
    trade_date_et: str,
    profile_id: str,
    slot_id: str,
    *,
    run_id: str | None = None,
    status: str = "fired",
    error: str | None = None,
) -> None:
    init_trading_db()
    with _connect() as db:
        db.execute(
            """
            UPDATE trading_schedule_fires
            SET status = ?, run_id = COALESCE(?, run_id), fired_at = ?, error = ?
            WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ? AND slot_id = ?
            """,
            (status, run_id, utc_now(), error, normalize_owner_id(owner_id), trade_date_et, _normalize_profile_id(profile_id), slot_id),
        )
    recalc_schedule_session(owner_id, trade_date_et, profile_id)


def list_schedule_fires(owner_id: str | None = None, trade_date_et: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_trading_db()
    clauses = []
    params: list[Any] = []
    if owner_id is not None:
        clauses.append("owner_id = ?")
        params.append(normalize_owner_id(owner_id))
    if trade_date_et is not None:
        clauses.append("trade_date_et = ?")
        params.append(trade_date_et)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 100), 500)))
    with _connect() as db:
        rows = db.execute(
            f"""
            SELECT owner_id, trade_date_et, profile_id, slot_id, session_id, scheduled_time_et,
                   action, gate_profile, status, run_id, allocated_capital, gate_result_json,
                   retry_count, last_replay_at, claimed_at, fired_at, error
            FROM trading_schedule_fires
            {where}
            ORDER BY trade_date_et DESC, scheduled_time_et DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [
        {
            "owner_id": row["owner_id"],
            "trade_date_et": row["trade_date_et"],
            "profile_id": row["profile_id"],
            "slot_id": row["slot_id"],
            "session_id": row["session_id"],
            "scheduled_time_et": row["scheduled_time_et"],
            "action": row["action"],
            "gate_profile": row["gate_profile"],
            "status": row["status"],
            "run_id": row["run_id"],
            "allocated_capital": float(row["allocated_capital"] or 0),
            "gate_result": _loads(row["gate_result_json"]) or {},
            "retry_count": int(row["retry_count"] or 0),
            "last_replay_at": to_et_iso(row["last_replay_at"]) if row["last_replay_at"] else None,
            "claimed_at": to_et_iso(row["claimed_at"]) if row["claimed_at"] else None,
            "fired_at": to_et_iso(row["fired_at"]) if row["fired_at"] else None,
            "error": row["error"],
        }
        for row in rows
    ]


def schedule_runtime_snapshot(limit: int = 20) -> dict[str, Any]:
    init_trading_db()
    cache_key = str(int(limit or 20))
    hit, cached = _schedule_snapshot_cache.get(cache_key)
    if hit:
        return cached  # type: ignore[return-value]
    with _connect() as db:
        status_rows = db.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM trading_schedule_sessions
            GROUP BY status
            """
        ).fetchall()
        session_rows = db.execute(
            """
            SELECT *
            FROM trading_schedule_sessions
            ORDER BY updated_at DESC, trade_date_et DESC
            LIMIT ?
            """,
            (max(1, min(int(limit or 20), 100)),),
        ).fetchall()
        fire_rows = db.execute(
            """
            SELECT status, claimed_at, fired_at, retry_count
            FROM trading_schedule_fires
            WHERE fired_at IS NOT NULL OR status IN ('claimed', 'retrying')
            ORDER BY claimed_at DESC
            LIMIT 100
            """
        ).fetchall()
    latencies: list[float] = []
    stale_claimed = 0
    now = datetime.now(timezone.utc)
    retrying = 0
    for row in fire_rows:
        status = str(row["status"] or "")
        claimed_at = _parse_dt(row["claimed_at"])
        fired_at = _parse_dt(row["fired_at"])
        if status == "retrying":
            retrying += 1
        if status == "claimed" and claimed_at and (now - claimed_at).total_seconds() > 30 * 60:
            stale_claimed += 1
        if claimed_at and fired_at:
            latencies.append(max(0.0, (fired_at - claimed_at).total_seconds()))
    sorted_latencies = sorted(latencies)
    p95_index = int(round((len(sorted_latencies) - 1) * 0.95)) if sorted_latencies else 0
    result = {
        "session_status_counts": {str(row["status"] or "unknown"): int(row["count"] or 0) for row in status_rows},
        "claimed_to_fired_latency": {
            "sample_size": len(latencies),
            "avg_seconds": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "p95_seconds": round(sorted_latencies[p95_index], 2) if sorted_latencies else None,
            "max_seconds": round(max(latencies), 2) if latencies else None,
        },
        "stale_claimed_slots": stale_claimed,
        "retrying_slots": retrying,
        "remaining_capital_curve": [
            {
                "session_id": row["session_id"],
                "owner_id": row["owner_id"],
                "trade_date_et": row["trade_date_et"],
                "profile_id": row["profile_id"],
                "status": row["status"],
                "total_capital": float(row["total_capital"] or 0),
                "allocated_capital": float(row["allocated_capital"] or 0),
                "remaining_capital": float(row["remaining_capital"] or 0),
                "updated_at": to_et_iso(row["updated_at"]) if row["updated_at"] else None,
            }
            for row in session_rows
        ],
    }
    _schedule_snapshot_cache.set(cache_key, result)
    return result


def _row_to_schedule_session(row: Any) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "owner_id": row["owner_id"],
        "trade_date_et": row["trade_date_et"],
        "profile_id": row["profile_id"],
        "status": row["status"],
        "total_capital": float(row["total_capital"] or 0),
        "allocated_capital": float(row["allocated_capital"] or 0),
        "remaining_capital": float(row["remaining_capital"] or 0),
        "slot_count": int(row["slot_count"] or 0),
        "fired_count": int(row["fired_count"] or 0),
        "skipped_count": int(row["skipped_count"] or 0),
        "failed_count": int(row["failed_count"] or 0),
        "config_hash": row["config_hash"] if "config_hash" in row.keys() else "",
        "created_at": to_et_iso(row["created_at"]) if row["created_at"] else None,
        "updated_at": to_et_iso(row["updated_at"]) if row["updated_at"] else None,
        "context": _loads(row["context_json"]) or {},
    }


def list_enabled_configs() -> list[dict[str, Any]]:
    init_trading_db()
    with _connect() as db:
        rows = db.execute("SELECT owner_id, config_json, last_run_date_et FROM trading_configs").fetchall()
    configs = []
    for row in rows:
        config = normalize_trading_config(_loads(row["config_json"]))
        if config.get("live_enabled"):
            config["owner_id"] = row["owner_id"]
            config["last_run_date_et"] = row["last_run_date_et"]
            configs.append(config)
    return configs


def create_trading_run(owner_id: str, config: dict[str, Any]) -> dict[str, Any]:
    init_trading_db()
    run_id = uuid.uuid4().hex
    locator_id = _locator_id("TRD", run_id)
    owner_id = normalize_owner_id(owner_id)
    normalized_config = normalize_trading_config(config)
    normalized_config["locator_id"] = locator_id
    instance = create_trade_instance(run_id, owner_id, normalized_config)
    with _connect() as db:
        db.execute(
            """
            INSERT INTO trading_runs
                (id, locator_id, owner_id, status, created_at, stage, progress, config_json, instance_json,
                 instance_version, lifecycle_state, protection_state, instance_updated_at)
            VALUES (?, ?, ?, 'queued', ?, 'queued', 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                locator_id,
                owner_id,
                instance["created_at"],
                json.dumps(normalized_config, ensure_ascii=False),
                json.dumps(instance, ensure_ascii=False),
                int(instance.get("version") or INSTANCE_VERSION),
                instance.get("lifecycle_state"),
                (instance.get("protection_status") or {}).get("state"),
                instance.get("updated_at"),
            ),
        )
    return get_trading_run(run_id, owner_id)  # type: ignore[return-value]


def mark_trading_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "orders_json" in fields:
        raw_orders = _loads(fields["orders_json"]) if isinstance(fields["orders_json"], str) else fields["orders_json"]
        if isinstance(raw_orders, list):
            fields["orders_json"] = sanitize_instance_orders(raw_orders)
    if "instance_json" in fields:
        meta = _instance_db_meta(fields.get("instance_json"))
        fields.setdefault("instance_version", meta["instance_version"])
        fields.setdefault("lifecycle_state", meta["lifecycle_state"])
        fields.setdefault("protection_state", meta["protection_state"])
        fields.setdefault("instance_updated_at", meta["instance_updated_at"])
    allowed = {
        "status",
        "started_at",
        "finished_at",
        "stage",
        "progress",
        "scan_results_json",
        "council_json",
        "selections_json",
        "orders_json",
        "instance_json",
        "instance_version",
        "lifecycle_state",
        "protection_state",
        "instance_updated_at",
        "error",
    }
    assignments = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        assignments.append(f"{key} = ?")
        if key.endswith("_json") and not isinstance(value, str):
            values.append(json.dumps(value, ensure_ascii=False))
        else:
            values.append(value)
    if not assignments:
        return
    values.append(run_id)
    with _connect() as db:
        db.execute(f"UPDATE trading_runs SET {', '.join(assignments)} WHERE id = ?", tuple(values))


def record_order_journal(
    *,
    owner_id: str = LOCAL_OWNER_ID,
    run_id: str | None = None,
    client_order_key: str | None = None,
    action: str,
    phase: str,
    broker: str | None = None,
    account_ref: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    quantity: float | None = None,
    price: float | None = None,
    order_id: str | None = None,
    status: str | None = None,
    detail: Any = None,
) -> None:
    """Append one append-only order-journal row. Best-effort: never raises into
    the broker-submit hot path (a journaling failure must not block or break a
    real order)."""
    try:
        init_trading_db()
        detail_json = None
        if detail is not None:
            try:
                detail_json = json.dumps(detail, ensure_ascii=False, default=str)[:8000]
            except (TypeError, ValueError):
                detail_json = str(detail)[:8000]
        with _connect() as db:
            db.execute(
                """
                INSERT INTO trading_order_journal
                    (owner_id, run_id, client_order_key, action, phase, broker, account_ref,
                     symbol, side, quantity, price, order_id, status, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalize_owner_id(owner_id), run_id, client_order_key, str(action), str(phase),
                    broker, account_ref, symbol, side,
                    float(quantity) if quantity is not None else None,
                    float(price) if price is not None else None,
                    order_id, status, detail_json, utc_now(),
                ),
            )
    except Exception:
        # Journaling is observability/idempotency support, not a hard gate.
        pass


def find_recent_order_journal(client_order_key: str, *, within_seconds: int = 600) -> list[dict[str, Any]]:
    """Return journal rows for a client_order_key (most recent first) so a retry
    can detect an already-submitted / in-flight order before re-submitting."""
    if not client_order_key:
        return []
    try:
        init_trading_db()
        with _connect() as db:
            rows = db.execute(
                """
                SELECT action, phase, order_id, status, created_at, detail_json
                FROM trading_order_journal
                WHERE client_order_key = ?
                ORDER BY id DESC
                LIMIT 50
                """,
                (client_order_key,),
            ).fetchall()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(within_seconds or 600)))
    for row in rows or []:
        item = dict(row) if not isinstance(row, dict) else row
        try:
            created = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        out.append(item)
    return out


def mark_interrupted_trading_runs(reason: str | None = None) -> int:
    init_trading_db()
    message = reason or "trading run was interrupted by server restart before completion"
    finished_at = utc_now()
    with _connect() as db:
        rows = db.execute(
            """
            SELECT id, instance_json
            FROM trading_runs
            WHERE status IN ('queued', 'running')
            """
        ).fetchall()
        for row in rows:
            instance = _loads(row["instance_json"]) or {
                "instance_id": row["id"],
                "event_timeline": [],
            }
            append_instance_event(instance, "interrupted", message, lifecycle_state="blocked", status="error")
            db.execute(
                """
                UPDATE trading_runs
                SET status = 'failed', stage = 'interrupted', finished_at = ?, error = ?, instance_json = ?,
                    instance_version = ?, lifecycle_state = ?, protection_state = ?, instance_updated_at = ?
                WHERE id = ?
                """,
                (
                    finished_at,
                    message,
                    json.dumps(instance, ensure_ascii=False),
                    int(instance.get("version") or INSTANCE_VERSION),
                    instance.get("lifecycle_state"),
                    (instance.get("protection_status") or {}).get("state"),
                    instance.get("updated_at"),
                    row["id"],
                ),
            )
        return len(rows)


def get_trading_run(run_id: str, owner_id: str | None = None, *, light: bool = False) -> dict[str, Any] | None:
    init_trading_db()
    owner_clause = ""
    params: tuple[Any, ...] = (run_id, run_id)
    if owner_id is not None:
        owner_clause = "AND owner_id = ?"
        params = (run_id, run_id, normalize_owner_id(owner_id))
    # Light mode skips scan_results_json + council_json + selections_json (entry-time data
    # that never changes after the run starts) to reduce polling payload by ~50-150 KB per request.
    columns = (
        "id, locator_id, owner_id, status, created_at, started_at, finished_at, stage, progress, "
        "config_json, orders_json, instance_json, lifecycle_state, protection_state, instance_updated_at, error"
        if light
        else "*"
    )
    with _connect() as db:
        row = db.execute(
            f"SELECT {columns} FROM trading_runs WHERE (id = ? OR locator_id = ?) {owner_clause}",
            params,
        ).fetchone()
    if not row:
        return None
    return _row_to_run_light(row) if light else _row_to_run(row)


def delete_trading_run(run_id: str, owner_id: str | None = None) -> bool:
    init_trading_db()
    owner_clause = ""
    params: tuple[Any, ...] = (run_id,)
    if owner_id is not None:
        owner_clause = "AND owner_id = ?"
        params = (run_id, normalize_owner_id(owner_id))
    with _connect() as db:
        cursor = db.execute(f"DELETE FROM trading_runs WHERE id = ? {owner_clause}", params)
        return cursor.rowcount > 0


def list_trading_runs(owner_id: str | None = None, limit: int = 20, summary: bool = True) -> list[dict[str, Any]]:
    init_trading_db()
    safe_limit = max(1, min(limit, 100))
    owner_clause = ""
    params: tuple[Any, ...]
    if owner_id is not None:
        owner_clause = "WHERE owner_id = ?"
        params = (normalize_owner_id(owner_id), safe_limit)
    else:
        params = (safe_limit,)
    with _connect() as db:
        if summary:
            rows = db.execute(
                f"""
                SELECT id, locator_id, owner_id, status, created_at, started_at, finished_at, stage, progress,
                       selections_json, orders_json, lifecycle_state, protection_state,
                       instance_updated_at, error
                FROM trading_runs
                {owner_clause}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [_row_to_run_summary(row) for row in rows]
        rows = db.execute(f"SELECT * FROM trading_runs {owner_clause} ORDER BY created_at DESC LIMIT ?", params).fetchall()
    return [_row_to_run(row) for row in rows]


def list_monitorable_trading_runs(limit: int = 100) -> list[dict[str, Any]]:
    init_trading_db()
    safe_limit = max(1, min(int(limit or 100), 500))
    with _connect() as db:
        rows = db.execute(
            """
            SELECT id, locator_id, owner_id, status, created_at, started_at, finished_at, stage, progress,
                   config_json, orders_json, instance_json, lifecycle_state, protection_state,
                   instance_updated_at, error
            FROM trading_runs
            WHERE (
                    status IN ('queued', 'running', 'succeeded')
                    OR (
                        status = 'failed'
                        AND COALESCE(lifecycle_state, 'created') IN (
                            'monitoring', 'manual_intervention_required', 'stop_failed',
                            'exiting', 'unprotected', 'partial_fill', 'protected', 'open'
                        )
                    )
                  )
              AND COALESCE(lifecycle_state, 'created') NOT IN ('closed', 'reviewed', 'blocked')
              AND (
                    orders_json IS NOT NULL
                 OR instance_json IS NOT NULL
                 OR COALESCE(protection_state, 'not_started') NOT IN ('not_started', 'completed')
              )
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [_row_to_monitorable_run(row) for row in rows]


def trading_readiness_risk_snapshot(owner_id: str, recent_limit: int = 50) -> dict[str, Any]:
    init_trading_db()
    owner_id = normalize_owner_id(owner_id)
    safe_limit = max(1, min(int(recent_limit or 50), 200))
    start_utc, end_utc = _today_et_utc_bounds()
    start_dt = datetime.fromisoformat(start_utc)
    end_dt = datetime.fromisoformat(end_utc)
    with _connect() as db:
        today_rows = db.execute(
            """
            SELECT id, status, created_at, lifecycle_state, protection_state, instance_json, orders_json
            FROM trading_runs
            WHERE owner_id = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (owner_id,),
        ).fetchall()
        recent_rows = db.execute(
            """
            SELECT id, status, created_at, lifecycle_state, protection_state, instance_json, orders_json
            FROM trading_runs
            WHERE owner_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, safe_limit),
        ).fetchall()

    consecutive_failures = 0
    active_unprotected = 0
    active_runs = 0
    manual_attention = 0
    consecutive_failure_runs: list[dict[str, Any]] = []
    manual_attention_runs: list[dict[str, Any]] = []
    active_unprotected_runs: list[dict[str, Any]] = []
    counting_failures = True
    today_trade_run_count = 0
    for row in today_rows or []:
        row_dt = _parse_utc_datetime(row["created_at"])
        if not row_dt or row_dt < start_dt or row_dt >= end_dt:
            continue
        instance = _loads(row["instance_json"]) or {}
        protection = instance.get("protection_status") if isinstance(instance.get("protection_status"), dict) else {}
        state = str(row["lifecycle_state"] or instance.get("lifecycle_state") or "")
        if _run_counts_for_risk(row, state, protection):
            today_trade_run_count += 1

    for row in recent_rows:
        instance = _loads(row["instance_json"]) or {}
        protection = instance.get("protection_status") if isinstance(instance.get("protection_status"), dict) else {}
        state = str(row["lifecycle_state"] or instance.get("lifecycle_state") or "")
        protection_state = str(row["protection_state"] or protection.get("state") or "")
        requires_attention = bool(protection.get("requires_manual_attention")) or state in {
            "manual_intervention_required",
            "stop_failed",
        }
        if requires_attention:
            manual_attention += 1
            manual_attention_runs.append({
                "id": row["id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "lifecycle_state": state,
                "protection_state": protection_state,
                "reason": protection.get("stop_failure_reason"),
            })

        row_dt = _parse_utc_datetime(row["created_at"])
        within_today = bool(row_dt and row_dt >= start_dt and row_dt < end_dt)
        risk_relevant = _run_counts_for_risk(row, state, protection)
        if counting_failures and within_today and (requires_attention or (row["status"] == "failed" and risk_relevant)):
            consecutive_failures += 1
            consecutive_failure_runs.append({
                "id": row["id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "lifecycle_state": state,
                "protection_state": protection_state,
            })
        elif counting_failures:
            counting_failures = False

        if state in {"closed", "reviewed", "blocked"}:
            continue
        active_runs += 1
        unprotected_quantity = int(float(protection.get("unprotected_quantity") or 0))
        active_unprotected += unprotected_quantity
        if unprotected_quantity > 0:
            active_unprotected_runs.append({
                "id": row["id"],
                "created_at": row["created_at"],
                "lifecycle_state": state,
                "protection_state": protection_state,
                "unprotected_quantity": unprotected_quantity,
            })

    return {
        "today_run_count": today_trade_run_count,
        "consecutive_failures": consecutive_failures,
        "consecutive_failure_runs": consecutive_failure_runs[:5],
        "active_unprotected_quantity": active_unprotected,
        "active_unprotected_runs": active_unprotected_runs[:5],
        "active_run_count": active_runs,
        "manual_attention_count": manual_attention,
        "manual_attention_runs": manual_attention_runs[:5],
        "sampled_recent_count": len(recent_rows),
    }


def _run_counts_for_risk(row: Any, state: str, protection: dict[str, Any]) -> bool:
    """Return true only for runs that exposed capital or need operator action.

    Auto-trade uses trading_runs for scan/decision audit rows too. A failed
    decision-gate run with zero orders is an observation result, not a trading
    failure, so it should not consume daily trade slots or trip consecutive
    failure breakers.
    """
    orders = _loads(row["orders_json"]) if "orders_json" in row.keys() else None
    has_orders = bool(orders) if isinstance(orders, list) else bool(orders)
    if has_orders:
        return True
    if bool(protection.get("requires_manual_attention")):
        return True
    if int(float(protection.get("unprotected_quantity") or 0)) > 0:
        return True
    if state in {"manual_intervention_required", "stop_failed", "unprotected", "partial_fill", "exiting", "monitoring", "open"}:
        return True
    return False


def _parse_utc_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def trading_runtime_counts(owner_id: str | None = None) -> dict[str, Any]:
    init_trading_db()
    cache_key = owner_id or "__all__"
    hit, cached = _runtime_counts_cache.get(cache_key)
    if hit:
        return cached  # type: ignore[return-value]
    owner_clause = ""
    params: tuple[Any, ...] = ()
    if owner_id is not None:
        owner_clause = "WHERE owner_id = ?"
        params = (normalize_owner_id(owner_id),)
    with _connect() as db:
        rows = db.execute(
            f"""
            SELECT status, COALESCE(lifecycle_state, 'created') AS lifecycle_state,
                   COALESCE(protection_state, 'not_started') AS protection_state,
                   COUNT(*) AS count
            FROM trading_runs
            {owner_clause}
            GROUP BY status, COALESCE(lifecycle_state, 'created'), COALESCE(protection_state, 'not_started')
            """,
            params,
        ).fetchall()
    total = 0
    active = 0
    attention = 0
    rows_out = []
    for row in rows:
        count = int(row["count"] or 0)
        total += count
        lifecycle = str(row["lifecycle_state"] or "")
        protection = str(row["protection_state"] or "")
        if lifecycle not in {"closed", "reviewed", "blocked"}:
            active += count
        if lifecycle in {"manual_intervention_required", "stop_failed"} or protection in {"stop_failed", "manual_intervention_required", "strategy_exit_failed"}:
            attention += count
        rows_out.append(
            {
                "status": row["status"],
                "lifecycle_state": lifecycle,
                "protection_state": protection,
                "count": count,
            }
        )
    result = {"total": total, "active": active, "attention": attention, "groups": rows_out}
    _runtime_counts_cache.set(cache_key, result)
    return result


def _invalidate_runtime_counts_cache() -> None:
    """Call after any bulk status change that would shift the GROUP BY counts."""
    _runtime_counts_cache.clear()


def save_capital_snapshot(
    owner_id: str,
    snapshot_date_et: str,
    account_name: str,
    total_capital: float,
    assets_payload: list[dict[str, Any]],
    executions_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    init_trading_db()
    owner_id = normalize_owner_id(owner_id)
    primary_assets = assets_payload[0] if assets_payload else {}
    now = utc_now()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO trading_capital_snapshots
                (owner_id, snapshot_date_et, created_at, account_name, total_capital,
                 net_assets, total_cash, buy_power, risk_level, assets_json, executions_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, snapshot_date_et) DO UPDATE SET
                created_at = excluded.created_at,
                account_name = excluded.account_name,
                total_capital = excluded.total_capital,
                net_assets = excluded.net_assets,
                total_cash = excluded.total_cash,
                buy_power = excluded.buy_power,
                risk_level = excluded.risk_level,
                assets_json = excluded.assets_json,
                executions_json = excluded.executions_json
            """,
            (
                owner_id,
                snapshot_date_et,
                now,
                account_name,
                total_capital,
                _coerce_number(primary_assets.get("net_assets")),
                _coerce_number(primary_assets.get("total_cash")),
                _coerce_number(primary_assets.get("buy_power")),
                primary_assets.get("risk_level"),
                json.dumps(assets_payload, ensure_ascii=False),
                json.dumps(executions_payload, ensure_ascii=False),
            ),
        )
    return get_capital_snapshot(owner_id, snapshot_date_et)  # type: ignore[return-value]


def get_capital_snapshot(owner_id: str, snapshot_date_et: str) -> dict[str, Any] | None:
    init_trading_db()
    with _connect() as db:
        row = db.execute(
            """
            SELECT * FROM trading_capital_snapshots
            WHERE owner_id = ? AND snapshot_date_et = ?
            """,
            (normalize_owner_id(owner_id), snapshot_date_et),
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def list_capital_snapshots(owner_id: str, days: int = 30) -> list[dict[str, Any]]:
    init_trading_db()
    safe_days = max(1, min(int(days or 30), 365))
    with _connect() as db:
        rows = db.execute(
            """
            SELECT * FROM trading_capital_snapshots
            WHERE owner_id = ?
            ORDER BY snapshot_date_et DESC
            LIMIT ?
            """,
            (normalize_owner_id(owner_id), safe_days),
        ).fetchall()
    return list(reversed([_row_to_snapshot(row) for row in rows]))


def normalize_trading_config(config: dict[str, Any] | None) -> dict[str, Any]:
    source = config or {}
    source_has_single_instance_enabled = "single_instance_enabled" in source
    normalized = dict(DEFAULT_TRADING_CONFIG)
    normalized.update({key: value for key, value in source.items() if key in normalized})
    normalized["universe"] = _normalize_universe(normalized.get("universe"))
    normalized["total_capital"] = max(float(normalized.get("total_capital") or 0), 0)
    normalized["top_n"] = max(1, min(int(normalized.get("top_n") or 1), 20))
    normalized["max_per_symbol"] = max(0, min(int(normalized.get("max_per_symbol") or 1), 20))
    normalized["default_stop_loss_pct"] = max(1.0, min(float(normalized.get("default_stop_loss_pct") or 25), 95.0))
    normalized["default_take_profit_pct"] = max(1.0, min(float(normalized.get("default_take_profit_pct") or 30), 500.0))
    normalized["tiered_take_profit_enabled"] = bool(normalized.get("tiered_take_profit_enabled"))
    normalized["default_take_profit_1_pct"] = max(1.0, min(float(normalized.get("default_take_profit_1_pct") or 20), 500.0))
    normalized["default_take_profit_2_pct"] = max(normalized["default_take_profit_1_pct"], min(float(normalized.get("default_take_profit_2_pct") or 35), 500.0))
    normalized["wait_for_fill_seconds"] = max(0, min(int(normalized.get("wait_for_fill_seconds") or 0), 60))
    normalized["entry_order_type"] = _normalize_entry_order_type(normalized.get("entry_order_type"))
    normalized["exit_order_type"] = _normalize_exit_order_type(normalized.get("exit_order_type"))
    normalized["broker"] = _normalize_broker(normalized.get("broker"))
    if normalized["broker"] == "longbridge":
        normalized["broker_account"] = None
    elif normalized.get("broker_account"):
        normalized["broker_account"] = str(normalized.get("broker_account") or "").strip()
    normalized["market_data_source"] = _normalize_market_data_source(normalized.get("market_data_source"))
    normalized["strategy_modes"] = normalize_strategy_modes(normalized.get("strategy_modes"))
    normalized["strategy_auto_execute_enabled"] = bool(normalized.get("strategy_auto_execute_enabled"))
    normalized["strategy_unwind_on_failure"] = bool(normalized.get("strategy_unwind_on_failure"))
    normalized["run_time_et"] = _normalize_time(str(normalized.get("run_time_et") or "10:30"))
    normalized["multi_instance_enabled"] = bool(normalized.get("multi_instance_enabled"))
    normalized["single_instance_enabled"] = (
        bool(normalized.get("single_instance_enabled"))
        if source_has_single_instance_enabled
        else not normalized["multi_instance_enabled"]
    )
    normalized["schedule_profile"] = _normalize_profile_id(normalized.get("schedule_profile"))
    normalized["schedule_slots"] = _normalize_schedule_slots(normalized.get("schedule_slots"))
    normalized["prompt_template"] = str(normalized.get("prompt_template") or DEFAULT_TRADING_PROMPT)
    normalized["live_enabled"] = bool(normalized.get("live_enabled"))
    normalized["use_ai"] = bool(normalized.get("use_ai"))
    normalized["council"] = bool(normalized.get("council")) if normalized.get("use_ai") else False
    normalized["ai_adjust_allocation"] = bool(normalized.get("ai_adjust_allocation"))
    normalized["ai_adjust_stop_loss"] = bool(normalized.get("ai_adjust_stop_loss"))
    normalized["ai_adjust_take_profit"] = bool(normalized.get("ai_adjust_take_profit"))
    normalized["force_no_overnight"] = bool(normalized.get("force_no_overnight"))
    normalized["max_allocation_pct_per_trade"] = max(0.0, min(float(normalized.get("max_allocation_pct_per_trade") or 0.0), 1.0))
    normalized["decision_directive"] = str(normalized.get("decision_directive") or "")[:2000]
    normalized["software_stop_enabled"] = bool(normalized.get("software_stop_enabled"))
    normalized["software_take_profit_enabled"] = bool(normalized.get("software_take_profit_enabled"))
    normalized["risk_max_daily_runs"] = max(1, min(int(normalized.get("risk_max_daily_runs") or 3), 20))
    normalized["risk_max_consecutive_failures"] = max(1, min(int(normalized.get("risk_max_consecutive_failures") or 2), 10))
    normalized["risk_max_unprotected_quantity"] = max(0, min(int(normalized.get("risk_max_unprotected_quantity") or 0), 1000))
    normalized["risk_max_single_stop_loss_pct"] = max(1.0, min(float(normalized.get("risk_max_single_stop_loss_pct") or 45), 95.0))
    normalized["risk_require_protection_for_market_order"] = bool(normalized.get("risk_require_protection_for_market_order"))
    normalized["low_gate_enabled"] = bool(normalized.get("low_gate_enabled"))
    normalized["dry_run"] = bool(normalized.get("dry_run"))
    if not isinstance(normalized.get("analysis_modules"), dict):
        normalized["analysis_modules"] = dict(DEFAULT_TRADING_CONFIG["analysis_modules"])
    else:
        modules = dict(DEFAULT_TRADING_CONFIG["analysis_modules"])
        modules.update({key: bool(value) for key, value in normalized["analysis_modules"].items() if key in modules})
        normalized["analysis_modules"] = modules
    return normalized


def _normalize_schedule_slots(value: Any) -> list[dict[str, Any]]:
    raw_slots = value if isinstance(value, list) and value else DEFAULT_SCHEDULE_SLOTS
    slots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_slots):
        if not isinstance(raw, dict):
            continue
        slot_id = _normalize_profile_id(raw.get("slot_id") or raw.get("id") or f"slot_{index + 1}")
        if not slot_id or slot_id in seen:
            continue
        seen.add(slot_id)
        modes = normalize_strategy_modes(raw.get("strategy_modes") or raw.get("modes") or ["single_leg"])
        slots.append(
            {
                "slot_id": slot_id,
                "label": str(raw.get("label") or slot_id).strip()[:40],
                "time_et": _normalize_time(str(raw.get("time_et") or raw.get("time") or "10:30")),
                "action": _normalize_slot_action(raw.get("action")),
                "strategy_modes": modes,
                "capital_pct": max(0.0, min(float(raw.get("capital_pct") or 0), 1.0)),
                "gate_profile": _normalize_profile_id(raw.get("gate_profile") or "default"),
                "allow_new_positions": bool(raw.get("allow_new_positions", True)),
                "force_no_overnight": bool(raw.get("force_no_overnight", False)),
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    return sorted(slots, key=lambda item: item["time_et"])


def _normalize_slot_action(value: Any) -> str:
    normalized = str(value or "open_or_adjust").strip().lower()
    return normalized if normalized in {"scan_open", "open_or_adjust", "reduce_or_exit", "risk_review"} else "open_or_adjust"


def _normalize_profile_id(value: Any) -> str:
    text = str(value or "default").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "_", text)
    return text[:48] or "default"


def _normalize_universe(value: Any) -> list[str]:
    use_default_when_empty = value is None
    if isinstance(value, str):
        raw_items = re.split(r"[,，;；\s]+", value)
    elif isinstance(value, list):
        raw_items = value
    elif value is None:
        raw_items = DEFAULT_UNIVERSE
    else:
        raw_items = DEFAULT_UNIVERSE
        use_default_when_empty = True
    output = []
    for item in raw_items:
        symbol = _normalize_symbol(item)
        if symbol and symbol not in output:
            output.append(symbol)
    if output:
        return output
    return list(DEFAULT_UNIVERSE) if use_default_when_empty else []


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if symbol.startswith("$"):
        symbol = symbol[1:]
    if symbol.endswith(".US"):
        symbol = symbol[:-3]
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,11}", symbol):
        return ""
    return symbol


def _normalize_entry_order_type(value: Any) -> str:
    return adaptive_pricing.normalize_order_type(value)


def _normalize_exit_order_type(value: Any) -> str:
    return adaptive_pricing.normalize_order_type(value)


def _normalize_market_data_source(value: Any) -> str:
    normalized = str(value or "thetadata").strip().lower()
    if normalized in {"", "auto", "yfinance"}:
        return "thetadata"
    return normalized if normalized in {"longbridge", "thetadata"} else "thetadata"


def _normalize_broker(value: Any) -> str:
    normalized = str(value or "longbridge").strip().lower()
    return normalized if normalized in {"longbridge", "alpaca", "usmart"} else "longbridge"


def _normalize_time(value: str) -> str:
    parts = value.strip().split(":")
    if len(parts) != 2:
        return "10:30"
    hour = max(0, min(int(parts[0]), 23))
    minute = max(0, min(int(parts[1]), 59))
    return f"{hour:02d}:{minute:02d}"


def _saved_last_run_date_for_config(owner_id: str, config: dict[str, Any]) -> str | None:
    current = get_trading_config(owner_id)
    existing = current.get("last_run_date_et")
    today = datetime.now(ET).date().isoformat()
    if not config.get("live_enabled"):
        return existing
    if config.get("multi_instance_enabled"):
        return existing
    now = datetime.now(ET)
    run_hour, run_minute = [int(part) for part in str(config.get("run_time_et") or "10:30").split(":", 1)]
    run_minutes = run_hour * 60 + run_minute
    current_minutes = now.hour * 60 + now.minute
    if now.weekday() >= 5 or current_minutes < run_minutes:
        return existing
    if existing == today:
        return existing
    if not current.get("live_enabled"):
        return today
    if existing is None:
        return today
    return existing


def _row_to_run(row: Any) -> dict[str, Any]:
    config = _loads(row["config_json"]) or {}
    locator_id = row["locator_id"] if "locator_id" in row.keys() and row["locator_id"] else _locator_id("TRD", row["id"])
    config.setdefault("locator_id", locator_id)
    orders = sanitize_instance_orders(_loads(row["orders_json"]) or [])
    instance = hydrate_trade_instance(
        _loads(row["instance_json"]),
        run_id=row["id"],
        owner_id=row["owner_id"],
        config=config,
        orders=orders,
        created_at=to_et_iso(row["created_at"]),
    )
    return {
        "id": row["id"],
        "locator_id": locator_id,
        "owner_id": row["owner_id"],
        "status": row["status"],
        "created_at": to_et_iso(row["created_at"]),
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "stage": row["stage"],
        "progress": int(row["progress"] or 0),
        "config": config,
        "scan_results": _loads(row["scan_results_json"]) or [],
        "council": _loads(row["council_json"]) or {},
        "selections": _loads(row["selections_json"]) or [],
        "orders": orders,
        "trade_instance": instance,
        "error": row["error"],
    }


def _row_to_monitorable_run(row: Any) -> dict[str, Any]:
    config = _loads(row["config_json"]) or {}
    locator_id = row["locator_id"] if "locator_id" in row.keys() and row["locator_id"] else _locator_id("TRD", row["id"])
    config.setdefault("locator_id", locator_id)
    orders = sanitize_instance_orders(_loads(row["orders_json"]) or [])
    instance = hydrate_trade_instance(
        _loads(row["instance_json"]),
        run_id=row["id"],
        owner_id=row["owner_id"],
        config=config,
        orders=orders,
        created_at=to_et_iso(row["created_at"]),
    )
    return {
        "id": row["id"],
        "locator_id": locator_id,
        "owner_id": row["owner_id"],
        "status": row["status"],
        "created_at": to_et_iso(row["created_at"]),
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "stage": row["stage"],
        "progress": int(row["progress"] or 0),
        "config": config,
        "orders": orders,
        "trade_instance": instance,
        "error": row["error"],
    }


def _row_to_run_light(row: Any) -> dict[str, Any]:
    # Light projection used by polling: omits scan_results/council/selections (entry-time blobs).
    # Returns the same shape as _row_to_run so the frontend can use either response interchangeably.
    config = _loads(row["config_json"]) or {}
    locator_id = row["locator_id"] if "locator_id" in row.keys() and row["locator_id"] else _locator_id("TRD", row["id"])
    config.setdefault("locator_id", locator_id)
    orders = sanitize_instance_orders(_loads(row["orders_json"]) or [])
    instance = hydrate_trade_instance(
        _loads(row["instance_json"]),
        run_id=row["id"],
        owner_id=row["owner_id"],
        config=config,
        orders=orders,
        created_at=to_et_iso(row["created_at"]),
    )
    return {
        "id": row["id"],
        "locator_id": locator_id,
        "owner_id": row["owner_id"],
        "status": row["status"],
        "created_at": to_et_iso(row["created_at"]),
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "stage": row["stage"],
        "progress": int(row["progress"] or 0),
        "config": config,
        "scan_results": None,
        "council": None,
        "selections": None,
        "orders": orders,
        "trade_instance": instance,
        "error": row["error"],
        "_payload_mode": "light",
    }


def _row_to_run_summary(row: Any) -> dict[str, Any]:
    selections = _loads(row["selections_json"]) or []
    orders = _loads(row["orders_json"]) or []
    stored_lifecycle = row["lifecycle_state"] or "created"
    if orders and stored_lifecycle not in {"closed", "reviewed"}:
        protection = build_protection_status(orders)
        protection_state = protection.get("state") or row["protection_state"] or "not_started"
        lifecycle_state = lifecycle_from_orders(orders)
    else:
        protection = {"state": row["protection_state"] or "not_started"}
        protection_state = row["protection_state"] or "not_started"
        lifecycle_state = stored_lifecycle
    locator_id = row["locator_id"] if "locator_id" in row.keys() and row["locator_id"] else _locator_id("TRD", row["id"])
    return {
        "id": row["id"],
        "locator_id": locator_id,
        "owner_id": row["owner_id"],
        "status": row["status"],
        "created_at": to_et_iso(row["created_at"]),
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "stage": row["stage"],
        "progress": int(row["progress"] or 0),
        "config": {},
        "scan_results": [],
        "council": {},
        "selections": [],
        "orders": [],
        "selection_count": len(selections) if isinstance(selections, list) else 0,
        "order_count": len(orders) if isinstance(orders, list) else 0,
        "lifecycle_state": lifecycle_state,
        "protection_state": protection_state,
        "trade_instance": {
            "instance_id": row["id"],
            "locator_id": locator_id,
            "created_at": to_et_iso(row["created_at"]),
            "updated_at": to_et_iso(row["instance_updated_at"]) if row["instance_updated_at"] else None,
            "lifecycle_state": lifecycle_state,
            "protection_status": protection,
            "ai_decision": {"selection_count": len(selections) if isinstance(selections, list) else 0},
            "risk_plan": {},
        },
        "error": row["error"],
    }


def _row_to_snapshot(row: Any) -> dict[str, Any]:
    return {
        "date": row["snapshot_date_et"],
        "time": row["snapshot_date_et"],
        "created_at": to_et_iso(row["created_at"]),
        "account_name": row["account_name"],
        "total_capital": float(row["total_capital"] or 0),
        "net_assets": _coerce_number(row["net_assets"]),
        "total_cash": _coerce_number(row["total_cash"]),
        "buy_power": _coerce_number(row["buy_power"]),
        "risk_level": row["risk_level"],
        "assets": _loads(row["assets_json"]) or [],
        "executions": _loads(row["executions_json"]) or [],
    }


def _backfill_instance_columns(db: Any) -> None:
    rows = db.execute(
        """
        SELECT id, locator_id, owner_id, created_at, config_json, orders_json, instance_json
        FROM trading_runs
        WHERE instance_version < ? OR lifecycle_state IS NULL OR protection_state IS NULL
        """,
        (INSTANCE_VERSION,),
    ).fetchall()
    for row in rows:
        config = _loads(row["config_json"]) or {}
        locator_id = row["locator_id"] or _locator_id("TRD", row["id"])
        config.setdefault("locator_id", locator_id)
        orders = sanitize_instance_orders(_loads(row["orders_json"]) or [])
        instance = hydrate_trade_instance(
            _loads(row["instance_json"]),
            run_id=row["id"],
            owner_id=row["owner_id"],
            config=config,
            orders=orders,
            created_at=to_et_iso(row["created_at"]),
        )
        db.execute(
            """
            UPDATE trading_runs
            SET orders_json = ?, instance_json = ?, instance_version = ?, lifecycle_state = ?,
                protection_state = ?, instance_updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(orders, ensure_ascii=False),
                json.dumps(instance, ensure_ascii=False),
                int(instance.get("version") or INSTANCE_VERSION),
                instance.get("lifecycle_state"),
                (instance.get("protection_status") or {}).get("state"),
                instance.get("updated_at"),
                row["id"],
            ),
        )


def _instance_db_meta(value: Any) -> dict[str, Any]:
    instance = _loads(value) if isinstance(value, str) else value
    if not isinstance(instance, dict):
        instance = {}
    protection = instance.get("protection_status") if isinstance(instance.get("protection_status"), dict) else {}
    return {
        "instance_version": int(instance.get("version") or INSTANCE_VERSION),
        "lifecycle_state": instance.get("lifecycle_state"),
        "protection_state": protection.get("state"),
        "instance_updated_at": instance.get("updated_at"),
    }


def _coerce_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _today_et_utc_bounds() -> tuple[str, str]:
    today = datetime.now(ET).date()
    start = datetime.combine(today, dt_time.min, tzinfo=ET).astimezone(timezone.utc)
    end = (start + timedelta(days=1)).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def _connect() -> Any:
    return connect()


def _locator_id(prefix: str, source_id: str) -> str:
    compact = "".join(ch for ch in str(source_id or "").upper() if ch.isalnum())
    return f"{prefix}-{(compact or uuid.uuid4().hex.upper())[:12]}"


def _stable_locator_id(prefix: str, source_id: str) -> str:
    digest = hashlib.sha256(str(source_id or uuid.uuid4().hex).encode("utf-8")).hexdigest().upper()
    return f"{prefix}-{digest[:12]}"


def schedule_config_hash(config: dict[str, Any]) -> str:
    normalized = normalize_trading_config(config or {})
    material = {
        "total_capital": normalized.get("total_capital"),
        "schedule_profile": normalized.get("schedule_profile"),
        "schedule_slots": normalized.get("schedule_slots"),
        "strategy_modes": normalized.get("strategy_modes"),
        "low_gate_enabled": normalized.get("low_gate_enabled"),
        "risk_max_daily_runs": normalized.get("risk_max_daily_runs"),
    }
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _backfill_trading_locator_ids(db: Any) -> None:
    rows = db.execute("SELECT id, locator_id FROM trading_runs WHERE locator_id IS NULL OR locator_id = ''").fetchall()
    for row in rows:
        db.execute("UPDATE trading_runs SET locator_id = ? WHERE id = ?", (_locator_id("TRD", row["id"]), row["id"]))


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


init_trading_db()

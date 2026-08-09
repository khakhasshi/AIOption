"""Persistence for the fully-automatic LLM trading feature.

Two tables:
- ``auto_trade_instances`` — one per configured auto-trader (watchlist, cadence,
  broker/dry-run, AI provider, risk preset, rolling cross-cycle memory).
- ``auto_trade_cycles`` — one row per scheduled wake ("cycle"): the audit spine
  holding the LLM plan, decision-gate result, schema-validation result, the
  linked ``trading_runs`` id (when an order was placed), and a summary. Heavy
  order detail lives in the linked trading run (instance_json) + the order
  journal, so a full lifecycle is reconstructable the next morning.

Mirrors the conventions of ``trading_store`` / ``observation_store`` (``?``
placeholders, ``run_db_init_once`` init, dict-like rows).
"""
from __future__ import annotations

import json
import uuid
from datetime import timezone
from typing import Any

from .account_store import LOCAL_OWNER_ID, normalize_owner_id, utc_now
from .db import connect, ensure_column, run_db_init_once
from .time_utils import parse_datetime

MAX_AUTO_TRADE_SYMBOLS = 8
RISK_PRESETS = {"conservative", "balanced", "aggressive"}
DEFAULT_RISK_PRESET = "conservative"
DEFAULT_TOTAL_CAPITAL = 3000.0
MAX_TOTAL_CAPITAL = 10_000_000.0


def _clamp_capital(value: Any, default: float = DEFAULT_TOTAL_CAPITAL) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return default
    if amount != amount:  # NaN guard
        return default
    return max(0.0, min(amount, MAX_TOTAL_CAPITAL))


def _connect() -> Any:
    return connect()


def _loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _utc_iso(value: Any) -> str | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def init_auto_trade_db() -> None:
    run_db_init_once("auto_trade_store", _init_auto_trade_db)


def _init_auto_trade_db() -> None:
    with _connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_trade_instances (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'stopped',
                use_broker INTEGER NOT NULL DEFAULT 0,
                broker TEXT,
                broker_account TEXT,
                ai_provider TEXT,
                symbols_json TEXT NOT NULL DEFAULT '[]',
                interval_minutes INTEGER NOT NULL DEFAULT 5,
                risk_preset TEXT NOT NULL DEFAULT 'conservative',
                total_capital REAL NOT NULL DEFAULT 3000,
                config_json TEXT NOT NULL DEFAULT '{}',
                session_policy TEXT NOT NULL DEFAULT 'regular_only',
                next_run_at TEXT,
                last_run_at TEXT,
                session_date_et TEXT,
                cycles_today INTEGER NOT NULL DEFAULT 0,
                orders_today INTEGER NOT NULL DEFAULT 0,
                realized_pnl_today REAL NOT NULL DEFAULT 0,
                halted_reason TEXT,
                last_cycle_summary_json TEXT,
                memory_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_trade_cycles (
                id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                cycle_index INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                session_state TEXT,
                intraday_phase TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                dry_run INTEGER NOT NULL DEFAULT 1,
                plan_json TEXT,
                decision_gate_json TEXT,
                validation_json TEXT,
                run_ids_json TEXT,
                summary_json TEXT,
                error TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_auto_trade_instances_owner ON auto_trade_instances(owner_id, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_auto_trade_instances_due ON auto_trade_instances(status, next_run_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_auto_trade_cycles_instance ON auto_trade_cycles(instance_id, started_at DESC)")
        # Migration: pre-existing instances (created before per-instance capital) get the default budget.
        ensure_column(db, "auto_trade_instances", "total_capital", "REAL NOT NULL DEFAULT 3000")
        # Migration: loss-based circuit breaker bookkeeping (Tier 1 — added after launch).
        ensure_column(db, "auto_trade_instances", "realized_pnl_today", "REAL NOT NULL DEFAULT 0")
        ensure_column(db, "auto_trade_instances", "halted_reason", "TEXT")


def _instance_locator() -> str:
    return f"AUTO-{uuid.uuid4().hex[:12].upper()}"


def _instance_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["symbols"] = _loads(item.pop("symbols_json", None)) or []
    item["config"] = _loads(item.pop("config_json", None)) or {}
    item["last_cycle_summary"] = _loads(item.pop("last_cycle_summary_json", None))
    item["memory"] = _loads(item.pop("memory_json", None)) or []
    item["use_broker"] = bool(item.get("use_broker"))
    item["total_capital"] = _clamp_capital(item.get("total_capital"))
    return item


def _normalize_symbols(raw: Any) -> list[str]:
    out: list[str] = []
    for s in raw or []:
        sym = str(s).strip().upper()
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= MAX_AUTO_TRADE_SYMBOLS:
            break
    return out


def create_auto_trade_instance(owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_auto_trade_db()
    owner_id = normalize_owner_id(owner_id)
    symbols = _normalize_symbols(payload.get("symbols"))
    preset = str(payload.get("risk_preset") or DEFAULT_RISK_PRESET)
    if preset not in RISK_PRESETS:
        preset = DEFAULT_RISK_PRESET
    interval = max(1, min(int(payload.get("interval_minutes") or 5), 240))
    instance_id = _instance_locator()
    now = utc_now()
    row = {
        "id": instance_id,
        "owner_id": owner_id,
        "name": str(payload.get("name") or "Auto-Trade")[:120],
        "status": "stopped",
        "use_broker": 1 if payload.get("use_broker") else 0,
        "broker": str(payload.get("broker") or "longbridge"),
        "broker_account": (payload.get("broker_account") or None),
        "ai_provider": str(payload.get("ai_provider") or "deepseek"),
        "symbols_json": _dumps(symbols),
        "interval_minutes": interval,
        "risk_preset": preset,
        "total_capital": _clamp_capital(payload.get("total_capital")),
        "config_json": _dumps(payload.get("config") or {}),
        "session_policy": str(payload.get("session_policy") or "regular_only"),
        "next_run_at": None,
        "last_run_at": None,
        "session_date_et": None,
        "cycles_today": 0,
        "orders_today": 0,
        "realized_pnl_today": 0.0,
        "halted_reason": None,
        "last_cycle_summary_json": None,
        "memory_json": _dumps([]),
        "created_at": now,
        "updated_at": now,
    }
    with _connect() as db:
        db.execute(
            """
            INSERT INTO auto_trade_instances
                (id, owner_id, name, status, use_broker, broker, broker_account, ai_provider,
                 symbols_json, interval_minutes, risk_preset, total_capital, config_json, session_policy,
                 next_run_at, last_run_at, session_date_et, cycles_today, orders_today,
                 realized_pnl_today, halted_reason,
                 last_cycle_summary_json, memory_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row[k] for k in (
                "id", "owner_id", "name", "status", "use_broker", "broker", "broker_account", "ai_provider",
                "symbols_json", "interval_minutes", "risk_preset", "total_capital", "config_json", "session_policy",
                "next_run_at", "last_run_at", "session_date_et", "cycles_today", "orders_today",
                "realized_pnl_today", "halted_reason",
                "last_cycle_summary_json", "memory_json", "created_at", "updated_at",
            )),
        )
    return get_auto_trade_instance(instance_id, owner_id)


def get_auto_trade_instance(instance_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
    init_auto_trade_db()
    with _connect() as db:
        if owner_id is not None:
            rows = db.execute(
                "SELECT * FROM auto_trade_instances WHERE id = ? AND owner_id = ?",
                (instance_id, normalize_owner_id(owner_id)),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM auto_trade_instances WHERE id = ?", (instance_id,)).fetchall()
    if not rows:
        return None
    return _instance_from_row(rows[0])


def list_auto_trade_instances(owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    init_auto_trade_db()
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM auto_trade_instances WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
            (normalize_owner_id(owner_id), int(limit)),
        ).fetchall()
    return [_instance_from_row(row) for row in rows or []]


def update_auto_trade_instance(instance_id: str, owner_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    init_auto_trade_db()
    allowed = {
        "name", "status", "use_broker", "broker", "broker_account", "ai_provider",
        "symbols_json", "interval_minutes", "risk_preset", "total_capital", "config_json", "session_policy",
        "next_run_at", "last_run_at", "session_date_et", "cycles_today", "orders_today",
        "realized_pnl_today", "halted_reason",
        "last_cycle_summary_json", "memory_json",
    }
    # Translate friendly keys to *_json columns.
    if "symbols" in changes:
        changes["symbols_json"] = _dumps(_normalize_symbols(changes.pop("symbols")))
    if "total_capital" in changes:
        changes["total_capital"] = _clamp_capital(changes.get("total_capital"))
    if "config" in changes:
        changes["config_json"] = _dumps(changes.pop("config") or {})
    if "last_cycle_summary" in changes:
        changes["last_cycle_summary_json"] = _dumps(changes.pop("last_cycle_summary"))
    if "memory" in changes:
        changes["memory_json"] = _dumps(changes.pop("memory") or [])
    if "next_run_at" in changes and changes["next_run_at"]:
        changes["next_run_at"] = _utc_iso(changes["next_run_at"]) or changes["next_run_at"]
    if "last_run_at" in changes and changes["last_run_at"]:
        changes["last_run_at"] = _utc_iso(changes["last_run_at"]) or changes["last_run_at"]
    if "use_broker" in changes:
        changes["use_broker"] = 1 if changes["use_broker"] else 0
    assignments = []
    values: list[Any] = []
    for key, value in changes.items():
        if key not in allowed:
            continue
        assignments.append(f"{key} = ?")
        values.append(value)
    if not assignments:
        return get_auto_trade_instance(instance_id, owner_id)
    assignments.append("updated_at = ?")
    values.append(utc_now())
    values.extend([instance_id, normalize_owner_id(owner_id)])
    with _connect() as db:
        db.execute(
            f"UPDATE auto_trade_instances SET {', '.join(assignments)} WHERE id = ? AND owner_id = ?",
            tuple(values),
        )
    return get_auto_trade_instance(instance_id, owner_id)


def delete_auto_trade_instance(instance_id: str, owner_id: str) -> None:
    init_auto_trade_db()
    with _connect() as db:
        db.execute("DELETE FROM auto_trade_instances WHERE id = ? AND owner_id = ?", (instance_id, normalize_owner_id(owner_id)))


def list_due_auto_trade_instances(limit: int = 20, *, now: str | None = None) -> list[dict[str, Any]]:
    init_auto_trade_db()
    now_text = now or utc_now()
    now_dt = parse_datetime(now_text)
    with _connect() as db:
        rows = db.execute(
            """
            SELECT * FROM auto_trade_instances
            WHERE status = 'active'
            ORDER BY next_run_at ASC NULLS FIRST
            LIMIT ?
            """,
            (max(int(limit), 1) * 5,),
        ).fetchall()
    due: list[dict[str, Any]] = []
    for row in rows or []:
        next_run_at = row["next_run_at"]
        next_dt = parse_datetime(next_run_at) if next_run_at else None
        if next_dt is None or now_dt is None or next_dt <= now_dt:
            due.append(_instance_from_row(row))
        if len(due) >= int(limit):
            break
    return due


def insert_auto_trade_cycle(
    instance_id: str,
    owner_id: str,
    cycle_index: int,
    *,
    session_state: str | None,
    intraday_phase: str | None,
    dry_run: bool,
) -> str:
    init_auto_trade_db()
    cycle_id = uuid.uuid4().hex
    with _connect() as db:
        db.execute(
            """
            INSERT INTO auto_trade_cycles
                (id, instance_id, owner_id, cycle_index, started_at, session_state, intraday_phase, status, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (cycle_id, instance_id, normalize_owner_id(owner_id), int(cycle_index), utc_now(),
             session_state, intraday_phase, 1 if dry_run else 0),
        )
    return cycle_id


def finish_auto_trade_cycle(cycle_id: str, **fields: Any) -> None:
    init_auto_trade_db()
    allowed = {"status", "plan_json", "decision_gate_json", "validation_json", "run_ids_json", "summary_json", "error"}
    for key in ("plan", "decision_gate", "validation", "run_ids", "summary"):
        if key in fields:
            fields[f"{key}_json" if key != "decision_gate" else "decision_gate_json"] = _dumps(fields.pop(key))
    assignments = ["finished_at = ?"]
    values: list[Any] = [utc_now()]
    for key, value in fields.items():
        if key not in allowed:
            continue
        assignments.append(f"{key} = ?")
        values.append(value if isinstance(value, str) or value is None else _dumps(value))
    values.append(cycle_id)
    with _connect() as db:
        db.execute(f"UPDATE auto_trade_cycles SET {', '.join(assignments)} WHERE id = ?", tuple(values))


def list_auto_trade_cycles(instance_id: str, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    init_auto_trade_db()
    with _connect() as db:
        rows = db.execute(
            """
            SELECT * FROM auto_trade_cycles
            WHERE instance_id = ? AND owner_id = ?
            ORDER BY started_at DESC LIMIT ?
            """,
            (instance_id, normalize_owner_id(owner_id), int(limit)),
        ).fetchall()
    out = []
    for row in rows or []:
        item = dict(row)
        item["dry_run"] = bool(item.get("dry_run"))
        for key in ("plan_json", "decision_gate_json", "validation_json", "run_ids_json", "summary_json"):
            item[key.replace("_json", "")] = _loads(item.pop(key, None))
        out.append(item)
    return out


def get_auto_trade_cycle(cycle_id: str, owner_id: str) -> dict[str, Any] | None:
    init_auto_trade_db()
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM auto_trade_cycles WHERE id = ? AND owner_id = ?",
            (cycle_id, normalize_owner_id(owner_id)),
        ).fetchall()
    if not rows:
        return None
    item = dict(rows[0])
    item["dry_run"] = bool(item.get("dry_run"))
    for key in ("plan_json", "decision_gate_json", "validation_json", "run_ids_json", "summary_json"):
        item[key.replace("_json", "")] = _loads(item.pop(key, None))
    return item

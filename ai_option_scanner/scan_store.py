from __future__ import annotations

import json
import uuid
from typing import Any

from .account_store import LOCAL_OWNER_ID, normalize_owner_id, utc_now
from .db import connect, ensure_column, is_postgres, run_db_init_once
from .scan_events import publish_scan_event
from .time_utils import to_et_iso


def init_scan_db() -> None:
    run_db_init_once("scan_store", _init_scan_db)


def _init_scan_db() -> None:
    with _connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                query TEXT NOT NULL,
                symbol TEXT,
                ai_provider TEXT NOT NULL,
                longbridge_account TEXT NOT NULL,
                market_data_source TEXT NOT NULL DEFAULT 'longbridge',
                use_ai INTEGER NOT NULL,
                council INTEGER NOT NULL,
                mode TEXT,
                used_ai INTEGER,
                answer TEXT,
                payload_json TEXT,
                charts_json TEXT,
                error TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_runs_created_at ON scan_runs(created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_runs_status ON scan_runs(status)")
        _ensure_column(db, "scan_runs", "stage", "TEXT")
        _ensure_column(db, "scan_runs", "progress", "INTEGER")
        _ensure_column(db, "scan_runs", "owner_id", f"TEXT NOT NULL DEFAULT '{LOCAL_OWNER_ID}'")
        _ensure_column(db, "scan_runs", "analysis_modules_json", "TEXT")
        _ensure_column(db, "scan_runs", "strategy_modes_json", "TEXT")
        _ensure_column(db, "scan_runs", "market_data_source", "TEXT NOT NULL DEFAULT 'longbridge'")
        _ensure_column(db, "scan_runs", "option_data_source", "TEXT NOT NULL DEFAULT 'thetadata'")
        _ensure_column(db, "scan_runs", "locator_id", "TEXT")
        _ensure_column(db, "scan_runs", "ai_provider_owner", "TEXT")
        _ensure_column(db, "scan_runs", "source_type", "TEXT")
        _ensure_column(db, "scan_runs", "source_id", "TEXT")
        _ensure_column(db, "scan_runs", "scan_loop_instance_id", "TEXT")
        _backfill_scan_locator_ids(db)
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_runs_owner_created ON scan_runs(owner_id, created_at DESC)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scan_runs_locator_id ON scan_runs(locator_id)")
        _ensure_scan_runs_trigram_indexes(db)


def _ensure_scan_runs_trigram_indexes(db: Any) -> None:
    # Postgres-only: pg_trgm GIN indexes turn the substring LIKE filters in
    # list_scan_runs_with_marks from full scans into index lookups. SQLite
    # can't use indexes for '%foo%' patterns, so this is a no-op there.
    if not is_postgres():
        return
    try:
        db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception:
        # Managed Postgres without the privilege — fall through quietly,
        # the LIKE queries will still work, just unindexed.
        return
    for column in ("query", "symbol", "locator_id"):
        index_name = f"idx_scan_runs_{column}_trgm"
        try:
            db.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON scan_runs USING GIN (LOWER(COALESCE({column}, '')) gin_trgm_ops)"
            )
        except Exception:
            # Index already present in a different shape, or extension partially installed.
            continue


def create_scan_run(
    query: str,
    symbol: str | None,
    ai_provider: str,
    longbridge_account: str,
    use_ai: bool,
    council: bool,
    analysis_modules: dict[str, Any] | None = None,
    strategy_modes: list[str] | None = None,
    market_data_source: str = "longbridge",
    option_data_source: str = "thetadata",
    owner_id: str = LOCAL_OWNER_ID,
    ai_provider_owner: str | None = None,
    source_type: str = "scan",
    source_id: str | None = None,
    scan_loop_instance_id: str | None = None,
) -> dict[str, Any]:
    init_scan_db()
    scan_id = uuid.uuid4().hex
    locator_id = _locator_id("SCN", scan_id)
    owner_id = normalize_owner_id(owner_id)
    normalized_provider_owner = normalize_owner_id(ai_provider_owner) if ai_provider_owner else owner_id
    with _connect() as db:
        db.execute(
            """
            INSERT INTO scan_runs
                (id, locator_id, owner_id, ai_provider_owner, source_type, source_id, scan_loop_instance_id,
                 status, stage, progress, created_at, query, symbol, ai_provider, longbridge_account,
                 market_data_source, option_data_source, use_ai, council, analysis_modules_json, strategy_modes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                locator_id,
                owner_id,
                normalized_provider_owner,
                source_type,
                source_id or "",
                scan_loop_instance_id or "",
                utc_now(),
                query,
                symbol,
                ai_provider,
                longbridge_account,
                market_data_source,
                option_data_source,
                int(use_ai),
                int(council),
                json.dumps(analysis_modules or {}, ensure_ascii=False),
                json.dumps(strategy_modes or [], ensure_ascii=False),
            ),
        )
    return get_scan_run(scan_id, owner_id=owner_id)  # type: ignore[return-value]


def mark_scan_running(scan_id: str) -> None:
    with _connect() as db:
        db.execute(
            "UPDATE scan_runs SET status = 'running', stage = 'starting', progress = 5, started_at = ? WHERE id = ?",
            (utc_now(), scan_id),
        )
    publish_scan_event(scan_id, {"status": "running", "stage": "starting", "progress": 5})


def mark_scan_stage(scan_id: str, stage: str, progress: int) -> None:
    bounded_progress = max(0, min(progress, 99))
    with _connect() as db:
        db.execute(
            "UPDATE scan_runs SET stage = ?, progress = ? WHERE id = ?",
            (stage, bounded_progress, scan_id),
        )
    publish_scan_event(scan_id, {"status": "running", "stage": stage, "progress": bounded_progress})


def mark_scan_succeeded(scan_id: str, result: dict[str, Any]) -> None:
    with _connect() as db:
        db.execute(
            """
            UPDATE scan_runs
            SET status = 'succeeded', stage = 'completed', progress = 100,
                finished_at = ?, mode = ?, used_ai = ?, answer = ?,
                payload_json = ?, charts_json = ?, error = NULL
            WHERE id = ?
            """,
            (
                utc_now(),
                result.get("mode"),
                int(bool(result.get("used_ai"))),
                result.get("answer"),
                json.dumps(result.get("payload"), ensure_ascii=False),
                json.dumps(result.get("charts"), ensure_ascii=False),
                scan_id,
            ),
        )
    # Terminal event carries no result body — clients re-fetch the full run via
    # GET /api/scans/{id}. Keeps the pub/sub payload small and avoids duplicating
    # the (potentially large) charts/payload over two channels.
    publish_scan_event(scan_id, {"status": "succeeded", "stage": "completed", "progress": 100})


def mark_scan_failed(scan_id: str, error: str) -> None:
    with _connect() as db:
        db.execute(
            "UPDATE scan_runs SET status = 'failed', stage = 'failed', finished_at = ?, error = ? WHERE id = ?",
            (utc_now(), error, scan_id),
        )
    publish_scan_event(scan_id, {"status": "failed", "stage": "failed", "progress": 100, "error": error})


def mark_interrupted_scan_runs(reason: str | None = None) -> int:
    init_scan_db()
    message = reason or "scan was interrupted by server restart before completion"
    with _connect() as db:
        cursor = db.execute(
            """
            UPDATE scan_runs
            SET status = 'failed', stage = 'interrupted', finished_at = ?, error = ?
            WHERE status IN ('queued', 'running')
            """,
            (utc_now(), message),
        )
        return cursor.rowcount


def get_scan_run(scan_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
    init_scan_db()
    owner_clause = ""
    params: tuple[Any, ...] = (scan_id, scan_id)
    if owner_id is not None:
        owner_clause = "AND owner_id = ?"
        params = (scan_id, scan_id, normalize_owner_id(owner_id))
    with _connect() as db:
        row = db.execute(f"SELECT * FROM scan_runs WHERE (id = ? OR locator_id = ?) {owner_clause}", params).fetchone()
    return _row_to_scan(row) if row else None


def list_scan_runs(limit: int = 30, owner_id: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
    init_scan_db()
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(int(offset or 0), 0)
    owner_clause = ""
    params: tuple[Any, ...]
    if owner_id is not None:
        owner_clause = "WHERE owner_id = ?"
        params = (normalize_owner_id(owner_id), safe_limit, safe_offset)
    else:
        params = (safe_limit, safe_offset)
    with _connect() as db:
        rows = db.execute(
            f"""
            SELECT id, locator_id, owner_id, ai_provider_owner, source_type, source_id, scan_loop_instance_id, status, stage, progress, created_at, started_at, finished_at,
                   query, symbol, ai_provider, longbridge_account, market_data_source, option_data_source, use_ai,
                   council, analysis_modules_json, strategy_modes_json, mode, used_ai, error
            FROM scan_runs
            {owner_clause}
            ORDER BY created_at DESC
            LIMIT ?
            OFFSET ?
            """,
            params,
        ).fetchall()
    return [_row_to_scan_summary(row) for row in rows]


def list_pending_scan_ids(limit: int = 200) -> list[str]:
    init_scan_db()
    safe_limit = max(1, min(limit, 1000))
    with _connect() as db:
        rows = db.execute(
            """
            SELECT id
            FROM scan_runs
            WHERE status = 'queued'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [str(row["id"]) for row in rows if row and row["id"]]


def scan_result_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if row["status"] != "succeeded":
        return None
    return {
        "answer": row["answer"],
        "locator_id": row.get("locator_id"),
        "used_ai": bool(row["used_ai"]),
        "mode": row["mode"],
        "ai_provider": row["ai_provider"],
        "longbridge_account": row["longbridge_account"],
        "market_data_source": row.get("market_data_source") or "longbridge",
        "option_data_source": row.get("option_data_source") or "thetadata",
        "payload": row.get("payload") or {},
        "charts": row.get("charts") or {},
    }


def _connect() -> Any:
    return connect()


def _ensure_column(db: Any, table: str, column: str, declaration: str) -> None:
    ensure_column(db, table, column, declaration)


def _row_to_scan_summary(row: Any) -> dict[str, Any]:
    analysis_modules = _loads(row["analysis_modules_json"]) if _row_has(row, "analysis_modules_json") else None
    strategy_modes = _loads(row["strategy_modes_json"]) if _row_has(row, "strategy_modes_json") else None
    return {
        "id": row["id"],
        "locator_id": row["locator_id"] if _row_has(row, "locator_id") and row["locator_id"] else _locator_id("SCN", row["id"]),
        "owner_id": row["owner_id"] if _row_has(row, "owner_id") else LOCAL_OWNER_ID,
        "ai_provider_owner": _scan_ai_provider_owner(row),
        "source_type": row["source_type"] if _row_has(row, "source_type") else "",
        "source_id": row["source_id"] if _row_has(row, "source_id") else "",
        "scan_loop_instance_id": row["scan_loop_instance_id"] if _row_has(row, "scan_loop_instance_id") else "",
        "status": row["status"],
        "stage": row["stage"],
        "progress": int(row["progress"] or 0),
        "created_at": to_et_iso(row["created_at"]),
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "query": row["query"],
        "symbol": row["symbol"],
        "ai_provider": row["ai_provider"],
        "longbridge_account": row["longbridge_account"],
        "market_data_source": row["market_data_source"] if _row_has(row, "market_data_source") else "longbridge",
        "option_data_source": row["option_data_source"] if _row_has(row, "option_data_source") else "thetadata",
        "use_ai": bool(row["use_ai"]),
        "council": bool(row["council"]),
        "analysis_modules": analysis_modules or {},
        "strategy_modes": strategy_modes or [],
        "mode": row["mode"],
        "used_ai": bool(row["used_ai"]) if row["used_ai"] is not None else None,
        "error": row["error"],
        "result": None,
    }


def _row_to_scan(row: Any) -> dict[str, Any]:
    payload = _loads(row["payload_json"])
    charts = _loads(row["charts_json"])
    analysis_modules = _loads(row["analysis_modules_json"]) if "analysis_modules_json" in row.keys() else None
    strategy_modes = _loads(row["strategy_modes_json"]) if "strategy_modes_json" in row.keys() else None
    return {
        "id": row["id"],
        "locator_id": row["locator_id"] if "locator_id" in row.keys() and row["locator_id"] else _locator_id("SCN", row["id"]),
        "owner_id": row["owner_id"] if "owner_id" in row.keys() else LOCAL_OWNER_ID,
        "ai_provider_owner": _scan_ai_provider_owner(row),
        "source_type": row["source_type"] if "source_type" in row.keys() else "",
        "source_id": row["source_id"] if "source_id" in row.keys() else "",
        "scan_loop_instance_id": row["scan_loop_instance_id"] if "scan_loop_instance_id" in row.keys() else "",
        "status": row["status"],
        "stage": row["stage"],
        "progress": int(row["progress"] or 0),
        "created_at": to_et_iso(row["created_at"]),
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "query": row["query"],
        "symbol": row["symbol"],
        "ai_provider": row["ai_provider"],
        "longbridge_account": row["longbridge_account"],
        "market_data_source": row["market_data_source"] if "market_data_source" in row.keys() else "longbridge",
        "option_data_source": row["option_data_source"] if "option_data_source" in row.keys() else "thetadata",
        "use_ai": bool(row["use_ai"]),
        "council": bool(row["council"]),
        "analysis_modules": analysis_modules or {},
        "strategy_modes": strategy_modes or [],
        "mode": row["mode"],
        "used_ai": bool(row["used_ai"]) if row["used_ai"] is not None else None,
        "answer": row["answer"],
        "payload": payload,
        "charts": charts,
        "error": row["error"],
        "result": scan_result_from_row(
            {
                "status": row["status"],
                "answer": row["answer"],
                "locator_id": row["locator_id"] if "locator_id" in row.keys() and row["locator_id"] else _locator_id("SCN", row["id"]),
                "used_ai": row["used_ai"],
                "mode": row["mode"],
                "ai_provider": row["ai_provider"],
                "longbridge_account": row["longbridge_account"],
                "market_data_source": row["market_data_source"] if "market_data_source" in row.keys() else "longbridge",
                "payload": payload,
                "charts": charts,
            }
        ),
    }


def _row_has(row: Any, key: str) -> bool:
    try:
        return key in row.keys()
    except Exception:
        return key in row


def _scan_ai_provider_owner(row: Any) -> str:
    if _row_has(row, "ai_provider_owner") and row["ai_provider_owner"]:
        return row["ai_provider_owner"]
    if _row_has(row, "owner_id") and row["owner_id"]:
        return row["owner_id"]
    return LOCAL_OWNER_ID


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _locator_id(prefix: str, source_id: str) -> str:
    compact = "".join(ch for ch in str(source_id or "").upper() if ch.isalnum())
    return f"{prefix}-{(compact or uuid.uuid4().hex.upper())[:12]}"


def _backfill_scan_locator_ids(db: Any) -> None:
    rows = db.execute("SELECT id, locator_id FROM scan_runs WHERE locator_id IS NULL OR locator_id = ''").fetchall()
    for row in rows:
        db.execute("UPDATE scan_runs SET locator_id = ? WHERE id = ?", (_locator_id("SCN", row["id"]), row["id"]))


init_scan_db()

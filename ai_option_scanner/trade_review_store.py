"""Persistence for AI-generated trade post-mortems.

One row per closed trading run. Worker (`post_mortem_worker`) polls for
`pending` rows, runs the AI summarizer, and writes back the review.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .account_store import normalize_owner_id, utc_now
from .db import connect, ensure_column, run_db_init_once


REVIEW_STATUS_PENDING = "pending"
REVIEW_STATUS_PROCESSING = "processing"
REVIEW_STATUS_COMPLETED = "completed"
REVIEW_STATUS_FAILED = "failed"
REVIEW_STATUS_SKIPPED = "skipped"


def init_trade_review_db() -> None:
    run_db_init_once("trade_review_store", _init_trade_review_db)


def _init_trade_review_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_reviews (
                run_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                locator_id TEXT,
                lifecycle_state TEXT,
                exit_reason TEXT,
                realized_pnl REAL,
                return_pct REAL,
                holding_minutes INTEGER,
                facts_json TEXT NOT NULL,
                review_json TEXT,
                review_status TEXT NOT NULL DEFAULT 'pending',
                review_error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                ai_provider TEXT,
                ai_model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reviewed_at TEXT
            )
            """
        )
        for column, declaration in {
            "locator_id": "TEXT",
            "lifecycle_state": "TEXT",
            "exit_reason": "TEXT",
            "realized_pnl": "REAL",
            "return_pct": "REAL",
            "holding_minutes": "INTEGER",
            "review_json": "TEXT",
            "review_status": "TEXT NOT NULL DEFAULT 'pending'",
            "review_error": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "ai_provider": "TEXT",
            "ai_model": "TEXT",
            "reviewed_at": "TEXT",
        }.items():
            ensure_column(db, "trade_reviews", column, declaration)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_reviews_owner_created "
            "ON trade_reviews(owner_id, created_at DESC)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_reviews_status "
            "ON trade_reviews(review_status, created_at)"
        )


def _loads(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _row_to_review(row: Any) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "owner_id": row["owner_id"],
        "locator_id": row["locator_id"],
        "lifecycle_state": row["lifecycle_state"],
        "exit_reason": row["exit_reason"],
        "realized_pnl": row["realized_pnl"],
        "return_pct": row["return_pct"],
        "holding_minutes": row["holding_minutes"],
        "facts": _loads(row["facts_json"]) or {},
        "review": _loads(row["review_json"]),
        "review_status": row["review_status"],
        "review_error": row["review_error"],
        "attempts": int(row["attempts"] or 0),
        "ai_provider": row["ai_provider"],
        "ai_model": row["ai_model"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "reviewed_at": row["reviewed_at"],
    }


def get_trade_review(run_id: str, owner_id: str | None = None) -> dict[str, Any] | None:
    init_trade_review_db()
    params: tuple[Any, ...] = (run_id,)
    owner_clause = ""
    if owner_id is not None:
        owner_clause = "AND owner_id = ?"
        params = (run_id, normalize_owner_id(owner_id))
    with connect() as db:
        row = db.execute(
            f"SELECT * FROM trade_reviews WHERE run_id = ? {owner_clause}",
            params,
        ).fetchone()
    return _row_to_review(row) if row else None


def list_pending_review_run_ids(limit: int = 20, max_attempts: int = 3) -> list[str]:
    init_trade_review_db()
    safe_limit = max(1, min(limit, 200))
    with connect() as db:
        rows = db.execute(
            """
            SELECT run_id
            FROM trade_reviews
            WHERE review_status IN ('pending', 'failed')
              AND attempts < ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (max_attempts, safe_limit),
        ).fetchall()
    return [row["run_id"] for row in rows]


def upsert_pending_review(
    *,
    run_id: str,
    owner_id: str | None,
    locator_id: str | None,
    lifecycle_state: str | None,
    facts: dict[str, Any],
    exit_reason: str | None = None,
    realized_pnl: float | None = None,
    return_pct: float | None = None,
    holding_minutes: int | None = None,
) -> dict[str, Any]:
    """Insert a `pending` review row for a closed run if one does not exist.

    Returns the resulting row (existing or newly created).
    """
    init_trade_review_db()
    owner = normalize_owner_id(owner_id)
    existing = get_trade_review(run_id)
    if existing:
        return existing
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO trade_reviews
                (run_id, owner_id, locator_id, lifecycle_state, exit_reason,
                 realized_pnl, return_pct, holding_minutes,
                 facts_json, review_status, attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                run_id,
                owner,
                locator_id,
                lifecycle_state,
                exit_reason,
                realized_pnl,
                return_pct,
                holding_minutes,
                _dumps(facts),
                now,
                now,
            ),
        )
    return get_trade_review(run_id) or {}


def mark_review_skipped(run_id: str, reason: str) -> None:
    init_trade_review_db()
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE trade_reviews
            SET review_status = 'skipped', review_error = ?, updated_at = ?, reviewed_at = ?
            WHERE run_id = ?
            """,
            (reason[:500], now, now, run_id),
        )


def mark_review_processing(run_id: str) -> None:
    init_trade_review_db()
    with connect() as db:
        db.execute(
            """
            UPDATE trade_reviews
            SET review_status = 'processing', attempts = attempts + 1, updated_at = ?
            WHERE run_id = ?
            """,
            (utc_now(), run_id),
        )


def mark_review_completed(
    run_id: str,
    *,
    review: dict[str, Any],
    ai_provider: str | None,
    ai_model: str | None,
) -> None:
    init_trade_review_db()
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE trade_reviews
            SET review_json = ?, review_status = 'completed', review_error = NULL,
                ai_provider = ?, ai_model = ?, updated_at = ?, reviewed_at = ?
            WHERE run_id = ?
            """,
            (_dumps(review), ai_provider, ai_model, now, now, run_id),
        )


def mark_review_failed(run_id: str, error: str) -> None:
    init_trade_review_db()
    with connect() as db:
        db.execute(
            """
            UPDATE trade_reviews
            SET review_status = 'failed', review_error = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (error[:500], utc_now(), run_id),
        )


def list_unreviewed_closed_run_ids(limit: int = 20, max_age_hours: int = 168) -> list[str]:
    """Return ids of `trading_runs` that are in a terminal lifecycle state
    but do not yet have a row in `trade_reviews`.

    Bounded by `max_age_hours` so the worker does not keep retrying very
    old runs forever. Default = 7 days.
    """
    init_trade_review_db()
    safe_limit = max(1, min(limit, 200))
    safe_age = max(1, min(int(max_age_hours or 168), 24 * 90))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=safe_age)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as db:
        rows = db.execute(
            """
            SELECT r.id
            FROM trading_runs r
            LEFT JOIN trade_reviews tr ON tr.run_id = r.id
            WHERE r.lifecycle_state IN ('closed', 'reviewed')
              AND tr.run_id IS NULL
              AND r.finished_at IS NOT NULL
              AND r.finished_at >= ?
            ORDER BY r.finished_at DESC
            LIMIT ?
            """,
            (cutoff, safe_limit),
        ).fetchall()
    return [row["id"] for row in rows]


def list_recent_trade_reviews(
    owner_id: str | None = None,
    limit: int = 50,
    statuses: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    init_trade_review_db()
    safe_limit = max(1, min(limit, 200))
    clauses: list[str] = []
    params: list[Any] = []
    if owner_id is not None:
        clauses.append("owner_id = ?")
        params.append(normalize_owner_id(owner_id))
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        clauses.append(f"review_status IN ({placeholders})")
        params.extend(statuses)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(safe_limit)
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT * FROM trade_reviews
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_review(row) for row in rows]

"""Persistence for OAuth identity bindings.

A single app user can link multiple "Sign in with" identities (one Google, one
Apple).  We key on `(provider, sub)` because the provider subject is stable for
the lifetime of the account, whereas email can change (and Apple may hand us a
private-relay address).  `username` is the join back into app_users / owner_id.

    oauth_identities(provider, sub) UNIQUE  ->  username

Schema init follows the same run_db_init_once + ensure_column pattern as the
rest of the codebase so it works on both SQLite and Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import connect, ensure_column, run_db_init_once


def init_oauth_db() -> None:
    run_db_init_once("oauth_identities", _init_oauth_db)


def _init_oauth_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                sub TEXT NOT NULL,
                username TEXT NOT NULL,
                email TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(provider, sub)
            )
            """
        )
        ensure_column(db, "oauth_identities", "email", "TEXT")
        ensure_column(db, "oauth_identities", "created_at", "TEXT")
        ensure_column(db, "oauth_identities", "updated_at", "TEXT")


def find_username_by_identity(provider: str, sub: str) -> str | None:
    init_oauth_db()
    try:
        with connect() as db:
            row = db.execute(
                "SELECT username FROM oauth_identities WHERE provider = ? AND sub = ?",
                (provider, sub),
            ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return _row_get(row, "username") or None


def link_identity(provider: str, sub: str, username: str, email: str | None) -> None:
    """Bind (provider, sub) to username, upserting on the unique key.

    Reassigns the binding to `username` if the same provider subject logs in
    again — provider subjects are stable, so a conflict means the same human."""
    init_oauth_db()
    now = _utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO oauth_identities (provider, sub, username, email, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, sub) DO UPDATE SET
                username = excluded.username,
                email = excluded.email,
                updated_at = excluded.updated_at
            """,
            (provider, sub, username, email or None, now, now),
        )


def unlink_identity(provider: str, username: str) -> int:
    init_oauth_db()
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM oauth_identities WHERE provider = ? AND username = ?",
            (provider, username),
        )
    return int(getattr(cursor, "rowcount", 0) or 0)


def list_identities_for_user(username: str) -> list[dict[str, Any]]:
    init_oauth_db()
    try:
        with connect() as db:
            rows = db.execute(
                """
                SELECT provider, sub, email, created_at, updated_at
                FROM oauth_identities
                WHERE username = ?
                ORDER BY provider ASC
                """,
                (username,),
            ).fetchall()
    except Exception:
        return []
    return [
        {
            "provider": _row_get(row, "provider"),
            "email": _row_get(row, "email"),
            "created_at": _row_get(row, "created_at"),
            "updated_at": _row_get(row, "updated_at"),
        }
        for row in rows
    ]


def count_identities_for_user(username: str) -> int:
    return len(list_identities_for_user(username))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)

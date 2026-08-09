from __future__ import annotations

import os
import sqlite3
import threading
from queue import Empty, LifoQueue
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = APP_ROOT / "ai_option_scanner.sqlite3"
POSTGRES_POOL_SIZE = max(int(os.getenv("AI_OPTION_DB_POOL_SIZE") or 4), 1)
_POSTGRES_POOL_LOCK = threading.Lock()
_POSTGRES_POOL: "LifoQueue[Any] | None" = None
_POSTGRES_POOL_URL: str | None = None
_POSTGRES_POOL_CREATED = 0
_INIT_ONCE_LOCK = threading.RLock()
_INIT_ONCE_DONE: set[str] = set()


def database_url() -> str | None:
    return os.getenv("AI_OPTION_DATABASE_URL") or os.getenv("DATABASE_URL")


def database_backend() -> str:
    url = database_url()
    if url and url.startswith(("postgres://", "postgresql://")):
        return "postgres"
    return "sqlite"


def is_postgres() -> bool:
    return database_backend() == "postgres"


def run_db_init_once(key: str, initializer: Any) -> None:
    """Run schema initialization once per process."""
    if key in _INIT_ONCE_DONE:
        return
    with _INIT_ONCE_LOCK:
        if key in _INIT_ONCE_DONE:
            return
        initializer()
        _INIT_ONCE_DONE.add(key)


def connect() -> Any:
    if is_postgres():
        return _postgres_connect()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA busy_timeout=10000")
    return _SQLiteConnection(db)


def db_pool_snapshot() -> dict[str, Any]:
    if not is_postgres():
        return {
            "backend": "sqlite",
            "pool_size": 0,
            "created": 0,
            "idle": 0,
            "in_use": 0,
        }
    pool = _POSTGRES_POOL
    idle = pool.qsize() if pool is not None else 0
    created = _POSTGRES_POOL_CREATED
    return {
        "backend": "postgres",
        "pool_size": POSTGRES_POOL_SIZE,
        "created": created,
        "idle": idle,
        "in_use": max(created - idle, 0),
    }


def ensure_column(db: Any, table: str, column: str, declaration: str) -> None:
    if isinstance(db, _PostgresConnection):
        row = db.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = ? AND column_name = ?
            """,
            (table, column),
        ).fetchone()
        if row is None:
            db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {declaration}")
        return
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


class _SQLiteConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> "_SQLiteConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        return self._connection.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _PostgresConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __enter__(self) -> "_PostgresConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        except Exception:
            self._connection.close()
            _mark_postgres_connection_closed()
            return
        _release_postgres_connection(self._connection)

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        return self._connection.execute(_postgres_sql(sql), params)


def _postgres_connect() -> _PostgresConnection:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Postgres backend requires psycopg; install psycopg[binary]") from exc
    connection = _acquire_postgres_connection(psycopg, dict_row)
    return _PostgresConnection(connection)


def _acquire_postgres_connection(psycopg: Any, dict_row: Any) -> Any:
    url = database_url()
    if not url:
        raise RuntimeError("Postgres backend requested without a database URL")
    pool = _ensure_postgres_pool(psycopg, dict_row, url)
    try:
        connection = pool.get_nowait()
        if not getattr(connection, "closed", False):
            return connection
        connection.close()
        _mark_postgres_connection_closed()
    except Empty:
        pass
    with _POSTGRES_POOL_LOCK:
        global _POSTGRES_POOL_CREATED
        if _POSTGRES_POOL_CREATED < POSTGRES_POOL_SIZE:
            _POSTGRES_POOL_CREATED += 1
            return psycopg.connect(url, row_factory=dict_row)
    connection = pool.get(timeout=30)
    if getattr(connection, "closed", False):
        connection.close()
        _mark_postgres_connection_closed()
        return _acquire_postgres_connection(psycopg, dict_row)
    return connection


def _release_postgres_connection(connection: Any) -> None:
    pool = _POSTGRES_POOL
    if pool is None:
        connection.close()
        return
    try:
        pool.put_nowait(connection)
    except Exception:
        connection.close()
        _mark_postgres_connection_closed()


def _ensure_postgres_pool(psycopg: Any, dict_row: Any, url: str) -> LifoQueue[Any]:
    global _POSTGRES_POOL, _POSTGRES_POOL_URL, _POSTGRES_POOL_CREATED
    with _POSTGRES_POOL_LOCK:
        if _POSTGRES_POOL is not None and _POSTGRES_POOL_URL == url:
            return _POSTGRES_POOL
        _POSTGRES_POOL = LifoQueue(maxsize=POSTGRES_POOL_SIZE)
        _POSTGRES_POOL_URL = url
        _POSTGRES_POOL_CREATED = 0
        return _POSTGRES_POOL


def _mark_postgres_connection_closed() -> None:
    global _POSTGRES_POOL_CREATED
    with _POSTGRES_POOL_LOCK:
        _POSTGRES_POOL_CREATED = max(0, _POSTGRES_POOL_CREATED - 1)


def _postgres_sql(sql: str) -> str:
    normalized = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    return normalized.replace("?", "%s")

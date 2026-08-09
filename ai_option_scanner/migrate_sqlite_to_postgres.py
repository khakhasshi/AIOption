from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from .db import DB_PATH, connect, is_postgres


def migrate_sqlite_to_postgres(sqlite_path: str | Path = DB_PATH) -> dict[str, int]:
    if not is_postgres():
        raise RuntimeError("set AI_OPTION_DATABASE_URL to a Postgres URL before running migration")
    _init_postgres_schema()
    source_path = Path(sqlite_path)
    if not source_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    copied: dict[str, int] = {}
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        source_tables = _sqlite_tables(source)
        with connect() as target:
            target_tables = _postgres_tables(target)
            for table in sorted(source_tables & target_tables):
                source_columns = _sqlite_columns(source, table)
                target_columns = _postgres_columns(target, table)
                columns = [column for column in source_columns if column in target_columns]
                if not columns:
                    copied[table] = 0
                    continue
                conflict_columns = _postgres_conflict_columns(target, table, columns)
                if not conflict_columns:
                    copied[table] = 0
                    continue
                rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                for row in rows:
                    _upsert_row(target, table, conflict_columns, columns, row)
                copied[table] = len(rows)
            _sync_sequences(target)
    finally:
        source.close()
    return copied


def _init_postgres_schema() -> None:
    from .account_store import init_db as init_account_db
    from .ai_provider_store import init_db_if_needed as init_ai_provider_db
    from .ai_usage_store import init_ai_usage_db
    from .app_auth import init_auth_db
    from .beta_lottery import init_beta_lottery_db
    from .broker_store import init_broker_db
    from .observation_store import init_observation_db
    from .scan_store import init_scan_db
    from .trading_store import init_trading_db

    init_account_db()
    init_ai_provider_db()
    init_ai_usage_db()
    init_auth_db()
    init_beta_lottery_db()
    init_broker_db()
    init_observation_db()
    init_scan_db()
    init_trading_db()


def _sqlite_tables(source: sqlite3.Connection) -> set[str]:
    rows = source.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _postgres_tables(target: Any) -> set[str]:
    rows = target.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """
    ).fetchall()
    return {str(row["table_name"]) for row in rows}


def _sqlite_columns(source: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in source.execute(f"PRAGMA table_info({table})").fetchall()]


def _postgres_columns(target: Any, table: str) -> list[str]:
    rows = target.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ?
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return [str(row["column_name"]) for row in rows]


def _postgres_conflict_columns(target: Any, table: str, available_columns: list[str]) -> list[str]:
    rows = target.execute(
        """
        SELECT a.attname AS column_name
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
        WHERE n.nspname = 'public'
          AND t.relname = ?
          AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        (table,),
    ).fetchall()
    primary = [str(row["column_name"]) for row in rows]
    if primary and all(column in available_columns for column in primary):
        return primary
    rows = target.execute(
        """
        SELECT indexrelid::regclass::text AS index_name
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public'
          AND t.relname = ?
          AND i.indisunique
          AND i.indpred IS NULL
        ORDER BY i.indisprimary DESC, array_length(i.indkey, 1), indexrelid::regclass::text
        """,
        (table,),
    ).fetchall()
    for index_row in rows:
        column_rows = target.execute(
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
            WHERE idx.oid = ?::regclass
            ORDER BY array_position(i.indkey, a.attnum)
            """,
            (index_row["index_name"],),
        ).fetchall()
        columns = [str(row["column_name"]) for row in column_rows]
        if columns and all(column in available_columns for column in columns):
            return columns
    return []


def _upsert_row(target: Any, table: str, conflict_columns: list[str], columns: list[str], row: sqlite3.Row) -> None:
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    conflict_key = ", ".join(conflict_columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column not in conflict_columns)
    values = tuple(row[column] for column in columns)
    if updates:
        sql = (
            f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_key}) DO UPDATE SET {updates}"
        )
    else:
        sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) ON CONFLICT({conflict_key}) DO NOTHING"
    target.execute(sql, values)


def _sync_sequences(target: Any) -> None:
    rows = target.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND column_default LIKE ?
        """,
        ("nextval(%",),
    ).fetchall()
    for row in rows:
        table = row["table_name"]
        column = row["column_name"]
        target.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), COALESCE((SELECT MAX({column}) FROM {table}), 1), true)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy SQLite data into the configured Postgres database.")
    parser.add_argument("--sqlite", default=str(DB_PATH), help="Path to ai_option_scanner.sqlite3")
    args = parser.parse_args()
    copied = migrate_sqlite_to_postgres(args.sqlite)
    for table, count in copied.items():
        print(f"{table}: {count}")


if __name__ == "__main__":
    main()

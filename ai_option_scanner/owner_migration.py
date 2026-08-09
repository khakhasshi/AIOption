from __future__ import annotations

from typing import Any

from .account_store import normalize_owner_id
from .db import connect


def migrate_browser_owner_to_user(source_owner: str | None, username: str) -> dict[str, Any]:
    source = normalize_owner_id(source_owner)
    target = normalize_owner_id(username)
    if not source or source == target or source == "local":
        return {"migrated": False, "source_owner": source, "target_owner": target, "reason": "same_owner_or_local"}
    if not source.startswith("browser-"):
        return {"migrated": False, "source_owner": source, "target_owner": target, "reason": "source_is_not_browser_owner"}
    result: dict[str, Any] = {"migrated": True, "source_owner": source, "target_owner": target, "tables": {}}
    try:
        with connect() as db:
            result["tables"]["scan_runs"] = merge_owner_rows(db, "scan_runs", source, target)
            result["tables"]["trading_runs"] = merge_owner_rows(db, "trading_runs", source, target)
            result["tables"]["trading_configs"] = merge_single_owner_row(db, "trading_configs", source, target)
            result["tables"]["trading_capital_snapshots"] = merge_owner_unique_rows(db, "trading_capital_snapshots", source, target, ["snapshot_date_et"])
            result["tables"]["trading_schedule_sessions"] = merge_owner_unique_rows(db, "trading_schedule_sessions", source, target, ["trade_date_et", "profile_id"])
            result["tables"]["trading_schedule_fires"] = merge_owner_unique_rows(db, "trading_schedule_fires", source, target, ["trade_date_et", "profile_id", "slot_id"])
            result["tables"]["ai_user_providers"] = merge_owner_unique_rows(db, "ai_user_providers", source, target, ["name"])
            result["tables"]["longbridge_accounts"] = merge_longbridge_accounts_owner(db, source, target)
    except Exception as exc:  # noqa: BLE001 - login should still succeed if legacy merge fails.
        result["migrated"] = False
        result["error"] = str(exc)[:240]
    return result


def merge_owner_rows(db: Any, table: str, source: str, target: str) -> int:
    cursor = db.execute(f"UPDATE {table} SET owner_id = ? WHERE owner_id = ?", (target, source))
    return int(getattr(cursor, "rowcount", 0) or 0)


def merge_single_owner_row(db: Any, table: str, source: str, target: str) -> int:
    target_exists = db.execute(f"SELECT 1 FROM {table} WHERE owner_id = ? LIMIT 1", (target,)).fetchone()
    if target_exists:
        cursor = db.execute(f"DELETE FROM {table} WHERE owner_id = ?", (source,))
        return int(getattr(cursor, "rowcount", 0) or 0)
    return merge_owner_rows(db, table, source, target)


def merge_owner_unique_rows(db: Any, table: str, source: str, target: str, unique_keys: list[str]) -> int:
    match = " AND ".join([f"target.{key} = {table}.{key}" for key in unique_keys])
    cursor = db.execute(
        f"""
        UPDATE {table}
        SET owner_id = ?
        WHERE owner_id = ?
          AND NOT EXISTS (
            SELECT 1 FROM {table} AS target
            WHERE target.owner_id = ? AND {match}
          )
        """,
        (target, source, target),
    )
    changed = int(getattr(cursor, "rowcount", 0) or 0)
    db.execute(f"DELETE FROM {table} WHERE owner_id = ?", (source,))
    return changed


def merge_longbridge_accounts_owner(db: Any, source: str, target: str) -> int:
    target_default = db.execute("SELECT 1 FROM longbridge_accounts WHERE owner_id = ? AND is_default = 1 LIMIT 1", (target,)).fetchone()
    if target_default:
        db.execute("UPDATE longbridge_accounts SET is_default = 0 WHERE owner_id = ?", (source,))
    return merge_owner_rows(db, "longbridge_accounts", source, target)

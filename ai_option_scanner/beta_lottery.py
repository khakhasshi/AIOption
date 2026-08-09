from __future__ import annotations

import ipaddress
import json
import os
import secrets
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .app_auth import (
    DEFAULT_RESOURCE_LIMITS,
    RESOURCE_LIMIT_FIELDS,
    expires_at_from_remaining_days,
    hash_password,
    init_auth_db,
    invalidate_auth_user_cache,
    validate_username,
)
from .db import connect, ensure_column, run_db_init_once


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
LOTTERY_NAME = os.getenv("AI_OPTION_BETA_LOTTERY_NAME", "public-beta-20260511").strip() or "public-beta-20260511"
SLOT_COUNT = max(1, int(os.getenv("AI_OPTION_BETA_LOTTERY_SLOT_COUNT") or "15"))
USER_VALID_DAYS = max(0.25, float(os.getenv("AI_OPTION_BETA_LOTTERY_USER_DAYS") or "7"))
USER_PREFIX = os.getenv("AI_OPTION_BETA_LOTTERY_USER_PREFIX", "beta").strip() or "beta"
ANNOUNCE_AT = datetime.fromisoformat(os.getenv("AI_OPTION_BETA_LOTTERY_ANNOUNCE_AT_BJ", "2026-05-11T19:30:00+08:00"))
REGISTRATION_START_AT = datetime.fromisoformat(os.getenv("AI_OPTION_BETA_LOTTERY_START_AT_BJ", "2026-05-04T19:30:00+08:00"))

# Admin-controlled lifecycle states:
#   draft     — registration closed, results not drawn (initial/paused state)
#   open      — registration open, users may enter
#   drawn     — winners drawn but not yet published to entrants
#   announced — results published; winners + credentials visible to entrants
LOTTERY_STATUS_VALUES = ("draft", "open", "drawn", "announced")
LOTTERY_ADMIN_ACTIONS = ("open", "close", "draw", "publish", "reset", "set_announce_at", "set_config")

def public_status(entry_token: str | None = None) -> dict[str, Any]:
    init_beta_lottery_db()
    state = _get_state()
    if state["status"] == "announced":
        finalize_draw()
    entry = _entry_by_token(entry_token)
    winners = _public_winners() if _announced() else []
    config = _get_config()
    return {
        "lottery_name": LOTTERY_NAME,
        "slot_count": config["slot_count"],
        "user_valid_days": config["user_valid_days"],
        "entry_count": _entry_count(),
        "winner_count": len(winners),
        "announce_at_bj": state["announce_at"],
        "registration_start_at_bj": state["registration_start_at"],
        "now_bj": _now_bj().isoformat(),
        "status": state["status"],
        "registration_open": state["status"] == "open",
        "announced": _announced(),
        "closed": _announced(),
        "entry": _public_entry(entry) if entry else None,
        "winners": winners,
    }


def enter_lottery(nickname: str, contact: str, request_meta: dict[str, Any], entry_token: str | None = None) -> dict[str, Any]:
    init_beta_lottery_db()
    status = _get_state()["status"]
    if status == "announced":
        finalize_draw()
        return {**public_status(entry_token), "accepted": False, "message": "抽签已截止，结果已公布。"}
    if status != "open":
        return {**public_status(entry_token), "accepted": False, "message": "报名尚未开放，请关注群通知后再来登记。"}
    existing = _entry_by_token(entry_token)
    if existing:
        return {**public_status(entry_token), "accepted": False, "message": "你已经完成登记，请等待开奖。"}

    fingerprint = _clean_text(str(request_meta.get("fingerprint") or ""), 128)
    if fingerprint:
        dup = _entry_by_fingerprint(fingerprint)
        if dup:
            # Same browser/device already registered under a different token —
            # block to stop one person from entering repeatedly (incognito,
            # cleared storage, multiple browser profiles) to lift their odds.
            return {
                **public_status(dup.get("entry_token")),
                "accepted": False,
                "message": "检测到该设备 / 浏览器已参与本次抽签，每台设备仅可登记一次。",
            }

    token = secrets.token_urlsafe(24)
    now = _utc_now()
    ip = _client_ip(request_meta)
    geo = _geo_for_ip(ip)
    cleaned_nickname = _clean_text(nickname, 64)
    cleaned_contact = _clean_text(contact, 128)
    if not cleaned_nickname:
        raise ValueError("请输入群昵称或称呼")
    with connect() as db:
        db.execute(
            """
            INSERT INTO beta_lottery_entries
                (lottery_name, entry_token, nickname, contact, ip_address, ip_location, ip_geo_json, user_agent, route_mode, fingerprint, selected, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                LOTTERY_NAME,
                token,
                cleaned_nickname,
                cleaned_contact,
                ip,
                geo.get("label") or "未知",
                json.dumps(geo, ensure_ascii=False),
                _clean_text(str(request_meta.get("user_agent") or ""), 512),
                _clean_text(str(request_meta.get("route_mode") or ""), 32),
                fingerprint,
                now,
                now,
            ),
        )
    return {**public_status(token), "accepted": True, "message": "登记成功，请保存当前浏览器或截图，开奖后回到本页面查看结果。"}


def admin_rows() -> dict[str, Any]:
    init_beta_lottery_db()
    if _announced():
        _finalize_draw_once()
    return _admin_rows_raw()


def finalize_draw() -> dict[str, Any]:
    init_beta_lottery_db()
    _finalize_draw_once()
    return _admin_rows_raw()


def admin_action(
    action: str,
    announce_at: str | None = None,
    registration_start_at: str | None = None,
    slot_count: Any = None,
    user_valid_days: Any = None,
    limits: Any = None,
) -> dict[str, Any]:
    """Drive the lottery lifecycle from the admin console."""
    init_beta_lottery_db()
    cleaned = (action or "").strip().lower()
    if cleaned == "open":
        _set_state(status="open")
    elif cleaned == "close":
        _set_state(status="draft")
    elif cleaned == "draw":
        _finalize_draw_once()
        _set_state(status="drawn")
    elif cleaned == "publish":
        _finalize_draw_once()
        _set_state(status="announced")
    elif cleaned == "reset":
        _reset_draw()
        _set_state(status="draft")
    elif cleaned == "set_announce_at":
        if not announce_at:
            raise ValueError("announce_at is required")
        _set_state(announce_at=_parse_schedule_dt(announce_at, "announce_at"))
    elif cleaned == "set_config":
        # Schedule times are optional on a config save; only update those provided.
        if announce_at:
            _set_state(announce_at=_parse_schedule_dt(announce_at, "announce_at"))
        if registration_start_at:
            _set_state(registration_start_at=_parse_schedule_dt(registration_start_at, "registration_start_at"))
        _set_config(slot_count=slot_count, user_valid_days=user_valid_days, limits=limits)
    else:
        raise ValueError(f"unknown lottery action: {action}")
    return _admin_rows_raw()


def _parse_schedule_dt(value: str, field: str) -> str:
    """Parse an ISO datetime; bare (no tz) values are treated as Beijing time."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.isoformat()


def _reset_draw() -> None:
    now = _utc_now()
    with connect() as db:
        db.execute(
            "UPDATE beta_lottery_slots SET assigned_entry_id = NULL, assigned_at = NULL WHERE lottery_name = ?",
            (LOTTERY_NAME,),
        )
        db.execute(
            "UPDATE beta_lottery_entries SET selected = 0, assigned_username = NULL, updated_at = ? WHERE lottery_name = ?",
            (now, LOTTERY_NAME),
        )


def _finalize_draw_once() -> None:
    with connect() as db:
        assigned = db.execute(
            "SELECT COUNT(*) AS count FROM beta_lottery_slots WHERE lottery_name = ? AND assigned_entry_id IS NOT NULL",
            (LOTTERY_NAME,),
        ).fetchone()
        if int(_row_get(assigned, "count") or 0) > 0:
            return

        entries = db.execute(
            "SELECT id FROM beta_lottery_entries WHERE lottery_name = ? ORDER BY created_at ASC, id ASC",
            (LOTTERY_NAME,),
        ).fetchall()
        slots = db.execute(
            "SELECT id, slot_number, username, password_plain FROM beta_lottery_slots WHERE lottery_name = ? ORDER BY slot_number ASC",
            (LOTTERY_NAME,),
        ).fetchall()
        entry_ids = [int(_row_get(row, "id")) for row in entries]
        secrets.SystemRandom().shuffle(entry_ids)
        winners = entry_ids[: min(len(entry_ids), len(slots))]
        winner_set = set(winners)
        now = _utc_now()
        db.execute("UPDATE beta_lottery_entries SET selected = 0, updated_at = ? WHERE lottery_name = ?", (now, LOTTERY_NAME))
        users_to_activate: list[tuple[str, str]] = []
        for row, entry_id in zip(slots, winners):
            username = str(_row_get(row, "username") or "")
            password = str(_row_get(row, "password_plain") or "")
            users_to_activate.append((username, password))
            db.execute(
                "UPDATE beta_lottery_slots SET assigned_entry_id = ?, assigned_at = ? WHERE id = ?",
                (entry_id, now, _row_get(row, "id")),
            )
            db.execute(
                "UPDATE beta_lottery_entries SET selected = 1, assigned_username = ?, updated_at = ? WHERE id = ?",
                (username, now, entry_id),
            )
        for entry_id in entry_ids:
            if entry_id not in winner_set:
                db.execute(
                    "UPDATE beta_lottery_entries SET selected = 0, assigned_username = NULL, updated_at = ? WHERE id = ?",
                    (now, entry_id),
                )
    _upsert_lottery_auth_users(users_to_activate)


def _admin_rows_raw() -> dict[str, Any]:
    with connect() as db:
        entries = db.execute(
            """
            SELECT id, entry_token, nickname, contact, ip_address, ip_location, ip_geo_json, user_agent,
                   route_mode, fingerprint, selected, assigned_username, created_at, updated_at
            FROM beta_lottery_entries
            WHERE lottery_name = ?
            ORDER BY created_at DESC, id DESC
            """,
            (LOTTERY_NAME,),
        ).fetchall()
        slots = db.execute(
            """
            SELECT slot_number, username, password_plain, assigned_entry_id, assigned_at, created_at
            FROM beta_lottery_slots
            WHERE lottery_name = ?
            ORDER BY slot_number ASC
            """,
            (LOTTERY_NAME,),
        ).fetchall()
    rows = [_entry_admin_dict(row) for row in entries]
    state = _get_state()
    config = _get_config()
    return {
        "lottery_name": LOTTERY_NAME,
        "slot_count": config["slot_count"],
        "user_valid_days": config["user_valid_days"],
        "config": config,
        "entry_count": len(rows),
        "winner_count": sum(1 for row in rows if row.get("selected")),
        "announce_at_bj": state["announce_at"],
        "registration_start_at_bj": state["registration_start_at"],
        "now_bj": _now_bj().isoformat(),
        "status": state["status"],
        "registration_open": state["status"] == "open",
        "announced": _announced(),
        "entries": rows,
        "slots": [_slot_admin_dict(row) for row in slots],
    }


def init_beta_lottery_db() -> None:
    run_db_init_once("beta_lottery", _init_beta_lottery_db)


def _init_beta_lottery_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_lottery_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lottery_name TEXT NOT NULL,
                slot_number INTEGER NOT NULL,
                username TEXT NOT NULL UNIQUE,
                password_plain TEXT NOT NULL,
                assigned_entry_id INTEGER,
                assigned_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_lottery_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lottery_name TEXT NOT NULL,
                entry_token TEXT NOT NULL UNIQUE,
                nickname TEXT NOT NULL DEFAULT '',
                contact TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                ip_location TEXT NOT NULL DEFAULT '',
                ip_geo_json TEXT NOT NULL DEFAULT '{}',
                user_agent TEXT NOT NULL DEFAULT '',
                route_mode TEXT NOT NULL DEFAULT '',
                selected INTEGER NOT NULL DEFAULT 0,
                assigned_username TEXT,
                fingerprint TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_lottery_ip_geo (
                ip_address TEXT PRIMARY KEY,
                geo_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS beta_lottery_state (
                lottery_name TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'open',
                announce_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(db, "beta_lottery_slots", "assigned_entry_id", "INTEGER")
        ensure_column(db, "beta_lottery_slots", "assigned_at", "TEXT")
        ensure_column(db, "beta_lottery_entries", "ip_location", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "beta_lottery_entries", "ip_geo_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(db, "beta_lottery_entries", "route_mode", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "beta_lottery_entries", "assigned_username", "TEXT")
        ensure_column(db, "beta_lottery_entries", "fingerprint", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "beta_lottery_state", "slot_count", "INTEGER")
        ensure_column(db, "beta_lottery_state", "user_valid_days", "REAL")
        ensure_column(db, "beta_lottery_state", "limits_json", "TEXT")
        ensure_column(db, "beta_lottery_state", "registration_start_at", "TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_beta_lottery_slots_name_slot ON beta_lottery_slots(lottery_name, slot_number)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_beta_lottery_entries_name_created ON beta_lottery_entries(lottery_name, created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_beta_lottery_entries_name_fingerprint ON beta_lottery_entries(lottery_name, fingerprint)")
    _seed_slots()


def _seed_slots() -> None:
    now = _utc_now()
    seeded_users: list[tuple[str, str]] = []
    slot_count = _get_config()["slot_count"]
    with connect() as db:
        for number in range(1, slot_count + 1):
            exists = db.execute(
                "SELECT username, password_plain FROM beta_lottery_slots WHERE lottery_name = ? AND slot_number = ?",
                (LOTTERY_NAME, number),
            ).fetchone()
            if exists:
                username = str(_row_get(exists, "username") or "")
                password = str(_row_get(exists, "password_plain") or "")
            else:
                username = f"{USER_PREFIX}{number:02d}@ai-option.test"
                password = f"AIOpt-{number:02d}-{secrets.token_urlsafe(8)}"
                db.execute(
                    """
                    INSERT INTO beta_lottery_slots (lottery_name, slot_number, username, password_plain, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (LOTTERY_NAME, number, username, password, now),
                )
            seeded_users.append((username, password))
    _upsert_lottery_auth_users(seeded_users)


def _prune_slots(slot_count: int) -> None:
    """Remove unassigned slots (and their test accounts) beyond the configured count."""
    with connect() as db:
        extra = db.execute(
            "SELECT username FROM beta_lottery_slots WHERE lottery_name = ? AND slot_number > ? AND assigned_entry_id IS NULL",
            (LOTTERY_NAME, slot_count),
        ).fetchall()
        usernames = [str(_row_get(row, "username") or "") for row in extra if _row_get(row, "username")]
        db.execute(
            "DELETE FROM beta_lottery_slots WHERE lottery_name = ? AND slot_number > ? AND assigned_entry_id IS NULL",
            (LOTTERY_NAME, slot_count),
        )
        for username in usernames:
            db.execute("DELETE FROM app_users WHERE username = ?", (username,))
    if usernames:
        invalidate_auth_user_cache()


def _upsert_lottery_auth_users(users: list[tuple[str, str]]) -> None:
    if not users:
        return
    init_auth_db()
    config = _get_config()
    limits = config["limits"]
    expires_at = expires_at_from_remaining_days(config["user_valid_days"])
    now = _utc_now()
    limit_cols = list(RESOURCE_LIMIT_FIELDS)
    limit_col_sql = ", ".join(limit_cols)
    limit_placeholders = ", ".join(["?"] * len(limit_cols))
    limit_update_sql = ", ".join(f"{col} = excluded.{col}" for col in limit_cols)
    with connect() as db:
        for username, password in users:
            limit_values = [int(limits.get(col, DEFAULT_RESOURCE_LIMITS[col])) for col in limit_cols]
            db.execute(
                f"""
                INSERT INTO app_users (username, password_hash, can_trade, is_admin, expires_at, {limit_col_sql}, created_at, updated_at)
                VALUES (?, ?, 0, 0, ?, {limit_placeholders}, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    can_trade = 0,
                    is_admin = 0,
                    expires_at = excluded.expires_at,
                    {limit_update_sql},
                    updated_at = excluded.updated_at
                """,
                (validate_username(username), hash_password(password), expires_at, *limit_values, now, now),
            )
    invalidate_auth_user_cache()


def _public_winners() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT nickname, contact, assigned_username, created_at
            FROM beta_lottery_entries
            WHERE lottery_name = ? AND selected = 1
            ORDER BY updated_at ASC, id ASC
            """,
            (LOTTERY_NAME,),
        ).fetchall()
    return [
        {
            "display_name": _mask_display(_row_get(row, "nickname") or _row_get(row, "contact") or "群友"),
            "assigned_username": _row_get(row, "assigned_username"),
            "created_at": _row_get(row, "created_at"),
        }
        for row in rows
    ]


def _entry_by_token(entry_token: str | None) -> dict[str, Any] | None:
    token = str(entry_token or "").strip()
    if not token:
        return None
    with connect() as db:
        row = db.execute(
            """
            SELECT e.*, s.password_plain
            FROM beta_lottery_entries e
            LEFT JOIN beta_lottery_slots s ON s.lottery_name = e.lottery_name AND s.assigned_entry_id = e.id
            WHERE e.lottery_name = ? AND e.entry_token = ?
            """,
            (LOTTERY_NAME, token),
        ).fetchone()
    return _entry_private_dict(row) if row else None


def _entry_count() -> int:
    with connect() as db:
        row = db.execute("SELECT COUNT(*) AS count FROM beta_lottery_entries WHERE lottery_name = ?", (LOTTERY_NAME,)).fetchone()
    return int(_row_get(row, "count") or 0)


def _entry_by_fingerprint(fingerprint: str) -> dict[str, Any] | None:
    value = str(fingerprint or "").strip()
    if not value:
        return None
    with connect() as db:
        row = db.execute(
            """
            SELECT e.*, s.password_plain
            FROM beta_lottery_entries e
            LEFT JOIN beta_lottery_slots s ON s.lottery_name = e.lottery_name AND s.assigned_entry_id = e.id
            WHERE e.lottery_name = ? AND e.fingerprint = ?
            ORDER BY e.created_at ASC, e.id ASC
            LIMIT 1
            """,
            (LOTTERY_NAME, value),
        ).fetchone()
    return _entry_private_dict(row) if row else None


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    output = {
        "entry_token": entry.get("entry_token"),
        "nickname": entry.get("nickname"),
        "contact": entry.get("contact"),
        "created_at": entry.get("created_at"),
        "selected": bool(entry.get("selected")),
        "assigned_username": entry.get("assigned_username") if entry.get("selected") else "",
        "password": entry.get("password_plain") if entry.get("selected") and _announced() else "",
    }
    return output


def _entry_private_dict(row: Any) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "id": _row_get(row, "id"),
        "entry_token": _row_get(row, "entry_token"),
        "nickname": _row_get(row, "nickname"),
        "contact": _row_get(row, "contact"),
        "ip_address": _row_get(row, "ip_address"),
        "ip_location": _row_get(row, "ip_location"),
        "ip_geo": _loads(_row_get(row, "ip_geo_json")),
        "user_agent": _row_get(row, "user_agent"),
        "route_mode": _row_get(row, "route_mode"),
        "fingerprint": _row_get(row, "fingerprint"),
        "selected": bool(_row_get(row, "selected")),
        "assigned_username": _row_get(row, "assigned_username"),
        "password_plain": _row_get(row, "password_plain"),
        "created_at": _row_get(row, "created_at"),
        "updated_at": _row_get(row, "updated_at"),
    }


def _entry_admin_dict(row: Any) -> dict[str, Any]:
    data = _entry_private_dict(row)
    data.pop("password_plain", None)
    return data


def _slot_admin_dict(row: Any) -> dict[str, Any]:
    return {
        "slot_number": _row_get(row, "slot_number"),
        "username": _row_get(row, "username"),
        "password": _row_get(row, "password_plain"),
        "assigned_entry_id": _row_get(row, "assigned_entry_id"),
        "assigned_at": _row_get(row, "assigned_at"),
        "created_at": _row_get(row, "created_at"),
    }


def _geo_for_ip(ip: str) -> dict[str, Any]:
    if not ip or ip == "unknown":
        return {"label": "未知", "kind": "unknown"}
    try:
        parsed = ipaddress.ip_address(ip)
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
            return {"label": "内网/本机", "kind": "private"}
    except ValueError:
        return {"label": "未知", "kind": "invalid"}
    cached = _geo_cache_get(ip)
    if cached:
        return cached
    if os.getenv("AI_OPTION_IP_GEO_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        geo = {"label": "未启用IP定位", "kind": "disabled"}
    else:
        geo = _fetch_ip_geo(ip)
    _geo_cache_set(ip, geo)
    return geo


def _fetch_ip_geo(ip: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"https://ipapi.co/{ip}/json/", timeout=2.5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except Exception as exc:  # noqa: BLE001
        return {"label": "定位失败", "kind": "lookup_failed", "error": str(exc)[:160]}
    city = str(payload.get("city") or "")
    region = str(payload.get("region") or payload.get("region_code") or "")
    country = str(payload.get("country_name") or payload.get("country") or "")
    org = str(payload.get("org") or "")
    label = " · ".join(item for item in (country, region, city) if item) or "未知"
    return {
        "label": label,
        "kind": "public",
        "country": country,
        "region": region,
        "city": city,
        "org": org,
        "timezone": str(payload.get("timezone") or ""),
    }


def _geo_cache_get(ip: str) -> dict[str, Any] | None:
    try:
        with connect() as db:
            row = db.execute("SELECT geo_json FROM beta_lottery_ip_geo WHERE ip_address = ?", (ip,)).fetchone()
    except Exception:
        return None
    return _loads(_row_get(row, "geo_json")) if row else None


def _geo_cache_set(ip: str, geo: dict[str, Any]) -> None:
    try:
        with connect() as db:
            db.execute(
                """
                INSERT INTO beta_lottery_ip_geo (ip_address, geo_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(ip_address) DO UPDATE SET geo_json = excluded.geo_json, updated_at = excluded.updated_at
                """,
                (ip, json.dumps(geo, ensure_ascii=False), _utc_now()),
            )
    except Exception:
        pass


def _client_ip(meta: dict[str, Any]) -> str:
    headers = meta.get("headers") or {}
    for key in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        value = str(headers.get(key) or headers.get(key.title()) or "").strip()
        if not value:
            continue
        first = value.split(",", 1)[0].strip()
        if first:
            return first
    return str(meta.get("client_host") or "unknown").strip() or "unknown"


def _announced() -> bool:
    return _get_state()["status"] == "announced"


def _get_state() -> dict[str, Any]:
    with connect() as db:
        row = db.execute(
            "SELECT status, announce_at, registration_start_at FROM beta_lottery_state WHERE lottery_name = ?",
            (LOTTERY_NAME,),
        ).fetchone()
    if row is None:
        default_status = "announced" if _now_bj() >= ANNOUNCE_AT.astimezone(BEIJING_TZ) else "open"
        _set_state(status=default_status, announce_at=ANNOUNCE_AT.isoformat(), registration_start_at=REGISTRATION_START_AT.isoformat())
        return {
            "status": default_status,
            "announce_at": ANNOUNCE_AT.isoformat(),
            "registration_start_at": REGISTRATION_START_AT.isoformat(),
        }
    status = str(_row_get(row, "status") or "open")
    if status not in LOTTERY_STATUS_VALUES:
        status = "open"
    announce_at = str(_row_get(row, "announce_at") or ANNOUNCE_AT.isoformat())
    registration_start_at = str(_row_get(row, "registration_start_at") or REGISTRATION_START_AT.isoformat())
    return {"status": status, "announce_at": announce_at, "registration_start_at": registration_start_at}


def _set_state(status: str | None = None, announce_at: str | None = None, registration_start_at: str | None = None) -> None:
    now = _utc_now()
    with connect() as db:
        existing = db.execute(
            "SELECT status, announce_at, registration_start_at FROM beta_lottery_state WHERE lottery_name = ?",
            (LOTTERY_NAME,),
        ).fetchone()
        if existing is None:
            db.execute(
                "INSERT INTO beta_lottery_state (lottery_name, status, announce_at, registration_start_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    LOTTERY_NAME,
                    status or "open",
                    announce_at or ANNOUNCE_AT.isoformat(),
                    registration_start_at or REGISTRATION_START_AT.isoformat(),
                    now,
                ),
            )
            return
        new_status = status or str(_row_get(existing, "status") or "open")
        new_announce = announce_at if announce_at is not None else (_row_get(existing, "announce_at") or ANNOUNCE_AT.isoformat())
        new_start = (
            registration_start_at
            if registration_start_at is not None
            else (_row_get(existing, "registration_start_at") or REGISTRATION_START_AT.isoformat())
        )
        db.execute(
            "UPDATE beta_lottery_state SET status = ?, announce_at = ?, registration_start_at = ?, updated_at = ? WHERE lottery_name = ?",
            (new_status, new_announce, new_start, now, LOTTERY_NAME),
        )


def _default_user_limits() -> dict[str, int]:
    return {field: int(DEFAULT_RESOURCE_LIMITS[field]) for field in RESOURCE_LIMIT_FIELDS}


def _get_config() -> dict[str, Any]:
    _get_state()  # ensure a state row exists
    slot_count = SLOT_COUNT
    valid_days = USER_VALID_DAYS
    limits = _default_user_limits()
    with connect() as db:
        row = db.execute(
            "SELECT slot_count, user_valid_days, limits_json FROM beta_lottery_state WHERE lottery_name = ?",
            (LOTTERY_NAME,),
        ).fetchone()
    if row is not None:
        raw_slots = _row_get(row, "slot_count")
        if raw_slots is not None:
            try:
                slot_count = max(1, int(raw_slots))
            except (TypeError, ValueError):
                slot_count = SLOT_COUNT
        raw_days = _row_get(row, "user_valid_days")
        if raw_days is not None:
            try:
                valid_days = max(0.25, float(raw_days))
            except (TypeError, ValueError):
                valid_days = USER_VALID_DAYS
        raw_limits = _row_get(row, "limits_json")
        if raw_limits:
            for key, value in _loads(raw_limits).items():
                if key in RESOURCE_LIMIT_FIELDS:
                    try:
                        limits[key] = int(value)
                    except (TypeError, ValueError):
                        continue
    return {"slot_count": slot_count, "user_valid_days": valid_days, "limits": limits}


def _set_config(slot_count: Any = None, user_valid_days: Any = None, limits: Any = None) -> None:
    current = _get_config()
    new_slot_count = current["slot_count"]
    new_valid_days = current["user_valid_days"]
    new_limits = dict(current["limits"])
    if slot_count is not None:
        try:
            new_slot_count = max(1, min(500, int(slot_count)))
        except (TypeError, ValueError) as exc:
            raise ValueError("slot_count must be an integer") from exc
    if user_valid_days is not None:
        try:
            new_valid_days = max(0.25, min(3650.0, float(user_valid_days)))
        except (TypeError, ValueError) as exc:
            raise ValueError("user_valid_days must be a number") from exc
    if isinstance(limits, dict):
        for key, value in limits.items():
            if key in RESOURCE_LIMIT_FIELDS:
                try:
                    new_limits[key] = max(-1, int(value))
                except (TypeError, ValueError):
                    continue
    now = _utc_now()
    with connect() as db:
        db.execute(
            "UPDATE beta_lottery_state SET slot_count = ?, user_valid_days = ?, limits_json = ?, updated_at = ? WHERE lottery_name = ?",
            (new_slot_count, new_valid_days, json.dumps(new_limits, ensure_ascii=False), now, LOTTERY_NAME),
        )
    _prune_slots(new_slot_count)
    _seed_slots()


def _now_bj() -> datetime:
    return datetime.now(BEIJING_TZ)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:limit]


def _mask_display(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "群友"
    if "@" in text:
        name, domain = text.split("@", 1)
        return f"{name[:2]}***@{domain}"
    if len(text) <= 2:
        return f"{text[0]}*"
    return f"{text[:2]}***{text[-1]}"


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import connect, ensure_column, run_db_init_once
from .ttl_cache import TTLCache


COOKIE_NAME = "ai_option_session"
SESSION_SECONDS = 60 * 60 * 12
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{3,96}$")
HASH_ALGORITHM = "pbkdf2_sha256"
HASH_ITERATIONS = 310_000
DEFAULT_AUTH_USER_DAYS = 7.0
AUTH_USER_CACHE_TTL_SECONDS = float(os.getenv("AI_OPTION_AUTH_USER_CACHE_TTL_SECONDS") or 5)
_FAILURES: dict[str, list[float]] = {}
_auth_users_cache: TTLCache[dict[str, "AuthUser"]] = TTLCache(AUTH_USER_CACHE_TTL_SECONDS, maxsize=1, namespace="auth_users")
RESOURCE_LIMIT_FIELDS = (
    "max_daily_scans",
    "max_daily_ai_scans",
    "max_daily_ai_chat",
    "max_watchlists",
    "max_scan_loop_instances",
    "max_notification_channels",
    "max_longbridge_accounts",
)
DEFAULT_RESOURCE_LIMITS: dict[str, int] = {
    "max_daily_scans": 50,
    "max_daily_ai_scans": 20,
    "max_daily_ai_chat": 30,
    "max_watchlists": 20,
    "max_scan_loop_instances": 20,
    "max_notification_channels": 10,
    "max_longbridge_accounts": 2,
}
UNLIMITED_RESOURCE_LIMITS: dict[str, int] = {key: -1 for key in RESOURCE_LIMIT_FIELDS}

# --- OAuth ("Sign in with Google/Apple") provisioning ---------------------
# Users who arrive via OAuth and have no account yet get a self-serve trial.
# The trial only unlocks the two features we currently sell: the analyzer scan
# (5/day, all AI-eligible) and the AI chat (10/day).  Every other resource is
# pinned to 0, which the quota gate treats as "disabled" (used >= 0 is always
# true).  Live trading stays off until an admin grants it.
OAUTH_TRIAL_DAYS = 15.0
OAUTH_TRIAL_LIMITS: dict[str, int] = {
    "max_daily_scans": 5,
    "max_daily_ai_scans": 5,
    "max_daily_ai_chat": 10,
    "max_watchlists": 0,
    "max_scan_loop_instances": 0,
    "max_notification_channels": 0,
    "max_longbridge_accounts": 0,
}
# A password hash that can never match any real password: verify_password only
# accepts the pbkdf2_sha256 algorithm, so this sentinel cleanly disables password
# login for OAuth-only accounts while still satisfying the NOT NULL column.
OAUTH_UNUSABLE_PASSWORD = "oauth-no-password:disabled"


@dataclass(frozen=True)
class AuthUser:
    username: str
    password_hash: str
    can_analyze: bool = True
    can_trade: bool = True
    is_admin: bool = False
    source: str = "db"
    expires_at: str | None = None
    max_daily_scans: int = DEFAULT_RESOURCE_LIMITS["max_daily_scans"]
    max_daily_ai_scans: int = DEFAULT_RESOURCE_LIMITS["max_daily_ai_scans"]
    max_daily_ai_chat: int = DEFAULT_RESOURCE_LIMITS["max_daily_ai_chat"]
    max_watchlists: int = DEFAULT_RESOURCE_LIMITS["max_watchlists"]
    max_scan_loop_instances: int = DEFAULT_RESOURCE_LIMITS["max_scan_loop_instances"]
    max_notification_channels: int = DEFAULT_RESOURCE_LIMITS["max_notification_channels"]
    max_longbridge_accounts: int = DEFAULT_RESOURCE_LIMITS["max_longbridge_accounts"]


def auth_enabled() -> bool:
    return bool(load_auth_users())


def load_auth_users() -> dict[str, AuthUser]:
    return _auth_users_cache.get_or_set("auth_users", _load_auth_users_uncached)


def invalidate_auth_user_cache() -> None:
    _auth_users_cache.clear()


def _load_auth_users_uncached() -> dict[str, AuthUser]:
    users: dict[str, AuthUser] = _load_db_auth_users()
    for index in range(1, 6):
        username = _clean_username(os.getenv(f"AI_OPTION_AUTH_USER_{index}"))
        password_hash = str(os.getenv(f"AI_OPTION_AUTH_PASSWORD_HASH_{index}") or "").strip()
        password = str(os.getenv(f"AI_OPTION_AUTH_PASSWORD_{index}") or "")
        can_analyze = _env_bool(f"AI_OPTION_AUTH_CAN_ANALYZE_{index}", True)
        can_trade = _env_bool(f"AI_OPTION_AUTH_CAN_TRADE_{index}", True)
        is_admin = _env_bool(f"AI_OPTION_AUTH_IS_ADMIN_{index}", index == 1)
        expires_at = _clean_optional(os.getenv(f"AI_OPTION_AUTH_EXPIRES_AT_{index}"))
        limits = _env_resource_limits(index)
        if not username:
            continue
        if password_hash:
            users[username] = AuthUser(
                username=username,
                password_hash=password_hash,
                can_analyze=can_analyze,
                can_trade=can_trade,
                is_admin=is_admin,
                source="env",
                expires_at=expires_at,
                **limits,
            )
        elif password:
            users[username] = AuthUser(
                username=username,
                password_hash=hash_password(password),
                can_analyze=can_analyze,
                can_trade=can_trade,
                is_admin=is_admin,
                source="env",
                expires_at=expires_at,
                **limits,
            )
    return users


def get_auth_user(username: str | None) -> AuthUser | None:
    return load_auth_users().get(_clean_username(username))


def auth_user_permissions(username: str | None) -> dict[str, Any]:
    if not auth_enabled():
        return {"can_analyze": True, "can_trade": True, "is_admin": True, "expired": False, "limits": dict(UNLIMITED_RESOURCE_LIMITS)}
    user = get_auth_user(username)
    active = bool(user and not auth_user_expired(user))
    return {
        "can_analyze": bool(active and user and user.can_analyze),
        "can_trade": bool(active and user and user.can_trade),
        "is_admin": bool(active and user and user.is_admin),
        "expired": bool(user and auth_user_expired(user)),
        "limits": auth_user_limits(username),
    }


def auth_user_limits(username: str | None) -> dict[str, int]:
    if not auth_enabled():
        return dict(UNLIMITED_RESOURCE_LIMITS)
    user = get_auth_user(username)
    if user is None:
        return dict(DEFAULT_RESOURCE_LIMITS)
    return {field: _normalize_limit_value(getattr(user, field, DEFAULT_RESOURCE_LIMITS[field]), field) for field in RESOURCE_LIMIT_FIELDS}


def auth_users_as_rows() -> list[dict[str, Any]]:
    rows = []
    for user in sorted(load_auth_users().values(), key=lambda item: item.username):
        remaining_seconds = auth_user_remaining_seconds(user)
        expired = auth_user_expired(user)
        limits = auth_user_limits(user.username)
        rows.append(
            {
                "username": user.username,
                "can_analyze": bool(user.can_analyze and not expired),
                "can_trade": user.can_trade,
                "is_admin": user.is_admin,
                "source": user.source,
                "editable": user.source == "db",
                "expires_at": user.expires_at,
                "remaining_seconds": remaining_seconds,
                "remaining_days": None if remaining_seconds is None else round(remaining_seconds / 86400, 2),
                "expired": expired,
                **limits,
                "limits": limits,
                "usage": auth_user_resource_usage(user.username),
            }
        )
    return rows


def auth_user_resource_usage(username: str | None) -> dict[str, int]:
    owner = _owner_id_for_username(username)
    if not owner:
        return {}
    # Daily counters reset at ET (America/New_York) midnight to match the
    # trading calendar, NOT UTC midnight.  Otherwise a user who uses up their
    # quota at 8p ET on a Tuesday would see it reset at 8p instead of midnight
    # because UTC had already rolled over.
    from .time_utils import EASTERN
    day_start = datetime.now(EASTERN).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    usage = {
        "daily_scans": 0,
        "daily_ai_scans": 0,
        "daily_ai_chat": 0,
        "watchlists": 0,
        "scan_loop_instances": 0,
        "notification_channels": 0,
        "longbridge_accounts": 0,
    }
    try:
        with connect() as db:
            usage["daily_scans"] = _safe_count(db, "SELECT COUNT(*) AS count FROM scan_runs WHERE owner_id = ? AND created_at >= ?", (owner, day_start))
            usage["daily_ai_scans"] = _safe_count(db, "SELECT COUNT(*) AS count FROM scan_runs WHERE owner_id = ? AND created_at >= ? AND use_ai = 1", (owner, day_start))
            usage["daily_ai_chat"] = _safe_count(db, "SELECT COUNT(*) AS count FROM chat_messages WHERE owner_id = ? AND role = 'user' AND created_at >= ?", (owner, day_start))
            usage["watchlists"] = _safe_count(db, "SELECT COUNT(*) AS count FROM watchlists WHERE owner_id = ?", (owner,))
            usage["scan_loop_instances"] = _safe_count(db, "SELECT COUNT(*) AS count FROM scan_loop_instances WHERE owner_id = ?", (owner,))
            usage["notification_channels"] = _safe_count(db, "SELECT COUNT(*) AS count FROM notification_channels WHERE owner_id = ?", (owner,))
            usage["longbridge_accounts"] = _safe_count(db, "SELECT COUNT(*) AS count FROM longbridge_accounts WHERE owner_id = ?", (owner,))
    except Exception:
        return usage
    return usage


def create_auth_user(
    username: str,
    password: str,
    can_analyze: bool = True,
    can_trade: bool = False,
    is_admin: bool = False,
    remaining_days: float = DEFAULT_AUTH_USER_DAYS,
    resource_limits: dict[str, Any] | None = None,
) -> AuthUser:
    username = validate_username(username)
    password = str(password or "")
    if len(password) < 8 or len(password) > 256:
        raise ValueError("password must be 8-256 characters")
    expires_at = expires_at_from_remaining_days(remaining_days)
    limits = _normalize_resource_limits(resource_limits)
    now = _utc_now()
    init_auth_db()
    with connect() as db:
        db.execute(
            """
            INSERT INTO app_users (
                username, password_hash, can_analyze, can_trade, is_admin, expires_at,
                max_daily_scans, max_daily_ai_scans, max_daily_ai_chat, max_watchlists, max_scan_loop_instances,
                max_notification_channels, max_longbridge_accounts, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password_hash = excluded.password_hash,
                can_analyze = excluded.can_analyze,
                can_trade = excluded.can_trade,
                is_admin = excluded.is_admin,
                expires_at = excluded.expires_at,
                max_daily_scans = excluded.max_daily_scans,
                max_daily_ai_scans = excluded.max_daily_ai_scans,
                max_daily_ai_chat = excluded.max_daily_ai_chat,
                max_watchlists = excluded.max_watchlists,
                max_scan_loop_instances = excluded.max_scan_loop_instances,
                max_notification_channels = excluded.max_notification_channels,
                max_longbridge_accounts = excluded.max_longbridge_accounts,
                updated_at = excluded.updated_at
            """,
            (
                username,
                hash_password(password),
                1 if can_analyze else 0,
                1 if can_trade else 0,
                1 if is_admin else 0,
                expires_at,
                limits["max_daily_scans"],
                limits["max_daily_ai_scans"],
                limits["max_daily_ai_chat"],
                limits["max_watchlists"],
                limits["max_scan_loop_instances"],
                limits["max_notification_channels"],
                limits["max_longbridge_accounts"],
                now,
                now,
            ),
        )
    invalidate_auth_user_cache()
    user = get_auth_user(username)
    if user is None:
        raise ValueError("failed to create user")
    return user


def update_auth_user_permissions(
    username: str,
    can_analyze: bool | None = None,
    can_trade: bool | None = None,
    is_admin: bool | None = None,
    remaining_days: float | None = None,
    resource_limits: dict[str, Any] | None = None,
) -> AuthUser:
    username = validate_username(username)
    init_auth_db()
    user = get_auth_user(username)
    if user is None:
        raise ValueError("user does not exist")
    if user.source != "db":
        raise ValueError("environment configured users cannot be edited from the UI")
    if is_admin is False and user.is_admin and _admin_count() <= 1:
        raise ValueError("cannot demote the last admin user")
    normalized_limits = _normalize_resource_limits(resource_limits, partial=True)
    if can_analyze is None and can_trade is None and is_admin is None and remaining_days is None and not normalized_limits:
        raise ValueError("nothing to update")
    assignments = ["updated_at = ?"]
    params: list[Any] = [_utc_now()]
    if can_analyze is not None:
        assignments.append("can_analyze = ?")
        params.append(1 if can_analyze else 0)
    if can_trade is not None:
        assignments.append("can_trade = ?")
        params.append(1 if can_trade else 0)
    if is_admin is not None:
        assignments.append("is_admin = ?")
        params.append(1 if is_admin else 0)
    if remaining_days is not None:
        assignments.append("expires_at = ?")
        params.append(expires_at_from_remaining_days(remaining_days))
    for key, value in normalized_limits.items():
        assignments.append(f"{key} = ?")
        params.append(value)
    params.append(username)
    with connect() as db:
        db.execute(
            f"UPDATE app_users SET {', '.join(assignments)} WHERE username = ?",
            params,
        )
    invalidate_auth_user_cache()
    updated = get_auth_user(username)
    if updated is None:
        raise ValueError("user does not exist")
    return updated


def delete_auth_user(username: str) -> None:
    username = validate_username(username)
    user = get_auth_user(username)
    if user is None:
        raise ValueError("user does not exist")
    if user.source != "db":
        raise ValueError("environment configured users cannot be deleted from the UI")
    if user.is_admin and _admin_count() <= 1:
        raise ValueError("cannot delete the last admin user")
    init_auth_db()
    with connect() as db:
        db.execute("DELETE FROM app_users WHERE username = ?", (username,))
    invalidate_auth_user_cache()


def user_has_password(username: str | None) -> bool:
    """True if the account can log in with a password (not an OAuth-only stub).

    Used to guard OAuth unlinking: an account whose only login method is a single
    OAuth identity must not be allowed to unlink it, or the user locks themselves
    out."""
    user = get_auth_user(username)
    if user is None:
        return False
    return str(user.password_hash or "").startswith(f"{HASH_ALGORITHM}:")


def provision_oauth_user(email: str) -> AuthUser:
    """Create (or return existing) a trial account for a verified OAuth email.

    The username IS the verified email (USERNAME_RE already permits @.+-).  New
    accounts get OAUTH_TRIAL_LIMITS and a 15-day expiry; an unusable password
    hash keeps password login disabled.  Idempotent: a concurrent first login
    races safely on the UNIQUE(username) constraint and both callers end up with
    the same row."""
    username = validate_username(email)
    init_auth_db()
    existing = get_auth_user(username)
    if existing is not None:
        return existing
    now = _utc_now()
    expires_at = expires_at_from_remaining_days(OAUTH_TRIAL_DAYS)
    with connect() as db:
        db.execute(
            """
            INSERT INTO app_users (
                username, password_hash, can_analyze, can_trade, is_admin, expires_at,
                max_daily_scans, max_daily_ai_scans, max_daily_ai_chat, max_watchlists, max_scan_loop_instances,
                max_notification_channels, max_longbridge_accounts, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO NOTHING
            """,
            (
                username,
                OAUTH_UNUSABLE_PASSWORD,
                1,  # can_analyze
                0,  # can_trade — live trading stays admin-gated
                0,  # is_admin
                expires_at,
                OAUTH_TRIAL_LIMITS["max_daily_scans"],
                OAUTH_TRIAL_LIMITS["max_daily_ai_scans"],
                OAUTH_TRIAL_LIMITS["max_daily_ai_chat"],
                OAUTH_TRIAL_LIMITS["max_watchlists"],
                OAUTH_TRIAL_LIMITS["max_scan_loop_instances"],
                OAUTH_TRIAL_LIMITS["max_notification_channels"],
                OAUTH_TRIAL_LIMITS["max_longbridge_accounts"],
                now,
                now,
            ),
        )
    invalidate_auth_user_cache()
    user = get_auth_user(username)
    if user is None:
        raise ValueError("failed to provision oauth user")
    return user


def init_auth_db() -> None:
    run_db_init_once("app_auth", _init_auth_db)


def _init_auth_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                can_analyze INTEGER NOT NULL DEFAULT 1,
                can_trade INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                max_daily_scans INTEGER NOT NULL DEFAULT 50,
                max_daily_ai_scans INTEGER NOT NULL DEFAULT 20,
                max_daily_ai_chat INTEGER NOT NULL DEFAULT 30,
                max_watchlists INTEGER NOT NULL DEFAULT 20,
                max_scan_loop_instances INTEGER NOT NULL DEFAULT 20,
                max_notification_channels INTEGER NOT NULL DEFAULT 10,
                max_longbridge_accounts INTEGER NOT NULL DEFAULT 2,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(db, "app_users", "can_analyze", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "app_users", "can_trade", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "app_users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "app_users", "expires_at", "TEXT")
        for key in RESOURCE_LIMIT_FIELDS:
            ensure_column(db, "app_users", key, f"INTEGER NOT NULL DEFAULT {DEFAULT_RESOURCE_LIMITS[key]}")
        ensure_column(db, "app_users", "created_at", "TEXT")
        ensure_column(db, "app_users", "updated_at", "TEXT")
        now = _utc_now()
        db.execute(
            "UPDATE app_users SET expires_at = ?, updated_at = COALESCE(updated_at, ?) WHERE expires_at IS NULL OR expires_at = ''",
            (expires_at_from_remaining_days(DEFAULT_AUTH_USER_DAYS), now),
        )


def _load_db_auth_users() -> dict[str, AuthUser]:
    try:
        init_auth_db()
        with connect() as db:
            rows = db.execute(
                """
                SELECT username, password_hash, can_analyze, can_trade, is_admin, expires_at,
                       max_daily_scans, max_daily_ai_scans, max_daily_ai_chat, max_watchlists, max_scan_loop_instances,
                       max_notification_channels, max_longbridge_accounts
                FROM app_users
                ORDER BY username ASC
                """
            ).fetchall()
    except Exception:
        return {}
    users: dict[str, AuthUser] = {}
    for row in rows:
        username = _clean_username(_row_get(row, "username"))
        if not username:
            continue
        users[username] = AuthUser(
            username=username,
            password_hash=str(_row_get(row, "password_hash") or ""),
            can_analyze=bool(_row_get(row, "can_analyze")),
            can_trade=bool(_row_get(row, "can_trade")),
            is_admin=bool(_row_get(row, "is_admin")),
            source="db",
            expires_at=_clean_optional(_row_get(row, "expires_at")),
            **{
                key: _normalize_limit_value(_row_get(row, key), key)
                for key in RESOURCE_LIMIT_FIELDS
            },
        )
    return users


def _admin_count() -> int:
    return sum(1 for user in load_auth_users().values() if user.is_admin and not auth_user_expired(user))


def validate_username(username: str | None) -> str:
    cleaned = _clean_username(username)
    if not cleaned or not USERNAME_RE.fullmatch(cleaned):
        raise ValueError("invalid username")
    return cleaned


def login_allowed(ip: str, username: str) -> bool:
    now = time.time()
    key = f"{ip}:{username}"
    recent = [stamp for stamp in _FAILURES.get(key, []) if now - stamp < 300]
    _FAILURES[key] = recent
    return len(recent) < 8


def record_login_failure(ip: str, username: str) -> None:
    key = f"{ip}:{username}"
    _FAILURES.setdefault(key, []).append(time.time())


def clear_login_failures(ip: str, username: str) -> None:
    _FAILURES.pop(f"{ip}:{username}", None)


def verify_login(username: str, password: str) -> bool:
    users = load_auth_users()
    user = users.get(username)
    if user is None or auth_user_expired(user):
        _constant_time_dummy_verify(password)
        return False
    return verify_password(password, user.password_hash)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    return ":".join(
        [
            HASH_ALGORITHM,
            str(HASH_ITERATIONS),
            _b64url(salt),
            _b64url(digest),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        separator = ":" if ":" in encoded else "$"
        algorithm, iterations_text, salt_text, digest_text = encoded.split(separator, 3)
        if algorithm != HASH_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = _b64url_decode(salt_text)
        expected = _b64url_decode(digest_text)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_session_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": int(time.time()) + SESSION_SECONDS,
        "nonce": secrets.token_urlsafe(12),
    }
    payload_text = _b64url(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _sign(payload_text)
    return f"{payload_text}.{signature}"


def verify_session_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload_text, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload_text), signature):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_text).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    username = _clean_username(payload.get("sub"))
    user = get_auth_user(username)
    if not username or user is None or auth_user_expired(user):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return username


def auth_user_expired(user: AuthUser | None) -> bool:
    expires_at = _parse_utc_datetime(user.expires_at if user else None)
    return bool(expires_at and expires_at <= datetime.now(timezone.utc))


def auth_user_remaining_seconds(user: AuthUser | None) -> int | None:
    expires_at = _parse_utc_datetime(user.expires_at if user else None)
    if expires_at is None:
        return None
    return max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))


def expires_at_from_remaining_days(days: float | int | str | None) -> str:
    try:
        value = float(days if days is not None else DEFAULT_AUTH_USER_DAYS)
    except (TypeError, ValueError) as exc:
        raise ValueError("remaining days must be a number") from exc
    if value < 0:
        raise ValueError("remaining days cannot be negative")
    if value > 3650:
        raise ValueError("remaining days cannot exceed 3650")
    return (datetime.now(timezone.utc) + timedelta(days=value)).isoformat()


def cookie_options(is_https: bool) -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": is_https,
        "samesite": "lax",
        "max_age": SESSION_SECONDS,
        "path": "/",
    }


def _sign(payload_text: str) -> str:
    secret = _auth_secret()
    digest = hmac.new(secret.encode("utf-8"), payload_text.encode("utf-8"), hashlib.sha256).digest()
    return _b64url(digest)


def _auth_secret() -> str:
    secret = str(os.getenv("AI_OPTION_AUTH_SECRET") or "").strip()
    if len(secret) < 32:
        fallback = str(os.getenv("DEEPSEEK_API_KEY") or "ai-option-dev-secret")
        secret = hashlib.sha256(fallback.encode("utf-8")).hexdigest()
    return secret


def _constant_time_dummy_verify(password: str) -> None:
    dummy = hash_password("not-the-password", b"0" * 16)
    verify_password(password, dummy)


def _clean_username(username: str | None) -> str:
    return str(username or "").strip().lower()


def _owner_id_for_username(username: str | None) -> str:
    value = _clean_username(username)
    safe = re.sub(r"[^a-z0-9_.-]+", "-", value).strip(".-_")
    return safe[:80]


def _clean_optional(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_resource_limits(index: int) -> dict[str, int]:
    limits: dict[str, int] = {}
    for field in RESOURCE_LIMIT_FIELDS:
        env_name = f"AI_OPTION_AUTH_{field.upper()}_{index}"
        limits[field] = _normalize_limit_value(os.getenv(env_name), field)
    return limits


def _normalize_resource_limits(values: dict[str, Any] | None, *, partial: bool = False) -> dict[str, int]:
    source = values or {}
    limits: dict[str, int] = {}
    for field in RESOURCE_LIMIT_FIELDS:
        if field not in source:
            if partial:
                continue
            limits[field] = DEFAULT_RESOURCE_LIMITS[field]
            continue
        limits[field] = _normalize_limit_value(source.get(field), field)
    return limits


def _normalize_limit_value(value: Any, field: str) -> int:
    if value is None or value == "":
        return DEFAULT_RESOURCE_LIMITS[field]
    try:
        parsed = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if parsed < -1:
        raise ValueError(f"{field} cannot be less than -1")
    if parsed > 1_000_000:
        raise ValueError(f"{field} cannot exceed 1000000")
    return parsed


def _safe_count(db: Any, sql: str, params: tuple[Any, ...]) -> int:
    try:
        row = db.execute(sql, params).fetchone()
    except Exception:
        return 0
    if row is None:
        return 0
    return int(_row_get(row, "count") or 0)


def _parse_utc_datetime(value: str | None) -> datetime | None:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)

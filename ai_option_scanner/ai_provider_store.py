from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .account_store import _decrypt_secret, _encrypt_secret, normalize_owner_id, utc_now
from .ai_providers import AIProvider, normalize_provider_type
from .db import connect, ensure_column, is_postgres, run_db_init_once


USER_PROVIDER_PREFIX = "user:"


@dataclass(frozen=True)
class UserAIProvider:
    id: int
    owner_id: str
    name: str
    label: str
    base_url: str
    model: str
    temperature: float
    provider_type: str
    api_key_suffix: str | None
    is_default: bool
    created_at: str
    updated_at: str


def init_db() -> None:
    run_db_init_once("ai_provider_store", _init_db)


def _init_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_user_providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                label TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model TEXT NOT NULL,
                temperature REAL NOT NULL DEFAULT 0.25,
                provider_type TEXT NOT NULL DEFAULT 'openai',
                api_key_enc TEXT NOT NULL,
                api_key_suffix TEXT,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(db, "ai_user_providers", "label", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "ai_user_providers", "provider_type", "TEXT NOT NULL DEFAULT 'openai'")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_user_providers_owner_name "
            "ON ai_user_providers(owner_id, name)"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_user_providers_owner_default "
            "ON ai_user_providers(owner_id, is_default) WHERE is_default = 1"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_user_providers_owner ON ai_user_providers(owner_id)")


def list_user_providers(owner_id: str | None) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    init_db_if_needed()
    with connect() as db:
        rows = db.execute(
            """
            SELECT id, owner_id, name, label, base_url, model, temperature,
                   provider_type, api_key_suffix, is_default, created_at, updated_at
            FROM ai_user_providers
            WHERE owner_id = ?
            ORDER BY is_default DESC, name ASC
            """,
            (owner,),
        ).fetchall()
    return [_row_to_public_dict(row) for row in rows]


def upsert_user_provider(
    owner_id: str | None,
    name: str,
    label: str | None,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float = 0.25,
    provider_type: str = "openai",
    is_default: bool = False,
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    normalized = normalize_user_provider_name(name)
    safe_base_url = normalize_base_url(base_url)
    safe_model = normalize_model(model)
    safe_key = validate_api_key(api_key)
    temp = max(0.0, min(float(temperature), 2.0))
    safe_provider_type = normalize_provider_type(provider_type)
    now = utc_now()
    init_db_if_needed()
    with connect() as db:
        if is_default:
            db.execute("UPDATE ai_user_providers SET is_default = 0 WHERE owner_id = ?", (owner,))
        db.execute(
            """
            INSERT INTO ai_user_providers
                (owner_id, name, label, base_url, model, temperature, provider_type,
                 api_key_enc, api_key_suffix, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, name) DO UPDATE SET
                label = excluded.label,
                base_url = excluded.base_url,
                model = excluded.model,
                temperature = excluded.temperature,
                provider_type = excluded.provider_type,
                api_key_enc = excluded.api_key_enc,
                api_key_suffix = excluded.api_key_suffix,
                is_default = excluded.is_default,
                updated_at = excluded.updated_at
            """,
            (
                owner,
                normalized,
                label or normalized,
                safe_base_url,
                safe_model,
                temp,
                safe_provider_type,
                _encrypt_secret(safe_key),
                safe_key[-6:],
                1 if is_default else 0,
                now,
                now,
            ),
        )
    return list_user_providers(owner)


def delete_user_provider(owner_id: str | None, name: str) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    normalized = normalize_user_provider_name(name)
    init_db_if_needed()
    with connect() as db:
        db.execute("DELETE FROM ai_user_providers WHERE owner_id = ? AND name = ?", (owner, normalized))
    return list_user_providers(owner)


def get_user_provider(owner_id: str | None, provider_name: str | None) -> AIProvider | None:
    if not provider_name:
        return None
    name = provider_name
    if name.startswith(USER_PROVIDER_PREFIX):
        name = name[len(USER_PROVIDER_PREFIX):]
    else:
        return None
    owner = normalize_owner_id(owner_id)
    normalized = normalize_user_provider_name(name)
    init_db_if_needed()
    with connect() as db:
        row = db.execute(
            """
            SELECT owner_id, name, base_url, model, temperature, provider_type, api_key_enc
            FROM ai_user_providers
            WHERE owner_id = ? AND name = ?
            """,
            (owner, normalized),
        ).fetchone()
    if row is None:
        return None
    return AIProvider(
        name=f"{USER_PROVIDER_PREFIX}{row['name']}",
        base_url=str(row["base_url"]),
        model=str(row["model"]),
        api_key_env="",
        temperature=float(row["temperature"]),
        api_key=_decrypt_secret(str(row["api_key_enc"])),
        provider_type=normalize_provider_type(row["provider_type"]),
    )


def preferred_user_provider_name(owner_id: str | None) -> str | None:
    owner = normalize_owner_id(owner_id)
    init_db_if_needed()
    with connect() as db:
        row = db.execute(
            """
            SELECT name FROM ai_user_providers
            WHERE owner_id = ? AND is_default = 1
            LIMIT 1
            """,
            (owner,),
        ).fetchone()
    return f"{USER_PROVIDER_PREFIX}{row['name']}" if row else None


def user_providers_as_rows(owner_id: str | None) -> list[dict[str, Any]]:
    return list_user_providers(owner_id)


def normalize_user_provider_name(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if normalized.startswith(USER_PROVIDER_PREFIX):
        normalized = normalized[len(USER_PROVIDER_PREFIX):]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,31}", normalized):
        raise ValueError("AI provider name must match [a-z0-9][a-z0-9_.-]{0,31}")
    return normalized


def normalize_base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text.startswith(("https://", "http://")) or len(text) > 512:
        raise ValueError("base_url must be a valid http(s) URL")
    if any(char in text for char in ("\x00", "\r", "\n")):
        raise ValueError("base_url contains invalid control characters")
    return text


def normalize_model(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128 or any(char in text for char in ("\x00", "\r", "\n")):
        raise ValueError("model is required and must be shorter than 128 characters")
    return text


def validate_api_key(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 4096:
        raise ValueError("api_key is required and must be shorter than 4096 characters")
    if any(char in text for char in ("\x00", "\r", "\n")):
        raise ValueError("api_key contains invalid control characters")
    return text


def _row_to_public_dict(row: Any) -> dict[str, Any]:
    return {
        "name": f"{USER_PROVIDER_PREFIX}{row['name']}",
        "raw_name": str(row["name"]),
        "label": str(row["label"] or row["name"]),
        "base_url": str(row["base_url"]),
        "model": str(row["model"]),
        "temperature": float(row["temperature"]),
        "provider_type": normalize_provider_type(row["provider_type"]),
        "api_key_suffix": str(row["api_key_suffix"]) if row["api_key_suffix"] else None,
        "is_default": bool(row["is_default"]),
        "server_managed": False,
        "configured": True,
    }


def init_db_if_needed() -> None:
    if is_postgres():
        init_db()
        return
    init_db()


init_db()

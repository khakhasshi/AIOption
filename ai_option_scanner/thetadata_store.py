from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

from .account_store import _decrypt_secret, _encrypt_secret, utc_now
from .db import connect, run_db_init_once


PROVIDER_KEY = "thetadata"


@dataclass(frozen=True)
class ThetaDataCredential:
    source: str
    revision: str
    email: str | None = None
    password: str | None = None
    credentials_file: str | None = None


def init_db() -> None:
    run_db_init_once("thetadata_store", _init_db)


def _init_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS data_source_credentials (
                provider TEXT PRIMARY KEY,
                identity_enc TEXT NOT NULL,
                secret_enc TEXT NOT NULL,
                identity_hint TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def save_thetadata_credentials(email: str, password: str) -> dict[str, Any]:
    safe_email = _validate_email(email)
    safe_password = _validate_password(password)
    now = utc_now()
    init_db()
    with connect() as db:
        db.execute(
            """
            INSERT INTO data_source_credentials
                (provider, identity_enc, secret_enc, identity_hint, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                identity_enc = excluded.identity_enc,
                secret_enc = excluded.secret_enc,
                identity_hint = excluded.identity_hint,
                updated_at = excluded.updated_at
            """,
            (
                PROVIDER_KEY,
                _encrypt_secret(safe_email),
                _encrypt_secret(safe_password),
                _mask_email(safe_email),
                now,
                now,
            ),
        )
    return thetadata_config_status()


def delete_thetadata_credentials() -> dict[str, Any]:
    init_db()
    with connect() as db:
        db.execute("DELETE FROM data_source_credentials WHERE provider = ?", (PROVIDER_KEY,))
    return thetadata_config_status()


def resolve_thetadata_credentials() -> ThetaDataCredential:
    email = str(os.getenv("THETADATA_EMAIL") or os.getenv("AI_OPTION_THETADATA_EMAIL") or "").strip()
    password = str(os.getenv("THETADATA_PASSWORD") or os.getenv("AI_OPTION_THETADATA_PASSWORD") or "")
    credentials_file = str(
        os.getenv("THETADATA_CREDENTIALS_FILE")
        or os.getenv("AI_OPTION_THETADATA_CREDENTIALS_FILE")
        or ""
    ).strip()

    if email and password:
        return ThetaDataCredential(
            source="environment",
            revision=_revision("environment", email, password),
            email=email,
            password=password,
        )
    if credentials_file:
        return ThetaDataCredential(
            source="credentials_file",
            revision=_revision("credentials_file", credentials_file),
            credentials_file=credentials_file,
        )

    row = _stored_row()
    if row is not None:
        stored_email = _decrypt_secret(str(row["identity_enc"]))
        stored_password = _decrypt_secret(str(row["secret_enc"]))
        return ThetaDataCredential(
            source="saved",
            revision=f"saved:{row['updated_at']}",
            email=stored_email,
            password=stored_password,
        )

    return ThetaDataCredential(source="sdk_default", revision="sdk_default")


def thetadata_config_status() -> dict[str, Any]:
    env_email = str(os.getenv("THETADATA_EMAIL") or os.getenv("AI_OPTION_THETADATA_EMAIL") or "").strip()
    env_password = str(os.getenv("THETADATA_PASSWORD") or os.getenv("AI_OPTION_THETADATA_PASSWORD") or "")
    credentials_file = str(
        os.getenv("THETADATA_CREDENTIALS_FILE")
        or os.getenv("AI_OPTION_THETADATA_CREDENTIALS_FILE")
        or ""
    ).strip()
    row = _stored_row()
    stored_configured = row is not None
    warning: str | None = None

    if bool(env_email) != bool(env_password):
        warning = "ThetaData environment credentials are incomplete and were ignored."

    try:
        active = resolve_thetadata_credentials()
    except ValueError:
        active = ThetaDataCredential(source="saved_unreadable", revision="saved_unreadable")
        warning = "Saved ThetaData credentials cannot be decrypted with the current credential secret."

    configured = active.source in {"environment", "credentials_file", "saved"}
    if active.email:
        email_hint = _mask_email(active.email)
    elif row is not None:
        email_hint = str(row["identity_hint"] or "") or None
    else:
        email_hint = None

    updated_at = str(row["updated_at"]) if row is not None else None
    return {
        "configured": configured,
        "source": active.source,
        "email_hint": email_hint,
        "stored_configured": stored_configured,
        "updated_at": updated_at,
        "environment_override": active.source in {"environment", "credentials_file"},
        "credentials_file_configured": active.source == "credentials_file",
        "warning": warning,
    }


def _stored_row() -> Any | None:
    init_db()
    with connect() as db:
        return db.execute(
            """
            SELECT identity_enc, secret_enc, identity_hint, created_at, updated_at
            FROM data_source_credentials
            WHERE provider = ?
            """,
            (PROVIDER_KEY,),
        ).fetchone()


def _validate_email(value: str) -> str:
    email = str(value or "").strip()
    if len(email) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("ThetaData email must be a valid email address")
    if any(char in email for char in ("\x00", "\r", "\n")):
        raise ValueError("ThetaData email contains invalid control characters")
    return email


def _validate_password(value: str) -> str:
    password = str(value or "")
    if not password or len(password) > 4096:
        raise ValueError("ThetaData password is required and must be shorter than 4096 characters")
    if any(char in password for char in ("\x00", "\r", "\n")):
        raise ValueError("ThetaData password contains invalid control characters")
    return password


def _mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not domain:
        return "***"
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def _revision(*parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8", errors="ignore")).hexdigest()
    return digest[:20]


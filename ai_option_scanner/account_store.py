from __future__ import annotations

import os
import re
import json
import base64
import hmac
import hashlib
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import DB_PATH, connect, ensure_column, is_postgres

APP_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = APP_ROOT / ".longbridge_accounts"
DEFAULT_ACCOUNT_NAME = "default"
LOCAL_OWNER_ID = "local"


@dataclass(frozen=True)
class LongbridgeAccount:
    id: int
    name: str
    owner_id: str
    label: str
    home_dir: str
    is_default: bool
    created_at: str
    updated_at: str
    last_used_at: str | None
    identity_fingerprint: str | None
    session_status: str | None
    region: str | None
    identity_meta: dict[str, Any]
    identity_updated_at: str | None
    sdk_credentials_configured: bool
    sdk_app_key_suffix: str | None
    sdk_credentials_updated_at: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with _connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS longbridge_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                home_dir TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )
        _ensure_column(db, "longbridge_accounts", "identity_fingerprint", "TEXT")
        _ensure_column(db, "longbridge_accounts", "session_status", "TEXT")
        _ensure_column(db, "longbridge_accounts", "region", "TEXT")
        _ensure_column(db, "longbridge_accounts", "identity_meta_json", "TEXT")
        _ensure_column(db, "longbridge_accounts", "identity_updated_at", "TEXT")
        _ensure_column(db, "longbridge_accounts", "owner_id", f"TEXT NOT NULL DEFAULT '{LOCAL_OWNER_ID}'")
        _ensure_column(db, "longbridge_accounts", "sdk_app_key_enc", "TEXT")
        _ensure_column(db, "longbridge_accounts", "sdk_app_secret_enc", "TEXT")
        _ensure_column(db, "longbridge_accounts", "sdk_access_token_enc", "TEXT")
        _ensure_column(db, "longbridge_accounts", "sdk_app_key_suffix", "TEXT")
        _ensure_column(db, "longbridge_accounts", "sdk_credentials_updated_at", "TEXT")
        db.execute("DROP INDEX IF EXISTS idx_longbridge_accounts_one_default")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_longbridge_accounts_owner_default "
            "ON longbridge_accounts(owner_id, is_default) WHERE is_default = 1"
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_longbridge_accounts_owner ON longbridge_accounts(owner_id)")
    ensure_default_account()


def ensure_default_account(owner_id: str = LOCAL_OWNER_ID) -> LongbridgeAccount | None:
    owner_id = normalize_owner_id(owner_id)
    if owner_id != LOCAL_OWNER_ID:
        return None
    account = get_account(DEFAULT_ACCOUNT_NAME, create_if_missing=False, owner_id=owner_id)
    if account is not None:
        return account
    now = utc_now()
    with _connect() as db:
        db.execute("UPDATE longbridge_accounts SET is_default = 0 WHERE owner_id = ?", (owner_id,))
        db.execute(
            """
            INSERT INTO longbridge_accounts
                (name, owner_id, label, home_dir, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (DEFAULT_ACCOUNT_NAME, owner_id, "Default CLI Session", str(Path.home()), now, now),
        )
    return get_account(DEFAULT_ACCOUNT_NAME, create_if_missing=False, owner_id=owner_id)


def list_accounts(owner_id: str | None = None) -> list[LongbridgeAccount]:
    init_db_if_needed()
    owner_clause = ""
    params: tuple[Any, ...] = ()
    if owner_id is not None:
        owner_clause = "WHERE owner_id = ?"
        params = (normalize_owner_id(owner_id),)
    with _connect() as db:
        rows = db.execute(
            f"""
            SELECT id, name, owner_id, label, home_dir, is_default, created_at, updated_at, last_used_at,
                   identity_fingerprint, session_status, region, identity_meta_json, identity_updated_at
                   , sdk_app_key_suffix, sdk_credentials_updated_at
            FROM longbridge_accounts
            {owner_clause}
            ORDER BY is_default DESC, name ASC
            """,
            params,
        ).fetchall()
    return [_row_to_account(row) for row in rows]


def preferred_sdk_account(owner_id: str | None = None) -> LongbridgeAccount | None:
    accounts = [account for account in list_accounts(owner_id) if account.sdk_credentials_configured]
    if not accounts:
        return None
    return next((account for account in accounts if account.is_default), accounts[0])


def accounts_as_rows(owner_id: str | None = None) -> list[dict[str, Any]]:
    return [asdict(account) for account in list_accounts(owner_id)]


def get_account(
    name: str | None,
    create_if_missing: bool = True,
    owner_id: str | None = None,
) -> LongbridgeAccount | None:
    if create_if_missing:
        init_db_if_needed()
    if not name:
        return get_default_account(owner_id or LOCAL_OWNER_ID)
    normalized = normalize_name(name)
    params: tuple[Any, ...]
    owner_clause = ""
    if owner_id is not None:
        owner_clause = "AND owner_id = ?"
        params = (normalized, normalize_owner_id(owner_id))
    else:
        params = (normalized,)
    with _connect() as db:
        row = db.execute(
            f"""
            SELECT id, name, owner_id, label, home_dir, is_default, created_at, updated_at, last_used_at,
                   identity_fingerprint, session_status, region, identity_meta_json, identity_updated_at
                   , sdk_app_key_suffix, sdk_credentials_updated_at
            FROM longbridge_accounts
            WHERE name = ? {owner_clause}
            """,
            params,
        ).fetchone()
    return _row_to_account(row) if row else None


def get_default_account(owner_id: str = LOCAL_OWNER_ID) -> LongbridgeAccount | None:
    init_db_if_needed()
    owner_id = normalize_owner_id(owner_id)
    with _connect() as db:
        row = db.execute(
            """
            SELECT id, name, owner_id, label, home_dir, is_default, created_at, updated_at, last_used_at,
                   identity_fingerprint, session_status, region, identity_meta_json, identity_updated_at
                   , sdk_app_key_suffix, sdk_credentials_updated_at
            FROM longbridge_accounts
            WHERE owner_id = ? AND is_default = 1
            LIMIT 1
            """,
            (owner_id,),
        ).fetchone()
    if row:
        return _row_to_account(row)
    return ensure_default_account(owner_id)


def resolve_account(name: str | None = None, owner_id: str | None = None) -> LongbridgeAccount:
    account = get_account(name, owner_id=owner_id)
    if account is None:
        raise ValueError(f"Longbridge account profile `{name}` does not exist")
    Path(account.home_dir).mkdir(parents=True, exist_ok=True)
    return account


def create_account(
    name: str,
    label: str | None = None,
    home_dir: str | None = None,
    set_default: bool = False,
    owner_id: str = LOCAL_OWNER_ID,
    app_key: str | None = None,
    app_secret: str | None = None,
    access_token: str | None = None,
) -> LongbridgeAccount:
    init_db_if_needed()
    owner_id = normalize_owner_id(owner_id)
    requested_name = normalize_name(name)
    normalized = _stored_name_for_owner(requested_name, owner_id)
    if owner_id == LOCAL_OWNER_ID and normalized == DEFAULT_ACCOUNT_NAME:
        raise ValueError("`default` is reserved for the machine's built-in Longbridge account slot")
    has_credentials = any((app_key, app_secret, access_token))
    if has_credentials and not all((app_key, app_secret, access_token)):
        raise ValueError("app_key, app_secret and access_token are required together")

    now = utc_now()
    profile_home = Path(home_dir).expanduser() if home_dir else PROFILE_ROOT / normalized / "home"
    profile_home.mkdir(parents=True, exist_ok=True)
    with _connect() as db:
        if set_default:
            db.execute("UPDATE longbridge_accounts SET is_default = 0 WHERE owner_id = ?", (owner_id,))
        db.execute(
            """
            INSERT INTO longbridge_accounts
                (name, owner_id, label, home_dir, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                owner_id = excluded.owner_id,
                label = excluded.label,
                home_dir = excluded.home_dir,
                is_default = CASE
                    WHEN excluded.is_default = 1 THEN 1
                    ELSE longbridge_accounts.is_default
                END,
                updated_at = excluded.updated_at
            """,
            (
                normalized,
                owner_id,
                label or requested_name,
                str(profile_home),
                1 if set_default else 0,
                now,
                now,
            ),
        )
        if has_credentials:
            _store_sdk_credentials(db, normalized, str(app_key), str(app_secret), str(access_token), now)
    return resolve_account(normalized, owner_id=owner_id)


def set_default_account(name: str, owner_id: str = LOCAL_OWNER_ID) -> LongbridgeAccount:
    owner_id = normalize_owner_id(owner_id)
    account = resolve_account(name, owner_id=owner_id)
    now = utc_now()
    with _connect() as db:
        db.execute("UPDATE longbridge_accounts SET is_default = 0 WHERE owner_id = ?", (owner_id,))
        db.execute(
            "UPDATE longbridge_accounts SET is_default = 1, updated_at = ? WHERE name = ?",
            (now, account.name),
        )
    return resolve_account(account.name, owner_id=owner_id)


def delete_account(name: str, owner_id: str | None = None, remove_home: bool = True) -> None:
    init_db_if_needed()
    normalized = normalize_name(name)
    if normalized == DEFAULT_ACCOUNT_NAME:
        raise ValueError("default Longbridge account profile cannot be deleted")
    account = resolve_account(normalized, owner_id=owner_id)
    with _connect() as db:
        db.execute("DELETE FROM longbridge_accounts WHERE name = ?", (account.name,))
    if remove_home:
        _remove_managed_home(account.home_dir)
    if account.is_default and account.owner_id == LOCAL_OWNER_ID:
        set_default_account(DEFAULT_ACCOUNT_NAME, owner_id=LOCAL_OWNER_ID)


def touch_account(name: str | None, owner_id: str | None = None) -> None:
    account = resolve_account(name, owner_id=owner_id)
    with _connect() as db:
        db.execute(
            "UPDATE longbridge_accounts SET last_used_at = ?, updated_at = ? WHERE name = ?",
            (utc_now(), utc_now(), account.name),
        )


def update_account_identity(name: str | None, check_payload: dict[str, Any], owner_id: str | None = None) -> LongbridgeAccount:
    account = resolve_account(name, owner_id=owner_id)
    session = check_payload.get("session") if isinstance(check_payload.get("session"), dict) else {}
    region_payload = check_payload.get("region") if isinstance(check_payload.get("region"), dict) else {}
    token_status = str(session.get("token") or "unknown")
    region = str(region_payload.get("active") or region_payload.get("cached") or "") or None
    fingerprint = (token_fingerprint(account.home_dir) or account.sdk_app_key_suffix) if token_status == "valid" else None
    meta = {
        "token": token_status,
        "detail": session.get("detail"),
        "region": region_payload,
        "fingerprint": fingerprint,
    }
    now = utc_now()
    with _connect() as db:
        db.execute(
            """
            UPDATE longbridge_accounts
            SET identity_fingerprint = ?, session_status = ?, region = ?,
                identity_meta_json = ?, identity_updated_at = ?, updated_at = ?
            WHERE name = ?
            """,
            (fingerprint, token_status, region, json.dumps(meta, ensure_ascii=False), now, now, account.name),
        )
    return resolve_account(account.name, owner_id=account.owner_id)


def update_account_sdk_credentials(
    name: str,
    app_key: str,
    app_secret: str,
    access_token: str,
    owner_id: str | None = None,
) -> LongbridgeAccount:
    account = resolve_account(name, owner_id=owner_id)
    _validate_sdk_credential(app_key, "app_key")
    _validate_sdk_credential(app_secret, "app_secret")
    _validate_sdk_credential(access_token, "access_token")
    now = utc_now()
    with _connect() as db:
        _store_sdk_credentials(db, account.name, app_key, app_secret, access_token, now)
    return resolve_account(account.name, owner_id=account.owner_id)


def get_account_sdk_credentials(name: str | None, owner_id: str | None = None) -> tuple[str, str, str] | None:
    account = resolve_account(name, owner_id=owner_id)
    with _connect() as db:
        row = db.execute(
            """
            SELECT sdk_app_key_enc, sdk_app_secret_enc, sdk_access_token_enc
            FROM longbridge_accounts
            WHERE name = ?
            """,
            (account.name,),
        ).fetchone()
    if row is None or not row["sdk_app_key_enc"] or not row["sdk_app_secret_enc"] or not row["sdk_access_token_enc"]:
        return None
    return (
        _decrypt_secret(str(row["sdk_app_key_enc"])),
        _decrypt_secret(str(row["sdk_app_secret_enc"])),
        _decrypt_secret(str(row["sdk_access_token_enc"])),
    )


def token_fingerprint(home_dir: str) -> str | None:
    token_root = Path(home_dir).expanduser() / ".longbridge" / "openapi" / "tokens"
    if not token_root.exists():
        fallback = Path(home_dir).expanduser() / ".longbridge" / "terminal" / ".openapi-session"
        if not fallback.exists():
            return None
        files = [fallback]
    else:
        files = sorted(path for path in token_root.rglob("*") if path.is_file())
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(Path(home_dir).expanduser())).encode("utf-8", errors="ignore"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:16]


def env_for_account(name: str | None = None, owner_id: str | None = None) -> tuple[LongbridgeAccount, dict[str, str]]:
    account = resolve_account(name, owner_id=owner_id)
    env = os.environ.copy()
    env["HOME"] = account.home_dir
    env.setdefault("LANG", "en_US.UTF-8")
    return account, env


def _store_sdk_credentials(db: Any, account_name: str, app_key: str, app_secret: str, access_token: str, updated_at: str) -> None:
    _validate_sdk_credential(app_key, "app_key")
    _validate_sdk_credential(app_secret, "app_secret")
    _validate_sdk_credential(access_token, "access_token")
    db.execute(
        """
        UPDATE longbridge_accounts
        SET sdk_app_key_enc = ?,
            sdk_app_secret_enc = ?,
            sdk_access_token_enc = ?,
            sdk_app_key_suffix = ?,
            sdk_credentials_updated_at = ?,
            updated_at = ?
        WHERE name = ?
        """,
        (
            _encrypt_secret(app_key),
            _encrypt_secret(app_secret),
            _encrypt_secret(access_token),
            app_key[-6:],
            updated_at,
            updated_at,
            account_name,
        ),
    )


def _validate_sdk_credential(value: str, field: str) -> None:
    text = str(value or "").strip()
    if not text or len(text) > 4096:
        raise ValueError(f"{field} is required and must be shorter than 4096 characters")
    if any(char in text for char in ("\x00", "\r", "\n")):
        raise ValueError(f"{field} contains invalid control characters")


def _credential_secret() -> bytes:
    value = (
        os.getenv("AI_OPTION_CREDENTIAL_SECRET")
        or os.getenv("AI_OPTION_AUTH_SECRET")
        or f"dev-local-secret:{DB_PATH}"
    )
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).digest()


def _encrypt_secret(value: str) -> str:
    nonce = os.urandom(16)
    plaintext = value.encode("utf-8")
    stream = _keystream(nonce, len(plaintext))
    ciphertext = bytes(left ^ right for left, right in zip(plaintext, stream))
    tag = hmac.new(_credential_secret(), nonce + ciphertext, hashlib.sha256).digest()
    payload = nonce + tag + ciphertext
    return "v1:" + base64.urlsafe_b64encode(payload).decode("ascii")


def _decrypt_secret(value: str) -> str:
    if not value.startswith("v1:"):
        raise ValueError("unsupported credential secret format")
    payload = base64.urlsafe_b64decode(value[3:].encode("ascii"))
    nonce, tag, ciphertext = payload[:16], payload[16:48], payload[48:]
    expected = hmac.new(_credential_secret(), nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("credential secret verification failed")
    stream = _keystream(nonce, len(ciphertext))
    plaintext = bytes(left ^ right for left, right in zip(ciphertext, stream))
    return plaintext.decode("utf-8")


def _keystream(nonce: bytes, length: int) -> bytes:
    chunks = []
    counter = 0
    key = _credential_secret()
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:length]


def normalize_name(name: str) -> str:
    normalized = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", normalized):
        raise ValueError("account profile name must match [a-z0-9][a-z0-9_.-]{0,63}")
    return normalized


def normalize_owner_id(owner_id: str | None) -> str:
    value = (owner_id or LOCAL_OWNER_ID).strip().lower()
    digest = hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]
    safe = re.sub(r"[^a-z0-9_.-]+", "-", value).strip(".-_")
    if not safe:
        return f"user-{digest}"
    return safe[:80]


def _stored_name_for_owner(name: str, owner_id: str) -> str:
    if owner_id == LOCAL_OWNER_ID:
        return name
    digest = hashlib.sha256(owner_id.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return normalize_name(f"u{digest}_{name[:50]}")


def _remove_managed_home(home_dir: str) -> None:
    path = Path(home_dir).expanduser().resolve()
    root = PROFILE_ROOT.resolve()
    if path == Path.home().resolve() or root not in path.parents:
        return
    shutil.rmtree(path.parent if path.name == "home" else path, ignore_errors=True)


def init_db_if_needed() -> None:
    if is_postgres() or not DB_PATH.exists():
        init_db()


def _connect() -> Any:
    return connect()


def _ensure_column(db: Any, table: str, column: str, declaration: str) -> None:
    ensure_column(db, table, column, declaration)


def _row_to_account(row: Any) -> LongbridgeAccount:
    meta_json = row["identity_meta_json"] if "identity_meta_json" in row.keys() else None
    try:
        meta = json.loads(meta_json) if meta_json else {}
    except json.JSONDecodeError:
        meta = {}
    return LongbridgeAccount(
        id=int(row["id"]),
        name=str(row["name"]),
        owner_id=str(row["owner_id"]) if "owner_id" in row.keys() and row["owner_id"] is not None else LOCAL_OWNER_ID,
        label=str(row["label"]),
        home_dir=str(row["home_dir"]),
        is_default=bool(row["is_default"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_used_at=str(row["last_used_at"]) if row["last_used_at"] is not None else None,
        identity_fingerprint=str(row["identity_fingerprint"]) if row["identity_fingerprint"] is not None else None,
        session_status=str(row["session_status"]) if row["session_status"] is not None else None,
        region=str(row["region"]) if row["region"] is not None else None,
        identity_meta=meta,
        identity_updated_at=str(row["identity_updated_at"]) if row["identity_updated_at"] is not None else None,
        sdk_credentials_configured=bool(row["sdk_credentials_updated_at"]) if "sdk_credentials_updated_at" in row.keys() else False,
        sdk_app_key_suffix=str(row["sdk_app_key_suffix"]) if "sdk_app_key_suffix" in row.keys() and row["sdk_app_key_suffix"] is not None else None,
        sdk_credentials_updated_at=str(row["sdk_credentials_updated_at"]) if "sdk_credentials_updated_at" in row.keys() and row["sdk_credentials_updated_at"] is not None else None,
    )


init_db()

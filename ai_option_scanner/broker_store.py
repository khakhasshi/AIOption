from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .account_store import (
    LOCAL_OWNER_ID,
    _decrypt_secret,
    _encrypt_secret,
    _validate_sdk_credential,
    normalize_name,
    normalize_owner_id,
    utc_now,
)
from .db import connect, ensure_column


@dataclass(frozen=True)
class BrokerAccount:
    id: int
    owner_id: str
    broker: str
    name: str
    label: str
    is_default: bool
    paper: bool
    api_key_suffix: str | None
    credentials_updated_at: str | None
    status: str | None
    status_meta: dict[str, Any]
    created_at: str
    updated_at: str
    last_used_at: str | None

    @property
    def ref(self) -> str:
        return broker_ref(self.broker, self.name, self.owner_id)


def init_broker_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS broker_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                broker TEXT NOT NULL,
                name TEXT NOT NULL,
                label TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                paper INTEGER NOT NULL DEFAULT 1,
                api_key_enc TEXT,
                api_secret_enc TEXT,
                api_key_suffix TEXT,
                credentials_updated_at TEXT,
                status TEXT,
                status_meta_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT,
                UNIQUE(owner_id, broker, name)
            )
            """
        )
        ensure_column(db, "broker_accounts", "paper", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "broker_accounts", "status", "TEXT")
        ensure_column(db, "broker_accounts", "status_meta_json", "TEXT")
        # uSMART needs more than a key/secret pair (channel id, RSA signing private
        # key, RSA encrypt public key, login phone/areaCode, trade password). Store
        # that structured blob, encrypted, in a single extra column so the existing
        # key/secret slots stay meaningful for the other brokers.
        ensure_column(db, "broker_accounts", "extra_enc", "TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_broker_accounts_owner ON broker_accounts(owner_id, broker)")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_broker_accounts_owner_default "
            "ON broker_accounts(owner_id, broker, is_default) WHERE is_default = 1"
        )


def broker_ref(broker: str, name: str | None, owner_id: str | None = None) -> str:
    normalized_broker = normalize_broker(broker)
    normalized_name = normalize_name(name or "default")
    if owner_id:
        return f"{normalized_broker}:{normalize_owner_id(owner_id)}:{normalized_name}"
    return f"{normalized_broker}:{normalized_name}"


def parse_broker_ref(value: str | None, default_broker: str = "longbridge") -> tuple[str, str | None, str | None]:
    text = str(value or "").strip()
    if ":" not in text:
        return normalize_broker(default_broker), None, text or None
    parts = text.split(":", 2)
    if len(parts) == 3:
        broker, owner, name = parts
        return normalize_broker(broker), normalize_owner_id(owner), normalize_name(name or "default")
    broker, name = parts
    return normalize_broker(broker), None, normalize_name(name or "default")


def list_broker_accounts(owner_id: str | None = None, broker: str | None = None) -> list[BrokerAccount]:
    init_broker_db()
    clauses = []
    params: list[Any] = []
    if owner_id is not None:
        clauses.append("owner_id = ?")
        params.append(normalize_owner_id(owner_id))
    if broker is not None:
        clauses.append("broker = ?")
        params.append(normalize_broker(broker))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM broker_accounts
            {where}
            ORDER BY broker ASC, is_default DESC, name ASC
            """,
            tuple(params),
        ).fetchall()
    return [_row_to_broker_account(row) for row in rows]


def broker_accounts_as_rows(owner_id: str | None = None, broker: str | None = None) -> list[dict[str, Any]]:
    return [asdict(account) | {"ref": account.ref} for account in list_broker_accounts(owner_id, broker)]


def create_broker_account(
    broker: str,
    name: str,
    *,
    label: str | None = None,
    api_key: str,
    api_secret: str,
    paper: bool = True,
    set_default: bool = False,
    owner_id: str = LOCAL_OWNER_ID,
) -> BrokerAccount:
    init_broker_db()
    normalized_broker = normalize_broker(broker)
    if normalized_broker == "longbridge":
        raise ValueError("Longbridge accounts are managed by the Longbridge account store")
    _validate_sdk_credential(api_key, "api_key")
    _validate_sdk_credential(api_secret, "api_secret")
    owner = normalize_owner_id(owner_id)
    normalized_name = normalize_name(name)
    now = utc_now()
    with connect() as db:
        if set_default:
            db.execute("UPDATE broker_accounts SET is_default = 0 WHERE owner_id = ? AND broker = ?", (owner, normalized_broker))
        db.execute(
            """
            INSERT INTO broker_accounts
                (owner_id, broker, name, label, is_default, paper, api_key_enc, api_secret_enc,
                 api_key_suffix, credentials_updated_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, broker, name) DO UPDATE SET
                label = excluded.label,
                is_default = CASE WHEN excluded.is_default = 1 THEN 1 ELSE broker_accounts.is_default END,
                paper = excluded.paper,
                api_key_enc = excluded.api_key_enc,
                api_secret_enc = excluded.api_secret_enc,
                api_key_suffix = excluded.api_key_suffix,
                credentials_updated_at = excluded.credentials_updated_at,
                updated_at = excluded.updated_at
            """,
            (
                owner,
                normalized_broker,
                normalized_name,
                label or normalized_name,
                1 if set_default else 0,
                1 if paper else 0,
                _encrypt_secret(api_key),
                _encrypt_secret(api_secret),
                api_key[-6:],
                now,
                now,
                now,
            ),
        )
    return resolve_broker_account(normalized_broker, normalized_name, owner_id=owner)


def resolve_broker_account(broker: str, name: str | None = None, owner_id: str | None = None) -> BrokerAccount:
    init_broker_db()
    normalized_broker = normalize_broker(broker)
    owner = normalize_owner_id(owner_id)
    normalized_name = normalize_name(name) if name else None
    if normalized_name:
        query = "SELECT * FROM broker_accounts WHERE owner_id = ? AND broker = ? AND name = ?"
        params: tuple[Any, ...] = (owner, normalized_broker, normalized_name)
    else:
        query = "SELECT * FROM broker_accounts WHERE owner_id = ? AND broker = ? AND is_default = 1 LIMIT 1"
        params = (owner, normalized_broker)
    with connect() as db:
        row = db.execute(query, params).fetchone()
    if row is None:
        label = normalized_name or "default"
        raise ValueError(f"{normalized_broker} broker account `{label}` does not exist")
    return _row_to_broker_account(row)


def get_broker_credentials(account: BrokerAccount) -> tuple[str, str]:
    init_broker_db()
    with connect() as db:
        row = db.execute(
            "SELECT api_key_enc, api_secret_enc FROM broker_accounts WHERE id = ?",
            (account.id,),
        ).fetchone()
    if row is None or not row["api_key_enc"] or not row["api_secret_enc"]:
        raise ValueError(f"{account.broker} account `{account.name}` has no API credentials")
    return _decrypt_secret(str(row["api_key_enc"])), _decrypt_secret(str(row["api_secret_enc"]))


# uSMART credential fields beyond the key/secret pair. `channel` and `sign_private_key`
# are mapped onto the shared api_key_enc/api_secret_enc slots (so get_broker_credentials
# still works); the rest live in the encrypted extra blob.
USMART_EXTRA_FIELDS = ("encrypt_public_key", "phone", "area_code", "trade_password")


def _validate_pem_credential(value: str, field: str) -> None:
    """PEM keys legitimately contain newlines, so the standard SDK-credential
    check (which rejects control chars) is too strict. Validate length + NUL only."""
    text = str(value or "").strip()
    if not text or len(text) > 8192:
        raise ValueError(f"{field} is required and must be shorter than 8192 characters")
    if "\x00" in text:
        raise ValueError(f"{field} contains invalid NUL characters")


def create_usmart_account(
    name: str,
    *,
    label: str | None = None,
    channel: str,
    sign_private_key: str,
    encrypt_public_key: str,
    phone: str,
    area_code: str = "852",
    trade_password: str = "",
    paper: bool = True,
    set_default: bool = False,
    owner_id: str = LOCAL_OWNER_ID,
) -> BrokerAccount:
    """Create/update a uSMART broker account. channel → api_key slot, RSA signing
    private key → api_secret slot, and the remaining fields → encrypted extra blob."""
    init_broker_db()
    _validate_sdk_credential(channel, "channel")
    _validate_pem_credential(sign_private_key, "sign_private_key")
    _validate_pem_credential(encrypt_public_key, "encrypt_public_key")
    _validate_sdk_credential(phone, "phone")
    owner = normalize_owner_id(owner_id)
    normalized_name = normalize_name(name)
    extra = {
        "encrypt_public_key": encrypt_public_key,
        "phone": phone,
        "area_code": str(area_code or "852"),
        "trade_password": trade_password or "",
    }
    now = utc_now()
    with connect() as db:
        if set_default:
            db.execute("UPDATE broker_accounts SET is_default = 0 WHERE owner_id = ? AND broker = ?", (owner, "usmart"))
        db.execute(
            """
            INSERT INTO broker_accounts
                (owner_id, broker, name, label, is_default, paper, api_key_enc, api_secret_enc,
                 api_key_suffix, extra_enc, credentials_updated_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, broker, name) DO UPDATE SET
                label = excluded.label,
                is_default = CASE WHEN excluded.is_default = 1 THEN 1 ELSE broker_accounts.is_default END,
                paper = excluded.paper,
                api_key_enc = excluded.api_key_enc,
                api_secret_enc = excluded.api_secret_enc,
                api_key_suffix = excluded.api_key_suffix,
                extra_enc = excluded.extra_enc,
                credentials_updated_at = excluded.credentials_updated_at,
                updated_at = excluded.updated_at
            """,
            (
                owner,
                "usmart",
                normalized_name,
                label or normalized_name,
                1 if set_default else 0,
                1 if paper else 0,
                _encrypt_secret(channel),
                _encrypt_secret(sign_private_key),
                channel[-6:],
                _encrypt_secret(json.dumps(extra, ensure_ascii=False)),
                now,
                now,
                now,
            ),
        )
    return resolve_broker_account("usmart", normalized_name, owner_id=owner)


def get_usmart_credentials(account: BrokerAccount) -> dict[str, str]:
    """Full uSMART credential bundle: channel + signing key (from the shared slots)
    plus the extra blob (encrypt public key, phone, area_code, trade_password)."""
    init_broker_db()
    channel, sign_private_key = get_broker_credentials(account)
    with connect() as db:
        row = db.execute("SELECT extra_enc FROM broker_accounts WHERE id = ?", (account.id,)).fetchone()
    extra: dict[str, Any] = {}
    if row is not None and row["extra_enc"]:
        extra = _loads(_decrypt_secret(str(row["extra_enc"]))) or {}
    return {
        "channel": channel,
        "sign_private_key": sign_private_key,
        "encrypt_public_key": str(extra.get("encrypt_public_key") or ""),
        "phone": str(extra.get("phone") or ""),
        "area_code": str(extra.get("area_code") or "852"),
        "trade_password": str(extra.get("trade_password") or ""),
    }


def set_broker_account_default(broker: str, name: str, owner_id: str = LOCAL_OWNER_ID) -> BrokerAccount:
    account = resolve_broker_account(broker, name, owner_id=owner_id)
    now = utc_now()
    with connect() as db:
        db.execute("UPDATE broker_accounts SET is_default = 0 WHERE owner_id = ? AND broker = ?", (account.owner_id, account.broker))
        db.execute("UPDATE broker_accounts SET is_default = 1, updated_at = ? WHERE id = ?", (now, account.id))
    return resolve_broker_account(account.broker, account.name, owner_id=account.owner_id)


def delete_broker_account(broker: str, name: str, owner_id: str | None = None) -> None:
    account = resolve_broker_account(broker, name, owner_id=owner_id)
    with connect() as db:
        db.execute("DELETE FROM broker_accounts WHERE id = ?", (account.id,))


def touch_broker_account(account: BrokerAccount) -> None:
    with connect() as db:
        db.execute("UPDATE broker_accounts SET last_used_at = ?, updated_at = ? WHERE id = ?", (utc_now(), utc_now(), account.id))


def update_broker_account_status(account: BrokerAccount, status: str, meta: dict[str, Any] | None = None) -> BrokerAccount:
    now = utc_now()
    with connect() as db:
        db.execute(
            "UPDATE broker_accounts SET status = ?, status_meta_json = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(meta or {}, ensure_ascii=False), now, account.id),
        )
    return resolve_broker_account(account.broker, account.name, owner_id=account.owner_id)


def normalize_broker(value: str | None) -> str:
    text = str(value or "longbridge").strip().lower()
    return text if text in {"longbridge", "alpaca", "usmart"} else "longbridge"


def _row_to_broker_account(row: Any) -> BrokerAccount:
    return BrokerAccount(
        id=int(row["id"]),
        owner_id=str(row["owner_id"]),
        broker=str(row["broker"]),
        name=str(row["name"]),
        label=str(row["label"]),
        is_default=bool(row["is_default"]),
        paper=bool(row["paper"]),
        api_key_suffix=row["api_key_suffix"],
        credentials_updated_at=row["credentials_updated_at"],
        status=row["status"],
        status_meta=_loads(row["status_meta_json"]) or {},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_used_at=row["last_used_at"],
    )


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from .account_store import delete_account, resolve_account, update_account_identity
from .longbridge_client import LongbridgeError
from .longbridge_sdk_client import check as sdk_check, logout as sdk_logout


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LongbridgeAuthManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._session_cache_ttl = 20.0

    def status(self, account_name: str | None = None, owner_id: str | None = None, force: bool = False) -> dict[str, Any]:
        account = resolve_account(account_name, owner_id=owner_id)
        session = self._check_session(account.name, owner_id=account.owner_id, force=force)
        refreshed = resolve_account(account.name, owner_id=account.owner_id)
        return {
            "mode": "python_sdk_api_key",
            "account_name": refreshed.name,
            "login_status": "configured" if session.get("token") == "valid" else "missing_or_invalid",
            "running": False,
            "logs": [],
            "account": {
                "name": refreshed.name,
                "label": refreshed.label,
                "home_dir": refreshed.home_dir,
                "is_default": refreshed.is_default,
                "identity_fingerprint": refreshed.identity_fingerprint,
                "session_status": refreshed.session_status,
                "region": refreshed.region,
                "identity_updated_at": refreshed.identity_updated_at,
                "sdk_credentials_configured": refreshed.sdk_credentials_configured,
                "sdk_app_key_suffix": refreshed.sdk_app_key_suffix,
                "sdk_credentials_updated_at": refreshed.sdk_credentials_updated_at,
            },
            "session": session,
        }

    def logout(self, account_name: str | None = None, owner_id: str | None = None) -> dict[str, Any]:
        account = resolve_account(account_name, owner_id=owner_id)
        sdk_logout(account.name)
        with self._lock:
            self._session_cache.pop(account.name, None)
        return self.status(account.name, owner_id=account.owner_id, force=True)

    def remove_account(self, account_name: str, owner_id: str | None = None) -> None:
        account = resolve_account(account_name, owner_id=owner_id)
        with self._lock:
            self._session_cache.pop(account.name, None)
        try:
            sdk_logout(account.name)
        except LongbridgeError:
            pass
        delete_account(account.name, owner_id=account.owner_id, remove_home=True)

    def _check_session(self, account_name: str, owner_id: str | None = None, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            cached = self._session_cache.get(account_name)
        if cached and not force and now - cached[0] < self._session_cache_ttl:
            return cached[1]
        try:
            payload = sdk_check(account_name)
        except Exception as exc:  # noqa: BLE001 - surface SDK credential/setup issues as status details.
            session = {"token": "invalid", "detail": str(exc)}
        else:
            update_account_identity(account_name, payload, owner_id=owner_id)
            session = payload.get("session", payload)
        with self._lock:
            self._session_cache[account_name] = (now, session)
        return session


auth_manager = LongbridgeAuthManager()

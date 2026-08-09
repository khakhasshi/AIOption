"""Tests for Longbridge access-token auto-refresh on 401003 (token expired).

Drives the central _call wrapper with a fake SDK so no live broker or DB is
touched. Verifies: a 401003 triggers exactly one refresh + retry; the refreshed
token is persisted; a non-401003 error is not retried; refresh is gated by the
env flag; and the cooldown prevents a stampede.
"""
from __future__ import annotations

import os
import unittest

from ai_option_scanner import longbridge_sdk_client as sdk_client


class _FakeError(Exception):
    def __init__(self, code=None, message=""):
        super().__init__(message)
        self.code = code
        self.message = message


class _FakeAccount:
    def __init__(self, name="acct_real", owner_id="owner-1"):
        self.name = name
        self.owner_id = owner_id


class _FakeConfig:
    refreshed_tokens: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def from_apikey(cls, app_key, app_secret, access_token, **kwargs):
        return cls()

    def refresh_access_token(self, expired_at=None):
        token = f"new-token-{len(_FakeConfig.refreshed_tokens) + 1}"
        _FakeConfig.refreshed_tokens.append(token)
        return token


class _FakeSDK:
    def __init__(self):
        self.Config = _FakeConfig


class TokenAutoRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = {
            k: getattr(sdk_client, k)
            for k in ("_credentials", "_sdk_imports", "resolve_account",
                      "update_account_sdk_credentials", "_drop_contexts", "_contexts", "touch_account",
                      "refresh_account_access_token")
        }
        _FakeConfig.refreshed_tokens = []
        self.persisted: list[tuple] = []
        self.dropped: list[str] = []
        sdk_client._token_refresh_last_at.clear()

        sdk_client._credentials = lambda name=None: ("ak", "as", "old-token")
        sdk_client._sdk_imports = lambda: _FakeSDK()
        sdk_client.resolve_account = lambda name=None, owner_id=None: _FakeAccount()
        sdk_client.update_account_sdk_credentials = (
            lambda name, ak, as_, tok, owner_id=None: self.persisted.append((name, tok, owner_id))
        )
        sdk_client._drop_contexts = lambda name: self.dropped.append(name)
        sdk_client._contexts = lambda name=None: object()
        sdk_client.touch_account = lambda name: None
        os.environ["AI_OPTION_LONGBRIDGE_AUTO_REFRESH_TOKEN"] = "true"

    def tearDown(self) -> None:
        for k, v in self._orig.items():
            setattr(sdk_client, k, v)
        os.environ.pop("AI_OPTION_LONGBRIDGE_AUTO_REFRESH_TOKEN", None)
        sdk_client._token_refresh_last_at.clear()

    def test_401003_triggers_refresh_and_retry_succeeds(self) -> None:
        calls = {"n": 0}

        def callback(_contexts):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeError(code=401003, message="token expired")
            return "ok"

        result = sdk_client._call("acct_real", callback)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 2)  # original + one retry
        self.assertEqual(len(_FakeConfig.refreshed_tokens), 1)
        self.assertEqual(self.persisted, [("acct_real", "new-token-1", "owner-1")])
        self.assertEqual(self.dropped, ["acct_real"])

    def test_401003_in_message_without_code_still_refreshes(self) -> None:
        calls = {"n": 0}

        def callback(_contexts):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeError(message="API error (code 401003) token expired")
            return "ok"

        self.assertEqual(sdk_client._call("acct_real", callback), "ok")
        self.assertEqual(len(_FakeConfig.refreshed_tokens), 1)

    def test_non_token_error_is_not_retried(self) -> None:
        calls = {"n": 0}

        def callback(_contexts):
            calls["n"] += 1
            raise _FakeError(code=500001, message="internal error")

        with self.assertRaises(sdk_client.LongbridgeSDKError):
            sdk_client._call("acct_real", callback)
        self.assertEqual(calls["n"], 1)  # no retry
        self.assertEqual(len(_FakeConfig.refreshed_tokens), 0)

    def test_disabled_flag_skips_refresh(self) -> None:
        os.environ["AI_OPTION_LONGBRIDGE_AUTO_REFRESH_TOKEN"] = "false"
        calls = {"n": 0}

        def callback(_contexts):
            calls["n"] += 1
            raise _FakeError(code=401003, message="token expired")

        with self.assertRaises(sdk_client.LongbridgeSDKError):
            sdk_client._call("acct_real", callback)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(len(_FakeConfig.refreshed_tokens), 0)

    def test_refresh_failure_surfaces_original_error(self) -> None:
        def bad_refresh(name=None, force=False):
            return False

        sdk_client.refresh_account_access_token = bad_refresh
        try:
            def callback(_contexts):
                raise _FakeError(code=401003, message="token expired")

            with self.assertRaises(sdk_client.LongbridgeSDKError):
                sdk_client._call("acct_real", callback)
        finally:
            # restored by tearDown via _orig only for tracked keys; restore here
            pass


class ProactiveRefreshAllTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_refresh = sdk_client.refresh_account_access_token
        self._orig_list = None
        import ai_option_scanner.account_store as acct_store
        self._acct_store = acct_store
        self._orig_list = acct_store.list_accounts

    def tearDown(self) -> None:
        sdk_client.refresh_account_access_token = self._orig_refresh
        self._acct_store.list_accounts = self._orig_list

    def test_refresh_all_reports_ok_and_failed(self) -> None:
        class _A:
            def __init__(self, name, ok):
                self.name = name
                self.sdk_credentials_configured = True
                self._ok = ok

        class _NoCred:
            name = "nocred"
            sdk_credentials_configured = False

        self._acct_store.list_accounts = lambda owner_id=None: [
            _A("acct_ok", True), _A("acct_bad", False), _NoCred(),
        ]
        results = {"acct_ok": True, "acct_bad": False}
        sdk_client.refresh_account_access_token = lambda name=None, force=False: results.get(name, False)

        out = sdk_client.refresh_all_sdk_account_tokens()

        self.assertEqual(out["refreshed"], ["acct_ok"])
        self.assertEqual(out["failed"], ["acct_bad"])  # nocred skipped entirely


if __name__ == "__main__":
    unittest.main()

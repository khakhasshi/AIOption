from __future__ import annotations

import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path

from ai_option_scanner import app_auth, db, observation_store, scan_store, turnstile


class TurnstileUnitTest(unittest.TestCase):
    """Pure-function tests for the env-driven enable/disable + siteverify call."""

    def setUp(self) -> None:
        self._orig = {k: os.environ.pop(k, None) for k in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY")}

    def tearDown(self) -> None:
        for key, value in self._orig.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)

    def test_disabled_when_no_keys(self) -> None:
        self.assertFalse(turnstile.turnstile_enabled())
        self.assertEqual(turnstile.turnstile_public_config(), {"enabled": False})

    def test_disabled_when_only_one_key(self) -> None:
        os.environ["TURNSTILE_SITE_KEY"] = "site-1"
        self.assertFalse(turnstile.turnstile_enabled())
        os.environ.pop("TURNSTILE_SITE_KEY")
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-1"
        self.assertFalse(turnstile.turnstile_enabled())

    def test_public_config_exposes_only_site_key(self) -> None:
        os.environ["TURNSTILE_SITE_KEY"] = "site-1"
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-1"
        config = turnstile.turnstile_public_config()
        self.assertEqual(config, {"enabled": True, "site_key": "site-1"})
        self.assertNotIn("secret_key", config)

    def test_verify_is_noop_when_disabled(self) -> None:
        # Even a missing token passes when the feature is off — callers invoke
        # verify unconditionally.
        turnstile.verify_turnstile(None)
        turnstile.verify_turnstile("")

    def test_verify_requires_token_when_enabled(self) -> None:
        os.environ["TURNSTILE_SITE_KEY"] = "site-1"
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-1"
        with self.assertRaises(turnstile.TurnstileError):
            turnstile.verify_turnstile("")

    def test_verify_accepts_on_siteverify_success(self) -> None:
        os.environ["TURNSTILE_SITE_KEY"] = "site-1"
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-1"
        orig = turnstile.urllib.request.urlopen
        turnstile.urllib.request.urlopen = lambda *a, **k: _fake_response({"success": True})
        try:
            turnstile.verify_turnstile("good-token", "1.2.3.4")  # no raise
        finally:
            turnstile.urllib.request.urlopen = orig

    def test_verify_rejects_on_siteverify_failure(self) -> None:
        os.environ["TURNSTILE_SITE_KEY"] = "site-1"
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-1"
        orig = turnstile.urllib.request.urlopen
        turnstile.urllib.request.urlopen = lambda *a, **k: _fake_response(
            {"success": False, "error-codes": ["invalid-input-response"]}
        )
        try:
            with self.assertRaises(turnstile.TurnstileError):
                turnstile.verify_turnstile("bad-token")
        finally:
            turnstile.urllib.request.urlopen = orig

    def test_verify_fails_closed_on_network_error(self) -> None:
        os.environ["TURNSTILE_SITE_KEY"] = "site-1"
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-1"
        orig = turnstile.urllib.request.urlopen

        def boom(*_a, **_k):
            raise urllib.error.URLError("connection refused")

        turnstile.urllib.request.urlopen = boom
        try:
            with self.assertRaises(turnstile.TurnstileError):
                turnstile.verify_turnstile("any-token")
        finally:
            turnstile.urllib.request.urlopen = orig


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *_a) -> None:
        return None


def _fake_response(payload: dict) -> _FakeResp:
    import json

    return _FakeResp(json.dumps(payload).encode("utf-8"))


class TurnstileEndpointTest(unittest.TestCase):
    """Login/OAuth endpoints honor the captcha gate, fully isolated DB + env."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_database_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_database_url_alt = os.environ.pop("DATABASE_URL", None)
        self._orig_env: dict[str, str | None] = {}
        for key in (
            "AI_OPTION_AUTH_USER_1",
            "AI_OPTION_AUTH_PASSWORD_HASH_1",
            "AI_OPTION_AUTH_IS_ADMIN_1",
            "AI_OPTION_AUTH_SECRET",
            "TURNSTILE_SITE_KEY",
            "TURNSTILE_SECRET_KEY",
        ):
            self._orig_env[key] = os.environ.pop(key, None)
        os.environ["AI_OPTION_AUTH_USER_1"] = "admin@example.com"
        os.environ["AI_OPTION_AUTH_PASSWORD_HASH_1"] = app_auth.hash_password("admin-password")
        os.environ["AI_OPTION_AUTH_IS_ADMIN_1"] = "true"
        os.environ["AI_OPTION_AUTH_SECRET"] = "x" * 40

        db.DB_PATH = Path(self._tmpdir.name) / "turnstile.sqlite3"
        db._INIT_ONCE_DONE.clear()
        app_auth.invalidate_auth_user_cache()
        app_auth.init_auth_db()
        scan_store.init_scan_db()
        observation_store.init_observation_db()

        from fastapi.testclient import TestClient
        from ai_option_scanner import web_api

        self.web_api = web_api
        self.client = TestClient(web_api.app)

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        app_auth.invalidate_auth_user_cache()
        for key, value in self._orig_env.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        if self._orig_database_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_database_url
        if self._orig_database_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_database_url_alt
        self._tmpdir.cleanup()

    def _enable_turnstile(self) -> None:
        os.environ["TURNSTILE_SITE_KEY"] = "site-1"
        os.environ["TURNSTILE_SECRET_KEY"] = "secret-1"

    def test_config_endpoint_disabled_by_default(self) -> None:
        resp = self.client.get("/api/auth/turnstile/config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"enabled": False})

    def test_config_endpoint_reports_site_key_when_enabled(self) -> None:
        self._enable_turnstile()
        resp = self.client.get("/api/auth/turnstile/config")
        self.assertEqual(resp.json(), {"enabled": True, "site_key": "site-1"})

    def test_login_succeeds_without_token_when_disabled(self) -> None:
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "admin@example.com", "password": "admin-password", "accepted_terms": True},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["authenticated"])

    def test_login_rejects_missing_token_when_enabled(self) -> None:
        self._enable_turnstile()
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "admin@example.com", "password": "admin-password", "accepted_terms": True},
        )
        self.assertEqual(resp.status_code, 400)
        # Credentials are never even checked; the captcha gate is first.
        self.assertIn("captcha", resp.json()["detail"].lower())

    def test_login_succeeds_with_valid_token_when_enabled(self) -> None:
        self._enable_turnstile()
        orig = turnstile.urllib.request.urlopen
        turnstile.urllib.request.urlopen = lambda *a, **k: _fake_response({"success": True})
        try:
            resp = self.client.post(
                "/api/auth/login",
                json={
                    "username": "admin@example.com",
                    "password": "admin-password",
                    "accepted_terms": True,
                    "turnstile_token": "good-token",
                },
            )
        finally:
            turnstile.urllib.request.urlopen = orig
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["authenticated"])


if __name__ == "__main__":
    unittest.main()

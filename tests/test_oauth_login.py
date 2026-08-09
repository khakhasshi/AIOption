from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ai_option_scanner import app_auth, db, oauth_login, oauth_store, observation_store, scan_store


class OAuthLoginTestBase(unittest.TestCase):
    """Shared temp-DB + env isolation, mirroring tests/test_app_auth.py."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_database_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_database_url_alt = os.environ.pop("DATABASE_URL", None)
        # One env-configured admin so auth_enabled() is True without depending on
        # the DB having rows yet.
        self._orig_env: dict[str, str | None] = {}
        for key in (
            "AI_OPTION_AUTH_USER_1",
            "AI_OPTION_AUTH_PASSWORD_HASH_1",
            "AI_OPTION_AUTH_IS_ADMIN_1",
            "GOOGLE_OAUTH_CLIENT_ID",
            "APPLE_OAUTH_CLIENT_ID",
            "AI_OPTION_AUTH_SECRET",
        ):
            self._orig_env[key] = os.environ.pop(key, None)
        os.environ["AI_OPTION_AUTH_USER_1"] = "admin@example.com"
        os.environ["AI_OPTION_AUTH_PASSWORD_HASH_1"] = app_auth.hash_password("admin-password")
        os.environ["AI_OPTION_AUTH_IS_ADMIN_1"] = "true"
        os.environ["AI_OPTION_AUTH_SECRET"] = "x" * 40
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "test-client.apps.googleusercontent.com"

        db.DB_PATH = Path(self._tmpdir.name) / "oauth.sqlite3"
        db._INIT_ONCE_DONE.clear()
        app_auth.invalidate_auth_user_cache()
        app_auth.init_auth_db()
        oauth_store.init_oauth_db()
        scan_store.init_scan_db()
        observation_store.init_observation_db()
        oauth_login._jwks_clients.clear()

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


class OAuthStoreTest(OAuthLoginTestBase):
    def test_provision_oauth_user_applies_trial_limits(self) -> None:
        user = app_auth.provision_oauth_user("trader@gmail.com")
        self.assertEqual(user.username, "trader@gmail.com")
        self.assertTrue(user.can_analyze)
        self.assertFalse(user.can_trade)
        self.assertFalse(user.is_admin)
        self.assertEqual(user.max_daily_scans, 5)
        self.assertEqual(user.max_daily_ai_scans, 5)
        self.assertEqual(user.max_daily_ai_chat, 10)
        # Every other resource is disabled (0).
        self.assertEqual(user.max_watchlists, 0)
        self.assertEqual(user.max_scan_loop_instances, 0)
        self.assertEqual(user.max_notification_channels, 0)
        self.assertEqual(user.max_longbridge_accounts, 0)

    def test_provision_is_idempotent(self) -> None:
        first = app_auth.provision_oauth_user("dup@gmail.com")
        second = app_auth.provision_oauth_user("dup@gmail.com")
        self.assertEqual(first.username, second.username)

    def test_oauth_user_cannot_password_login(self) -> None:
        app_auth.provision_oauth_user("nopass@gmail.com")
        self.assertFalse(app_auth.user_has_password("nopass@gmail.com"))
        self.assertFalse(app_auth.verify_login("nopass@gmail.com", app_auth.OAUTH_UNUSABLE_PASSWORD))

    def test_trial_expiry_is_15_days(self) -> None:
        user = app_auth.provision_oauth_user("expiry@gmail.com")
        remaining = app_auth.auth_user_remaining_seconds(user)
        self.assertIsNotNone(remaining)
        # 15 days minus a sliver of execution time.
        self.assertGreater(remaining, 14 * 86400)
        self.assertLessEqual(remaining, 15 * 86400)

    def test_link_and_unlink_identity(self) -> None:
        oauth_store.link_identity("google", "sub-123", "user@gmail.com", "user@gmail.com")
        self.assertEqual(oauth_store.find_username_by_identity("google", "sub-123"), "user@gmail.com")
        rows = oauth_store.list_identities_for_user("user@gmail.com")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "google")
        removed = oauth_store.unlink_identity("google", "user@gmail.com")
        self.assertEqual(removed, 1)
        self.assertIsNone(oauth_store.find_username_by_identity("google", "sub-123"))


class OAuthVerifyConfigTest(OAuthLoginTestBase):
    def test_only_configured_providers_are_advertised(self) -> None:
        config = oauth_login.oauth_public_config()
        providers = {entry["provider"] for entry in config["providers"]}
        self.assertEqual(providers, {"google"})  # apple not configured in setUp
        self.assertTrue(config["enabled"])

    def test_unsupported_provider_rejected(self) -> None:
        with self.assertRaises(oauth_login.OAuthError):
            oauth_login.normalize_provider("facebook")

    def test_verify_rejects_when_provider_unconfigured(self) -> None:
        with self.assertRaises(oauth_login.OAuthError):
            oauth_login.verify_id_token("apple", "any-token")


class OAuthEndpointTest(OAuthLoginTestBase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        from ai_option_scanner import web_api

        self.web_api = web_api
        self.client = TestClient(web_api.app)
        self._orig_verify = web_api.verify_id_token

    def tearDown(self) -> None:
        self.web_api.verify_id_token = self._orig_verify
        super().tearDown()

    def _patch_verify(self, identity: dict) -> None:
        def fake_verify(provider: str, credential: str, nonce=None) -> dict:
            return {"provider": provider, **identity}

        self.web_api.verify_id_token = fake_verify

    def test_config_endpoint_public(self) -> None:
        resp = self.client.get("/api/auth/oauth/config")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["enabled"])

    def test_login_requires_terms(self) -> None:
        self._patch_verify({"sub": "g-1", "email": "newbie@gmail.com", "email_verified": True})
        resp = self.client.post(
            "/api/auth/oauth/login",
            json={"provider": "google", "credential": "tok", "accepted_terms": False},
        )
        self.assertEqual(resp.status_code, 400)

    def test_login_provisions_new_user_and_sets_cookie(self) -> None:
        self._patch_verify({"sub": "g-1", "email": "newbie@gmail.com", "email_verified": True})
        resp = self.client.post(
            "/api/auth/oauth/login",
            json={"provider": "google", "credential": "tok", "accepted_terms": True},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["username"], "newbie@gmail.com")
        self.assertTrue(body["can_analyze"])
        self.assertFalse(body["can_trade"])
        self.assertIn(app_auth.COOKIE_NAME, resp.cookies)
        # The provisioned account is discoverable and trial-limited.
        user = app_auth.get_auth_user("newbie@gmail.com")
        self.assertIsNotNone(user)
        self.assertEqual(user.max_daily_ai_chat, 10)

    def test_login_rejects_unverified_email_for_new_user(self) -> None:
        self._patch_verify({"sub": "g-2", "email": "shady@gmail.com", "email_verified": False})
        resp = self.client.post(
            "/api/auth/oauth/login",
            json={"provider": "google", "credential": "tok", "accepted_terms": True},
        )
        self.assertEqual(resp.status_code, 400)

    def test_second_login_same_sub_reuses_account(self) -> None:
        self._patch_verify({"sub": "g-3", "email": "repeat@gmail.com", "email_verified": True})
        first = self.client.post(
            "/api/auth/oauth/login",
            json={"provider": "google", "credential": "tok", "accepted_terms": True},
        )
        self.assertEqual(first.status_code, 200)
        # Even if the email later changes, the stable sub maps to the same user.
        self._patch_verify({"sub": "g-3", "email": "changed@gmail.com", "email_verified": True})
        second = self.client.post(
            "/api/auth/oauth/login",
            json={"provider": "google", "credential": "tok2", "accepted_terms": True},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["username"], "repeat@gmail.com")

    def test_invalid_token_returns_401(self) -> None:
        def boom(provider, credential, nonce=None):
            raise self.web_api.OAuthError("invalid or expired token")

        self.web_api.verify_id_token = boom
        resp = self.client.post(
            "/api/auth/oauth/login",
            json={"provider": "google", "credential": "bad", "accepted_terms": True},
        )
        self.assertEqual(resp.status_code, 401)

    def test_unlink_guard_blocks_last_method(self) -> None:
        # Provision + login an OAuth-only user, then try to unlink their only id.
        self._patch_verify({"sub": "g-4", "email": "solo@gmail.com", "email_verified": True})
        self.client.post(
            "/api/auth/oauth/login",
            json={"provider": "google", "credential": "tok", "accepted_terms": True},
        )
        resp = self.client.delete("/api/auth/oauth/links/google")
        self.assertEqual(resp.status_code, 400)
        # Identity is still present.
        self.assertEqual(oauth_store.count_identities_for_user("solo@gmail.com"), 1)


class OAuthErrorShim(Exception):
    pass


class OAuthErrorShim(Exception):
    pass


class OAuthErrorShim(Exception):
    pass


class OAuthErrorShim(Exception):
    pass


if __name__ == "__main__":
    unittest.main()

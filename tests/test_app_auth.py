from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ai_option_scanner import app_auth, db, observation_store, scan_store


class AppAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_database_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_database_url_alt = os.environ.pop("DATABASE_URL", None)
        self._orig_env: dict[str, str | None] = {}
        for index in range(1, 6):
            for suffix in (
                "USER",
                "PASSWORD",
                "PASSWORD_HASH",
                "CAN_ANALYZE",
                "CAN_TRADE",
                "IS_ADMIN",
                "EXPIRES_AT",
            ):
                key = f"AI_OPTION_AUTH_{suffix}_{index}"
                self._orig_env[key] = os.environ.pop(key, None)
        db.DB_PATH = Path(self._tmpdir.name) / "auth.sqlite3"
        db._INIT_ONCE_DONE.clear()
        app_auth.invalidate_auth_user_cache()
        app_auth.init_auth_db()
        scan_store.init_scan_db()
        observation_store.init_observation_db()

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

    def test_create_user_with_permissions_and_resource_limits(self) -> None:
        user = app_auth.create_auth_user(
            "quota@example.com",
            "password123",
            can_analyze=True,
            can_trade=False,
            resource_limits={
                "max_daily_scans": 8,
                "max_daily_ai_scans": 3,
                "max_watchlists": 2,
                "max_scan_loop_instances": 4,
                "max_notification_channels": 5,
                "max_longbridge_accounts": 1,
            },
        )

        self.assertTrue(user.can_analyze)
        self.assertFalse(user.can_trade)
        self.assertEqual(user.max_daily_scans, 8)
        permissions = app_auth.auth_user_permissions("quota@example.com")
        self.assertTrue(permissions["can_analyze"])
        self.assertFalse(permissions["can_trade"])
        self.assertEqual(permissions["limits"]["max_daily_ai_scans"], 3)

    def test_update_user_permissions_and_limits(self) -> None:
        app_auth.create_auth_user("edit@example.com", "password123", can_analyze=True)

        updated = app_auth.update_auth_user_permissions(
            "edit@example.com",
            can_analyze=False,
            can_trade=True,
            resource_limits={"max_notification_channels": -1},
        )

        self.assertFalse(updated.can_analyze)
        self.assertTrue(updated.can_trade)
        self.assertEqual(updated.max_notification_channels, -1)
        permissions = app_auth.auth_user_permissions("edit@example.com")
        self.assertFalse(permissions["can_analyze"])
        self.assertTrue(permissions["can_trade"])

    def test_user_rows_include_usage(self) -> None:
        app_auth.create_auth_user("usage@example.com", "password123", resource_limits={"max_watchlists": 1})
        observation_store.create_watchlist("usage@example.com", {"name": "Core", "symbols": ["SPY", "QQQ"]})

        row = next(item for item in app_auth.auth_users_as_rows() if item["username"] == "usage@example.com")

        self.assertEqual(row["max_watchlists"], 1)
        self.assertEqual(row["usage"]["watchlists"], 1)


if __name__ == "__main__":
    unittest.main()

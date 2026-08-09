from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ai_option_scanner import db, thetadata_store


class ThetaDataStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = db.DB_PATH
        self._original_env = {
            key: os.environ.pop(key, None)
            for key in (
                "THETADATA_EMAIL",
                "THETADATA_PASSWORD",
                "THETADATA_CREDENTIALS_FILE",
                "AI_OPTION_THETADATA_EMAIL",
                "AI_OPTION_THETADATA_PASSWORD",
                "AI_OPTION_THETADATA_CREDENTIALS_FILE",
            )
        }
        db.DB_PATH = Path(self._tmpdir.name) / "thetadata-store.sqlite3"
        db._INIT_ONCE_DONE.clear()

    def tearDown(self) -> None:
        db.DB_PATH = self._original_db_path
        db._INIT_ONCE_DONE.clear()
        for key, value in self._original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()

    def test_saved_credentials_are_encrypted_and_status_is_masked(self) -> None:
        status = thetadata_store.save_thetadata_credentials("research@example.com", "theta-password")

        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "saved")
        self.assertEqual(status["email_hint"], "r***@example.com")
        resolved = thetadata_store.resolve_thetadata_credentials()
        self.assertEqual(resolved.email, "research@example.com")
        self.assertEqual(resolved.password, "theta-password")

        with db.connect() as connection:
            row = connection.execute(
                "SELECT identity_enc, secret_enc FROM data_source_credentials WHERE provider = ?",
                (thetadata_store.PROVIDER_KEY,),
            ).fetchone()
        self.assertNotIn("research@example.com", str(row["identity_enc"]))
        self.assertNotIn("theta-password", str(row["secret_enc"]))

    def test_environment_credentials_override_saved_credentials(self) -> None:
        thetadata_store.save_thetadata_credentials("saved@example.com", "saved-password")
        os.environ["THETADATA_EMAIL"] = "env@example.com"
        os.environ["THETADATA_PASSWORD"] = "env-password"

        status = thetadata_store.thetadata_config_status()
        resolved = thetadata_store.resolve_thetadata_credentials()

        self.assertEqual(status["source"], "environment")
        self.assertTrue(status["environment_override"])
        self.assertTrue(status["stored_configured"])
        self.assertEqual(resolved.email, "env@example.com")

    def test_delete_returns_to_sdk_default(self) -> None:
        thetadata_store.save_thetadata_credentials("delete@example.com", "delete-password")

        status = thetadata_store.delete_thetadata_credentials()

        self.assertFalse(status["configured"])
        self.assertEqual(status["source"], "sdk_default")
        self.assertFalse(status["stored_configured"])

    def test_rejects_invalid_credentials(self) -> None:
        with self.assertRaises(ValueError):
            thetadata_store.save_thetadata_credentials("not-an-email", "password")
        with self.assertRaises(ValueError):
            thetadata_store.save_thetadata_credentials("valid@example.com", "")


if __name__ == "__main__":
    unittest.main()

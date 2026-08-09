from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_option_scanner import db, scan_jobs, scan_store


class ScanProviderOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_database_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_database_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "scan.sqlite3"
        db._INIT_ONCE_DONE.clear()
        scan_store.init_scan_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_database_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_database_url
        if self._orig_database_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_database_url_alt
        self._tmpdir.cleanup()

    def test_scan_run_persists_distinct_ai_provider_owner(self) -> None:
        row = scan_store.create_scan_run(
            query="QQQ no ai",
            symbol="QQQ",
            ai_provider="user:deepseek-test",
            longbridge_account="yfinance",
            use_ai=True,
            council=False,
            market_data_source="yfinance",
            owner_id="workspace-owner",
            ai_provider_owner="provider-owner",
        )

        self.assertEqual(row["owner_id"], "workspace-owner")
        self.assertEqual(row["ai_provider_owner"], "provider-owner")

        loaded = scan_store.get_scan_run(row["id"], owner_id="workspace-owner")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["ai_provider_owner"], "provider-owner")

    def test_scan_job_passes_provider_owner_to_run_scan(self) -> None:
        row = scan_store.create_scan_run(
            query="QQQ with user provider",
            symbol="QQQ",
            ai_provider="user:deepseek-test",
            longbridge_account="yfinance",
            use_ai=True,
            council=False,
            market_data_source="yfinance",
            owner_id="workspace-owner",
            ai_provider_owner="provider-owner",
        )
        seen: dict[str, object] = {}

        def fake_run_scan(**kwargs):
            seen.update(kwargs)
            return {
                "mode": "single",
                "used_ai": True,
                "answer": "ok",
                "payload": {},
                "charts": {},
            }

        with mock.patch.object(scan_jobs, "run_scan", side_effect=fake_run_scan):
            scan_jobs._run_scan_job(row["id"])

        self.assertEqual(seen["ai_provider_owner"], "provider-owner")
        completed = scan_store.get_scan_run(row["id"], owner_id="workspace-owner")
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(completed["used_ai"])


if __name__ == "__main__":
    unittest.main()

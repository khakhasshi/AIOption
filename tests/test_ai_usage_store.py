from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ai_option_scanner import ai_usage_store, app_auth, db, observation_store, scan_store


class AIUsageStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_database_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_database_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "usage.sqlite3"
        db._INIT_ONCE_DONE.clear()
        app_auth.invalidate_auth_user_cache()
        app_auth.init_auth_db()
        scan_store.init_scan_db()
        observation_store.init_observation_db()
        ai_usage_store.init_ai_usage_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_database_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_database_url
        if self._orig_database_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_database_url_alt
        self._tmpdir.cleanup()

    def test_record_usage_and_summary(self) -> None:
        ai_usage_store.record_ai_usage_event(
            owner_id="alice",
            provider="deepseek",
            model="deepseek-v4-flash",
            usage={
                "prompt_tokens": 1200,
                "prompt_cache_hit_tokens": 200,
                "prompt_cache_miss_tokens": 1000,
                "completion_tokens": 300,
                "total_tokens": 1500,
            },
            context={"source_type": "scan", "scan_id": "scan-1", "symbol": "SPY"},
        )

        summary = ai_usage_store.ai_usage_summary(owner_id="alice", days=30, limit=10)
        self.assertEqual(summary["totals"]["30d"]["calls"], 1)
        self.assertEqual(summary["recent"][0]["scan_id"], "scan-1")
        self.assertGreater(summary["totals"]["30d"]["estimated_cost_cny"], 0)
        self.assertIn("deepseek-v4-flash", summary["price_table"])

    def test_estimate_ai_cost_uses_prices(self) -> None:
        cost = ai_usage_store.estimate_ai_cost(
            provider="deepseek",
            model="deepseek-v4-flash",
            usage={
                "prompt_cache_hit_tokens": 1000,
                "prompt_cache_miss_tokens": 2000,
                "completion_tokens": 3000,
            },
        )

        self.assertGreater(cost["estimated_cost_cny"], 0)
        self.assertGreater(cost["estimated_cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()

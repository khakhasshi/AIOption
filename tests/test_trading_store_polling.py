from __future__ import annotations

import json as _json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_option_scanner import db, trading_store


def _ts(offset_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _insert_run(
    run_id: str,
    *,
    status: str,
    lifecycle_state: str | None,
    protection_state: str | None = None,
    has_orders: bool = True,
    has_instance: bool = True,
    created_at: str | None = None,
    protection_status: dict | None = None,
) -> None:
    orders_json = _json.dumps([{"contract_symbol": "SPY260619C00500000"}]) if has_orders else None
    instance_payload = {"lifecycle_state": lifecycle_state or "created"}
    if protection_status is not None:
        instance_payload["protection_status"] = protection_status
    instance_json = _json.dumps(instance_payload) if has_instance else None
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO trading_runs
                (id, owner_id, status, created_at, started_at, finished_at,
                 stage, progress, config_json, scan_results_json, council_json,
                 selections_json, orders_json, instance_json, instance_version,
                 lifecycle_state, protection_state, instance_updated_at, error)
            VALUES (?, 'tester', ?, ?, NULL, NULL, NULL, 50, '{}', '[]', '{}',
                    '[]', ?, ?, 1, ?, ?, ?, NULL)
            """,
            (
                run_id,
                status,
                created_at or _ts(-60),
                orders_json,
                instance_json,
                lifecycle_state,
                protection_state,
                _ts(-1),
            ),
        )


class TradingStoreLightModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "store.sqlite3"
        db._INIT_ONCE_DONE.clear()
        trading_store.init_trading_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def test_light_mode_omits_heavy_entry_blobs(self) -> None:
        # Stuff scan_results/council/selections with non-trivial payloads.
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO trading_runs
                    (id, owner_id, status, created_at, started_at, finished_at,
                     stage, progress, config_json, scan_results_json, council_json,
                     selections_json, orders_json, instance_json, instance_version,
                     lifecycle_state, protection_state, instance_updated_at, error)
                VALUES ('R1', 'tester', 'running', ?, NULL, NULL, 'monitoring', 80,
                        '{}', ?, ?, ?, '[]', '{}', 1, 'monitoring', 'completed', ?, NULL)
                """,
                (
                    _ts(-60),
                    _json.dumps([{"symbol": "SPY", "candidates": list(range(50))}]),
                    _json.dumps({"advisors": list(range(40))}),
                    _json.dumps(list(range(30))),
                    _ts(-1),
                ),
            )

        full = trading_store.get_trading_run("R1")
        light = trading_store.get_trading_run("R1", light=True)

        self.assertIsNotNone(full)
        self.assertIsNotNone(light)
        # Full mode preserves entry-time payloads.
        self.assertTrue(full["scan_results"])
        self.assertTrue(full["council"])
        self.assertTrue(full["selections"])
        # Light mode skips them but keeps orders/instance/status.
        self.assertIsNone(light["scan_results"])
        self.assertIsNone(light["council"])
        self.assertIsNone(light["selections"])
        self.assertEqual(light["_payload_mode"], "light")
        self.assertEqual(light["status"], "running")
        self.assertEqual(light["progress"], 80)
        # Shape matches \u2014 trade_instance present in both.
        self.assertIn("trade_instance", light)
        self.assertIn("orders", light)


class MonitorableSqlExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "monitorable.sqlite3"
        db._INIT_ONCE_DONE.clear()
        trading_store.init_trading_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def test_failed_runs_with_attention_lifecycle_states_are_included(self) -> None:
        # Regression: previously only failed+monitoring was monitored. These runs
        # would be silently abandoned even though they still need broker attention.
        scenarios = [
            ("R-manual", "manual_intervention_required"),
            ("R-stopfail", "stop_failed"),
            ("R-exiting", "exiting"),
            ("R-unprot", "unprotected"),
            ("R-partial", "partial_fill"),
            ("R-protect", "protected"),
            ("R-open", "open"),
            ("R-monitor", "monitoring"),
        ]
        for run_id, lifecycle in scenarios:
            _insert_run(run_id, status="failed", lifecycle_state=lifecycle)

        monitorable = trading_store.list_monitorable_trading_runs(limit=50)
        ids = {row["id"] for row in monitorable}
        for run_id, _ in scenarios:
            with self.subTest(run_id=run_id):
                self.assertIn(run_id, ids)

    def test_terminal_lifecycles_remain_excluded(self) -> None:
        _insert_run("R-closed", status="succeeded", lifecycle_state="closed")
        _insert_run("R-reviewed", status="succeeded", lifecycle_state="reviewed")
        _insert_run("R-blocked", status="succeeded", lifecycle_state="blocked")
        monitorable = trading_store.list_monitorable_trading_runs(limit=50)
        ids = {row["id"] for row in monitorable}
        self.assertNotIn("R-closed", ids)
        self.assertNotIn("R-reviewed", ids)
        self.assertNotIn("R-blocked", ids)

    def test_risk_snapshot_separates_today_failures_from_manual_attention(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        _insert_run("R-old-blocked", status="failed", lifecycle_state="blocked", has_orders=False, created_at=old)
        _insert_run(
            "R-manual-attention",
            status="failed",
            lifecycle_state="manual_intervention_required",
            protection_state="broker_combo_close_required",
            created_at=old,
            protection_status={
                "state": "broker_combo_close_required",
                "requires_manual_attention": True,
                "stop_failure_reason": "exit order Canceled",
                "unprotected_quantity": 0,
            },
        )
        snapshot = trading_store.trading_readiness_risk_snapshot("tester", recent_limit=10)
        self.assertEqual(snapshot["consecutive_failures"], 0)
        self.assertEqual(snapshot["manual_attention_count"], 1)
        self.assertEqual(snapshot["manual_attention_runs"][0]["id"], "R-manual-attention")

    def test_risk_snapshot_ignores_no_order_gate_blocked_failures(self) -> None:
        _insert_run("R-gate-2", status="failed", lifecycle_state="blocked", has_orders=False, created_at=_ts(-1))
        _insert_run("R-gate-1", status="failed", lifecycle_state="blocked", has_orders=False, created_at=_ts(-2))
        snapshot = trading_store.trading_readiness_risk_snapshot("tester", recent_limit=10)
        self.assertEqual(snapshot["today_run_count"], 0)
        self.assertEqual(snapshot["consecutive_failures"], 0)
        self.assertEqual(snapshot["consecutive_failure_runs"], [])

    def test_risk_snapshot_counts_order_bearing_failures(self) -> None:
        _insert_run("R-order-2", status="failed", lifecycle_state="blocked", has_orders=True, created_at=_ts(-1))
        _insert_run("R-order-1", status="failed", lifecycle_state="blocked", has_orders=True, created_at=_ts(-2))
        snapshot = trading_store.trading_readiness_risk_snapshot("tester", recent_limit=10)
        self.assertEqual(snapshot["today_run_count"], 2)
        self.assertEqual(snapshot["consecutive_failures"], 2)
        self.assertEqual([row["id"] for row in snapshot["consecutive_failure_runs"]], ["R-order-2", "R-order-1"])

    def test_failed_with_unrelated_lifecycle_still_excluded(self) -> None:
        # A failed run in 'created' (never started) state should NOT be monitored.
        _insert_run("R-created", status="failed", lifecycle_state="created", has_orders=False, has_instance=False)
        monitorable = trading_store.list_monitorable_trading_runs(limit=50)
        self.assertNotIn("R-created", {row["id"] for row in monitorable})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from ai_option_scanner.db import connect
from ai_option_scanner.observation_store import init_observation_db, prune_observation_history


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class PruneObservationHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        init_observation_db()
        self.owner = f"owner-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        self.old_ts = _iso(now - timedelta(days=200))
        self.fresh_ts = _iso(now - timedelta(days=1))
        self.old_id = f"old-{uuid.uuid4().hex[:8]}"
        self.fresh_id = f"fresh-{uuid.uuid4().hex[:8]}"
        with connect() as db:
            for row_id, ts in ((self.old_id, self.old_ts), (self.fresh_id, self.fresh_ts)):
                db.execute(
                    "INSERT INTO opportunity_events (id, owner_id, opportunity_id, event_type, title, body, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_id, self.owner, "opp-1", "test", "t", "b", "{}", ts),
                )

    def tearDown(self) -> None:
        with connect() as db:
            db.execute("DELETE FROM opportunity_events WHERE owner_id = ?", (self.owner,))

    def _ids(self) -> set[str]:
        with connect() as db:
            rows = db.execute("SELECT id FROM opportunity_events WHERE owner_id = ?", (self.owner,)).fetchall()
        return {str(r[0] if not isinstance(r, dict) else r["id"]) for r in rows}

    def test_prune_removes_old_keeps_fresh(self) -> None:
        self.assertEqual(self._ids(), {self.old_id, self.fresh_id})
        deleted = prune_observation_history(retention_days=90)
        ids = self._ids()
        self.assertIn(self.fresh_id, ids)
        self.assertNotIn(self.old_id, ids)
        # at least the one old opportunity_events row we inserted was deleted
        self.assertGreaterEqual(deleted.get("opportunity_events", 0), 1)

    def test_retention_window_is_clamped_to_minimum(self) -> None:
        # retention_days below the floor (7) must not delete a 1-day-old row.
        prune_observation_history(retention_days=0)
        self.assertIn(self.fresh_id, self._ids())

    def test_returns_per_table_counts_for_all_targets(self) -> None:
        deleted = prune_observation_history(retention_days=90)
        for table in ("notification_events", "notification_delivery_logs", "opportunity_events", "scan_loop_runs", "scan_loop_run_items"):
            self.assertIn(table, deleted)


if __name__ == "__main__":
    unittest.main()

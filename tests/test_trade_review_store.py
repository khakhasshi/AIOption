from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ai_option_scanner import db, trade_review, trade_review_store, trading_store
from ai_option_scanner.post_mortem_worker import run_post_mortem_worker_once


def _ts(offset_hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).strftime("%Y-%m-%d %H:%M:%S")


def _make_closed_run(run_id: str, owner_id: str = "tester", *, with_positions: bool = True,
                     realized_pnl: float | None = 120.0, holding_minutes: int | None = 75,
                     entry_minutes_ago: int = 120, exit_minutes_ago: int = 30) -> None:
    entry_iso = (datetime.now(timezone.utc) - timedelta(minutes=entry_minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    exit_iso = (datetime.now(timezone.utc) - timedelta(minutes=exit_minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    config = {
        "total_capital": 10000,
        "strategy_modes": ["long_call"],
        "entry_order_type": "MO",
        "ai_provider": "deepseek",
    }
    risk_positions = [
        {
            "contract_symbol": "NVDA260618C00200000",
            "action": "BUY_TO_OPEN",
            "side": "LONG_CALL",
            "strike": 200.0,
            "expiration": "2026-06-18",
            "units": 2,
            "entry_price": 5.40,
            "allocation_pct": 0.4,
            "realized_pnl": realized_pnl,
            "exit_price": 6.00,
        }
    ] if with_positions else []
    instance = {
        "lifecycle_state": "closed",
        "ai_decision": {
            "council_mode": "single",
            "selection_count": 1,
            "selected_contracts": [{"contract_symbol": "NVDA260618C00200000"}],
            "rejected_count": 0,
            "advisor_reports": [
                {"advisor": "trend", "conviction_score": 0.72, "summary": "uptrend confirmed"}
            ],
        },
        "risk_plan": {
            "total_planned_capital": 4000,
            "planned_contracts": 2,
            "planned_premium_at_risk": 1080.0,
            "max_loss_if_all_premiums_lost": 1080.0,
            "positions": risk_positions,
        },
        "review_metrics": {
            "realized_pnl": realized_pnl,
            "estimated_total_pnl": realized_pnl,
            "return_pct": 0.18 if realized_pnl else 0.0,
            "entry_cost": 1080.0,
            "holding_minutes": holding_minutes,
            "first_exit_trigger": "take_profit",
            "win_loss": "win" if (realized_pnl or 0) > 0 else "loss",
        },
        "event_timeline": [
            {"time": entry_iso, "event_type": "entry_submitted", "lifecycle_state": "entering"},
            {"time": exit_iso, "event_type": "exit_filled", "lifecycle_state": "closed"},
        ],
    }
    orders = [
        {
            "contract_symbol": "NVDA260618C00200000",
            "entry_time": entry_iso,
            "exit_filled_at": exit_iso,
            "single_leg_smart_exit_reason": "tp1_hit",
        }
    ]
    import json as _json
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO trading_runs
                (id, owner_id, status, created_at, started_at, finished_at,
                 stage, progress, config_json, scan_results_json, council_json,
                 selections_json, orders_json, instance_json, instance_version,
                 lifecycle_state, protection_state, instance_updated_at, error)
            VALUES (?, ?, 'succeeded', ?, ?, ?, 'closed', 100, ?, '[]', '{}',
                    '[]', ?, ?, 1, 'closed', 'completed', ?, NULL)
            """,
            (
                run_id, owner_id,
                _ts(-3), _ts(-3), _ts(-1),
                _json.dumps(config),
                _json.dumps(orders),
                _json.dumps(instance),
                _ts(-1),
            ),
        )


class TradeReviewStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "trade_reviews.sqlite3"
        db._INIT_ONCE_DONE.clear()
        trading_store.init_trading_db()
        trade_review_store.init_trade_review_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def test_init_is_idempotent(self) -> None:
        trade_review_store.init_trade_review_db()
        trade_review_store.init_trade_review_db()  # second call is a no-op

    def test_upsert_dedupes_by_run_id(self) -> None:
        first = trade_review_store.upsert_pending_review(
            run_id="run-A", owner_id="tester", locator_id="TRD-1",
            lifecycle_state="closed", facts={"hello": "world"},
            realized_pnl=120.0, holding_minutes=75,
        )
        second = trade_review_store.upsert_pending_review(
            run_id="run-A", owner_id="tester", locator_id="TRD-1",
            lifecycle_state="closed", facts={"new": "payload"},
        )
        self.assertEqual(first["run_id"], second["run_id"])
        # second call must not overwrite facts
        self.assertEqual(second["facts"], {"hello": "world"})
        listed = trade_review_store.list_pending_review_run_ids()
        self.assertEqual(listed, ["run-A"])

    def test_state_transitions(self) -> None:
        trade_review_store.upsert_pending_review(
            run_id="run-B", owner_id="tester", locator_id="TRD-2",
            lifecycle_state="closed", facts={"k": "v"},
        )
        trade_review_store.mark_review_processing("run-B")
        row = trade_review_store.get_trade_review("run-B")
        self.assertEqual(row["review_status"], "processing")
        self.assertEqual(row["attempts"], 1)

        trade_review_store.mark_review_completed(
            "run-B",
            review={"verdict": "win", "score": 80, "summary": "ok"},
            ai_provider="deepseek",
            ai_model="deepseek-chat",
        )
        row = trade_review_store.get_trade_review("run-B")
        self.assertEqual(row["review_status"], "completed")
        self.assertEqual(row["review"]["score"], 80)
        self.assertEqual(row["ai_provider"], "deepseek")
        # completed rows are no longer in pending list
        self.assertEqual(trade_review_store.list_pending_review_run_ids(), [])

    def test_failed_then_retry(self) -> None:
        trade_review_store.upsert_pending_review(
            run_id="run-C", owner_id="tester", locator_id="TRD-3",
            lifecycle_state="closed", facts={"k": "v"},
        )
        trade_review_store.mark_review_processing("run-C")
        trade_review_store.mark_review_failed("run-C", "boom")
        row = trade_review_store.get_trade_review("run-C")
        self.assertEqual(row["review_status"], "failed")
        self.assertEqual(row["review_error"], "boom")
        # failed status is re-eligible for pending pickup
        self.assertEqual(trade_review_store.list_pending_review_run_ids(), ["run-C"])
        # but once attempts >= max_attempts it stops appearing
        self.assertEqual(
            trade_review_store.list_pending_review_run_ids(max_attempts=1),
            [],
        )

    def test_list_unreviewed_closed_run_ids_excludes_existing_reviews(self) -> None:
        _make_closed_run("run-D", "tester")
        ids = trade_review_store.list_unreviewed_closed_run_ids()
        self.assertEqual(ids, ["run-D"])
        trade_review_store.upsert_pending_review(
            run_id="run-D", owner_id="tester", locator_id=None,
            lifecycle_state="closed", facts={},
        )
        self.assertEqual(trade_review_store.list_unreviewed_closed_run_ids(), [])


class TradeReviewLogicTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "trade_reviews.sqlite3"
        db._INIT_ONCE_DONE.clear()
        trading_store.init_trading_db()
        trade_review_store.init_trade_review_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def test_build_facts_includes_decision_and_metrics(self) -> None:
        run = {
            "id": "run-E",
            "owner_id": "tester",
            "locator_id": "TRD-E",
            "status": "succeeded",
            "created_at": "2026-06-01T13:30:00Z",
            "finished_at": "2026-06-01T15:00:00Z",
            "config": {
                "strategy_modes": ["long_call"],
                "entry_order_type": "MO",
                "ai_provider": "deepseek",
                "total_capital": 10000,
                "default_stop_loss_pct": 0.4,
                "default_take_profit_pct": 0.6,
            },
            "orders": [
                {
                    "contract_symbol": "NVDA260618C00200000",
                    "entry_time": "2026-06-01T13:31:00Z",
                    "exit_filled_at": "2026-06-01T14:46:00Z",
                    "single_leg_smart_exit_reason": "tp1_hit",
                }
            ],
            "trade_instance": {
                "lifecycle_state": "closed",
                "ai_decision": {
                    "council_mode": "single",
                    "selection_count": 2,
                    "rejected_count": 1,
                    "selected_contracts": [{"contract_symbol": "NVDA260618C00200000"}],
                    "advisor_reports": [
                        {"advisor": "trend", "conviction_score": 0.72, "summary": "uptrend"}
                    ],
                },
                "risk_plan": {
                    "total_planned_capital": 4000,
                    "planned_contracts": 2,
                    "planned_premium_at_risk": 1080.0,
                    "max_loss_if_all_premiums_lost": 1080.0,
                    "positions": [
                        {"contract_symbol": "NVDA260618C00200000", "action": "BUY_TO_OPEN",
                         "units": 2, "entry_price": 5.4, "allocation_pct": 0.4}
                    ],
                },
                "review_metrics": {
                    "realized_pnl": 250.0,
                    "estimated_total_pnl": 250.0,
                    "return_pct": 0.23,
                    "entry_cost": 1080.0,
                    "holding_minutes": 75,
                    "first_exit_trigger": "take_profit",
                    "win_loss": "win",
                },
                "event_timeline": [
                    {"time": "2026-06-01T13:31:00Z", "event_type": "entry", "lifecycle_state": "entering"},
                ],
            },
        }
        facts = trade_review.build_facts_from_run(run)
        self.assertEqual(facts["run_id"], "run-E")
        self.assertEqual(facts["ai_decision"]["selection_count"], 2)
        self.assertEqual(facts["ai_decision"]["rejected_count"], 1)
        self.assertEqual(facts["metrics"]["realized_pnl"], 250.0)
        self.assertEqual(facts["metrics"]["exit_reason"], "tp1_hit")
        self.assertEqual(facts["metrics"]["holding_minutes"], 75)
        self.assertEqual(len(facts["risk_plan"]["positions"]), 1)
        self.assertEqual(facts["config"]["ai_provider"], "deepseek")
        self.assertIsNone(trade_review.should_skip_review(facts))

    def test_should_skip_review_no_positions(self) -> None:
        facts = {"risk_plan": {"positions": []}, "metrics": {}, "event_timeline": []}
        self.assertEqual(trade_review.should_skip_review(facts), "no_positions_submitted")

    def test_should_skip_review_below_thresholds(self) -> None:
        facts = {
            "risk_plan": {"positions": [{"contract_symbol": "x"}]},
            "metrics": {"realized_pnl": 10.0, "holding_minutes": 5},
            "event_timeline": [{"event_type": "entry"}],
        }
        self.assertEqual(
            trade_review.should_skip_review(facts),
            "below_pnl_and_holding_thresholds",
        )

    def test_should_not_skip_when_large_pnl(self) -> None:
        facts = {
            "risk_plan": {"positions": [{"contract_symbol": "x"}]},
            "metrics": {"realized_pnl": 250.0, "holding_minutes": 5},
            "event_timeline": [{"event_type": "entry"}],
        }
        self.assertIsNone(trade_review.should_skip_review(facts))


class PostMortemWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "trade_reviews.sqlite3"
        db._INIT_ONCE_DONE.clear()
        trading_store.init_trading_db()
        trade_review_store.init_trade_review_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def test_worker_processes_eligible_run_and_skips_trivial_one(self) -> None:
        # "big" has a 90-minute holding window → eligible
        _make_closed_run("run-big", "tester", entry_minutes_ago=120, exit_minutes_ago=30)
        # "small" has a 2-minute holding window AND no order-derived pnl → skipped
        _make_closed_run("run-small", "tester", entry_minutes_ago=10, exit_minutes_ago=8)
        fake_review = {
            "verdict": "win",
            "score": 82,
            "summary": "TP1 hit cleanly",
            "what_went_right": ["entry timing"],
            "what_went_wrong": [],
            "lessons": ["scale out earlier"],
            "suggested_changes": ["tighten stop after TP1"],
        }
        with mock.patch(
            "ai_option_scanner.post_mortem_worker.generate_review",
            return_value=(fake_review, None),
        ) as gen_mock:
            result = run_post_mortem_worker_once()
        self.assertEqual(result["discovered"], 2)
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(gen_mock.call_count, 1)
        big = trade_review_store.get_trade_review("run-big")
        small = trade_review_store.get_trade_review("run-small")
        self.assertEqual(big["review_status"], "completed")
        self.assertEqual(big["review"]["score"], 82)
        self.assertEqual(small["review_status"], "skipped")

    def test_worker_marks_failed_when_ai_returns_error(self) -> None:
        _make_closed_run("run-fail", "tester", entry_minutes_ago=120, exit_minutes_ago=20)
        with mock.patch(
            "ai_option_scanner.post_mortem_worker.generate_review",
            return_value=(None, "ai_returned_empty"),
        ):
            result = run_post_mortem_worker_once()
        self.assertEqual(result["failed"], 1)
        row = trade_review_store.get_trade_review("run-fail")
        self.assertEqual(row["review_status"], "failed")
        self.assertEqual(row["review_error"], "ai_returned_empty")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

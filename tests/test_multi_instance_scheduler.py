from __future__ import annotations

import uuid
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ai_option_scanner import trading_agent, trading_scheduler
from ai_option_scanner.db import connect
from ai_option_scanner.trading_instance import create_trade_instance
from ai_option_scanner.trading_agent import evaluate_schedule_slot_gate, trading_run_entry_blockers
from ai_option_scanner.trading_store import (
    claim_schedule_slot,
    create_trading_run,
    get_or_create_schedule_session,
    get_schedule_session,
    list_schedule_fires,
    mark_schedule_slot_fired,
    mark_trading_run,
    normalize_trading_config,
    recover_stale_schedule_slots,
    schedule_config_hash,
    schedule_runtime_snapshot,
    skip_schedule_slot,
)


class MultiInstanceSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_now_et = trading_scheduler.now_et
        self._orig_redis_available = trading_scheduler.redis_available
        self._orig_list_enabled_configs = trading_scheduler.list_enabled_configs
        self._orig_start_scheduled_trading_slot = trading_scheduler.start_scheduled_trading_slot
        self._orig_start_trading_run = trading_scheduler.start_trading_run
        self._orig_set_last_run_date = trading_scheduler.set_last_run_date
        self._orig_resolve_account = trading_agent.resolve_account
        self._orig_flatten_trade_instance = trading_agent.flatten_trade_instance

    def tearDown(self) -> None:
        trading_scheduler.now_et = self._orig_now_et
        trading_scheduler.redis_available = self._orig_redis_available
        trading_scheduler.list_enabled_configs = self._orig_list_enabled_configs
        trading_scheduler.start_scheduled_trading_slot = self._orig_start_scheduled_trading_slot
        trading_scheduler.start_trading_run = self._orig_start_trading_run
        trading_scheduler.set_last_run_date = self._orig_set_last_run_date
        trading_agent.resolve_account = self._orig_resolve_account
        trading_agent.flatten_trade_instance = self._orig_flatten_trade_instance

    def test_config_normalizes_multi_instance_slots(self) -> None:
        config = normalize_trading_config(
            {
                "multi_instance_enabled": True,
                "schedule_profile": "Balanced Multi Slot",
                "schedule_slots": [
                    {
                        "slot_id": "Midday Structure",
                        "time_et": "12:45",
                        "strategy_modes": ["calendar", "iron_condor"],
                        "capital_pct": 0.35,
                        "force_no_overnight": True,
                    }
                ],
            }
        )

        self.assertTrue(config["multi_instance_enabled"])
        self.assertEqual(config["schedule_profile"], "balanced_multi_slot")
        self.assertEqual(config["schedule_slots"][0]["slot_id"], "midday_structure")
        self.assertEqual(config["schedule_slots"][0]["strategy_modes"], ["calendar", "iron_condor"])
        self.assertTrue(config["schedule_slots"][0]["force_no_overnight"])

    def test_multi_instance_defaults_manual_single_instance_off(self) -> None:
        multi_config = normalize_trading_config({"multi_instance_enabled": True})
        self.assertFalse(multi_config["single_instance_enabled"])
        self.assertIn("单实例创建开关已关闭", "; ".join(trading_run_entry_blockers(multi_config, trigger_source="manual")))

        explicit_single = normalize_trading_config({"multi_instance_enabled": True, "single_instance_enabled": True})
        self.assertTrue(explicit_single["single_instance_enabled"])
        self.assertEqual(trading_run_entry_blockers(explicit_single, trigger_source="manual"), [])

        slot_config = normalize_trading_config(
            {
                "multi_instance_enabled": True,
                "single_instance_enabled": False,
                "schedule_slot_id": "midday_structure",
            }
        )
        self.assertEqual(trading_run_entry_blockers(slot_config, trigger_source="scheduler:balanced_multi_slot:midday_structure"), [])

    def test_scheduler_triggers_due_slots_without_single_run_dedupe(self) -> None:
        fired: list[dict] = []
        config = normalize_trading_config(
            {
                "owner_id": "owner-1",
                "live_enabled": True,
                "multi_instance_enabled": True,
                "schedule_profile": "balanced_multi_slot",
                "schedule_slots": [
                    {"slot_id": "open", "time_et": "09:45", "strategy_modes": ["single_leg"], "enabled": True},
                    {"slot_id": "later", "time_et": "15:45", "strategy_modes": ["calendar"], "enabled": True},
                ],
            }
        )
        config["owner_id"] = "owner-1"
        config["last_run_date_et"] = "2026-05-12"
        trading_scheduler.now_et = lambda: datetime(2026, 5, 12, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        trading_scheduler.redis_available = lambda: False
        trading_scheduler.list_enabled_configs = lambda: [config]
        trading_scheduler.start_trading_run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("single-run scheduler should not fire"))
        trading_scheduler.set_last_run_date = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("multi-instance should use slot ledger"))

        def start_slot(owner_id, cfg, *, trade_date_et, profile_id, slot):
            fired.append({"owner_id": owner_id, "trade_date_et": trade_date_et, "profile_id": profile_id, "slot_id": slot["slot_id"]})
            return {"id": f"run-{slot['slot_id']}"}

        trading_scheduler.start_scheduled_trading_slot = start_slot
        trading_scheduler._tick()

        self.assertEqual(fired, [{"owner_id": "owner-1", "trade_date_et": "2026-05-12", "profile_id": "balanced_multi_slot", "slot_id": "open"}])

    def test_one_failing_config_does_not_skip_later_configs(self) -> None:
        # A config whose start_trading_run raises (e.g. TradingRunBlockedError on a
        # failed readiness gate) must not abort the tick and skip configs ordered
        # after it. Both single-instance configs are due at 10:00.
        bad = normalize_trading_config({"owner_id": "owner-bad", "single_instance_enabled": True, "run_time_et": "09:30"})
        bad["owner_id"] = "owner-bad"
        good = normalize_trading_config({"owner_id": "owner-good", "single_instance_enabled": True, "run_time_et": "09:30"})
        good["owner_id"] = "owner-good"

        triggered: list[str] = []
        recorded: list[str] = []

        def fake_start(owner_id, cfg, *, trigger_source="manual"):
            if owner_id == "owner-bad":
                raise trading_agent.TradingRunBlockedError("readiness failed")
            triggered.append(owner_id)
            return {"id": f"run-{owner_id}"}

        trading_scheduler.now_et = lambda: datetime(2026, 5, 12, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        trading_scheduler.redis_available = lambda: False
        trading_scheduler.list_enabled_configs = lambda: [bad, good]
        trading_scheduler.start_trading_run = fake_start
        trading_scheduler.set_last_run_date = lambda owner_id, today: recorded.append(owner_id)

        trading_scheduler._tick()

        # The good config still fired despite the bad one raising first.
        self.assertEqual(triggered, ["owner-good"])
        # The blocked config did NOT record a run-date (so it can retry later);
        # only the successfully-submitted one did.
        self.assertEqual(recorded, ["owner-good"])

    def test_parent_session_uses_enabled_slot_count_and_tracks_state(self) -> None:
        owner_id = f"owner-{uuid.uuid4().hex[:8]}"
        trade_date_et = "2099-05-12"
        profile_id = f"profile-{uuid.uuid4().hex[:8]}"
        config = normalize_trading_config(
            {
                "live_enabled": True,
                "multi_instance_enabled": True,
                "schedule_profile": profile_id,
                "total_capital": 1000,
                "schedule_slots": [
                    {"slot_id": "open", "time_et": "09:45", "strategy_modes": ["single_leg"], "capital_pct": 0.4, "gate_profile": "strict_momentum"},
                    {"slot_id": "mid", "time_et": "12:45", "strategy_modes": ["calendar"], "capital_pct": 0.7, "gate_profile": "structure_specific"},
                    {"slot_id": "exit", "time_et": "15:10", "action": "reduce_or_exit", "allow_new_positions": False, "capital_pct": 0.1, "gate_profile": "no_overnight"},
                    {"slot_id": "disabled", "time_et": "15:30", "strategy_modes": ["single_leg"], "capital_pct": 0.1, "enabled": False},
                ],
            }
        )

        session = get_or_create_schedule_session(owner_id, trade_date_et, profile_id, config)
        self.assertEqual(session["slot_count"], 3)
        self.assertEqual(session["remaining_capital"], 1000.0)

        open_slot = config["schedule_slots"][0]
        open_gate = evaluate_schedule_slot_gate(config, open_slot, session)
        self.assertTrue(open_gate["allowed"])
        self.assertEqual(open_gate["allocated_capital"], 400.0)
        self.assertTrue(
            claim_schedule_slot(
                owner_id,
                trade_date_et,
                profile_id,
                "open",
                "09:45",
                session_id=session["session_id"],
                action="scan_open",
                gate_profile="strict_momentum",
                allocated_capital=open_gate["allocated_capital"],
                gate_result=open_gate,
            )
        )

        session = get_schedule_session(owner_id, trade_date_et, profile_id)
        self.assertIsNotNone(session)
        self.assertEqual(session["allocated_capital"], 400.0)
        self.assertEqual(session["remaining_capital"], 600.0)
        self.assertEqual(session["fired_count"], 0)
        self.assertEqual(session["skipped_count"], 0)
        self.assertEqual(session["status"], "open")
        mark_schedule_slot_fired(owner_id, trade_date_et, profile_id, "open", run_id="run-open", status="fired")

        blocked_gate = evaluate_schedule_slot_gate(config, config["schedule_slots"][1], session)
        self.assertFalse(blocked_gate["allowed"])
        self.assertIn("exceeds remaining capital", "; ".join(blocked_gate["blockers"]))
        self.assertTrue(
            skip_schedule_slot(
                owner_id,
                trade_date_et,
                profile_id,
                "mid",
                "12:45",
                session_id=session["session_id"],
                action="open_or_adjust",
                gate_profile="structure_specific",
                gate_result=blocked_gate,
                reason="blocked by test",
            )
        )

        exit_gate = evaluate_schedule_slot_gate(config, config["schedule_slots"][2], session)
        self.assertTrue(exit_gate["allowed"])
        self.assertFalse(exit_gate["skip"])
        self.assertTrue(
            claim_schedule_slot(
                owner_id,
                trade_date_et,
                profile_id,
                "exit",
                "15:10",
                session_id=session["session_id"],
                action="reduce_or_exit",
                gate_profile="no_overnight",
                allocated_capital=exit_gate["allocated_capital"],
                gate_result=exit_gate,
            )
        )
        mark_schedule_slot_fired(owner_id, trade_date_et, profile_id, "exit", run_id="scheduled-exit", status="fired")

        fires = list_schedule_fires(owner_id=owner_id, trade_date_et=trade_date_et, limit=10)
        self.assertEqual({item["status"] for item in fires}, {"fired", "skipped"})

        session = get_schedule_session(owner_id, trade_date_et, profile_id)
        self.assertIsNotNone(session)
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["fired_count"], 2)
        self.assertEqual(session["skipped_count"], 1)
        self.assertEqual(session["failed_count"], 0)

    def test_exit_slot_flattens_active_instances_without_new_scan(self) -> None:
        owner_id = f"owner-{uuid.uuid4().hex[:8]}"
        trade_date_et = "2099-05-18"
        profile_id = f"profile-{uuid.uuid4().hex[:8]}"
        config = normalize_trading_config(
            {
                "owner_id": owner_id,
                "live_enabled": True,
                "longbridge_account": "paper",
                "multi_instance_enabled": True,
                "schedule_profile": profile_id,
                "total_capital": 1000,
                "schedule_slots": [
                    {
                        "slot_id": "exit",
                        "time_et": "15:10",
                        "action": "reduce_or_exit",
                        "allow_new_positions": False,
                        "force_no_overnight": True,
                        "capital_pct": 0.1,
                        "gate_profile": "no_overnight",
                    }
                ],
            }
        )
        run = create_trading_run(owner_id, {**config, "schedule_slot_id": "open"})
        orders = [
            {
                "symbol": "AAPL",
                "order_symbol": "AAPL  990115C00100000",
                "quantity": 2,
                "entry_filled_quantity": 2,
                "status": "submitted",
            }
        ]
        mark_trading_run(run["id"], status="succeeded", orders_json=orders, instance_json=run["trade_instance"])
        flattened: list[str] = []
        trading_agent.resolve_account = lambda *args, **kwargs: SimpleNamespace(name="paper")
        trading_agent.flatten_trade_instance = lambda run_id, owner, account_name: flattened.append(run_id) or {
            "run_id": run_id,
            "status": "ok",
            "submitted_count": 1,
            "strategy_submitted_count": 0,
            "failed_count": 0,
            "strategy_failed_count": 0,
        }

        result = trading_agent.start_scheduled_trading_slot(
            owner_id,
            config,
            trade_date_et=trade_date_et,
            profile_id=profile_id,
            slot=config["schedule_slots"][0],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(flattened, [run["id"]])
        fires = list_schedule_fires(owner_id=owner_id, trade_date_et=trade_date_et, limit=5)
        self.assertEqual(fires[0]["status"], "fired")
        self.assertEqual(fires[0]["run_id"], f"scheduled-exit:{profile_id}:exit")

    def test_parent_session_id_differs_by_profile(self) -> None:
        owner_id = f"owner-{uuid.uuid4().hex[:8]}"
        trade_date_et = "2099-05-12"
        base_config = normalize_trading_config(
            {
                "multi_instance_enabled": True,
                "total_capital": 1000,
                "schedule_slots": [{"slot_id": "open", "time_et": "09:45", "strategy_modes": ["single_leg"], "capital_pct": 0.4}],
            }
        )
        session_a = get_or_create_schedule_session(owner_id, trade_date_et, "profile-a", {**base_config, "schedule_profile": "profile-a"})
        session_b = get_or_create_schedule_session(owner_id, trade_date_et, "profile-b", {**base_config, "schedule_profile": "profile-b"})

        self.assertNotEqual(session_a["session_id"], session_b["session_id"])

    def test_trade_instance_carries_schedule_session_metadata(self) -> None:
        instance = create_trade_instance(
            "run-test",
            "owner-x",
            {
                "multi_instance_enabled": True,
                "schedule_profile": "profile-x",
                "schedule_session_id": "SES-123",
                "schedule_slot_id": "open",
                "schedule_slot_label": "开盘确认",
                "schedule_slot_time_et": "09:45",
                "schedule_slot_action": "scan_open",
                "schedule_slot_gate_profile": "strict_momentum",
                "schedule_slot_allow_new_positions": True,
                "schedule_slot_force_no_overnight": False,
                "trade_date_et": "2099-05-12",
                "strategy_modes": ["single_leg"],
            },
        )

        self.assertEqual(instance["basic_info"]["schedule_session_id"], "SES-123")
        self.assertEqual(instance["basic_info"]["schedule_slot_action"], "scan_open")
        self.assertEqual(instance["basic_info"]["schedule_slot_gate_profile"], "strict_momentum")
        self.assertEqual(instance["schedule_context"]["session_id"], "SES-123")
        self.assertEqual(instance["schedule_context"]["slot_action"], "scan_open")
        self.assertEqual(instance["schedule_context"]["gate_profile"], "strict_momentum")

    def test_schedule_config_hash_changes_when_rules_change(self) -> None:
        base = normalize_trading_config({"multi_instance_enabled": True, "schedule_slots": [{"slot_id": "open", "time_et": "09:45", "strategy_modes": ["single_leg"], "capital_pct": 0.4}]})
        changed = normalize_trading_config({"multi_instance_enabled": True, "schedule_slots": [{"slot_id": "open", "time_et": "09:45", "strategy_modes": ["calendar"], "capital_pct": 0.4}]})
        self.assertNotEqual(schedule_config_hash(base), schedule_config_hash(changed))

    def test_stale_claimed_slot_can_recover_and_replay(self) -> None:
        owner_id = f"owner-{uuid.uuid4().hex[:8]}"
        trade_date_et = "2099-05-14"
        profile_id = f"profile-{uuid.uuid4().hex[:8]}"
        config = normalize_trading_config(
            {
                "multi_instance_enabled": True,
                "schedule_profile": profile_id,
                "total_capital": 1000,
                "schedule_slots": [{"slot_id": "open", "time_et": "09:45", "strategy_modes": ["single_leg"], "capital_pct": 0.4}],
            }
        )
        session = get_or_create_schedule_session(owner_id, trade_date_et, profile_id, config)
        gate = evaluate_schedule_slot_gate(config, config["schedule_slots"][0], session)
        self.assertTrue(
            claim_schedule_slot(
                owner_id,
                trade_date_et,
                profile_id,
                "open",
                "09:45",
                session_id=session["session_id"],
                action="scan_open",
                gate_profile="strict_momentum",
                allocated_capital=gate["allocated_capital"],
                gate_result=gate,
            )
        )
        with connect() as db:
            db.execute(
                """
                UPDATE trading_schedule_fires
                SET claimed_at = ?, status = 'claimed'
                WHERE owner_id = ? AND trade_date_et = ? AND profile_id = ? AND slot_id = ?
                """,
                ("2020-01-01T00:00:00+00:00", owner_id, trade_date_et, profile_id, "open"),
            )
        recovered = recover_stale_schedule_slots(owner_id, trade_date_et, profile_id, stale_after_minutes=30)
        self.assertEqual(recovered, 1)
        fires = list_schedule_fires(owner_id=owner_id, trade_date_et=trade_date_et, limit=10)
        self.assertEqual(fires[0]["status"], "retrying")
        self.assertEqual(fires[0]["retry_count"], 0)
        self.assertTrue(
            claim_schedule_slot(
                owner_id,
                trade_date_et,
                profile_id,
                "open",
                "09:45",
                session_id=session["session_id"],
                action="scan_open",
                gate_profile="strict_momentum",
                allocated_capital=gate["allocated_capital"],
                gate_result=gate,
            )
        )
        fires = list_schedule_fires(owner_id=owner_id, trade_date_et=trade_date_et, limit=10)
        self.assertEqual(fires[0]["status"], "claimed")
        self.assertEqual(fires[0]["retry_count"], 1)

    def test_session_degrades_on_config_drift(self) -> None:
        owner_id = f"owner-{uuid.uuid4().hex[:8]}"
        trade_date_et = "2099-05-15"
        profile_id = f"profile-{uuid.uuid4().hex[:8]}"
        base = normalize_trading_config({"multi_instance_enabled": True, "schedule_profile": profile_id, "schedule_slots": [{"slot_id": "open", "time_et": "09:45", "strategy_modes": ["single_leg"], "capital_pct": 0.4}]})
        drifted = normalize_trading_config({"multi_instance_enabled": True, "schedule_profile": profile_id, "schedule_slots": [{"slot_id": "open", "time_et": "10:15", "strategy_modes": ["single_leg"], "capital_pct": 0.4}]})
        session = get_or_create_schedule_session(owner_id, trade_date_et, profile_id, base)
        self.assertEqual(session["status"], "open")
        session = get_or_create_schedule_session(owner_id, trade_date_et, profile_id, drifted)
        self.assertEqual(session["status"], "degraded")
        self.assertIn("config_drift", session["context"])

    def test_schedule_runtime_snapshot_exposes_latency_and_curve(self) -> None:
        owner_id = f"owner-{uuid.uuid4().hex[:8]}"
        trade_date_et = "2099-05-16"
        profile_id = f"profile-{uuid.uuid4().hex[:8]}"
        config = normalize_trading_config({"multi_instance_enabled": True, "schedule_profile": profile_id, "total_capital": 1000, "schedule_slots": [{"slot_id": "open", "time_et": "09:45", "strategy_modes": ["single_leg"], "capital_pct": 0.4}]})
        session = get_or_create_schedule_session(owner_id, trade_date_et, profile_id, config)
        gate = evaluate_schedule_slot_gate(config, config["schedule_slots"][0], session)
        claim_schedule_slot(owner_id, trade_date_et, profile_id, "open", "09:45", session_id=session["session_id"], action="scan_open", gate_profile="strict_momentum", allocated_capital=gate["allocated_capital"], gate_result=gate)
        mark_schedule_slot_fired(owner_id, trade_date_et, profile_id, "open", run_id="run-open", status="fired")
        snapshot = schedule_runtime_snapshot()
        self.assertIn("claimed_to_fired_latency", snapshot)
        self.assertTrue(snapshot["claimed_to_fired_latency"]["sample_size"] >= 1)
        self.assertTrue(any(item["session_id"] == session["session_id"] for item in snapshot["remaining_capital_curve"]))


if __name__ == "__main__":
    unittest.main()

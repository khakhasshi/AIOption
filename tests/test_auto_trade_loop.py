from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from ai_option_scanner import auto_trade_loop, auto_trade_store, db
from ai_option_scanner.auto_trade_config import build_run_config, intraday_phase, preset_caps, preset_trading_fields

ET = ZoneInfo("America/New_York")


def _env(session_state: str, *, now: datetime, open_h: int = 9, open_m: int = 30, close_h: int = 16) -> dict:
    """Minimal market_environment stub shaped like market_calendar.market_environment."""
    day = now.date()
    open_at = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_at = now.replace(hour=close_h, minute=0, second=0, microsecond=0)
    next_open = open_at + timedelta(days=1)
    return {
        "session_state": session_state,
        "now_et": now.isoformat(),
        "date_et": day.isoformat(),
        "regular_open_at_et": open_at.isoformat(),
        "regular_close_at_et": close_at.isoformat(),
        "next_regular_open_at_et": next_open.isoformat(),
    }


class IntradayPhaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = datetime(2026, 6, 15, 9, 30, tzinfo=ET)

    def _phase_at(self, hour: int, minute: int) -> str:
        now = self.base.replace(hour=hour, minute=minute)
        return intraday_phase(_env("regular_open", now=now), now=now)

    def test_just_opened_first_30m(self) -> None:
        self.assertEqual(self._phase_at(9, 31), "just_opened")
        self.assertEqual(self._phase_at(9, 59), "just_opened")

    def test_morning(self) -> None:
        self.assertEqual(self._phase_at(10, 30), "morning")

    def test_lunch(self) -> None:
        self.assertEqual(self._phase_at(12, 0), "lunch")

    def test_afternoon(self) -> None:
        self.assertEqual(self._phase_at(14, 0), "afternoon")

    def test_power_hour_last_60m(self) -> None:
        self.assertEqual(self._phase_at(15, 5), "power_hour")

    def test_pre_close_last_15m(self) -> None:
        self.assertEqual(self._phase_at(15, 50), "pre_close")

    def test_premarket_and_afterhours(self) -> None:
        now = self.base.replace(hour=8, minute=0)
        self.assertEqual(intraday_phase(_env("premarket", now=now), now=now), "premarket")
        now2 = self.base.replace(hour=17, minute=0)
        self.assertEqual(intraday_phase(_env("afterhours", now=now2), now=now2), "afterhours")


class PresetTest(unittest.TestCase):
    def test_caps_increase_with_aggressiveness(self) -> None:
        cons = preset_caps("conservative")
        agg = preset_caps("aggressive")
        self.assertLess(cons["max_order_cycles_per_session"], agg["max_order_cycles_per_session"])
        self.assertLessEqual(cons["max_open_positions"], agg["max_open_positions"])

    def test_unknown_preset_falls_back_to_conservative(self) -> None:
        self.assertEqual(preset_caps("nonsense"), preset_caps("conservative"))

    def test_build_run_config_dry_run_when_no_broker(self) -> None:
        instance = {"risk_preset": "conservative", "use_broker": False, "symbols": ["SPY"], "config": {}}
        cfg = build_run_config(instance)
        self.assertFalse(cfg["live_enabled"])
        self.assertEqual(cfg["universe"], ["SPY"])
        self.assertTrue(cfg["use_ai"])

    def test_build_run_config_enables_intelligent_mode(self) -> None:
        instance = {"risk_preset": "balanced", "use_broker": False, "symbols": ["SPY"], "total_capital": 5000, "config": {}}
        cfg = build_run_config(instance)
        # LLM controls sizing + stops + take-profit.
        self.assertTrue(cfg["ai_adjust_allocation"])
        self.assertTrue(cfg["ai_adjust_stop_loss"])
        self.assertTrue(cfg["ai_adjust_take_profit"])
        # Pre-close flatten safety net forced on.
        self.assertTrue(cfg["force_no_overnight"])
        # total_capital is the session budget = total * session_capital_budget_pct.
        self.assertAlmostEqual(cfg["total_capital"], 5000 * 0.50, places=2)
        # Per-trade allocation clamp present.
        self.assertGreater(cfg["max_allocation_pct_per_trade"], 0)

    def test_build_run_config_zero_capital_safe(self) -> None:
        instance = {"risk_preset": "conservative", "use_broker": False, "symbols": ["SPY"], "total_capital": 0, "config": {}}
        cfg = build_run_config(instance)
        self.assertEqual(cfg["total_capital"], 0.0)

    def test_preset_max_allocation_ordered(self) -> None:
        self.assertLess(preset_caps("conservative")["max_allocation_pct_per_trade"],
                        preset_caps("aggressive")["max_allocation_pct_per_trade"])

    def test_build_run_config_live_when_broker(self) -> None:
        instance = {"risk_preset": "aggressive", "use_broker": True, "symbols": ["TSLA"], "config": {}}
        cfg = build_run_config(instance)
        self.assertTrue(cfg["live_enabled"])
        self.assertEqual(cfg["entry_order_type"], "market")

    def test_user_config_overrides_preset(self) -> None:
        instance = {"risk_preset": "conservative", "use_broker": False, "symbols": ["SPY"], "config": {"top_n": 5}}
        self.assertEqual(build_run_config(instance)["top_n"], 5)


class AutoTradeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "auto_trade.sqlite3"
        db._INIT_ONCE_DONE.clear()
        auto_trade_store.init_auto_trade_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def _make(self, **over) -> dict:
        payload = {"name": "T", "symbols": ["SPY", "QQQ"], "interval_minutes": 5, "risk_preset": "conservative"}
        payload.update(over)
        return auto_trade_store.create_auto_trade_instance("owner-a", payload)

    def test_create_caps_symbols_at_8(self) -> None:
        inst = self._make(symbols=[f"S{i}" for i in range(12)])
        self.assertEqual(len(inst["symbols"]), 8)

    def test_create_normalizes_invalid_preset(self) -> None:
        inst = self._make(risk_preset="wild")
        self.assertEqual(inst["risk_preset"], "conservative")

    def test_total_capital_round_trip_and_default(self) -> None:
        inst = self._make(total_capital=7500)
        self.assertEqual(inst["total_capital"], 7500.0)
        # Default when omitted.
        inst2 = self._make()
        self.assertEqual(inst2["total_capital"], 3000.0)
        # Update path clamps negatives to 0.
        updated = auto_trade_store.update_auto_trade_instance(inst["id"], "owner-a", {"total_capital": -5})
        self.assertEqual(updated["total_capital"], 0.0)

    def test_owner_isolation(self) -> None:
        inst = self._make()
        self.assertIsNone(auto_trade_store.get_auto_trade_instance(inst["id"], "owner-b"))
        self.assertIsNotNone(auto_trade_store.get_auto_trade_instance(inst["id"], "owner-a"))

    def test_due_listing_respects_status_and_next_run(self) -> None:
        inst = self._make()
        # stopped → never due
        self.assertEqual(auto_trade_store.list_due_auto_trade_instances(), [])
        auto_trade_store.update_auto_trade_instance(inst["id"], "owner-a", {"status": "active", "next_run_at": None})
        due = auto_trade_store.list_due_auto_trade_instances()
        self.assertEqual([d["id"] for d in due], [inst["id"]])
        # future next_run_at → not due
        future = (datetime.now().astimezone() + timedelta(hours=2)).isoformat()
        auto_trade_store.update_auto_trade_instance(inst["id"], "owner-a", {"next_run_at": future})
        self.assertEqual(auto_trade_store.list_due_auto_trade_instances(), [])

    def test_due_listing_compares_timezone_aware_next_run_at(self) -> None:
        inst = self._make()
        auto_trade_store.update_auto_trade_instance(inst["id"], "owner-a", {
            "status": "active",
            "next_run_at": datetime(2026, 6, 15, 10, 12, tzinfo=ET).isoformat(),
        })
        before = datetime(2026, 6, 15, 14, 9, tzinfo=timezone.utc).isoformat()
        after = datetime(2026, 6, 15, 14, 13, tzinfo=timezone.utc).isoformat()
        self.assertEqual(auto_trade_store.list_due_auto_trade_instances(now=before), [])
        due = auto_trade_store.list_due_auto_trade_instances(now=after)
        self.assertEqual([d["id"] for d in due], [inst["id"]])

    def test_cycle_persist_and_fetch(self) -> None:
        inst = self._make()
        cid = auto_trade_store.insert_auto_trade_cycle(
            inst["id"], "owner-a", 1, session_state="regular_open", intraday_phase="morning", dry_run=True
        )
        auto_trade_store.finish_auto_trade_cycle(cid, status="completed", run_ids=["RUN1"], summary={"orders": 1})
        cycles = auto_trade_store.list_auto_trade_cycles(inst["id"], "owner-a")
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["status"], "completed")
        self.assertEqual(cycles[0]["run_ids"], ["RUN1"])
        full = auto_trade_store.get_auto_trade_cycle(cid, "owner-a")
        self.assertEqual(full["summary"]["orders"], 1)


class CycleEngineTest(unittest.TestCase):
    """Faked end-to-end cycle: no broker, no live LLM — patch the trading run."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "auto_trade.sqlite3"
        db._INIT_ONCE_DONE.clear()
        auto_trade_store.init_auto_trade_db()
        self.instance = auto_trade_store.create_auto_trade_instance(
            "owner-a", {"name": "T", "symbols": ["SPY"], "interval_minutes": 5, "risk_preset": "conservative"}
        )

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def _refresh(self) -> dict:
        return auto_trade_store.get_auto_trade_instance(self.instance["id"], "owner-a")

    def test_skips_outside_session(self) -> None:
        now = datetime(2026, 6, 15, 8, 0, tzinfo=ET)
        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("premarket", now=now)):
            summary = auto_trade_loop.run_auto_trade_cycle(self.instance)
        self.assertEqual(summary.get("skipped"), "outside_session")
        # next_run_at advanced so we don't hot-loop.
        self.assertIsNotNone(self._refresh()["next_run_at"])

    def test_kill_switch_disables(self) -> None:
        with mock.patch.dict(os.environ, {"AI_OPTION_AUTO_TRADE_ENABLED": "false"}):
            summary = auto_trade_loop.run_auto_trade_cycle(self.instance)
        self.assertEqual(summary.get("skipped"), "auto_trade_disabled")

    def test_full_cycle_records_row_and_memory(self) -> None:
        now = datetime(2026, 6, 15, 10, 30, tzinfo=ET)
        fake_run = {"id": "RUN-XYZ"}
        fake_detail = {"id": "RUN-XYZ", "status": "completed", "stage": "done",
                       "orders": [{"order_id": "O1", "entry_order": {}}]}
        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("regular_open", now=now)), \
             mock.patch.object(auto_trade_loop, "get_trading_run", return_value=fake_detail), \
             mock.patch("ai_option_scanner.trading_agent.create_auto_trade_run_and_execute", return_value=fake_run):
            summary = auto_trade_loop.run_auto_trade_cycle(self.instance)

        self.assertEqual(summary["cycle_index"], 1)
        self.assertEqual(summary["intraday_phase"], "morning")
        self.assertEqual(summary["orders"], 1)
        # A cycle row was persisted and linked to the run.
        cycles = auto_trade_store.list_auto_trade_cycles(self.instance["id"], "owner-a")
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["status"], "completed")
        # Memory rolled forward so cycle N+1 will see cycle N.
        refreshed = self._refresh()
        self.assertEqual(len(refreshed["memory"]), 1)
        self.assertEqual(refreshed["memory"][0]["cycle_index"], 1)
        self.assertEqual(refreshed["cycles_today"], 1)
        # Dry-run (no broker) "orders" never count against the real-order cap.
        self.assertEqual(refreshed["orders_today"], 0)

    def test_next_run_at_persisted_before_slow_cycle(self) -> None:
        # The advanced next_run_at must be persisted BEFORE create_auto_trade_run_
        # and_execute runs, so a peer node that ticks mid-cycle (leader lock TTL
        # can lapse) sees this instance as not-due and won't double-submit. We
        # assert the DB already shows the future next_run_at while still inside
        # the (mocked) slow execute call.
        now = datetime(2026, 6, 15, 10, 30, tzinfo=ET)
        seen: dict = {}

        def _capture(owner_id, config, trigger_source=None):
            inst = auto_trade_store.get_auto_trade_instance(self.instance["id"], "owner-a")
            seen["next_run_at"] = inst.get("next_run_at")
            return {"id": "RUN-CLAIM"}

        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("regular_open", now=now)), \
             mock.patch.object(auto_trade_loop, "get_trading_run", return_value={"id": "RUN-CLAIM", "status": "completed", "orders": []}), \
             mock.patch("ai_option_scanner.trading_agent.create_auto_trade_run_and_execute", side_effect=_capture):
            auto_trade_loop.run_auto_trade_cycle(self.instance)

        self.assertIsNotNone(seen.get("next_run_at"))
        claimed = datetime.fromisoformat(seen["next_run_at"])
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=timezone.utc)
        self.assertGreater(claimed, now.astimezone(timezone.utc))

    def test_order_cap_forces_dry_run(self) -> None:
        # Pre-load the instance at the conservative cap so this cycle can't place.
        cap = preset_caps("conservative")["max_order_cycles_per_session"]
        now = datetime(2026, 6, 15, 10, 30, tzinfo=ET)
        today = now.date().isoformat()
        auto_trade_store.update_auto_trade_instance(
            self.instance["id"], "owner-a",
            {"orders_today": cap, "cycles_today": cap, "session_date_et": today},
        )
        instance = self._refresh()
        captured = {}

        def _capture(owner_id, config, trigger_source="auto"):
            captured["dry_run"] = config.get("dry_run")
            captured["live_enabled"] = config.get("live_enabled")
            return {"id": "RUN-CAP"}

        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("regular_open", now=now)), \
             mock.patch.object(auto_trade_loop, "get_trading_run", return_value={"id": "RUN-CAP", "status": "completed", "orders": []}), \
             mock.patch("ai_option_scanner.trading_agent.create_auto_trade_run_and_execute", side_effect=_capture):
            summary = auto_trade_loop.run_auto_trade_cycle(instance)

        self.assertTrue(summary["order_cap_reached"])
        # Cap reached → forced analysis-only this cycle.
        self.assertTrue(captured["dry_run"])
        self.assertFalse(captured["live_enabled"])

    def test_live_broker_cycle_increments_orders_today(self) -> None:
        # A broker-backed instance: a placed order DOES count toward the cap.
        live = auto_trade_store.create_auto_trade_instance(
            "owner-a", {"name": "L", "symbols": ["SPY"], "interval_minutes": 5,
                        "risk_preset": "conservative", "use_broker": True, "broker_account": "acct-1"}
        )
        now = datetime(2026, 6, 15, 10, 30, tzinfo=ET)
        fake_detail = {"id": "RUN-LIVE", "status": "completed", "orders": [{"order_id": "O1"}]}
        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("regular_open", now=now)), \
             mock.patch.object(auto_trade_loop, "get_trading_run", return_value=fake_detail), \
             mock.patch("ai_option_scanner.trading_agent.create_auto_trade_run_and_execute", return_value={"id": "RUN-LIVE"}):
            auto_trade_loop.run_auto_trade_cycle(live)
        refreshed = auto_trade_store.get_auto_trade_instance(live["id"], "owner-a")
        self.assertEqual(refreshed["orders_today"], 1)

    def test_cycle_passes_intelligent_config(self) -> None:
        # The run config handed to the engine must carry capital + ai_adjust flags
        # + the session directive, so the LLM can size and set smart exits.
        capital_instance = auto_trade_store.create_auto_trade_instance(
            "owner-a", {"name": "C", "symbols": ["SPY"], "risk_preset": "balanced", "total_capital": 8000}
        )
        now = datetime(2026, 6, 15, 10, 30, tzinfo=ET)
        captured = {}

        def _capture(owner_id, config, trigger_source="auto"):
            captured.update(config)
            return {"id": "RUN-CFG"}

        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("regular_open", now=now)), \
             mock.patch.object(auto_trade_loop, "get_trading_run", return_value={"id": "RUN-CFG", "status": "completed", "orders": []}), \
             mock.patch("ai_option_scanner.trading_agent.create_auto_trade_run_and_execute", side_effect=_capture):
            auto_trade_loop.run_auto_trade_cycle(capital_instance)

        self.assertTrue(captured["ai_adjust_allocation"])
        self.assertTrue(captured["ai_adjust_stop_loss"])
        self.assertTrue(captured["force_no_overnight"])
        self.assertAlmostEqual(captured["total_capital"], 8000 * 0.50, places=2)
        self.assertIn("decision_directive", captured)
        self.assertIn("智能退出", captured["decision_directive"])
        # Robustness: deterministic fallback stop pct still present.
        self.assertGreater(captured["default_stop_loss_pct"], 0)

    def test_memory_block_carries_prior_cycle(self) -> None:
        instance = dict(self.instance)
        instance["memory"] = [{"cycle_index": 3, "intraday_phase": "morning", "finished_at": "2026-06-15T14:00:00",
                               "thesis": "SPY calls on breakout", "orders": 1}]
        block = auto_trade_loop._memory_block(instance)
        self.assertIn("第3次扫描", block)
        self.assertIn("SPY calls on breakout", block)


class NormalizeConfigTest(unittest.TestCase):
    """The new intelligent-mode fields must survive normalize_trading_config
    (which whitelists keys against DEFAULT_TRADING_CONFIG)."""

    def test_new_fields_preserved(self) -> None:
        from ai_option_scanner.trading_store import normalize_trading_config
        cfg = normalize_trading_config({
            "force_no_overnight": True,
            "max_allocation_pct_per_trade": 0.35,
            "decision_directive": "test directive",
        })
        self.assertTrue(cfg["force_no_overnight"])
        self.assertAlmostEqual(cfg["max_allocation_pct_per_trade"], 0.35)
        self.assertEqual(cfg["decision_directive"], "test directive")

    def test_allocation_cap_clamped(self) -> None:
        from ai_option_scanner.trading_store import normalize_trading_config
        self.assertEqual(normalize_trading_config({"max_allocation_pct_per_trade": 5})["max_allocation_pct_per_trade"], 1.0)
        self.assertEqual(normalize_trading_config({"max_allocation_pct_per_trade": -1})["max_allocation_pct_per_trade"], 0.0)

    def test_defaults_off_for_manual(self) -> None:
        from ai_option_scanner.trading_store import normalize_trading_config
        cfg = normalize_trading_config({})
        self.assertFalse(cfg["force_no_overnight"])
        self.assertEqual(cfg["max_allocation_pct_per_trade"], 0.0)

    def test_delayed_market_sources_migrate_to_thetadata(self) -> None:
        from ai_option_scanner.trading_store import normalize_trading_config

        self.assertEqual(normalize_trading_config({"market_data_source": "yfinance"})["market_data_source"], "thetadata")
        self.assertEqual(normalize_trading_config({"market_data_source": "auto"})["market_data_source"], "thetadata")
        self.assertEqual(normalize_trading_config({"market_data_source": "longbridge"})["market_data_source"], "longbridge")


class PresetBreakerThresholdTest(unittest.TestCase):
    def test_loss_and_drawdown_caps_present_and_ordered(self) -> None:
        cons = preset_caps("conservative")
        bal = preset_caps("balanced")
        agg = preset_caps("aggressive")
        for caps in (cons, bal, agg):
            self.assertIn("max_daily_loss_pct", caps)
            self.assertIn("max_drawdown_pct", caps)
            self.assertGreater(caps["max_daily_loss_pct"], 0)
        # Conservative tightest, aggressive loosest.
        self.assertLess(cons["max_daily_loss_pct"], agg["max_daily_loss_pct"])
        self.assertLess(cons["max_drawdown_pct"], agg["max_drawdown_pct"])
        self.assertLessEqual(cons["max_daily_loss_pct"], bal["max_daily_loss_pct"])


class DirectiveClauseTest(unittest.TestCase):
    """The soft Tier 1/2 clauses appear when data is present and are omitted
    fail-soft when absent."""

    def _directive(self, **signals) -> str:
        return auto_trade_loop._build_decision_directive(
            {"config": {}}, {"now_et": "2026-06-15T10:30"}, "morning",
            preset_caps("conservative"), {"total_capital": 900.0}, **signals,
        )

    def test_base_directive_has_no_optional_clauses(self) -> None:
        text = self._directive()
        self.assertIn("智能退出", text)
        self.assertNotIn("历史战绩", text)
        self.assertNotIn("组合敞口", text)
        self.assertNotIn("市场环境", text)

    def test_track_record_clause_present(self) -> None:
        text = self._directive(track_record={
            "sample_size": 12, "win_rate": 58.0, "avg_confidence_vs_return": -9.0,
            "recent_lessons": ["追高被套", "止损太宽"],
        })
        self.assertIn("历史战绩", text)
        self.assertIn("胜率 58.0%", text)
        self.assertIn("过度自信", text)  # negative calibration tone
        self.assertIn("追高被套", text)

    def test_track_record_empty_sample_omitted(self) -> None:
        self.assertNotIn("历史战绩", self._directive(track_record={"sample_size": 0}))

    def test_portfolio_clause_present(self) -> None:
        text = self._directive(portfolio={
            "available": True, "open_positions": 3, "net_delta": 1.8, "gross_delta": 2.4,
            "top_symbols": [{"symbol": "NVDA", "net_delta": 1.2}],
        })
        self.assertIn("组合敞口", text)
        self.assertIn("NVDA", text)
        self.assertIn("偏多头", text)

    def test_portfolio_unavailable_omitted(self) -> None:
        self.assertNotIn("组合敞口", self._directive(portfolio={"available": False}))

    def test_macro_clause_present(self) -> None:
        text = self._directive(macro={
            "vix": {"available": True, "vix": 31.2, "regime": "stressed", "rising": True},
            "earnings_soon": [{"symbol": "TSLA", "days": 2}],
        })
        self.assertIn("市场环境", text)
        self.assertIn("VIX 31.2", text)
        self.assertIn("TSLA", text)

    def test_macro_empty_omitted(self) -> None:
        self.assertNotIn("市场环境", self._directive(macro={"vix": {"available": False}}))


class DailyPnlTest(unittest.TestCase):
    def test_aggregates_realized_and_floating(self) -> None:
        from ai_option_scanner import auto_trade_signals as sig
        cycles = [{"run_ids": ["R1"]}, {"run_ids": ["R2", "R2"]}, {"run_ids": ["R3"]}]
        reviews = {"R1": {"realized_pnl": -120.0, "return_pct": -12.0}}
        open_runs = {"R2": {"trade_instance": {"review_metrics": {"estimated_total_pnl": 40.0}}}}

        with mock.patch.object(sig, "_num", wraps=sig._num), \
             mock.patch("ai_option_scanner.trade_review_store.get_trade_review",
                        side_effect=lambda rid, owner=None: reviews.get(rid)), \
             mock.patch("ai_option_scanner.trading_store.get_trading_run",
                        side_effect=lambda rid, owner=None, light=False: open_runs.get(rid)):
            result = sig.daily_pnl_for_instance("owner-a", cycles, "2026-06-15")

        # R1 realized -120, R2 floating +40, R3 unknown → skipped. Net -80.
        self.assertAlmostEqual(result["realized"], -120.0)
        self.assertAlmostEqual(result["floating"], 40.0)
        self.assertAlmostEqual(result["total"], -80.0)
        self.assertEqual(result["run_count"], 3)

    def test_empty_cycles_zero(self) -> None:
        from ai_option_scanner import auto_trade_signals as sig
        result = sig.daily_pnl_for_instance("owner-a", [], "2026-06-15")
        self.assertEqual(result["total"], 0.0)
        self.assertEqual(result["run_count"], 0)


class PortfolioExposureTest(unittest.TestCase):
    def test_net_delta_signs_by_direction(self) -> None:
        from ai_option_scanner import auto_trade_signals as sig
        runs = [{
            "owner_id": "owner-a",
            "orders": [
                {"symbol": "SPY", "side": "buy", "entry_filled_quantity": 2, "candidate": {"delta": 0.5}},
                {"symbol": "SPY", "side": "sell", "entry_filled_quantity": 1, "candidate": {"delta": 0.4}},
                {"symbol": "QQQ", "side": "buy", "entry_filled_quantity": 1, "candidate": {"delta": 0.3}},
            ],
        }]
        with mock.patch("ai_option_scanner.trading_store.list_monitorable_trading_runs", return_value=runs):
            result = sig.portfolio_exposure("owner-a")
        self.assertTrue(result["available"])
        self.assertEqual(result["open_positions"], 3)
        # net = 0.5*2 - 0.4*1 + 0.3*1 = 1.0 - 0.4 + 0.3 = 0.9
        self.assertAlmostEqual(result["net_delta"], 0.9)
        self.assertEqual(result["long_count"], 2)
        self.assertEqual(result["short_count"], 1)

    def test_no_positions_unavailable(self) -> None:
        from ai_option_scanner import auto_trade_signals as sig
        with mock.patch("ai_option_scanner.trading_store.list_monitorable_trading_runs", return_value=[]):
            result = sig.portfolio_exposure("owner-a")
        self.assertFalse(result["available"])

    def test_other_owner_excluded(self) -> None:
        from ai_option_scanner import auto_trade_signals as sig
        runs = [{"owner_id": "owner-b", "orders": [{"symbol": "SPY", "side": "buy", "candidate": {"delta": 0.5}}]}]
        with mock.patch("ai_option_scanner.trading_store.list_monitorable_trading_runs", return_value=runs):
            result = sig.portfolio_exposure("owner-a")
        self.assertFalse(result["available"])


class LossBreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = db.DB_PATH
        self._orig_db_url = os.environ.pop("AI_OPTION_DATABASE_URL", None)
        self._orig_db_url_alt = os.environ.pop("DATABASE_URL", None)
        db.DB_PATH = Path(self._tmpdir.name) / "auto_trade.sqlite3"
        db._INIT_ONCE_DONE.clear()
        auto_trade_store.init_auto_trade_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._orig_db_path
        db._INIT_ONCE_DONE.clear()
        if self._orig_db_url is not None:
            os.environ["AI_OPTION_DATABASE_URL"] = self._orig_db_url
        if self._orig_db_url_alt is not None:
            os.environ["DATABASE_URL"] = self._orig_db_url_alt
        self._tmpdir.cleanup()

    def test_daily_loss_breaker_forces_dry_run(self) -> None:
        # total_capital 1000, conservative max_daily_loss_pct 0.06 → limit -60.
        instance = auto_trade_store.create_auto_trade_instance(
            "owner-a", {"name": "B", "symbols": ["SPY"], "risk_preset": "conservative",
                        "use_broker": True, "broker_account": "acct-1", "total_capital": 1000},
        )
        now = datetime(2026, 6, 15, 10, 30, tzinfo=ET)
        captured = {}

        def _capture(owner_id, config, trigger_source="auto"):
            captured["dry_run"] = config.get("dry_run")
            captured["live_enabled"] = config.get("live_enabled")
            return {"id": "RUN-BRK"}

        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("regular_open", now=now)), \
             mock.patch.object(auto_trade_loop, "daily_pnl_for_instance",
                               return_value={"total": -250.0, "realized": -250.0, "floating": 0.0, "run_count": 1, "sample": []}), \
             mock.patch.object(auto_trade_loop, "track_record_summary", return_value={}), \
             mock.patch.object(auto_trade_loop, "portfolio_exposure", return_value={"available": False}), \
             mock.patch.object(auto_trade_loop, "macro_regime", return_value={"vix": {"available": False}}), \
             mock.patch.object(auto_trade_loop, "get_trading_run", return_value={"id": "RUN-BRK", "status": "completed", "orders": []}), \
             mock.patch("ai_option_scanner.trading_agent.create_auto_trade_run_and_execute", side_effect=_capture):
            summary = auto_trade_loop.run_auto_trade_cycle(instance)

        self.assertTrue(summary["loss_halt"])
        self.assertIn("halted_reason", summary)
        # Loss breaker → forced analysis-only even though use_broker is True.
        self.assertTrue(captured["dry_run"])
        self.assertFalse(captured["live_enabled"])
        # Persisted on the instance for the UI.
        refreshed = auto_trade_store.get_auto_trade_instance(instance["id"], "owner-a")
        self.assertIsNotNone(refreshed["halted_reason"])
        self.assertAlmostEqual(refreshed["realized_pnl_today"], -250.0)

    def test_within_limit_does_not_halt(self) -> None:
        instance = auto_trade_store.create_auto_trade_instance(
            "owner-a", {"name": "B", "symbols": ["SPY"], "risk_preset": "conservative",
                        "use_broker": True, "broker_account": "acct-1", "total_capital": 1000},
        )
        now = datetime(2026, 6, 15, 10, 30, tzinfo=ET)
        with mock.patch.object(auto_trade_loop, "market_environment", return_value=_env("regular_open", now=now)), \
             mock.patch.object(auto_trade_loop, "daily_pnl_for_instance",
                               return_value={"total": -10.0, "realized": -10.0, "floating": 0.0, "run_count": 1, "sample": []}), \
             mock.patch.object(auto_trade_loop, "track_record_summary", return_value={}), \
             mock.patch.object(auto_trade_loop, "portfolio_exposure", return_value={"available": False}), \
             mock.patch.object(auto_trade_loop, "macro_regime", return_value={"vix": {"available": False}}), \
             mock.patch.object(auto_trade_loop, "get_trading_run", return_value={"id": "RUN-OK", "status": "completed", "orders": []}), \
             mock.patch("ai_option_scanner.trading_agent.create_auto_trade_run_and_execute", return_value={"id": "RUN-OK"}):
            summary = auto_trade_loop.run_auto_trade_cycle(instance)
        self.assertFalse(summary["loss_halt"])


if __name__ == "__main__":
    unittest.main()

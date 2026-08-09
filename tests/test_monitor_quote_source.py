from __future__ import annotations

import unittest
import os
import sys
import types
from datetime import datetime, timedelta, timezone

from ai_option_scanner import trading_monitor
from ai_option_scanner.trading_instance import build_protection_status, lifecycle_from_orders


class MonitorQuoteSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._yf_module_name = "ai_option_scanner.yfinance_option_tool"
        self._theta_module_name = "ai_option_scanner.thetadata_option_tool"
        self._orig_yf_module = sys.modules.get(self._yf_module_name)
        self._orig_theta_module = sys.modules.get(self._theta_module_name)
        self.yf_module = types.ModuleType(self._yf_module_name)
        self.theta_module = types.ModuleType(self._theta_module_name)
        sys.modules[self._yf_module_name] = self.yf_module
        sys.modules[self._theta_module_name] = self.theta_module
        self._orig_lb_option_quote = trading_monitor.longbridge_sdk_option_quote
        self._orig_lb_quote = trading_monitor.quote
        self.lb_option_calls = 0
        self.lb_underlying_calls = 0
        trading_monitor._cycle_quote_cache.clear()

    def tearDown(self) -> None:
        if self._orig_yf_module is None:
            sys.modules.pop(self._yf_module_name, None)
        else:
            sys.modules[self._yf_module_name] = self._orig_yf_module
        if self._orig_theta_module is None:
            sys.modules.pop(self._theta_module_name, None)
        else:
            sys.modules[self._theta_module_name] = self._orig_theta_module
        trading_monitor.longbridge_sdk_option_quote = self._orig_lb_option_quote
        trading_monitor.quote = self._orig_lb_quote

    def test_option_monitor_defaults_to_thetadata_first(self) -> None:
        self.theta_module.quote_option_contract = lambda symbol: {
            "available": True,
            "bid": 1.4,
            "ask": 1.5,
            "pricing_source": "thetadata_test",
        }
        self.yf_module.quote_option_contract = lambda symbol: {
            "available": True,
            "bid": 1.2,
            "ask": 1.3,
            "pricing_source": "yfinance_test",
        }

        def lb_quote(symbol, account_name):
            self.lb_option_calls += 1
            return {"available": True, "bid": 9.0}

        trading_monitor.longbridge_sdk_option_quote = lb_quote
        row = trading_monitor._monitor_option_quote("SPY260619C00500000", "paper")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "thetadata")
        self.assertEqual(self.lb_option_calls, 0)

    def test_option_monitor_uses_thetadata_first_when_selected(self) -> None:
        self.theta_module.quote_option_contract = lambda symbol: {
            "available": True,
            "bid": 1.4,
            "ask": 1.5,
            "pricing_source": "thetadata_test",
        }
        self.yf_module.quote_option_contract = lambda symbol: {
            "available": True,
            "bid": 9.0,
            "ask": 9.2,
            "pricing_source": "yfinance_test",
        }

        row = trading_monitor._monitor_option_quote("SPY260619C00500000", "paper", "thetadata")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "thetadata")
        self.assertEqual(row["provider_source"], "thetadata_test")
        self.assertEqual(row["exit_price"], 1.4)

    def test_option_monitor_carries_greeks_for_smart_exit_rules(self) -> None:
        self.theta_module.quote_option_contract = lambda symbol: {
            "available": True,
            "bid": 1.0,
            "ask": 1.2,
            "mid": 1.1,
            "delta": 0.22,
            "theta_per_day": -0.08,
            "implied_volatility": 0.31,
        }

        row = trading_monitor._monitor_option_quote("SPY260619C00500000", "paper", "thetadata")

        self.assertEqual(row["delta"], 0.22)
        self.assertEqual(row["theta"], -0.08)
        self.assertEqual(row["iv"], 0.31)

    def test_option_monitor_falls_back_from_thetadata_to_longbridge_before_yfinance(self) -> None:
        self.theta_module.quote_option_contract = lambda symbol: {
            "available": False,
            "error": "theta unavailable",
        }
        self.yf_module.quote_option_contract = lambda symbol: self.fail("yfinance must be the final protection fallback")

        def lb_quote(symbol, account_name):
            self.lb_option_calls += 1
            return {"available": True, "bid": 1.1, "ask": 1.2, "pricing_source": "longbridge_test"}

        trading_monitor.longbridge_sdk_option_quote = lb_quote
        row = trading_monitor._monitor_option_quote("SPY260619C00500000", "paper", "thetadata")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "longbridge_sdk")
        self.assertEqual(row["fallback_from"], "thetadata")
        self.assertEqual(self.lb_option_calls, 1)

    def test_option_monitor_falls_back_to_longbridge_when_thetadata_and_yfinance_fail(self) -> None:
        self.theta_module.quote_option_contract = lambda symbol: {
            "available": False,
            "error": "theta down",
        }
        self.yf_module.quote_option_contract = lambda symbol: {
            "available": False,
            "error": "yf down",
        }

        def lb_quote(symbol, account_name):
            self.lb_option_calls += 1
            return {"available": True, "bid": 1.1, "ask": 1.2, "pricing_source": "longbridge_test"}

        trading_monitor.longbridge_sdk_option_quote = lb_quote
        row = trading_monitor._monitor_option_quote("SPY260619C00500000", "paper")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "longbridge_sdk")
        self.assertEqual(row["fallback_from"], "thetadata")
        self.assertEqual(self.lb_option_calls, 1)

    def test_underlying_monitor_defaults_to_thetadata(self) -> None:
        self.theta_module.market_data = lambda symbol, daily_count=5: {
            "quote": {"last": 502.25, "source": "thetadata_test"}
        }
        self.yf_module.market_data = lambda symbol, daily_count=5: {
            "quote": {"last": 501.25, "source": "yfinance_test"}
        }

        def lb_quote(symbol, account_name):
            self.lb_underlying_calls += 1
            return {"last": 999.0}

        trading_monitor.quote = lb_quote
        row = trading_monitor._monitor_underlying_quote("SPY", "paper")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "thetadata")
        self.assertEqual(row["price"], 502.25)
        self.assertEqual(self.lb_underlying_calls, 0)

    def test_underlying_monitor_uses_thetadata_when_selected(self) -> None:
        self.theta_module.market_data = lambda symbol, daily_count=5: {
            "quote": {"last": 502.25, "source": "thetadata_test"}
        }
        self.yf_module.market_data = lambda symbol, daily_count=5: {
            "quote": {"last": 999.0, "source": "yfinance_test"}
        }

        row = trading_monitor._monitor_underlying_quote("SPY", "paper", "thetadata")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "thetadata")
        self.assertEqual(row["price"], 502.25)

    def test_underlying_monitor_falls_back_to_longbridge_when_thetadata_and_yfinance_fail(self) -> None:
        self.theta_module.market_data = lambda symbol, daily_count=5: {"quote": {}}
        self.yf_module.market_data = lambda symbol, daily_count=5: {"quote": {}}

        def lb_quote(symbol, account_name):
            self.lb_underlying_calls += 1
            return {"last": 501.5}

        trading_monitor.quote = lb_quote
        row = trading_monitor._monitor_underlying_quote("SPY", "paper")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "longbridge_sdk")
        self.assertEqual(row["fallback_from"], "thetadata")
        self.assertEqual(self.lb_underlying_calls, 1)


class SoftwareProtectionRobustnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_quote = trading_monitor._software_stop_quote
        self._orig_submit_market_order = trading_monitor.submit_market_order
        self._orig_cancel_order = trading_monitor.cancel_order
        self._orig_order_detail = trading_monitor.order_detail
        self._orig_submit_stop_sell_order = trading_monitor.submit_stop_sell_order
        self._orig_strategy_position_mark = trading_monitor._strategy_position_mark
        self._orig_monitor_option_quote = trading_monitor._monitor_option_quote
        self._orig_monitor_underlying_quote = trading_monitor._monitor_underlying_quote
        self._orig_list_trading_runs = trading_monitor.list_trading_runs
        self._orig_mark_trading_run = trading_monitor.mark_trading_run
        self._orig_redis_available = trading_monitor.redis_available
        self._orig_redis_setnx = trading_monitor.redis_setnx
        self._orig_redis_del = trading_monitor.redis_del
        self._orig_wait_for_order_fill = trading_monitor.wait_for_order_fill
        self._orig_account_ref_for_config = trading_monitor.account_ref_for_config
        trading_monitor.wait_for_order_fill = lambda order_id, account_name, timeout_seconds=3: {
            "status": "submitted",
            "executed_quantity": 0,
        }
        # These tests assert single-cycle stop firing; pin the debounce off so a
        # single breach fires immediately. Debounce is covered by its own tests.
        self._orig_sw_stop_confirm = os.environ.get("AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES")
        os.environ["AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES"] = "1"

    def tearDown(self) -> None:
        trading_monitor._software_stop_quote = self._orig_quote
        trading_monitor.submit_market_order = self._orig_submit_market_order
        trading_monitor.cancel_order = self._orig_cancel_order
        trading_monitor.order_detail = self._orig_order_detail
        trading_monitor.submit_stop_sell_order = self._orig_submit_stop_sell_order
        trading_monitor._strategy_position_mark = self._orig_strategy_position_mark
        trading_monitor._monitor_option_quote = self._orig_monitor_option_quote
        trading_monitor._monitor_underlying_quote = self._orig_monitor_underlying_quote
        trading_monitor.list_trading_runs = self._orig_list_trading_runs
        trading_monitor.mark_trading_run = self._orig_mark_trading_run
        trading_monitor.redis_available = self._orig_redis_available
        trading_monitor.redis_setnx = self._orig_redis_setnx
        trading_monitor.redis_del = self._orig_redis_del
        trading_monitor.wait_for_order_fill = self._orig_wait_for_order_fill
        trading_monitor.account_ref_for_config = self._orig_account_ref_for_config
        if self._orig_sw_stop_confirm is None:
            os.environ.pop("AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES", None)
        else:
            os.environ["AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES"] = self._orig_sw_stop_confirm

    def _base_order(self) -> dict:
        return {
            "status": "protected",
            "symbol": "SPY",
            "order_symbol": "SPY260619C00500000.US",
            "contract_symbol": "SPY260619C00500000",
            "quantity": 2,
            "entry_filled_quantity": 2,
            "stop_trigger_price": 1.0,
            "software_stop_active": True,
            "software_stop_quantity": 2,
            "software_take_profit_active": True,
            "software_take_profit_quantity": 2,
            "software_take_profit_targets": [{"name": "tp1", "price": 0.9, "quantity": 1}],
        }

    def test_software_stop_debounces_single_bad_tick_then_fires_on_confirm(self) -> None:
        os.environ["AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES"] = "2"
        order = self._base_order()
        submitted: list[dict] = []
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 0.4}
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: (
            submitted.append({"symbol": symbol}) or {"order_id": "close-stop"}
        )

        # Cycle 1: breach observed but below confirm threshold — no order.
        first = trading_monitor._try_software_stop(order, "paper")
        self.assertEqual(first["triggered"], 0)
        self.assertEqual(order["software_stop_status"], "breach_pending_confirm")
        self.assertEqual(order["software_stop_breach_cycles"], 1)
        self.assertTrue(order["software_stop_active"])
        self.assertEqual(len(submitted), 0)

        # Cycle 2: breach persists — fire.
        second = trading_monitor._try_software_stop(order, "paper")
        self.assertEqual(second["triggered"], 1)
        self.assertEqual(order["status"], "software_stop_submitted")
        self.assertEqual(len(submitted), 1)

    def test_software_stop_single_bad_tick_recovers_and_resets_breach_count(self) -> None:
        os.environ["AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES"] = "2"
        order = self._base_order()
        submitted: list[dict] = []
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: (
            submitted.append({"symbol": symbol}) or {"order_id": "close-stop"}
        )

        # Cycle 1: a single spiked-down bid breaches.
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 0.4}
        first = trading_monitor._try_software_stop(order, "paper")
        self.assertEqual(first["triggered"], 0)
        self.assertEqual(order["software_stop_breach_cycles"], 1)

        # Cycle 2: quote recovers above the stop — breach count resets, no order.
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 1.5}
        second = trading_monitor._try_software_stop(order, "paper")
        self.assertEqual(second["triggered"], 0)
        self.assertEqual(order["software_stop_status"], "armed")
        self.assertEqual(order["software_stop_breach_cycles"], 0)
        self.assertEqual(len(submitted), 0)

    def test_account_resolution_failure_marks_protection_attention(self) -> None:
        order = self._base_order()
        protection = build_protection_status([{**order, "monitor_account_error": "broker account unavailable: missing"}])

        self.assertEqual(protection["state"], "unprotected")
        self.assertTrue(protection["requires_manual_attention"])
        self.assertIn("broker account unavailable", protection["stop_failure_reason"])

    def test_monitor_surfaces_missing_broker_account_on_instance(self) -> None:
        order = self._base_order()
        instance = {"risk_plan": {"positions": [], "strategy_positions": []}, "event_timeline": []}
        saved: list[dict] = []
        trading_monitor.redis_available = lambda: False
        trading_monitor.list_trading_runs = lambda owner_id=None, limit=100, summary=False: [
            {
                "id": "run-1",
                "owner_id": "owner-1",
                "orders": [order],
                "trade_instance": instance,
                "config": {"broker": "alpaca", "broker_account": "missing"},
            }
        ]
        trading_monitor.account_ref_for_config = lambda config, owner_id=None: (_ for _ in ()).throw(ValueError("missing broker account"))
        trading_monitor.mark_trading_run = lambda run_id, **kwargs: saved.append({"run_id": run_id, **kwargs})

        result = trading_monitor.monitor_pending_stops()

        self.assertEqual(result["orders_changed"], 1)
        self.assertTrue(saved)
        saved_instance = saved[0]["instance_json"]
        saved_order = saved[0]["orders_json"][0]
        self.assertEqual(saved_order["monitor_status"], "broker_account_unavailable")
        self.assertIn("missing broker account", saved_order["monitor_account_error"])
        self.assertEqual(saved_instance["lifecycle_state"], "manual_intervention_required")
        self.assertEqual(saved_instance["event_timeline"][-1]["event_type"], "broker_account_unavailable")
        self.assertTrue(saved_instance["protection_status"]["requires_manual_attention"])

    def test_monitor_prioritizes_stop_loss_before_take_profit(self) -> None:
        order = self._base_order()
        run = {"id": "run-1", "orders": [order], "config": {"longbridge_account": "paper"}, "trade_instance": {}}
        submitted: list[dict] = []
        marked: list[dict] = []
        trading_monitor.redis_available = lambda: False
        trading_monitor.list_trading_runs = lambda account_name, limit, summary=False: [run]
        trading_monitor.mark_trading_run = lambda run_id, **kwargs: marked.append({"run_id": run_id, **kwargs})
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 0.95}

        def submit_market_order(symbol, quantity, side, account_name, remark):
            submitted.append({"symbol": symbol, "quantity": quantity, "side": side, "remark": remark})
            return {"order_id": "close-stop"}

        trading_monitor.submit_market_order = submit_market_order
        summary = trading_monitor.monitor_pending_stops()

        self.assertEqual(summary["software_stop_triggered"], 1)
        self.assertEqual(summary["software_take_profit_triggered"], 0)
        self.assertEqual(len(submitted), 1)
        self.assertIn("AI_OPTION_SW_STOP", submitted[0]["remark"])
        self.assertEqual(order["software_take_profit_targets"][0].get("status", "pending"), "pending")
        self.assertEqual(marked[0]["run_id"], "run-1")

    def test_order_monitor_default_interval_is_fast_enough_for_protection(self) -> None:
        self.assertLessEqual(trading_monitor.ORDER_MONITOR_INTERVAL_SECONDS, 5.0)

    def test_software_stop_submission_waits_for_longbridge_fill_report_before_closed(self) -> None:
        order = self._base_order()
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 0.8}
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: {"order_id": "close-stop"}

        result = trading_monitor._try_software_stop(order, "paper")

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(order["status"], "software_stop_submitted")
        self.assertEqual(order["software_stop_submitted_quantity"], 2)
        self.assertEqual(int(order.get("software_stop_closed_quantity") or 0), 0)

        trading_monitor.order_detail = lambda order_id, account_name: {"status": "filled", "executed_quantity": 2, "executed_price": 0.78}
        reconcile = trading_monitor._try_single_leg_exit_reconcile(order, "paper")

        self.assertTrue(reconcile["changed"])
        self.assertEqual(order["status"], "software_stop_filled")
        self.assertEqual(order["software_stop_exit_status"], "filled")
        self.assertEqual(order["software_stop_closed_quantity"], 2)
        self.assertEqual(lifecycle_from_orders([order]), "closed")

    def test_manual_flatten_submission_reconciles_broker_fill_before_closed(self) -> None:
        order = self._base_order()
        order["status"] = "instance_flatten_submitted"
        order["instance_flatten_order"] = {"order_id": "close-flat"}
        order["instance_flatten_submitted_quantity"] = 2
        trading_monitor.order_detail = lambda order_id, account_name: {"status": "filled", "executed_quantity": 2, "executed_price": 0.92}

        reconcile = trading_monitor._try_single_leg_exit_reconcile(order, "paper")

        self.assertTrue(reconcile["changed"])
        self.assertEqual(order["status"], "instance_flatten_filled")
        self.assertEqual(order["instance_flatten_exit_status"], "filled")
        self.assertEqual(order["instance_flatten_closed_quantity"], 2)
        self.assertEqual(order["instance_flatten_exit_executed_price"], 0.92)
        self.assertEqual(lifecycle_from_orders([order]), "closed")

    def test_pending_single_leg_exit_is_exiting_not_unprotected(self) -> None:
        order = self._base_order()
        order["covered_quantity"] = 0
        order["software_stop_active"] = False
        order["software_stop_quantity"] = 0
        order["software_take_profit_active"] = False
        order["software_take_profit_quantity"] = 0
        order["status"] = "software_stop_submitted"
        order["software_stop_order"] = {"order_id": "close-stop"}
        order["software_stop_submitted_quantity"] = 2

        protection = build_protection_status([order])

        self.assertEqual(protection["state"], "exiting")
        self.assertEqual(protection["single_leg_exit_submitted_quantity"], 2)
        self.assertEqual(protection["unprotected_quantity"], 0)
        self.assertFalse(protection["requires_manual_attention"])
        self.assertEqual(lifecycle_from_orders([order]), "exiting")

    def test_partial_single_leg_exit_lifecycle_stays_exiting(self) -> None:
        order = self._base_order()
        order["quantity"] = 3
        order["entry_filled_quantity"] = 3
        order["covered_quantity"] = 0
        order["software_stop_active"] = False
        order["software_stop_quantity"] = 0
        order["software_take_profit_active"] = False
        order["software_take_profit_quantity"] = 0
        order["status"] = "software_take_profit_partial_filled"
        order["software_take_profit_submitted_quantity"] = 3
        order["software_take_profit_closed_quantity"] = 1

        protection = build_protection_status([order])

        self.assertEqual(protection["state"], "exiting")
        self.assertEqual(protection["single_leg_exit_submitted_quantity"], 2)
        self.assertEqual(protection["unprotected_quantity"], 0)
        self.assertEqual(lifecycle_from_orders([order]), "exiting")

    def test_monitor_prioritizes_software_take_profit_before_smart_exit(self) -> None:
        order = self._base_order()
        order["entry_price"] = 1.0
        order["single_leg_exit_conditions"] = [{"type": "option_price_take_profit", "price": 1.1, "reason": "smart tp"}]
        run = {"id": "run-1", "orders": [order], "config": {"longbridge_account": "paper"}, "trade_instance": {}}
        submitted: list[dict] = []
        trading_monitor.redis_available = lambda: False
        trading_monitor.list_trading_runs = lambda account_name, limit, summary=False: [run]
        trading_monitor.mark_trading_run = lambda run_id, **kwargs: None
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 1.2}

        def submit_market_order(symbol, quantity, side, account_name, remark):
            submitted.append({"symbol": symbol, "quantity": quantity, "side": side, "remark": remark})
            return {"order_id": "close-tp"}

        trading_monitor.submit_market_order = submit_market_order
        summary = trading_monitor.monitor_pending_stops()

        self.assertEqual(summary["software_take_profit_triggered"], 1)
        self.assertEqual(summary["single_leg_smart_exit_triggered"], 0)
        self.assertEqual(len(submitted), 1)
        self.assertIn("AI_OPTION_SW_TP", submitted[0]["remark"])
        self.assertEqual(order["software_take_profit_targets"][0]["status"], "submitted")

    def test_attach_stop_arms_software_take_profit_after_entry_fill(self) -> None:
        order = {
            "status": "entry_submitted_stop_pending_unfilled",
            "symbol": "SPY",
            "order_symbol": "SPY260619C00500000.US",
            "entry_order": {"order_id": "entry-1"},
            "quantity": 2,
            "entry_price": 2.0,
            "stop_loss_pct": 25,
            "take_profit_pct": 30,
        }
        stop_orders: list[dict] = []
        trading_monitor.order_detail = lambda order_id, account_name: {
            "status": "filled",
            "executed_quantity": 2,
            "executed_price": 1.8,
        }

        def submit_stop(symbol, quantity, stop_price, account_name, remark):
            stop_orders.append({"symbol": symbol, "quantity": quantity, "stop_price": stop_price, "remark": remark})
            return {"order_id": "stop-1", "quantity": quantity, "stop_price": stop_price}

        trading_monitor.submit_stop_sell_order = submit_stop

        changed = trading_monitor._try_attach_stop(order, "paper", software_stop_enabled=True, software_take_profit_enabled=True)

        self.assertTrue(changed)
        self.assertEqual(order["entry_price"], 1.8)
        self.assertEqual(order["entry_price_source"], "executed_price")
        self.assertEqual(order["stop_trigger_price"], 1.35)
        self.assertEqual(order["status"], "stop_submitted_after_fill")
        self.assertEqual(stop_orders[0]["quantity"], 2)
        self.assertTrue(order["software_take_profit_active"])
        self.assertEqual(order["software_take_profit_targets"], [
            {"name": "take_profit", "price": 2.34, "quantity": 2, "status": "pending"},
        ])

    def test_attach_stop_arms_tiered_software_take_profit_when_enabled(self) -> None:
        order = {
            "status": "entry_submitted_stop_pending_unfilled",
            "symbol": "SPY",
            "order_symbol": "SPY260619C00500000.US",
            "entry_order": {"order_id": "entry-1"},
            "quantity": 2,
            "entry_price": 2.0,
            "stop_loss_pct": 25,
            "take_profit_pct": 30,
            "tiered_take_profit_enabled": True,
            "take_profit_1_pct": 20,
            "take_profit_2_pct": 35,
        }
        trading_monitor.order_detail = lambda order_id, account_name: {
            "status": "filled",
            "executed_quantity": 2,
            "executed_price": 1.8,
        }
        trading_monitor.submit_stop_sell_order = lambda symbol, quantity, stop_price, account_name, remark: {"order_id": "stop-1"}

        trading_monitor._try_attach_stop(order, "paper", software_stop_enabled=True, software_take_profit_enabled=True)

        self.assertEqual(order["software_take_profit_targets"], [
            {"name": "tp1", "price": 2.16, "quantity": 1, "status": "pending"},
            {"name": "tp2", "price": 2.43, "quantity": 1, "status": "pending"},
        ])

    def test_attach_stop_records_generic_broker_error_without_raising(self) -> None:
        order = {
            "status": "entry_submitted_stop_pending_unfilled",
            "symbol": "SPY",
            "order_symbol": "SPY260619C00500000.US",
            "entry_order": {"order_id": "entry-1"},
            "quantity": 1,
            "covered_quantity": 0,
            "entry_price": 2.0,
            "stop_loss_pct": 25,
            "stop_trigger_price": 1.5,
        }
        trading_monitor.order_detail = lambda order_id, account_name: {
            "status": "filled",
            "executed_quantity": 1,
            "executed_price": 2.0,
        }
        trading_monitor.submit_stop_sell_order = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("alpaca stop rejected"))

        changed = trading_monitor._try_attach_stop(order, "alpaca:owner:paper", software_stop_enabled=True, software_take_profit_enabled=True)

        self.assertTrue(changed)
        self.assertEqual(order["monitor_error"], "alpaca stop rejected")
        self.assertEqual(order["entry_filled_quantity"], 1)

    def test_single_leg_smart_exit_cancels_protective_orders_before_close(self) -> None:
        order = self._base_order()
        order["entry_price"] = 1.0
        order["software_stop_active"] = False
        order["software_take_profit_active"] = False
        order["stop_order"] = {"order_id": "stop-1"}
        order["stop_orders"] = [{"order_id": "stop-1"}]
        order["single_leg_exit_conditions"] = [{"type": "option_price_take_profit", "price": 1.1, "reason": "smart tp"}]
        submitted: list[dict] = []
        canceled: list[str] = []
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 1.2}

        def cancel_order(order_id, account_name):
            canceled.append(order_id)
            return {"status": "canceled"}

        def submit_market_order(symbol, quantity, side, account_name, remark):
            submitted.append({"symbol": symbol, "quantity": quantity, "side": side, "remark": remark})
            return {"order_id": "smart-close"}

        trading_monitor.cancel_order = cancel_order
        trading_monitor.submit_market_order = submit_market_order

        first = trading_monitor._try_single_leg_smart_exit(order, "paper")
        result = trading_monitor._try_single_leg_smart_exit(order, "paper")

        self.assertEqual(first["triggered"], 0)
        self.assertEqual(order.get("pending_smart_exit_trigger"), None)
        self.assertEqual(result["triggered"], 1)
        self.assertEqual(canceled, ["stop-1"])
        self.assertEqual(len(submitted), 1)
        self.assertIn("AI_OPTION_SMART_EXIT", submitted[0]["remark"])
        self.assertEqual(order["stop_orders"], [])
        self.assertNotIn("stop_order", order)
        self.assertEqual(order["covered_quantity"], 0)
        self.assertEqual(order["single_leg_smart_exit_status"], "market_close_submitted")

    def test_single_leg_smart_exit_can_trigger_on_greek_decay(self) -> None:
        order = self._base_order()
        order["entry_price"] = 1.0
        order["software_stop_active"] = False
        order["software_take_profit_active"] = False
        order["single_leg_exit_conditions"] = [{"type": "option_greek", "field": "delta", "operator": "<=", "value": 0.25, "reason": "delta faded"}]
        submitted: list[dict] = []
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 1.05, "delta": 0.22}
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: submitted.append({"symbol": symbol, "quantity": quantity, "side": side, "remark": remark}) or {"order_id": "greek-close"}

        first = trading_monitor._try_single_leg_smart_exit(order, "paper")
        result = trading_monitor._try_single_leg_smart_exit(order, "paper")

        self.assertEqual(first["triggered"], 0)
        self.assertEqual(result["triggered"], 1)
        self.assertEqual(order["single_leg_smart_exit_trigger"], "smart_option_delta_exit")
        self.assertEqual(submitted[0]["quantity"], 2)

    def test_residual_single_leg_smart_exit_starts_exit_reconcile(self) -> None:
        order = {
            "status": "strategy_residual_tracking",
            "symbol": "NVDA",
            "order_symbol": "NVDA260619C00230000.US",
            "contract_symbol": "NVDA260619C00230000",
            "quantity": 3,
            "entry_filled_quantity": 3,
            "entry_price": 6.2,
            "residual_leg_tracking_active": True,
            "residual_strategy_tracking_id": "strategy-nvda",
            "residual_leg_contract_symbol": "NVDA260619C00230000",
            "residual_leg_quantity": 3,
            "single_leg_exit_conditions": [{"type": "time_exit", "exit_at": "2000-01-01T00:00:00-04:00", "reason": "到期前退出"}],
        }
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": False, "error": "quote unavailable"}
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: {"order_id": "smart-close"}

        result = trading_monitor._try_single_leg_smart_exit(order, "paper")

        self.assertEqual(result["triggered"], 1)
        self.assertEqual(order["status"], "single_leg_smart_exit_submitted")
        self.assertEqual(order["strategy_exit_status"], "submitted")
        self.assertFalse(order["residual_leg_tracking_active"])
        self.assertTrue(trading_monitor._residual_exit_reconcile_needed(order))

    def test_monitor_skips_take_profit_when_stop_loss_close_is_retrying(self) -> None:
        order = self._base_order()
        order["stop_order"] = {"order_id": "stop-1"}
        run = {"id": "run-1", "orders": [order], "config": {"longbridge_account": "paper"}, "trade_instance": {}}
        cancel_calls = 0
        trading_monitor.redis_available = lambda: False
        trading_monitor.list_trading_runs = lambda account_name, limit, summary=False: [run]
        trading_monitor.mark_trading_run = lambda run_id, **kwargs: None
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 0.95}
        trading_monitor.submit_market_order = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broker busy"))

        def cancel_order(*args, **kwargs):
            nonlocal cancel_calls
            cancel_calls += 1
            return {"status": "canceled"}

        trading_monitor.cancel_order = cancel_order
        summary = trading_monitor.monitor_pending_stops()

        self.assertEqual(summary["software_stop_triggered"], 0)
        self.assertEqual(summary["software_take_profit_triggered"], 0)
        self.assertEqual(cancel_calls, 0)
        self.assertEqual(order["software_stop_status"], "market_close_retry_scheduled")
        self.assertEqual(order["software_take_profit_targets"][0].get("status", "pending"), "pending")

    def test_software_stop_close_failure_stays_armed_and_suppresses_duplicate_retry(self) -> None:
        order = self._base_order()
        calls = 0
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 0.8}

        def failing_market_order(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("broker busy")

        trading_monitor.submit_market_order = failing_market_order
        result = trading_monitor._try_software_stop(order, "paper")
        second = trading_monitor._try_software_stop(order, "paper")

        self.assertTrue(result["changed"])
        self.assertEqual(result["failed"], 0)
        self.assertTrue(order["software_stop_active"])
        self.assertEqual(order["software_stop_status"], "market_close_retry_scheduled")
        self.assertEqual(order["software_stop_retry_count"], 1)
        self.assertFalse(second["changed"])
        self.assertEqual(calls, 1)

    def test_take_profit_cancel_failure_stays_armed_and_suppresses_duplicate_retry(self) -> None:
        order = self._base_order()
        order["stop_order"] = {"order_id": "stop-1"}
        cancel_calls = 0
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 1.2}

        def failing_cancel(*args, **kwargs):
            nonlocal cancel_calls
            cancel_calls += 1
            raise RuntimeError("cancel rejected")

        trading_monitor.cancel_order = failing_cancel
        result = trading_monitor._try_software_take_profit(order, "paper")
        second = trading_monitor._try_software_take_profit(order, "paper")

        self.assertTrue(result["changed"])
        self.assertEqual(result["failed"], 0)
        self.assertTrue(order["software_take_profit_active"])
        self.assertEqual(order["software_take_profit_status"], "cancel_stop_retry_scheduled")
        self.assertEqual(order["software_take_profit_retry_count"], 1)
        self.assertFalse(second["changed"])
        self.assertEqual(cancel_calls, 1)

    def test_take_profit_close_failure_rearms_software_stop_after_canceling_broker_stop(self) -> None:
        order = self._base_order()
        order["software_stop_active"] = False
        order["stop_order"] = {"order_id": "stop-1"}
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 1.2}
        trading_monitor.cancel_order = lambda *args, **kwargs: {"status": "canceled"}

        def failing_market_order(*args, **kwargs):
            raise RuntimeError("market order rejected")

        trading_monitor.submit_market_order = failing_market_order
        result = trading_monitor._try_software_take_profit(order, "paper")

        self.assertTrue(result["changed"])
        self.assertTrue(order["software_take_profit_active"])
        self.assertEqual(order["software_take_profit_status"], "market_close_retry_scheduled")
        self.assertTrue(order["software_stop_active"])
        self.assertEqual(order["software_stop_reason"], "take_profit_close_failed_after_stop_cancel")
        self.assertEqual(order["software_stop_quantity"], 2)

    def test_retry_waiting_accepts_naive_and_zulu_timestamps(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(seconds=60)).replace(tzinfo=None)
        zulu_future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")

        self.assertTrue(trading_monitor._retry_waiting({"software_stop_next_retry_at": future.isoformat()}, "software_stop"))
        self.assertTrue(trading_monitor._retry_waiting({"software_stop_next_retry_at": zulu_future}, "software_stop"))
        self.assertFalse(trading_monitor._retry_waiting({"software_stop_next_retry_at": "not-a-time"}, "software_stop"))

    def test_strategy_time_exit_triggers_auto_exit(self) -> None:
        order = {
            "tracking_id": "strategy-1",
            "status": "submitted",
            "legs": [{"leg": {"contract_symbol": "SPY260619C00500000", "action": "buy", "side": "call"}, "filled_quantity": 1, "quantity": 1}],
        }
        position = {
            "tracking_id": "strategy-1",
            "symbol": "SPY",
            "label": "calendar",
            "strategy_type": "call_calendar_spread",
            "risk_tracking_active": True,
            "execution_status": "submitted",
            "strategy_units": 1,
            "exit_conditions": [{"type": "time_exit", "exit_at": "2000-01-01T00:00:00-04:00", "reason": "到期前退出"}],
            "best_pnl": 0,
        }
        instance = {"risk_plan": {"strategy_positions": [position], "strategy_auto_execute_enabled": True}}
        submitted: list[dict] = []
        trading_monitor._strategy_position_mark = lambda pos, account_name: {"available": True, "mark": 1.0, "pnl": 10.0, "leg_quotes": [], "status": "ok"}
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: submitted.append({"symbol": symbol, "quantity": quantity, "side": side, "remark": remark}) or {"order_id": "exit-1"}

        result = trading_monitor._try_strategy_risk_tracking(instance, "paper", [order])

        self.assertEqual(result["smart_exit_alerted"], 1)
        self.assertEqual(result["auto_exit_submitted"], 1)
        # Exit was submitted but not yet broker-confirmed filled, so tracking must
        # stay active until fills land — guards against silent abandonment of a
        # position whose close order is still in flight.
        self.assertTrue(position["risk_tracking_active"])
        self.assertEqual(position["smart_exit_trigger"], "smart_time_exit")
        self.assertEqual(order["strategy_exit_status"], "submitted")
        self.assertEqual(len(submitted), 1)

    def test_strategy_pnl_mark_and_thresholds_use_total_units(self) -> None:
        position = {
            "tracking_id": "strategy-spy",
            "symbol": "SPY",
            "strategy_units": 49,
            "legs": [
                {"contract_symbol": "SPY260518C00739000", "action": "buy", "side": "call", "qty": 1, "price": 3.46},
                {"contract_symbol": "SPY260518C00745000", "action": "sell", "side": "call", "qty": 1, "price": 1.33},
            ],
            "stop_loss_pnl": -53.25,
            "take_profit_1_pnl": 103.59,
            "take_profit_2_pnl": 207.18,
        }
        trading_monitor._monitor_option_quote = lambda symbol, account_name, market_data_source=None: {
            "available": True,
            "price": 3.60 if "739" in str(symbol) else 1.39,
            "exit_price": 3.60 if "739" in str(symbol) else 1.39,
        }

        mark = trading_monitor._strategy_position_mark(position, "paper")
        trading_monitor._ensure_strategy_total_threshold_basis(position)

        self.assertTrue(mark["available"], mark)
        self.assertEqual(round(mark["pnl"], 2), 392.0)
        self.assertEqual(position["pnl_threshold_basis"], "total_position")
        self.assertEqual(position["per_unit_take_profit_1_pnl"], 103.59)
        self.assertEqual(position["take_profit_1_pnl"], 5075.91)
        self.assertEqual(position["stop_loss_pnl"], -2609.25)

    def test_tiered_strategy_tp1_exits_half_units_and_keeps_tracking(self) -> None:
        position = {
            "tracking_id": "strategy-spy",
            "symbol": "SPY",
            "strategy_units": 10,
            "tiered_take_profit_enabled": True,
        }
        order = {
            "tracking_id": "strategy-spy",
            "status": "submitted",
            "legs": [
                {"leg": {"contract_symbol": "SPY260518C00739000", "action": "buy", "side": "call", "qty": 1}, "filled_quantity": 10, "quantity": 10},
                {"leg": {"contract_symbol": "SPY260518C00745000", "action": "sell", "side": "call", "qty": 1}, "filled_quantity": 10, "quantity": 10},
            ],
        }
        submitted: list[dict] = []
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: submitted.append({"symbol": symbol, "quantity": quantity, "side": side}) or {"order_id": f"exit-{len(submitted)}"}

        exit_result = trading_monitor._submit_strategy_auto_exit(position, [order], "paper", "tp1")
        trading_monitor._sync_strategy_order_exit([order], "strategy-spy", exit_result)

        self.assertTrue(exit_result["partial"])
        self.assertEqual(exit_result["closed_units"], 5)
        self.assertEqual([item["quantity"] for item in submitted], [5, 5])
        self.assertEqual(order["strategy_exit_status"], "partial_submitted")
        self.assertTrue(order["risk_tracking_active"])
        self.assertEqual(order["legs"][0]["strategy_exit_quantity"], 5)

    def test_strategy_take_profit_uses_take_profit_path_not_smart_exit(self) -> None:
        position = {
            "tracking_id": "strategy-spy",
            "symbol": "SPY",
            "risk_tracking_active": True,
            "execution_status": "submitted",
            "strategy_units": 1,
            "take_profit_1_pnl": 10.0,
            "best_pnl": 12.0,
        }
        instance = {"risk_plan": {"strategy_positions": [position], "strategy_auto_execute_enabled": False}}
        trading_monitor._strategy_position_mark = lambda pos, account_name: {"available": True, "mark": 1.0, "pnl": 12.0, "leg_quotes": [], "status": "ok"}

        first = trading_monitor._try_strategy_risk_tracking(instance, "paper", [])
        second = trading_monitor._try_strategy_risk_tracking(instance, "paper", [])

        self.assertEqual(first["take_profit_alerted"], 0)
        self.assertEqual(first["smart_exit_alerted"], 0)
        self.assertEqual(second["take_profit_alerted"], 1)
        self.assertEqual(second["smart_exit_alerted"], 0)
        self.assertEqual(position["take_profit_1_status"], "alerted")
        self.assertNotIn("smart_exit_trigger", position)

    def test_strategy_auto_exit_switches_rejected_long_residual_to_single_leg_tracking(self) -> None:
        position = {
            "tracking_id": "strategy-nvda",
            "symbol": "NVDA",
            "strategy_units": 3,
        }
        order = {
            "tracking_id": "strategy-nvda",
            "status": "submitted",
            "strategy_auto_execute": True,
            "quantity": 3,
            "entry_filled_quantity": 3,
            "legs": [
                {"leg": {"contract_symbol": "NVDA260518C230000", "action": "buy", "side": "call", "qty": 1, "price": 6.2}, "filled_quantity": 3, "quantity": 3},
                {"leg": {"contract_symbol": "NVDA260518C235000", "action": "sell", "side": "call", "qty": 1}, "filled_quantity": 3, "quantity": 3},
            ],
        }
        submitted: list[dict] = []

        def submit_market_order(symbol, quantity, side, account_name, remark):
            submitted.append({"symbol": symbol, "quantity": quantity, "side": side})
            return {"order_id": f"exit-{len(submitted)}"}

        trading_monitor.submit_market_order = submit_market_order
        trading_monitor.wait_for_order_fill = lambda order_id, account_name, timeout_seconds=3: (
            {"status": "filled", "executed_quantity": 3}
            if order_id == "exit-1"
            else {
                "status": "rejected",
                "executed_quantity": 0,
                "msg": "This closing order may trigger a margin call. Please do not split the options strategy.",
            }
        )

        exit_result = trading_monitor._submit_strategy_auto_exit(position, [order], "paper", "stop")
        trading_monitor._sync_strategy_order_exit([order], "strategy-nvda", exit_result)

        self.assertEqual(exit_result["submitted"], 1)
        self.assertEqual(exit_result["filled"], 1)
        self.assertEqual(exit_result["failed"], 1)
        self.assertEqual(order["status"], "strategy_residual_tracking")
        self.assertEqual(order["strategy_exit_status"], "residual_tracking")
        self.assertTrue(order["residual_leg_tracking_active"])
        self.assertTrue(order["software_stop_active"])
        self.assertTrue(order["software_take_profit_active"])
        self.assertEqual(order["contract_symbol"], "NVDA260518C230000")
        self.assertEqual(order["entry_filled_quantity"], 3)
        self.assertIn("do not split", order["strategy_exit_error"])
        self.assertEqual(order["legs"][1]["strategy_exit_status"], "filled")
        self.assertEqual(order["legs"][0]["strategy_exit_status"], "failed")
        self.assertIn("do not split", order["legs"][0]["strategy_exit_error"])
        protection = build_protection_status([order])
        self.assertEqual(protection["state"], "strategy_residual_tracking")
        self.assertFalse(protection["requires_manual_attention"])
        self.assertEqual(lifecycle_from_orders([order]), "monitoring")

    def test_strategy_auto_exit_defers_long_exit_when_short_cover_fails(self) -> None:
        # Bull call spread exit: the SHORT cover (buy-to-close) fails. The long
        # hedge must NOT be sold — doing so leaves a naked short. It is deferred
        # so the next cycle retries the short cover with the hedge intact.
        position = {"tracking_id": "strategy-amzn", "symbol": "AMZN", "strategy_units": 2}
        order = {
            "tracking_id": "strategy-amzn",
            "status": "submitted",
            "strategy_auto_execute": True,
            "quantity": 2,
            "entry_filled_quantity": 2,
            "legs": [
                {"leg": {"contract_symbol": "AMZN260518C230000", "action": "buy", "side": "call", "qty": 1, "price": 6.2}, "filled_quantity": 2, "quantity": 2},
                {"leg": {"contract_symbol": "AMZN260518C235000", "action": "sell", "side": "call", "qty": 1}, "filled_quantity": 2, "quantity": 2},
            ],
        }
        submitted: list[dict] = []

        def submit_market_order(symbol, quantity, side, account_name, remark):
            submitted.append({"symbol": symbol, "quantity": quantity, "side": side})
            return {"order_id": f"exit-{len(submitted)}"}

        trading_monitor.submit_market_order = submit_market_order
        # The only order that reaches the broker is the short cover (buy-to-close);
        # it is rejected. The long exit must never be submitted.
        trading_monitor.wait_for_order_fill = lambda order_id, account_name, timeout_seconds=3: {
            "status": "rejected",
            "executed_quantity": 0,
            "msg": "buy-to-close rejected: insufficient buying power",
        }

        exit_result = trading_monitor._submit_strategy_auto_exit(position, [order], "paper", "stop")

        # Exactly one broker order (the short cover). The long was deferred.
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0]["side"], "buy")
        self.assertEqual(submitted[0]["symbol"], trading_monitor.option_order_symbol("AMZN260518C235000"))
        self.assertEqual(exit_result["failed"], 1)
        self.assertEqual(exit_result["filled"], 0)
        # The long (buy-action) leg was deferred, not sold.
        long_leg = order["legs"][0]
        self.assertEqual(long_leg["strategy_exit_status"], "deferred_short_cover_failed")
        self.assertNotIn("strategy_exit_order", long_leg)
        short_leg = order["legs"][1]
        self.assertEqual(short_leg["strategy_exit_status"], "failed")

    def test_strategy_exit_keeps_instance_monitoring_when_another_strategy_is_active(self) -> None:
        instance = {
            "risk_plan": {
                "strategy_positions": [
                    {"tracking_id": "strategy-closed", "risk_tracking_active": False},
                    {"tracking_id": "strategy-active", "risk_tracking_active": True},
                ]
            }
        }
        orders = [
            {"tracking_id": "strategy-closed", "status": "strategy_auto_exit_filled", "risk_tracking_active": False},
            {"tracking_id": "strategy-active", "status": "submitted", "risk_tracking_active": True},
        ]

        self.assertTrue(trading_monitor._has_other_active_strategy_tracking(instance, "strategy-closed", orders))

    def test_strategy_exit_reconcile_switches_rejected_long_residual_to_single_leg_tracking(self) -> None:
        order = {
            "tracking_id": "strategy-nvda",
            "status": "strategy_auto_exit_submitted",
            "strategy_exit_status": "submitted",
            "quantity": 3,
            "entry_filled_quantity": 3,
            "legs": [
                {
                    "leg": {"contract_symbol": "NVDA260518C230000", "action": "buy", "side": "call", "price": 6.2},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_order": {"order_id": "sell-long"},
                    "strategy_exit_status": "submitted",
                    "strategy_exit_quantity": 3,
                },
                {
                    "leg": {"contract_symbol": "NVDA260518C235000", "action": "sell", "side": "call"},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_order": {"order_id": "buy-short"},
                    "strategy_exit_status": "submitted",
                    "strategy_exit_quantity": 3,
                },
            ],
        }
        instance = {
            "risk_plan": {
                "strategy_positions": [
                    {
                        "tracking_id": "strategy-nvda",
                        "risk_tracking_active": True,
                        "tracking_status": "auto_exit_submitted",
                        "stop_loss_pct": 25,
                        "take_profit_pct": 30,
                    }
                ]
            }
        }
        trading_monitor.order_detail = lambda order_id, account_name: (
            {"status": "filled", "executed_quantity": 3}
            if order_id == "buy-short"
            else {
                "status": "rejected",
                "executed_quantity": 0,
                "msg": "This closing order may trigger a margin call. Please do not split the options strategy.",
            }
        )

        result = trading_monitor._try_strategy_exit_reconcile(order, instance, "paper")

        self.assertTrue(result["changed"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(order["status"], "strategy_residual_tracking")
        self.assertEqual(order["strategy_exit_status"], "residual_tracking")
        self.assertTrue(order["software_stop_active"])
        self.assertTrue(order["software_take_profit_active"])
        self.assertIn("do not split", order["strategy_exit_error"])
        position = instance["risk_plan"]["strategy_positions"][0]
        self.assertFalse(position["risk_tracking_active"])
        self.assertEqual(position["tracking_status"], "residual_leg_tracking")
        self.assertIn("do not split", position["strategy_exit_error"])
        protection = build_protection_status([order])
        self.assertEqual(protection["state"], "strategy_residual_tracking")
        self.assertFalse(protection["requires_manual_attention"])
        self.assertEqual(lifecycle_from_orders([order]), "monitoring")

    def test_strategy_exit_reconcile_marks_rejected_short_residual_as_combo_required(self) -> None:
        order = {
            "tracking_id": "strategy-nvda",
            "status": "strategy_auto_exit_submitted",
            "strategy_exit_status": "submitted",
            "quantity": 3,
            "entry_filled_quantity": 3,
            "legs": [
                {
                    "leg": {"contract_symbol": "NVDA260518C230000", "action": "buy", "side": "call"},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_order": {"order_id": "sell-long"},
                    "strategy_exit_status": "submitted",
                    "strategy_exit_quantity": 3,
                },
                {
                    "leg": {"contract_symbol": "NVDA260518C235000", "action": "sell", "side": "call"},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_order": {"order_id": "buy-short"},
                    "strategy_exit_status": "submitted",
                    "strategy_exit_quantity": 3,
                },
            ],
        }
        instance = {"risk_plan": {"strategy_positions": [{"tracking_id": "strategy-nvda", "risk_tracking_active": True}]}}
        trading_monitor.order_detail = lambda order_id, account_name: (
            {"status": "filled", "executed_quantity": 3}
            if order_id == "sell-long"
            else {
                "status": "rejected",
                "executed_quantity": 0,
                "msg": "This closing order may trigger a margin call. Please do not split the options strategy.",
            }
        )

        result = trading_monitor._try_strategy_exit_reconcile(order, instance, "paper")

        self.assertTrue(result["changed"])
        self.assertEqual(order["status"], "broker_combo_close_required")
        self.assertEqual(order["strategy_exit_status"], "broker_combo_required")
        self.assertTrue(order["broker_combo_close_required"])
        position = instance["risk_plan"]["strategy_positions"][0]
        self.assertEqual(position["tracking_status"], "broker_combo_close_required")
        protection = build_protection_status([order])
        self.assertEqual(protection["state"], "broker_combo_close_required")
        self.assertTrue(protection["requires_manual_attention"])
        self.assertEqual(lifecycle_from_orders([order]), "manual_intervention_required")

    def test_residual_long_leg_exit_reconcile_marks_auto_exit_completed(self) -> None:
        order = {
            "tracking_id": "strategy-nvda",
            "status": "software_stop_submitted",
            "strategy_exit_status": "submitted",
            "residual_strategy_tracking_id": "strategy-nvda",
            "residual_leg_contract_symbol": "NVDA260518C230000",
            "residual_leg_quantity": 3,
            "residual_leg_exit_source": "software_stop",
            "quantity": 3,
            "entry_filled_quantity": 3,
            "software_stop_order": {"order_id": "close-long"},
            "legs": [
                {
                    "leg": {"contract_symbol": "NVDA260518C230000", "action": "buy", "side": "call", "price": 6.2},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_status": "failed",
                    "strategy_exit_quantity": 3,
                },
                {
                    "leg": {"contract_symbol": "NVDA260518C235000", "action": "sell", "side": "call", "price": 3.65},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_status": "filled",
                    "strategy_exit_quantity": 3,
                    "strategy_exit_filled_quantity": 3,
                    "strategy_exit_executed_price": 3.65,
                },
            ],
        }
        trading_monitor.order_detail = lambda order_id, account_name: {"status": "filled", "executed_quantity": 3, "executed_price": 5.9}

        result = trading_monitor._try_residual_exit_reconcile(order, "paper")

        self.assertTrue(result["changed"])
        self.assertEqual(order["status"], "strategy_auto_exit_filled")
        self.assertEqual(order["strategy_exit_status"], "filled")
        self.assertFalse(order["residual_leg_tracking_active"])
        self.assertEqual(order["legs"][0]["strategy_exit_status"], "filled")
        self.assertEqual(order["legs"][0]["strategy_exit_executed_price"], 5.9)
        protection = build_protection_status([order])
        self.assertEqual(protection["state"], "strategy_exited")
        self.assertFalse(protection["requires_manual_attention"])
        self.assertEqual(lifecycle_from_orders([order]), "closed")

    def test_residual_tracking_detects_manual_flat_from_broker_positions(self) -> None:
        order = {
            "tracking_id": "strategy-nvda",
            "status": "strategy_residual_tracking",
            "strategy_exit_status": "residual_tracking",
            "residual_leg_tracking_active": True,
            "residual_leg_contract_symbol": "NVDA260518C00230000",
            "contract_symbol": "NVDA260518C00230000",
            "order_symbol": "NVDA260518C230000.US",
            "residual_leg_quantity": 3,
            "quantity": 3,
            "entry_filled_quantity": 3,
            "software_stop_active": True,
            "software_stop_quantity": 3,
            "software_take_profit_active": True,
            "software_take_profit_quantity": 3,
            "legs": [
                {
                    "leg": {"contract_symbol": "NVDA260518C00230000", "action": "buy", "side": "call", "price": 6.2},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_status": "failed",
                    "strategy_exit_quantity": 3,
                },
                {
                    "leg": {"contract_symbol": "NVDA260518C00235000", "action": "sell", "side": "call", "price": 3.65},
                    "filled_quantity": 3,
                    "quantity": 3,
                    "strategy_exit_status": "filled",
                    "strategy_exit_quantity": 3,
                    "strategy_exit_filled_quantity": 3,
                    "strategy_exit_executed_price": 3.65,
                },
            ],
        }
        trading_monitor.positions = lambda account_name: []

        # Flat is now confirmed over multiple cycles so a single incomplete
        # positions() response can't permanently disarm protection on a still-open
        # leg. First cycle: not yet flat, protection stays armed.
        first = trading_monitor._try_residual_position_reconcile(order, "paper")
        self.assertEqual(first["closed"], 0)
        self.assertTrue(order["residual_leg_tracking_active"])
        self.assertTrue(order["software_stop_active"])

        # Second cycle reaches the default threshold (2) -> declared flat.
        result = trading_monitor._try_residual_position_reconcile(order, "paper")

        self.assertTrue(result["changed"])
        self.assertEqual(result["closed"], 1)
        self.assertEqual(order["status"], "strategy_manual_exit_detected")
        self.assertEqual(order["strategy_exit_status"], "filled")
        self.assertFalse(order["residual_leg_tracking_active"])
        self.assertFalse(order["software_stop_active"])
        self.assertFalse(order["software_take_profit_active"])
        self.assertEqual(order["legs"][0]["strategy_exit_status"], "filled")
        protection = build_protection_status([order])
        self.assertEqual(protection["state"], "strategy_exited")
        self.assertFalse(protection["requires_manual_attention"])
        self.assertEqual(lifecycle_from_orders([order]), "closed")

    def test_residual_tracking_keeps_monitoring_when_broker_position_exists(self) -> None:
        order = {
            "status": "strategy_residual_tracking",
            "residual_leg_tracking_active": True,
            "residual_leg_contract_symbol": "NVDA260518C00230000",
            "quantity": 3,
            "entry_filled_quantity": 3,
            "software_stop_active": True,
            "software_stop_quantity": 3,
        }
        trading_monitor.positions = lambda account_name: [{"symbol": "NVDA260518C230000.US", "quantity": "3"}]

        result = trading_monitor._try_residual_position_reconcile(order, "paper")

        self.assertTrue(result["changed"])
        self.assertEqual(result["closed"], 0)
        self.assertTrue(order["residual_leg_tracking_active"])
        self.assertEqual(order["residual_position_quantity"], 3)

    def test_strategy_invalidation_text_triggers_underlying_price_exit(self) -> None:
        position = {
            "tracking_id": "strategy-2",
            "symbol": "SPY",
            "label": "bull_call_spread",
            "strategy_type": "bull_call_spread",
            "risk_tracking_active": True,
            "execution_status": "submitted",
            "strategy_units": 1,
            "invalidation": "正股跌破 500 失效",
            "best_pnl": 5.0,
        }
        instance = {"risk_plan": {"strategy_positions": [position], "strategy_auto_execute_enabled": False}}
        trading_monitor._strategy_position_mark = lambda pos, account_name: {"available": True, "mark": 1.0, "pnl": 4.0, "leg_quotes": [], "status": "ok"}
        trading_monitor._monitor_underlying_quote = lambda symbol, account_name, market_data_source=None: {"available": True, "price": 499.0, "source": "test"}

        first = trading_monitor._try_strategy_risk_tracking(instance, "paper", [])
        pending_status = position["tracking_status"]
        result = trading_monitor._try_strategy_risk_tracking(instance, "paper", [])

        self.assertEqual(first["smart_exit_alerted"], 0)
        self.assertEqual(pending_status, "smart_underlying_price_exit_pending_confirmation")
        self.assertEqual(result["smart_exit_alerted"], 1)
        self.assertEqual(result["auto_exit_submitted"], 0)
        self.assertFalse(position["risk_tracking_active"])
        self.assertEqual(position["smart_exit_trigger"], "smart_underlying_price_exit")
        self.assertEqual(position["tracking_status"], "smart_exit_alerted")

    def test_single_leg_latest_exit_triggers_smart_exit(self) -> None:
        order = {
            "status": "submitted",
            "symbol": "SPY",
            "order_symbol": "SPY260619C00500000.US",
            "contract_symbol": "SPY260619C00500000",
            "quantity": 2,
            "entry_filled_quantity": 2,
            "entry_price": 2.0,
            "single_leg_exit_conditions": [{"type": "time_exit", "exit_at": "2000-01-01T00:00:00-04:00", "reason": "到期前退出"}],
        }
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": False, "error": "no quote needed"}
        submitted: list[dict] = []
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: submitted.append({"symbol": symbol, "quantity": quantity, "side": side, "remark": remark}) or {"order_id": "exit-1"}

        result = trading_monitor._try_single_leg_smart_exit(order, "paper")

        self.assertEqual(result["triggered"], 1)
        self.assertTrue(submitted)
        self.assertEqual(order["status"], "single_leg_smart_exit_submitted")
        self.assertFalse(order.get("single_leg_smart_exit_active"))
        self.assertEqual(order["single_leg_smart_exit_trigger"], "smart_time_exit")

    def test_single_leg_invalidation_text_triggers_underlying_exit(self) -> None:
        order = {
            "status": "submitted",
            "symbol": "SPY",
            "order_symbol": "SPY260619C00500000.US",
            "contract_symbol": "SPY260619C00500000",
            "quantity": 2,
            "entry_filled_quantity": 2,
            "entry_price": 2.0,
            "candidate": {"risk_plan": {"invalidation": "正股跌破 500 失效"}},
        }
        trading_monitor._software_stop_quote = lambda item, account_name: {"available": True, "exit_price": 2.0}
        trading_monitor._monitor_underlying_quote = lambda symbol, account_name, market_data_source=None: {"available": True, "price": 499.0, "source": "test"}
        submitted: list[dict] = []
        trading_monitor.submit_market_order = lambda symbol, quantity, side, account_name, remark: submitted.append({"symbol": symbol, "quantity": quantity, "side": side, "remark": remark}) or {"order_id": "exit-2"}

        first = trading_monitor._try_single_leg_smart_exit(order, "paper")
        pending_status = order["single_leg_smart_exit_status"]
        result = trading_monitor._try_single_leg_smart_exit(order, "paper")

        self.assertEqual(first["triggered"], 0)
        self.assertEqual(pending_status, "smart_underlying_price_exit_pending_confirmation")
        self.assertEqual(result["triggered"], 1)
        self.assertTrue(submitted)
        self.assertEqual(order["single_leg_smart_exit_trigger"], "smart_underlying_price_exit")
        self.assertEqual(order["single_leg_smart_exit_reason"], "正股跌破 500 失效")


if __name__ == "__main__":
    unittest.main()

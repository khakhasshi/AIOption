from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from ai_option_scanner import trading_agent, trading_monitor
from ai_option_scanner.trading_instance import build_review_metrics


def _spread_position(*, allocation_pct: float = 1.0) -> dict:
    return {
        "tracking_id": "strategy-1",
        "symbol": "SPY",
        "family": "spread",
        "strategy_type": "bull_call_spread",
        "allocation_pct": allocation_pct,
        "capital_required": 43.0,
        "max_loss": 43.0,
        "net_debit": 0.43,
        "entry_mark": 43.0,
        "live_executable": True,
        "legs": [
            {"contract_symbol": "SPY260713C00753000", "action": "buy", "side": "call", "qty": 1, "price": 1.76},
            {"contract_symbol": "SPY260713C00754000", "action": "sell", "side": "call", "qty": 1, "price": 1.33},
        ],
    }


class StrategyPathRiskTest(unittest.TestCase):
    def test_units_are_capped_by_temporary_long_leg_exposure(self) -> None:
        position = _spread_position()
        quotes = {
            "SPY260713C00753000": {"available": True, "bid": 1.75, "ask": 1.76, "source": "longbridge_sdk"},
            "SPY260713C00754000": {"available": True, "bid": 1.33, "ask": 1.34, "source": "longbridge_sdk"},
        }
        submitted = []

        def submit(symbol, quantity, price, account, remark, order_type="market"):
            submitted.append((symbol, quantity))
            return {"order_id": f"o-{len(submitted)}"}

        with (
            mock.patch.object(trading_agent, "quote_option_contract", side_effect=lambda symbol, account=None: quotes[symbol]),
            mock.patch.object(trading_agent, "submit_buy_order", side_effect=submit),
            mock.patch.object(trading_agent, "submit_sell_order", side_effect=submit),
            mock.patch.object(trading_agent, "wait_for_order_fill", side_effect=lambda order_id, account, timeout=8: {"status": "filled", "executed_quantity": 1, "executed_price": 1.75 if order_id == "o-1" else 1.33}),
        ):
            result = trading_agent._submit_one_strategy_order(
                position,
                {"entry_order_type": "market", "total_capital": 300, "wait_for_fill_seconds": 1},
                "paper",
            )

        self.assertEqual(result["status"], "submitted", result)
        self.assertEqual(result["units"], 1)
        self.assertEqual(result["temporary_exposure_per_unit"], 176.0)
        self.assertEqual([quantity for _, quantity in submitted], [1, 1])

    def test_risk_plan_is_recomputed_from_executable_units(self) -> None:
        instance = {"risk_plan": {}, "execution_plan": {}, "ai_decision": {}}
        positions = [
            {"tracking_id": "a", "max_loss": 43.0},
            {"tracking_id": "b", "max_loss": 304.0},
        ]
        orders = [
            {"tracking_id": "a", "status": "submitted", "units": 2, "temporary_exposure_per_unit": 176.0},
            {"tracking_id": "b", "status": "skipped_insufficient_allocation", "units": 0, "temporary_exposure_per_unit": 558.0},
        ]

        trading_agent._refresh_strategy_plan_after_orders(instance, positions, orders)

        risk = instance["risk_plan"]
        self.assertEqual(risk["planned_strategy_count"], 2)
        self.assertEqual(risk["planned_units"], 2)
        self.assertEqual(risk["planned_premium_at_risk"], 86.0)
        self.assertEqual(risk["temporary_leg_exposure"], 352.0)

    def test_yfinance_quote_cannot_pass_live_strategy_gate(self) -> None:
        position = _spread_position()
        with mock.patch.object(
            trading_agent,
            "quote_option_contract",
            return_value={"available": True, "bid": 1.0, "ask": 1.1, "source": "yfinance", "execution_trusted": False},
        ):
            result = trading_agent._strategy_net_price_gate(position, position["legs"], "paper")

        self.assertFalse(result["passed"])
        self.assertTrue(any("untrusted execution quote source yfinance" in issue for issue in result["issues"]))


class StrategyMonitorSafetyTest(unittest.TestCase):
    def test_stop_has_entry_grace_and_two_cycle_confirmation(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        position = {"actual_entry_at": now}
        with mock.patch.dict(os.environ, {"AI_OPTION_STRATEGY_STOP_GRACE_SECONDS": "60", "AI_OPTION_STRATEGY_STOP_CONFIRM_CYCLES": "2"}):
            self.assertTrue(trading_monitor._strategy_stop_grace_active(position, {}))
            position["actual_entry_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            self.assertFalse(trading_monitor._strategy_stop_grace_active(position, {}))
            self.assertFalse(trading_monitor._strategy_pnl_trigger_confirmed(position, "stop", -30.0, -27.4))
            self.assertTrue(trading_monitor._strategy_pnl_trigger_confirmed(position, "stop", -31.0, -27.4))

    def test_complete_manual_flat_reconciles_broker_fills_and_pnl(self) -> None:
        opened = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        position = {
            "tracking_id": "strategy-1",
            "symbol": "SPY",
            "strategy_type": "bull_call_spread",
            "strategy_units": 1,
            "risk_tracking_active": True,
            "actual_entry_at": opened,
        }
        order = {
            "tracking_id": "strategy-1",
            "symbol": "SPY",
            "strategy_type": "bull_call_spread",
            "strategy_auto_execute": True,
            "strategy_entry_status": "submitted",
            "risk_tracking_active": True,
            "actual_entry_at": opened,
            "units": 1,
            "quantity": 1,
            "legs": [
                {
                    "order_id": "entry-long",
                    "entry_order": {"order_id": "entry-long"},
                    "status": "filled",
                    "filled_quantity": 1,
                    "quantity": 1,
                    "entry_price": 2.69,
                    "leg": {"contract_symbol": "SPY260715C00754000", "action": "buy", "qty": 1},
                },
                {
                    "order_id": "entry-short",
                    "entry_order": {"order_id": "entry-short"},
                    "status": "filled",
                    "filled_quantity": 1,
                    "quantity": 1,
                    "entry_price": 1.82,
                    "leg": {"contract_symbol": "SPY260715C00756000", "action": "sell", "qty": 1},
                },
            ],
        }
        instance = {
            "instance_id": "run-1",
            "owner_id": "owner-1",
            "started_at": opened,
            "lifecycle_state": "monitoring",
            "risk_plan": {"strategy_positions": [position]},
            "event_timeline": [],
        }
        executions = [
            {"trade_id": "t1", "order_id": "manual-long", "symbol": "SPY260715C754000.US", "quantity": "1", "price": "2.67", "trade_done_at": datetime.now(timezone.utc).isoformat()},
            {"trade_id": "t2", "order_id": "manual-short", "symbol": "SPY260715C756000.US", "quantity": "1", "price": "1.79", "trade_done_at": datetime.now(timezone.utc).isoformat()},
        ]

        def detail(order_id, account):
            return {"order_id": order_id, "side": "Sell" if order_id == "manual-long" else "Buy", "status": "Filled"}

        with (
            mock.patch.dict(os.environ, {"AI_OPTION_STRATEGY_POSITION_RECONCILE_GRACE_SECONDS": "0", "AI_OPTION_STRATEGY_FLAT_CONFIRM_CYCLES": "2"}),
            mock.patch.object(trading_monitor, "broker_executions", return_value=executions),
            mock.patch.object(trading_monitor, "order_detail", side_effect=detail),
        ):
            first = trading_monitor._reconcile_complete_strategy_position(instance, position, [order], [], "paper")
            second = trading_monitor._reconcile_complete_strategy_position(instance, position, [order], [], "paper")

        self.assertFalse(first["closed"])
        self.assertTrue(second["closed"])
        self.assertEqual(order["status"], "strategy_manual_exit_detected")
        self.assertEqual(order["strategy_realized_pnl"], 1.0)
        self.assertEqual(build_review_metrics([order])["realized_pnl"], 1.0)
        self.assertFalse(position["risk_tracking_active"])


class StrategyOrderAuditTest(unittest.TestCase):
    def test_strategy_journal_uses_real_run_and_owner(self) -> None:
        position = _spread_position()
        captured = []
        quotes = {
            "SPY260713C00753000": {"available": True, "bid": 1.75, "ask": 1.76, "source": "longbridge_sdk"},
            "SPY260713C00754000": {"available": True, "bid": 1.33, "ask": 1.34, "source": "longbridge_sdk"},
        }

        with (
            mock.patch.object(trading_agent, "quote_option_contract", side_effect=lambda symbol, account=None: quotes[symbol]),
            mock.patch.object(trading_agent, "find_recent_order_journal", return_value=[]),
            mock.patch.object(trading_agent, "record_order_journal", side_effect=lambda **kwargs: captured.append(kwargs)),
            mock.patch.object(trading_agent, "submit_buy_order", return_value={"order_id": "buy-1"}),
            mock.patch.object(trading_agent, "submit_sell_order", return_value={"order_id": "sell-1"}),
            mock.patch.object(trading_agent, "wait_for_order_fill", side_effect=lambda order_id, account, timeout=8: {"status": "filled", "executed_quantity": 1, "executed_price": 1.75 if order_id == "buy-1" else 1.33}),
        ):
            result = trading_agent._submit_one_strategy_order(
                position,
                {"entry_order_type": "market", "total_capital": 300},
                "paper",
                run_id="run-real",
                owner_id="owner-real",
            )

        self.assertEqual(result["status"], "submitted")
        self.assertTrue(captured)
        self.assertTrue(all(row.get("run_id") == "run-real" for row in captured))
        self.assertTrue(all(row.get("owner_id") == "owner-real" for row in captured))
        self.assertTrue(all(str(row.get("client_order_key") or "").startswith("run-real-") for row in captured))


if __name__ == "__main__":
    unittest.main()

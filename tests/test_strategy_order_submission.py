from __future__ import annotations

import os
import unittest

from ai_option_scanner import trading_agent, trading_instance_actions, trading_monitor
from ai_option_scanner.trading_instance import attach_order_results, build_protection_status, build_review_metrics, create_trade_instance, lifecycle_from_orders
from ai_option_scanner.trading_store import normalize_trading_config


class StrategyOrderSubmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orders: dict[str, int] = {}
        self._counter = 0
        self._orig_buy = trading_agent.submit_buy_order
        self._orig_sell = trading_agent.submit_sell_order
        self._orig_stop = trading_agent.submit_stop_sell_order
        self._orig_wait = trading_agent.wait_for_order_fill
        self._orig_positions = trading_agent.lb_positions
        self._orig_assets = trading_agent.lb_assets
        self._orig_quote_option_contract = trading_agent.quote_option_contract
        self.quote_prices: dict[str, float] = {}
        self.executed_prices: dict[str, float] = {}

        def submit(side: str, symbol: str, quantity: int, price, account_name, remark, order_type="market"):
            self._counter += 1
            order_id = f"{side}-{self._counter}"
            self._orders[order_id] = int(quantity)
            return {"order_id": order_id, "status": "submitted", "quantity": quantity, "symbol": symbol, "price": price, "order_type": order_type}

        trading_agent.submit_buy_order = lambda symbol, quantity, price, account_name, remark, order_type="market": submit("buy", symbol, quantity, price, account_name, remark, order_type)
        trading_agent.submit_sell_order = lambda symbol, quantity, price, account_name, remark, order_type="market": submit("sell", symbol, quantity, price, account_name, remark, order_type)
        trading_agent.submit_stop_sell_order = lambda symbol, quantity, stop_price, account_name, remark: {"order_id": f"stop-{symbol}", "status": "submitted", "quantity": quantity, "stop_price": stop_price}
        trading_agent.wait_for_order_fill = lambda order_id, account_name, timeout_seconds=8: self._fill_detail(order_id)
        trading_agent.lb_positions = lambda account_name: [{"symbol": "SPY.US", "available_quantity": 300}]
        trading_agent.lb_assets = lambda account_name, currency="USD": [{"buy_power": 100000}]
        trading_agent.quote_option_contract = lambda symbol, account_name=None: self._quote(symbol)

    def tearDown(self) -> None:
        trading_agent.submit_buy_order = self._orig_buy
        trading_agent.submit_sell_order = self._orig_sell
        trading_agent.submit_stop_sell_order = self._orig_stop
        trading_agent.wait_for_order_fill = self._orig_wait
        trading_agent.lb_positions = self._orig_positions
        trading_agent.lb_assets = self._orig_assets
        trading_agent.quote_option_contract = self._orig_quote_option_contract

    def _quote(self, symbol: str) -> dict:
        price = self.quote_prices.get(str(symbol)) or self.quote_prices.get(str(symbol).replace(".US", ""))
        if price is None:
            return {"available": False, "error": f"missing fake quote for {symbol}"}
        return {"available": True, "bid": price, "ask": price, "limit_price": price, "last_price": price, "pricing_source": "test"}

    def _fill_detail(self, order_id: str) -> dict:
        detail = {
            "status": "filled",
            "executed_quantity": self._orders.get(order_id, 0),
        }
        if order_id in self.executed_prices:
            detail["executed_price"] = self.executed_prices[order_id]
        return detail

    def _install_position_quotes(self, position: dict) -> None:
        for leg in position.get("legs") or []:
            symbol = str(leg.get("contract_symbol") or leg.get("option_symbol") or leg.get("symbol") or "")
            if not symbol:
                continue
            price = float(leg.get("price") or leg.get("ask") or leg.get("bid") or 0)
            self.quote_prices[symbol] = price

    def test_all_strategy_families_can_submit_with_valid_backing(self) -> None:
        config = {
            "entry_order_type": "limit",
            "strategy_unwind_on_failure": True,
            "wait_for_fill_seconds": 1,
            "total_capital": 300000,
        }
        for position in _strategy_positions():
            with self.subTest(position["strategy_type"]):
                self._install_position_quotes(position)
                result = trading_agent._submit_one_strategy_order(position, config, "paper")
                self.assertEqual(result["status"], "submitted", result)
                self.assertTrue(result["strategy_entry_order_ids"], result)
                self.assertEqual(len(result["legs"]), _option_leg_count(position))

    def test_leg_aliases_are_normalized_before_submit(self) -> None:
        position = _base_position(
            "spread",
            "alias_vertical",
            [
                {"symbol": "SPY260619C00500000", "side": "long", "quantity": 1, "ask": 3.0},
                {"option_symbol": "SPY260619C00505000", "side": "short", "quantity": 1, "bid": 1.4},
            ],
            capital_required=160,
            max_loss=160,
        )
        self._install_position_quotes(position)
        result = trading_agent._submit_one_strategy_order(position, {"entry_order_type": "limit", "total_capital": 10000}, "paper")
        self.assertEqual(result["status"], "submitted", result)

    def test_stock_backed_units_are_capped_by_available_shares(self) -> None:
        position = _base_position(
            "covered_call",
            "covered_call",
            [_leg("SPY260619C00510000", "sell", 1, 2.0)],
            capital_required=50000,
            max_loss=49000,
        )
        self._install_position_quotes(position)
        result = trading_agent._submit_one_strategy_order(position, {"entry_order_type": "limit", "total_capital": 1000000}, "paper")
        self.assertEqual(result["status"], "submitted", result)
        self.assertEqual(result["units"], 3)
        self.assertEqual(result["legs"][0]["quantity"], 3)

    def test_strategy_net_price_gate_blocks_debit_drift(self) -> None:
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            capital_required=160,
            max_loss=160,
        )
        self.quote_prices = {
            "SPY260619C00500000": 4.0,
            "SPY260619C00505000": 1.0,
        }
        result = trading_agent._submit_one_strategy_order(position, {"entry_order_type": "limit", "total_capital": 10000}, "paper")
        self.assertEqual(result["status"], "blocked_strategy_net_price_gate", result)
        self.assertIn("net_debit", result["message"])

    def test_strategy_net_price_gate_blocks_small_credit(self) -> None:
        position = _base_position(
            "iron_condor",
            "iron_condor",
            [_leg("SPY260619P00490000", "sell", 1, 1.4), _leg("SPY260619P00485000", "buy", 1, 0.8), _leg("SPY260619C00510000", "sell", 1, 1.2), _leg("SPY260619C00515000", "buy", 1, 0.7)],
            capital_required=300,
            max_loss=300,
            net_credit=1.1,
        )
        position["width"] = 5.0
        self.quote_prices = {
            "SPY260619P00490000": 0.9,
            "SPY260619P00485000": 0.8,
            "SPY260619C00510000": 0.9,
            "SPY260619C00515000": 0.8,
        }
        result = trading_agent._submit_one_strategy_order(position, {"entry_order_type": "limit", "total_capital": 300000}, "paper")
        self.assertEqual(result["status"], "blocked_strategy_net_price_gate", result)
        self.assertIn("credit", result["message"])

    def test_default_take_profit_pct_is_normalized_and_arms_single_leg_targets(self) -> None:
        config = normalize_trading_config({"default_take_profit_pct": 30})
        order = {
            "symbol": "SPY",
            "entry_price": 2.0,
            "take_profit_pct": config["default_take_profit_pct"],
            "candidate": {"risk_plan": {"take_profit_1": 9.0, "take_profit_2": 10.0}},
        }

        trading_agent._arm_software_take_profit(order, 1, config)

        self.assertTrue(order["software_take_profit_active"])
        self.assertEqual(order["software_take_profit_pct"], 30)
        self.assertEqual(order["software_take_profit_targets"], [{"name": "take_profit", "price": 2.6, "quantity": 1, "status": "pending"}])

    def test_strategy_default_take_profit_pct_sets_pnl_targets(self) -> None:
        opportunity = {
            "symbol": "SPY",
            "strategy_key": "SPY:spread",
            "candidate": _base_position(
                "spread",
                "bull_call_spread",
                [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
                capital_required=160,
                max_loss=160,
                net_credit=0.0,
            ),
        }
        config = {"top_n": 1, "total_capital": 10000, "default_stop_loss_pct": 25, "default_take_profit_pct": 30}

        positions = trading_agent._build_strategy_risk_positions([opportunity], config, [])

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["take_profit_pct"], 30)
        self.assertEqual(positions[0]["take_profit_1_pnl"], 48.0)
        self.assertEqual(positions[0]["take_profit_2_pnl"], 0.0)

    def test_strategy_risk_positions_carry_market_data_source_for_monitoring(self) -> None:
        positions = trading_agent._build_strategy_risk_positions(_strategy_positions(), {"top_n": 1, "total_capital": 10000, "market_data_source": "thetadata"}, [])

        self.assertEqual(positions[0]["market_data_source"], "thetadata")

    def test_debit_spread_take_profit_basis_is_premium_paid_not_max_profit(self) -> None:
        # Regression: a 20% take-profit on a debit spread must mean +20% on the
        # premium paid (capital deployed), NOT 20% of the spread's theoretical
        # max profit — which previously let a 20% setting ride to ~56% return.
        # Reproduces a real AMZN bear_put_spread: debit 3.26, width 12.47.
        position = {
            "tracking_id": "strategy-1",
            "strategy_type": "bear_put_spread",
            "family": "spread",
            "width": 12.47,
            "actual_net_debit": 3.26,
            "actual_net_credit": 0.0,
            "stop_loss_pct": 25,
            "take_profit_pct": 20,
        }
        config = {"default_stop_loss_pct": 25, "default_take_profit_pct": 20}
        trading_agent._reprice_strategy_risk_thresholds_from_actual_basis(position, config)
        # Premium paid = 3.26 * 100 = 326 → profit_basis = 326 (not max_profit 921).
        self.assertEqual(position["actual_profit_basis"], 326.0)
        self.assertEqual(position["actual_risk_basis"], 326.0)
        # 20% take-profit on $326 cost = $65.2 (was 184.2 = 20% of $921 max profit).
        self.assertEqual(position["take_profit_1_pnl"], 65.2)

    def test_credit_spread_stop_loss_basis_is_max_loss_not_credit_received(self) -> None:
        # Regression: a bull_put_spread built in generic "spread" mode carries
        # family="spread" (not "credit_spread"), so the old code missed it and
        # risk_basis fell through to abs(actual_entry_mark) = credit received,
        # roughly doubling the stop. Reproduces a real SPY bull_put_spread:
        # credit 2.68, width 3.93 → max loss = (3.93 - 2.68) * 100 = 125.
        position = {
            "tracking_id": "strategy-1",
            "strategy_type": "bull_put_spread",
            "family": "spread",
            "width": 3.93,
            "actual_net_debit": 0.0,
            "actual_net_credit": 2.68,
            "actual_entry_mark": -268.0,
            "max_loss": 125.0,
            "stop_loss_pct": 20,
            "take_profit_pct": 20,
        }
        config = {"default_stop_loss_pct": 20, "default_take_profit_pct": 20}
        trading_agent._reprice_strategy_risk_thresholds_from_actual_basis(position, config)
        # Risk basis = max loss 125 (NOT credit 268). 20% stop = -$25.0 (was -53.6).
        self.assertEqual(position["actual_risk_basis"], 125.0)
        self.assertEqual(position["stop_loss_pnl"], -25.0)
        # Take-profit basis stays the credit received (max profit) = 268 → 20% = +53.6.
        self.assertEqual(position["actual_profit_basis"], 268.0)
        self.assertEqual(position["take_profit_1_pnl"], 53.6)

    def test_strategy_tiered_take_profit_sets_two_targets(self) -> None:
        opportunity = {
            "symbol": "SPY",
            "strategy_key": "SPY:spread:tiered",
            "candidate": _base_position(
                "spread",
                "bull_call_spread",
                [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
                capital_required=160,
                max_loss=160,
                net_credit=0.0,
            ),
        }
        config = {
            "top_n": 1,
            "total_capital": 10000,
            "default_stop_loss_pct": 25,
            "tiered_take_profit_enabled": True,
            "default_take_profit_1_pct": 20,
            "default_take_profit_2_pct": 35,
        }

        positions = trading_agent._build_strategy_risk_positions([opportunity], config, [])

        self.assertEqual(len(positions), 1)
        self.assertTrue(positions[0]["tiered_take_profit_enabled"])
        self.assertEqual(positions[0]["take_profit_1_pnl"], 32.0)
        self.assertEqual(positions[0]["take_profit_2_pnl"], 56.0)

    def test_strategy_filled_legs_rewrite_actual_risk_basis(self) -> None:
        position = _base_position(
            "calendar",
            "call_calendar_spread",
            [_leg("GOOGL260513C00387500", "sell", 1, 2.40), _leg("GOOGL260518C00387500", "buy", 1, 5.95)],
            capital_required=355,
            max_loss=355,
        )
        self.quote_prices = {
            "GOOGL260513C00387500": 2.40,
            "GOOGL260518C00387500": 5.95,
        }
        self.executed_prices = {"buy-1": 4.10, "sell-2": 1.55}

        orders = trading_agent._submit_strategy_orders([position], {"entry_order_type": "limit", "total_capital": 3000, "default_stop_loss_pct": 25, "default_take_profit_pct": 30}, "paper")

        self.assertEqual(orders[0]["status"], "submitted", orders[0])
        self.assertEqual(position["actual_net_debit"], 2.55)
        self.assertEqual(position["entry_mark"], 255.0)
        self.assertEqual(position["stop_loss_pnl"], -63.75)
        by_symbol = {leg["contract_symbol"]: leg for leg in position["legs"]}
        self.assertEqual(by_symbol["GOOGL260518C00387500"]["price"], 4.1)
        self.assertEqual(by_symbol["GOOGL260513C00387500"]["price"], 1.55)
        self.assertEqual(by_symbol["GOOGL260518C00387500"]["planned_entry_price"], 5.95)

        original_quote = trading_monitor._monitor_option_quote
        trading_monitor._monitor_option_quote = lambda symbol, account_name=None, market_data_source=None: {
            "available": True,
            "price": 4.80 if "260518" in str(symbol) else 2.15,
            "exit_price": 4.80 if "260518" in str(symbol) else 2.15,
        }
        try:
            mark = trading_monitor._strategy_position_mark(position, "paper")
        finally:
            trading_monitor._monitor_option_quote = original_quote
        self.assertTrue(mark["available"], mark)
        self.assertEqual(position["strategy_units"], 1)
        self.assertEqual(round(mark["pnl"], 2), 10.0)

    def test_inter_leg_recheck_breaches_when_remaining_leg_quote_moves_adversely(self) -> None:
        # bull_call_spread: buy 500C @3.0, sell 505C @1.4 → planned net debit 1.6.
        # The buy leg fills first; before the sell leg, its bid collapses so the
        # combined net debit balloons → recheck must breach and the filled buy leg
        # must land in residual tracking rather than completing a bad combo.
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            160,
            160,
        )
        self.quote_prices = {"SPY260619C00500000": 3.0, "SPY260619C00505000": 1.4}
        self.executed_prices = {"buy-1": 3.0}

        buy_filled = {"done": False}
        orig_quote = trading_agent.quote_option_contract
        orig_buy = trading_agent.submit_buy_order

        def buy(symbol, quantity, price, account_name, remark, order_type="market"):
            buy_filled["done"] = True
            return orig_buy(symbol, quantity, price, account_name, remark, order_type=order_type)

        def quote(symbol, account_name=None):
            row = self._quote(symbol)
            # After the buy leg fills, the sell leg's bid has collapsed to 0.10.
            if buy_filled["done"] and "00505000" in str(symbol):
                return {**row, "bid": 0.10, "ask": 0.20, "limit_price": 0.10, "last_price": 0.10}
            return row

        trading_agent.submit_buy_order = lambda symbol, quantity, price, account_name, remark, order_type="market": buy(symbol, quantity, price, account_name, remark, order_type)
        trading_agent.quote_option_contract = quote
        try:
            orders = trading_agent._submit_strategy_orders([position], {"entry_order_type": "market", "total_capital": 10000, "strategy_unwind_on_failure": False}, "paper")
        finally:
            trading_agent.quote_option_contract = orig_quote
            trading_agent.submit_buy_order = orig_buy

        order = orders[0]
        self.assertEqual(order["status"], "strategy_residual_tracking", order)
        self.assertTrue(order.get("strategy_inter_leg_rechecks"), order)
        self.assertTrue(order["strategy_inter_leg_rechecks"][-1]["issues"], order["strategy_inter_leg_rechecks"])
        # Only the buy leg was submitted; the sell leg was never sent.
        self.assertEqual(len(order["legs"]), 1)
        self.assertEqual(order["legs"][0]["leg"]["action"], "buy")

    def test_inter_leg_recheck_breach_unwinds_filled_leg_to_flat(self) -> None:
        # Same breach as above, but with unwind ON (default): the filled buy leg
        # must be closed by an unwind market order and the order reconciled to a
        # flat, non-residual terminal state — NOT left stuck in residual tracking
        # / manual attention after the close fills. (Regression: TRD-AA3C17485EE5
        # — unwind submitted the close but never reconciled, leaving a ghost
        # residual that pinned the run in manual_attention forever.)
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            160,
            160,
        )
        self.quote_prices = {"SPY260619C00500000": 3.0, "SPY260619C00505000": 1.4}
        # buy leg fills @3.0; the unwind sell (sell-N) fills @2.7 → realized loss.
        self.executed_prices = {"buy-1": 3.0}

        buy_filled = {"done": False}
        orig_quote = trading_agent.quote_option_contract
        orig_buy = trading_agent.submit_buy_order
        orig_wait = trading_agent.wait_for_order_fill

        def buy(symbol, quantity, price, account_name, remark, order_type="market"):
            buy_filled["done"] = True
            return orig_buy(symbol, quantity, price, account_name, remark, order_type=order_type)

        def quote(symbol, account_name=None):
            row = self._quote(symbol)
            if buy_filled["done"] and "00505000" in str(symbol):
                return {**row, "bid": 0.10, "ask": 0.20, "limit_price": 0.10, "last_price": 0.10}
            return row

        def wait_for_fill(order_id, account_name, timeout_seconds=8):
            detail = self._fill_detail(order_id)
            if str(order_id).startswith("sell-"):
                detail["executed_price"] = 2.7  # unwind close fill
            return detail

        trading_agent.submit_buy_order = lambda symbol, quantity, price, account_name, remark, order_type="market": buy(symbol, quantity, price, account_name, remark, order_type)
        trading_agent.quote_option_contract = quote
        trading_agent.wait_for_order_fill = wait_for_fill
        # Legacy dump-the-long-leg behavior is now opt-in; this regression test
        # asserts that path, so enable it explicitly.
        os.environ["AI_OPTION_STRATEGY_UNWIND_LONG_LEGS"] = "true"
        try:
            orders = trading_agent._submit_strategy_orders([position], {"entry_order_type": "market", "total_capital": 10000, "strategy_unwind_on_failure": True}, "paper")
        finally:
            trading_agent.quote_option_contract = orig_quote
            trading_agent.submit_buy_order = orig_buy
            trading_agent.wait_for_order_fill = orig_wait
            os.environ.pop("AI_OPTION_STRATEGY_UNWIND_LONG_LEGS", None)

        order = orders[0]
        # The recheck still breached and only the buy leg was ever submitted.
        self.assertTrue(order["strategy_inter_leg_rechecks"][-1]["issues"], order)
        self.assertEqual(len(order["legs"]), 1)
        # Unwind closed the filled buy leg and confirmed the fill.
        self.assertTrue(order.get("unwind"), order)
        self.assertTrue(order["unwind"][0].get("confirmed"), order["unwind"])
        self.assertEqual(order["unwind"][0]["side"], "sell")
        # Reconciled to flat: no residual tracking, not stuck in manual attention.
        self.assertEqual(order["status"], "failed", order)
        self.assertFalse(order.get("residual_leg_tracking_active"), order)
        self.assertEqual(order.get("residual_leg_quantity"), 0)
        self.assertEqual(order.get("strategy_exit_status"), "filled", order)
        # Fill ledger reflects the realized loss (entry 3.0 → exit 2.7, 12 units)
        # and zero open.
        ledger = order.get("strategy_fill_ledger") or {}
        self.assertEqual(ledger.get("open_units"), 0, ledger)
        self.assertEqual(order.get("strategy_realized_pnl"), -180.0, order)

        protection = build_protection_status([order])
        self.assertNotEqual(protection["state"], "strategy_residual_tracking", protection)
        self.assertFalse(protection["requires_manual_attention"], protection)
        self.assertEqual(lifecycle_from_orders([order]), "closed", order)

    def test_inter_leg_recheck_passes_when_remaining_leg_quote_holds(self) -> None:
        # Same spread, but the sell leg quote holds at plan → recheck passes and
        # both legs submit normally.
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            160,
            160,
        )
        self.quote_prices = {"SPY260619C00500000": 3.0, "SPY260619C00505000": 1.4}

        orders = trading_agent._submit_strategy_orders([position], {"entry_order_type": "market", "total_capital": 10000}, "paper")

        order = orders[0]
        self.assertEqual(order["status"], "submitted", order)
        self.assertEqual(len(order["legs"]), 2)

    def test_inter_leg_recheck_uses_mid_not_raw_bid_avoiding_false_breach(self) -> None:
        # Fix A: the recheck values the remaining leg at MID, not the spread-
        # crossing bid. Buy 500C @3.0, sell 505C plan 1.4 → net debit 1.6. After
        # the buy fills, the sell leg widens to bid 1.0 / ask 1.8 (mid 1.4, a
        # normal quote — same fair value, just a wide book). Raw-bid math would
        # read net = 3.0 - 1.0 = 2.0 and FALSELY breach (>1.84 tolerance); mid math
        # reads 3.0 - 1.4 = 1.6 and correctly passes, so both legs submit.
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            160,
            160,
        )
        self.quote_prices = {"SPY260619C00500000": 3.0, "SPY260619C00505000": 1.4}
        self.executed_prices = {"buy-1": 3.0}

        buy_filled = {"done": False}
        orig_quote = trading_agent.quote_option_contract
        orig_buy = trading_agent.submit_buy_order

        def buy(symbol, quantity, price, account_name, remark, order_type="market"):
            buy_filled["done"] = True
            return orig_buy(symbol, quantity, price, account_name, remark, order_type=order_type)

        def quote(symbol, account_name=None):
            row = self._quote(symbol)
            if buy_filled["done"] and "00505000" in str(symbol):
                return {**row, "bid": 1.0, "ask": 1.8, "limit_price": 1.4, "last_price": 1.4}
            return row

        trading_agent.submit_buy_order = lambda symbol, quantity, price, account_name, remark, order_type="market": buy(symbol, quantity, price, account_name, remark, order_type)
        trading_agent.quote_option_contract = quote
        try:
            orders = trading_agent._submit_strategy_orders([position], {"entry_order_type": "market", "total_capital": 10000, "strategy_unwind_on_failure": True}, "paper")
        finally:
            trading_agent.quote_option_contract = orig_quote
            trading_agent.submit_buy_order = orig_buy

        order = orders[0]
        recheck = order["strategy_inter_leg_rechecks"][-1]
        self.assertFalse(recheck["issues"], recheck)
        self.assertEqual(order["status"], "submitted", order)
        self.assertEqual(len(order["legs"]), 2)

    def test_inter_leg_recheck_bad_tick_suppresses_breach(self) -> None:
        # Fix B: a one-sided / crossed tick on the remaining leg must NOT declare a
        # breach (prod META/TSLA: a bid that collapsed while the ask held). Here the
        # sell leg returns a one-sided quote (ask missing) after the buy fills — it
        # is recorded as a quote error, issues stay empty, and the combo proceeds
        # instead of unwinding on garbage data.
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            160,
            160,
        )
        self.quote_prices = {"SPY260619C00500000": 3.0, "SPY260619C00505000": 1.4}
        self.executed_prices = {"buy-1": 3.0}

        buy_filled = {"done": False}
        orig_quote = trading_agent.quote_option_contract
        orig_buy = trading_agent.submit_buy_order

        def buy(symbol, quantity, price, account_name, remark, order_type="market"):
            buy_filled["done"] = True
            return orig_buy(symbol, quantity, price, account_name, remark, order_type=order_type)

        def quote(symbol, account_name=None):
            row = self._quote(symbol)
            if buy_filled["done"] and "00505000" in str(symbol):
                # One-sided: bid collapsed, ask absent → untrustworthy.
                return {"available": True, "bid": 0.10, "ask": 0.0, "limit_price": 0.10, "last_price": 0.10}
            return row

        trading_agent.submit_buy_order = lambda symbol, quantity, price, account_name, remark, order_type="market": buy(symbol, quantity, price, account_name, remark, order_type)
        trading_agent.quote_option_contract = quote
        try:
            orders = trading_agent._submit_strategy_orders([position], {"entry_order_type": "market", "total_capital": 10000, "strategy_unwind_on_failure": True}, "paper")
        finally:
            trading_agent.quote_option_contract = orig_quote
            trading_agent.submit_buy_order = orig_buy

        order = orders[0]
        recheck = order["strategy_inter_leg_rechecks"][-1]
        self.assertTrue(recheck["quote_errors"], recheck)
        self.assertFalse(recheck["issues"], recheck)
        self.assertEqual(order["status"], "submitted", order)
        # No unwind happened — the filled buy leg was not dumped.
        self.assertFalse(order.get("unwind"), order)

    def test_genuine_breach_holds_long_leg_as_protected_residual_by_default(self) -> None:
        # Fix C: on a REAL breach (trusted quote, normal spread) with unwind ON but
        # the legacy long-dump flag OFF (default), the filled defined-risk LONG leg
        # is NOT market-dumped for a certain spread loss — it is held as a protected
        # single-leg residual with a software stop armed. Only naked shorts unwind.
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            160,
            160,
        )
        self.quote_prices = {"SPY260619C00500000": 3.0, "SPY260619C00505000": 1.4}
        self.executed_prices = {"buy-1": 3.0}

        buy_filled = {"done": False}
        orig_quote = trading_agent.quote_option_contract
        orig_buy = trading_agent.submit_buy_order

        def buy(symbol, quantity, price, account_name, remark, order_type="market"):
            buy_filled["done"] = True
            return orig_buy(symbol, quantity, price, account_name, remark, order_type=order_type)

        def quote(symbol, account_name=None):
            row = self._quote(symbol)
            # Real adverse move: tight, trustworthy book that still breaches (mid 0.15).
            if buy_filled["done"] and "00505000" in str(symbol):
                return {**row, "bid": 0.10, "ask": 0.20, "limit_price": 0.15, "last_price": 0.15}
            return row

        trading_agent.submit_buy_order = lambda symbol, quantity, price, account_name, remark, order_type="market": buy(symbol, quantity, price, account_name, remark, order_type)
        trading_agent.quote_option_contract = quote
        try:
            orders = trading_agent._submit_strategy_orders([position], {"entry_order_type": "market", "total_capital": 10000, "strategy_unwind_on_failure": True}, "paper")
        finally:
            trading_agent.quote_option_contract = orig_quote
            trading_agent.submit_buy_order = orig_buy

        order = orders[0]
        self.assertTrue(order["strategy_inter_leg_rechecks"][-1]["issues"], order)
        self.assertEqual(order["status"], "strategy_residual_tracking", order)
        self.assertTrue(order.get("residual_leg_tracking_active"), order)
        # The long leg was HELD, not unwound.
        self.assertFalse(order.get("unwind"), order)
        self.assertEqual(len(order["legs"]), 1)
        self.assertEqual(order["legs"][0]["leg"]["action"], "buy")
        # A software stop is armed on the held long leg.
        self.assertTrue(order.get("software_stop_active"), order)

    def test_single_leg_filled_order_rewrites_limit_entry_basis(self) -> None:
        self.quote_prices = {"SPY260619C00500000": 2.0}
        self.executed_prices = {"buy-1": 1.8}
        selection = {
            "symbol": "SPY",
            "contract_symbol": "SPY260619C00500000",
            "order_symbol": "SPY260619C00500000.US",
            "entry_price": 2.0,
            "allocation_pct": 0.01,
            "stop_loss_pct": 25,
            "take_profit_pct": 30,
        }

        orders = trading_agent._submit_orders([selection], {"entry_order_type": "limit", "total_capital": 100000, "wait_for_fill_seconds": 1, "software_take_profit_enabled": True}, "paper")

        self.assertEqual(orders[0]["status"], "submitted", orders[0])
        self.assertEqual(orders[0]["planned_entry_price"], 2.0)
        self.assertEqual(orders[0]["entry_price"], 1.8)
        self.assertEqual(orders[0]["entry_price_source"], "executed_price")
        self.assertEqual(orders[0]["stop_trigger_price"], 1.35)
        self.assertEqual(orders[0]["software_take_profit_targets"][0]["price"], 2.34)

    def test_single_leg_order_carries_market_data_source_for_monitoring(self) -> None:
        self.quote_prices = {"SPY260619C00500000": 2.0}
        self.executed_prices = {"buy-1": 1.8}
        selection = {
            "symbol": "SPY",
            "contract_symbol": "SPY260619C00500000",
            "order_symbol": "SPY260619C00500000.US",
            "entry_price": 2.0,
            "allocation_pct": 0.01,
            "stop_loss_pct": 25,
            "take_profit_pct": 30,
        }

        orders = trading_agent._submit_orders([selection], {"entry_order_type": "market", "total_capital": 100000, "wait_for_fill_seconds": 1, "market_data_source": "thetadata"}, "paper")

        self.assertEqual(orders[0]["market_data_source"], "thetadata")

    def test_strategy_instance_events_follow_action_order(self) -> None:
        config = {"entry_order_type": "limit", "total_capital": 10000, "software_stop_enabled": True, "software_take_profit_enabled": True}
        position = _base_position(
            "spread",
            "bull_call_spread",
            [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)],
            capital_required=160,
            max_loss=160,
        )
        instance = create_trade_instance("TRD-TEST", "owner", config)

        trading_agent._attach_strategy_decision_and_plan(
            instance,
            {"summary": "ok", "council_mode": "strategy_score_rank"},
            [position],
            [{"strategy_key": "SPY:spread", "candidate": position}],
            {"source": "system_default", "risk_notes": []},
            config,
            strategy_auto_execute=True,
            manual_review_required=False,
        )
        trading_agent._refresh_strategy_plan_after_orders(
            instance,
            [position],
            [{"status": "submitted", "quantity": 1, "entry_filled_quantity": 1, "risk_tracking_active": True, "strategy_auto_execute": True}],
        )
        attach_order_results(instance, instance["execution_plan"]["strategy_orders"])

        event_types = [event["event_type"] for event in instance["event_timeline"]]
        self.assertLess(event_types.index("strategy_decision"), event_types.index("strategy_risk_plan"))
        self.assertLess(event_types.index("strategy_risk_plan"), event_types.index("orders_submitted"))
        self.assertEqual(instance["risk_plan"]["strategy_tracking_count"], 1)
        self.assertEqual(instance["protection_status"]["state"], "strategy_protected")

    def test_blocked_strategy_order_marks_no_position_in_lifecycle(self) -> None:
        orders = [
            {
                "status": "blocked_strategy_net_price_gate",
                "strategy_auto_execute": True,
                "quantity": 17,
                "entry_filled_quantity": 0,
                "risk_tracking_active": False,
            },
            {
                "status": "blocked_strategy_net_price_gate",
                "strategy_auto_execute": True,
                "quantity": 20,
                "entry_filled_quantity": 0,
                "risk_tracking_active": False,
            },
        ]

        self.assertEqual(build_protection_status(orders)["state"], "no_position")
        self.assertEqual(lifecycle_from_orders(orders), "blocked")

    def test_blocked_strategy_orders_make_auto_execute_run_failed(self) -> None:
        outcome = trading_agent._strategy_auto_execute_outcome(
            [
                {"status": "blocked_strategy_net_price_gate", "strategy_auto_execute": True, "quantity": 14},
                {"status": "blocked_strategy_net_price_gate", "strategy_auto_execute": True, "quantity": 26},
            ]
        )

        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["stage"], "strategy_no_execution")

    def test_mixed_strategy_tracking_and_exit_marks_partial_exiting(self) -> None:
        orders = [
            {
                "status": "submitted",
                "strategy_auto_execute": True,
                "risk_tracking_active": True,
                "quantity": 49,
                "entry_filled_quantity": 49,
            },
            {
                "status": "strategy_auto_exit_submitted",
                "strategy_auto_execute": True,
                "risk_tracking_active": False,
                "strategy_exit_status": "submitted",
                "quantity": 39,
                "entry_filled_quantity": 39,
            },
        ]

        protection = build_protection_status(orders)

        self.assertEqual(protection["state"], "strategy_partial_exiting")
        self.assertEqual(protection["strategy_tracked_quantity"], 49)
        self.assertEqual(protection["strategy_exit_submitted_quantity"], 39)
        self.assertEqual(lifecycle_from_orders(orders), "exiting")

    def test_failed_strategy_with_filled_leg_marks_residual_tracking(self) -> None:
        orders = [
            {
                "status": "failed",
                "strategy_auto_execute": True,
                "strategy_entry_status": "failed",
                "quantity": 3,
                "legs": [
                    {
                        "status": "filled",
                        "filled_quantity": 3,
                        "quantity": 3,
                        "leg": {"contract_symbol": "NVDA260529C00225000", "action": "buy"},
                    },
                    {
                        "status": "unfilled",
                        "filled_quantity": 0,
                        "quantity": 3,
                        "error": "leg not confirmed filled",
                        "leg": {"contract_symbol": "NVDA260522C00225000", "action": "sell"},
                    },
                ],
            }
        ]

        protection = build_protection_status(orders)
        outcome = trading_agent._strategy_auto_execute_outcome(orders)

        self.assertEqual(protection["state"], "strategy_residual_tracking")
        self.assertTrue(protection["requires_manual_attention"])
        self.assertEqual(protection["strategy_residual_tracking_quantity"], 3)
        self.assertEqual(lifecycle_from_orders(orders), "monitoring")
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["stage"], "strategy_manual_attention")

    def test_strategy_residual_tracking_quantity_uses_actual_fill_not_target_quantity(self) -> None:
        orders = [
            {
                "status": "strategy_residual_tracking",
                "strategy_auto_execute": True,
                "strategy_entry_status": "failed",
                "quantity": 10,
                "residual_leg_tracking_active": True,
                "residual_leg_quantity": 3,
                "entry_filled_quantity": 3,
                "legs": [
                    {
                        "status": "filled",
                        "filled_quantity": 3,
                        "quantity": 10,
                        "leg": {"contract_symbol": "MSFT260529C00450000", "action": "buy"},
                    }
                ],
            }
        ]

        protection = build_protection_status(orders)

        self.assertEqual(protection["state"], "strategy_residual_tracking")
        self.assertEqual(protection["strategy_residual_tracking_quantity"], 3)

    def test_strategy_submit_failure_after_opened_leg_records_residual(self) -> None:
        position = _base_position(
            "calendar",
            "call_calendar_spread",
            [_leg("SPY260619C00500000", "sell", 1, 1.4), _leg("SPY260717C00500000", "buy", 1, 3.2)],
            capital_required=180,
            max_loss=180,
        )
        self._install_position_quotes(position)

        def wait_for_fill(order_id: str, account_name: str, timeout_seconds: int = 8) -> dict:
            if order_id == "buy-1":
                return {"status": "filled", "executed_quantity": 2, "executed_price": 3.1}
            return {"status": "rejected", "executed_quantity": 0, "executed_price": 0}

        trading_agent.wait_for_order_fill = wait_for_fill

        result = trading_agent._submit_one_strategy_order(
            position,
            {"entry_order_type": "limit", "total_capital": 10000, "strategy_unwind_on_failure": False},
            "paper",
        )

        self.assertEqual(result["status"], "strategy_residual_tracking", result)
        self.assertTrue(result["residual_leg_tracking_active"])
        self.assertEqual(result["residual_leg_quantity"], 2)
        self.assertEqual(result["residual_legs"][0]["contract_symbol"], "SPY260717C00500000")
        # gap #2: the residual must carry top-level identity so the monitor's
        # _try_residual_position_reconcile can find the contract and auto-heal
        # when it goes flat, and must arm a software stop so the long leg is not
        # left unprotected while "tracking".
        self.assertEqual(result["residual_leg_contract_symbol"], "SPY260717C00500000")
        self.assertEqual(result["contract_symbol"], "SPY260717C00500000")
        self.assertTrue(result.get("order_symbol"))
        self.assertTrue(result.get("software_stop_active"), result)
        self.assertEqual(result.get("software_stop_status"), "armed")
        self.assertGreater(float(result.get("stop_trigger_price") or 0), 0)
        # And it must not be flagged for manual attention (it is protected now).
        protection = build_protection_status([result])
        self.assertFalse(protection["requires_manual_attention"], protection)
        self.assertEqual(lifecycle_from_orders([result]), "monitoring", result)

    def test_strategy_multi_leg_residual_requires_broker_combo_close(self) -> None:
        # Execution order is always buys-then-sells, so a 2-leg spread can only
        # ever leave a single LONG residual. A multi-leg residual (which cannot
        # be closed safely leg-by-leg) needs 3+ legs where ≥2 longs fill before a
        # later leg rejects. Two long legs fill, the short leg is rejected →
        # residual = two longs → must route to broker_combo_close_required.
        position = _base_position(
            "spread",
            "call_ratio_spread",
            [
                _leg("SPY260619C00500000", "buy", 1, 3.0),
                _leg("SPY260619C00505000", "buy", 1, 1.4),
                _leg("SPY260619C00510000", "sell", 2, 0.6),
            ],
            160,
            160,
        )
        self._install_position_quotes(position)

        def wait_for_fill(order_id: str, account_name: str, timeout_seconds: int = 8) -> dict:
            if str(order_id).startswith("buy-"):
                return {"status": "filled", "executed_quantity": 12, "executed_price": 3.0}
            return {"status": "rejected", "executed_quantity": 0, "executed_price": 0}

        trading_agent.wait_for_order_fill = wait_for_fill

        result = trading_agent._submit_one_strategy_order(
            position,
            {"entry_order_type": "limit", "total_capital": 10000, "strategy_unwind_on_failure": False},
            "paper",
        )

        self.assertGreaterEqual(len(result["residual_legs"]), 2, result)
        self.assertEqual(result["status"], "broker_combo_close_required", result)
        self.assertTrue(result.get("broker_combo_close_required"))
        self.assertTrue(result.get("residual_leg_tracking_active"))
        self.assertEqual(lifecycle_from_orders([result]), "manual_intervention_required", result)

    def test_strategy_review_keeps_open_quantity_for_residual_short_after_unwind(self) -> None:
        orders = [
            {
                "tracking_id": "strategy-1",
                "symbol": "AMZN",
                "status": "strategy_residual_tracking",
                "strategy_auto_execute": True,
                "quantity": 23,
                "entry_filled_quantity": 23,
                "residual_leg_tracking_active": True,
                "residual_leg_quantity": 23,
                "residual_legs": [
                    {
                        "contract_symbol": "AMZN260605C00245000",
                        "order_symbol": "AMZN260605C245000.US",
                        "action": "sell",
                        "filled_quantity": 23,
                        "entry_price": 5.05,
                        "order_id": "sell-245",
                    }
                ],
                "legs": [
                    {
                        "status": "filled",
                        "order_id": "buy-2475",
                        "filled_quantity": 23,
                        "quantity": 23,
                        "entry_price": 3.3,
                        "entry_detail": {"status": "Filled", "executed_quantity": 23, "executed_price": 3.3},
                        "strategy_exit_status": "filled",
                        "strategy_exit_quantity": 23,
                        "strategy_exit_filled_quantity": 23,
                        "strategy_exit_detail": {"status": "Filled", "executed_quantity": 23, "executed_price": 3.3},
                        "leg": {"contract_symbol": "AMZN260605C00247500", "action": "buy", "qty": 1},
                    },
                    {
                        "status": "filled",
                        "order_id": "sell-245",
                        "filled_quantity": 23,
                        "quantity": 23,
                        "entry_price": 5.05,
                        "entry_detail": {"status": "Filled", "executed_quantity": 23, "executed_price": 5.05},
                        "leg": {"contract_symbol": "AMZN260605C00245000", "action": "sell", "qty": 1},
                    },
                ],
            }
        ]

        metrics = build_review_metrics(orders)
        protection = build_protection_status(orders)

        self.assertEqual(metrics["open_quantity"], 23)
        self.assertEqual(metrics["entry_cost"], 4025.0)
        self.assertEqual(metrics["realized_pnl"], 0.0)
        self.assertEqual(protection["state"], "strategy_residual_tracking")
        self.assertEqual(protection["strategy_residual_tracking_quantity"], 23)

    def test_strategy_review_uses_broker_executed_exit_prices(self) -> None:
        orders = [
            {
                "tracking_id": "strategy-qqq",
                "status": "strategy_auto_exit_filled",
                "strategy_auto_execute": True,
                "strategy_exit_status": "filled",
                "quantity": 105,
                "entry_filled_quantity": 105,
                "strategy_exit_trigger_mark_pnl": -5880.0,
                "legs": [
                    {
                        "leg": {"contract_symbol": "QQQ260514C00705000", "action": "buy", "side": "call", "qty": 1, "price": 9.89},
                        "filled_quantity": 105,
                        "quantity": 105,
                        "entry_price": 9.89,
                        "strategy_exit_status": "filled",
                        "strategy_exit_quantity": 105,
                        "strategy_exit_filled_quantity": 105,
                        "strategy_exit_detail": {"status": "Filled", "executed_quantity": "105", "executed_price": "9.6600"},
                    },
                    {
                        "leg": {"contract_symbol": "QQQ260514C00715000", "action": "buy", "side": "call", "qty": 1, "price": 3.06},
                        "filled_quantity": 105,
                        "quantity": 105,
                        "entry_price": 3.06,
                        "strategy_exit_status": "filled",
                        "strategy_exit_quantity": 105,
                        "strategy_exit_filled_quantity": 105,
                        "strategy_exit_detail": {"status": "Filled", "executed_quantity": "105", "executed_price": "3.2200"},
                    },
                    {
                        "leg": {"contract_symbol": "QQQ260514C00710000", "action": "sell", "side": "call", "qty": 2, "price": 6.07},
                        "filled_quantity": 210,
                        "quantity": 210,
                        "entry_price": 6.07,
                        "strategy_exit_status": "filled",
                        "strategy_exit_quantity": 210,
                        "strategy_exit_filled_quantity": 210,
                        "strategy_exit_detail": {"status": "Filled", "executed_quantity": "210", "executed_price": "6.0500"},
                    },
                ],
            }
        ]

        metrics = build_review_metrics(orders)
        protection = build_protection_status(orders)

        self.assertEqual(metrics["strategy_trigger_mark_pnl"], -5880.0)
        self.assertEqual(metrics["strategy_realized_pnl"], -315.0)
        self.assertEqual(metrics["realized_pnl"], -315.0)
        self.assertEqual(metrics["estimated_total_pnl"], -315.0)
        self.assertEqual(metrics["closed_quantity"], 105)
        self.assertEqual(protection["state"], "strategy_exited")
        self.assertEqual(lifecycle_from_orders(orders), "closed")

    def test_single_leg_review_uses_broker_exit_fill_not_trigger_quote(self) -> None:
        order = {
            "status": "software_take_profit_filled",
            "symbol": "SPY",
            "contract_symbol": "SPY260619C00500000",
            "order_symbol": "SPY260619C00500000.US",
            "quantity": 2,
            "entry_filled_quantity": 2,
            "entry_price": 1.0,
            "actual_entry_price": 1.0,
            "software_take_profit_closed_quantity": 2,
            "software_take_profit_targets": [
                {
                    "name": "take_profit",
                    "status": "filled",
                    "quantity": 2,
                    "filled_quantity": 2,
                    "trigger_quote": 1.50,
                    "executed_price": 1.42,
                }
            ],
        }

        metrics = build_review_metrics([order])

        self.assertEqual(metrics["realized_pnl"], 84.0)
        self.assertEqual(metrics["estimated_total_pnl"], 84.0)
        self.assertEqual(metrics["pnl_basis"], "broker_confirmed")

    def test_single_leg_review_uses_broker_entry_fill_before_planned_entry(self) -> None:
        order = {
            "status": "software_stop_filled",
            "symbol": "SPY",
            "contract_symbol": "SPY260619C00500000",
            "order_symbol": "SPY260619C00500000.US",
            "quantity": 1,
            "entry_filled_quantity": 1,
            "entry_price": 1.0,
            "entry_detail": {"status": "filled", "executed_quantity": 1, "filled_avg_price": 1.20},
            "software_stop_closed_quantity": 1,
            "software_stop_exit_detail": {"status": "filled", "executed_quantity": 1, "filled_avg_price": 0.95},
        }

        metrics = build_review_metrics([order])

        self.assertEqual(metrics["entry_cost"], 120.0)
        self.assertEqual(metrics["realized_pnl"], -25.0)
        self.assertEqual(metrics["pnl_basis"], "broker_confirmed")

    def test_single_leg_smart_exit_counts_as_closed_lot_in_review_metrics(self) -> None:
        orders = [
            {
                "status": "single_leg_smart_exit_submitted",
                "symbol": "SPY",
                "order_symbol": "SPY260619C00500000.US",
                "quantity": 2,
                "entry_filled_quantity": 2,
                "entry_price": 1.0,
                "single_leg_smart_exit_closed_quantity": 2,
                "single_leg_smart_exit_trigger_quote": 1.5,
                "single_leg_smart_exit_triggered_at": "2026-05-23T20:00:00+00:00",
            }
        ]

        metrics = build_review_metrics(orders)

        self.assertEqual(metrics["open_quantity"], 0)
        self.assertEqual(metrics["closed_quantity"], 2)
        self.assertEqual(metrics["realized_pnl"], 100.0)
        self.assertEqual(metrics["estimated_total_pnl"], 100.0)
        self.assertEqual(metrics["first_exit_trigger"], "smart_exit")
        self.assertEqual(metrics["single_leg_smart_exit_submitted"], 1)

    def test_strategy_rejected_leg_does_not_create_residual_open_quantity(self) -> None:
        order = {
            "status": "failed",
            "strategy_entry_status": "failed",
            "legs": [
                {
                    "status": "unfilled",
                    "quantity": 156,
                    "filled_quantity": 0,
                    "entry_price": 1.452,
                    "entry_detail": {"status": "Rejected", "executed_quantity": 0},
                    "leg": {"contract_symbol": "NVDA260603C00215000", "action": "buy", "qty": 1},
                }
            ],
        }

        metrics = build_review_metrics([order])
        protection = build_protection_status([order])

        self.assertEqual(metrics["open_quantity"], 0)
        self.assertEqual(metrics["entry_cost"], 0.0)
        self.assertEqual(protection["state"], "no_position")
        self.assertEqual(protection["strategy_residual_tracking_quantity"], 0)

    def test_strategy_partial_fill_uses_confirmed_quantity_only_for_residuals(self) -> None:
        order = {
            "status": "strategy_residual_tracking",
            "strategy_entry_status": "failed",
            "residual_leg_tracking_active": True,
            "legs": [
                {
                    "status": "filled",
                    "quantity": 23,
                    "filled_quantity": 23,
                    "entry_price": 3.3,
                    "entry_detail": {"status": "Filled", "executed_quantity": 23, "executed_price": 3.3},
                    "leg": {"contract_symbol": "AMZN260605C00247500", "action": "buy", "qty": 1},
                },
                {
                    "status": "unfilled",
                    "quantity": 23,
                    "filled_quantity": 0,
                    "entry_price": 4.2,
                    "entry_detail": {"status": "New", "executed_quantity": 0},
                    "leg": {"contract_symbol": "AMZN260605C00245000", "action": "sell", "qty": 1},
                },
            ],
        }

        metrics = build_review_metrics([order])
        protection = build_protection_status([order])

        self.assertEqual(metrics["open_quantity"], 23)
        self.assertEqual(metrics["entry_cost"], 7590.0)
        self.assertEqual(protection["strategy_residual_tracking_quantity"], 23)
        self.assertEqual(len(protection["contracts"][0]["residual_legs"]), 1)
        self.assertEqual(protection["contracts"][0]["residual_legs"][0]["contract_symbol"], "AMZN260605C00247500")

    def test_flatten_remaining_quantity_counts_smart_exit_as_pending_close(self) -> None:
        order = {
            "entry_filled_quantity": 3,
            "single_leg_smart_exit_closed_quantity": 2,
            "software_take_profit_closed_quantity": 1,
        }

        self.assertEqual(trading_instance_actions._closed_or_pending_close_quantity(order), 3)

    def test_flatten_remaining_quantity_counts_pending_close_submissions(self) -> None:
        order = {
            "entry_filled_quantity": 3,
            "software_stop_submitted_quantity": 2,
            "software_take_profit_closed_quantity": 1,
        }

        self.assertEqual(trading_instance_actions._closed_or_pending_close_quantity(order), 3)

    def test_flatten_remaining_quantity_does_not_double_count_same_exit_path(self) -> None:
        order = {
            "entry_filled_quantity": 2,
            "software_stop_closed_quantity": 2,
            "software_stop_submitted_quantity": 2,
        }

        self.assertEqual(trading_instance_actions._closed_or_pending_close_quantity(order), 2)

    def test_instance_flatten_builds_strategy_leg_closes(self) -> None:
        orders = [
            {
                "tracking_id": "spread-1",
                "strategy_type": "bull_call_spread",
                "legs": [
                    {
                        "leg": {"contract_symbol": "SPY260619C00500000", "action": "buy", "qty": 1},
                        "quantity": 2,
                        "filled_quantity": 2,
                    },
                    {
                        "leg": {"contract_symbol": "SPY260619C00505000", "action": "sell", "qty": 1},
                        "quantity": 2,
                        "filled_quantity": 2,
                        "strategy_exit_quantity": 1,
                    },
                ],
            }
        ]

        desired = trading_instance_actions._desired_strategy_close_legs(orders)
        self.assertEqual(len(desired), 2)
        self.assertEqual(desired[0]["side"], "sell")
        self.assertEqual(desired[0]["quantity"], 2)
        self.assertEqual(desired[1]["side"], "buy")
        self.assertEqual(desired[1]["quantity"], 1)

        trading_instance_actions._mark_strategy_leg_flattened(orders, desired[0], {"order_id": "exit-1"})
        self.assertEqual(orders[0]["legs"][0]["strategy_exit_quantity"], 2)
        self.assertEqual(orders[0]["legs"][0]["strategy_exit_status"], "submitted")
        self.assertFalse(orders[0]["risk_tracking_active"])
        self.assertEqual(orders[0]["status"], "strategy_auto_exit_submitted")


def _strategy_positions() -> list[dict]:
    return [
        _base_position("spread", "bull_call_spread", [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619C00505000", "sell", 1, 1.4)], 160, 160),
        _base_position("credit_spread", "bull_put_spread", [_leg("SPY260619P00490000", "buy", 1, 1.0), _leg("SPY260619P00495000", "sell", 1, 2.3)], 370, 370, net_credit=1.3),
        _base_position("straddle", "long_straddle", [_leg("SPY260619C00500000", "buy", 1, 3.0), _leg("SPY260619P00500000", "buy", 1, 2.8)], 580, 580),
        _base_position("strangle", "long_strangle", [_leg("SPY260619C00510000", "buy", 1, 1.6), _leg("SPY260619P00490000", "buy", 1, 1.5)], 310, 310),
        _base_position("collar", "protective_collar", [_leg("SPY260619P00490000", "buy", 1, 1.5), _leg("SPY260619C00510000", "sell", 1, 1.6)], 50000, 1000),
        _base_position("covered_call", "covered_call", [_leg("SPY260619C00510000", "sell", 1, 1.6)], 50000, 49000, net_credit=1.6),
        _base_position("cash_secured_put", "cash_secured_put", [_leg("SPY260619P00490000", "sell", 1, 2.2)], 49000, 48800, net_credit=2.2),
        _base_position("calendar", "call_calendar_spread", [_leg("SPY260619C00500000", "sell", 1, 1.4), _leg("SPY260717C00500000", "buy", 1, 3.2)], 180, 180),
        _base_position("diagonal", "call_diagonal_spread", [_leg("SPY260619C00510000", "sell", 1, 1.4), _leg("SPY260717C00500000", "buy", 1, 3.4)], 200, 200),
        _base_position("poor_mans_covered_call", "poor_mans_covered_call", [_leg("SPY260619C00510000", "sell", 1, 1.4), _leg("SPY260717C00480000", "buy", 1, 24.0)], 2260, 2260),
        _base_position("iron_condor", "iron_condor", [_leg("SPY260619P00490000", "sell", 1, 1.4), _leg("SPY260619P00485000", "buy", 1, 0.8), _leg("SPY260619C00510000", "sell", 1, 1.2), _leg("SPY260619C00515000", "buy", 1, 0.7)], 300, 300, net_credit=1.1),
        _base_position("butterfly", "call_butterfly", [_leg("SPY260619C00495000", "buy", 1, 5.0), _leg("SPY260619C00500000", "sell", 2, 2.8), _leg("SPY260619C00505000", "buy", 1, 1.2)], 60, 60),
    ]


def _base_position(family: str, strategy_type: str, legs: list[dict], capital_required: float, max_loss: float, net_credit: float = 0.0) -> dict:
    return {
        "tracking_id": f"test-{strategy_type}",
        "symbol": "SPY",
        "family": family,
        "strategy_type": strategy_type,
        "label": strategy_type,
        "legs": legs,
        "allocation_pct": 0.2,
        "capital_required": capital_required,
        "max_loss": max_loss,
        "net_debit": max(_entry_mark(legs) / 100, 0.0),
        "net_credit": net_credit,
        "entry_mark": _entry_mark(legs),
        "live_executable": True,
    }


def _leg(contract_symbol: str, action: str, qty: int, price: float) -> dict:
    return {
        "contract_symbol": contract_symbol,
        "action": action,
        "qty": qty,
        "price": price,
        "bid": price,
        "ask": price,
        "side": "call" if "C" in contract_symbol[-9:] else "put",
    }


def _entry_mark(legs: list[dict]) -> float:
    total = 0.0
    for leg in legs:
        signed = float(leg.get("price") or leg.get("ask") or leg.get("bid") or 0) * int(leg.get("qty") or leg.get("quantity") or 1) * 100
        total += signed if str(leg.get("action") or leg.get("side")).lower() in {"buy", "long"} else -signed
    return round(total, 2)


def _option_leg_count(position: dict) -> int:
    return sum(1 for leg in position["legs"] if str(leg.get("side") or "").lower() != "stock")


if __name__ == "__main__":
    unittest.main()

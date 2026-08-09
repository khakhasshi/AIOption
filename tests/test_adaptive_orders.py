"""Tests for adaptive ("smart") limit-order pricing.

An adaptive order is a limit order priced BETWEEN the mid and the opposite
touch: buy at mid + aggr*half_spread, sell at mid - aggr*half_spread. It saves
the half-spread the plain "limit" path always pays, while the reprice loop walks
aggr toward 1.0 so a resting order still crosses the market before we give up.

The per-instance order type is authoritative. AI_OPTION_ADAPTIVE_ORDER_ENABLED
and AI_OPTION_ADAPTIVE_EXIT_ENABLED are global kill switches for adaptive
pricing only; a plain limit order remains a plain limit order.
"""
from __future__ import annotations

import os
import unittest

from ai_option_scanner import trading_agent as agent
from ai_option_scanner import trading_store as store
from ai_option_scanner import adaptive_pricing as ap
from ai_option_scanner import trading_monitor as monitor


class AdaptivePricingMathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {
            k: os.environ.get(k)
            for k in ("AI_OPTION_ADAPTIVE_ORDER_ENABLED", "AI_OPTION_ADAPTIVE_AGGR_START")
        }
        os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = "1"
        os.environ["AI_OPTION_ADAPTIVE_AGGR_START"] = "0.3"

    def tearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_buy_prices_between_mid_and_ask(self):
        q = {"available": True, "bid": 2.00, "ask": 2.40}  # mid 2.20, half 0.20
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "buy", 0.0), 2.20, places=2)
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "buy", 0.3), 2.26, places=2)
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "buy", 1.0), 2.40, places=2)

    def test_sell_prices_between_mid_and_bid(self):
        q = {"available": True, "bid": 2.00, "ask": 2.40}
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "sell", 0.0), 2.20, places=2)
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "sell", 0.3), 2.14, places=2)
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "sell", 1.0), 2.00, places=2)

    def test_buy_is_never_worse_than_plain_limit_ask(self):
        # Adaptive buy at any aggr <= 1 must be <= ask (the plain-limit price).
        q = {"available": True, "bid": 1.00, "ask": 1.50}
        for aggr in (0.0, 0.25, 0.5, 0.75, 1.0):
            self.assertLessEqual(agent._adaptive_limit_price(q, "buy", aggr), 1.50 + 1e-9)

    def test_sell_is_never_worse_than_plain_limit_bid(self):
        q = {"available": True, "bid": 1.00, "ask": 1.50}
        for aggr in (0.0, 0.25, 0.5, 0.75, 1.0):
            self.assertGreaterEqual(agent._adaptive_limit_price(q, "sell", aggr), 1.00 - 1e-9)


class AdaptiveBadTickFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("AI_OPTION_ADAPTIVE_ORDER_ENABLED")
        os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = "1"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("AI_OPTION_ADAPTIVE_ORDER_ENABLED", None)
        else:
            os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = self._prev

    def test_crossed_book_falls_back_to_conservative_touch(self):
        # bid > ask (crossed) is untrustworthy -> fall back to ask for a buy.
        q = {"available": True, "bid": 2.50, "ask": 2.00}
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "buy", 0.3), 2.00, places=2)

    def test_one_sided_quote_falls_back(self):
        # Missing bid -> mid meaningless -> fall back to ask for a buy.
        q = {"available": True, "bid": 0.0, "ask": 2.40}
        self.assertAlmostEqual(agent._adaptive_limit_price(q, "buy", 0.3), 2.40, places=2)

    def test_unavailable_quote_returns_zero(self):
        self.assertEqual(agent._adaptive_limit_price({"available": False}, "buy", 0.3), 0.0)


class TickRoundingTest(unittest.TestCase):
    def test_penny_band_below_three_dollars(self):
        # < $3.00 uses $0.01 ticks; buy rounds up, sell rounds down.
        self.assertAlmostEqual(agent._round_to_tick(2.234, "buy"), 2.24, places=2)
        self.assertAlmostEqual(agent._round_to_tick(2.234, "sell"), 2.23, places=2)

    def test_nickel_band_at_or_above_three_dollars(self):
        # >= $3.00 uses $0.05 ticks; buy up, sell down.
        self.assertAlmostEqual(agent._round_to_tick(3.13, "buy"), 3.15, places=2)
        self.assertAlmostEqual(agent._round_to_tick(3.13, "sell"), 3.10, places=2)

    def test_band_crossing_buy_rerounds_to_legal_nickel(self):
        # A buy at 2.994 rounds up across $3.00 and must land on a legal $0.05 tick.
        self.assertAlmostEqual(agent._round_to_tick(2.994, "buy"), 3.00, places=2)

    def test_floor_is_one_cent(self):
        self.assertGreaterEqual(agent._round_to_tick(0.001, "sell"), 0.01)


class AggrWalkScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("AI_OPTION_ADAPTIVE_AGGR_START")
        os.environ["AI_OPTION_ADAPTIVE_AGGR_START"] = "0.3"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("AI_OPTION_ADAPTIVE_AGGR_START", None)
        else:
            os.environ["AI_OPTION_ADAPTIVE_AGGR_START"] = self._prev

    def test_walk_starts_passive_ends_marketable(self):
        walk = [round(agent._adaptive_aggr_for_attempt(i, 3), 3) for i in range(3)]
        self.assertEqual(walk[0], 0.3)     # first attempt = start (passive)
        self.assertEqual(walk[-1], 1.0)    # last attempt = marketable
        self.assertLess(walk[0], walk[1])  # monotonically escalating
        self.assertLess(walk[1], walk[2])

    def test_single_attempt_uses_start(self):
        self.assertAlmostEqual(agent._adaptive_aggr_for_attempt(0, 1), 0.3, places=3)

    def test_final_attempt_always_marketable(self):
        self.assertEqual(agent._adaptive_aggr_for_attempt(4, 5), 1.0)


class NormalizerTest(unittest.TestCase):
    def test_agent_preserves_adaptive(self):
        self.assertEqual(agent._normalize_entry_order_type("adaptive"), "adaptive")
        self.assertEqual(agent._normalize_entry_order_type("smart"), "adaptive")
        self.assertEqual(agent._normalize_entry_order_type("market"), "market")
        self.assertEqual(agent._normalize_entry_order_type("limit"), "limit")
        self.assertEqual(agent._normalize_entry_order_type(None), "market")

    def test_store_preserves_adaptive(self):
        # Regression: the store normalizer used to coerce anything not limit/lo
        # to "market", which silently killed adaptive at config save/load time.
        self.assertEqual(store._normalize_entry_order_type("adaptive"), "adaptive")
        self.assertEqual(store._normalize_entry_order_type("market"), "market")
        self.assertEqual(store._normalize_entry_order_type("limit"), "limit")

    def test_config_roundtrips_adaptive(self):
        cfg = store.normalize_trading_config({"entry_order_type": "adaptive", "exit_order_type": "limit"})
        self.assertEqual(cfg["entry_order_type"], "adaptive")
        self.assertEqual(cfg["exit_order_type"], "limit")


class AdaptiveKillSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("AI_OPTION_ADAPTIVE_ORDER_ENABLED")
        os.environ.pop("AI_OPTION_ADAPTIVE_ORDER_ENABLED", None)

    def tearDown(self) -> None:
        if self._prev is not None:
            os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = self._prev

    def test_unset_kill_switch_allows_adaptive_pricing(self):
        self.assertTrue(agent._adaptive_order_enabled())

    def test_explicitly_disabled_kill_switch_blocks_adaptive_pricing(self):
        os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = "false"
        self.assertFalse(agent._adaptive_order_enabled())


class AdaptiveExitDecisionTest(unittest.TestCase):
    RAW = {"available": True, "bid": 2.00, "ask": 2.40}  # mid 2.20

    def setUp(self) -> None:
        self._prev = {
            k: os.environ.get(k)
            for k in (
                "AI_OPTION_ADAPTIVE_ORDER_ENABLED",
                "AI_OPTION_ADAPTIVE_EXIT_ENABLED",
                "AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES",
                "AI_OPTION_ADAPTIVE_AGGR_START",
            )
        }
        os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = "1"
        os.environ["AI_OPTION_ADAPTIVE_EXIT_ENABLED"] = "1"
        os.environ["AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES"] = "3"
        os.environ["AI_OPTION_ADAPTIVE_AGGR_START"] = "0.3"

    def tearDown(self) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_exit_kill_switch_is_allowed_when_unset(self):
        os.environ.pop("AI_OPTION_ADAPTIVE_EXIT_ENABLED", None)
        self.assertTrue(ap.adaptive_exit_enabled())
        price, use_market = ap.adaptive_exit_decision(self.RAW, "sell", 0)
        self.assertFalse(use_market)
        self.assertGreater(price, 2.00)

    def test_exit_disabled_when_master_kill_switch_is_off(self):
        os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = "0"
        self.assertFalse(ap.adaptive_exit_enabled())  # AND-gated with master

    def test_exit_disabled_when_exit_kill_switch_is_off(self):
        os.environ["AI_OPTION_ADAPTIVE_EXIT_ENABLED"] = "off"
        self.assertFalse(ap.adaptive_exit_enabled())

    def test_market_mode_always_uses_market(self):
        price, use_market = ap.adaptive_exit_decision(self.RAW, "sell", 0, "market")
        self.assertTrue(use_market)
        self.assertEqual(price, 0.0)

    def test_limit_mode_uses_touch_even_when_adaptive_is_disabled(self):
        os.environ["AI_OPTION_ADAPTIVE_EXIT_ENABLED"] = "0"
        price, use_market = ap.adaptive_exit_decision(self.RAW, "sell", 0, "limit")
        self.assertFalse(use_market)
        self.assertEqual(price, 2.00)

    def test_cycle_zero_rests_passive_limit_below_mid(self):
        # A sell exit at cycle 0 rests near mid (spread saved), strictly > bid.
        price, use_market = ap.adaptive_exit_decision(self.RAW, "sell", 0)
        self.assertFalse(use_market)
        self.assertGreater(price, 2.00)   # better than the bid (touch)
        self.assertLessEqual(price, 2.20)  # at/below mid

    def test_walk_escalates_toward_bid_each_cycle(self):
        p0, _ = ap.adaptive_exit_decision(self.RAW, "sell", 0)
        p1, _ = ap.adaptive_exit_decision(self.RAW, "sell", 1)
        self.assertGreaterEqual(p0, p1)  # sell walks DOWN toward bid

    def test_market_fallback_at_max_cycles(self):
        # cycle >= max_cycles (3) -> guaranteed market exit, never a resting limit.
        price, use_market = ap.adaptive_exit_decision(self.RAW, "sell", 3)
        self.assertTrue(use_market)
        self.assertEqual(price, 0.0)

    def test_bad_quote_falls_to_market(self):
        # Crossed / one-sided quote must not rest a limit on garbage.
        bad = {"available": True, "bid": 2.50, "ask": 2.00}
        price, use_market = ap.adaptive_exit_decision(bad, "sell", 0)
        self.assertTrue(use_market)
        self.assertEqual(price, 0.0)

    def test_unavailable_quote_falls_to_market(self):
        price, use_market = ap.adaptive_exit_decision({"available": False}, "sell", 0)
        self.assertTrue(use_market)


class AdaptiveTakeProfitEscalationTest(unittest.TestCase):
    """Reconciler walks a resting adaptive TP limit -> market at walk-end."""

    def setUp(self) -> None:
        self._prev = {
            k: os.environ.get(k)
            for k in (
                "AI_OPTION_ADAPTIVE_ORDER_ENABLED",
                "AI_OPTION_ADAPTIVE_EXIT_ENABLED",
                "AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES",
                "AI_OPTION_ADAPTIVE_AGGR_START",
            )
        }
        os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = "1"
        os.environ["AI_OPTION_ADAPTIVE_EXIT_ENABLED"] = "1"
        os.environ["AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES"] = "3"
        os.environ["AI_OPTION_ADAPTIVE_AGGR_START"] = "0.3"
        # Capture broker calls; patch monitor module symbols.
        self.submitted: list[dict[str, Any]] = []
        self.canceled: list[str] = []
        self._orig = {
            name: getattr(monitor, name)
            for name in ("cancel_order", "order_detail", "submit_sell_order", "submit_market_order", "longbridge_sdk_option_quote", "wait_for_order_fill")
        }
        monitor.cancel_order = lambda oid, acct=None: self.canceled.append(oid) or {"ok": True}
        # Resting limit never filled (empty detail => qty 0, blank status).
        monitor.order_detail = lambda oid, acct=None: {"status": "new", "executed_quantity": 0}
        monitor.submit_sell_order = lambda sym, qty, price, acct=None, remark=None, order_type="limit": (
            self.submitted.append({"kind": "limit", "price": price, "qty": qty}) or {"order_id": f"L{len(self.submitted)}"}
        )
        monitor.submit_market_order = lambda sym, qty, side, acct=None, remark=None: (
            self.submitted.append({"kind": "market", "qty": qty, "side": side}) or {"order_id": f"M{len(self.submitted)}"}
        )
        monitor.longbridge_sdk_option_quote = lambda sym, acct=None: {"available": True, "bid": 2.00, "ask": 2.40}
        monitor.wait_for_order_fill = lambda oid, acct=None, timeout_seconds=3: {}

    def tearDown(self) -> None:
        for name, fn in self._orig.items():
            setattr(monitor, name, fn)
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _order_with_resting_tp(self, cycle: int) -> tuple[dict, dict]:
        target = {"name": "tp1", "adaptive_exit_resting": True, "adaptive_exit_cycle": cycle, "order": {"order_id": "OLD"}}
        order = {"order_symbol": "AAPL240119C00150000", "contract_symbol": "AAPL240119C00150000", "exit_order_type": "adaptive"}
        row = {"source": "software_take_profit", "order_id": "OLD", "quantity": 3, "target": target}
        return order, row

    def test_escalation_cancels_and_resubmits_more_aggressive_limit(self):
        order, row = self._order_with_resting_tp(cycle=0)
        acted = monitor._escalate_adaptive_take_profit(order, row, "acct")
        self.assertTrue(acted)
        self.assertEqual(self.canceled, ["OLD"])
        self.assertEqual(self.submitted[-1]["kind"], "limit")  # cycle 1 still limit
        self.assertEqual(row["target"]["adaptive_exit_cycle"], 1)
        self.assertTrue(row["target"]["adaptive_exit_resting"])

    def test_escalation_falls_to_market_at_walk_end(self):
        # cycle 2 -> next_cycle 3 == max_cycles -> guaranteed market.
        order, row = self._order_with_resting_tp(cycle=2)
        acted = monitor._escalate_adaptive_take_profit(order, row, "acct")
        self.assertTrue(acted)
        self.assertEqual(self.submitted[-1]["kind"], "market")
        self.assertFalse(row["target"]["adaptive_exit_resting"])

    def test_no_op_when_target_not_adaptive_resting(self):
        # A plain-market TP target (no adaptive flag) must NOT be escalated.
        target = {"name": "tp1", "order": {"order_id": "OLD"}}
        order = {"order_symbol": "AAPL240119C00150000"}
        row = {"source": "software_take_profit", "order_id": "OLD", "quantity": 3, "target": target}
        acted = monitor._escalate_adaptive_take_profit(order, row, "acct")
        self.assertFalse(acted)
        self.assertEqual(self.canceled, [])
        self.assertEqual(self.submitted, [])

    def test_post_cancel_fill_is_not_double_closed(self):
        # If the resting limit actually filled in the race window, mark filled and
        # do NOT resubmit (never over-close).
        monitor.order_detail = lambda oid, acct=None: {"status": "filled", "executed_quantity": 3, "executed_price": 2.15}
        order, row = self._order_with_resting_tp(cycle=0)
        acted = monitor._escalate_adaptive_take_profit(order, row, "acct")
        self.assertTrue(acted)
        self.assertEqual(self.canceled, ["OLD"])
        self.assertEqual(self.submitted, [])  # no resubmit


class AdaptiveStrategyExitEscalationTest(unittest.TestCase):
    """Reconciler walks a resting adaptive strategy LONG-leg exit -> market at walk-end."""

    def setUp(self) -> None:
        self._prev = {
            k: os.environ.get(k)
            for k in (
                "AI_OPTION_ADAPTIVE_ORDER_ENABLED",
                "AI_OPTION_ADAPTIVE_EXIT_ENABLED",
                "AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES",
                "AI_OPTION_ADAPTIVE_AGGR_START",
            )
        }
        os.environ["AI_OPTION_ADAPTIVE_ORDER_ENABLED"] = "1"
        os.environ["AI_OPTION_ADAPTIVE_EXIT_ENABLED"] = "1"
        os.environ["AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES"] = "3"
        os.environ["AI_OPTION_ADAPTIVE_AGGR_START"] = "0.3"
        self.submitted: list[dict[str, Any]] = []
        self.canceled: list[str] = []
        self._orig = {
            name: getattr(monitor, name)
            for name in ("cancel_order", "order_detail", "submit_sell_order", "submit_market_order", "longbridge_sdk_option_quote", "wait_for_order_fill")
        }
        monitor.cancel_order = lambda oid, acct=None: self.canceled.append(oid) or {"ok": True}
        monitor.order_detail = lambda oid, acct=None: {"status": "new", "executed_quantity": 0}
        monitor.submit_sell_order = lambda sym, qty, price, acct=None, remark=None, order_type="limit": (
            self.submitted.append({"kind": "limit", "price": price, "qty": qty}) or {"order_id": f"L{len(self.submitted)}"}
        )
        monitor.submit_market_order = lambda sym, qty, side, acct=None, remark=None: (
            self.submitted.append({"kind": "market", "qty": qty, "side": side}) or {"order_id": f"M{len(self.submitted)}"}
        )
        monitor.longbridge_sdk_option_quote = lambda sym, acct=None: {"available": True, "bid": 2.00, "ask": 2.40}
        monitor.wait_for_order_fill = lambda oid, acct=None, timeout_seconds=3: {}

    def tearDown(self) -> None:
        for name, fn in self._orig.items():
            setattr(monitor, name, fn)
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _order_with_resting_leg(self, cycle: int) -> tuple[dict, dict]:
        entry = {
            "contract_symbol": "AAPL240119C00150000",
            "strategy_exit_order": {"order_id": "OLD"},
            "strategy_exit_quantity": 3,
            "strategy_exit_side": "sell",
            "adaptive_exit_resting": True,
            "adaptive_exit_cycle": cycle,
        }
        order = {"tracking_id": "T1", "exit_order_type": "adaptive", "legs": [entry]}
        return order, entry

    def test_escalation_cancels_and_resubmits_more_aggressive_limit(self):
        order, entry = self._order_with_resting_leg(cycle=0)
        acted = monitor._escalate_adaptive_strategy_exit(order, entry, "acct")
        self.assertTrue(acted)
        self.assertEqual(self.canceled, ["OLD"])
        self.assertEqual(self.submitted[-1]["kind"], "limit")  # cycle 1 still limit
        self.assertEqual(entry["adaptive_exit_cycle"], 1)
        self.assertTrue(entry["adaptive_exit_resting"])
        self.assertEqual(entry["strategy_exit_status"], "submitted")

    def test_escalation_falls_to_market_at_walk_end(self):
        # cycle 2 -> next_cycle 3 == max_cycles -> guaranteed market.
        order, entry = self._order_with_resting_leg(cycle=2)
        acted = monitor._escalate_adaptive_strategy_exit(order, entry, "acct")
        self.assertTrue(acted)
        self.assertEqual(self.submitted[-1]["kind"], "market")
        self.assertEqual(self.submitted[-1]["side"], "sell")
        self.assertFalse(entry["adaptive_exit_resting"])

    def test_no_op_when_leg_not_adaptive_resting(self):
        # A market short-cover leg (never flagged) must NOT be escalated.
        entry = {"contract_symbol": "AAPL240119C00150000", "strategy_exit_order": {"order_id": "OLD"}, "strategy_exit_quantity": 3}
        order = {"tracking_id": "T1", "legs": [entry]}
        acted = monitor._escalate_adaptive_strategy_exit(order, entry, "acct")
        self.assertFalse(acted)
        self.assertEqual(self.canceled, [])
        self.assertEqual(self.submitted, [])

    def test_post_cancel_fill_is_not_double_closed(self):
        # If the resting limit filled in the race window, mark filled, do NOT resubmit.
        monitor.order_detail = lambda oid, acct=None: {"status": "filled", "executed_quantity": 3, "executed_price": 2.15}
        order, entry = self._order_with_resting_leg(cycle=0)
        acted = monitor._escalate_adaptive_strategy_exit(order, entry, "acct")
        self.assertTrue(acted)
        self.assertEqual(self.canceled, ["OLD"])
        self.assertEqual(self.submitted, [])  # no resubmit
        self.assertEqual(entry["strategy_exit_status"], "filled")
        self.assertFalse(entry["adaptive_exit_resting"])

    def test_buy_side_cover_walk_stays_buy(self):
        # Defensive: if a buy-side leg were ever flagged adaptive, walk keeps side=buy.
        order, entry = self._order_with_resting_leg(cycle=2)
        entry["strategy_exit_side"] = "buy"
        acted = monitor._escalate_adaptive_strategy_exit(order, entry, "acct")
        self.assertTrue(acted)
        self.assertEqual(self.submitted[-1]["kind"], "market")
        self.assertEqual(self.submitted[-1]["side"], "buy")

    def test_strategy_auto_exit_uses_the_configured_adaptive_mode(self):
        position = {"tracking_id": "T1", "strategy_units": 1}
        entry = {
            "leg": {"contract_symbol": "AAPL240119C00150000", "action": "buy", "side": "call", "qty": 1},
            "filled_quantity": 1,
            "quantity": 1,
        }
        order = {"tracking_id": "T1", "exit_order_type": "adaptive", "legs": [entry]}

        result = monitor._submit_strategy_auto_exit(position, [order], "acct", "tp1")

        self.assertEqual(result["submitted"], 1)
        self.assertEqual(self.submitted[-1]["kind"], "limit")
        self.assertTrue(entry["adaptive_exit_resting"])


if __name__ == "__main__":
    unittest.main()

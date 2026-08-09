"""Regression tests for Phase 1 live-trading correctness fixes.

Covers:
- _is_filled_status no longer treats "unfilled"/"partially_filled" as filled
- strategy close uses confirmed leg fills, not planned quantity
- _quote_exit_price uses sell-to-close (bid) basis, never ask
- parse_exit_at "day before expiry" lands on a real trading day
"""
from __future__ import annotations

import unittest
from datetime import datetime

from ai_option_scanner import trading_instance, trading_monitor, smart_exit_rules
from ai_option_scanner.trading_instance_actions import _desired_strategy_close_legs
from ai_option_scanner.time_utils import EASTERN


class FilledStatusTest(unittest.TestCase):
    def test_unfilled_is_not_filled(self):
        self.assertFalse(trading_instance._is_filled_status({"status": "unfilled"}))
        self.assertFalse(trading_instance._is_filled_status({"entry_detail": {"status": "unfilled"}}))

    def test_partial_filled_is_not_full_fill(self):
        self.assertFalse(trading_instance._is_filled_status({"status": "partially_filled"}))
        self.assertFalse(trading_instance._is_filled_status({"status": "partial_filled"}))
        self.assertFalse(trading_instance._is_filled_status({"entry_detail": {"status": "PartiallyFilled"}}))

    def test_genuine_filled_states(self):
        self.assertTrue(trading_instance._is_filled_status({"status": "filled"}))
        self.assertTrue(trading_instance._is_filled_status({"status": "fully_filled"}))
        self.assertTrue(trading_instance._is_filled_status({"status": "stop_submitted_after_fill"}))
        self.assertTrue(trading_instance._is_filled_status({"entry_detail": {"status": "Filled"}}))

    def test_confirmed_quantity_is_authoritative(self):
        self.assertTrue(trading_instance._is_filled_status({"status": "unfilled", "entry_filled_quantity": 2}))
        self.assertFalse(trading_instance._is_filled_status({"status": "unfilled", "entry_filled_quantity": 0}))


class StrategyCloseFillTest(unittest.TestCase):
    def test_unfilled_leg_is_not_closed(self):
        # Combo order that never entered (no parent fill); a planned leg must NOT
        # be treated as an open position to close.
        orders = [{
            "tracking_id": "t1",
            "is_strategy_order": True,
            "entry_filled_quantity": 0,
            "legs": [
                {"contract_symbol": "AAPL260101C100", "quantity": 2,
                 "leg": {"action": "buy", "contract_symbol": "AAPL260101C100"}},
            ],
        }]
        self.assertEqual(_desired_strategy_close_legs(orders), [])

    def test_filled_leg_is_closed(self):
        orders = [{
            "tracking_id": "t1",
            "entry_filled_quantity": 2,
            "legs": [
                {"contract_symbol": "AAPL260101C100", "quantity": 2, "filled_quantity": 2,
                 "leg": {"action": "buy", "contract_symbol": "AAPL260101C100"}},
            ],
        }]
        legs = _desired_strategy_close_legs(orders)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["quantity"], 2)
        self.assertEqual(legs[0]["side"], "sell")  # close a long with a sell

    def test_planned_qty_used_only_when_parent_confirmed(self):
        # Parent confirmed entry but per-leg fill not stamped → trust planned qty.
        orders = [{
            "tracking_id": "t1",
            "entry_filled_quantity": 1,
            "legs": [
                {"contract_symbol": "AAPL260101C100", "quantity": 1,
                 "leg": {"action": "sell", "contract_symbol": "AAPL260101C100"}},
            ],
        }]
        legs = _desired_strategy_close_legs(orders)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["side"], "buy")  # close a short with a buy


class QuoteExitPriceTest(unittest.TestCase):
    def test_prefers_bid_over_ask(self):
        self.assertEqual(trading_monitor._quote_exit_price({"bid": 1.20, "ask": 1.80}), 1.20)

    def test_ask_only_is_not_a_trigger_price(self):
        # Only an ask available → treat as unavailable (0.0), do NOT return ask.
        self.assertEqual(trading_monitor._quote_exit_price({"ask": 1.80}), 0.0)

    def test_last_used_when_no_bid(self):
        self.assertEqual(trading_monitor._quote_exit_price({"last_done": 1.50, "ask": 1.80}), 1.50)


class TradingDayExitTest(unittest.TestCase):
    def test_day_before_monday_expiry_is_prior_friday(self):
        # Monday 2026-06-15 expiry → "day before" must be Fri 2026-06-12, not Sun 06-14.
        position = {"expiration": "2026-06-15"}
        exit_at = smart_exit_rules.parse_exit_at("到期前一交易日 15:50", position)
        self.assertIsNotNone(exit_at)
        self.assertEqual(exit_at.date().isoformat(), "2026-06-12")
        self.assertEqual((exit_at.hour, exit_at.minute), (15, 50))

    def test_one_trading_day_before_phrase(self):
        position = {"expiration": "2026-06-15"}
        exit_at = smart_exit_rules.parse_exit_at("到期前 1 个交易日", position)
        self.assertIsNotNone(exit_at)
        self.assertEqual(exit_at.date().isoformat(), "2026-06-12")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from ai_option_scanner import broker_client
from ai_option_scanner.broker_client import BrokerError
from ai_option_scanner.trading_agent import _status_is_filled
from ai_option_scanner.smart_exit_rules import _hhmm_to_minutes, evaluate_exit_rules


class FillStatusTokenTest(unittest.TestCase):
    """The bare `"filled" in status` substring test treated uSMART's "unfilled"
    and "partially_filled" as filled. _status_is_filled is token-aware."""

    def test_unfilled_is_not_filled(self) -> None:
        self.assertFalse(_status_is_filled("unfilled"))

    def test_partially_filled_is_not_filled(self) -> None:
        self.assertFalse(_status_is_filled("partially_filled"))
        self.assertFalse(_status_is_filled("partial_fill"))

    def test_genuine_filled_states(self) -> None:
        self.assertTrue(_status_is_filled("filled"))
        self.assertTrue(_status_is_filled("Filled"))
        self.assertTrue(_status_is_filled("FilledStatus"))

    def test_empty_and_none(self) -> None:
        self.assertFalse(_status_is_filled(""))
        self.assertFalse(_status_is_filled(None))


class OrderResolverFailClosedTest(unittest.TestCase):
    """submit_* must refuse to route an order to a default broker account when
    the ref is missing — Longbridge previously fell through to its default
    (often LIVE) account on a None ref."""

    def test_none_ref_rejected(self) -> None:
        with self.assertRaises(BrokerError):
            broker_client._resolve_for_order(None)

    def test_blank_ref_rejected(self) -> None:
        with self.assertRaises(BrokerError):
            broker_client._resolve_for_order("   ")

    def test_submit_buy_with_none_ref_raises(self) -> None:
        with self.assertRaises(BrokerError):
            broker_client.submit_buy_order("AAPL.US", 1, 1.0, None, "x", "limit")

    def test_read_path_still_lenient(self) -> None:
        # _resolve (used by reads/cancel) maps None -> default longbridge, no raise.
        broker, _account = broker_client._resolve(None)
        self.assertEqual(broker, "longbridge")


class AlpacaOrderTypeNormalizeTest(unittest.TestCase):
    def test_known_types(self) -> None:
        from ai_option_scanner.alpaca_client import _normalize_order_type
        self.assertEqual(_normalize_order_type("limit"), "limit")
        self.assertEqual(_normalize_order_type("lo"), "limit")
        self.assertEqual(_normalize_order_type("market"), "market")
        self.assertEqual(_normalize_order_type("mo"), "market")

    def test_empty_defaults_market(self) -> None:
        from ai_option_scanner.alpaca_client import _normalize_order_type
        self.assertEqual(_normalize_order_type(None), "market")
        self.assertEqual(_normalize_order_type(""), "market")

    def test_unknown_raises_not_downgraded_to_market(self) -> None:
        from ai_option_scanner.alpaca_client import _normalize_order_type, AlpacaError
        with self.assertRaises(AlpacaError):
            _normalize_order_type("lmt")
        with self.assertRaises(AlpacaError):
            _normalize_order_type("stop_limit")


class NoOvernightTimeCompareTest(unittest.TestCase):
    def test_hhmm_parsing(self) -> None:
        self.assertEqual(_hhmm_to_minutes("9:30"), 570)
        self.assertEqual(_hhmm_to_minutes("09:30"), 570)
        self.assertEqual(_hhmm_to_minutes("15:50"), 950)
        self.assertIsNone(_hhmm_to_minutes("nope"))
        self.assertIsNone(_hhmm_to_minutes("25:00"))

    def test_single_digit_hour_threshold_fires_after_cutoff(self) -> None:
        # At 10:00 ET with an unpadded "9:30" cutoff, lexicographic compare gave
        # "10:00" >= "9:30" == False (never fired). Numeric compare fires.
        from datetime import datetime
        from ai_option_scanner.time_utils import EASTERN
        now = datetime(2026, 6, 26, 10, 0, tzinfo=EASTERN)
        trigger = evaluate_exit_rules(
            rules=[{"type": "no_overnight", "time_et": "9:30", "reason": "no overnight"}],
            position={"symbol": "SPY"},
            current_price=1.0,
            entry_price=1.0,
            now=now,
        )
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["trigger"], "smart_no_overnight_exit")

    def test_before_cutoff_does_not_fire(self) -> None:
        from datetime import datetime
        from ai_option_scanner.time_utils import EASTERN
        now = datetime(2026, 6, 26, 9, 0, tzinfo=EASTERN)
        trigger = evaluate_exit_rules(
            rules=[{"type": "no_overnight", "time_et": "9:30", "reason": "no overnight"}],
            position={"symbol": "SPY"},
            current_price=1.0,
            entry_price=1.0,
            now=now,
        )
        self.assertIsNone(trigger)


if __name__ == "__main__":
    unittest.main()

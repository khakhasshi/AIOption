from __future__ import annotations

import unittest
from datetime import datetime

from ai_option_scanner.market_calendar import ET, market_environment, next_regular_open_after, nyse_early_close_name


class MarketCalendarTest(unittest.TestCase):
    def test_market_environment_identifies_regular_open_and_next_close(self) -> None:
        env = market_environment(datetime(2026, 5, 22, 10, 0, tzinfo=ET))

        self.assertTrue(env["is_market_open_regular"])
        self.assertEqual(env["session_state"], "regular_open")
        self.assertEqual(env["market_state"], "regular_open")
        self.assertEqual(env["regular_close_at_et"], "2026-05-22T16:00:00-04:00")
        self.assertEqual(env["next_regular_close_at_et"], "2026-05-22T16:00:00-04:00")

    def test_market_environment_identifies_weekend_and_next_open_after_holiday(self) -> None:
        env = market_environment(datetime(2026, 5, 23, 10, 0, tzinfo=ET))

        self.assertFalse(env["is_market_open_regular"])
        self.assertEqual(env["session_state"], "weekend")
        self.assertEqual(env["market_state"], "weekend")
        self.assertEqual(env["next_regular_open_at_et"], "2026-05-26T09:30:00-04:00")
        self.assertEqual(next_regular_open_after(datetime(2026, 5, 23, 10, 0, tzinfo=ET), grace_minutes=10).isoformat(), "2026-05-26T09:40:00-04:00")

    def test_market_environment_uses_early_close_calendar(self) -> None:
        self.assertEqual(nyse_early_close_name(datetime(2026, 7, 2, tzinfo=ET).date()), "Independence Day early close")

        env = market_environment(datetime(2026, 7, 2, 12, 30, tzinfo=ET))

        self.assertTrue(env["is_market_open_regular"])
        self.assertTrue(env["is_early_close"])
        self.assertEqual(env["regular_close_at_et"], "2026-07-02T13:00:00-04:00")
        self.assertEqual(env["early_close_reason"], "Independence Day early close")


if __name__ == "__main__":
    unittest.main()

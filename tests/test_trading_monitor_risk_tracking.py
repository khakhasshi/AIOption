from __future__ import annotations

import unittest

from ai_option_scanner import trading_monitor


class StrategyPositionUnitsTest(unittest.TestCase):
    def test_returns_first_positive_unit_field(self):
        position = {"strategy_units": 3, "units": 0, "quantity": 0}
        self.assertEqual(trading_monitor._strategy_position_units(position), 3)

    def test_falls_through_to_next_field(self):
        position = {"strategy_units": 0, "units": 2, "quantity": 5}
        self.assertEqual(trading_monitor._strategy_position_units(position), 2)

    def test_returns_zero_when_all_fields_missing_or_zero(self):
        # Regression: previously defaulted to 1, fabricating phantom positions.
        for fields in [
            {},
            {"strategy_units": 0, "units": 0, "quantity": 0, "entry_filled_quantity": 0},
            {"strategy_units": None, "units": None, "quantity": None},
            {"strategy_units": "not-a-number"},
        ]:
            with self.subTest(fields=fields):
                self.assertEqual(trading_monitor._strategy_position_units(fields), 0)

    def test_open_units_handles_zero_total(self):
        position = {"strategy_units": 0, "strategy_exit_closed_units": 0}
        self.assertEqual(trading_monitor._strategy_position_open_units(position), 0)


class PhantomPositionGuardTest(unittest.TestCase):
    def test_zero_unit_position_is_marked_inactive_and_skipped(self):
        instance = {
            "risk_plan": {
                "strategy_positions": [
                    {
                        "tracking_id": "T1",
                        "risk_tracking_active": True,
                        "strategy_units": 0,
                        "units": 0,
                        "quantity": 0,
                        "stop_loss_pnl": -100.0,
                    }
                ]
            }
        }
        # _try_strategy_risk_tracking should short-circuit before calling _strategy_position_mark.
        called = {"mark": 0}

        def fake_mark(*_args, **_kwargs):
            called["mark"] += 1
            return {"available": False}

        original = trading_monitor._strategy_position_mark
        trading_monitor._strategy_position_mark = fake_mark
        try:
            result = trading_monitor._try_strategy_risk_tracking(instance, "paper", [])
        finally:
            trading_monitor._strategy_position_mark = original

        self.assertEqual(called["mark"], 0)
        position = instance["risk_plan"]["strategy_positions"][0]
        self.assertFalse(position["risk_tracking_active"])
        self.assertEqual(position["tracking_status"], "zero_units_skip")
        # Stop loss was -100 with pnl=0 — without the guard this would fire stop trigger.
        self.assertEqual(result["stop_alerted"], 0)


if __name__ == "__main__":
    unittest.main()

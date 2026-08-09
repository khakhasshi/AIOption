from __future__ import annotations

import unittest

from ai_option_scanner import trading_agent


class TradingP0GuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_validate_trading_readiness = trading_agent.validate_trading_readiness
        self._orig_create_trading_run = trading_agent.create_trading_run
        self._orig_executor = trading_agent._executor

    def tearDown(self) -> None:
        trading_agent.validate_trading_readiness = self._orig_validate_trading_readiness
        trading_agent.create_trading_run = self._orig_create_trading_run
        trading_agent._executor = self._orig_executor

    def test_start_trading_run_blocks_when_readiness_is_not_ok(self) -> None:
        created = []
        trading_agent.validate_trading_readiness = lambda *args, **kwargs: {"ok": False, "issues": ["risk breaker tripped"]}
        trading_agent.create_trading_run = lambda *args, **kwargs: created.append(True) or {"id": "run-1"}

        with self.assertRaises(trading_agent.TradingRunBlockedError) as ctx:
            trading_agent.start_trading_run(
                "owner",
                {
                    "live_enabled": True,
                    "total_capital": 1000,
                    "universe": ["SPY"],
                    "single_instance_enabled": True,
                },
                trigger_source="scheduler:balanced:open",
            )

        self.assertIn("risk breaker tripped", str(ctx.exception))
        self.assertEqual(created, [])

    def test_single_leg_order_outcome_fails_when_every_order_failed_or_skipped(self) -> None:
        outcome = trading_agent._single_leg_order_outcome(
            [
                {"status": "failed", "error": "broker rejected"},
                {"status": "skipped", "message": "no quantity"},
            ]
        )

        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["stage"], "order_submission_failed")
        self.assertIn("broker rejected", outcome["error"])

    def test_single_leg_order_outcome_succeeds_when_entry_is_live_or_filled(self) -> None:
        live_outcome = trading_agent._single_leg_order_outcome([{"status": "entry_submitted_stop_pending_unfilled"}])
        filled_outcome = trading_agent._single_leg_order_outcome([{"status": "stop_failed", "entry_filled_quantity": 1}])

        self.assertEqual(live_outcome["status"], "succeeded")
        self.assertEqual(filled_outcome["status"], "succeeded")

    def test_order_has_flattenable_quantity_counts_pending_close_submissions(self) -> None:
        order = {
            "entry_filled_quantity": 3,
            "software_stop_submitted_quantity": 2,
            "software_take_profit_closed_quantity": 1,
        }

        self.assertFalse(trading_agent._order_has_flattenable_quantity(order))


if __name__ == "__main__":
    unittest.main()

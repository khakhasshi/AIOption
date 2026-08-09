from __future__ import annotations

import unittest

from ai_option_scanner import trading_instance_actions as actions
from ai_option_scanner.trading_instance_actions import (
    InstanceHasLiveBrokerStateError,
    delete_trade_instance,
    reset_trade_instance_risk,
)


def _resting_entry_order(run_id: str = "run-1") -> dict:
    """One instance order with an unfilled resting entry order."""
    return {
        "run_id": run_id,
        "status": "completed",
        "orders": [
            {
                "order_symbol": "AAPL250117C00150000",
                "quantity": 1,
                "entry_filled_quantity": 0,
                "entry_order": {"order_id": "BRK-ENTRY-1"},
            }
        ],
        "trade_instance": {},
    }


def _open_position_run(run_id: str = "run-2") -> dict:
    """One instance order that is filled and not yet closed -> open position."""
    return {
        "run_id": run_id,
        "status": "completed",
        "orders": [
            {
                "order_symbol": "AAPL250117C00150000",
                "quantity": 2,
                "entry_filled_quantity": 2,
                "entry_order": {"order_id": "BRK-ENTRY-2"},
            }
        ],
        "trade_instance": {},
    }


def _clean_run(run_id: str = "run-3") -> dict:
    """Fully closed: filled then flattened, entry order also filled (not resting)."""
    return {
        "run_id": run_id,
        "status": "completed",
        "orders": [
            {
                "order_symbol": "AAPL250117C00150000",
                "quantity": 1,
                "entry_filled_quantity": 1,
                "entry_order": {"order_id": "BRK-ENTRY-3"},
                "instance_flatten_submitted_quantity": 1,
            }
        ],
        "trade_instance": {},
    }


class InstanceDeleteReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._deleted: list[str] = []
        self._orig_get = actions.get_trading_run
        self._orig_delete = actions.delete_trading_run
        self._orig_mark = actions.mark_trading_run
        actions.delete_trading_run = lambda run_id, owner_id: (self._deleted.append(run_id) or True)
        actions.mark_trading_run = lambda *a, **k: None

    def tearDown(self) -> None:
        actions.get_trading_run = self._orig_get
        actions.delete_trading_run = self._orig_delete
        actions.mark_trading_run = self._orig_mark

    def _patch_run(self, run: dict) -> None:
        actions.get_trading_run = lambda run_id, owner_id: run

    def test_delete_blocks_on_resting_order(self) -> None:
        self._patch_run(_resting_entry_order())
        with self.assertRaises(InstanceHasLiveBrokerStateError) as ctx:
            delete_trade_instance("run-1", "owner")
        self.assertTrue(ctx.exception.live_state["has_live_state"])
        self.assertEqual(len(ctx.exception.live_state["resting_orders"]), 1)
        self.assertEqual(self._deleted, [])

    def test_delete_blocks_on_open_position(self) -> None:
        self._patch_run(_open_position_run())
        with self.assertRaises(InstanceHasLiveBrokerStateError) as ctx:
            delete_trade_instance("run-2", "owner")
        self.assertEqual(ctx.exception.live_state["open_positions"], {"AAPL250117C00150000": 2})
        self.assertEqual(self._deleted, [])

    def test_force_delete_proceeds_and_reports_orphans(self) -> None:
        self._patch_run(_open_position_run())
        result = delete_trade_instance("run-2", "owner", force=True)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["forced"])
        self.assertIsNotNone(result["orphaned_broker_state"])
        self.assertEqual(self._deleted, ["run-2"])

    def test_clean_instance_deletes_without_force(self) -> None:
        self._patch_run(_clean_run())
        result = delete_trade_instance("run-3", "owner")
        self.assertTrue(result["deleted"])
        self.assertFalse(result["forced"])
        self.assertIsNone(result["orphaned_broker_state"])
        self.assertEqual(self._deleted, ["run-3"])

    def test_reset_risk_flags_local_only_when_live_state(self) -> None:
        self._patch_run(_open_position_run())
        result = reset_trade_instance_risk("run-2", "owner")
        self.assertEqual(result["broker_reconcile"], "local_only")
        self.assertTrue(result["live_broker_state"]["has_live_state"])


if __name__ == "__main__":
    unittest.main()

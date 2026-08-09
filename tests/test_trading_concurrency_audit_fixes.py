from __future__ import annotations

import unittest

from ai_option_scanner import trading_scheduler as tsched
from ai_option_scanner import trading_monitor as monitor


class TradingSchedulerLockTokenTest(unittest.TestCase):
    """The trading_scheduler leader lock used a constant value + unconditional
    redis_del, so a tick that overran its TTL could delete a peer's lock. It now
    uses a uuid token and releases only its own lock (token-matched Lua)."""

    def setUp(self) -> None:
        self._orig = {
            "redis_available": tsched.redis_available,
            "redis_setnx": tsched.redis_setnx,
            "redis_eval": tsched.redis_eval,
            "is_trading_day": tsched.is_nyse_trading_day,
            "list_enabled_configs": tsched.list_enabled_configs,
        }
        tsched.redis_available = lambda: True
        self.setnx_calls: list = []
        self.eval_calls: list = []
        tsched.redis_setnx = lambda name, value, ttl: (self.setnx_calls.append((name, value, ttl)) or True)
        tsched.redis_eval = lambda script, keys, args: (self.eval_calls.append((script, tuple(keys), tuple(args))) or 1)
        # No trading day -> tick returns early but still acquires+releases the lock.
        tsched.is_nyse_trading_day = lambda d: (False, "weekend")
        tsched.list_enabled_configs = lambda: []

    def tearDown(self) -> None:
        tsched.redis_available = self._orig["redis_available"]
        tsched.redis_setnx = self._orig["redis_setnx"]
        tsched.redis_eval = self._orig["redis_eval"]
        tsched.is_nyse_trading_day = self._orig["is_trading_day"]
        tsched.list_enabled_configs = self._orig["list_enabled_configs"]

    def test_acquires_with_token_and_releases_own_lock(self) -> None:
        tsched._tick()
        self.assertEqual(len(self.setnx_calls), 1)
        token = self.setnx_calls[0][1]
        self.assertNotEqual(token, "1")
        self.assertGreaterEqual(len(token), 8)
        releases = [c for c in self.eval_calls if c[0] == tsched._RELEASE_LUA]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0][2][0], token)  # ARGV[1] == our token

    def test_not_leader_returns_without_release(self) -> None:
        tsched.redis_setnx = lambda name, value, ttl: False
        tsched._tick()
        self.assertEqual(self.eval_calls, [])


class MonitorPoisonRunIsolationTest(unittest.TestCase):
    """A single run raising in the monitor loop must not skip every later run
    (which would leave their positions unprotected). Each run is isolated."""

    def setUp(self) -> None:
        self._orig = {
            "redis_available": monitor.redis_available,
            "list_trading_runs": monitor.list_trading_runs,
            "get_trading_run": monitor.get_trading_run,
            "process": monitor._monitor_process_one_run,
            "_has_order": monitor._has_monitorable_order,
            "_has_strategy": monitor._has_monitorable_strategy,
        }
        monitor.redis_available = lambda: False
        monitor._has_monitorable_order = lambda orders: True
        monitor._has_monitorable_strategy = lambda inst: False
        monitor.get_trading_run = lambda rid, owner=None: None

    def tearDown(self) -> None:
        monitor.redis_available = self._orig["redis_available"]
        monitor.list_trading_runs = self._orig["list_trading_runs"]
        monitor.get_trading_run = self._orig["get_trading_run"]
        monitor._monitor_process_one_run = self._orig["process"]
        monitor._has_monitorable_order = self._orig["_has_order"]
        monitor._has_monitorable_strategy = self._orig["_has_strategy"]

    def test_poison_run_does_not_starve_later_runs(self) -> None:
        runs = [
            {"id": "r1", "owner_id": "o", "orders": [{}]},
            {"id": "bad", "owner_id": "o", "orders": [{}]},
            {"id": "r3", "owner_id": "o", "orders": [{}]},
        ]
        monitor.list_trading_runs = lambda owner, limit, summary=False: runs
        processed: list[str] = []

        def fake_process(run, summary):
            if run["id"] == "bad":
                raise RuntimeError("poison")
            processed.append(run["id"])

        monitor._monitor_process_one_run = fake_process
        result = monitor.monitor_pending_stops(limit=10)
        # r1 and r3 still processed despite "bad" throwing.
        self.assertEqual(processed, ["r1", "r3"])
        self.assertEqual(result.get("runs_errored"), 1)


class ResidualFlatConfirmTest(unittest.TestCase):
    """A residual leg must not be declared flat (disarming software protection)
    off a single 'not found' positions() response — require N cycles."""

    def setUp(self) -> None:
        self._orig_positions = monitor.positions
        self._orig_find = monitor._find_position_row
        self._orig_position_quantity = monitor._position_quantity
        # Always return an empty book -> contract not found.
        monitor.positions = lambda account: []
        monitor._find_position_row = lambda rows, sym: None

    def tearDown(self) -> None:
        monitor.positions = self._orig_positions
        monitor._find_position_row = self._orig_find
        monitor._position_quantity = self._orig_position_quantity

    def test_requires_two_cycles_before_flat(self) -> None:
        order = {
            "residual_leg_tracking_active": True,
            "residual_leg_contract_symbol": "TSLA260626C00380000",
            "software_stop_active": True,
        }
        # Cycle 1: not confirmed flat yet, protection stays armed.
        r1 = monitor._try_residual_position_reconcile(order, "acct")
        self.assertEqual(r1["closed"], 0)
        self.assertTrue(order["residual_leg_tracking_active"])
        self.assertTrue(order["software_stop_active"])
        self.assertEqual(order["residual_position_flat_cycles"], 1)
        # Cycle 2: threshold reached -> marked flat, protection disarmed.
        r2 = monitor._try_residual_position_reconcile(order, "acct")
        self.assertEqual(r2["closed"], 1)
        self.assertFalse(order["residual_leg_tracking_active"])
        self.assertFalse(order["software_stop_active"])

    def test_found_position_resets_flat_counter(self) -> None:
        order = {
            "residual_leg_tracking_active": True,
            "residual_leg_contract_symbol": "TSLA260626C00380000",
            "residual_position_flat_cycles": 1,
        }
        monitor._find_position_row = lambda rows, sym: {"quantity": 4}
        monitor._position_quantity = lambda row: 4
        r = monitor._try_residual_position_reconcile(order, "acct")
        self.assertEqual(r["closed"], 0)
        self.assertTrue(order["residual_leg_tracking_active"])
        self.assertNotIn("residual_position_flat_cycles", order)


if __name__ == "__main__":
    unittest.main()

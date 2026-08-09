"""Regression tests for Phase 3: concurrency, fail-closed locks, escalation."""
from __future__ import annotations

import threading
import unittest

from ai_option_scanner import trading_agent, trading_monitor
from ai_option_scanner.trading_locks import run_action_lock


class FailClosedLockTest(unittest.TestCase):
    def setUp(self):
        self._orig_redis_available = trading_agent.redis_available
        self._orig_require = trading_agent._require_distributed_lock
        self._orig_finish = trading_agent._finish_trading_run
        self._orig_get = trading_agent.get_trading_run
        self._orig_body = trading_agent._execute_trading_run_body

    def tearDown(self):
        trading_agent.redis_available = self._orig_redis_available
        trading_agent._require_distributed_lock = self._orig_require
        trading_agent._finish_trading_run = self._orig_finish
        trading_agent.get_trading_run = self._orig_get
        trading_agent._execute_trading_run_body = self._orig_body

    def test_refuses_to_execute_without_redis_lock(self):
        finishes = []
        body_calls = []
        trading_agent.redis_available = lambda: False
        trading_agent._require_distributed_lock = lambda: True
        trading_agent.get_trading_run = lambda *a, **k: {"trade_instance": {}}
        trading_agent._finish_trading_run = lambda *a, **k: finishes.append(k.get("status") or (a[3] if len(a) > 3 else None))
        trading_agent._execute_trading_run_body = lambda *a, **k: body_calls.append(True)

        trading_agent.execute_trading_run("run-x", "owner", {"live_enabled": True, "total_capital": 1000, "universe": ["SPY"]})
        # The run body must NOT run; the run is finished as failed instead.
        self.assertEqual(body_calls, [])
        self.assertTrue(finishes)

    def test_allows_execution_when_lock_disabled(self):
        body_calls = []
        trading_agent.redis_available = lambda: False
        trading_agent._require_distributed_lock = lambda: False  # kill-switch off
        trading_agent._execute_trading_run_body = lambda *a, **k: body_calls.append(True)
        trading_agent._force_finish_stuck_running = lambda *a, **k: None
        trading_agent.execute_trading_run("run-y", "owner", {"live_enabled": True, "total_capital": 1000, "universe": ["SPY"]})
        self.assertEqual(body_calls, [True])


class RunActionLockTest(unittest.TestCase):
    def test_mutual_exclusion_same_run(self):
        run_id = "lock-test-run"
        with run_action_lock(run_id) as a:
            self.assertTrue(a)
            # A second acquire from "another actor" should fail fast (busy).
            result = {}
            def grab():
                with run_action_lock(run_id, timeout_seconds=0.2) as b:
                    result["acquired"] = b
            t = threading.Thread(target=grab)
            t.start(); t.join()
            self.assertFalse(result["acquired"])

    def test_lock_released_after_block(self):
        run_id = "lock-test-run-2"
        with run_action_lock(run_id) as a:
            self.assertTrue(a)
        # Now reacquirable.
        with run_action_lock(run_id) as b:
            self.assertTrue(b)


class QuoteOutageEscalationTest(unittest.TestCase):
    def test_escalates_after_threshold(self):
        order: dict = {}
        # Default threshold is 5; below it, no manual-attention flag.
        for _ in range(4):
            trading_monitor._note_protection_quote_outage(order, "software_stop")
        self.assertNotIn("requires_manual_attention", order)
        trading_monitor._note_protection_quote_outage(order, "software_stop")  # 5th
        self.assertTrue(order.get("requires_manual_attention"))
        self.assertIn("_instance_event", order)

    def test_clear_resets_counter(self):
        order: dict = {}
        trading_monitor._note_protection_quote_outage(order, "software_stop")
        trading_monitor._clear_protection_quote_outage(order, "software_stop")
        self.assertEqual(order.get("software_stop_quote_outage_cycles"), 0)


if __name__ == "__main__":
    unittest.main()

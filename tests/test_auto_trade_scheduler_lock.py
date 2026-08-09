from __future__ import annotations

import unittest

from ai_option_scanner import auto_trade_scheduler as sched


class AutoTradeLeaderLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = {
            "auto_trade_enabled": sched.auto_trade_enabled,
            "redis_available": sched.redis_available,
            "redis_setnx": sched.redis_setnx,
            "redis_eval": sched.redis_eval,
            "list_due": sched.list_due_auto_trade_instances,
            "run_cycle": sched.run_auto_trade_cycle,
        }
        sched.auto_trade_enabled = lambda: True
        sched.redis_available = lambda: True
        self.setnx_calls: list[tuple] = []
        self.eval_calls: list[tuple] = []

        def fake_setnx(name, value, ttl):
            self.setnx_calls.append((name, value, ttl))
            return True  # we are the leader

        def fake_eval(script, keys, args):
            self.eval_calls.append((script, tuple(keys), tuple(args)))
            return 1

        sched.redis_setnx = fake_setnx
        sched.redis_eval = fake_eval

    def tearDown(self) -> None:
        sched.auto_trade_enabled = self._orig["auto_trade_enabled"]
        sched.redis_available = self._orig["redis_available"]
        sched.redis_setnx = self._orig["redis_setnx"]
        sched.redis_eval = self._orig["redis_eval"]
        sched.list_due_auto_trade_instances = self._orig["list_due"]
        sched.run_auto_trade_cycle = self._orig["run_cycle"]

    def test_lock_uses_unique_token_renews_per_instance_and_releases(self) -> None:
        instances = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        sched.list_due_auto_trade_instances = lambda limit=20: instances
        sched.run_auto_trade_cycle = lambda inst: {"ok": inst["id"]}

        result = sched._tick(limit=20)

        self.assertEqual(result["checked_count"], 3)
        # Acquired once with a non-trivial token (not the old "1").
        self.assertEqual(len(self.setnx_calls), 1)
        token = self.setnx_calls[0][1]
        self.assertNotEqual(token, "1")
        self.assertGreaterEqual(len(token), 8)
        # One renew per instance (3) + one release = 4 eval calls, all carrying the token.
        renews = [c for c in self.eval_calls if c[0] == sched._RENEW_LUA]
        releases = [c for c in self.eval_calls if c[0] == sched._RELEASE_LUA]
        self.assertEqual(len(renews), 3)
        self.assertEqual(len(releases), 1)
        for call in self.eval_calls:
            self.assertEqual(call[2][0], token)  # ARGV[1] is always our token

    def test_failing_instance_still_renews_and_releases(self) -> None:
        sched.list_due_auto_trade_instances = lambda limit=20: [{"id": "bad"}, {"id": "good"}]

        def run(inst):
            if inst["id"] == "bad":
                raise RuntimeError("cycle blew up")
            return {"ok": inst["id"]}

        sched.run_auto_trade_cycle = run
        result = sched._tick(limit=20)

        # Both processed (one error, one ok); lock renewed twice + released once.
        self.assertEqual(result["checked_count"], 2)
        self.assertEqual(len([c for c in self.eval_calls if c[0] == sched._RENEW_LUA]), 2)
        self.assertEqual(len([c for c in self.eval_calls if c[0] == sched._RELEASE_LUA]), 1)

    def test_not_leader_skips_without_running(self) -> None:
        sched.redis_setnx = lambda name, value, ttl: False
        ran: list = []
        sched.list_due_auto_trade_instances = lambda limit=20: ([ran.append(1)] and [])  # pragma: no cover
        sched.run_auto_trade_cycle = lambda inst: ran.append(inst)

        result = sched._tick(limit=20)
        self.assertEqual(result, {"skipped": "not_leader"})
        self.assertEqual(ran, [])
        self.assertEqual(self.eval_calls, [])


if __name__ == "__main__":
    unittest.main()

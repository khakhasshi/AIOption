from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from ai_option_scanner import thetadata_option_tool as theta


class CrossTalkClient:
    """A stand-in for ThetaClient's single gRPC session.

    The real session multiplexes responses by arrival order, so two threads
    issuing requests at once can read each other's frames. This fake reproduces
    that hazard: ``request`` records the symbol on shared state, sleeps to widen
    the race window, then returns whatever symbol is *currently* recorded. With
    a request lock held by the caller the recorded symbol can never change
    mid-call, so every thread reads back its own symbol; without it, overlapping
    calls return another thread's symbol (the AT&T-for-everything bug).
    """

    def __init__(self) -> None:
        self._inflight: str | None = None
        self.overlaps = 0
        self._guard = threading.Lock()

    def request(self, symbol: str) -> str:
        with self._guard:
            if self._inflight is not None:
                self.overlaps += 1
            self._inflight = symbol
        time.sleep(0.005)
        result = self._inflight
        with self._guard:
            self._inflight = None
        return result


class RequestSerializationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_singleton = theta._client_singleton
        self.fake = CrossTalkClient()
        theta._client_singleton = self.fake

    def tearDown(self) -> None:
        theta._client_singleton = self._saved_singleton

    def test_with_session_retry_serializes_concurrent_requests(self) -> None:
        symbols = [f"SYM{i}" for i in range(12)]

        def call(sym: str) -> str:
            return theta._with_session_retry(lambda client: client.request(sym))

        with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
            results = list(pool.map(call, symbols))

        # Each thread must read back its own symbol — no cross-talk.
        self.assertEqual(results, symbols)
        self.assertEqual(self.fake.overlaps, 0)

    def test_lock_is_non_reentrant(self) -> None:
        # Guards against someone swapping in an RLock, which would silently
        # re-permit nested requests and reopen the cross-talk window.
        acquired_first = theta._request_lock.acquire(blocking=False)
        self.assertTrue(acquired_first)
        try:
            reacquired = theta._request_lock.acquire(blocking=False)
            self.assertFalse(reacquired, "_request_lock must be a non-reentrant Lock")
            if reacquired:
                theta._request_lock.release()
        finally:
            theta._request_lock.release()


if __name__ == "__main__":
    unittest.main()

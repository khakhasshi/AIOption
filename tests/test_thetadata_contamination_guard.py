from __future__ import annotations

import sys
import types
import unittest
from datetime import timedelta

import pandas as pd

from ai_option_scanner import thetadata_option_tool as theta
from ai_option_scanner.time_utils import et_today


def _future(days: int):
    return et_today() + timedelta(days=days)


class _CleanClient:
    """Returns data whose root matches the requested symbol (happy path)."""

    def __init__(self, **kwargs):
        self.reset_count = 0

    def option_list_expirations(self, symbol):
        root = symbol[:-3] if symbol.endswith(".US") else symbol
        return pd.DataFrame([{"root": root, "expiration": _future(7)}])


class _CrossedRootClient:
    """Simulates a crossed gRPC response: a TSLA request returns AT&T ('T') rows."""

    def option_list_expirations(self, symbol):
        return pd.DataFrame([{"root": "T", "expiration": _future(7)}])


class _StaleExpirationClient:
    """Simulates a stale response: only past-dated (2012) expirations come back."""

    def option_list_expirations(self, symbol):
        root = symbol[:-3] if symbol.endswith(".US") else symbol
        return pd.DataFrame([{"root": root, "expiration": "2012-06-01"}])


class ContaminationGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        sys.modules["thetadata"] = types.SimpleNamespace(ThetaClient=_CleanClient)
        theta._option_expirations_cache.clear()
        theta._option_snapshot_cache.clear()
        self._reset_calls = []
        self._orig_reset = theta._reset_client

        def _spy_reset(stale):
            self._reset_calls.append(stale)
            return self._orig_reset(stale)

        theta._reset_client = _spy_reset

    def tearDown(self) -> None:
        theta._reset_client = self._orig_reset
        theta._client_singleton = None
        theta._option_expirations_cache.clear()
        theta._option_snapshot_cache.clear()

    def _install(self, client_cls) -> None:
        theta._client_singleton = client_cls()

    def test_clean_response_passes(self) -> None:
        self._install(_CleanClient)
        exps = theta._option_expirations_uncached("TSLA")
        self.assertEqual(len(exps), 1)
        self.assertEqual(self._reset_calls, [])

    def test_crossed_root_is_rejected_and_resets(self) -> None:
        self._install(_CrossedRootClient)
        with self.assertRaises(theta.ThetaDataUnavailable):
            theta._option_expirations_uncached("TSLA")
        # The crossed response must trigger a session reset to flush the stream.
        self.assertEqual(len(self._reset_calls), 1)

    def test_crossed_root_is_not_cached(self) -> None:
        self._install(_CrossedRootClient)
        with self.assertRaises(theta.ThetaDataUnavailable):
            theta._option_expirations_cache.get_or_set("TSLA", lambda: theta._option_expirations_uncached("TSLA"))
        # Nothing was cached, so a subsequent clean fetch is not poisoned.
        self.assertIsNone(theta._option_expirations_cache.get_stale("TSLA"))

    def test_only_past_dated_expirations_is_rejected_and_resets(self) -> None:
        self._install(_StaleExpirationClient)
        with self.assertRaises(theta.ThetaDataUnavailable):
            theta._option_expirations_uncached("TSLA")
        self.assertEqual(len(self._reset_calls), 1)


if __name__ == "__main__":
    unittest.main()

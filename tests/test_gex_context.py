from __future__ import annotations

import types
import unittest

from ai_option_scanner.intraday_option_tools import build_gex_context


def _cand(contract_symbol: str, side: str, strike: float, gamma: float, oi: float):
    return types.SimpleNamespace(
        contract_symbol=contract_symbol,
        side=side,
        strike=strike,
        gamma=gamma,
        open_interest=oi,
    )


class GexContextComputeTest(unittest.TestCase):
    """Direct coverage of the dealer-gamma-exposure compute core, independent of
    the scan/observation pipeline that consumes it."""

    def test_empty_inputs_return_unavailable(self) -> None:
        self.assertFalse(build_gex_context([], 100.0)["available"])
        # Spot <= 0 is also unavailable even with candidates.
        self.assertFalse(build_gex_context([_cand("C1", "call", 100, 0.05, 1000)], 0.0)["available"])

    def test_candidates_without_gamma_or_oi_are_skipped(self) -> None:
        # Zero gamma and zero OI rows carry no exposure → unavailable.
        rows = [_cand("C1", "call", 100, 0.0, 1000), _cand("P1", "put", 95, 0.05, 0)]
        self.assertFalse(build_gex_context(rows, 100.0)["available"])

    def test_call_heavy_book_is_positive_gamma_with_call_wall(self) -> None:
        # A dominant call strike above spot should set a positive-gamma regime and
        # surface that strike as the call wall.
        rows = [
            _cand("C_above", "call", 110, 0.08, 5000),
            _cand("P_below", "put", 90, 0.02, 500),
        ]
        ctx = build_gex_context(rows, 100.0)
        self.assertTrue(ctx["available"])
        self.assertEqual(ctx["regime"], "positive_gamma")
        self.assertEqual(ctx["call_wall"], 110.0)
        self.assertGreater(ctx["net_gex"], 0)

    def test_put_heavy_book_is_negative_gamma_with_put_wall(self) -> None:
        rows = [
            _cand("P_below", "put", 90, 0.08, 5000),
            _cand("C_above", "call", 110, 0.02, 500),
        ]
        ctx = build_gex_context(rows, 100.0)
        self.assertTrue(ctx["available"])
        self.assertEqual(ctx["regime"], "negative_gamma")
        self.assertEqual(ctx["put_wall"], 90.0)
        self.assertLess(ctx["net_gex"], 0)

    def test_gross_gex_accumulates_absolute_exposure(self) -> None:
        rows = [
            _cand("C", "call", 110, 0.05, 1000),
            _cand("P", "put", 90, 0.05, 1000),
        ]
        ctx = build_gex_context(rows, 100.0)
        # Gross is the sum of |exposure|; net nets the signs. Gross >= |net|.
        self.assertGreaterEqual(ctx["gross_gex"], abs(ctx["net_gex"]))


if __name__ == "__main__":
    unittest.main()

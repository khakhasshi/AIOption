from __future__ import annotations

import unittest
from types import SimpleNamespace

from ai_option_scanner.ai_decision_guard import build_strict_analysis_payload
from ai_option_scanner.intraday_option_tools import enrich_option_analysis, normalize_analysis_modules
from ai_option_scanner.market_structure import build_volume_profile, build_volatility_context
from ai_option_scanner.yfinance_option_tool import OptionCandidate


class MarketStructureAnalysisTest(unittest.TestCase):
    def test_volatility_context_builds_iv_rv_and_event_risk(self) -> None:
        daily = [{"close": 100 + index * 0.4} for index in range(70)]
        candidates = [
            _candidate("SPY260619C00500000", "call", 500, 0.42),
            _candidate("SPY260619P00490000", "put", 490, 0.38),
        ]

        context = build_volatility_context(candidates, daily, [{"title": "SPY earnings guidance raises event risk"}])

        self.assertTrue(context["available"])
        self.assertGreater(context["rv20"], 0)
        self.assertGreater(context["iv_rv_ratio"], 0)
        self.assertEqual(context["event_risk"]["state"], "high")

    def test_volume_profile_finds_poc_and_side_score_fields_on_candidate(self) -> None:
        intraday = []
        for index in range(40):
            price = 498 + index * 0.1
            intraday.append({"price": price, "volume": 5000 if 499.8 <= price <= 500.2 else 500})
        daily = [
            {
                "close": 496 + index * 0.2 + (0.8 if index % 3 == 0 else -0.4),
                "high": 497 + index * 0.2 + (0.8 if index % 3 == 0 else -0.4),
                "low": 495 + index * 0.2 + (0.8 if index % 3 == 0 else -0.4),
                "volume": 1000000,
            }
            for index in range(70)
        ]
        profile = build_volume_profile(intraday, daily, 501.5)

        self.assertTrue(profile["available"])
        self.assertGreater(profile["poc"], 499)

        enriched = enrich_option_analysis(
            [_candidate("SPY260619C00505000", "call", 505, 0.35)],
            501.5,
            SimpleNamespace(day_trade=True, lottery=False, cheap=False),
            normalize_analysis_modules({"market_structure": True, "volatility": True}),
            daily_candles=daily,
            intraday_points=intraday,
            news_items=[],
        )

        self.assertEqual(enriched[0].volume_profile_position, profile["position"])
        self.assertNotEqual(enriched[0].market_structure_score, 0.0)
        self.assertIn("volume_profile_note", enriched[0].risk_plan)
        self.assertGreater(enriched[0].iv_rv_ratio, 0)

    def test_strict_ai_payload_carries_volatility_and_volume_profile_context(self) -> None:
        payload = {
            "symbol": "SPY",
            "quote": {"last": 501.5},
            "volatility_context": {"available": True, "rv20": 0.22, "rv60": 0.19, "iv_rv_ratio": 1.35},
            "volume_profile": {"available": True, "poc": 500.0, "value_area_low": 496.0, "value_area_high": 505.0},
            "option_candidates": [
                {
                    "contract_symbol": "SPY260619C00500000",
                    "side": "call",
                    "expiration": "2026-06-19",
                    "strike": 500,
                    "bid": 1.9,
                    "ask": 2.1,
                    "implied_volatility": 0.3,
                    "rv20": 0.22,
                    "rv60": 0.19,
                    "iv_rv_ratio": 1.36,
                    "iv_edge_state": "fair",
                    "event_risk_score": 20,
                    "market_structure_score": 1.2,
                    "volume_profile_state": "supportive",
                    "volume_profile_position": "inside_value",
                    "volume_profile_poc": 500.0,
                    "volume_profile_value_area_low": 496.0,
                    "volume_profile_value_area_high": 505.0,
                    "market_structure_flags": ["price holding above POC"],
                    "risk_plan": {"volume_profile_note": "POC 500.0 provides nearby support"},
                }
            ],
        }

        strict = build_strict_analysis_payload(payload)
        candidate = strict["option_candidates"][0]

        self.assertEqual(strict["volatility_context"]["rv20"], 0.22)
        self.assertEqual(strict["volume_profile"]["poc"], 500.0)
        self.assertEqual(candidate["iv_rv_ratio"], 1.36)
        self.assertEqual(candidate["volume_profile_poc"], 500.0)
        self.assertEqual(candidate["market_structure_flags"], ["price holding above POC"])


def _candidate(symbol: str, side: str, strike: float, iv: float) -> OptionCandidate:
    return OptionCandidate(
        contract_symbol=symbol,
        expiration="2026-06-19",
        side=side,
        strike=strike,
        last_price=2.0,
        bid=1.9,
        ask=2.1,
        volume=1200,
        open_interest=5000,
        implied_volatility=iv,
        in_the_money=False,
        moneyness_pct=1.0,
        spread_pct=9.5,
        score=10.0,
    ).with_greeks(
        delta=0.45 if side == "call" else -0.45,
        gamma=0.04,
        theta_per_day=-0.04,
        breakeven=strike + 2.1 if side == "call" else strike - 2.1,
        move_to_strike_pct=1.0,
        move_to_breakeven_pct=1.5,
        days_to_expiration=14,
    )


if __name__ == "__main__":
    unittest.main()

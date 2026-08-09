from __future__ import annotations

import unittest

from ai_option_scanner.decision_gate import build_decision_environment_gate
from ai_option_scanner import trading_agent
from ai_option_scanner.trading_agent import _contract_opportunities, _strategy_opportunities


class StrategyFamilyGateTest(unittest.TestCase):
    def test_range_positive_gamma_prefers_income_and_time_spreads(self) -> None:
        gate = build_decision_environment_gate(
            technical_bias="neutral",
            daily_summary={"change_pct": 0.1},
            intraday_summary={"change_pct": 0.05, "vs_vwap_pct": 0.05},
            intraday_tools={
                "multi_timeframe_trend": {"15m": {"state": "flat"}, "5m": {"state": "flat"}, "1m": {"state": "flat"}},
                "vwap_structure": {"state": "near_vwap", "vs_vwap_pct": 0.05},
                "opening_ranges": {"15m": {"state": "inside_range"}},
                "relative_volume": {"rvol_time_adjusted": 0.9},
            },
            gex_context={"available": True, "regime": "positive_gamma", "nearest_wall_distance_pct": 1.0},
            candidates=[_candidate()],
        )

        allowed = gate["allowed_strategy_families"]
        self.assertTrue(allowed["calendar"])
        self.assertTrue(allowed["iron_condor"])
        self.assertTrue(allowed["butterfly"])
        self.assertFalse(allowed["diagonal"])

    def test_low_gate_controls_soft_single_leg_override(self) -> None:
        kwargs = dict(
            technical_bias="bullish",
            daily_summary={"change_pct": 0.1},
            intraday_summary={"change_pct": 0.2, "vs_vwap_pct": 0.1},
            intraday_tools={
                "multi_timeframe_trend": {"15m": {"state": "up"}, "5m": {"state": "flat"}, "1m": {"state": "flat"}},
                "vwap_structure": {"state": "bullish_hold_above_vwap", "vs_vwap_pct": 0.1},
                "opening_ranges": {"15m": {"state": "breakout_above_orh"}},
                "relative_volume": {"rvol_time_adjusted": 1.4},
            },
            gex_context={"available": True, "regime": "positive_gamma", "nearest_wall_distance_pct": 2.0},
            candidates=[_candidate()],
        )

        normal_gate = build_decision_environment_gate(**kwargs)
        low_gate = build_decision_environment_gate(**kwargs, low_gate_enabled=True)

        self.assertFalse(normal_gate["low_gate_enabled"])
        self.assertFalse(normal_gate["allow_single_leg"])
        self.assertTrue(low_gate["low_gate_enabled"])
        self.assertTrue(low_gate["allow_single_leg"])

    def test_negative_gamma_momentum_blocks_pinning_structures(self) -> None:
        gate = build_decision_environment_gate(
            technical_bias="bullish",
            daily_summary={"change_pct": 1.2},
            intraday_summary={"change_pct": 0.8, "vs_vwap_pct": 0.4},
            intraday_tools={
                "multi_timeframe_trend": {"15m": {"state": "up"}, "5m": {"state": "up"}, "1m": {"state": "up"}},
                "vwap_structure": {"state": "bullish_hold_above_vwap", "vs_vwap_pct": 0.4},
                "opening_ranges": {"15m": {"state": "breakout_above_orh"}},
                "relative_volume": {"rvol_time_adjusted": 1.8},
                "ema_trend": {"state": "bullish_stack"},
                "macd_momentum": {"state": "bullish_momentum"},
            },
            gex_context={"available": True, "regime": "negative_gamma"},
            candidates=[_candidate(iv_percentile=55)],
        )

        allowed = gate["allowed_strategy_families"]
        self.assertTrue(allowed["spread"])
        self.assertFalse(allowed["calendar"])
        self.assertFalse(allowed["iron_condor"])
        self.assertFalse(allowed["butterfly"])

    def test_strategy_opportunities_filter_family_before_limit(self) -> None:
        gate = {
            "should_trade": True,
            "allow_strategy": True,
            "allow_auto_trade": True,
            "allowed_strategy_families": {"calendar": False, "spread": True},
            "strategy_family_gates": {
                "calendar": {"allowed": False, "blockers": ["not suitable"], "reasons": [], "warnings": []},
                "spread": {"allowed": True, "blockers": [], "reasons": ["directional"], "warnings": []},
            },
        }
        rows = _strategy_opportunities(
            [
                {
                    "symbol": "SPY",
                    "decision_gate": gate,
                    "strategy_candidates": [
                        {"family": "calendar", "strategy_type": "call_calendar_spread", "expiration": "2026-06-19", "label": "blocked", "score": 99, "legs": []},
                        {"family": "spread", "strategy_type": "bull_call_spread", "expiration": "2026-06-19", "label": "allowed", "score": 80, "legs": []},
                    ],
                }
            ],
            {"strategy_auto_execute_enabled": False, "strategy_modes": ["calendar", "spread"], "candidates_per_symbol": 1},
            account_name=None,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["family"], "spread")

    def test_strategy_opportunities_keep_all_enabled_families_before_symbol_cap(self) -> None:
        gate = {
            "should_trade": True,
            "allow_strategy": True,
            "allow_auto_trade": True,
            "allowed_strategy_families": {"calendar": True, "spread": True, "iron_condor": True, "butterfly": True},
            "strategy_family_gates": {
                "calendar": {"allowed": True, "blockers": [], "reasons": [], "warnings": []},
                "spread": {"allowed": True, "blockers": [], "reasons": [], "warnings": []},
                "iron_condor": {"allowed": True, "blockers": [], "reasons": [], "warnings": []},
                "butterfly": {"allowed": True, "blockers": [], "reasons": [], "warnings": []},
            },
        }
        rows = _strategy_opportunities(
            [
                {
                    "symbol": "SPY",
                    "decision_gate": gate,
                    "strategy_candidates": [
                        {"family": "spread", "strategy_type": "bull_call_spread", "expiration": "2026-06-19", "label": "spread", "score": 95, "legs": []},
                        {"family": "calendar", "strategy_type": "call_calendar_spread", "expiration": "2026-06-19", "label": "calendar", "score": 90, "legs": []},
                        {"family": "iron_condor", "strategy_type": "iron_condor", "expiration": "2026-06-19", "label": "ic", "score": 85, "legs": []},
                        {"family": "butterfly", "strategy_type": "call_butterfly", "expiration": "2026-06-19", "label": "fly", "score": 80, "legs": []},
                    ],
                }
            ],
            {"strategy_auto_execute_enabled": True, "strategy_modes": ["spread", "calendar", "iron_condor", "butterfly"], "candidates_per_symbol": 1},
            account_name=None,
        )

        self.assertEqual(len(rows), 4)
        self.assertEqual({row["family"] for row in rows}, {"spread", "calendar", "iron_condor", "butterfly"})

    def test_strategy_opportunity_keys_are_unique_across_symbols_and_legs(self) -> None:
        gate = {"should_trade": True, "allow_strategy": True, "allow_auto_trade": True}
        rows = _strategy_opportunities(
            [
                {
                    "symbol": "NVDA",
                    "decision_gate": gate,
                    "strategy_candidates": [
                        _strategy_candidate_with_key("spread::bear_put_spread::2026-05-29::看跌价差", "NVDA260529P00210000", "NVDA260529P00207500"),
                    ],
                },
                {
                    "symbol": "AMZN",
                    "decision_gate": gate,
                    "strategy_candidates": [
                        _strategy_candidate_with_key("spread::bear_put_spread::2026-05-29::看跌价差", "AMZN260529P00260000", "AMZN260529P00257500"),
                    ],
                },
            ],
            {"strategy_auto_execute_enabled": True, "strategy_modes": ["spread"], "candidates_per_symbol": 1},
            account_name=None,
        )

        keys = [row["strategy_key"] for row in rows]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(any(key.startswith("NVDA::spread::bear_put_spread") for key in keys))
        self.assertTrue(any(key.startswith("AMZN::spread::bear_put_spread") for key in keys))

    def test_strategy_only_live_scan_does_not_expose_single_leg_candidates(self) -> None:
        original_run_scan = trading_agent.run_scan
        try:
            trading_agent.run_scan = lambda **kwargs: {
                "answer": "ok",
                "payload": {
                    "technical_bias": "bullish",
                    "daily_summary": {"change_pct": 1.0},
                    "intraday_summary": {"change_pct": 0.5},
                    "decision_gate": {"should_trade": True, "allow_single_leg": True, "allow_strategy": True, "allow_auto_trade": True},
                    "option_candidates": [
                        {
                            "contract_symbol": "SPY260605C00500000",
                            "side": "call",
                            "ask": 1.2,
                            "bid": 1.1,
                            "spread_pct": 5,
                            "volume": 1000,
                            "open_interest": 5000,
                            "decision_score": 99,
                        }
                    ],
                    "strategy_candidates": [
                        {
                            "family": "credit_spread",
                            "strategy_type": "bull_put_spread",
                            "expiration": "2026-06-05",
                            "label": "Bull Put Spread",
                            "score": 72,
                            "legs": [],
                        }
                    ],
                },
            }
            config = {"universe": ["SPY"], "strategy_modes": ["credit_spread"], "candidates_per_symbol": 3}

            rows = trading_agent._scan_universe(config, None, "owner-a")

            self.assertEqual(rows[0]["status"], "succeeded")
            self.assertIsNone(rows[0]["candidate"])
            self.assertEqual(rows[0]["candidates"], [])
            self.assertEqual(rows[0]["evidence_card"]["candidate_count_sent_to_council"], 0)
            self.assertEqual(_contract_opportunities(rows, config), [])
        finally:
            trading_agent.run_scan = original_run_scan

    def test_contract_opportunities_reject_option_root_mismatch(self) -> None:
        rows = [
            {
                "symbol": "SPY",
                "decision_gate": {"should_trade": True, "allow_single_leg": True, "allow_auto_trade": True},
                "candidates": [
                    {"contract_symbol": "T260626C00023500", "ask": 0.34, "bid": 0.33, "spread_pct": 3, "volume": 1000, "open_interest": 5000, "execution_quality_score": 70, "trigger_score": 100},
                    {"contract_symbol": "SPY260626C00590000", "ask": 1.2, "bid": 1.18, "spread_pct": 2, "volume": 1000, "open_interest": 5000, "execution_quality_score": 65, "trigger_score": 100},
                ],
            }
        ]

        opportunities = _contract_opportunities(rows, {"strategy_modes": ["single_leg"], "candidates_per_symbol": 3})

        self.assertEqual([row["contract_symbol"] for row in opportunities], ["SPY260626C00590000"])

    def test_scan_universe_marks_root_mismatch_as_data_integrity_blocked(self) -> None:
        original_run_scan = trading_agent.run_scan
        try:
            trading_agent.run_scan = lambda **kwargs: {
                "answer": "**T 扫描结论**",
                "payload": {
                    "technical_bias": "mixed",
                    "daily_summary": {},
                    "intraday_summary": {},
                    "decision_gate": {"should_trade": True, "allow_single_leg": True, "allow_auto_trade": True},
                    "option_candidates": [
                        {"contract_symbol": "T260626C00023500", "ask": 0.34, "bid": 0.33, "spread_pct": 3, "volume": 1000, "open_interest": 5000, "execution_quality_score": 70, "trigger_score": 100},
                    ],
                    "strategy_candidates": [],
                },
            }

            rows = trading_agent._scan_universe(
                {"universe": ["SPY"], "strategy_modes": ["single_leg"], "candidates_per_symbol": 3},
                None,
                "owner-a",
            )

            self.assertEqual(rows[0]["status"], "data_integrity_blocked")
            self.assertEqual(rows[0]["candidates"], [])
            self.assertEqual(rows[0]["data_integrity"]["rejected_count"], 1)
            self.assertIn("T!=SPY", rows[0]["data_integrity"]["rejected"][0]["reason"])
        finally:
            trading_agent.run_scan = original_run_scan

    def test_strategy_opportunities_reject_leg_root_mismatch(self) -> None:
        gate = {"should_trade": True, "allow_strategy": True, "allow_auto_trade": True}
        rows = _strategy_opportunities(
            [
                {
                    "symbol": "SPY",
                    "decision_gate": gate,
                    "strategy_candidates": [
                        _strategy_candidate_with_key("spread::bad", "T260626C00023500", "T260626C00024000"),
                        _strategy_candidate_with_key("spread::good", "SPY260626C00590000", "SPY260626C00600000"),
                    ],
                }
            ],
            {"strategy_auto_execute_enabled": True, "strategy_modes": ["spread"], "candidates_per_symbol": 3},
            account_name=None,
        )

        self.assertEqual(len(rows), 1)
        self.assertIn("SPY260626C00590000", rows[0]["strategy_key"])

    def test_single_leg_order_submit_is_blocked_when_mode_excludes_single_leg(self) -> None:
        orders = trading_agent._submit_orders(
            [
                {
                    "symbol": "SPY",
                    "contract_symbol": "SPY260605C00500000",
                    "order_symbol": "SPY260605C00500000",
                    "entry_price": 1.0,
                    "allocation_pct": 0.1,
                    "stop_loss_pct": 25,
                }
            ],
            {"strategy_modes": ["credit_spread"], "total_capital": 10000},
            "paper",
        )

        self.assertEqual(orders[0]["status"], "skipped_single_leg_not_allowed")

    def test_single_leg_order_submit_rejects_option_root_mismatch(self) -> None:
        orders = trading_agent._submit_orders(
            [
                {
                    "symbol": "QQQ",
                    "contract_symbol": "T260626C00023500",
                    "order_symbol": "T260626C23500.US",
                    "entry_price": 0.34,
                    "allocation_pct": 0.1,
                    "stop_loss_pct": 25,
                }
            ],
            {"strategy_modes": ["single_leg"], "total_capital": 10000},
            "paper",
        )

        self.assertEqual(orders[0]["status"], "skipped_contract_symbol_mismatch")
        self.assertIn("T!=QQQ", orders[0]["message"])


def _candidate(iv_percentile: float = 55) -> dict:
    return {
        "execution_quality_score": 82,
        "spread_pct": 4,
        "theta_to_ask_pct": 10,
        "iv_percentile": iv_percentile,
        "probability_breakeven": 40,
        "reward_risk_score": 85,
    }


def _strategy_candidate_with_key(strategy_key: str, long_contract: str, short_contract: str) -> dict:
    return {
        "strategy_key": strategy_key,
        "family": "spread",
        "strategy_type": "bear_put_spread",
        "expiration": "2026-05-29",
        "label": "看跌价差",
        "score": 95,
        "legs": [
            {"action": "buy", "contract_symbol": long_contract, "qty": 1},
            {"action": "sell", "contract_symbol": short_contract, "qty": 1},
        ],
    }


if __name__ == "__main__":
    unittest.main()

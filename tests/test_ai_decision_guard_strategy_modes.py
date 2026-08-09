from __future__ import annotations

import unittest

from ai_option_scanner.ai_decision_guard import extract_json_object, validate_analysis_decision
from ai_option_scanner.strategy_structures import STRATEGY_MODE_ORDER


class AiDecisionGuardStrategyModesTest(unittest.TestCase):
    def test_extract_json_object_repairs_unescaped_quotes_in_chinese_reason(self) -> None:
        parsed = extract_json_object(
            '{'
            '"advisor_key":"risk",'
            '"rejected":[{"strategy_key":"SPY::spread","reason":"fit_notes明确"方向不清，低优先级"，不适合进攻型方向策略。"}]'
            '}'
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["advisor_key"], "risk")
        self.assertIn("方向不清", parsed["rejected"][0]["reason"])

    def test_strategy_only_modes_reject_single_leg_ai_selection(self) -> None:
        for mode in [item for item in STRATEGY_MODE_ORDER if item != "single_leg"]:
            with self.subTest(mode=mode):
                validation = validate_analysis_decision(
                    {
                        "action": "trade",
                        "selection_type": "single_leg",
                        "selected_contract_symbol": "NVDA260529P00210000",
                        "selected_strategy_key": "",
                        "evidence": [
                            {"field": "option_candidates[0].decision_score", "value": 66.6},
                            {"field": "option_candidates[0].execution_score", "value": 95.1},
                            {"field": "option_candidates[0].risk_plan.max_loss_per_contract", "value": 213},
                        ],
                    },
                    {
                        "intent": {"strategy_modes": [mode]},
                        "decision_gate": {"should_trade": True, "allow_auto_trade": True},
                        "option_candidates": [_single_leg_candidate()],
                        "strategy_candidates": [_strategy_candidate(mode)],
                    },
                )

                self.assertFalse(validation["valid"])
                self.assertFalse(validation["execution_allowed"])
                self.assertIn("single_leg selection is not allowed by intent.strategy_modes", validation["errors"])

    def test_mixed_single_leg_and_strategy_mode_can_accept_single_leg(self) -> None:
        validation = validate_analysis_decision(
            {
                "action": "trade",
                "selection_type": "single_leg",
                "selected_contract_symbol": "NVDA260529P00210000",
                "selected_strategy_key": "",
                "evidence": [
                    {"field": "option_candidates[0].decision_score", "value": 66.6},
                    {"field": "option_candidates[0].execution_score", "value": 95.1},
                    {"field": "option_candidates[0].risk_plan.max_loss_per_contract", "value": 213},
                ],
            },
            {
                "intent": {"strategy_modes": ["single_leg", "credit_spread"]},
                "decision_gate": {"should_trade": True, "allow_auto_trade": True},
                "option_candidates": [_single_leg_candidate()],
                "strategy_candidates": [_strategy_candidate("credit_spread")],
            },
        )

        self.assertTrue(validation["valid"])
        self.assertTrue(validation["execution_allowed"])


def _single_leg_candidate() -> dict:
    return {
        "contract_symbol": "NVDA260529P00210000",
        "decision_score": 66.6,
        "analysis_score": 60,
        "alpha_score": 20,
        "execution_score": 95,
        "trigger_score": 100,
        "gamma": 0.04,
        "gex_regime": "negative_gamma",
        "risk_plan": {
            "max_loss_per_contract": 213,
            "stop_loss_option_price": 1.17,
            "take_profit_1": 3.83,
            "take_profit_2": 6.39,
            "latest_exit": "到期前1个交易日",
        },
        "decision_bucket": "execution_first",
        "execution_hard_flags": [],
    }


def _strategy_candidate(mode: str = "credit_spread") -> dict:
    return {
        "strategy_key": f"{mode}:example:2026-06-05:1",
        "strategy_type": "bull_put_spread",
        "family": mode,
        "score": 68.39,
        "max_loss": 160,
        "legs": [
            {"action": "sell", "contract_symbol": "NVDA260605P00212500"},
            {"action": "buy", "contract_symbol": "NVDA260605P00210000"},
        ],
    }


if __name__ == "__main__":
    unittest.main()

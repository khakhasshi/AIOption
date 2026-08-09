from __future__ import annotations

import unittest

from ai_option_scanner.smart_exit_rules import evaluate_exit_rules, normalize_exit_rules


class SmartExitRulesTest(unittest.TestCase):
    def test_option_greek_exit_matches_current_quote_metric(self) -> None:
        trigger = evaluate_exit_rules(
            rules=[{"type": "option_greek", "field": "delta", "operator": "<=", "value": 0.25, "reason": "delta faded"}],
            position={"symbol": "SPY"},
            current_price=1.2,
            entry_price=1.0,
            option_quote={"delta": 0.21},
        )

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["trigger"], "smart_option_delta_exit")
        self.assertEqual(trigger["reason"], "delta faded")

    def test_option_greek_change_exit_uses_entry_reference(self) -> None:
        rules = normalize_exit_rules(
            raw_conditions=[
                {"type": "option_greek_change", "field": "theta", "operator": "<=", "change_pct": -40, "reason": "theta deteriorated"}
            ],
            position={"entry_theta": -0.05},
        )

        trigger = evaluate_exit_rules(
            rules=rules,
            position={"symbol": "SPY", "entry_theta": -0.05},
            current_price=1.2,
            entry_price=1.0,
            option_quote={"raw": {"theta_per_day": -0.08}},
        )

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["trigger"], "smart_option_theta_change_exit")
        self.assertEqual(trigger["threshold"], -40)


if __name__ == "__main__":
    unittest.main()

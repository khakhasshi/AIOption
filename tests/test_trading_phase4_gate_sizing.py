"""Regression tests for Phase 4: gate-bypass + sizing fixes."""
from __future__ import annotations

import unittest

from ai_option_scanner import trading_agent


class RepairGateBlockTest(unittest.TestCase):
    def test_repair_skips_gate_blocked_opportunity(self):
        # AI produced no usable selections; the only opportunity to "repair" from
        # was blocked by the decision gate. It must NOT be auto-funded.
        candidate = {
            "contract_symbol": "SPY260101C500", "order_symbol": "SPY260101C500",
            "entry_price": 1.50, "ask": 1.50, "spread_pct": 5, "volume": 5000,
            "open_interest": 5000, "execution_quality_score": 80, "risk_plan": {},
        }
        opportunities = [{"contract_symbol": "SPY260101C500", "candidate": candidate, "decision_bucket": "blocked_execution"}]
        config = {"top_n": 1, "default_stop_loss_pct": 25, "default_take_profit_pct": 30, "total_capital": 10000}
        accepted, report = trading_agent._post_validate_and_repair_selections([], opportunities, config)
        self.assertEqual(accepted, [])
        reasons = [r for row in report["rejected"] for r in row.get("issues", [])]
        self.assertIn("decision_gate_blocked", reasons)


class StrategyBuyPowerLedgerTest(unittest.TestCase):
    def setUp(self):
        self._orig_bp = trading_agent._buy_power_for_account
        self._orig_unit = trading_agent._strategy_unit_capital_required

    def tearDown(self):
        trading_agent._buy_power_for_account = self._orig_bp
        trading_agent._strategy_unit_capital_required = self._orig_unit

    def test_cash_secured_put_subtracts_committed(self):
        # $10k buying power, each put needs $5k → first position can do 2 units,
        # but after committing $10k the second should see 0 available.
        trading_agent._buy_power_for_account = lambda acct: 10000.0
        trading_agent._strategy_unit_capital_required = lambda pos: 5000.0
        pos = {"family": "cash_secured_put", "capital_required": 5000.0}
        first = trading_agent._strategy_max_executable_units(pos, "acct", {"buy_power": 0.0, "stock": {}})
        self.assertEqual(first, 2)
        after = trading_agent._strategy_max_executable_units(pos, "acct", {"buy_power": 10000.0, "stock": {}})
        self.assertEqual(after, 0)

    def test_covered_call_subtracts_committed_stock(self):
        self._orig_sq = trading_agent._strategy_stock_quantity
        trading_agent._strategy_stock_quantity = lambda pos, acct: 300
        try:
            pos = {"family": "covered_call", "symbol": "AAPL"}
            first = trading_agent._strategy_max_executable_units(pos, "acct", {"buy_power": 0.0, "stock": {}})
            self.assertEqual(first, 3)  # 300 // 100
            after = trading_agent._strategy_max_executable_units(pos, "acct", {"buy_power": 0.0, "stock": {"AAPL": 200}})
            self.assertEqual(after, 1)  # (300 - 200) // 100
        finally:
            trading_agent._strategy_stock_quantity = self._orig_sq


if __name__ == "__main__":
    unittest.main()

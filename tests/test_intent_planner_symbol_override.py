from __future__ import annotations

import json
import unittest
from unittest import mock

from ai_option_scanner import intent_planner


class IntentPlannerSymbolOverrideTest(unittest.TestCase):
    def test_explicit_symbol_overrides_llm_returned_symbol(self) -> None:
        # The LLM planner returns "T" (truncated/hallucinated) but the caller
        # passed the authoritative symbol "TSLA". The explicit symbol must win,
        # otherwise the scan fetches AT&T's chain and the data-integrity guard
        # blocks every cycle (contract_root_mismatch).
        llm_answer = json.dumps({"symbol": "T", "preferred_side": "call"})
        with mock.patch.object(intent_planner, "ask_ai", return_value=llm_answer):
            intent, plan = intent_planner.plan_scan_intent(
                "扫描 TSLA：找出最值得执行的期权方案", "TSLA", "deepseek"
            )
        self.assertEqual(intent.symbol, "TSLA")
        self.assertEqual(plan["symbol"], "TSLA")

    def test_llm_symbol_used_when_no_explicit_override(self) -> None:
        # Free-text scans with no UI symbol pick still let the planner choose.
        llm_answer = json.dumps({"symbol": "NVDA", "preferred_side": "call"})
        with mock.patch.object(intent_planner, "ask_ai", return_value=llm_answer):
            intent, _plan = intent_planner.plan_scan_intent(
                "找一个 NVDA 的看涨期权", None, "deepseek"
            )
        self.assertEqual(intent.symbol, "NVDA")


class RulesPathSymbolOverrideTest(unittest.TestCase):
    # The non-AI rules path (use_ai=False, used by auto-trade) goes through
    # plan_scan_intent_rules -> parse_query. parse_query scrapes a ticker from
    # the query text and lets it win over the passed symbol — fine for free-text
    # chat, but auto-trade prompts are templated prose whose first uppercase run
    # is the "T" inside an ISO timestamp ("...26T11:07"), mis-read as AT&T. The
    # explicit symbol must override it, mirroring the LLM path.
    AUTO_TRADE_PROMPT = (
        "【全自动交易 · 时段感知】现在是美东 2026-06-26T11:07，交易时段：上午 morning trend。"
        "\n【本次任务】扫描 SPY：按当前策略模式找出最值得执行的期权方案。"
    )

    def test_explicit_symbol_overrides_timestamp_t_in_prompt(self) -> None:
        intent, plan = intent_planner.plan_scan_intent_rules(self.AUTO_TRADE_PROMPT, "SPY")
        self.assertEqual(intent.symbol, "SPY")
        self.assertEqual(plan["symbol"], "SPY")

    def test_explicit_symbol_overrides_other_query_ticker(self) -> None:
        intent, _plan = intent_planner.plan_scan_intent_rules("扫描 TSLA 的期权", "NVDA")
        self.assertEqual(intent.symbol, "NVDA")

    def test_query_ticker_used_when_no_explicit_symbol(self) -> None:
        intent, _plan = intent_planner.plan_scan_intent_rules("扫描 TSLA 日内动量期权", None)
        self.assertEqual(intent.symbol, "TSLA")


if __name__ == "__main__":
    unittest.main()

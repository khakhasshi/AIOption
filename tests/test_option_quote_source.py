from __future__ import annotations

import sys
import types
import unittest

from ai_option_scanner import longbridge_client


class OptionQuoteSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._yf_module_name = "ai_option_scanner.yfinance_option_tool"
        self._theta_module_name = "ai_option_scanner.thetadata_option_tool"
        self._orig_yf_module = sys.modules.get(self._yf_module_name)
        self._orig_theta_module = sys.modules.get(self._theta_module_name)
        self.yf_module = types.ModuleType(self._yf_module_name)
        self.theta_module = types.ModuleType(self._theta_module_name)
        sys.modules[self._yf_module_name] = self.yf_module
        sys.modules[self._theta_module_name] = self.theta_module
        self._orig_lb_quote = longbridge_client.lb_quote_option_contract
        self._orig_sdk_allowed = longbridge_client.sdk_client.sdk_is_allowed
        self._orig_sdk_required = longbridge_client.sdk_client.sdk_is_required
        self.lb_calls = 0
        self.yf_calls = 0
        longbridge_client.sdk_client.sdk_is_allowed = lambda: True
        longbridge_client.sdk_client.sdk_is_required = lambda: False

    def tearDown(self) -> None:
        if self._orig_yf_module is None:
            sys.modules.pop(self._yf_module_name, None)
        else:
            sys.modules[self._yf_module_name] = self._orig_yf_module
        if self._orig_theta_module is None:
            sys.modules.pop(self._theta_module_name, None)
        else:
            sys.modules[self._theta_module_name] = self._orig_theta_module
        longbridge_client.lb_quote_option_contract = self._orig_lb_quote
        longbridge_client.sdk_client.sdk_is_allowed = self._orig_sdk_allowed
        longbridge_client.sdk_client.sdk_is_required = self._orig_sdk_required

    def test_option_quote_uses_thetadata_before_longbridge_and_yfinance(self) -> None:
        def yf_quote(symbol):
            self.yf_calls += 1
            return {
                "available": True,
                "contract_symbol": symbol,
                "bid": 1.2,
                "ask": 1.3,
                "pricing_source": "yfinance_test",
            }

        self.yf_module.quote_option_contract = yf_quote
        self.theta_module.quote_option_contract_live = lambda symbol: {
            "available": True,
            "bid": 2.0,
            "ask": 2.1,
            "pricing_source": "thetadata_live_test",
        }

        def lb_quote(symbol, account_name):
            self.lb_calls += 1
            return {"available": True, "bid": 9.0, "ask": 9.5}

        longbridge_client.lb_quote_option_contract = lb_quote
        row = longbridge_client.quote_option_contract("SPY260619C00500000", "paper")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "thetadata")
        self.assertEqual(row["provider_source"], "thetadata_live_test")
        self.assertEqual(self.lb_calls, 0)
        self.assertEqual(self.yf_calls, 0)

    def test_option_quote_falls_back_to_longbridge_before_yfinance(self) -> None:
        self.yf_module.quote_option_contract = lambda symbol: self.fail("yfinance must not run when Longbridge is available")
        self.theta_module.quote_option_contract_live = lambda symbol: {
            "available": False,
            "error": "theta unavailable",
        }
        symbols = []

        def lb_quote(symbol, account_name):
            self.lb_calls += 1
            symbols.append(symbol)
            return {"available": True, "bid": 1.15, "ask": 1.25, "pricing_source": "longbridge_test"}

        longbridge_client.lb_quote_option_contract = lb_quote
        row = longbridge_client.quote_option_contract("SPY260619C500000.US", "paper")

        self.assertTrue(row["available"])
        self.assertEqual(row["source"], "longbridge_sdk")
        self.assertEqual(row["provider_source"], "longbridge_test")
        self.assertEqual(row["fallback_from"], "thetadata")
        self.assertEqual(self.lb_calls, 1)
        self.assertEqual(symbols, ["SPY260619C500000.US"])

    def test_yfinance_is_last_fallback_and_marked_untrusted_for_execution(self) -> None:
        self.theta_module.quote_option_contract_live = lambda symbol: {"available": False, "error": "theta unavailable"}
        self.yf_module.quote_option_contract = lambda symbol: {
            "available": True,
            "bid": 1.2,
            "ask": 1.3,
            "pricing_source": "yfinance_test",
        }
        longbridge_client.lb_quote_option_contract = lambda symbol, account_name: {"available": False, "error": "lb unavailable"}

        row = longbridge_client.quote_option_contract("SPY260619C00500000", "paper")

        self.assertEqual(row["source"], "yfinance")
        self.assertFalse(row["execution_trusted"])
        self.assertEqual(row["fallback_from"], "thetadata,longbridge")

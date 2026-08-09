from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from ai_option_scanner import scan_service


class NormalizeDataSourceTest(unittest.TestCase):
    def test_market_source_defaults_and_auto_map_to_thetadata(self) -> None:
        self.assertEqual(scan_service._normalize_market_data_source(None), "thetadata")
        self.assertEqual(scan_service._normalize_market_data_source("auto"), "thetadata")
        self.assertEqual(scan_service._normalize_market_data_source("garbage"), "thetadata")

    def test_market_source_passthrough_known(self) -> None:
        for src in ("longbridge", "yfinance", "thetadata"):
            self.assertEqual(scan_service._normalize_market_data_source(src.upper()), src)

    def test_option_source_mirrors_market_source(self) -> None:
        self.assertEqual(scan_service._normalize_option_data_source(None), "thetadata")
        self.assertEqual(scan_service._normalize_option_data_source("auto"), "thetadata")
        self.assertEqual(scan_service._normalize_option_data_source("yfinance"), "yfinance")


class LongbridgeMarketDataFallbackTest(unittest.TestCase):
    def test_fetch_returns_normalized_secondary_source_payload(self) -> None:
        account = SimpleNamespace(name="paper", sdk_credentials_configured=True)
        with (
            patch.object(scan_service, "resolve_account", return_value=account),
            patch.object(scan_service, "quote", return_value={"last": 501.25, "time": "2026-07-10T14:30:00Z"}),
            patch.object(scan_service, "kline", return_value=[{"close": 500.0, "time": "2026-07-09T20:00:00Z"}]),
            patch.object(scan_service, "intraday", return_value=[{"price": 501.0, "time": "2026-07-10T14:30:00Z"}]),
            patch.object(scan_service, "news", return_value=[{"title": "test", "time": "2026-07-10T14:00:00Z"}]),
        ):
            account_name, quote, daily, intraday, news = scan_service._fetch_longbridge_market_data(
                "SPY.US",
                "paper",
                {},
                2,
            )

        self.assertEqual(account_name, "paper")
        self.assertEqual(quote["last"], 501.25)
        self.assertEqual(len(daily), 1)
        self.assertEqual(len(intraday), 1)
        self.assertEqual(news[0]["source"], "longbridge")


class DeriveOptionSideTest(unittest.TestCase):
    def test_preferred_side_wins(self) -> None:
        self.assertEqual(scan_service._derive_option_side("put", "bullish"), "put")
        self.assertEqual(scan_service._derive_option_side("call", "bearish"), "call")

    def test_bias_maps_to_side(self) -> None:
        self.assertEqual(scan_service._derive_option_side(None, "bullish"), "call")
        self.assertEqual(scan_service._derive_option_side(None, "bullish_strong"), "call")
        self.assertEqual(scan_service._derive_option_side(None, "bearish"), "put")
        self.assertIsNone(scan_service._derive_option_side(None, "neutral"))


class DeriveOptionSideForModesTest(unittest.TestCase):
    def test_two_legged_volatility_modes_have_no_single_side(self) -> None:
        for mode in ("straddle", "strangle", "iron_condor", "calendar", "diagonal", "collar"):
            self.assertIsNone(scan_service._derive_option_side_for_modes(None, "bullish", [mode]))

    def test_cash_secured_put_forces_put(self) -> None:
        self.assertEqual(scan_service._derive_option_side_for_modes(None, "bullish", ["cash_secured_put"]), "put")

    def test_covered_call_and_pmcc_force_call(self) -> None:
        self.assertEqual(scan_service._derive_option_side_for_modes(None, "bearish", ["covered_call"]), "call")
        self.assertEqual(scan_service._derive_option_side_for_modes(None, "bearish", ["poor_mans_covered_call"]), "call")

    def test_single_leg_falls_back_to_bias(self) -> None:
        # normalize_strategy_modes(None) -> ['single_leg'], so it defers to bias.
        self.assertEqual(scan_service._derive_option_side_for_modes(None, "bullish", None), "call")
        self.assertEqual(scan_service._derive_option_side_for_modes(None, "bearish", None), "put")


class NumAndFormatTest(unittest.TestCase):
    def test_num_coerces_and_defaults_zero(self) -> None:
        self.assertEqual(scan_service._num("3.5"), 3.5)
        self.assertEqual(scan_service._num(None), 0.0)
        self.assertEqual(scan_service._num("not-a-number"), 0.0)

    def test_round_renders_dashes_for_none(self) -> None:
        self.assertEqual(scan_service._round(None), "--")
        self.assertEqual(scan_service._round("1.2"), "1.20")
        self.assertEqual(scan_service._round(3), "3.00")

    def test_percent_format(self) -> None:
        self.assertEqual(scan_service._percent(12.5), "12.50%")


class ContractSymbolsInTextTest(unittest.TestCase):
    def test_extracts_occ_symbols_with_optional_space(self) -> None:
        text = "建议买入 SPY 260619C00500000 和 AAPL260117P00150000。"
        self.assertEqual(
            scan_service._contract_symbols_in_text(text),
            ["SPY260619C00500000", "AAPL260117P00150000"],
        )

    def test_empty_and_none_safe(self) -> None:
        self.assertEqual(scan_service._contract_symbols_in_text(""), [])
        self.assertEqual(scan_service._contract_symbols_in_text(None), [])


class CandidateToRowTest(unittest.TestCase):
    def test_dict_is_copied_not_aliased(self) -> None:
        src = {"a": 1}
        row = scan_service._candidate_to_row(src)
        row["a"] = 2
        self.assertEqual(src["a"], 1)

    def test_to_dict_method_is_used(self) -> None:
        class HasToDict:
            def to_dict(self):
                return {"x": 9}

        self.assertEqual(scan_service._candidate_to_row(HasToDict()), {"x": 9})

    def test_dataclass_is_supported(self) -> None:
        @dataclass
        class C:
            sym: str
            qty: int

        self.assertEqual(scan_service._candidate_to_row(C("AAPL", 2)), {"sym": "AAPL", "qty": 2})


class StrategyKeyTest(unittest.TestCase):
    def test_explicit_key_wins(self) -> None:
        self.assertEqual(scan_service._strategy_key({"strategy_key": "abc"}, 0), "abc")

    def test_composite_key_from_fields_spaces_to_underscore(self) -> None:
        row = {"family": "credit spread", "strategy_type": "bull put", "expiration": "2026-06-19", "label": "A"}
        self.assertEqual(scan_service._strategy_key(row, 3), "credit_spread::bull_put::2026-06-19::A")

    def test_composite_key_falls_back_to_index_when_label_missing(self) -> None:
        self.assertEqual(scan_service._strategy_key({}, 7), "strategy::type::exp::7")

    def test_strategy_to_row_stamps_key(self) -> None:
        row = scan_service._strategy_to_row({"family": "iron_condor"}, 2)
        self.assertEqual(row["strategy_key"], "iron_condor::type::exp::2")


if __name__ == "__main__":
    unittest.main()

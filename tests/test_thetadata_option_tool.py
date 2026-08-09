from __future__ import annotations

import sys
import types
import unittest
from datetime import date, timedelta

import pandas as pd

from ai_option_scanner import thetadata_option_tool as theta
from ai_option_scanner import scan_jobs, scan_service, trading_store
from ai_option_scanner import yfinance_option_tool as yf_tool
from ai_option_scanner.intraday_option_tools import enrich_option_greeks, supplement_option_greek_inputs_from_yfinance
from ai_option_scanner.time_utils import et_today
from ai_option_scanner.yfinance_option_tool import OptionCandidate


class FakeThetaClient:
    expiration = et_today() + timedelta(days=10)

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def stock_history_eod(self, symbol, start_date=None, end_date=None):
        return pd.DataFrame(
            [
                {"created": et_today() - timedelta(days=1), "open": 100, "high": 103, "low": 99, "close": 102, "volume": 1_000_000},
                {"created": et_today(), "open": 102, "high": 106, "low": 101, "close": 105, "volume": 1_200_000},
            ]
        )

    def stock_history_ohlc(self, symbol, date=None, interval="1m"):
        return pd.DataFrame(
            [
                {"timestamp": "2026-05-22T13:30:00", "open": 104, "high": 105, "low": 103, "close": 104.5, "volume": 1000, "vwap": 104.2},
                {"timestamp": "2026-05-22T13:31:00", "open": 104.5, "high": 105.5, "low": 104, "close": 105, "volume": 1200, "vwap": 104.6},
            ]
        )

    def stock_snapshot_quote(self, symbol):
        return pd.DataFrame([{"timestamp": "2026-05-22T20:00:00", "symbol": symbol, "bid": 104.95, "ask": 105.05}])

    def stock_snapshot_ohlc(self, symbol):
        return pd.DataFrame([{"timestamp": "2026-05-22T20:00:00", "symbol": symbol, "close": 105.0, "volume": 1_300_000}])

    def option_list_expirations(self, symbol):
        return pd.DataFrame([{"root": symbol, "expiration": self.expiration}])

    def option_snapshot_quote(self, symbol, expiration="*", strike="*", right="both"):
        return pd.DataFrame(
            [
                {"strike": 106, "right": "C", "bid": 1.2, "ask": 1.35},
                {"strike": 104, "right": "P", "bid": 1.0, "ask": 1.2},
            ]
        )

    def option_snapshot_open_interest(self, symbol, expiration="*", strike="*", right="both"):
        return pd.DataFrame(
            [
                {"strike": 106, "right": "C", "open_interest": 350},
                {"strike": 104, "right": "P", "open_interest": 300},
            ]
        )

    def option_snapshot_ohlc(self, symbol, expiration="*", strike="*", right="both"):
        return pd.DataFrame(
            [
                {"strike": 106, "right": "C", "close": 1.28, "volume": 90},
                {"strike": 104, "right": "P", "close": 1.1, "volume": 80},
            ]
        )

    def option_snapshot_greeks_implied_volatility(self, symbol, expiration="*", strike="*", right="both"):
        return pd.DataFrame(
            [
                {"strike": 106, "right": "C", "implied_vol": 0.31},
                {"strike": 104, "right": "P", "implied_vol": 0.33},
            ]
        )


class ThetaDataOptionToolTest(unittest.TestCase):
    def setUp(self) -> None:
        sys.modules["thetadata"] = types.SimpleNamespace(ThetaClient=FakeThetaClient)
        theta._client_singleton = None
        theta._client_credential_revision = None
        theta._market_data_cache.clear()
        theta._option_expirations_cache.clear()
        theta._option_snapshot_cache.clear()
        self._orig_yf_collect_candidates = yf_tool.collect_candidates

    def tearDown(self) -> None:
        yf_tool.collect_candidates = self._orig_yf_collect_candidates
        theta._client_singleton = None
        theta._client_credential_revision = None

    def test_market_data_normalizes_quote_daily_intraday(self) -> None:
        data = theta.market_data("SPY")

        self.assertEqual(data["quote"]["source"], "thetadata")
        self.assertEqual(data["quote"]["last"], 105.0)
        self.assertEqual(len(data["daily"]), 2)
        self.assertEqual(len(data["intraday"]), 2)
        self.assertEqual(data["news"], [])

    def test_thetadata_intraday_skips_holiday_zero_price_frame(self) -> None:
        class HolidayThenTradingClient:
            def stock_history_ohlc(self, symbol, date=None, interval="1m"):
                if date == date_from_iso("2026-05-25"):
                    return pd.DataFrame(
                        [
                            {"timestamp": "2026-05-25T09:30:00-04:00", "close": float("nan"), "volume": 0, "vwap": 0},
                            {"timestamp": "2026-05-25T09:31:00-04:00", "close": float("nan"), "volume": 0, "vwap": 0},
                        ]
                    )
                return pd.DataFrame(
                    [
                        {"timestamp": "2026-05-22T09:30:00-04:00", "close": 219.99, "volume": 1000, "vwap": 220.33},
                        {"timestamp": "2026-05-22T09:31:00-04:00", "close": 220.12, "volume": 900, "vwap": 220.3},
                        {"timestamp": "2026-05-22T16:00:00-04:00", "close": float("nan"), "volume": 0, "vwap": 217.02},
                    ]
                )

        frame = theta._latest_intraday_frame(HolidayThenTradingClient(), "NVDA", date_from_iso("2026-05-25"))
        rows = theta._intraday_rows(frame)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["time"], "2026-05-22T09:30:00-04:00")
        self.assertGreater(rows[-1]["price"], 0)

    def test_collect_candidates_uses_snapshot_quote_oi_ohlc_and_iv(self) -> None:
        rows = theta.collect_candidates(
            symbol="SPY",
            spot=105.0,
            min_days=1,
            max_days=30,
            max_ask=5,
            lottery=False,
            preferred_side=None,
        )

        self.assertEqual({item.side for item in rows}, {"call", "put"})
        self.assertTrue(all(item.pricing_source == "thetadata_bid_ask" for item in rows))
        self.assertEqual(rows[0].contract_symbol[:3], "SPY")
        self.assertTrue(any(item.implied_volatility == 0.31 for item in rows))

    def test_quote_option_contract_returns_merged_snapshot(self) -> None:
        contract = theta._contract_symbol("SPY", FakeThetaClient.expiration.isoformat(), "call", 106)

        quote = theta.quote_option_contract(contract)

        self.assertTrue(quote["available"])
        self.assertEqual(quote["source"], "thetadata")
        self.assertEqual(quote["ask"], 1.35)
        self.assertEqual(quote["open_interest"], 350)

    def test_thetadata_invalid_session_clears_cached_client_and_retries(self) -> None:
        future_expiration = (et_today() + timedelta(days=14)).isoformat()

        class SessionClient:
            attempts = 0

            def __init__(self, **kwargs):
                SessionClient.attempts += 1
                self.attempt = SessionClient.attempts

            def option_list_expirations(self, symbol):
                if self.attempt == 1:
                    raise RuntimeError("StatusCode.UNAUTHENTICATED: Invalid session ID")
                return pd.DataFrame([{"root": symbol, "expiration": date_from_iso(future_expiration)}])

        sys.modules["thetadata"] = types.SimpleNamespace(ThetaClient=SessionClient)
        theta._client_singleton = None
        theta._client_credential_revision = None
        theta._option_expirations_cache.clear()

        self.assertEqual(theta.option_expirations("SPY"), [future_expiration])
        self.assertEqual(SessionClient.attempts, 2)

    def test_thetadata_is_accepted_as_market_data_source(self) -> None:
        self.assertEqual(scan_service._normalize_market_data_source("thetadata"), "thetadata")
        self.assertEqual(scan_jobs._normalize_market_data_source("thetadata"), "thetadata")
        self.assertEqual(trading_store._normalize_market_data_source("thetadata"), "thetadata")
        self.assertEqual(scan_service._normalize_market_data_source(None), "thetadata")
        self.assertEqual(scan_service._normalize_market_data_source("auto"), "thetadata")
        self.assertEqual(scan_jobs._normalize_market_data_source(None), "thetadata")
        self.assertEqual(trading_store._normalize_market_data_source(None), "thetadata")

    def test_thetadata_candidates_reuse_yfinance_iv_for_greek_inputs(self) -> None:
        expiration = FakeThetaClient.expiration.isoformat()
        theta_candidate = OptionCandidate(
            contract_symbol=theta._contract_symbol("SPY", expiration, "call", 106),
            expiration=expiration,
            side="call",
            strike=106,
            last_price=1.28,
            bid=1.2,
            ask=1.35,
            volume=90,
            open_interest=350,
            implied_volatility=0.0,
            in_the_money=False,
            moneyness_pct=0.95,
            spread_pct=11.1,
            score=10,
            pricing_source="thetadata_bid_ask",
        )
        legacy_candidate = OptionCandidate(
            **{
                **theta_candidate.__dict__,
                "implied_volatility": 0.42,
                "pricing_source": "bid_ask",
                "quote_warning": "",
            }
        )
        yf_tool.collect_candidates = lambda **kwargs: [legacy_candidate]

        supplemented = supplement_option_greek_inputs_from_yfinance([theta_candidate], 105.0)
        enriched = enrich_option_greeks(supplemented, 105.0)

        self.assertEqual(supplemented[0].implied_volatility, 0.42)
        self.assertIn("yfinance implied volatility", supplemented[0].quote_warning)
        self.assertNotEqual(enriched[0].gamma, 0.0)

def date_from_iso(value: str) -> date:
    return date.fromisoformat(value)


if __name__ == "__main__":
    unittest.main()

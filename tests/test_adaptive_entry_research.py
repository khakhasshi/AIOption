from datetime import datetime

import pandas as pd
import pytest

from ai_option_scanner.time_utils import EASTERN
from scripts.research_adaptive_entry import (
    Leg,
    Sample,
    _close_cash,
    _entry_cash,
    _entry_time_for_policy,
    _risk_and_profit_basis,
    _sample_signature,
)


def _sample(*, direction: str = "bullish", strategy_type: str = "bull_call") -> Sample:
    return Sample(
        locator_id="TRD-TEST",
        created_at="2026-07-10T13:40:00Z",
        start_at=datetime(2026, 7, 10, 9, 40, tzinfo=EASTERN),
        symbol="SPY",
        strategy_type=strategy_type,
        direction=direction,
        units=1,
        stop_loss_pct=20,
        take_profit_pct=20,
        legs=[
            Leg("SPY260710C00620000", "buy", 1, "SPY", "2026-07-10", 620, "call"),
            Leg("SPY260710C00625000", "sell", 1, "SPY", "2026-07-10", 625, "call"),
        ],
    )


def test_executable_cash_uses_ask_to_buy_and_bid_to_sell() -> None:
    row = pd.Series({"ask_0": 2.10, "bid_0": 2.00, "ask_1": 1.10, "bid_1": 1.00})

    assert _entry_cash(row, _sample()) == -110
    assert _close_cash(row, _sample()) == pytest.approx(90)


def test_risk_basis_handles_debit_and_credit_spreads() -> None:
    sample = _sample()

    assert _risk_and_profit_basis(sample, -110) == (110, 110)
    assert _risk_and_profit_basis(sample, 120) == (380, 120)


def test_sample_signature_ignores_run_id_but_keeps_structure() -> None:
    first = _sample()
    second = _sample()
    second.locator_id = "TRD-OTHER"

    assert _sample_signature(first) == _sample_signature(second)
    second.legs[1].contract_symbol = "SPY260710C00630000"
    assert _sample_signature(first) != _sample_signature(second)


def test_vwap_reclaim_waits_for_opposite_touch_and_two_confirmations() -> None:
    sample = _sample()
    timestamps = pd.date_range(sample.start_at, periods=5, freq="min")
    stock = pd.DataFrame(
        {
            "timestamp": timestamps,
            "close": [99.0, 101.0, 102.0, 103.0, 104.0],
            "vwap": [100.0] * 5,
            "ema9": [99.0, 100.1, 101.0, 102.0, 103.0],
            "ema20": [99.5, 100.0, 100.2, 100.5, 101.0],
        }
    )

    entry = _entry_time_for_policy(sample, stock, "vwap_reclaim", 60)

    assert entry == timestamps[2].to_pydatetime()

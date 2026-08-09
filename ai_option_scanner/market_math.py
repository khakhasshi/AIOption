from __future__ import annotations

from datetime import date
from statistics import mean
from typing import Any


def f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _prev_calendar_month_prefix(candles: list[dict[str, Any]]) -> str | None:
    for item in reversed(candles):
        raw = str(item.get("time", ""))[:10]
        try:
            ref = date.fromisoformat(raw)
        except ValueError:
            continue
        year = ref.year if ref.month > 1 else ref.year - 1
        month = ref.month - 1 if ref.month > 1 else 12
        return f"{year:04d}-{month:02d}"
    return None


def summarize_daily(candles: list[dict[str, Any]]) -> dict[str, float]:
    closes = [f(item["close"]) for item in candles]
    highs = [f(item["high"]) for item in candles]
    lows = [f(item["low"]) for item in candles]
    volumes = [f(item.get("volume")) for item in candles]
    if len(closes) < 20:
        return {}

    true_ranges = [
        max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1]))
        for index in range(1, len(candles))
    ]
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes[-14:]]
    losses = [-min(change, 0.0) for change in changes[-14:]]
    average_gain = sum(gains) / 14
    average_loss = sum(losses) / 14
    rsi = 100 if average_loss == 0 else 100 - 100 / (1 + average_gain / average_loss)

    current = candles[-1]
    last_close = closes[-1]
    prev_close = closes[-2]
    day_pct = (last_close / prev_close - 1) * 100 if prev_close else 0.0
    atr14 = mean(true_ranges[-14:])
    atr14_pct = (atr14 / last_close * 100) if last_close else 0.0

    prev_month = _prev_calendar_month_prefix(candles)
    prev_month_lows = [f(item["low"]) for item in candles if prev_month and str(item.get("time", "")).startswith(prev_month)]
    prev_month_highs = [f(item["high"]) for item in candles if prev_month and str(item.get("time", "")).startswith(prev_month)]

    return {
        "close": last_close,
        "prev_close": prev_close,
        "day_pct": day_pct,
        "open": f(current.get("open")),
        "high": highs[-1],
        "low": lows[-1],
        "volume_m": volumes[-1] / 1_000_000,
        "sma10": mean(closes[-10:]),
        "sma20": mean(closes[-20:]),
        "sma50": mean(closes[-50:]) if len(closes) >= 50 else mean(closes),
        "atr14": atr14,
        "atr14_pct": atr14_pct,
        "rsi14": rsi,
        "prev_month_low": min(prev_month_lows) if prev_month_lows else min(lows[-20:]),
        "prev_month_high": max(prev_month_highs) if prev_month_highs else max(highs[-20:]),
    }


def summarize_intraday(points: list[dict[str, Any]]) -> dict[str, float | str]:
    if not points:
        return {}

    prices = [f(item["price"]) for item in points]
    volumes = [f(item.get("volume")) for item in points]
    last = points[-1]
    vwap = f(last.get("avg_price"))

    return {
        "first_time": str(points[0].get("time")),
        "last_time": str(last.get("time")),
        "first_price": prices[0],
        "last_price": prices[-1],
        "high": max(prices),
        "low": min(prices),
        "vwap": vwap,
        "volume_m": sum(volumes) / 1_000_000,
        "first_to_last_pct": (prices[-1] / prices[0] - 1) * 100 if prices[0] else 0,
        "vs_vwap_pct": (prices[-1] / vwap - 1) * 100 if vwap else 0,
    }


def infer_bias(daily: dict[str, float], intraday: dict[str, float | str]) -> str:
    close = float(daily.get("close", 0))
    above_vwap = float(intraday.get("vs_vwap_pct", 0)) > 0
    above_sma20 = close > float(daily.get("sma20", close))
    below_sma10 = close < float(daily.get("sma10", close))
    hot_rsi = float(daily.get("rsi14", 50)) > 75

    if above_vwap and above_sma20 and not hot_rsi:
        return "bullish"
    if above_vwap and above_sma20 and hot_rsi:
        return "bullish_breakout_only"
    if not above_vwap and below_sma10:
        return "bearish"
    return "mixed"

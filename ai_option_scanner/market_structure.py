from __future__ import annotations

from math import log, sqrt
from statistics import median
from typing import Any


TRADING_DAYS = 252


def build_volatility_context(candidates: list[Any], daily_candles: list[dict[str, Any]], news_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rv = realized_volatility_context(daily_candles)
    iv_values = [float(getattr(item, "implied_volatility", 0.0) or 0.0) for item in candidates if float(getattr(item, "implied_volatility", 0.0) or 0.0) > 0]
    current_iv = median(iv_values) if iv_values else 0.0
    rv20 = float(rv.get("rv20") or 0.0)
    iv_rv_ratio = current_iv / rv20 if rv20 > 0 and current_iv > 0 else 0.0
    return {
        "available": bool(current_iv > 0 or rv.get("available")),
        "current_iv_median": round(current_iv, 4),
        "rv20": rv20,
        "rv60": float(rv.get("rv60") or 0.0),
        "rv_rank": float(rv.get("rv_rank") or 0.0),
        "iv_rv_ratio": round(iv_rv_ratio, 3) if iv_rv_ratio else 0.0,
        "iv_rv_premium_pct": round((iv_rv_ratio - 1) * 100, 2) if iv_rv_ratio else 0.0,
        "state": _iv_rv_state(iv_rv_ratio),
        "event_risk": detect_event_risk(news_items or []),
        "source": "option_chain_iv_and_underlying_realized_volatility",
    }


def realized_volatility_context(daily_candles: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [_num(item.get("close") or item.get("last") or item.get("price")) for item in daily_candles or []]
    closes = [value for value in closes if value > 0]
    returns = [log(closes[index] / closes[index - 1]) for index in range(1, len(closes)) if closes[index - 1] > 0]
    rv20 = _annualized_vol(returns[-20:])
    rv60 = _annualized_vol(returns[-60:])
    rolling20 = [_annualized_vol(returns[index - 20 : index]) for index in range(20, len(returns) + 1)]
    rolling20 = [value for value in rolling20 if value > 0]
    rv_rank = _percentile_rank(rv20, rolling20) if rv20 > 0 and rolling20 else 0.0
    return {
        "available": bool(rv20 > 0),
        "rv20": round(rv20, 4),
        "rv60": round(rv60, 4),
        "rv_rank": round(rv_rank, 2),
        "sample_size": len(returns),
    }


def detect_event_risk(news_items: list[dict[str, Any]]) -> dict[str, Any]:
    high_keywords = ("earnings", "guidance", "fomc", "cpi", "fed", "sec", "fda", "lawsuit", "investigation", "merger", "acquisition")
    medium_keywords = ("upgrade", "downgrade", "price target", "analyst", "delivery", "recall", "strike", "tariff", "regulation")
    hits: list[dict[str, str]] = []
    score = 0
    for item in news_items[:12]:
        title = str(item.get("title") or item.get("headline") or "").strip()
        text = title.lower()
        if not text:
            continue
        high = next((keyword for keyword in high_keywords if keyword in text), "")
        medium = next((keyword for keyword in medium_keywords if keyword in text), "")
        if high:
            score = max(score, 80)
            hits.append({"keyword": high, "severity": "high", "title": title})
        elif medium:
            score = max(score, 45)
            hits.append({"keyword": medium, "severity": "medium", "title": title})
    return {
        "score": score,
        "state": "high" if score >= 70 else "medium" if score >= 40 else "low",
        "flags": hits[:5],
    }


def build_volume_profile(intraday_points: list[dict[str, Any]], daily_candles: list[dict[str, Any]], spot: float, *, bins: int = 24) -> dict[str, Any]:
    rows = _profile_rows(intraday_points)
    source = "intraday"
    if len(rows) < 8:
        rows = _daily_profile_rows(daily_candles[-60:])
        source = "daily_proxy"
    if len(rows) < 8:
        return {"available": False, "source": source, "reason": "insufficient price/volume rows"}
    prices = [row[0] for row in rows]
    low = min(prices)
    high = max(prices)
    if high <= low:
        return {"available": False, "source": source, "reason": "flat price range"}
    bin_count = max(8, min(int(bins or 24), 48))
    step = (high - low) / bin_count
    buckets = [{"low": low + step * index, "high": low + step * (index + 1), "volume": 0.0} for index in range(bin_count)]
    for price, volume in rows:
        index = min(bin_count - 1, max(0, int((price - low) / step)))
        buckets[index]["volume"] += max(volume, 0.0)
    total_volume = sum(item["volume"] for item in buckets)
    if total_volume <= 0:
        return {"available": False, "source": source, "reason": "missing usable volume"}
    poc_index = max(range(len(buckets)), key=lambda index: buckets[index]["volume"])
    poc = _bucket_mid(buckets[poc_index])
    value_area = _value_area(buckets, poc_index, total_volume * 0.70)
    vah = buckets[value_area[1]]["high"]
    val = buckets[value_area[0]]["low"]
    low_volume_nodes = _low_volume_nodes(buckets, total_volume)
    above_room = _low_volume_room_pct(spot, low_volume_nodes, direction="up")
    below_room = _low_volume_room_pct(spot, low_volume_nodes, direction="down")
    position = _profile_position(spot, poc, val, vah)
    return {
        "available": True,
        "source": source,
        "poc": round(poc, 4),
        "value_area_low": round(val, 4),
        "value_area_high": round(vah, 4),
        "position": position,
        "poc_distance_pct": round(_pct_distance(spot, poc), 2),
        "value_area_low_distance_pct": round(_pct_distance(spot, val), 2),
        "value_area_high_distance_pct": round(_pct_distance(spot, vah), 2),
        "low_volume_nodes": [{"price": round(_bucket_mid(item), 4), "volume_share_pct": round(item["volume"] / total_volume * 100, 2)} for item in low_volume_nodes[:8]],
        "low_volume_room_up_pct": round(above_room, 2),
        "low_volume_room_down_pct": round(below_room, 2),
        "bins": [{"price": round(_bucket_mid(item), 4), "volume_share_pct": round(item["volume"] / total_volume * 100, 2)} for item in buckets],
    }


def volume_profile_side_score(profile: dict[str, Any], side: str, spot: float) -> dict[str, Any]:
    if not profile.get("available") or spot <= 0:
        return {"score": 0.0, "state": "unavailable", "flags": []}
    side = str(side or "").lower()
    vah = _num(profile.get("value_area_high"))
    val = _num(profile.get("value_area_low"))
    poc = _num(profile.get("poc"))
    room_up = _num(profile.get("low_volume_room_up_pct"))
    room_down = _num(profile.get("low_volume_room_down_pct"))
    flags: list[str] = []
    score = 0.0
    if side == "call":
        if vah > spot and _pct_distance(spot, vah) <= 1.0:
            score -= 2.0
            flags.append("call_under_value_area_high_resistance")
        if room_up >= 1.2:
            score += 2.0
            flags.append("call_has_low_volume_breakout_room")
        if spot > vah:
            score += 1.2
            flags.append("call_above_value_area")
    elif side == "put":
        if val < spot and abs(_pct_distance(spot, val)) <= 1.0:
            score -= 2.0
            flags.append("put_above_value_area_low_support")
        if room_down >= 1.2:
            score += 2.0
            flags.append("put_has_low_volume_breakdown_room")
        if spot < val:
            score += 1.2
            flags.append("put_below_value_area")
    if poc > 0 and abs(_pct_distance(spot, poc)) <= 0.35:
        score -= 0.8
        flags.append("near_poc_chop_risk")
    return {"score": round(score, 2), "state": "supportive" if score > 0.5 else "resistance_risk" if score < -0.5 else "neutral", "flags": flags}


def _iv_rv_state(ratio: float) -> str:
    if ratio <= 0:
        return "unknown"
    if ratio >= 1.7:
        return "iv_expensive_vs_rv"
    if ratio >= 1.3:
        return "iv_elevated_vs_rv"
    if ratio <= 0.8:
        return "iv_cheap_vs_rv"
    return "iv_fair_vs_rv"


def _annualized_vol(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    variance = sum((item - avg) ** 2 for item in returns) / (len(returns) - 1)
    return sqrt(variance) * sqrt(TRADING_DAYS)


def _percentile_rank(value: float, sample: list[float]) -> float:
    if not sample:
        return 0.0
    below = sum(1 for item in sample if item <= value)
    return below / len(sample) * 100


def _profile_rows(points: list[dict[str, Any]]) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for item in points or []:
        price = _num(item.get("price") or item.get("close") or item.get("last"))
        volume = _num(item.get("volume") or item.get("turnover") or item.get("trade_volume"))
        if price > 0 and volume > 0:
            rows.append((price, volume))
    return rows


def _daily_profile_rows(candles: list[dict[str, Any]]) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for item in candles or []:
        high = _num(item.get("high"))
        low = _num(item.get("low"))
        close = _num(item.get("close"))
        volume = _num(item.get("volume"))
        if high > 0 and low > 0 and close > 0 and volume > 0:
            rows.extend([(low, volume * 0.25), (close, volume * 0.5), (high, volume * 0.25)])
    return rows


def _value_area(buckets: list[dict[str, float]], poc_index: int, target_volume: float) -> tuple[int, int]:
    left = right = poc_index
    total = buckets[poc_index]["volume"]
    while total < target_volume and (left > 0 or right < len(buckets) - 1):
        left_volume = buckets[left - 1]["volume"] if left > 0 else -1
        right_volume = buckets[right + 1]["volume"] if right < len(buckets) - 1 else -1
        if right_volume >= left_volume and right < len(buckets) - 1:
            right += 1
            total += buckets[right]["volume"]
        elif left > 0:
            left -= 1
            total += buckets[left]["volume"]
        else:
            break
    return left, right


def _low_volume_nodes(buckets: list[dict[str, float]], total_volume: float) -> list[dict[str, float]]:
    if total_volume <= 0:
        return []
    threshold = 100 / len(buckets) * 0.45
    nodes = [item for item in buckets if item["volume"] / total_volume * 100 <= threshold]
    return sorted(nodes, key=lambda item: item["volume"])


def _low_volume_room_pct(spot: float, nodes: list[dict[str, float]], *, direction: str) -> float:
    if spot <= 0:
        return 0.0
    prices = sorted(_bucket_mid(item) for item in nodes)
    if direction == "up":
        above = [price for price in prices if price > spot]
        return (above[0] / spot - 1) * 100 if above else 0.0
    below = [price for price in prices if price < spot]
    return (1 - below[-1] / spot) * 100 if below else 0.0


def _profile_position(spot: float, poc: float, val: float, vah: float) -> str:
    if spot <= 0:
        return "unknown"
    if spot > vah:
        return "above_value_area"
    if spot < val:
        return "below_value_area"
    if abs(_pct_distance(spot, poc)) <= 0.35:
        return "near_poc"
    return "inside_value_area"


def _bucket_mid(bucket: dict[str, float]) -> float:
    return (bucket["low"] + bucket["high"]) / 2


def _pct_distance(base: float, target: float) -> float:
    return (target / base - 1) * 100 if base > 0 and target > 0 else 0.0


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

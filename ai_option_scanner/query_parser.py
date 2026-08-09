from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .strategy_structures import normalize_strategy_modes
from .time_utils import TIMEZONE_LABEL, et_today


@dataclass(frozen=True)
class ScanIntent:
    symbol: str
    max_ask: float
    min_days: int
    max_days: int
    lottery: bool
    cheap: bool
    day_trade: bool
    preferred_side: str | None
    requested_date_et: str | None
    time_basis: str
    horizon_label: str
    strategy_modes: list[str]
    semantic_notes: list[str]


def parse_query(query: str, symbol: str | None = None) -> ScanIntent:
    normalized = query.upper()
    query_symbol = _detect_symbol(normalized, required=False)
    detected_symbol = query_symbol or symbol
    if not detected_symbol:
        raise ValueError("No ticker found. Pass --symbol, e.g. --symbol NVDA")
    requested_date = _detect_requested_date(query)
    semantic_notes: list[str] = []

    cheap = _has_any(query, normalized, ["便宜", "低价", "低成本", "不贵", "cheap", "low cost"])
    very_cheap = _has_any(query, normalized, ["很便宜", "超便宜", "极便宜", "一美元", "1美元", "$1", "penny"])
    lottery = _has_any(query, normalized, ["彩票", "大额", "大额度", "爆发", "高赔率", "暴击", "lottery", "asymmetric"])
    day_trade = _has_any(query, normalized, ["日内", "当日", "当天", "今天", "今日", "今晚", "分时", "day trade", "daytrade", "intraday", "0DTE", "0DTE"])
    zero_dte = _has_any(query, normalized, ["0DTE", "零日", "当天到期", "今日到期"])
    weeklies = _has_any(query, normalized, ["本周", "周内", "这周", "weekly", "weeklies"])
    slightly_far = _has_any(query, normalized, ["稍微远", "远一点", "稍远", "下周", "两周", "1-2周", "一两周"])
    farther = _has_any(query, normalized, ["更远", "月底", "月度", "月内", "一个月", "月线", "month", "monthly"])
    strategy_modes = _detect_strategy_modes(query, normalized)

    preferred_side = None
    if _has_any(query, normalized, ["看跌", "买put", "买 PUT", "put", "PUT", "空头", "做空", "下跌", "跌破", "回落"]):
        preferred_side = "put"
    elif _has_any(query, normalized, ["看涨", "买call", "买 CALL", "call", "CALL", "多头", "做多", "上涨", "突破", "反弹"]):
        preferred_side = "call"

    max_ask = 1.5 if cheap else 8.0
    if cheap and not very_cheap:
        max_ask = 2.5
    if very_cheap or (lottery and cheap):
        max_ask = 1.25

    if zero_dte:
        min_days, max_days = 0, 0
        horizon_label = "0DTE"
    elif farther:
        min_days, max_days = 14, 45
        horizon_label = "monthly"
    elif slightly_far:
        min_days, max_days = 7, 24
        horizon_label = "slightly_far"
    elif weeklies:
        min_days, max_days = 0, 7
        horizon_label = "weekly"
    elif day_trade:
        min_days, max_days = 0, 12
        horizon_label = "near_term_intraday_context"
    else:
        min_days, max_days = 0, 12
        horizon_label = "near_term"

    if requested_date:
        semantic_notes.append(f"User referenced {requested_date.isoformat()} in America/New_York; this is treated as analysis context, not automatically as an option expiration.")
    if query_symbol and symbol and query_symbol != symbol:
        semantic_notes.append(f"Ticker in query ({query_symbol}) overrides stale UI ticker field ({symbol}).")

    return ScanIntent(
        symbol=detected_symbol,
        max_ask=max_ask,
        min_days=min_days,
        max_days=max_days,
        lottery=lottery,
        cheap=cheap,
        day_trade=day_trade,
        preferred_side=preferred_side,
        requested_date_et=requested_date.isoformat() if requested_date else None,
        time_basis=TIMEZONE_LABEL,
        horizon_label=horizon_label,
        strategy_modes=strategy_modes,
        semantic_notes=semantic_notes,
    )


def _detect_symbol(query: str, required: bool = True) -> str | None:
    candidates = re.findall(r"[A-Z]{1,5}", query)
    ignored = {"API", "CLI", "WEB", "AI", "K", "D", "PUT", "CALL"}
    for candidate in candidates:
        if candidate not in ignored:
            return candidate
    if required:
        raise ValueError("No ticker found. Pass --symbol, e.g. --symbol NVDA")
    return None


def _has_any(query: str, normalized: str, words: list[str]) -> bool:
    return any((word in normalized if word.isascii() else word in query) for word in words)


def _detect_requested_date(query: str) -> date | None:
    today = et_today()
    if any(word in query for word in ["今天", "今日", "今晚", "当日", "当天"]):
        return today
    if any(word in query for word in ["明天", "明日", "tomorrow"]):
        return today + timedelta(days=1)
    if any(word in query for word in ["昨天", "昨日", "yesterday"]):
        return today - timedelta(days=1)

    iso_match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", query)
    if iso_match:
        return _safe_date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))

    cn_match = re.search(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})[日号]?", query)
    if cn_match:
        year = int(cn_match.group(1) or today.year)
        return _safe_date(year, int(cn_match.group(2)), int(cn_match.group(3)))

    slash_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", query)
    if slash_match:
        return _safe_date(today.year, int(slash_match.group(1)), int(slash_match.group(2)))

    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _detect_strategy_modes(query: str, normalized: str) -> list[str]:
    modes: list[str] = []
    if _has_any(query, normalized, ["组合", "结构", "策略组合", "多腿", "defined risk", "multi-leg", "multileg"]):
        modes.extend(["single_leg", "spread", "credit_spread", "straddle", "strangle", "collar", "covered_call", "cash_secured_put", "calendar", "diagonal", "poor_mans_covered_call", "iron_condor", "butterfly"])
    if _has_any(query, normalized, ["单腿", "single_leg", "single leg", "one leg", "one-leg"]):
        modes.append("single_leg")
    if _has_any(query, normalized, ["价差", "spread", "垂直价差", "bull call", "bear put", "vertical"]):
        modes.append("spread")
    if _has_any(query, normalized, ["信用价差", "credit spread", "bull put", "bear call", "收租价差"]):
        modes.append("credit_spread")
    if _has_any(query, normalized, ["跨式", "straddle"]):
        modes.append("straddle")
    if _has_any(query, normalized, ["宽跨", "strangle"]):
        modes.append("strangle")
    if _has_any(query, normalized, ["领式", "collar"]):
        modes.append("collar")
    if _has_any(query, normalized, ["备兑", "covered call"]):
        modes.append("covered_call")
    if _has_any(query, normalized, ["现金担保", "现金担保put", "cash secured put", "secured put", "卖put接股"]):
        modes.append("cash_secured_put")
    if _has_any(query, normalized, ["日历价差", "calendar", "calendar spread"]):
        modes.append("calendar")
    if _has_any(query, normalized, ["对角价差", "diagonal", "diagonal spread"]):
        modes.append("diagonal")
    if _has_any(query, normalized, ["穷人备兑", "poor man's covered call", "poor mans covered call", "pmcc"]):
        modes.append("poor_mans_covered_call")
    if _has_any(query, normalized, ["铁鹰", "iron condor"]):
        modes.append("iron_condor")
    if _has_any(query, normalized, ["蝶式", "butterfly"]):
        modes.append("butterfly")
    if any(mode in modes for mode in ["credit_spread", "calendar", "diagonal"]) and not _has_any(query, normalized, ["垂直价差", "vertical", "bull call", "bear put"]):
        modes = [mode for mode in modes if mode != "spread"]
    if "poor_mans_covered_call" in modes and not _has_any(query, normalized, ["普通备兑", "传统备兑"]):
        modes = [mode for mode in modes if mode != "covered_call"]
    if not modes:
        modes.append("single_leg")
    return normalize_strategy_modes(modes)

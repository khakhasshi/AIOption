from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import log1p
from typing import Any, Literal

import pandas as pd
import yfinance as yf

from .concurrency import yfinance_limiter
from .time_utils import et_today
from .ttl_cache import TTLCache


OptionSide = Literal["call", "put"]
MARKET_DATA_TTL_SECONDS = float(os.getenv("AI_OPTION_YF_MARKET_DATA_TTL_SECONDS") or 15)
OPTION_CHAIN_TTL_SECONDS = float(os.getenv("AI_OPTION_YF_OPTION_CHAIN_TTL_SECONDS") or 60)
OPTION_EXPIRATIONS_TTL_SECONDS = float(os.getenv("AI_OPTION_YF_EXPIRATIONS_TTL_SECONDS") or 300)
_market_data_cache: TTLCache[dict[str, Any]] = TTLCache(MARKET_DATA_TTL_SECONDS, maxsize=128, namespace="yf_market")
_option_chain_cache: TTLCache[Any] = TTLCache(OPTION_CHAIN_TTL_SECONDS, maxsize=512, namespace="yf_chain")
_option_expirations_cache: TTLCache[list[str]] = TTLCache(OPTION_EXPIRATIONS_TTL_SECONDS, maxsize=256, namespace="yf_expirations")


@dataclass(frozen=True)
class OptionCandidate:
    contract_symbol: str
    expiration: str
    side: OptionSide
    strike: float
    last_price: float
    bid: float
    ask: float
    volume: int
    open_interest: int
    implied_volatility: float
    in_the_money: bool
    moneyness_pct: float
    spread_pct: float
    score: float
    pricing_source: str = "bid_ask"
    quote_warning: str = ""
    delta: float = 0.0
    gamma: float = 0.0
    theta_per_day: float = 0.0
    breakeven: float = 0.0
    move_to_strike_pct: float = 0.0
    move_to_breakeven_pct: float = 0.0
    days_to_expiration: float = 0.0
    mid_price: float = 0.0
    ask_to_mid_pct: float = 0.0
    execution_quality_score: float = 0.0
    execution_quality_state: str = "unknown"
    alpha_score: float = 0.0
    execution_score: float = 0.0
    decision_score: float = 0.0
    decision_bucket: str = "unknown"
    trigger_score: float = 0.0
    trigger_state: str = "unknown"
    trigger_reasons: list[str] = field(default_factory=list)
    execution_hard_flags: list[str] = field(default_factory=list)
    time_value_risk_penalty: float = 0.0
    theta_to_ask_pct: float = 0.0
    theta_pressure_state: str = "unknown"
    expected_move: float = 0.0
    expected_move_pct: float = 0.0
    breakeven_within_expected_move: bool = False
    iv_rank: float = 0.0
    iv_percentile: float = 0.0
    delta_style: str = "unknown"
    delta_style_min: float = 0.0
    delta_style_max: float = 0.0
    delta_style_match: bool = False
    expiration_iv_mean: float = 0.0
    expiration_iv_premium_pct: float = 0.0
    term_structure_slope_pct: float = 0.0
    term_structure_state: str = "unknown"
    rv20: float = 0.0
    rv60: float = 0.0
    rv_rank: float = 0.0
    iv_rv_ratio: float = 0.0
    iv_rv_premium_pct: float = 0.0
    iv_edge_state: str = "unknown"
    iv_rank_source: str = "chain_cross_section"
    event_risk_score: float = 0.0
    event_risk_state: str = "low"
    event_risk_flags: list[dict[str, str]] = field(default_factory=list)
    volatility_score: float = 0.0
    volume_profile_score: float = 0.0
    volume_profile_state: str = "unknown"
    volume_profile_poc: float = 0.0
    volume_profile_value_area_low: float = 0.0
    volume_profile_value_area_high: float = 0.0
    volume_profile_position: str = "unknown"
    volume_profile_low_volume_room_pct: float = 0.0
    market_structure_score: float = 0.0
    market_structure_flags: list[str] = field(default_factory=list)
    strategy_tag: str = "unknown"
    probability_itm: float = 0.0
    probability_breakeven: float = 0.0
    probability_touch: float = 0.0
    reward_risk_score: float = 0.0
    gex_value: float = 0.0
    gex_per_1pct: float = 0.0
    gex_share_pct: float = 0.0
    gex_strike_rank: int = 0
    gex_regime: str = "unknown"
    gex_alignment: str = "neutral"
    gex_nearest_wall: str = ""
    gex_nearest_wall_distance_pct: float = 0.0
    gex_call_wall: float = 0.0
    gex_put_wall: float = 0.0
    gex_gamma_flip: float = 0.0
    scenario_prices: dict[str, float] = field(default_factory=dict)
    risk_plan: dict[str, str | float] = field(default_factory=dict)
    analysis_score: float = 0.0

    def with_greeks(
        self,
        delta: float,
        gamma: float,
        theta_per_day: float,
        breakeven: float,
        move_to_strike_pct: float,
        move_to_breakeven_pct: float,
        days_to_expiration: float,
    ) -> "OptionCandidate":
        return replace(
            self,
            delta=delta,
            gamma=gamma,
            theta_per_day=theta_per_day,
            breakeven=breakeven,
            move_to_strike_pct=move_to_strike_pct,
            move_to_breakeven_pct=move_to_breakeven_pct,
            days_to_expiration=days_to_expiration,
        )

    def with_option_analysis(
        self,
        mid_price: float,
        ask_to_mid_pct: float,
        execution_quality_score: float,
        execution_quality_state: str,
        alpha_score: float,
        execution_score: float,
        decision_score: float,
        decision_bucket: str,
        theta_to_ask_pct: float,
        theta_pressure_state: str,
        expected_move: float,
        expected_move_pct: float,
        breakeven_within_expected_move: bool,
        iv_rank: float,
        iv_percentile: float,
        delta_style: str,
        delta_style_min: float,
        delta_style_max: float,
        delta_style_match: bool,
        expiration_iv_mean: float,
        expiration_iv_premium_pct: float,
        term_structure_slope_pct: float,
        term_structure_state: str,
        rv20: float = 0.0,
        rv60: float = 0.0,
        rv_rank: float = 0.0,
        iv_rv_ratio: float = 0.0,
        iv_rv_premium_pct: float = 0.0,
        iv_edge_state: str = "unknown",
        iv_rank_source: str = "chain_cross_section",
        event_risk_score: float = 0.0,
        event_risk_state: str = "low",
        event_risk_flags: list[dict[str, str]] | None = None,
        volatility_score: float = 0.0,
        volume_profile_score: float = 0.0,
        volume_profile_state: str = "unknown",
        volume_profile_poc: float = 0.0,
        volume_profile_value_area_low: float = 0.0,
        volume_profile_value_area_high: float = 0.0,
        volume_profile_position: str = "unknown",
        volume_profile_low_volume_room_pct: float = 0.0,
        market_structure_score: float = 0.0,
        market_structure_flags: list[str] | None = None,
        strategy_tag: str = "unknown",
        probability_itm: float = 0.0,
        probability_breakeven: float = 0.0,
        probability_touch: float = 0.0,
        reward_risk_score: float = 0.0,
        gex_value: float = 0.0,
        gex_per_1pct: float = 0.0,
        gex_share_pct: float = 0.0,
        gex_strike_rank: int = 0,
        gex_regime: str = "unknown",
        gex_alignment: str = "neutral",
        gex_nearest_wall: str = "",
        gex_nearest_wall_distance_pct: float = 0.0,
        gex_call_wall: float = 0.0,
        gex_put_wall: float = 0.0,
        gex_gamma_flip: float = 0.0,
        scenario_prices: dict[str, float] | None = None,
        risk_plan: dict[str, str | float] | None = None,
        analysis_score: float = 0.0,
        trigger_score: float = 0.0,
        trigger_state: str = "unknown",
        trigger_reasons: list[str] | None = None,
        execution_hard_flags: list[str] | None = None,
        time_value_risk_penalty: float = 0.0,
    ) -> "OptionCandidate":
        return replace(
            self,
            mid_price=mid_price,
            ask_to_mid_pct=ask_to_mid_pct,
            execution_quality_score=execution_quality_score,
            execution_quality_state=execution_quality_state,
            alpha_score=alpha_score,
            execution_score=execution_score,
            decision_score=decision_score,
            decision_bucket=decision_bucket,
            trigger_score=trigger_score,
            trigger_state=trigger_state,
            trigger_reasons=list(trigger_reasons or []),
            execution_hard_flags=list(execution_hard_flags or []),
            time_value_risk_penalty=time_value_risk_penalty,
            theta_to_ask_pct=theta_to_ask_pct,
            theta_pressure_state=theta_pressure_state,
            expected_move=expected_move,
            expected_move_pct=expected_move_pct,
            breakeven_within_expected_move=breakeven_within_expected_move,
            iv_rank=iv_rank,
            iv_percentile=iv_percentile,
            delta_style=delta_style,
            delta_style_min=delta_style_min,
            delta_style_max=delta_style_max,
            delta_style_match=delta_style_match,
            expiration_iv_mean=expiration_iv_mean,
            expiration_iv_premium_pct=expiration_iv_premium_pct,
            term_structure_slope_pct=term_structure_slope_pct,
            term_structure_state=term_structure_state,
            rv20=rv20,
            rv60=rv60,
            rv_rank=rv_rank,
            iv_rv_ratio=iv_rv_ratio,
            iv_rv_premium_pct=iv_rv_premium_pct,
            iv_edge_state=iv_edge_state,
            iv_rank_source=iv_rank_source,
            event_risk_score=event_risk_score,
            event_risk_state=event_risk_state,
            event_risk_flags=list(event_risk_flags or []),
            volatility_score=volatility_score,
            volume_profile_score=volume_profile_score,
            volume_profile_state=volume_profile_state,
            volume_profile_poc=volume_profile_poc,
            volume_profile_value_area_low=volume_profile_value_area_low,
            volume_profile_value_area_high=volume_profile_value_area_high,
            volume_profile_position=volume_profile_position,
            volume_profile_low_volume_room_pct=volume_profile_low_volume_room_pct,
            market_structure_score=market_structure_score,
            market_structure_flags=list(market_structure_flags or []),
            strategy_tag=strategy_tag,
            probability_itm=probability_itm,
            probability_breakeven=probability_breakeven,
            probability_touch=probability_touch,
            reward_risk_score=reward_risk_score,
            gex_value=gex_value,
            gex_per_1pct=gex_per_1pct,
            gex_share_pct=gex_share_pct,
            gex_strike_rank=gex_strike_rank,
            gex_regime=gex_regime,
            gex_alignment=gex_alignment,
            gex_nearest_wall=gex_nearest_wall,
            gex_nearest_wall_distance_pct=gex_nearest_wall_distance_pct,
            gex_call_wall=gex_call_wall,
            gex_put_wall=gex_put_wall,
            gex_gamma_flip=gex_gamma_flip,
            scenario_prices=dict(scenario_prices or {}),
            risk_plan=dict(risk_plan or {}),
            analysis_score=analysis_score,
        )


def collect_candidates(
    symbol: str,
    spot: float,
    min_days: int,
    max_days: int,
    max_ask: float,
    lottery: bool,
    preferred_side: str | None,
    min_ask: float = 0.05,
    gex_mode: bool = False,
) -> list[OptionCandidate]:
    today = et_today()
    candidates: list[OptionCandidate] = []

    for expiration in _ticker_options(symbol):
        expiry_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        days = (expiry_date - today).days
        if days < min_days or days > max_days:
            continue

        chain = _option_chain(symbol, expiration)
        sides: list[tuple[OptionSide, pd.DataFrame]] = [("call", chain.calls), ("put", chain.puts)]
        for side, frame in sides:
            if preferred_side and side != preferred_side:
                continue
            candidates.extend(_frame_to_candidates(frame, expiration, side, spot, min_ask, max_ask, lottery, gex_mode=gex_mode))

    return sorted(candidates, key=lambda item: item.score, reverse=True)


def market_data(symbol: str, daily_count: int = 80) -> dict[str, Any]:
    key = (symbol.upper(), daily_count)
    try:
        data = _market_data_cache.get_or_set_stale_while_revalidate(key, lambda: _market_data_uncached(symbol, daily_count))
    except Exception:
        stale = _market_data_cache.get_stale(key)
        if stale is None:
            raise
        data = stale
        data.setdefault("cache", {})["stale"] = True
    data.setdefault("cache", {})["ttl_seconds"] = MARKET_DATA_TTL_SECONDS
    return data


def _market_data_uncached(symbol: str, daily_count: int = 80) -> dict[str, Any]:
    with yfinance_limiter.acquire():
        ticker = yf.Ticker(symbol)
        daily_frame = ticker.history(period="6mo", interval="1d", auto_adjust=False)
        intraday_frame = ticker.history(period="1d", interval="5m", auto_adjust=False)
        news_items = _ticker_news(ticker)
    daily = _history_to_daily(daily_frame)[-daily_count:]
    intraday = _history_to_intraday(intraday_frame)
    last_price = _last_price(daily, intraday)
    return {
        "quote": {
            "symbol": symbol,
            "last": last_price,
            "price": last_price,
            "time": intraday[-1]["time"] if intraday else daily[-1]["time"] if daily else "",
            "source": "yfinance",
        },
        "daily": daily,
        "intraday": intraday,
        "news": news_items,
    }


def _ticker_news(ticker: Any) -> list[dict[str, Any]]:
    try:
        rows = ticker.news or []
    except Exception:
        return []
    output: list[dict[str, Any]] = []
    for item in rows[:12]:
        content = item.get("content") if isinstance(item, dict) else None
        source = content if isinstance(content, dict) else item if isinstance(item, dict) else {}
        title = source.get("title") or source.get("headline")
        if not title:
            continue
        published_at = source.get("pubDate") or source.get("displayTime") or source.get("providerPublishTime")
        url = source.get("canonicalUrl") or source.get("clickThroughUrl") or source.get("link")
        if isinstance(url, dict):
            url = url.get("url")
        output.append(
            {
                "title": str(title),
                "published_at": str(published_at or ""),
                "url": str(url or ""),
                "source": "yfinance",
            }
        )
    return output


def quote_option_contract(contract_symbol: str) -> dict[str, Any]:
    parsed = _parse_contract_symbol(contract_symbol)
    if not parsed:
        return {
            "contract_symbol": contract_symbol,
            "available": False,
            "error": "Unsupported option contract symbol format.",
        }
    chain = _option_chain(parsed["root"], parsed["expiration"])
    frame = chain.calls if parsed["side"] == "call" else chain.puts
    if frame.empty:
        return {
            "contract_symbol": contract_symbol,
            "available": False,
            "error": "Option chain is empty.",
            **parsed,
        }
    matched = frame[frame["contractSymbol"] == contract_symbol]
    if matched.empty:
        matched = frame[frame["strike"].astype(float) == float(parsed["strike"])]
    if matched.empty:
        return {
            "contract_symbol": contract_symbol,
            "available": False,
            "error": "Contract not found in refreshed option chain.",
            **parsed,
        }
    row = matched.iloc[0].to_dict()
    bid = _num(row.get("bid"))
    ask = _num(row.get("ask"))
    last_price = _num(row.get("lastPrice"))
    bid, ask, pricing_source, quote_warning = _normalize_option_quote(bid, ask, last_price)
    mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else 0.0
    limit_price = ask if ask > 0 else last_price
    return {
        "contract_symbol": contract_symbol,
        "available": limit_price > 0,
        "root": parsed["root"],
        "expiration": parsed["expiration"],
        "side": parsed["side"],
        "strike": parsed["strike"],
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last_price": last_price,
        "limit_price": round(limit_price, 2),
        "volume": int(_num(row.get("volume"))),
        "open_interest": int(_num(row.get("openInterest"))),
        "implied_volatility": _num(row.get("impliedVolatility")),
        "pricing_source": pricing_source,
        "quote_warning": quote_warning,
    }


def _ticker_options(symbol: str) -> list[str]:
    key = symbol.upper()
    try:
        return _option_expirations_cache.get_or_set_stale_while_revalidate(key, lambda: _fetch_ticker_options(key))
    except Exception:
        stale = _option_expirations_cache.get_stale(key)
        if stale is not None:
            return stale
        raise


def _option_chain(symbol: str, expiration: str) -> Any:
    key = (symbol.upper(), expiration)
    try:
        return _option_chain_cache.get_or_set_stale_while_revalidate(key, lambda: _fetch_option_chain(symbol, expiration))
    except Exception:
        stale = _option_chain_cache.get_stale(key)
        if stale is not None:
            return stale
        raise


def _fetch_ticker_options(symbol: str) -> list[str]:
    with yfinance_limiter.acquire():
        return list(yf.Ticker(symbol).options or [])


def _fetch_option_chain(symbol: str, expiration: str) -> Any:
    with yfinance_limiter.acquire():
        return yf.Ticker(symbol).option_chain(expiration)


def _history_to_daily(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    output: list[dict[str, Any]] = []
    for index, row in frame.reset_index().iterrows():
        time_value = row.get("Date") or row.get("Datetime") or index
        output.append(
            {
                "time": _time_text(time_value),
                "open": _num(row.get("Open")),
                "high": _num(row.get("High")),
                "low": _num(row.get("Low")),
                "close": _num(row.get("Close")),
                "volume": int(_num(row.get("Volume"))),
            }
        )
    return output


def _history_to_intraday(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    output: list[dict[str, Any]] = []
    cumulative_volume = 0.0
    cumulative_notional = 0.0
    for index, row in frame.reset_index().iterrows():
        time_value = row.get("Datetime") or row.get("Date") or index
        close = _num(row.get("Close"))
        high = _num(row.get("High"))
        low = _num(row.get("Low"))
        volume = _num(row.get("Volume"))
        typical_price = (high + low + close) / 3 if high > 0 and low > 0 and close > 0 else close
        cumulative_volume += volume
        cumulative_notional += typical_price * volume
        avg_price = cumulative_notional / cumulative_volume if cumulative_volume > 0 else close
        output.append(
            {
                "time": _time_text(time_value),
                "price": close,
                "avg_price": avg_price,
                "volume": int(volume),
            }
        )
    return output


def _last_price(daily: list[dict[str, Any]], intraday: list[dict[str, Any]]) -> float:
    if intraday and _num(intraday[-1].get("price")) > 0:
        return _num(intraday[-1].get("price"))
    if daily and _num(daily[-1].get("close")) > 0:
        return _num(daily[-1].get("close"))
    return 0.0


def _time_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _parse_contract_symbol(contract_symbol: str) -> dict[str, Any] | None:
    marker_index = -1
    for index in range(len(contract_symbol) - 9):
        if contract_symbol[index : index + 6].isdigit() and contract_symbol[index + 6] in {"C", "P"}:
            marker_index = index
            break
    if marker_index < 1:
        return None
    root = contract_symbol[:marker_index]
    expiry = contract_symbol[marker_index : marker_index + 6]
    side = "call" if contract_symbol[marker_index + 6] == "C" else "put"
    strike_code = contract_symbol[marker_index + 7 :]
    if not strike_code.isdigit():
        return None
    return {
        "root": root,
        "expiration": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:6]}",
        "side": side,
        "strike": int(strike_code) / 1000,
    }


def _frame_to_candidates(
    frame: pd.DataFrame,
    expiration: str,
    side: OptionSide,
    spot: float,
    min_ask: float,
    max_ask: float,
    lottery: bool,
    gex_mode: bool = False,
) -> list[OptionCandidate]:
    output: list[OptionCandidate] = []
    if frame.empty:
        return output

    for row in frame.to_dict("records"):
        bid = _num(row.get("bid"))
        ask = _num(row.get("ask"))
        last_price = _num(row.get("lastPrice"))
        bid, ask, pricing_source, quote_warning = _normalize_option_quote(bid, ask, last_price)
        strike = _num(row.get("strike"))
        volume = int(_num(row.get("volume")))
        open_interest = int(_num(row.get("openInterest")))
        implied_volatility = _num(row.get("impliedVolatility"))
        if gex_mode and (strike <= 0 or open_interest <= 0 or implied_volatility <= 0):
            continue
        if not gex_mode and (ask < min_ask or ask > max_ask):
            continue
        if not gex_mode:
            if volume < 100 and open_interest < 1000:
                continue

        moneyness_pct = (strike / spot - 1) * 100 if side == "call" else (1 - strike / spot) * 100
        if not gex_mode:
            if lottery and moneyness_pct < 1.0:
                continue
            if not lottery and moneyness_pct < -2.0:
                continue

        spread_pct = (ask - bid) / ask * 100 if ask else 100
        if not gex_mode and spread_pct > 35:
            continue

        liquidity_score = log1p(volume) + 0.55 * log1p(open_interest)
        price_score = max(0.1, (max_ask - ask) / max_ask)
        lottery_score = min(max(moneyness_pct, 0), 8) / 8
        spread_score = max(0.0, 1 - spread_pct / 35)
        score = liquidity_score * 1.2 + price_score * 2.0 + spread_score * 2.0 + lottery_score * (3.0 if lottery else 1.0)

        output.append(
            OptionCandidate(
                contract_symbol=str(row.get("contractSymbol")),
                expiration=expiration,
                side=side,
                strike=strike,
                last_price=last_price,
                bid=bid,
                ask=ask,
                volume=volume,
                open_interest=open_interest,
                implied_volatility=implied_volatility,
                in_the_money=bool(row.get("inTheMoney")),
                moneyness_pct=moneyness_pct,
                spread_pct=spread_pct,
                score=score,
                pricing_source=pricing_source,
                quote_warning=quote_warning,
            )
        )
    return output


def _normalize_option_quote(bid: float, ask: float, last_price: float) -> tuple[float, float, str, str]:
    if ask > 0 and bid > 0:
        return bid, ask, "bid_ask", ""
    if ask > 0:
        synthetic_bid = bid if bid > 0 else max(0.01, min(ask * 0.9, last_price if last_price > 0 else ask * 0.9))
        return synthetic_bid, ask, "ask_only", "yfinance bid is unavailable; using ask with an indicative bid."
    if last_price > 0:
        synthetic_bid = max(0.01, last_price * 0.9)
        return synthetic_bid, last_price, "last_price_fallback", "yfinance bid/ask are unavailable; using lastPrice as an indicative option price."
    return bid, ask, "unavailable", "yfinance did not return a usable bid/ask or lastPrice."


def _num(value: object) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0

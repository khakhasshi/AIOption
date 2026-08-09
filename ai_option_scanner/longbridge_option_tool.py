from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import log1p
from typing import Any, Literal

from .longbridge_sdk_client import depth as sdk_depth
from .longbridge_sdk_client import option_chain_info, option_expirations, option_quotes
from .time_utils import et_today


OptionSide = Literal["call", "put"]
DEPTH_REFRESH_LIMIT = max(int(os.getenv("AI_OPTION_LONGBRIDGE_OPTION_DEPTH_REFRESH_LIMIT") or 16), 0)
MAX_QUOTES_PER_EXPIRATION = max(int(os.getenv("AI_OPTION_LONGBRIDGE_OPTION_QUOTES_PER_EXPIRATION") or 64), 8)
# Inter-call throttle (seconds) between sdk_depth() invocations during a single
# shortlist refresh. Longbridge OpenAPI rate-limits depth requests; without this
# burst of 16 calls can trip 429s. Default 50ms keeps us under most quotas.
DEPTH_REFRESH_INTERVAL_MS = max(int(os.getenv("AI_OPTION_LONGBRIDGE_DEPTH_INTERVAL_MS") or 50), 0)


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
    pricing_source: str = "longbridge_option_quote"
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

    def with_greeks(self, delta: float, gamma: float, theta_per_day: float, breakeven: float, move_to_strike_pct: float, move_to_breakeven_pct: float, days_to_expiration: float) -> "OptionCandidate":
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
    account_name: str | None = None,
    gex_mode: bool = False,
) -> list[OptionCandidate]:
    today = et_today()
    candidates: list[OptionCandidate] = []
    symbol = symbol.upper().strip()
    if not symbol.endswith(".US"):
        symbol = f"{symbol}.US"
    expirations = option_expirations(symbol, account_name)
    for expiration in expirations:
        expiry_date = datetime.strptime(expiration, "%Y-%m-%d").date()
        days = (expiry_date - today).days
        if days < min_days or days > max_days:
            continue
        chain = option_chain_info(symbol, expiration, account_name)
        rows: list[dict[str, Any]] = []
        for item in chain:
            for side, symbol_key in (("call", "call_symbol"), ("put", "put_symbol")):
                if preferred_side and side != preferred_side:
                    continue
                contract_symbol = str(item.get(symbol_key) or "").strip()
                if not contract_symbol:
                    continue
                strike = _num(item.get("strike"))
                if strike <= 0:
                    continue
                moneyness_pct = (strike / spot - 1) * 100 if side == "call" else (1 - strike / spot) * 100
                if not gex_mode:
                    if lottery and moneyness_pct < 1.0:
                        continue
                    if not lottery and moneyness_pct < -2.0:
                        continue
                rows.append(
                    {
                        "contract_symbol": contract_symbol,
                        "expiration": expiration,
                        "side": side,
                        "strike": strike,
                        "moneyness_pct": moneyness_pct,
                    }
                )
        if not rows:
            continue
        rows = rows if gex_mode else _trim_rows_for_quote(rows, spot, lottery)
        if not rows:
            continue
        quotes = _quote_map([row["contract_symbol"] for row in rows], account_name)
        for row in rows:
            quote = quotes.get(row["contract_symbol"], {})
            bid, ask, pricing_source, quote_warning = _normalize_option_quote(
                _num(quote.get("bid")),
                _num(quote.get("ask")),
                _num(quote.get("last_price") or quote.get("last_done") or quote.get("last")),
            )
            if quote.get("best_bid") is not None and bid <= 0:
                bid = _num(quote.get("best_bid"))
            if quote.get("best_ask") is not None and ask <= 0:
                ask = _num(quote.get("best_ask"))
            if ask < min_ask or ask > max_ask:
                if not gex_mode:
                    continue
            volume = int(_num(quote.get("volume")))
            open_interest = int(_num(quote.get("open_interest")))
            implied_volatility = _num(quote.get("implied_volatility"))
            if gex_mode and (open_interest <= 0 or implied_volatility <= 0):
                continue
            if not gex_mode:
                if volume < 20 and open_interest < 100:
                    continue
            spread_pct = (ask - bid) / ask * 100 if ask else 100
            if not gex_mode and spread_pct > 45:
                continue
            liquidity_score = log1p(max(volume, 0)) + 0.55 * log1p(max(open_interest, 0))
            price_score = max(0.1, (max_ask - ask) / max_ask)
            lottery_score = min(max(row["moneyness_pct"], 0), 8) / 8
            spread_score = max(0.0, 1 - spread_pct / 45)
            score = liquidity_score * 1.2 + price_score * 2.0 + spread_score * 2.0 + lottery_score * (3.0 if lottery else 1.0)
            candidates.append(
                OptionCandidate(
                    contract_symbol=row["contract_symbol"],
                    expiration=row["expiration"],
                    side=row["side"],
                    strike=row["strike"],
                    last_price=_num(quote.get("last_price") or quote.get("last_done") or quote.get("last")),
                    bid=bid,
                    ask=ask,
                    volume=volume,
                    open_interest=open_interest,
                    implied_volatility=implied_volatility,
                    in_the_money=(row["strike"] < spot if row["side"] == "call" else row["strike"] > spot),
                    moneyness_pct=row["moneyness_pct"],
                    spread_pct=spread_pct,
                    score=score,
                    pricing_source=pricing_source,
                    quote_warning=quote_warning,
                )
            )
    return _refresh_depth_for_shortlist(sorted(candidates, key=lambda item: item.score, reverse=True), account_name)


def market_data(symbol: str, daily_count: int = 80, account_name: str | None = None) -> dict[str, Any]:
    from .longbridge_client import kline as lb_kline, intraday as lb_intraday, news as lb_news, quote as lb_quote

    symbol = symbol.upper().strip()
    if not symbol.endswith(".US"):
        symbol = f"{symbol}.US"
    quote_data = lb_quote(symbol, account_name)
    daily = lb_kline(symbol, daily_count, account_name)
    intraday = lb_intraday(symbol, account_name)
    news_items = lb_news(symbol, account_name)
    last_price = _last_price(daily, intraday, quote_data)
    return {
        "quote": {
            "symbol": symbol,
            "last": last_price,
            "price": last_price,
            "time": intraday[-1]["time"] if intraday else daily[-1]["time"] if daily else "",
            "source": "longbridge",
            "raw": quote_data,
        },
        "daily": daily,
        "intraday": intraday,
        "news": news_items,
    }


def quote_option_contract(contract_symbol: str, account_name: str | None = None) -> dict[str, Any]:
    parsed = _parse_contract_symbol(contract_symbol)
    if not parsed:
        return {
            "contract_symbol": contract_symbol,
            "available": False,
            "error": "Unsupported option contract symbol format.",
        }
    symbol = parsed["symbol"]
    quote_rows = _quote_map([symbol], account_name)
    quote = quote_rows.get(symbol, {})
    depth = sdk_depth(symbol, account_name)
    bid = _num(quote.get("bid"))
    ask = _num(quote.get("ask"))
    used_depth = False
    if bid <= 0:
        bid = _num(depth.get("best_bid"))
        used_depth = bid > 0
    if ask <= 0:
        ask = _num(depth.get("best_ask"))
        used_depth = used_depth or ask > 0
    last_price = _num(quote.get("last_price") or quote.get("last_done") or quote.get("last"))
    mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else 0.0
    limit_price = ask if ask > 0 else last_price or _num(quote.get("last")) or 0.0
    available = limit_price > 0 or bid > 0 or ask > 0
    return {
        "contract_symbol": contract_symbol,
        "available": available,
        "root": parsed["root"],
        "expiration": parsed["expiration"],
        "side": parsed["side"],
        "strike": parsed["strike"],
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last_price": last_price,
        "limit_price": round(limit_price, 2) if limit_price > 0 else 0,
        "volume": int(_num(quote.get("volume"))),
        "open_interest": int(_num(quote.get("open_interest"))),
        "implied_volatility": _num(quote.get("implied_volatility")),
        "pricing_source": "longbridge_depth" if used_depth else "longbridge_option_quote",
        "quote_warning": "",
        "raw_quote": quote,
        "raw_depth": depth,
    }


def _quote_map(symbols: list[str], account_name: str | None = None) -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    batch_size = 200
    for offset in range(0, len(symbols), batch_size):
        rows.extend(option_quotes(symbols[offset : offset + batch_size], account_name))
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or row.get("contract_symbol") or row.get("security") or "").strip()
        if symbol:
            output[symbol] = row
    return output


def _refresh_depth_for_shortlist(candidates: list[OptionCandidate], account_name: str | None = None) -> list[OptionCandidate]:
    if DEPTH_REFRESH_LIMIT <= 0:
        return candidates
    interval = DEPTH_REFRESH_INTERVAL_MS / 1000.0
    refreshed: list[OptionCandidate] = []
    refresh_count = 0
    for index, candidate in enumerate(candidates):
        if index >= DEPTH_REFRESH_LIMIT:
            refreshed.append(candidate)
            continue
        if refresh_count > 0 and interval > 0:
            time.sleep(interval)
        refresh_count += 1
        try:
            depth = sdk_depth(candidate.contract_symbol, account_name)
        except Exception:
            refreshed.append(candidate)
            continue
        bid = _num(depth.get("best_bid")) or candidate.bid
        ask = _num(depth.get("best_ask")) or candidate.ask
        if bid <= 0 and ask <= 0:
            refreshed.append(candidate)
            continue
        if bid <= 0:
            bid = max(0.01, min(ask * 0.9, candidate.last_price if candidate.last_price > 0 else ask * 0.9))
        if ask <= 0:
            ask = candidate.last_price if candidate.last_price > 0 else candidate.ask
        spread_pct = (ask - bid) / ask * 100 if ask > 0 else candidate.spread_pct
        spread_score = max(0.0, 1 - spread_pct / 45)
        score = candidate.score + spread_score * 0.75
        refreshed.append(
            replace(
                candidate,
                bid=bid,
                ask=ask,
                spread_pct=spread_pct,
                score=score,
                pricing_source="longbridge_depth",
                quote_warning="" if depth.get("best_bid") and depth.get("best_ask") else candidate.quote_warning,
            )
        )
    return sorted(refreshed, key=lambda item: item.score, reverse=True)


def _trim_rows_for_quote(rows: list[dict[str, Any]], spot: float, lottery: bool) -> list[dict[str, Any]]:
    if len(rows) <= MAX_QUOTES_PER_EXPIRATION:
        return rows
    target_moneyness = 4.0 if lottery else 1.5

    def sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
        moneyness = abs(_num(item.get("moneyness_pct")) - target_moneyness)
        strike_bias = abs(_num(item.get("strike")) - spot) / max(spot, 1.0)
        return (moneyness, strike_bias, str(item.get("contract_symbol") or ""))

    ordered = sorted(rows, key=sort_key)
    selected: list[dict[str, Any]] = []
    side_count = len({str(row.get("side") or "") for row in rows})
    side_limit = MAX_QUOTES_PER_EXPIRATION if side_count <= 1 else max(4, MAX_QUOTES_PER_EXPIRATION // 2)
    seen_sides = {"call": 0, "put": 0}
    for row in ordered:
        side = str(row.get("side") or "")
        if side in seen_sides and seen_sides[side] >= side_limit:
            continue
        selected.append(row)
        if side in seen_sides:
            seen_sides[side] += 1
        if len(selected) >= MAX_QUOTES_PER_EXPIRATION:
            break
    return selected


def _parse_contract_symbol(contract_symbol: str) -> dict[str, Any] | None:
    text = str(contract_symbol or "").strip()
    if not text:
        return None
    if text.endswith(".US"):
        text = text[:-3]
    marker_index = -1
    for index in range(len(text) - 9):
        if text[index : index + 6].isdigit() and text[index + 6] in {"C", "P"}:
            marker_index = index
            break
    if marker_index < 1:
        return None
    root = text[:marker_index]
    expiry = text[marker_index : marker_index + 6]
    side = "call" if text[marker_index + 6] == "C" else "put"
    strike_code = text[marker_index + 7 :]
    if not strike_code.isdigit():
        return None
    return {
        "root": root,
        "expiration": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:6]}",
        "side": side,
        "strike": int(strike_code) / 1000,
        "symbol": f"{root}{expiry}{text[marker_index + 6]}{strike_code}.US",
    }


def _normalize_option_quote(bid: float, ask: float, last_price: float) -> tuple[float, float, str, str]:
    if ask > 0 and bid > 0:
        return bid, ask, "bid_ask", ""
    if ask > 0:
        synthetic_bid = bid if bid > 0 else max(0.01, min(ask * 0.9, last_price if last_price > 0 else ask * 0.9))
        return synthetic_bid, ask, "ask_only", "Longbridge bid is unavailable; using ask with an indicative bid."
    if last_price > 0:
        synthetic_bid = max(0.01, last_price * 0.9)
        return synthetic_bid, last_price, "last_price_fallback", "Longbridge bid/ask are unavailable; using last price as an indicative option price."
    return bid, ask, "unavailable", "Longbridge did not return a usable bid/ask or last price."


def _last_price(daily: list[dict[str, Any]], intraday: list[dict[str, Any]], quote_data: dict[str, Any]) -> float:
    for key in ("last", "last_price", "price", "last_done"):
        value = quote_data.get(key)
        number = _num(value)
        if number > 0:
            return number
    if intraday and _num(intraday[-1].get("price")) > 0:
        return _num(intraday[-1].get("price"))
    if daily and _num(daily[-1].get("close")) > 0:
        return _num(daily[-1].get("close"))
    return 0.0


def _num(value: object) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0

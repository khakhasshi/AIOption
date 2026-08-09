from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from math import erf, exp, log, pi, sqrt
from statistics import mean
from typing import Any

from .time_utils import et_today
from .decision_gate import split_candidate_decision_scores
from .market_structure import build_volatility_context, build_volume_profile, volume_profile_side_score

from .market_math import f


SESSION_MINUTES = 390
DELTA_STYLE_RANGES = {
    "day_trend": (0.30, 0.55),
    "lottery": (0.08, 0.25),
    "steady_follow": (0.45, 0.65),
    "budget_swing": (0.20, 0.45),
}


def build_intraday_option_tools(
    quote: dict[str, Any],
    daily_candles: list[dict[str, Any]],
    intraday_points: list[dict[str, Any]],
) -> dict[str, Any]:
    prices = [_num(item.get("price")) for item in intraday_points if _num(item.get("price")) > 0]
    if not prices:
        return {
            "tool_names": [
                "vwap_structure",
                "opening_range_breakout",
                "relative_volume",
                "ema_trend",
                "key_levels",
                "macd_momentum",
                "option_greeks",
                "volume_profile",
            ],
            "available": False,
            "volume_profile": build_volume_profile(intraday_points, daily_candles, 0.0),
        }

    last_price = prices[-1]
    vwap = _num(intraday_points[-1].get("avg_price"))
    tools = {
        "tool_names": [
            "vwap_structure",
            "opening_range_breakout",
            "relative_volume",
            "ema_trend",
            "key_levels",
            "macd_momentum",
            "option_greeks",
            "volume_profile",
        ],
        "available": True,
        "vwap_structure": _vwap_structure(prices, vwap),
        "opening_ranges": {
            "5m": _opening_range(intraday_points, 5),
            "15m": _opening_range(intraday_points, 15),
            "30m": _opening_range(intraday_points, 30),
        },
        "relative_volume": _relative_volume(daily_candles, intraday_points),
        "ema_trend": _ema_trend(prices),
        "multi_timeframe_trend": {
            "1m": _timeframe_trend(prices),
            "5m": _timeframe_trend(_sample_closes(prices, 5)),
            "15m": _timeframe_trend(_sample_closes(prices, 15)),
        },
        "key_levels": _key_levels(quote, daily_candles, intraday_points, last_price),
        "volume_profile": build_volume_profile(intraday_points, daily_candles, last_price),
        "macd_momentum": _macd(prices),
    }
    tools["day_trade_bias"] = _day_trade_bias(tools)
    return tools


def enrich_option_greeks(candidates: list[Any], spot: float) -> list[Any]:
    enriched: list[Any] = []
    for candidate in candidates:
        greeks = estimate_option_greeks(
            spot=spot,
            strike=float(candidate.strike),
            expiration=str(candidate.expiration),
            side=str(candidate.side),
            implied_volatility=float(candidate.implied_volatility),
            option_price=float(candidate.ask),
        )
        enriched.append(candidate.with_greeks(**greeks))
    return enriched


def supplement_option_greek_inputs_from_yfinance(candidates: list[Any], spot: float) -> list[Any]:
    rows = list(candidates or [])
    if not rows or spot <= 0:
        return rows
    # ThetaData (paid options tier) supplies implied volatility per contract; full
    # native Greeks are not in the current plan, so Greeks are computed via BSM from
    # this IV downstream. We only borrow yfinance IV for contracts where the primary
    # option source did NOT return a usable IV. Contracts that already carry IV keep
    # it, which avoids a redundant yfinance round-trip and honours "options use
    # ThetaData" — yfinance is a fallback only.
    missing_iv = [candidate for candidate in rows if _num(getattr(candidate, "implied_volatility", 0.0)) <= 0]
    if not missing_iv:
        return rows
    days = [_days_to_expiration(getattr(candidate, "expiration", "")) for candidate in missing_iv]
    valid_days = [item for item in days if item >= 0]
    if not valid_days:
        return rows
    symbol = _candidate_root_symbol(rows[0])
    if not symbol:
        return rows
    try:
        from .yfinance_option_tool import collect_candidates as yf_collect_candidates

        legacy_rows = yf_collect_candidates(
            symbol=symbol,
            spot=spot,
            min_days=min(valid_days),
            max_days=max(valid_days),
            max_ask=9999,
            lottery=False,
            preferred_side=None,
            min_ask=0.0,
            gex_mode=True,
        )
    except Exception:
        return rows
    legacy_by_key = {_candidate_greek_key(item): item for item in legacy_rows if _candidate_greek_key(item)}
    if not legacy_by_key:
        return rows
    supplemented: list[Any] = []
    for candidate in rows:
        if _num(getattr(candidate, "implied_volatility", 0.0)) > 0:
            # Primary option source (ThetaData) already provided IV — keep it.
            supplemented.append(candidate)
            continue
        legacy = legacy_by_key.get(_candidate_greek_key(candidate))
        legacy_iv = _num(getattr(legacy, "implied_volatility", 0.0)) if legacy is not None else 0.0
        if legacy_iv <= 0:
            supplemented.append(candidate)
            continue
        warning = _append_quote_warning(
            str(getattr(candidate, "quote_warning", "") or ""),
            "Greek inputs use yfinance implied volatility because the option source returned no IV.",
        )
        supplemented.append(replace(candidate, implied_volatility=legacy_iv, quote_warning=warning))
    return supplemented


def build_gex_context(candidates: list[Any], spot: float) -> dict[str, Any]:
    context, _rows = _gex_context(candidates, spot)
    return context


DEFAULT_ANALYSIS_MODULES = {
    "intraday": True,
    "greeks": True,
    "gex": True,
    "execution": True,
    "volatility": True,
    "strategy": True,
    "scenario": True,
    "risk": True,
    "market_structure": True,
}


def normalize_analysis_modules(modules: dict[str, Any] | None = None) -> dict[str, bool]:
    normalized = dict(DEFAULT_ANALYSIS_MODULES)
    for key, value in (modules or {}).items():
        if key in normalized:
            normalized[key] = bool(value)
    return normalized


def enrich_option_analysis(
    candidates: list[Any],
    spot: float,
    intent: Any,
    modules: dict[str, Any] | None = None,
    daily_candles: list[dict[str, Any]] | None = None,
    intraday_points: list[dict[str, Any]] | None = None,
    news_items: list[dict[str, Any]] | None = None,
) -> list[Any]:
    if not candidates:
        return []
    modules = normalize_analysis_modules(modules)
    style = infer_delta_style(intent)
    delta_min, delta_max = DELTA_STYLE_RANGES[style]
    iv_values = sorted(float(candidate.implied_volatility) for candidate in candidates if float(candidate.implied_volatility) > 0)
    iv_min = min(iv_values) if iv_values else 0.0
    iv_max = max(iv_values) if iv_values else 0.0
    expiration_iv_means = _expiration_iv_means(candidates) if modules["volatility"] else {}
    term_slope_pct, term_state = _term_structure(expiration_iv_means) if modules["volatility"] else (0.0, "disabled")
    gex_context, gex_map = _gex_context(candidates, spot) if modules.get("gex", True) and modules["greeks"] else (_empty_gex_context(), {})
    volatility_context = build_volatility_context(candidates, daily_candles or [], news_items or []) if modules["volatility"] else {"available": False, "event_risk": {"score": 0, "state": "disabled", "flags": []}}
    volume_profile = build_volume_profile(intraday_points or [], daily_candles or [], spot) if modules.get("market_structure", True) else {"available": False}

    analyzed = []
    for candidate in candidates:
        mid = _mid_price(candidate.bid, candidate.ask)
        ask_to_mid_pct = (candidate.ask / mid - 1) * 100 if mid else 100.0
        execution_score, execution_state = _execution_quality(candidate, ask_to_mid_pct) if modules["execution"] else (0.0, "disabled")
        theta_to_ask_pct, theta_state = _theta_pressure(candidate)
        expected_move = spot * float(candidate.implied_volatility) * sqrt(max(candidate.days_to_expiration, 1.0) / 365)
        expected_move_pct = expected_move / spot * 100 if spot else 0.0
        breakeven_within_expected_move = abs(float(candidate.move_to_breakeven_pct)) <= expected_move_pct
        iv_rank, iv_percentile = _iv_position(float(candidate.implied_volatility), iv_values, iv_min, iv_max) if modules["volatility"] else (0.0, 0.0)
        expiration_iv_mean = expiration_iv_means.get(str(candidate.expiration), 0.0)
        expiration_iv_premium_pct = _safe_pct(float(candidate.implied_volatility), expiration_iv_mean)
        delta_abs = abs(float(candidate.delta))
        delta_match = delta_min <= delta_abs <= delta_max
        strategy_tag = infer_strategy_tag(candidate, intent, iv_percentile, term_state) if modules["strategy"] else "disabled"
        rv20 = float(volatility_context.get("rv20") or 0.0)
        rv60 = float(volatility_context.get("rv60") or 0.0)
        rv_rank = float(volatility_context.get("rv_rank") or 0.0)
        iv_rv_ratio = float(candidate.implied_volatility) / rv20 if rv20 > 0 and float(candidate.implied_volatility) > 0 else 0.0
        iv_rv_premium_pct = (iv_rv_ratio - 1) * 100 if iv_rv_ratio else 0.0
        event_risk = volatility_context.get("event_risk") if isinstance(volatility_context.get("event_risk"), dict) else {}
        volatility_score, iv_edge_state = _volatility_edge_score(
            iv_rv_ratio=iv_rv_ratio,
            iv_percentile=iv_percentile,
            rv_rank=rv_rank,
            event_risk_score=float(event_risk.get("score") or 0.0),
            strategy_tag=strategy_tag,
        )
        scenario_prices = _scenario_prices(candidate, spot) if modules["scenario"] else {}
        gex_row = gex_map.get(str(candidate.contract_symbol), {}) if modules.get("gex", True) else {}
        volume_profile_row = volume_profile_side_score(volume_profile, str(candidate.side), spot) if modules.get("market_structure", True) else {"score": 0.0, "state": "disabled", "flags": []}
        market_structure_score = float(volume_profile_row.get("score") or 0.0)
        if modules["scenario"]:
            probability_itm, probability_breakeven, probability_touch = _probabilities(candidate, spot)
            reward_risk_score = _reward_risk_score(candidate, scenario_prices, probability_breakeven)
        else:
            probability_itm, probability_breakeven, probability_touch, reward_risk_score = 0.0, 0.0, 0.0, 0.0
        risk_plan = _risk_plan(candidate, strategy_tag, spot, scenario_prices) if modules["risk"] else {}
        if risk_plan:
            risk_plan["iv_rv_note"] = f"IV/RV {iv_rv_ratio:.2f}，IV 相对 RV 溢价 {iv_rv_premium_pct:.0f}%。"
            risk_plan["volume_profile_note"] = _volume_profile_note_from_flags(str(candidate.side), volume_profile, volume_profile_row)
        analysis_score = _analysis_score(
            candidate=candidate,
            execution_score=execution_score,
            theta_to_ask_pct=theta_to_ask_pct,
            breakeven_within_expected_move=breakeven_within_expected_move,
            delta_match=delta_match,
            style=style,
            strategy_tag=strategy_tag,
            iv_percentile=iv_percentile,
            expiration_iv_premium_pct=expiration_iv_premium_pct,
            iv_rv_ratio=iv_rv_ratio,
            rv_rank=rv_rank,
            event_risk_score=float(event_risk.get("score") or 0.0),
            volatility_score=volatility_score,
            term_structure_state=term_state,
            gex_context=gex_context,
            gex_row=gex_row,
            market_structure_score=market_structure_score,
            probability_breakeven=probability_breakeven,
            reward_risk_score=reward_risk_score,
            modules=modules,
        )
        analyzed.append(
            candidate.with_option_analysis(
                mid_price=mid,
                ask_to_mid_pct=ask_to_mid_pct,
                execution_quality_score=execution_score,
                execution_quality_state=execution_state,
                alpha_score=0.0,
                execution_score=execution_score,
                decision_score=0.0,
                decision_bucket="pending_gate",
                theta_to_ask_pct=theta_to_ask_pct,
                theta_pressure_state=theta_state,
                expected_move=expected_move,
                expected_move_pct=expected_move_pct,
                breakeven_within_expected_move=breakeven_within_expected_move,
                iv_rank=iv_rank,
                iv_percentile=iv_percentile,
                delta_style=style,
                delta_style_min=delta_min,
                delta_style_max=delta_max,
                delta_style_match=delta_match,
                expiration_iv_mean=expiration_iv_mean,
                expiration_iv_premium_pct=expiration_iv_premium_pct,
                term_structure_slope_pct=term_slope_pct,
                term_structure_state=term_state,
                rv20=rv20,
                rv60=rv60,
                rv_rank=rv_rank,
                iv_rv_ratio=iv_rv_ratio,
                iv_rv_premium_pct=iv_rv_premium_pct,
                iv_edge_state=iv_edge_state,
                iv_rank_source="chain_cross_section",
                event_risk_score=float(event_risk.get("score") or 0.0),
                event_risk_state=str(event_risk.get("state") or "low"),
                event_risk_flags=list(event_risk.get("flags") or []),
                volatility_score=volatility_score,
                volume_profile_score=float(volume_profile_row.get("score") or 0.0),
                volume_profile_state=str(volume_profile_row.get("state") or "unknown"),
                volume_profile_poc=float(volume_profile.get("poc") or 0.0),
                volume_profile_value_area_low=float(volume_profile.get("value_area_low") or 0.0),
                volume_profile_value_area_high=float(volume_profile.get("value_area_high") or 0.0),
                volume_profile_position=str(volume_profile.get("position") or "unknown"),
                volume_profile_low_volume_room_pct=float((volume_profile.get("low_volume_room_up_pct") if str(candidate.side) == "call" else volume_profile.get("low_volume_room_down_pct")) or 0.0),
                market_structure_score=market_structure_score,
                market_structure_flags=list(volume_profile_row.get("flags") or []),
                strategy_tag=strategy_tag,
                probability_itm=probability_itm,
                probability_breakeven=probability_breakeven,
                probability_touch=probability_touch,
                reward_risk_score=reward_risk_score,
                gex_value=_num(gex_row.get("gex_value")),
                gex_per_1pct=_num(gex_row.get("gex_per_1pct")),
                gex_share_pct=_num(gex_row.get("gex_share_pct")),
                gex_strike_rank=int(_num(gex_row.get("gex_strike_rank"))),
                gex_regime=str(gex_row.get("gex_regime") or gex_context.get("regime") or "unknown"),
                gex_alignment=str(gex_row.get("gex_alignment") or "neutral"),
                gex_nearest_wall=str(gex_row.get("gex_nearest_wall") or gex_context.get("nearest_wall") or ""),
                gex_nearest_wall_distance_pct=_num(gex_row.get("gex_nearest_wall_distance_pct")),
                gex_call_wall=_num(gex_context.get("call_wall")),
                gex_put_wall=_num(gex_context.get("put_wall")),
                gex_gamma_flip=_num(gex_context.get("gamma_flip")),
                scenario_prices=scenario_prices,
                risk_plan=risk_plan,
                analysis_score=analysis_score,
            )
    )
    return sorted(analyzed, key=lambda item: item.analysis_score, reverse=True)


def apply_decision_scores(candidates: list[Any], gate: dict[str, Any] | None = None) -> list[Any]:
    if not candidates:
        return []
    scored = []
    for candidate in candidates:
        decision = _decision_scores(candidate, gate)
        scored.append(
            candidate.with_option_analysis(
                mid_price=float(candidate.mid_price),
                ask_to_mid_pct=float(candidate.ask_to_mid_pct),
                execution_quality_score=float(candidate.execution_quality_score),
                execution_quality_state=str(candidate.execution_quality_state),
                alpha_score=decision["alpha_score"],
                execution_score=decision["execution_score"],
                decision_score=decision["decision_score"],
                decision_bucket=decision["decision_bucket"],
                theta_to_ask_pct=float(candidate.theta_to_ask_pct),
                theta_pressure_state=str(candidate.theta_pressure_state),
                expected_move=float(candidate.expected_move),
                expected_move_pct=float(candidate.expected_move_pct),
                breakeven_within_expected_move=bool(candidate.breakeven_within_expected_move),
                iv_rank=float(candidate.iv_rank),
                iv_percentile=float(candidate.iv_percentile),
                delta_style=str(candidate.delta_style),
                delta_style_min=float(candidate.delta_style_min),
                delta_style_max=float(candidate.delta_style_max),
                delta_style_match=bool(candidate.delta_style_match),
                expiration_iv_mean=float(candidate.expiration_iv_mean),
                expiration_iv_premium_pct=float(candidate.expiration_iv_premium_pct),
                term_structure_slope_pct=float(candidate.term_structure_slope_pct),
                term_structure_state=str(candidate.term_structure_state),
                rv20=float(getattr(candidate, "rv20", 0.0)),
                rv60=float(getattr(candidate, "rv60", 0.0)),
                rv_rank=float(getattr(candidate, "rv_rank", 0.0)),
                iv_rv_ratio=float(getattr(candidate, "iv_rv_ratio", 0.0)),
                iv_rv_premium_pct=float(getattr(candidate, "iv_rv_premium_pct", 0.0)),
                iv_edge_state=str(getattr(candidate, "iv_edge_state", "unknown")),
                iv_rank_source=str(getattr(candidate, "iv_rank_source", "chain_cross_section")),
                event_risk_score=float(getattr(candidate, "event_risk_score", 0.0)),
                event_risk_state=str(getattr(candidate, "event_risk_state", "low")),
                event_risk_flags=list(getattr(candidate, "event_risk_flags", []) or []),
                volatility_score=float(getattr(candidate, "volatility_score", 0.0)),
                volume_profile_score=float(getattr(candidate, "volume_profile_score", 0.0)),
                volume_profile_state=str(getattr(candidate, "volume_profile_state", "unknown")),
                volume_profile_poc=float(getattr(candidate, "volume_profile_poc", 0.0)),
                volume_profile_value_area_low=float(getattr(candidate, "volume_profile_value_area_low", 0.0)),
                volume_profile_value_area_high=float(getattr(candidate, "volume_profile_value_area_high", 0.0)),
                volume_profile_position=str(getattr(candidate, "volume_profile_position", "unknown")),
                volume_profile_low_volume_room_pct=float(getattr(candidate, "volume_profile_low_volume_room_pct", 0.0)),
                market_structure_score=float(getattr(candidate, "market_structure_score", 0.0)),
                market_structure_flags=list(getattr(candidate, "market_structure_flags", []) or []),
                strategy_tag=str(candidate.strategy_tag),
                probability_itm=float(candidate.probability_itm),
                probability_breakeven=float(candidate.probability_breakeven),
                probability_touch=float(candidate.probability_touch),
                reward_risk_score=float(candidate.reward_risk_score),
                gex_value=float(candidate.gex_value),
                gex_per_1pct=float(candidate.gex_per_1pct),
                gex_share_pct=float(candidate.gex_share_pct),
                gex_strike_rank=int(candidate.gex_strike_rank),
                gex_regime=str(candidate.gex_regime),
                gex_alignment=str(candidate.gex_alignment),
                gex_nearest_wall=str(candidate.gex_nearest_wall),
                gex_nearest_wall_distance_pct=float(candidate.gex_nearest_wall_distance_pct),
                gex_call_wall=float(candidate.gex_call_wall),
                gex_put_wall=float(candidate.gex_put_wall),
                gex_gamma_flip=float(candidate.gex_gamma_flip),
                scenario_prices=dict(candidate.scenario_prices),
                risk_plan=dict(candidate.risk_plan),
                analysis_score=float(candidate.analysis_score),
                trigger_score=float(decision["trigger_score"]),
                trigger_state=str(decision["trigger_state"]),
                trigger_reasons=list(decision["trigger_reasons"]),
                execution_hard_flags=list(decision["execution_hard_flags"]),
                time_value_risk_penalty=float(decision["time_value_risk_penalty"]),
            )
        )
    return sorted(scored, key=lambda item: getattr(item, "decision_score", 0.0), reverse=True)


def infer_delta_style(intent: Any) -> str:
    if getattr(intent, "day_trade", False):
        return "day_trend"
    if getattr(intent, "lottery", False):
        return "lottery"
    if not getattr(intent, "cheap", False):
        return "steady_follow"
    return "budget_swing"


def infer_strategy_tag(candidate: Any, intent: Any, iv_percentile: float, term_state: str) -> str:
    days = float(candidate.days_to_expiration)
    moneyness = float(candidate.moneyness_pct)
    if getattr(intent, "day_trade", False):
        return "日内动量单"
    if getattr(intent, "lottery", False) or (days <= 14 and moneyness >= 4 and abs(float(candidate.delta)) <= 0.25):
        return "纯彩票单"
    if days <= 10 and (iv_percentile >= 70 or term_state in {"front_iv_elevated", "front_iv_hot"}):
        return "隔夜事件单"
    return "趋势跟随单"


def estimate_option_greeks(
    spot: float,
    strike: float,
    expiration: str,
    side: str,
    implied_volatility: float,
    option_price: float,
    risk_free_rate: float = 0.045,
) -> dict[str, float]:
    today = et_today()
    expiry = datetime.strptime(expiration, "%Y-%m-%d").date()
    days = max((expiry - today).days, 0)
    time_years = max(days / 365, 1 / 365)
    sigma = max(implied_volatility, 0.0001)
    if spot <= 0 or strike <= 0:
        return _empty_greeks(side, strike, option_price)

    d1 = (log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * time_years) / (sigma * sqrt(time_years))
    d2 = d1 - sigma * sqrt(time_years)
    pdf = exp(-0.5 * d1 * d1) / sqrt(2 * pi)
    gamma = pdf / (spot * sigma * sqrt(time_years))
    if side == "call":
        delta = _norm_cdf(d1)
        theta = (-(spot * pdf * sigma) / (2 * sqrt(time_years)) - risk_free_rate * strike * exp(-risk_free_rate * time_years) * _norm_cdf(d2)) / 365
        breakeven = strike + option_price
        move_to_breakeven_pct = (breakeven / spot - 1) * 100
    else:
        delta = _norm_cdf(d1) - 1
        theta = (-(spot * pdf * sigma) / (2 * sqrt(time_years)) + risk_free_rate * strike * exp(-risk_free_rate * time_years) * _norm_cdf(-d2)) / 365
        breakeven = strike - option_price
        move_to_breakeven_pct = (1 - breakeven / spot) * 100

    return {
        "delta": delta,
        "gamma": gamma,
        "theta_per_day": theta,
        "breakeven": breakeven,
        "move_to_strike_pct": abs(strike / spot - 1) * 100,
        "move_to_breakeven_pct": move_to_breakeven_pct,
        "days_to_expiration": float(days),
    }


def _mid_price(bid: float, ask: float) -> float:
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return ask if ask > 0 else bid


def _execution_quality(candidate: Any, ask_to_mid_pct: float) -> tuple[float, str]:
    spread_score = max(0.0, 1 - float(candidate.spread_pct) / 25)
    mid_score = max(0.0, 1 - ask_to_mid_pct / 15)
    volume_score = min(1.0, log(max(float(candidate.volume), 0.0) + 1) / log(10_000))
    oi_score = min(1.0, log(max(float(candidate.open_interest), 0.0) + 1) / log(25_000))
    score = 100 * (0.35 * spread_score + 0.25 * mid_score + 0.2 * volume_score + 0.2 * oi_score)
    if score >= 80:
        state = "excellent"
    elif score >= 65:
        state = "good"
    elif score >= 45:
        state = "acceptable"
    else:
        state = "poor"
    return score, state


def _theta_pressure(candidate: Any) -> tuple[float, str]:
    theta_to_ask_pct = abs(float(candidate.theta_per_day)) / float(candidate.ask) * 100 if float(candidate.ask) > 0 else 100.0
    if theta_to_ask_pct >= 35:
        state = "severe"
    elif theta_to_ask_pct >= 18:
        state = "elevated"
    elif theta_to_ask_pct >= 8:
        state = "moderate"
    else:
        state = "low"
    return theta_to_ask_pct, state


def _iv_position(iv: float, iv_values: list[float], iv_min: float, iv_max: float) -> tuple[float, float]:
    if not iv_values:
        return 0.0, 0.0
    iv_rank = (iv - iv_min) / (iv_max - iv_min) * 100 if iv_max > iv_min else 50.0
    count_at_or_below = sum(1 for value in iv_values if value <= iv)
    iv_percentile = count_at_or_below / len(iv_values) * 100
    return iv_rank, iv_percentile


def _expiration_iv_means(candidates: list[Any]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for candidate in candidates:
        iv = float(candidate.implied_volatility)
        if iv > 0:
            grouped.setdefault(str(candidate.expiration), []).append(iv)
    return {expiration: mean(values) for expiration, values in grouped.items() if values}


def _term_structure(expiration_iv_means: dict[str, float]) -> tuple[float, str]:
    if len(expiration_iv_means) < 2:
        return 0.0, "single_expiration"
    ordered = sorted(expiration_iv_means.items(), key=lambda item: item[0])
    front_iv = ordered[0][1]
    back_iv = ordered[-1][1]
    slope_pct = _safe_pct(front_iv, back_iv)
    if slope_pct >= 25:
        state = "front_iv_hot"
    elif slope_pct >= 10:
        state = "front_iv_elevated"
    elif slope_pct <= -15:
        state = "back_iv_richer"
    else:
        state = "balanced"
    return slope_pct, state


def _scenario_prices(candidate: Any, spot: float) -> dict[str, float]:
    moves = {
        "underlying_-2pct_now": -0.02,
        "underlying_-1pct_now": -0.01,
        "underlying_+1pct_now": 0.01,
        "underlying_+2pct_now": 0.02,
    }
    prices: dict[str, float] = {}
    for label, move in moves.items():
        prices[label] = _bs_price(
            spot=spot * (1 + move),
            strike=float(candidate.strike),
            side=str(candidate.side),
            implied_volatility=float(candidate.implied_volatility),
            days=max(float(candidate.days_to_expiration), 1.0),
        )
    prices["one_day_decay"] = _bs_price(
        spot=spot,
        strike=float(candidate.strike),
        side=str(candidate.side),
        implied_volatility=float(candidate.implied_volatility),
        days=max(float(candidate.days_to_expiration) - 1, 1 / 365),
    )
    prices["three_day_decay"] = _bs_price(
        spot=spot,
        strike=float(candidate.strike),
        side=str(candidate.side),
        implied_volatility=float(candidate.implied_volatility),
        days=max(float(candidate.days_to_expiration) - 3, 1 / 365),
    )
    prices["upside_payoff_multiple_2pct"] = prices["underlying_+2pct_now"] / float(candidate.ask) if str(candidate.side) == "call" and candidate.ask else prices["underlying_-2pct_now"] / float(candidate.ask) if candidate.ask else 0.0
    return {key: round(value, 4) for key, value in prices.items()}


def _probabilities(candidate: Any, spot: float, risk_free_rate: float = 0.045) -> tuple[float, float, float]:
    days = max(float(candidate.days_to_expiration), 1.0)
    sigma = max(float(candidate.implied_volatility), 0.0001)
    time_years = days / 365
    strike = float(candidate.strike)
    breakeven = float(candidate.breakeven)
    probability_itm = _probability_above(spot, strike, sigma, time_years, risk_free_rate)
    probability_breakeven = _probability_above(spot, breakeven, sigma, time_years, risk_free_rate)
    if str(candidate.side) == "put":
        probability_itm = 1 - probability_itm
        probability_breakeven = 1 - probability_breakeven
    probability_touch = min(1.0, max(probability_itm, probability_breakeven) * 2)
    return probability_itm * 100, probability_breakeven * 100, probability_touch * 100


def _probability_above(spot: float, target: float, sigma: float, time_years: float, risk_free_rate: float) -> float:
    if spot <= 0 or target <= 0:
        return 0.0
    d2 = (log(spot / target) + (risk_free_rate - 0.5 * sigma * sigma) * time_years) / (sigma * sqrt(time_years))
    return _norm_cdf(d2)


def _reward_risk_score(candidate: Any, scenario_prices: dict[str, float], probability_breakeven: float) -> float:
    ask = float(candidate.ask)
    if ask <= 0:
        return 0.0
    favorable_key = "underlying_+2pct_now" if str(candidate.side) == "call" else "underlying_-2pct_now"
    unfavorable_key = "underlying_-1pct_now" if str(candidate.side) == "call" else "underlying_+1pct_now"
    favorable = scenario_prices.get(favorable_key, 0.0)
    unfavorable = scenario_prices.get(unfavorable_key, 0.0)
    upside = max(favorable / ask - 1, -1.0)
    downside = min(max(1 - unfavorable / ask, 0.0), 1.0)
    return round((upside * 100 * (probability_breakeven / 100)) - (downside * 45), 2)


def _risk_plan(candidate: Any, strategy_tag: str, spot: float, scenario_prices: dict[str, float]) -> dict[str, str | float]:
    ask = float(candidate.ask)
    max_loss = ask * 100
    stop_loss_price = max(round(ask * 0.55, 2), 0.01)
    take_profit_1 = round(ask * (1.8 if strategy_tag == "纯彩票单" else 1.45), 2)
    take_profit_2 = round(ask * (3.0 if strategy_tag == "纯彩票单" else 2.1), 2)
    latest_exit = "当日收盘前" if strategy_tag == "日内动量单" else "到期前 1 个交易日" if float(candidate.days_to_expiration) <= 7 else "到期前 3 个交易日"
    if str(candidate.side) == "call":
        invalidation = f"正股跌回 {spot * 0.99:.2f} 下方或无法站上触发位"
    else:
        invalidation = f"正股反弹回 {spot * 1.01:.2f} 上方或无法跌破触发位"
    return {
        "max_loss_per_contract": round(max_loss, 2),
        "stop_loss_option_price": stop_loss_price,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "invalidation": invalidation,
        "latest_exit": latest_exit,
        "iv_rv_note": f"IV/RV {float(getattr(candidate, 'iv_rv_ratio', 0.0)):.2f}；避免在高 IV 溢价且方向触发不足时追单。" if float(getattr(candidate, "iv_rv_ratio", 0.0)) > 0 else "",
        "volume_profile_note": _volume_profile_risk_note(candidate),
        "position_note": "单张合约风险为权利金全损；避免市价单，挂 mid 附近限价。",
    }


def _volume_profile_risk_note(candidate: Any) -> str:
    state = str(getattr(candidate, "volume_profile_state", "") or "")
    flags = list(getattr(candidate, "market_structure_flags", []) or [])
    if "call_under_value_area_high_resistance" in flags:
        return "CALL 上方接近 Value Area High/筹码峰阻力，突破确认前降低追价。"
    if "put_above_value_area_low_support" in flags:
        return "PUT 下方接近 Value Area Low/筹码峰支撑，跌破确认前降低追价。"
    if "call_has_low_volume_breakout_room" in flags or "put_has_low_volume_breakdown_room" in flags:
        return "存在低成交量真空区，触发后价格更容易加速，止盈可参考下一价值区边界。"
    if state == "unavailable":
        return "成交量价格分布不可用，按普通技术位管理。"
    return ""


def _volume_profile_note_from_flags(side: str, profile: dict[str, Any], row: dict[str, Any]) -> str:
    flags = list(row.get("flags") or [])
    if "call_under_value_area_high_resistance" in flags:
        return f"CALL 上方接近筹码峰/VAH {float(profile.get('value_area_high') or 0):.2f}，突破确认前避免追高。"
    if "put_above_value_area_low_support" in flags:
        return f"PUT 下方接近筹码峰/VAL {float(profile.get('value_area_low') or 0):.2f}，跌破确认前避免追空。"
    if "call_has_low_volume_breakout_room" in flags:
        return f"上方低成交量真空区空间约 {float(profile.get('low_volume_room_up_pct') or 0):.1f}%，止盈可参考 VAH/下一节点。"
    if "put_has_low_volume_breakdown_room" in flags:
        return f"下方低成交量真空区空间约 {float(profile.get('low_volume_room_down_pct') or 0):.1f}%，止盈可参考 VAL/下一节点。"
    if profile.get("available"):
        return f"成交量价格分布位置 {profile.get('position') or 'unknown'}，POC {float(profile.get('poc') or 0):.2f}。"
    return "成交量价格分布不可用，使用普通技术位。"


def _analysis_score(
    candidate: Any,
    execution_score: float,
    theta_to_ask_pct: float,
    breakeven_within_expected_move: bool,
    delta_match: bool,
    style: str,
    strategy_tag: str,
    iv_percentile: float,
    expiration_iv_premium_pct: float,
    iv_rv_ratio: float,
    rv_rank: float,
    event_risk_score: float,
    volatility_score: float,
    term_structure_state: str,
    gex_context: dict[str, Any],
    gex_row: dict[str, Any],
    market_structure_score: float,
    probability_breakeven: float,
    reward_risk_score: float,
    modules: dict[str, bool],
) -> float:
    score = float(candidate.score)
    score += execution_score / 10
    score += 2.5 if delta_match else -2.0
    score += 1.5 if breakeven_within_expected_move else -1.5
    score -= min(theta_to_ask_pct / 12, 4.0)
    if modules["scenario"]:
        score += min(max(reward_risk_score, -20), 40) / 10
        score += min(probability_breakeven / 18, 2.5)
    if modules["volatility"]:
        score += volatility_score
        if iv_percentile >= 85:
            score -= 3.0
        elif iv_percentile >= 70:
            score -= 1.5
        elif iv_percentile <= 35:
            score += 1.0
        if expiration_iv_premium_pct >= 20:
            score -= 1.8
        elif expiration_iv_premium_pct <= -10:
            score += 0.8
        if term_structure_state == "front_iv_hot":
            score += 0.8 if strategy_tag == "隔夜事件单" else -1.5
        elif term_structure_state == "back_iv_richer" and strategy_tag in {"趋势跟随单", "纯彩票单"}:
            score += 0.8
        if iv_rv_ratio >= 1.7 and strategy_tag not in {"隔夜事件单", "IV预扩张"}:
            score -= 2.2
        elif 0 < iv_rv_ratio <= 0.85 and strategy_tag in {"趋势跟随单", "日内动量单"}:
            score += 1.0
        if event_risk_score >= 70 and style != "lottery":
            score -= 1.2
        if rv_rank >= 80 and strategy_tag == "纯彩票单":
            score -= 0.8
    if modules["strategy"]:
        if strategy_tag == "日内动量单":
            score += execution_score / 18 - min(theta_to_ask_pct / 20, 2.0)
        elif strategy_tag == "隔夜事件单":
            score += min(probability_breakeven / 22, 2.0) - (1.0 if iv_percentile > 80 else 0.0)
        elif strategy_tag == "趋势跟随单":
            score += 1.0 if delta_match and theta_to_ask_pct < 15 else -0.5
        elif strategy_tag == "纯彩票单":
            score += min(abs(float(candidate.gamma)) * 25, 2.5) + min(max(float(candidate.moneyness_pct), 0), 8) / 8
    if style == "lottery":
        score += min(abs(float(candidate.gamma)) * 20, 2.0)
    elif style == "day_trend":
        score += min(abs(float(candidate.gamma)) * 12, 1.5)
    if modules.get("gex", True):
        gex_regime = str(gex_context.get("regime") or "unknown")
        gex_share_pct = max(0.0, float(gex_row.get("gex_share_pct") or 0))
        gex_distance = float(gex_row.get("gex_nearest_wall_distance_pct") or 0)
        if gex_regime == "positive_gamma":
            score -= min(gex_share_pct / 6, 3.0)
            if gex_distance <= 2.0:
                score -= 0.8
        elif gex_regime == "negative_gamma":
            score += min(gex_share_pct / 8, 2.0)
            if strategy_tag in {"日内动量单", "纯彩票单"}:
                score += 0.8
        if str(gex_row.get("gex_alignment") or "") == "tailwind":
            score += 0.8
        elif str(gex_row.get("gex_alignment") or "") == "headwind":
            score -= 0.8
    if modules.get("market_structure", True):
        score += market_structure_score
    return score


def _volatility_edge_score(*, iv_rv_ratio: float, iv_percentile: float, rv_rank: float, event_risk_score: float, strategy_tag: str) -> tuple[float, str]:
    score = 0.0
    if iv_rv_ratio >= 1.7:
        score -= 2.0
        state = "expensive_iv_crush_risk"
    elif iv_rv_ratio >= 1.3:
        score -= 0.8
        state = "iv_elevated"
    elif 0 < iv_rv_ratio <= 0.85:
        score += 1.0
        state = "iv_cheap_vs_rv"
    else:
        state = "iv_fair"
    if iv_percentile >= 85:
        score -= 1.0
    elif iv_percentile <= 30:
        score += 0.5
    if event_risk_score >= 70:
        score += 0.8 if strategy_tag == "隔夜事件单" else -0.8
    if rv_rank >= 85 and iv_rv_ratio > 1.25:
        score -= 0.7
    return round(score, 2), state


def _decision_scores(candidate: Any, gate: dict[str, Any] | None = None) -> dict[str, float | str]:
    alpha = float(candidate.analysis_score) * 0.7
    alpha += float(candidate.reward_risk_score) * 0.12
    alpha += float(getattr(candidate, "volatility_score", 0.0)) * 0.8
    alpha += float(getattr(candidate, "market_structure_score", 0.0)) * 0.7
    alpha += min(abs(float(candidate.gamma)) * 28, 4.0)
    alpha += 1.5 if candidate.delta_style_match else -1.0
    execution = float(candidate.execution_quality_score)
    trigger_score, trigger_state, trigger_reasons = _trigger_score(candidate, gate)
    hard_flags = _execution_hard_flags(candidate)
    time_penalty = _time_value_risk_penalty(candidate)
    if gate:
        if not gate.get("allow_auto_trade", True):
            execution -= 10.0
        if str((gate.get("gex") or {}).get("regime") or "") == "positive_gamma" and str(candidate.gex_alignment) == "headwind":
            alpha -= 3.0
        if str((gate.get("gex") or {}).get("regime") or "") == "negative_gamma" and str(candidate.gex_alignment) == "tailwind":
            alpha += 2.5
    decision_score = alpha * 0.45 + execution * 0.30 + trigger_score * 0.25 - time_penalty
    if hard_flags:
        decision_score -= 28.0
    if gate and gate.get("allow_single_leg") is False:
        decision_score -= 12.0
    if gate and not gate.get("should_trade", True):
        decision_score -= 15.0
    if hard_flags:
        bucket = "blocked_execution"
    elif trigger_score < 60:
        bucket = "observe_trigger_not_met"
    elif alpha >= execution:
        bucket = "alpha_first"
    else:
        bucket = "execution_first"
    return {
        "alpha_score": round(alpha, 2),
        "execution_score": round(execution, 2),
        "trigger_score": round(trigger_score, 2),
        "trigger_state": trigger_state,
        "trigger_reasons": trigger_reasons,
        "execution_hard_flags": hard_flags,
        "time_value_risk_penalty": round(time_penalty, 2),
        "decision_score": round(decision_score, 2),
        "decision_bucket": bucket,
    }


def _trigger_score(candidate: Any, gate: dict[str, Any] | None = None) -> tuple[float, str, list[str]]:
    trigger = (gate or {}).get("single_leg_trigger") or {}
    if not trigger.get("triggered"):
        return 0.0, "observe", list(trigger.get("reasons") or ["单腿硬触发未满足"])
    expected_direction = str(trigger.get("direction") or "neutral")
    candidate_direction = _candidate_direction(candidate)
    reasons = list(trigger.get("reasons") or [])
    if expected_direction in {"bullish", "bearish"} and candidate_direction not in {expected_direction, "neutral"}:
        reasons.append("合约方向与触发方向冲突")
        return 24.0, "mismatch", reasons
    score = float(trigger.get("score") or 0.0)
    if candidate_direction == expected_direction:
        score += 6.0
    if float(candidate.theta_to_ask_pct) <= 20:
        score += 4.0
    return min(score, 100.0), "aligned" if candidate_direction == expected_direction else "triggered", reasons


def _candidate_direction(candidate: Any) -> str:
    if str(getattr(candidate, "side", "")).lower() == "call":
        return "bullish"
    if str(getattr(candidate, "side", "")).lower() == "put":
        return "bearish"
    return "neutral"


def _time_value_risk_penalty(candidate: Any) -> float:
    penalty = 0.0
    theta_pct = float(getattr(candidate, "theta_to_ask_pct", 0.0))
    days = float(getattr(candidate, "days_to_expiration", 0.0))
    ask = float(getattr(candidate, "ask", 0.0))
    breakeven_prob = float(getattr(candidate, "probability_breakeven", 0.0))
    from .time_utils import now_et

    current = now_et()
    minutes_to_close = max(0.0, (16 - current.hour) * 60 - current.minute)
    if days <= 0.5 and current.hour >= 12:
        penalty += 14.0
    if theta_pct > 25:
        penalty += min((theta_pct - 25) * 0.45, 14.0)
    if ask > 0 and ask <= 1.25 and breakeven_prob < 22:
        penalty += 7.0
    if days <= 1 and minutes_to_close <= 180:
        penalty += 8.0
    return round(penalty, 2)


def _execution_hard_flags(candidate: Any) -> list[str]:
    flags: list[str] = []
    ask = float(getattr(candidate, "ask", 0.0))
    bid = float(getattr(candidate, "bid", 0.0))
    spread_pct = float(getattr(candidate, "spread_pct", 0.0))
    volume = float(getattr(candidate, "volume", 0.0))
    open_interest = float(getattr(candidate, "open_interest", 0.0))
    pricing_source = str(getattr(candidate, "pricing_source", "") or "")
    if ask <= 0:
        flags.append("invalid_ask")
    if spread_pct > 30:
        flags.append("wide_spread")
    if volume < 100 and open_interest < 1000:
        flags.append("thin_liquidity")
    if pricing_source == "unavailable":
        flags.append("quote_unavailable")
    if bid <= 0:
        flags.append("no_bid")
    return flags


def _gex_context(candidates: list[Any], spot: float) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    rows = [candidate for candidate in candidates if float(getattr(candidate, "gamma", 0.0)) > 0 and float(getattr(candidate, "open_interest", 0.0)) > 0 and float(getattr(candidate, "strike", 0.0)) > 0]
    if not rows or spot <= 0:
        return _empty_gex_context(), {}
    per_strike: dict[float, float] = {}
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    total_abs = 0.0
    net = 0.0
    ranked: list[tuple[str, float]] = []
    for candidate in rows:
        contract_symbol = str(candidate.contract_symbol)
        sign = 1.0 if str(candidate.side) == "call" else -1.0
        gex_value = float(candidate.gamma) * float(candidate.open_interest) * 100.0 * spot * spot * 0.01 * sign
        per_strike[float(candidate.strike)] = per_strike.get(float(candidate.strike), 0.0) + gex_value
        total_abs += abs(gex_value)
        net += gex_value
        ranked.append((contract_symbol, gex_value))
        rows_by_symbol[contract_symbol] = {
            "gex_value": round(gex_value, 2),
            "gex_per_1pct": round(gex_value, 2),
            "gex_share_pct": 0.0,
            "gex_strike_rank": 0,
        }
    call_wall = max((strike for strike, value in per_strike.items() if value > 0), key=lambda strike: per_strike[strike], default=0.0)
    put_wall = min((strike for strike, value in per_strike.items() if value < 0), key=lambda strike: per_strike[strike], default=0.0)
    flip = _gex_flip(per_strike)
    regime = "positive_gamma" if net > total_abs * 0.12 else "negative_gamma" if net < -total_abs * 0.12 else "neutral"
    nearest_wall = _nearest_wall(spot, call_wall, put_wall)
    for rank, (contract_symbol, gex_value) in enumerate(sorted(ranked, key=lambda item: abs(item[1]), reverse=True), start=1):
        rows_by_symbol[contract_symbol]["gex_share_pct"] = round(abs(gex_value) / total_abs * 100, 2) if total_abs > 0 else 0.0
        rows_by_symbol[contract_symbol]["gex_strike_rank"] = rank
        rows_by_symbol[contract_symbol]["gex_regime"] = regime
        rows_by_symbol[contract_symbol]["gex_nearest_wall"] = nearest_wall["label"]
        rows_by_symbol[contract_symbol]["gex_nearest_wall_distance_pct"] = round(nearest_wall["distance_pct"], 2)
        rows_by_symbol[contract_symbol]["gex_alignment"] = _gex_alignment(candidate_by_symbol(candidates, contract_symbol), regime, nearest_wall["label"], nearest_wall["distance_pct"])
    context = {
        "available": True,
        "regime": regime,
        "spot": round(spot, 2),
        "net_gex": round(net, 2),
        "gross_gex": round(total_abs, 2),
        "call_wall": round(call_wall, 2) if call_wall else 0.0,
        "put_wall": round(put_wall, 2) if put_wall else 0.0,
        "gamma_flip": round(flip, 2) if flip else 0.0,
        "nearest_wall": nearest_wall["label"],
        "nearest_wall_distance_pct": round(nearest_wall["distance_pct"], 2),
        "tailwind": "short_gamma_acceleration" if regime == "negative_gamma" else "pinning_and_mean_reversion" if regime == "positive_gamma" else "mixed",
    }
    return context, rows_by_symbol


def _empty_gex_context() -> dict[str, Any]:
    return {
        "available": False,
        "regime": "unknown",
        "spot": 0.0,
        "net_gex": 0.0,
        "gross_gex": 0.0,
        "call_wall": 0.0,
        "put_wall": 0.0,
        "gamma_flip": 0.0,
        "nearest_wall": "",
        "nearest_wall_distance_pct": 0.0,
        "tailwind": "unknown",
    }


def _gex_flip(per_strike: dict[float, float]) -> float:
    if not per_strike:
        return 0.0
    cumulative = 0.0
    best_strike = 0.0
    best_abs = float("inf")
    for strike, gex in sorted(per_strike.items()):
        cumulative += gex
        current_abs = abs(cumulative)
        if current_abs < best_abs:
            best_abs = current_abs
            best_strike = strike
    return best_strike


def _nearest_wall(spot: float, call_wall: float, put_wall: float) -> dict[str, Any]:
    candidates: list[tuple[str, float]] = []
    if call_wall > 0:
        candidates.append(("call_wall", call_wall))
    if put_wall > 0:
        candidates.append(("put_wall", put_wall))
    if not candidates:
        return {"label": "", "distance_pct": 0.0}
    label, wall = min(candidates, key=lambda item: abs(item[1] - spot))
    return {"label": label, "distance_pct": abs(wall / spot - 1) * 100 if spot > 0 else 0.0}


def candidate_by_symbol(candidates: list[Any], symbol: str) -> Any:
    for candidate in candidates:
        if str(getattr(candidate, "contract_symbol", "")) == symbol:
            return candidate
    return None


def _gex_alignment(candidate: Any, regime: str, nearest_wall: str, distance_pct: float) -> str:
    if candidate is None:
        return "neutral"
    if regime == "positive_gamma":
        if distance_pct <= 2.0:
            return "tailwind" if abs(float(candidate.moneyness_pct)) <= 1.5 else "neutral"
        return "neutral"
    if regime == "negative_gamma":
        if float(candidate.gamma) >= 0 and str(candidate.side) in {"call", "put"}:
            return "tailwind"
        return "headwind"
    return "neutral"


def _bs_price(
    spot: float,
    strike: float,
    side: str,
    implied_volatility: float,
    days: float,
    risk_free_rate: float = 0.045,
) -> float:
    if spot <= 0 or strike <= 0:
        return 0.0
    time_years = max(days / 365, 1 / 365)
    sigma = max(implied_volatility, 0.0001)
    d1 = (log(spot / strike) + (risk_free_rate + 0.5 * sigma * sigma) * time_years) / (sigma * sqrt(time_years))
    d2 = d1 - sigma * sqrt(time_years)
    if side == "call":
        return max(spot * _norm_cdf(d1) - strike * exp(-risk_free_rate * time_years) * _norm_cdf(d2), 0.0)
    return max(strike * exp(-risk_free_rate * time_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1), 0.0)


def _safe_pct(value: float, baseline: float) -> float:
    return (value / baseline - 1) * 100 if baseline else 0.0


def _vwap_structure(prices: list[float], vwap: float) -> dict[str, Any]:
    if not vwap:
        return {"state": "unknown", "vs_vwap_pct": 0.0, "above_vwap_ratio": 0.0}
    recent = prices[-30:] if len(prices) >= 30 else prices
    above_ratio = sum(1 for price in recent if price > vwap) / len(recent)
    vs_vwap_pct = (prices[-1] / vwap - 1) * 100
    slope = _slope_pct(prices[-20:]) if len(prices) >= 20 else _slope_pct(prices)
    if above_ratio >= 0.7 and vs_vwap_pct > 0:
        state = "bullish_hold_above_vwap"
    elif above_ratio <= 0.3 and vs_vwap_pct < 0:
        state = "bearish_hold_below_vwap"
    else:
        state = "mixed_around_vwap"
    return {
        "state": state,
        "last_price": prices[-1],
        "vwap": vwap,
        "vs_vwap_pct": vs_vwap_pct,
        "above_vwap_ratio_30m": above_ratio,
        "recent_price_slope_pct": slope,
    }


def _opening_range(points: list[dict[str, Any]], minutes: int) -> dict[str, Any]:
    window = points[:minutes]
    if len(window) < max(2, minutes // 2):
        return {"minutes": minutes, "state": "insufficient_data"}
    prices = [_num(item.get("price")) for item in window]
    high = max(prices)
    low = min(prices)
    last = _num(points[-1].get("price"))
    if last > high:
        state = "breakout_above_orh"
    elif last < low:
        state = "breakdown_below_orl"
    else:
        state = "inside_opening_range"
    return {
        "minutes": minutes,
        "high": high,
        "low": low,
        "range_pct": (high / low - 1) * 100 if low else 0.0,
        "state": state,
        "distance_to_high_pct": (last / high - 1) * 100 if high else 0.0,
        "distance_to_low_pct": (last / low - 1) * 100 if low else 0.0,
    }


def _relative_volume(daily_candles: list[dict[str, Any]], points: list[dict[str, Any]]) -> dict[str, Any]:
    prior_volumes = [f(item.get("volume")) for item in daily_candles[-21:-1] if f(item.get("volume")) > 0]
    avg_daily_volume = mean(prior_volumes) if prior_volumes else 0.0
    current_volume = sum(_num(item.get("volume")) for item in points)
    elapsed_minutes = _elapsed_minutes(points)
    expected_volume = avg_daily_volume * min(max(elapsed_minutes / SESSION_MINUTES, 0.02), 1.0) if avg_daily_volume else 0.0
    rvol = current_volume / expected_volume if expected_volume else 0.0
    return {
        "current_volume": current_volume,
        "avg_20d_daily_volume": avg_daily_volume,
        "elapsed_minutes": elapsed_minutes,
        "rvol_time_adjusted": rvol,
        "state": "high_participation" if rvol >= 1.5 else "normal" if rvol >= 0.8 else "light_volume",
    }


def _ema_trend(prices: list[float]) -> dict[str, Any]:
    ema9 = _ema(prices, 9)
    ema20 = _ema(prices, 20)
    last = prices[-1]
    if last > ema9 > ema20:
        state = "bullish_stack"
    elif last < ema9 < ema20:
        state = "bearish_stack"
    else:
        state = "mixed"
    return {
        "last_price": last,
        "ema9": ema9,
        "ema20": ema20,
        "state": state,
        "distance_to_ema20_pct": (last / ema20 - 1) * 100 if ema20 else 0.0,
    }


def _timeframe_trend(prices: list[float]) -> dict[str, Any]:
    if len(prices) < 5:
        return {"state": "insufficient_data"}
    ema_fast = _ema(prices, min(8, len(prices)))
    ema_slow = _ema(prices, min(21, len(prices)))
    last = prices[-1]
    if last > ema_fast >= ema_slow:
        state = "up"
    elif last < ema_fast <= ema_slow:
        state = "down"
    else:
        state = "mixed"
    return {"state": state, "slope_pct": _slope_pct(prices[-8:]), "last": last}


def _key_levels(
    quote: dict[str, Any],
    daily_candles: list[dict[str, Any]],
    points: list[dict[str, Any]],
    last_price: float,
) -> dict[str, Any]:
    prev = daily_candles[-2] if len(daily_candles) >= 2 else {}
    pre_market = quote.get("pre_market_quote") if isinstance(quote.get("pre_market_quote"), dict) else {}
    levels = {
        "last_price": last_price,
        "intraday_high": max(_num(item.get("price")) for item in points),
        "intraday_low": min(_num(item.get("price")) for item in points),
        "previous_day_high": _num(prev.get("high")),
        "previous_day_low": _num(prev.get("low")),
        "previous_close": _num(quote.get("prev_close") or prev.get("close")),
        "premarket_high": _num(pre_market.get("high")),
        "premarket_low": _num(pre_market.get("low")),
    }
    nearby = {
        key: {"price": value, "distance_pct": (last_price / value - 1) * 100}
        for key, value in levels.items()
        if key != "last_price" and value > 0
    }
    return {**levels, "nearby_levels": nearby}


def _macd(prices: list[float]) -> dict[str, Any]:
    if len(prices) < 35:
        return {"state": "insufficient_data"}
    macd_series = []
    for index in range(26, len(prices) + 1):
        slice_prices = prices[:index]
        macd_series.append(_ema(slice_prices, 12) - _ema(slice_prices, 26))
    signal = _ema(macd_series, 9)
    hist = macd_series[-1] - signal
    prev_hist = macd_series[-2] - _ema(macd_series[:-1], 9) if len(macd_series) > 10 else hist
    if hist > 0 and hist > prev_hist:
        state = "bullish_momentum_expanding"
    elif hist < 0 and hist < prev_hist:
        state = "bearish_momentum_expanding"
    else:
        state = "momentum_fading_or_mixed"
    return {"macd": macd_series[-1], "signal": signal, "histogram": hist, "previous_histogram": prev_hist, "state": state}


def _day_trade_bias(tools: dict[str, Any]) -> dict[str, Any]:
    votes: list[str] = []
    if tools["vwap_structure"]["state"].startswith("bullish"):
        votes.append("call")
    elif tools["vwap_structure"]["state"].startswith("bearish"):
        votes.append("put")
    if tools["ema_trend"]["state"] == "bullish_stack":
        votes.append("call")
    elif tools["ema_trend"]["state"] == "bearish_stack":
        votes.append("put")
    if tools["opening_ranges"]["15m"]["state"] == "breakout_above_orh":
        votes.append("call")
    elif tools["opening_ranges"]["15m"]["state"] == "breakdown_below_orl":
        votes.append("put")
    if tools["macd_momentum"].get("state") == "bullish_momentum_expanding":
        votes.append("call")
    elif tools["macd_momentum"].get("state") == "bearish_momentum_expanding":
        votes.append("put")

    call_votes = votes.count("call")
    put_votes = votes.count("put")
    if call_votes >= put_votes + 2:
        bias = "call_favored"
    elif put_votes >= call_votes + 2:
        bias = "put_favored"
    else:
        bias = "wait_or_mixed"
    return {"bias": bias, "call_votes": call_votes, "put_votes": put_votes, "votes": votes}


def _sample_closes(prices: list[float], interval: int) -> list[float]:
    return [prices[index] for index in range(interval - 1, len(prices), interval)]


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (period + 1)
    value = values[0]
    for item in values[1:]:
        value = item * alpha + value * (1 - alpha)
    return value


def _slope_pct(values: list[float]) -> float:
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return (values[-1] / values[0] - 1) * 100


def _elapsed_minutes(points: list[dict[str, Any]]) -> int:
    try:
        start = datetime.strptime(str(points[0].get("time")), "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(str(points[-1].get("time")), "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return len(points)
    return max(1, int((end - start).total_seconds() // 60) + 1)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1 + erf(value / sqrt(2)))


def _empty_greeks(side: str, strike: float, option_price: float) -> dict[str, float]:
    breakeven = strike + option_price if side == "call" else strike - option_price
    return {
        "delta": 0.0,
        "gamma": 0.0,
        "theta_per_day": 0.0,
        "breakeven": breakeven,
        "move_to_strike_pct": 0.0,
        "move_to_breakeven_pct": 0.0,
        "days_to_expiration": 0.0,
    }


def _candidate_greek_key(candidate: Any) -> tuple[str, str, str, float] | None:
    root = _candidate_root_symbol(candidate)
    expiration = str(getattr(candidate, "expiration", "") or "")
    side = str(getattr(candidate, "side", "") or "").lower()
    strike = _num(getattr(candidate, "strike", 0.0))
    if not root or not expiration or side not in {"call", "put"} or strike <= 0:
        return None
    return (root, expiration, side, round(strike, 3))


def _candidate_root_symbol(candidate: Any) -> str:
    symbol = str(getattr(candidate, "contract_symbol", "") or "").strip().upper()
    if symbol.endswith(".US"):
        symbol = symbol[:-3]
    for index in range(len(symbol) - 9):
        if symbol[index : index + 6].isdigit() and symbol[index + 6] in {"C", "P"}:
            return symbol[:index]
    return symbol


def _days_to_expiration(expiration: Any) -> int:
    try:
        expiry = datetime.strptime(str(expiration), "%Y-%m-%d").date()
    except ValueError:
        return -1
    return max((expiry - et_today()).days, 0)


def _append_quote_warning(current: str, message: str) -> str:
    if not current:
        return message
    if message in current:
        return current
    return f"{current} {message}"


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

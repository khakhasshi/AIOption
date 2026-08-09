from __future__ import annotations

from typing import Any

from .strategy_gate_templates import strategy_template_catalog
from .time_utils import now_et


STRATEGY_FAMILIES = [
    "spread",
    "credit_spread",
    "straddle",
    "strangle",
    "collar",
    "covered_call",
    "cash_secured_put",
    "calendar",
    "diagonal",
    "poor_mans_covered_call",
    "iron_condor",
    "butterfly",
]


def build_decision_environment_gate(
    *,
    technical_bias: str | None,
    daily_summary: dict[str, Any] | None,
    intraday_summary: dict[str, Any] | None,
    intraday_tools: dict[str, Any] | None,
    gex_context: dict[str, Any] | None,
    candidates: list[Any] | None,
    low_gate_enabled: bool = False,
) -> dict[str, Any]:
    daily = daily_summary or {}
    intraday = intraday_summary or {}
    tools = intraday_tools or {}
    gex = gex_context or {}
    rows = [_candidate_row(item) for item in (candidates or [])]
    candidate_count = len(rows)
    avg_execution = _avg(row.get("execution_quality_score") for row in rows[:8])
    avg_spread = _avg(row.get("spread_pct") for row in rows[:8])
    avg_theta = _avg(row.get("theta_to_ask_pct") for row in rows[:8])
    avg_iv = _avg(row.get("iv_percentile") for row in rows[:8])
    top_probability = max((_num(row.get("probability_breakeven")) for row in rows[:8]), default=0.0)
    top_reward_risk = max((_num(row.get("reward_risk_score")) for row in rows[:8]), default=0.0)

    regime = _market_regime(str(technical_bias or ""), daily, intraday, tools, gex)
    direction_bias = _direction_bias(str(technical_bias or ""))
    direction_votes = _direction_votes(direction_bias, daily, intraday, tools, gex)
    vote_counts = _vote_counts(direction_votes)
    majority_direction = vote_counts["majority_direction"]
    majority_strength = vote_counts["majority_strength"]
    trigger = _single_leg_trigger(direction_bias, daily, intraday, tools, gex)
    preferred_strategy_families = _preferred_strategy_families(regime, gex, avg_iv, majority_direction, trigger)

    blockers: list[str] = []
    warnings: list[str] = []
    reasons: list[str] = []
    allow_single_leg = True
    allow_strategy = True
    allow_auto_trade = True
    should_trade = True

    if candidate_count <= 0:
        blockers.append("候选池为空。")
        allow_single_leg = False
        allow_strategy = False
        allow_auto_trade = False
        should_trade = False
    execution_block_threshold = 25 if low_gate_enabled else 35
    execution_warning_threshold = 45 if low_gate_enabled else 55
    spread_auto_threshold = 35 if low_gate_enabled else 30
    weak_probability_threshold = 15 if low_gate_enabled else 18
    weak_reward_risk_threshold = 6 if low_gate_enabled else 8

    if avg_execution and avg_execution < execution_block_threshold:
        blockers.append("候选执行质量过低。")
        allow_auto_trade = False
    elif avg_execution and avg_execution < execution_warning_threshold:
        warnings.append("候选执行质量一般，优先限价并降低仓位。")
    if avg_spread > spread_auto_threshold:
        warnings.append("候选平均价差偏宽。")
        allow_auto_trade = False
    if avg_theta > 28:
        warnings.append("Theta 压力偏高，单腿需要更明确触发。")

    gex_regime = str(gex.get("regime") or "unknown")
    nearest_wall_distance = _num(gex.get("nearest_wall_distance_pct"))
    if gex_regime == "positive_gamma":
        reasons.append("正 GEX 环境偏钉住/均值回归。")
        if nearest_wall_distance and nearest_wall_distance <= 1.5:
            warnings.append("价格靠近 gamma wall，方向性突破单需要等待确认。")
            allow_single_leg = False if regime in {"range", "choppy"} else allow_single_leg
    elif gex_regime == "negative_gamma":
        reasons.append("负 GEX 环境偏顺势加速。")
        if regime in {"bull_trend", "bear_trend", "momentum"}:
            allow_single_leg = True
    if top_probability and top_probability < weak_probability_threshold and top_reward_risk < weak_reward_risk_threshold:
        warnings.append("达平衡概率和赔率同时偏弱。")
        allow_auto_trade = False

    if not trigger["triggered"]:
        allow_single_leg = False
        warnings.append("单腿硬触发未满足，只保留观察池。")
    elif majority_direction in {"bullish", "bearish"} and trigger["direction"] in {"bullish", "bearish"} and majority_direction != trigger["direction"]:
        allow_single_leg = False
        warnings.append("多周期投票方向与入场触发方向冲突。")

    if majority_direction == "neutral" or majority_strength < 3:
        allow_single_leg = False
        warnings.append("多周期方向票不足，单腿禁用，优先结构策略。")

    if low_gate_enabled and _soft_single_leg_override(
        trigger=trigger,
        majority_direction=majority_direction,
        majority_strength=majority_strength,
        avg_execution=avg_execution,
        avg_spread=avg_spread,
        top_probability=top_probability,
        top_reward_risk=top_reward_risk,
        blocker_count=len(blockers),
        allow_auto_trade=allow_auto_trade,
        should_trade=should_trade,
    ):
        allow_single_leg = True
        warnings = [warning for warning in warnings if warning != "多周期方向票不足，单腿禁用，优先结构策略。"]
        warnings.append("强触发与执行质量达标，轻量放行单腿自动执行。")
        reasons.append("强触发分数、流动性和赔率满足轻量放行条件。")

    if regime in {"range", "choppy"} and gex_regime == "positive_gamma":
        allow_strategy = True
        reasons.append("震荡加正 GEX 更适合定义风险或收租结构。")
    if regime == "unclear" and not warnings:
        warnings.append("市场方向不清，建议等待触发。")
    if not preferred_strategy_families:
        allow_strategy = False
        warnings.append("当前环境未给出清晰策略结构偏好。")

    confidence = _confidence(candidate_count, avg_execution, len(blockers), len(warnings), gex_regime)
    if blockers:
        should_trade = False
    strategy_family_gates = _strategy_family_gates(
        regime=regime,
        gex_regime=gex_regime,
        avg_iv=avg_iv,
        avg_execution=avg_execution,
        avg_spread=avg_spread,
        majority_direction=majority_direction,
        majority_strength=majority_strength,
        trigger=trigger,
        preferred_strategy_families=preferred_strategy_families,
        blocker_count=len(blockers),
        should_trade=should_trade,
        low_gate_enabled=low_gate_enabled,
    )
    allowed_strategy_families = {family: bool(gate.get("allowed")) for family, gate in strategy_family_gates.items()}
    if should_trade:
        allow_strategy = any(allowed_strategy_families.values())
    return {
        "version": 2,
        "low_gate_enabled": bool(low_gate_enabled),
        "gate_profile": "low" if low_gate_enabled else "normal",
        "regime": regime,
        "direction_bias": direction_bias,
        "should_trade": should_trade,
        "allow_single_leg": allow_single_leg,
        "allow_strategy": allow_strategy,
        "allow_auto_trade": allow_auto_trade,
        "preferred_execution": "limit_only" if not allow_auto_trade and should_trade else "wait_trigger" if warnings else "normal",
        "confidence": confidence,
        "candidate_count": candidate_count,
        "avg_execution_quality_score": round(avg_execution, 2),
        "avg_spread_pct": round(avg_spread, 2),
        "avg_theta_to_ask_pct": round(avg_theta, 2),
        "avg_iv_percentile": round(avg_iv, 2),
        "top_probability_breakeven": round(top_probability, 2),
        "top_reward_risk_score": round(top_reward_risk, 2),
        "gex": {
            "available": bool(gex.get("available")),
            "regime": gex_regime,
            "nearest_wall": gex.get("nearest_wall") or "",
            "nearest_wall_distance_pct": round(nearest_wall_distance, 2),
            "call_wall": _num(gex.get("call_wall")),
            "put_wall": _num(gex.get("put_wall")),
            "gamma_flip": _num(gex.get("gamma_flip")),
        },
        "direction_votes": direction_votes,
        "vote_summary": {
            "bullish": vote_counts["bullish"],
            "bearish": vote_counts["bearish"],
            "neutral": vote_counts["neutral"],
            "majority_direction": majority_direction,
            "majority_strength": majority_strength,
        },
        "single_leg_trigger": trigger,
        "preferred_strategy_families": preferred_strategy_families,
        "allowed_strategy_families": allowed_strategy_families,
        "strategy_family_gates": strategy_family_gates,
        "blockers": blockers,
        "warnings": warnings,
        "reasons": reasons,
    }


def split_candidate_decision_scores(candidate: dict[str, Any], gate: dict[str, Any] | None = None) -> dict[str, float | str]:
    alpha = _num(candidate.get("reward_risk_score")) * 0.35
    alpha += _num(candidate.get("probability_breakeven")) * 0.08
    alpha += min(abs(_num(candidate.get("gamma"))) * 20, 3.0)
    alpha += _num(candidate.get("analysis_score")) * 0.35
    execution = _num(candidate.get("execution_quality_score"))
    if execution <= 0:
        spread = _num(candidate.get("spread_pct"))
        volume = _num(candidate.get("volume"))
        oi = _num(candidate.get("open_interest"))
        execution = max(0.0, 100.0 - min(spread * 2, 70.0)) + min(volume / 5000, 1.0) * 15 + min(oi / 15000, 1.0) * 15
    trigger_score, trigger_state, trigger_reasons = _trigger_score(candidate, gate)
    time_penalty = _time_value_risk_penalty(candidate)
    gex_adjustment = 0.0
    if gate:
        gex_regime = str((gate.get("gex") or {}).get("regime") or "")
        alignment = str(candidate.get("gex_alignment") or "")
        if gex_regime == "negative_gamma" and alignment == "tailwind":
            gex_adjustment = 4.0
        elif gex_regime == "positive_gamma" and abs(_num(candidate.get("moneyness_pct"))) > 2:
            gex_adjustment = -4.0
    execution_penalty = max(0.0, 55.0 - execution) * 0.18
    gate_penalty = 0.0
    if gate and gate.get("allow_single_leg") is False:
        gate_penalty += 16.0
    if gate and gate.get("allow_auto_trade") is False:
        gate_penalty += 6.0
    if not trigger_score:
        gate_penalty += 18.0
    hard_flags = _execution_hard_flags(candidate)
    if hard_flags:
        gate_penalty += 28.0
    final = alpha + gex_adjustment + trigger_score * 0.55 - execution_penalty - gate_penalty - time_penalty
    if gate and not gate.get("should_trade", True):
        final -= 15.0
    bucket = "tradable" if trigger_score >= 60 and not hard_flags else "observe_trigger_not_met" if trigger_score < 60 else "blocked_execution"
    return {
        "alpha_score": round(alpha, 2),
        "execution_score": round(execution, 2),
        "trigger_score": round(trigger_score, 2),
        "trigger_state": trigger_state,
        "trigger_reasons": trigger_reasons,
        "execution_hard_flags": hard_flags,
        "time_value_risk_penalty": round(time_penalty, 2),
        "gex_adjustment": round(gex_adjustment, 2),
        "decision_score": round(final, 2),
        "decision_bucket": bucket,
    }


def _market_regime(bias: str, daily: dict[str, Any], intraday: dict[str, Any], tools: dict[str, Any], gex: dict[str, Any]) -> str:
    daily_change = _daily_change_pct(daily)
    intraday_change = _intraday_change_pct(intraday)
    vs_vwap = _num(intraday.get("vs_vwap_pct"))
    rvol = _num(((tools.get("relative_volume") or {}) if isinstance(tools.get("relative_volume"), dict) else {}).get("rvol_time_adjusted"))
    gex_regime = str(gex.get("regime") or "")
    if "bullish" in bias and intraday_change > 0 and vs_vwap >= 0:
        return "momentum" if gex_regime == "negative_gamma" and rvol >= 1.3 else "bull_trend"
    if "bearish" in bias and intraday_change < 0 and vs_vwap <= 0:
        return "momentum" if gex_regime == "negative_gamma" and rvol >= 1.3 else "bear_trend"
    if abs(daily_change) < 0.6 and abs(vs_vwap) < 0.25:
        return "range"
    if daily_change * intraday_change < 0:
        return "choppy"
    return "unclear"


def _direction_bias(bias: str) -> str:
    if "bullish" in bias:
        return "bullish"
    if "bearish" in bias:
        return "bearish"
    return "neutral"


def _direction_votes(direction_bias: str, daily: dict[str, Any], intraday: dict[str, Any], tools: dict[str, Any], gex: dict[str, Any]) -> list[dict[str, Any]]:
    votes: list[dict[str, Any]] = []
    daily_change = _daily_change_pct(daily)
    if daily_change > 0.35:
        votes.append({"source": "daily_trend", "vote": "bullish", "reason": f"daily_change_pct={daily_change:.2f}"})
    elif daily_change < -0.35:
        votes.append({"source": "daily_trend", "vote": "bearish", "reason": f"daily_change_pct={daily_change:.2f}"})
    else:
        votes.append({"source": "daily_trend", "vote": "neutral", "reason": "daily trend too flat"})

    mtf = tools.get("multi_timeframe_trend") or {}
    structure_state = [str((mtf.get(key) or {}).get("state") or "") for key in ("15m", "5m", "1m")]
    bullish_count = sum(1 for state in structure_state if state == "up")
    bearish_count = sum(1 for state in structure_state if state == "down")
    if bullish_count > bearish_count and bullish_count > 0:
        votes.append({"source": "intraday_structure", "vote": "bullish", "reason": "15m/5m/1m 结构偏多"})
    elif bearish_count > bullish_count and bearish_count > 0:
        votes.append({"source": "intraday_structure", "vote": "bearish", "reason": "15m/5m/1m 结构偏空"})
    else:
        votes.append({"source": "intraday_structure", "vote": "neutral", "reason": "多周期结构未形成统一方向"})

    vwap = tools.get("vwap_structure") or {}
    vwap_state = str(vwap.get("state") or "")
    if vwap_state == "bullish_hold_above_vwap":
        votes.append({"source": "vwap", "vote": "bullish", "reason": "价格稳定站上 VWAP"})
    elif vwap_state == "bearish_hold_below_vwap":
        votes.append({"source": "vwap", "vote": "bearish", "reason": "价格稳定跌破 VWAP"})
    else:
        votes.append({"source": "vwap", "vote": "neutral", "reason": "VWAP 周围震荡"})

    orb = (tools.get("opening_ranges") or {}).get("15m") or {}
    orb_state = str(orb.get("state") or "")
    if orb_state == "breakout_above_orh":
        votes.append({"source": "orb", "vote": "bullish", "reason": "突破 ORH"})
    elif orb_state == "breakdown_below_orl":
        votes.append({"source": "orb", "vote": "bearish", "reason": "跌破 ORL"})
    else:
        votes.append({"source": "orb", "vote": "neutral", "reason": "未突破开盘区间"})

    gex_regime = str(gex.get("regime") or "")
    if gex_regime == "negative_gamma":
        votes.append({"source": "gex", "vote": "bullish" if direction_bias != "bearish" else "bearish", "reason": "负 GEX 偏顺势加速"})
    elif gex_regime == "positive_gamma":
        votes.append({"source": "gex", "vote": "neutral", "reason": "正 GEX 偏钉住与均值回归"})
    else:
        votes.append({"source": "gex", "vote": "neutral", "reason": "GEX 未形成明确偏向"})

    macd = tools.get("macd_momentum") or {}
    macd_state = str(macd.get("state") or "")
    if "bull" in macd_state:
        votes.append({"source": "macd", "vote": "bullish", "reason": macd_state})
    elif "bear" in macd_state:
        votes.append({"source": "macd", "vote": "bearish", "reason": macd_state})
    else:
        votes.append({"source": "macd", "vote": "neutral", "reason": macd_state or "macd unknown"})

    return votes


def _vote_counts(votes: list[dict[str, Any]]) -> dict[str, Any]:
    bullish = sum(1 for item in votes if str(item.get("vote")) == "bullish")
    bearish = sum(1 for item in votes if str(item.get("vote")) == "bearish")
    neutral = sum(1 for item in votes if str(item.get("vote")) == "neutral")
    if bullish > bearish and bullish > neutral:
        majority_direction = "bullish"
        majority_strength = bullish
    elif bearish > bullish and bearish > neutral:
        majority_direction = "bearish"
        majority_strength = bearish
    else:
        majority_direction = "neutral"
        majority_strength = max(bullish, bearish)
    return {
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "majority_direction": majority_direction,
        "majority_strength": majority_strength,
    }


def _single_leg_trigger(direction_bias: str, daily: dict[str, Any], intraday: dict[str, Any], tools: dict[str, Any], gex: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    score = 0.0
    direction = "neutral"

    orb = (tools.get("opening_ranges") or {}).get("15m") or {}
    orb_state = str(orb.get("state") or "")
    if orb_state == "breakout_above_orh":
        score += 30
        direction = "bullish"
        reasons.append("突破 ORH")
    elif orb_state == "breakdown_below_orl":
        score += 30
        direction = "bearish"
        reasons.append("跌破 ORL")

    vwap = tools.get("vwap_structure") or {}
    vwap_state = str(vwap.get("state") or "")
    vs_vwap = _num(vwap.get("vs_vwap_pct"))
    if vwap_state == "bullish_hold_above_vwap":
        score += 20
        if direction == "neutral":
            direction = "bullish"
        reasons.append("站上 VWAP")
    elif vwap_state == "bearish_hold_below_vwap":
        score += 20
        if direction == "neutral":
            direction = "bearish"
        reasons.append("跌破 VWAP")
    elif abs(vs_vwap) <= 0.25:
        reasons.append("贴近 VWAP")

    ema = tools.get("ema_trend") or {}
    ema_state = str(ema.get("state") or "")
    if ema_state == "bullish_stack":
        score += 15
        if direction == "neutral":
            direction = "bullish"
        reasons.append("EMA9/20 多头排列")
    elif ema_state == "bearish_stack":
        score += 15
        if direction == "neutral":
            direction = "bearish"
        reasons.append("EMA9/20 空头排列")

    rvol = _num((tools.get("relative_volume") or {}).get("rvol_time_adjusted"))
    if rvol >= 1.3:
        score += 15
        reasons.append(f"RVOL {rvol:.2f}")

    macd = tools.get("macd_momentum") or {}
    macd_state = str(macd.get("state") or "")
    if "bull" in macd_state:
        score += 15
        if direction == "neutral":
            direction = "bullish"
        reasons.append(macd_state)
    elif "bear" in macd_state:
        score += 15
        if direction == "neutral":
            direction = "bearish"
        reasons.append(macd_state)

    gex_regime = str(gex.get("regime") or "")
    if gex_regime == "negative_gamma" and direction in {"bullish", "bearish"}:
        score += 10
        reasons.append("负 GEX 顺风")
    elif gex_regime == "positive_gamma":
        score += 5
        reasons.append("正 GEX 但更偏均值回归")

    if direction_bias in {"bullish", "bearish"} and direction == "neutral":
        direction = direction_bias

    triggered = score >= 60 and direction in {"bullish", "bearish"}
    if not triggered:
        reasons.append("未满足硬触发阈值")
    return {
        "triggered": triggered,
        "score": round(score, 2),
        "direction": direction,
        "reasons": reasons,
        "orb_state": orb_state,
        "vwap_state": vwap_state,
        "ema_state": ema_state,
        "rvol": round(rvol, 2),
        "macd_state": macd_state,
    }


def _soft_single_leg_override(
    *,
    trigger: dict[str, Any],
    majority_direction: str,
    majority_strength: int,
    avg_execution: float,
    avg_spread: float,
    top_probability: float,
    top_reward_risk: float,
    blocker_count: int,
    allow_auto_trade: bool,
    should_trade: bool,
) -> bool:
    if not should_trade or not allow_auto_trade or blocker_count > 0:
        return False
    if not trigger.get("triggered") or str(trigger.get("direction") or "") not in {"bullish", "bearish"}:
        return False
    if _num(trigger.get("score")) < 65:
        return False
    if majority_direction not in {"neutral", str(trigger.get("direction") or "")}:
        return False
    if majority_strength < 3:
        return False
    if avg_execution < 75 or avg_spread > 8:
        return False
    return top_probability >= 35 or top_reward_risk >= 80


def _strategy_family_gates(
    *,
    regime: str,
    gex_regime: str,
    avg_iv: float,
    avg_execution: float,
    avg_spread: float,
    majority_direction: str,
    majority_strength: int,
    trigger: dict[str, Any],
    preferred_strategy_families: list[str],
    blocker_count: int,
    should_trade: bool,
    low_gate_enabled: bool,
) -> dict[str, dict[str, Any]]:
    preferred = set(preferred_strategy_families or [])
    trigger_direction = str(trigger.get("direction") or "neutral")
    trigger_score = _num(trigger.get("score"))
    trigger_active = bool(trigger.get("triggered"))
    directional = majority_direction in {"bullish", "bearish"} or trigger_direction in {"bullish", "bearish"}
    trend = regime in {"bull_trend", "bear_trend", "momentum"}
    range_bound = regime in {"range", "choppy"} or gex_regime == "positive_gamma"
    templates = strategy_template_catalog()

    gates: dict[str, dict[str, Any]] = {}
    for family in STRATEGY_FAMILIES:
        gates[family] = {"allowed": False, "reasons": [], "warnings": [], "blockers": [], "template": templates.get(family)}

    def allow(family: str, reason: str, warning: str = "") -> None:
        gates[family]["allowed"] = True
        gates[family]["reasons"].append(reason)
        if warning:
            gates[family]["warnings"].append(warning)

    def block(family: str, reason: str) -> None:
        gates[family]["allowed"] = False
        gates[family]["blockers"].append(reason)

    if not should_trade or blocker_count > 0:
        for family in STRATEGY_FAMILIES:
            block(family, "全局交易门禁未通过。")
        return gates
    execution_floor = 35 if low_gate_enabled else 45
    spread_floor = 40 if low_gate_enabled else 35
    credit_iv_floor = 35 if low_gate_enabled else 45
    vol_buy_iv_floor = 60 if low_gate_enabled else 65

    if avg_execution and avg_execution < execution_floor:
        for family in STRATEGY_FAMILIES:
            block(family, "候选执行质量偏低，不适合自动策略结构。")
        return gates
    if avg_spread > spread_floor:
        for family in STRATEGY_FAMILIES:
            block(family, "候选平均价差过宽，不适合策略结构自动执行。")
        return gates

    if directional:
        allow("spread", "方向票或触发信号明确，适合定义风险方向价差。")
    else:
        block("spread", "方向不明确，方向价差优先级降低。")

    if directional and avg_iv >= credit_iv_floor:
        allow("credit_spread", "方向存在且 IV 不低，信用价差有收租与定义风险基础。")
    elif range_bound and avg_iv >= credit_iv_floor:
        allow("credit_spread", "区间/正 GEX 环境且 IV 不低，可考虑价外信用价差。")
    else:
        block("credit_spread", "IV 或方向/区间条件不足，信用价差收租质量不够。")

    if (majority_direction == "neutral" and avg_iv >= vol_buy_iv_floor) or (gex_regime == "negative_gamma" and trigger_active and trigger_score >= 65):
        allow("straddle", "中性高 IV/事件波动或负 GEX 强触发，允许跨式捕捉大波动。")
        allow("strangle", "中性高 IV/事件波动或负 GEX 强触发，允许宽跨捕捉尾部波动。")
    else:
        block("straddle", "波动扩张条件不足，跨式容易被时间价值拖累。")
        block("strangle", "波动扩张条件不足，宽跨容易被时间价值拖累。")

    if range_bound and not (gex_regime == "negative_gamma" and trend):
        allow("calendar", "区间/正 GEX 或钉住环境更适合买远卖近的时间价差。")
    elif majority_direction == "neutral" and avg_iv < 65:
        allow("calendar", "方向中性且 IV 不过热，允许日历价差。")
    else:
        block("calendar", "趋势或负 GEX 动量过强，日历价差的钉住假设不足。")

    if directional and majority_direction != "neutral":
        allow("diagonal", "方向明确且可用近月短腿降低远月持仓成本。")
        allow("poor_mans_covered_call", "偏多方向明确，穷人备兑具备方向基础。")
    else:
        block("diagonal", "方向不足，对角价差短腿风险不划算。")
        block("poor_mans_covered_call", "偏多方向不足，穷人备兑不适合自动执行。")

    if range_bound and avg_iv >= credit_iv_floor and not (trigger_active and trigger_score >= 75):
        allow("iron_condor", "区间/正 GEX 且 IV 不低，允许铁鹰收取双边权利金。")
    else:
        block("iron_condor", "趋势触发或 IV 条件不满足，铁鹰区间假设不足。")

    if range_bound and not (gex_regime == "negative_gamma" and trend):
        allow("butterfly", "区间/钉住环境允许蝶式围绕中间行权价收敛。")
    else:
        block("butterfly", "趋势或负 GEX 动量环境不适合蝶式钉住假设。")

    if majority_direction != "bearish":
        allow("covered_call", "非明确看跌环境，备兑可在持股基础上收租。")
        allow("cash_secured_put", "非明确看跌环境，现金担保 Put 可在接股意愿下收租。")
    else:
        block("covered_call", "明确看跌环境下备兑无法保护主要下行风险。")
        block("cash_secured_put", "明确看跌环境下现金担保 Put 接股风险偏高。")

    if majority_direction in {"bearish", "neutral"} or regime in {"choppy", "range"}:
        allow("collar", "中性/看跌或震荡环境允许领式保护底仓。")
    else:
        block("collar", "强趋势偏多时领式会过早牺牲上行，自动执行优先级低。")

    for family in preferred:
        if family in gates and not gates[family]["allowed"] and not gates[family]["blockers"]:
            allow(family, "匹配当前环境的首选策略族。")
        elif family in gates and gates[family]["allowed"]:
            gates[family]["reasons"].append("同时匹配当前环境首选策略族。")
    for gate in gates.values():
        gate["reasons"] = list(dict.fromkeys(gate["reasons"]))
        gate["warnings"] = list(dict.fromkeys(gate["warnings"]))
        gate["blockers"] = list(dict.fromkeys(gate["blockers"]))
    return gates


def _preferred_strategy_families(regime: str, gex: dict[str, Any], avg_iv: float, majority_direction: str, trigger: dict[str, Any]) -> list[str]:
    gex_regime = str(gex.get("regime") or "")
    families: list[str] = []
    if regime in {"bull_trend", "bear_trend", "momentum"} and gex_regime == "negative_gamma":
        families.extend(["single_leg", "spread"])
    elif regime in {"bull_trend", "bear_trend", "momentum"} and avg_iv >= 65:
        families.extend(["spread", "credit_spread", "diagonal"])
    elif regime in {"range", "choppy"} and gex_regime == "positive_gamma":
        families.extend(["iron_condor", "butterfly", "calendar", "credit_spread"])
    elif avg_iv >= 70 and majority_direction == "neutral":
        families.extend(["straddle", "strangle"])
    elif majority_direction in {"bullish", "bearish"}:
        families.extend(["single_leg", "spread"])
    if majority_direction == "bullish" and avg_iv >= 55:
        families.extend(["cash_secured_put", "poor_mans_covered_call"])
    if majority_direction == "neutral" and avg_iv < 65:
        families.append("calendar")
    if majority_direction == "neutral" and avg_iv < 60 and gex_regime not in {"positive_gamma", "negative_gamma"}:
        return []
    if trigger.get("triggered") and trigger.get("direction") in {"bullish", "bearish"} and "single_leg" not in families:
        families.insert(0, "single_leg")
    return list(dict.fromkeys(families))


def _confidence(candidate_count: int, avg_execution: float, blocker_count: int, warning_count: int, gex_regime: str) -> float:
    value = 0.45
    value += min(candidate_count, 8) * 0.035
    value += min(max(avg_execution, 0), 100) / 500
    value += 0.05 if gex_regime in {"positive_gamma", "negative_gamma", "neutral"} else 0.0
    value -= blocker_count * 0.2
    value -= warning_count * 0.06
    return round(max(0.05, min(value, 0.95)), 2)


def _avg(values: Any) -> float:
    rows = [_num(value) for value in values]
    rows = [value for value in rows if value > 0]
    return sum(rows) / len(rows) if rows else 0.0


def _daily_change_pct(daily: dict[str, Any]) -> float:
    if "change_pct" in daily:
        return _num(daily.get("change_pct"))
    if "day_pct" in daily:
        return _num(daily.get("day_pct"))
    close = _num(daily.get("close"))
    prev = _num(daily.get("prev_close"))
    if close > 0 and prev > 0:
        return (close / prev - 1) * 100
    return 0.0


def _intraday_change_pct(intraday: dict[str, Any]) -> float:
    if "change_pct" in intraday:
        return _num(intraday.get("change_pct"))
    if "first_to_last_pct" in intraday:
        return _num(intraday.get("first_to_last_pct"))
    first = _num(intraday.get("first_price"))
    last = _num(intraday.get("last_price"))
    if first > 0 and last > 0:
        return (last / first - 1) * 100
    return 0.0


def _time_value_risk_penalty(candidate: dict[str, Any]) -> float:
    penalty = 0.0
    reasons: list[str] = []
    theta_pct = _num(candidate.get("theta_to_ask_pct"))
    days = _num(candidate.get("days_to_expiration"))
    ask = _num(candidate.get("ask"))
    breakeven_prob = _num(candidate.get("probability_breakeven"))
    now = now_et()
    minutes_to_close = max(0.0, (16 - now.hour) * 60 - now.minute)

    if days <= 0.5 and now.hour >= 12:
        penalty += 14.0
        reasons.append("0DTE 午后惩罚")
    if theta_pct > 25:
        penalty += min((theta_pct - 25) * 0.45, 14.0)
        reasons.append("theta/ask 偏高")
    if ask > 0 and ask <= 1.25 and breakeven_prob < 22:
        penalty += 7.0
        reasons.append("便宜彩票但平衡概率偏低")
    if days <= 1 and minutes_to_close <= 180:
        penalty += 8.0
        reasons.append("临近收盘的时间价值折损")
    return round(penalty, 2)


def _execution_hard_flags(candidate: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    ask = _num(candidate.get("ask"))
    bid = _num(candidate.get("bid"))
    spread_pct = _num(candidate.get("spread_pct"))
    volume = _num(candidate.get("volume"))
    open_interest = _num(candidate.get("open_interest"))
    pricing_source = str(candidate.get("pricing_source") or "")
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


def _candidate_row(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    return dict(getattr(item, "__dict__", {}) or {})


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

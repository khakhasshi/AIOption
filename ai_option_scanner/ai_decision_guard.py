from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from typing import Any


DECISION_TEMPERATURE = float(os.getenv("AI_OPTION_DECISION_TEMPERATURE", "0.05"))
JSON_RESPONSE_FORMAT = {"type": "json_object"}

REQUIRED_SINGLE_LEG_EVIDENCE = (
    "decision_score",
    "alpha_score",
    "execution_score",
    "risk_plan.max_loss_per_contract",
    "risk_plan.stop_loss_option_price",
    "risk_plan.take_profit_1",
    "risk_plan.take_profit_2",
    "risk_plan.latest_exit",
)


def extract_json_object(answer: str | None) -> dict[str, Any] | None:
    if not answer:
        return None
    text = str(answer).strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_escape_likely_unescaped_quotes(text[start : end + 1]))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _escape_likely_unescaped_quotes(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    length = len(text)
    for index, char in enumerate(text):
        if escaped:
            output.append(char)
            escaped = False
            continue
        if char == "\\" and in_string:
            output.append(char)
            escaped = True
            continue
        if char != '"':
            output.append(char)
            continue
        if not in_string:
            output.append(char)
            in_string = True
            continue
        next_index = index + 1
        while next_index < length and text[next_index].isspace():
            next_index += 1
        next_char = text[next_index] if next_index < length else ""
        if next_char in {":", ",", "}", "]", ""}:
            output.append(char)
            in_string = False
        else:
            output.append('\\"')
    return "".join(output)


def candidate_row(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if is_dataclass(item):
        return asdict(item)
    return dict(getattr(item, "__dict__", {}) or {})


def strategy_key(item: Any, index: int = 0) -> str:
    row = candidate_row(item)
    explicit = str(row.get("strategy_key") or "").strip()
    if explicit:
        return explicit
    parts = [
        row.get("family") or "strategy",
        row.get("strategy_type") or "type",
        row.get("expiration") or "exp",
        row.get("label") or index,
    ]
    return "::".join(str(part).replace(" ", "_") for part in parts)


def strategy_row_with_key(item: Any, index: int) -> dict[str, Any]:
    row = candidate_row(item)
    row["strategy_key"] = strategy_key(row, index)
    return row


def compact_candidates_for_ai(candidates: list[Any], limit: int = 20) -> list[dict[str, Any]]:
    rows = [candidate_row(item) for item in candidates[:limit]]
    return [
        {
            "contract_symbol": row.get("contract_symbol"),
            "side": row.get("side"),
            "expiration": row.get("expiration"),
            "strike": row.get("strike"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "spread_pct": row.get("spread_pct"),
            "volume": row.get("volume"),
            "open_interest": row.get("open_interest"),
            "implied_volatility": row.get("implied_volatility"),
            "rv20": row.get("rv20"),
            "rv60": row.get("rv60"),
            "iv_rv_ratio": row.get("iv_rv_ratio"),
            "iv_rv_premium_pct": row.get("iv_rv_premium_pct"),
            "iv_edge_state": row.get("iv_edge_state"),
            "event_risk_score": row.get("event_risk_score"),
            "event_risk_state": row.get("event_risk_state"),
            "delta": row.get("delta"),
            "gamma": row.get("gamma"),
            "theta_per_day": row.get("theta_per_day"),
            "theta_to_ask_pct": row.get("theta_to_ask_pct"),
            "analysis_score": row.get("analysis_score") or row.get("score"),
            "alpha_score": row.get("alpha_score"),
            "execution_score": row.get("execution_score") or row.get("execution_quality_score"),
            "decision_score": row.get("decision_score"),
            "decision_bucket": row.get("decision_bucket"),
            "trigger_score": row.get("trigger_score"),
            "trigger_state": row.get("trigger_state"),
            "trigger_reasons": row.get("trigger_reasons") or [],
            "execution_hard_flags": row.get("execution_hard_flags") or [],
            "time_value_risk_penalty": row.get("time_value_risk_penalty"),
            "reward_risk_score": row.get("reward_risk_score"),
            "volatility_score": row.get("volatility_score"),
            "market_structure_score": row.get("market_structure_score"),
            "volume_profile_state": row.get("volume_profile_state"),
            "volume_profile_position": row.get("volume_profile_position"),
            "volume_profile_poc": row.get("volume_profile_poc"),
            "volume_profile_value_area_high": row.get("volume_profile_value_area_high"),
            "volume_profile_value_area_low": row.get("volume_profile_value_area_low"),
            "market_structure_flags": row.get("market_structure_flags") or [],
            "probability_breakeven": row.get("probability_breakeven"),
            "strategy_tag": row.get("strategy_tag"),
            "gex_regime": row.get("gex_regime"),
            "gex_alignment": row.get("gex_alignment"),
            "risk_plan": row.get("risk_plan") or {},
        }
        for row in rows
    ]


def compact_strategies_for_ai(strategies: list[Any], limit: int = 20) -> list[dict[str, Any]]:
    rows = [strategy_row_with_key(item, index) for index, item in enumerate(strategies[:limit], start=1)]
    return [
        {
            "strategy_key": row.get("strategy_key"),
            "family": row.get("family"),
            "strategy_type": row.get("strategy_type"),
            "label": row.get("label"),
            "direction": row.get("direction"),
            "expiration": row.get("expiration"),
            "legs": row.get("legs") or [],
            "net_debit": row.get("net_debit"),
            "net_credit": row.get("net_credit"),
            "max_loss": row.get("max_loss"),
            "max_profit": row.get("max_profit"),
            "breakevens": row.get("breakevens") or [],
            "capital_required": row.get("capital_required"),
            "probability_hint": row.get("probability_hint"),
            "score": row.get("score"),
            "structure_fit_score": row.get("structure_fit_score"),
            "payoff_quality_score": row.get("payoff_quality_score"),
            "execution_complexity_score": row.get("execution_complexity_score"),
            "capital_efficiency_score": row.get("capital_efficiency_score"),
            "risk_defined_score": row.get("risk_defined_score"),
            "quote_consistency_score": row.get("quote_consistency_score"),
            "quote_consistency_state": row.get("quote_consistency_state"),
            "natural_exit": row.get("natural_exit") or {},
            "fit_notes": row.get("fit_notes") or [],
            "hard_flags": row.get("hard_flags") or [],
            "summary": row.get("summary"),
        }
        for row in rows
    ]


def build_strict_analysis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "instruction": "Return one JSON object only. Select only from option_candidates.contract_symbol or strategy_candidates.strategy_key. If intent.strategy_modes excludes single_leg, selection_type must be strategy and selected_strategy_key must reference strategy_candidates.",
        "time_context": payload.get("time_context") or {},
        "user_query": payload.get("user_query"),
        "symbol": payload.get("symbol"),
        "quote": payload.get("quote") or {},
        "technical_bias": payload.get("technical_bias"),
        "daily_summary": payload.get("daily_summary") or {},
        "intraday_summary": payload.get("intraday_summary") or {},
        "latest_news_titles": payload.get("latest_news_titles") or [],
        "latest_news_source": payload.get("latest_news_source"),
        "intent": payload.get("intent") or {},
        "decision_gate": payload.get("decision_gate") or {},
        "gex_context": payload.get("gex_context") or {},
        "volatility_context": payload.get("volatility_context") or {},
        "volume_profile": payload.get("volume_profile") or {},
        "intraday_option_tools": payload.get("intraday_option_tools") or {},
        "option_candidates": compact_candidates_for_ai(payload.get("option_candidates") or []),
        "strategy_candidates": compact_strategies_for_ai(payload.get("strategy_candidates") or []),
        "required_evidence_fields": list(REQUIRED_SINGLE_LEG_EVIDENCE),
    }


def validate_analysis_decision(raw: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    decision = raw if isinstance(raw, dict) else {}
    candidates = [candidate_row(item) for item in payload.get("option_candidates") or []]
    strategies = [strategy_row_with_key(item, index) for index, item in enumerate(payload.get("strategy_candidates") or [], start=1)]
    by_contract = {str(item.get("contract_symbol") or ""): item for item in candidates if item.get("contract_symbol")}
    by_strategy = {str(item.get("strategy_key") or ""): item for item in strategies if item.get("strategy_key")}
    gate = payload.get("decision_gate") or {}
    strategy_modes = _strategy_modes(payload)
    single_leg_allowed = "single_leg" in strategy_modes or not strategy_modes
    strategy_only = bool(strategy_modes) and any(mode != "single_leg" for mode in strategy_modes) and not single_leg_allowed
    errors: list[str] = []
    warnings: list[str] = []
    action = str(decision.get("action") or "observe").lower()
    if action not in {"trade", "observe", "no_trade"}:
        errors.append("action must be trade|observe|no_trade")
        action = "observe"
    selection_type = str(decision.get("selection_type") or "none").lower()
    if selection_type not in {"single_leg", "strategy", "none"}:
        errors.append("selection_type must be single_leg|strategy|none")
        selection_type = "none"

    selected_contract = str(decision.get("selected_contract_symbol") or "").strip()
    selected_strategy = str(decision.get("selected_strategy_key") or "").strip()
    selected_candidate = by_contract.get(selected_contract) if selected_contract else None
    selected_strategy_row = by_strategy.get(selected_strategy) if selected_strategy else None

    if gate.get("should_trade") is False and action == "trade":
        errors.append("decision_gate.should_trade=false forbids trade action")
    if gate.get("allow_auto_trade") is False and action == "trade":
        warnings.append("decision_gate.allow_auto_trade=false; execution must stay observe/limit-only")
    if selection_type == "single_leg":
        if strategy_only:
            errors.append("single_leg selection is not allowed by intent.strategy_modes")
        if not selected_candidate:
            errors.append("selected_contract_symbol must exist in option_candidates")
        else:
            for field in REQUIRED_SINGLE_LEG_EVIDENCE:
                if _field_missing(selected_candidate, field):
                    errors.append(f"missing evidence field: option_candidates[{selected_contract}].{field}")
            if str(selected_candidate.get("decision_bucket") or "") in {"observe_trigger_not_met", "blocked_execution"}:
                errors.append("selected single-leg candidate is not in a tradable decision bucket")
            if _num(selected_candidate.get("trigger_score")) < 60:
                errors.append("selected single-leg candidate trigger_score below tradable threshold")
            if selected_candidate.get("execution_hard_flags"):
                errors.append("selected single-leg candidate has execution_hard_flags")
    elif selection_type == "strategy":
        if not selected_strategy_row:
            errors.append("selected_strategy_key must exist in strategy_candidates")
        else:
            for field in ("score", "max_loss", "legs"):
                if _field_missing(selected_strategy_row, field):
                    errors.append(f"missing evidence field: strategy_candidates[{selected_strategy}].{field}")
            if any(flag in set(selected_strategy_row.get("hard_flags") or []) for flag in {"bad_long_ask", "short_leg_bid_unavailable", "net_price_inconsistent", "needs_stock_backing", "needs_cash_secured"}):
                errors.append("selected strategy has blocking hard_flags")
    elif action == "trade":
        errors.append("trade action requires a selected single_leg or strategy")

    evidence = decision.get("evidence")
    if not isinstance(evidence, list) or len(evidence) < (3 if action == "trade" else 1):
        errors.append("evidence must include field-bound references")
    else:
        for index, item in enumerate(evidence[:12]):
            if not isinstance(item, dict) or not item.get("field") or "value" not in item:
                errors.append(f"evidence[{index}] must include field and value")

    top_contract = _top_contract(candidates)
    if action == "trade" and selection_type == "single_leg" and selected_contract and top_contract and selected_contract != top_contract:
        warnings.append("AI selected contract differs from top decision_score candidate; execution should be observe unless manually approved")
        action = "observe"

    if errors:
        action = "observe" if candidates or strategies else "no_trade"

    return {
        "valid": not errors,
        "action": action,
        "selection_type": selection_type if not errors else "none" if not (selected_candidate or selected_strategy_row) else selection_type,
        "selected_contract_symbol": selected_contract if selected_candidate else "",
        "selected_strategy_key": selected_strategy if selected_strategy_row else "",
        "selected_candidate": selected_candidate,
        "selected_strategy": selected_strategy_row,
        "summary": str(decision.get("summary") or ""),
        "rationale": str(decision.get("rationale") or ""),
        "evidence": evidence if isinstance(evidence, list) else [],
        "risk": decision.get("risk") if isinstance(decision.get("risk"), dict) else {},
        "warnings": warnings + [str(item) for item in decision.get("warnings", []) if isinstance(item, str)][:8],
        "errors": errors,
        "top_ranked_contract": top_contract,
        "execution_allowed": not errors and action == "trade" and gate.get("should_trade") is not False and gate.get("allow_auto_trade") is not False,
        "raw_decision": decision,
    }


def validation_status(validation: dict[str, Any]) -> str:
    if validation.get("errors"):
        return "invalid_json_schema"
    if validation.get("action") != "trade":
        return "observe"
    if validation.get("warnings"):
        return "valid_with_warnings"
    return "valid"


def _top_contract(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return ""
    top = sorted(candidates, key=lambda item: _num(item.get("decision_score"), _num(item.get("analysis_score"), 0.0)), reverse=True)[0]
    return str(top.get("contract_symbol") or "")


def _strategy_modes(payload: dict[str, Any]) -> list[str]:
    raw = (payload.get("intent") or {}).get("strategy_modes") or payload.get("strategy_modes") or []
    if not isinstance(raw, list):
        return []
    return [str(item or "").strip().lower() for item in raw if str(item or "").strip()]


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _field_missing(row: dict[str, Any], dotted: str) -> bool:
    value: Any = row
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return True
        value = value.get(part)
    if value is None or value == "":
        return True
    if isinstance(value, (list, tuple, dict)) and not value:
        return True
    return False


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

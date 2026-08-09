from __future__ import annotations

from typing import Any


def evaluate_strategy_template(candidate: Any, gate: dict[str, Any] | None = None) -> dict[str, Any]:
    """Family-specific strategy gate for net price, width and regime fit."""
    family = str(_get(candidate, "family") or _get(candidate, "strategy_type") or "").strip().lower()
    template = _template_for_family(family)
    metrics = _metrics(candidate)
    gate = gate or {}
    blockers: list[str] = []
    warnings: list[str] = []
    reasons: list[str] = []

    if not template:
        return {
            "version": 1,
            "family": family,
            "template": "default",
            "allowed": True,
            "score_adjustment": 0.0,
            "metrics": metrics,
            "reasons": ["no dedicated template; fallback strategy gate applied"],
            "warnings": [],
            "blockers": [],
        }

    if gate:
        _check_environment(template, gate, blockers, warnings, reasons)
    else:
        reasons.append("environment gate unavailable; price template only")
    _check_net_price(template, metrics, blockers, warnings, reasons)
    _check_width(template, metrics, blockers, warnings, reasons)
    _check_quote_quality(template, metrics, blockers, warnings, reasons)
    _check_breakevens(template, metrics, blockers, warnings, reasons)

    allowed = not blockers
    score_adjustment = -28.0 if blockers else -7.0 * len(warnings) + 4.0 * min(len(reasons), 3)
    return {
        "version": 1,
        "family": family,
        "strategy_type": str(_get(candidate, "strategy_type") or ""),
        "template": template["name"],
        "allowed": allowed,
        "score_adjustment": round(score_adjustment, 2),
        "metrics": metrics,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "blockers": list(dict.fromkeys(blockers)),
    }


def strategy_template_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for family in ("calendar", "iron_condor", "straddle", "strangle"):
        template = _template_for_family(family) or {}
        catalog[family] = {
            "name": template.get("name") or family,
            "allowed_regimes": sorted(template.get("allowed_regimes") or []),
            "preferred_gex": sorted(template.get("preferred_gex") or []),
            "max_debit_to_spot_pct": template.get("max_debit_to_spot_pct"),
            "min_credit_to_width_pct": template.get("min_credit_to_width_pct"),
            "max_credit_to_width_pct": template.get("max_credit_to_width_pct"),
            "min_width": template.get("min_width"),
            "min_dte_gap_days": template.get("min_dte_gap_days"),
            "min_breakeven_span_pct": template.get("min_breakeven_span_pct"),
        }
    return catalog


def _template_for_family(family: str) -> dict[str, Any] | None:
    templates = {
        "calendar": {
            "name": "calendar_time_spread",
            "allowed_regimes": {"range", "choppy", "unclear"},
            "preferred_gex": {"positive_gamma", "neutral", "unknown"},
            "max_debit_to_spot_pct": 2.8,
            "max_avg_leg_spread_pct": 28.0,
            "min_dte_gap_days": 5,
            "block_negative_gamma_momentum": True,
        },
        "iron_condor": {
            "name": "iron_condor_range_credit",
            "allowed_regimes": {"range", "choppy", "unclear"},
            "preferred_gex": {"positive_gamma", "neutral", "unknown"},
            "min_credit_to_width_pct": 12.0,
            "max_credit_to_width_pct": 55.0,
            "min_width": 1.0,
            "max_avg_leg_spread_pct": 32.0,
            "block_strong_directional_trigger": True,
        },
        "straddle": {
            "name": "long_straddle_vol_expansion",
            "allowed_regimes": {"momentum", "unclear", "choppy"},
            "preferred_gex": {"negative_gamma", "unknown"},
            "max_debit_to_spot_pct": 6.5,
            "max_avg_leg_spread_pct": 24.0,
            "require_vol_or_trigger": True,
        },
        "strangle": {
            "name": "long_strangle_tail_vol",
            "allowed_regimes": {"momentum", "unclear", "choppy"},
            "preferred_gex": {"negative_gamma", "unknown"},
            "max_debit_to_spot_pct": 4.2,
            "max_avg_leg_spread_pct": 28.0,
            "min_breakeven_span_pct": 1.0,
            "require_vol_or_trigger": True,
        },
    }
    return templates.get(family)


def _check_environment(template: dict[str, Any], gate: dict[str, Any], blockers: list[str], warnings: list[str], reasons: list[str]) -> None:
    regime = str(gate.get("regime") or "unknown")
    gex = str((gate.get("gex") or {}).get("regime") or "unknown")
    trigger = gate.get("single_leg_trigger") or {}
    trigger_score = _num(trigger.get("score"))
    avg_iv = _num(gate.get("avg_iv_percentile"))
    if regime in template.get("allowed_regimes", set()):
        reasons.append(f"regime {regime} fits {template['name']}")
    else:
        warnings.append(f"regime {regime} is not ideal for {template['name']}")
    if gex in template.get("preferred_gex", set()):
        reasons.append(f"GEX {gex} fits template")
    elif gex == "negative_gamma" and template.get("block_negative_gamma_momentum") and regime == "momentum":
        blockers.append("negative_gamma_momentum_blocks_calendar")
    else:
        warnings.append(f"GEX {gex} is not preferred")
    if template.get("block_strong_directional_trigger") and trigger.get("triggered") and trigger_score >= 75:
        blockers.append("strong_directional_trigger_blocks_range_credit")
    if template.get("require_vol_or_trigger") and not (avg_iv >= 60 or (trigger.get("triggered") and trigger_score >= 65) or gex == "negative_gamma"):
        blockers.append("vol_expansion_or_strong_trigger_required")


def _check_net_price(template: dict[str, Any], metrics: dict[str, float], blockers: list[str], warnings: list[str], reasons: list[str]) -> None:
    debit = metrics["net_debit"]
    credit = metrics["net_credit"]
    spot = max(metrics["spot"], 1.0)
    width = metrics["width"]
    max_debit_pct = _num(template.get("max_debit_to_spot_pct"))
    if max_debit_pct and debit > 0:
        debit_pct = debit / spot * 100
        if debit_pct > max_debit_pct:
            blockers.append(f"net_debit_too_high:{debit_pct:.2f}%>{max_debit_pct:.2f}%")
        else:
            reasons.append(f"net debit {debit_pct:.2f}% of spot within template")
    min_credit_width = _num(template.get("min_credit_to_width_pct"))
    max_credit_width = _num(template.get("max_credit_to_width_pct"))
    if min_credit_width and width > 0:
        ratio = credit / width * 100 if credit > 0 else 0.0
        if ratio < min_credit_width:
            blockers.append(f"net_credit_too_low:{ratio:.2f}%<{min_credit_width:.2f}% width")
        elif max_credit_width and ratio > max_credit_width:
            warnings.append(f"net_credit_unusually_high:{ratio:.2f}% width")
        else:
            reasons.append(f"net credit {ratio:.2f}% of width within template")


def _check_width(template: dict[str, Any], metrics: dict[str, float], blockers: list[str], warnings: list[str], reasons: list[str]) -> None:
    width = metrics["width"]
    min_width = _num(template.get("min_width"))
    if min_width and width < min_width:
        blockers.append(f"width_too_narrow:{width:.2f}<{min_width:.2f}")
    elif min_width:
        reasons.append(f"width {width:.2f} passes minimum")
    min_dte_gap = _num(template.get("min_dte_gap_days"))
    if min_dte_gap:
        gap = metrics["dte_gap_days"]
        if gap and gap < min_dte_gap:
            blockers.append(f"expiration_gap_too_small:{gap:.0f}d<{min_dte_gap:.0f}d")
        elif gap:
            reasons.append(f"expiration gap {gap:.0f}d passes calendar template")


def _check_quote_quality(template: dict[str, Any], metrics: dict[str, float], blockers: list[str], warnings: list[str], reasons: list[str]) -> None:
    avg_spread = metrics["avg_leg_spread_pct"]
    max_spread = _num(template.get("max_avg_leg_spread_pct"))
    if max_spread and avg_spread > max_spread:
        blockers.append(f"avg_leg_spread_too_wide:{avg_spread:.2f}%>{max_spread:.2f}%")
    elif max_spread:
        reasons.append(f"avg leg spread {avg_spread:.2f}% within template")


def _check_breakevens(template: dict[str, Any], metrics: dict[str, float], blockers: list[str], warnings: list[str], reasons: list[str]) -> None:
    min_span = _num(template.get("min_breakeven_span_pct"))
    if not min_span:
        return
    span = metrics["breakeven_span_pct"]
    if span < min_span:
        warnings.append(f"breakeven_span_narrow:{span:.2f}%<{min_span:.2f}%")
    else:
        reasons.append(f"breakeven span {span:.2f}% is meaningful")


def _metrics(candidate: Any) -> dict[str, float]:
    legs = _get(candidate, "legs") or []
    spot = _infer_spot(legs, candidate)
    spreads = [_num((leg or {}).get("spread_pct")) for leg in legs if isinstance(leg, dict) and str(leg.get("side") or "") != "stock"]
    expirations = [str((leg or {}).get("expiration") or "") for leg in legs if isinstance(leg, dict) and (leg or {}).get("expiration")]
    dte_gap = _expiration_gap_days(expirations)
    breakevens = [_num(item) for item in (_get(candidate, "breakevens") or []) if _num(item) > 0]
    if len(breakevens) >= 2 and spot > 0:
        breakeven_span_pct = (max(breakevens) - min(breakevens)) / spot * 100
    else:
        breakeven_span_pct = 0.0
    return {
        "net_debit": _num(_get(candidate, "net_debit")),
        "net_credit": _num(_get(candidate, "net_credit")),
        "width": _num(_get(candidate, "width")),
        "spot": spot,
        "avg_leg_spread_pct": sum(spreads) / len(spreads) if spreads else 0.0,
        "dte_gap_days": dte_gap,
        "breakeven_span_pct": breakeven_span_pct,
        "max_loss": _num(_get(candidate, "max_loss")),
        "capital_required": _num(_get(candidate, "capital_required")),
    }


def _infer_spot(legs: list[Any], candidate: Any) -> float:
    strikes = [_num((leg or {}).get("strike")) for leg in legs if isinstance(leg, dict) and _num((leg or {}).get("strike")) > 0]
    breakevens = [_num(item) for item in (_get(candidate, "breakevens") or []) if _num(item) > 0]
    rows = breakevens or strikes
    return sum(rows) / len(rows) if rows else 1.0


def _expiration_gap_days(expirations: list[str]) -> float:
    if len(set(expirations)) < 2:
        return 0.0
    from datetime import date

    parsed = []
    for value in sorted(set(expirations)):
        try:
            parsed.append(date.fromisoformat(value))
        except ValueError:
            continue
    if len(parsed) < 2:
        return 0.0
    return float((parsed[-1] - parsed[0]).days)


def _get(candidate: Any, key: str) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(key)
    return getattr(candidate, key, None)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

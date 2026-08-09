"""Risk presets, per-session caps, and intraday-phase derivation for auto-trade.

A preset is a bundle of the EXISTING trading-config risk_* fields (so it flows
through the unchanged readiness gate + risk breakers in trading_agent), plus a
small set of auto-trade-only caps the loop enforces itself — because the per-run
``risk_max_daily_runs`` breaker is clamped to 20 and an every-N-minutes loop can
wake far more often than that, so over-trade protection must live in the loop.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .time_utils import parse_datetime

# Auto-trade-only caps the loop enforces (NOT the per-run trading breakers).
# order_cycles = cycles in a session that are allowed to PLACE an order;
# open_positions = max concurrent open auto-positions; capital_budget_pct =
# fraction of total_capital the loop may deploy across a whole session;
# max_daily_loss_pct / max_drawdown_pct = loss-based circuit breakers (fraction
# of total_capital). When today's net P&L breaches either, the loop forces the
# rest of the session to dry-run (analyze, never place) — conservative tightest.
_PRESET_CAPS = {
    "conservative": {"max_order_cycles_per_session": 4, "max_open_positions": 2, "session_capital_budget_pct": 0.30, "max_allocation_pct_per_trade": 0.20, "max_daily_loss_pct": 0.06, "max_drawdown_pct": 0.08},
    "balanced": {"max_order_cycles_per_session": 8, "max_open_positions": 3, "session_capital_budget_pct": 0.50, "max_allocation_pct_per_trade": 0.35, "max_daily_loss_pct": 0.10, "max_drawdown_pct": 0.15},
    "aggressive": {"max_order_cycles_per_session": 16, "max_open_positions": 5, "session_capital_budget_pct": 0.80, "max_allocation_pct_per_trade": 0.50, "max_daily_loss_pct": 0.18, "max_drawdown_pct": 0.25},
}

# Trading-config field bundles per preset (merged over the user's base config).
_PRESET_TRADING_FIELDS = {
    "conservative": {
        "entry_order_type": "limit",
        "software_stop_enabled": True,
        "software_take_profit_enabled": True,
        "risk_require_protection_for_market_order": True,
        "default_stop_loss_pct": 20.0,
        "default_take_profit_pct": 25.0,
        "risk_max_unprotected_quantity": 0,
        "risk_max_single_stop_loss_pct": 30.0,
        "low_gate_enabled": False,
        "max_per_symbol": 1,
        "top_n": 1,
    },
    "balanced": {
        "entry_order_type": "limit",
        "software_stop_enabled": True,
        "software_take_profit_enabled": True,
        "risk_require_protection_for_market_order": True,
        "default_stop_loss_pct": 25.0,
        "default_take_profit_pct": 35.0,
        "risk_max_unprotected_quantity": 0,
        "risk_max_single_stop_loss_pct": 45.0,
        "low_gate_enabled": False,
        "max_per_symbol": 1,
        "top_n": 2,
    },
    "aggressive": {
        "entry_order_type": "market",
        "software_stop_enabled": True,
        "software_take_profit_enabled": True,
        "risk_require_protection_for_market_order": True,
        "default_stop_loss_pct": 35.0,
        "default_take_profit_pct": 60.0,
        "risk_max_unprotected_quantity": 0,
        "risk_max_single_stop_loss_pct": 60.0,
        "low_gate_enabled": True,
        "max_per_symbol": 2,
        "top_n": 3,
    },
}


def preset_caps(preset: str) -> dict[str, Any]:
    return dict(_PRESET_CAPS.get(preset) or _PRESET_CAPS["conservative"])


def preset_trading_fields(preset: str) -> dict[str, Any]:
    return dict(_PRESET_TRADING_FIELDS.get(preset) or _PRESET_TRADING_FIELDS["conservative"])


def build_run_config(instance: dict[str, Any]) -> dict[str, Any]:
    """Build a trading_run config from an auto-trade instance + its preset.

    The result is a normal trading config (consumed unchanged by
    ``start_trading_run`` / ``validate_trading_readiness``). live_enabled is set
    only when the instance uses a real broker; dry-run instances keep it false
    so the loop simulates instead of submitting.
    """
    preset = str(instance.get("risk_preset") or "conservative")
    base = dict(instance.get("config") or {})
    fields = preset_trading_fields(preset)
    caps = preset_caps(preset)
    use_broker = bool(instance.get("use_broker"))
    # Per-instance capital, bounded to the session budget so a single cycle's run
    # can never size off more than the preset allows the loop to deploy in a day.
    total_capital = max(0.0, float(instance.get("total_capital") or 0.0))
    session_budget = round(total_capital * float(caps.get("session_capital_budget_pct") or 0.3), 2)
    config = {
        **fields,
        **base,  # user overrides win over preset defaults
        "live_enabled": use_broker,
        "use_ai": True,
        "total_capital": session_budget,
        # Let the LLM size positions + set per-trade stop/TP; the engine falls back
        # to config defaults (default_stop_loss_pct etc.) whenever the LLM omits or
        # returns an invalid value, so robustness is preserved.
        "ai_adjust_allocation": True,
        "ai_adjust_stop_loss": True,
        "ai_adjust_take_profit": True,
        # Robustness: always arm the deterministic pre-close flatten (no_overnight),
        # independent of what the LLM returns, so auto-positions never sit overnight.
        "force_no_overnight": True,
        "max_allocation_pct_per_trade": float(caps.get("max_allocation_pct_per_trade") or 0.5),
        "broker": instance.get("broker") or "longbridge",
        "broker_account": instance.get("broker_account") or "",
        "longbridge_account": instance.get("broker_account") or "" if (instance.get("broker") or "longbridge") == "longbridge" else "",
        "ai_provider": instance.get("ai_provider") or "deepseek",
        "universe": list(instance.get("symbols") or []),
        # The loop enforces its own cadence caps; keep the per-run daily breaker
        # generous so it doesn't fight the loop (loop caps are authoritative).
        "risk_max_daily_runs": 20,
        "single_instance_enabled": True,
        "multi_instance_enabled": False,
    }
    return config


def intraday_phase(env: dict[str, Any], *, now: datetime | None = None) -> str:
    """Derive a fine-grained intraday phase from market_environment.

    market_environment only distinguishes premarket/regular/afterhours, so we
    compute morning / lunch / afternoon / power-hour etc. from the position of
    now between the regular open and close. Returns a phase token the planner
    prompt injects so the LLM knows e.g. it's the open drive vs the close.
    """
    state = str(env.get("session_state") or "")
    if state == "premarket":
        return "premarket"
    if state in {"weekend", "holiday"}:
        return "closed"
    if state == "afterhours":
        return "afterhours"
    open_at = parse_datetime(env.get("regular_open_at_et"))
    close_at = parse_datetime(env.get("regular_close_at_et"))
    current = now or parse_datetime(env.get("now_et"))
    if not (open_at and close_at and current):
        return "regular"
    since_open = (current - open_at).total_seconds() / 60.0
    to_close = (close_at - current).total_seconds() / 60.0
    if since_open < 0:
        return "premarket"
    if to_close <= 0:
        return "afterhours"
    if to_close <= 15:
        return "pre_close"
    if to_close <= 60:
        return "power_hour"
    if since_open <= 30:
        return "just_opened"
    if since_open <= 120:
        return "morning"
    # Lunch lull roughly 11:30-13:30 ET ≈ 120-240 min after a 09:30 open.
    if 120 < since_open <= 240:
        return "lunch"
    return "afternoon"


def intraday_phase_label(phase: str) -> str:
    return {
        "premarket": "盘前 pre-market",
        "just_opened": "刚开盘 opening drive (first 30m)",
        "morning": "上午 morning trend",
        "lunch": "午盘 lunch lull",
        "afternoon": "下午 afternoon",
        "power_hour": "尾盘 power hour (last 60m)",
        "pre_close": "临近收盘 pre-close (last 15m)",
        "afterhours": "盘后 after-hours",
        "closed": "休市 closed",
        "regular": "盘中 regular session",
    }.get(phase, phase)

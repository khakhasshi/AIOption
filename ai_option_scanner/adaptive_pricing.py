"""Pure pricing helpers for adaptive ("smart") limit orders.

An adaptive order is a limit order priced BETWEEN the mid and the opposite
touch: buy at ``mid + aggr*half_spread``, sell at ``mid - aggr*half_spread``
(``aggr`` in [0, 1]; 0 = passive mid, 1 = marketable at ask/bid). It saves the
half-spread the plain "limit" path always pays (that path prices at ask/bid =
the opposite touch), while a reprice/escalation walk raises ``aggr`` toward 1.0
so a resting order still crosses the market before we give up.

This module is intentionally self-contained (no imports from trading_agent or
trading_monitor) so BOTH the entry path (trading_agent) and the exit path
(trading_monitor) can share ONE source of truth for the pricing math without
creating a cross-module dependency or drifting duplicate copies of financial
logic.

The order TYPE is chosen per-instance in the UI (config ``entry_order_type`` /
``exit_order_type`` ∈ {market, limit, adaptive}) — that choice is authoritative.
The env flags below are NOT the on-switch; they are a global KILL-SWITCH: they
default to ALLOW and only disable adaptive behavior when explicitly set to a
falsy value (0/false/no/off). This lets an operator hard-stop adaptive pricing
fleet-wide without touching every instance's config, while leaving the normal
decision to the per-instance order type.
"""
from __future__ import annotations

import math
import os
from typing import Any

ORDER_TYPES = ("market", "limit", "adaptive")


def coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_order_type(value: Any) -> str:
    """Canonicalize an order-type string to one of market/limit/adaptive.

    Single source of truth for both entry and exit order types; the agent and
    store normalizers delegate here. Aliases smart/mid/adaptive_limit → adaptive
    and lo → limit; anything unrecognized (incl. None) → market (the safe,
    behavior-unchanged default).
    """
    normalized = str(value or "market").strip().lower()
    if normalized in {"adaptive", "smart", "mid", "adaptive_limit"}:
        return "adaptive"
    if normalized in {"limit", "lo"}:
        return "limit"
    return "market"


def _kill_switch_allows(env_name: str) -> bool:
    """A global kill-switch env var: ALLOW by default, disable only if set falsy.

    Returns False only when the var is explicitly set to 0/false/no/off; an
    unset or empty var (the fleet's normal state) or any truthy value returns
    True. This inverts the old "off unless set" gate so the per-instance UI
    order-type choice is authoritative, while preserving a hard global stop.
    """
    raw = os.getenv(env_name)
    if raw is None:
        return True
    token = raw.strip().lower()
    if token == "":
        return True
    return token not in {"0", "false", "no", "off"}


def adaptive_order_enabled() -> bool:
    """Global kill-switch for adaptive ENTRY pricing (allow-by-default).

    Not the on-switch — the per-instance ``entry_order_type=="adaptive"`` choice
    is. Returns False only when AI_OPTION_ADAPTIVE_ORDER_ENABLED is explicitly
    set falsy (operator hard-stop); otherwise True.
    """
    return _kill_switch_allows("AI_OPTION_ADAPTIVE_ORDER_ENABLED")


def adaptive_exit_enabled() -> bool:
    """Global kill-switch for adaptive EXIT pricing (allow-by-default).

    Not the on-switch — the per-instance ``exit_order_type`` choice is (see
    ``adaptive_exit_decision``'s ``mode``). Both the master
    AI_OPTION_ADAPTIVE_ORDER_ENABLED and the exit-specific
    AI_OPTION_ADAPTIVE_EXIT_ENABLED must NOT be explicitly disabled. Software
    stops and short covers are never adaptive regardless (capital protection /
    naked-short safety) — those callers force market independently of this flag.
    """
    if not adaptive_order_enabled():
        return False
    return _kill_switch_allows("AI_OPTION_ADAPTIVE_EXIT_ENABLED")


def adaptive_exit_max_cycles() -> int:
    """Monitor cycles a resting adaptive exit-limit walks before market fallback.

    Each monitor cycle that a take-profit / long-leg exit limit sits unfilled we
    cancel + reprice more aggressively; after this many cycles we submit a market
    order so the exit is GUARANTEED (a resting limit must never strand a position
    that already hit its target). Clamped to [1, 10]; default 3.
    """
    raw = int(coerce_float(os.getenv("AI_OPTION_ADAPTIVE_EXIT_MAX_CYCLES"), 3))
    return max(1, min(raw, 10))


def adaptive_aggr_start() -> float:
    # How far the FIRST attempt sits from mid toward the opposite touch (0=mid,
    # 1=touch). Small by default so the opening try is cheap; the walk escalates
    # from here to 1.0 (marketable) on the final attempt.
    return max(0.0, min(coerce_float(os.getenv("AI_OPTION_ADAPTIVE_AGGR_START"), 0.3), 1.0))


def adaptive_aggr_for_attempt(attempt_idx: int, max_attempts: int) -> float:
    """Aggressiveness for attempt ``attempt_idx`` (0-based).

    Walks linearly from ``adaptive_aggr_start()`` on the first attempt to 1.0
    (marketable, at the opposite touch) on the last, so an order that did not
    fill passively is guaranteed to be crossing the market by the final try.
    With a single attempt it collapses to the start value.
    """
    start = adaptive_aggr_start()
    if max_attempts <= 1 or attempt_idx <= 0:
        return start if attempt_idx <= 0 else 1.0
    if attempt_idx >= max_attempts - 1:
        return 1.0
    step = (1.0 - start) / (max_attempts - 1)
    return max(0.0, min(start + step * attempt_idx, 1.0))


def round_to_tick(price: float, action: str) -> float:
    """Round ``price`` to a valid US option tick, in the aggressive direction.

    US option ticks: $0.01 below $3.00, $0.05 at/above $3.00 (Penny Pilot). We
    round a BUY up and a SELL down so tick-rounding never turns a marketable
    price into a passive one (which would silently fail to fill). Longbridge's
    submit path only formats to 2 decimals and its docs don't state whether an
    off-tick price is rejected or adjusted, so we normalize defensively here.
    """
    if price <= 0:
        return 0.0
    tick = 0.01 if price < 3.0 else 0.05
    units = price / tick
    if str(action or "").lower() == "buy":
        rounded_units = math.ceil(units - 1e-9)
    else:
        rounded_units = math.floor(units + 1e-9)
    rounded = rounded_units * tick
    # A $2.99->$3.00 buy crosses into the $0.05 band; re-round so the result is
    # itself a legal tick in its own band.
    if rounded >= 3.0 and tick == 0.01:
        return round_to_tick(rounded, action)
    return round(max(rounded, 0.01), 2)


def quote_touch_price(quote_row: dict[str, Any], action: str) -> float:
    """Conservative single-sided touch: ask for a buy, bid for a sell.

    Mirrors trading_agent._strategy_quote_leg_price so adaptive can fall back to
    exactly the price the plain "limit" path would use when a quote is not
    trustworthy — making adaptive NEVER worse than plain limit.
    """
    if not quote_row.get("available"):
        return 0.0
    keys = ("ask", "limit_price", "last_price", "last") if action == "buy" else ("bid", "last_price", "last", "limit_price")
    for key in keys:
        price = coerce_float(quote_row.get(key), 0.0)
        if price > 0:
            return price
    return 0.0


def quote_is_trustworthy(quote_row: dict[str, Any]) -> bool:
    """True when both sides of the quote look internally consistent.

    Rejects the bad ticks that drove the instant-loss unwinds (a bid that
    collapsed to a fraction of mid while the ask stayed put): non-positive
    bid/ask, a crossed book (bid > ask), or a spread wider than
    AI_OPTION_STRATEGY_MAX_QUOTE_SPREAD_PCT % of mid. Mirrors
    trading_agent._strategy_quote_is_trustworthy.
    """
    if not quote_row.get("available"):
        return False
    bid = coerce_float(quote_row.get("bid"), 0.0)
    ask = coerce_float(quote_row.get("ask"), 0.0)
    if bid <= 0 or ask <= 0:
        return False
    if bid > ask:
        return False
    mid = (bid + ask) / 2
    if mid <= 0:
        return False
    max_pct = max(10.0, min(coerce_float(os.getenv("AI_OPTION_STRATEGY_MAX_QUOTE_SPREAD_PCT"), 100.0), 500.0))
    return (ask - bid) <= mid * (max_pct / 100)


def adaptive_limit_price(quote_row: dict[str, Any], action: str, aggr: float) -> float:
    """Adaptive limit price for ``action`` at aggressiveness ``aggr`` in [0,1].

    buy  = mid + aggr*half_spread  (aggr=1 -> ask)
    sell = mid - aggr*half_spread  (aggr=1 -> bid)

    Falls back to the conservative single-sided touch when the quote is
    untrustworthy (one-sided, crossed, or absurdly wide) — so adaptive is NEVER
    worse than the plain limit path. Result is tick-rounded (aggressive side).
    """
    conservative = quote_touch_price(quote_row, action)
    if not quote_is_trustworthy(quote_row):
        return round_to_tick(conservative, action) if conservative > 0 else 0.0
    bid = coerce_float(quote_row.get("bid"), 0.0)
    ask = coerce_float(quote_row.get("ask"), 0.0)
    mid = (bid + ask) / 2
    half_spread = (ask - bid) / 2
    clamped = max(0.0, min(aggr, 1.0))
    if str(action or "").lower() == "buy":
        price = mid + clamped * half_spread
    else:
        price = mid - clamped * half_spread
    if price <= 0:
        return round_to_tick(conservative, action) if conservative > 0 else 0.0
    return round_to_tick(price, action)


def adaptive_exit_decision(raw_quote: dict[str, Any], action: str, cycle: int, mode: str = "adaptive") -> tuple[float, bool]:
    """Decide how to submit an EXIT close for monitor cycle ``cycle``.

    ``mode`` is the per-instance exit order type (market/limit/adaptive):
    - ``"market"`` → always ``(0.0, True)``: submit a plain market order. This is
      the behavior-unchanged default and what software stops / short covers use.
    - ``"limit"`` → a MARKETABLE limit at the opposite touch every cycle
      (sell@bid / buy@ask, aggr=1.0): caps against a bad print but fills nearly
      immediately in a liquid book; still falls to market at the walk's end.
    - ``"adaptive"`` → a mid-ward limit that WALKS toward the touch as ``cycle``
      grows (cycle 0 = most passive / most spread saved; final cycle marketable).

    Returns ``(limit_price, use_market)``:
    - ``use_market=True`` (limit_price 0) means submit a plain market order — the
      guaranteed-fill fallback. Used for ``mode=="market"``, when adaptive mode's
      global kill-switch is disabled, when the escalation walk is exhausted
      (``cycle >= max_cycles``), or when the quote is untrustworthy/unavailable.
    - ``use_market=False`` means submit a LIMIT at ``limit_price``.

    ``raw_quote`` must carry bid/ask (the monitor's quote_row["raw"], NOT the
    top-level quote_row which only has exit_price). A resting exit-limit that
    never fills would strand a position that already hit its target, so we ALWAYS
    fall to market at the walk's end — an exit is guaranteed, spread-saving is
    best-effort.
    """
    mode = normalize_order_type(mode)
    if mode == "market":
        return 0.0, True
    if mode == "adaptive" and not adaptive_exit_enabled():
        return 0.0, True
    max_cycles = adaptive_exit_max_cycles()
    if cycle >= max_cycles:
        return 0.0, True
    if not quote_is_trustworthy(raw_quote):
        return 0.0, True
    # "limit" is a marketable limit at the touch every cycle (aggr=1.0); only
    # "adaptive" walks from passive toward the touch.
    aggr = 1.0 if mode == "limit" else adaptive_aggr_for_attempt(cycle, max_cycles + 1)
    price = adaptive_limit_price(raw_quote, action, aggr)
    if price <= 0:
        return 0.0, True
    return price, False

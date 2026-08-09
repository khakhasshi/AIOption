"""Read-only signal aggregators feeding the auto-trade decision loop.

Everything here is best-effort and FAIL-SOFT: on any error or missing data the
helper returns a neutral/empty result and the loop proceeds on its existing
deterministic safeguards. No function here may raise into a cycle.

Four signals:
- ``daily_pnl_for_instance`` — today's realized (closed-run reviews) + floating
  (open-run estimated) P&L, used by the loss circuit breaker (Feature B).
- ``track_record_summary`` — win-rate / confidence calibration / recent lessons,
  fed back into the prompt so the LLM learns from outcomes (Feature A).
- ``portfolio_exposure`` — net/gross delta + per-symbol/direction concentration
  across the owner's open runs (Feature C).
- ``macro_regime`` — VIX volatility regime + earnings proximity + aggregated
  IV/RV + event-risk state (Feature D).
"""
from __future__ import annotations

import os
from typing import Any

from .account_store import normalize_owner_id
from .time_utils import now_et
from .ttl_cache import TTLCache

# VIX + earnings are slow-moving; cache across cycles/instances to avoid a
# yfinance round-trip every wake.
_MACRO_TTL_SECONDS = float(os.getenv("AI_OPTION_AUTO_TRADE_MACRO_TTL_SECONDS") or 600)
_vix_cache: TTLCache[dict[str, Any]] = TTLCache(_MACRO_TTL_SECONDS, maxsize=4, namespace="auto_trade_vix")
_earnings_cache: TTLCache[dict[str, Any]] = TTLCache(_MACRO_TTL_SECONDS, maxsize=256, namespace="auto_trade_earnings")


def _num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


# --------------------------------------------------------------------------- #
# Feature B — daily P&L (realized + floating) for the loss circuit breaker.
# --------------------------------------------------------------------------- #

def daily_pnl_for_instance(owner_id: str, cycles: list[dict[str, Any]], session_date_et: str) -> dict[str, Any]:
    """Net P&L booked by an instance *today*.

    ``cycles`` are this instance's cycle rows for the current trading day (as the
    caller filters them). For each linked run we sum realized P&L (closed →
    ``get_trade_review``) or floating P&L (open → run review_metrics).

    Returns ``{realized, floating, total, run_count, sample}``. Fail-soft: any
    unreadable run is skipped; a total of 0.0 is returned if nothing is found.
    """
    owner_id = normalize_owner_id(owner_id)
    run_ids: list[str] = []
    for cycle in cycles or []:
        for rid in cycle.get("run_ids") or []:
            if rid and rid not in run_ids:
                run_ids.append(str(rid))
    if not run_ids:
        return {"realized": 0.0, "floating": 0.0, "total": 0.0, "run_count": 0, "sample": []}

    realized = 0.0
    floating = 0.0
    sample: list[dict[str, Any]] = []
    try:
        from .trade_review_store import get_trade_review
    except Exception:  # noqa: BLE001
        get_trade_review = None  # type: ignore[assignment]
    try:
        from .trading_store import get_trading_run
    except Exception:  # noqa: BLE001
        get_trading_run = None  # type: ignore[assignment]

    for rid in run_ids:
        booked = None
        # Prefer the closed-run realized P&L from the post-mortem store.
        if get_trade_review is not None:
            try:
                review = get_trade_review(rid, owner_id)
            except Exception:  # noqa: BLE001
                review = None
            if review:
                value = _num(review.get("realized_pnl"))
                if value is not None:
                    realized += value
                    booked = {"run_id": rid, "realized_pnl": value, "source": "review"}
        # Still open (or no review yet): use the run's floating estimate.
        if booked is None and get_trading_run is not None:
            try:
                run = get_trading_run(rid, owner_id, light=True)
            except Exception:  # noqa: BLE001
                run = None
            metrics = ((run or {}).get("trade_instance") or {}).get("review_metrics") or {}
            value = _num(metrics.get("estimated_total_pnl"))
            if value is None:
                value = _num(metrics.get("realized_pnl"))
            if value is not None:
                floating += value
                booked = {"run_id": rid, "estimated_total_pnl": value, "source": "open_run"}
        if booked is not None:
            sample.append(booked)

    return {
        "realized": round(realized, 2),
        "floating": round(floating, 2),
        "total": round(realized + floating, 2),
        "run_count": len(run_ids),
        "sample": sample[:8],
    }


# --------------------------------------------------------------------------- #
# Feature A — track record (win-rate / calibration / recent lessons).
# --------------------------------------------------------------------------- #

def track_record_summary(owner_id: str, *, limit: int = 40, lessons: int = 3) -> dict[str, Any]:
    """Compact self-history for the decision prompt. Fail-soft → empty dict."""
    owner_id = normalize_owner_id(owner_id)
    out: dict[str, Any] = {}
    try:
        from .trading_quality import ai_decision_quality

        quality = ai_decision_quality(owner_id, limit) or {}
        if int(quality.get("sample_size") or 0) > 0:
            out["sample_size"] = int(quality.get("sample_size") or 0)
            out["win_rate"] = quality.get("win_rate")
            out["avg_confidence"] = quality.get("avg_confidence")
            out["avg_return_pct"] = quality.get("avg_return_pct")
            out["avg_confidence_vs_return"] = quality.get("avg_confidence_vs_return")
    except Exception:  # noqa: BLE001
        pass

    recent_lessons: list[str] = []
    try:
        from .trade_review_store import list_recent_trade_reviews

        reviews = list_recent_trade_reviews(owner_id, limit=12, statuses=("completed",)) or []
        for review in reviews:
            body = review.get("review") if isinstance(review.get("review"), dict) else {}
            for lesson in (body.get("lessons") or [])[:2]:
                text = str(lesson or "").strip()
                if text and text not in recent_lessons:
                    recent_lessons.append(text)
                if len(recent_lessons) >= lessons:
                    break
            if len(recent_lessons) >= lessons:
                break
    except Exception:  # noqa: BLE001
        pass
    if recent_lessons:
        out["recent_lessons"] = recent_lessons
    return out


# --------------------------------------------------------------------------- #
# Feature C — portfolio net-delta + concentration across open runs.
# --------------------------------------------------------------------------- #

def _signed_delta(order: dict[str, Any]) -> float | None:
    """Position delta signed by direction (long option → +, short → -)."""
    candidate = order.get("candidate") if isinstance(order.get("candidate"), dict) else {}
    delta = _num(order.get("delta"))
    if delta is None:
        delta = _num(candidate.get("delta"))
    if delta is None:
        greeks = candidate.get("greeks") if isinstance(candidate.get("greeks"), dict) else {}
        delta = _num(greeks.get("delta"))
    if delta is None:
        return None
    side = str(order.get("side") or order.get("action") or candidate.get("side") or "").strip().lower()
    qty = _num(order.get("entry_filled_quantity")) or _num(order.get("quantity")) or 1.0
    sign = -1.0 if side in {"sell", "short", "sell_to_open", "write"} else 1.0
    return delta * sign * abs(qty)


def _position_symbol(order: dict[str, Any]) -> str:
    candidate = order.get("candidate") if isinstance(order.get("candidate"), dict) else {}
    for key in ("symbol", "underlying", "underlying_symbol"):
        value = order.get(key) or candidate.get(key)
        if value:
            return str(value).strip().upper()
    contract = str(order.get("contract_symbol") or candidate.get("contract_symbol") or "").strip().upper()
    if contract:
        # Option OCC-ish symbols start with the root ticker (letters).
        root = "".join(ch for ch in contract if ch.isalpha())
        return root[:6] or contract
    return "?"


def portfolio_exposure(owner_id: str) -> dict[str, Any]:
    """Net/gross delta + per-symbol & directional concentration across the
    owner's currently-open runs. Fail-soft → ``{available: False}``."""
    owner_id = normalize_owner_id(owner_id)
    try:
        from .trading_store import list_monitorable_trading_runs
    except Exception:  # noqa: BLE001
        return {"available": False}
    try:
        runs = list_monitorable_trading_runs(200) or []
    except Exception:  # noqa: BLE001
        return {"available": False}

    net_delta = 0.0
    gross_delta = 0.0
    per_symbol: dict[str, float] = {}
    long_count = 0
    short_count = 0
    position_count = 0
    for run in runs:
        if normalize_owner_id(run.get("owner_id")) != owner_id:
            continue
        for order in run.get("orders") or []:
            if not isinstance(order, dict):
                continue
            signed = _signed_delta(order)
            if signed is None:
                continue
            position_count += 1
            net_delta += signed
            gross_delta += abs(signed)
            symbol = _position_symbol(order)
            per_symbol[symbol] = per_symbol.get(symbol, 0.0) + signed
            if signed >= 0:
                long_count += 1
            else:
                short_count += 1

    if position_count == 0:
        return {"available": False, "open_positions": 0}

    top = sorted(per_symbol.items(), key=lambda kv: abs(kv[1]), reverse=True)[:4]
    return {
        "available": True,
        "open_positions": position_count,
        "net_delta": round(net_delta, 3),
        "gross_delta": round(gross_delta, 3),
        "long_count": long_count,
        "short_count": short_count,
        "top_symbols": [{"symbol": sym, "net_delta": round(val, 3)} for sym, val in top],
    }


# --------------------------------------------------------------------------- #
# Feature D — macro / regime (VIX + earnings proximity + IV/RV + event risk).
# --------------------------------------------------------------------------- #

def _vix_regime() -> dict[str, Any]:
    def _fetch() -> dict[str, Any]:
        try:
            import yfinance as yf

            from .concurrency import yfinance_limiter

            with yfinance_limiter.acquire():
                frame = yf.Ticker("^VIX").history(period="1mo", interval="1d", auto_adjust=False)
        except Exception:  # noqa: BLE001
            return {"available": False}
        try:
            closes = [float(c) for c in list(frame["Close"]) if c == c and float(c) > 0]
        except Exception:  # noqa: BLE001
            return {"available": False}
        if not closes:
            return {"available": False}
        latest = closes[-1]
        window = closes[-10:] if len(closes) >= 3 else closes
        sma = sum(window) / len(window)
        if latest < 15:
            regime = "calm"
        elif latest < 20:
            regime = "normal"
        elif latest < 28:
            regime = "elevated"
        else:
            regime = "stressed"
        return {
            "available": True,
            "vix": round(latest, 2),
            "sma10": round(sma, 2),
            "rising": latest > sma,
            "regime": regime,
        }

    try:
        return _vix_cache.get_or_set("vix", _fetch)
    except Exception:  # noqa: BLE001
        return {"available": False}


def _earnings_days_away(symbol: str) -> int | None:
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return None

    def _fetch() -> dict[str, Any]:
        try:
            import yfinance as yf

            from .concurrency import yfinance_limiter

            with yfinance_limiter.acquire():
                ticker = yf.Ticker(symbol)
                next_dt = None
                # Newer yfinance: get_earnings_dates(); older: .calendar.
                try:
                    frame = ticker.get_earnings_dates(limit=8)
                    if frame is not None and len(frame) > 0:
                        from datetime import datetime, timezone

                        now = datetime.now(timezone.utc)
                        future = [idx for idx in list(frame.index) if idx.to_pydatetime().astimezone(timezone.utc) >= now]
                        if future:
                            next_dt = min(future).to_pydatetime()
                except Exception:  # noqa: BLE001
                    next_dt = None
                if next_dt is None:
                    cal = getattr(ticker, "calendar", None)
                    value = None
                    if isinstance(cal, dict):
                        value = cal.get("Earnings Date")
                        if isinstance(value, (list, tuple)) and value:
                            value = value[0]
                    if value is not None:
                        from datetime import datetime

                        next_dt = value if hasattr(value, "year") else datetime.fromisoformat(str(value))
        except Exception:  # noqa: BLE001
            return {"days": None}
        if next_dt is None:
            return {"days": None}
        try:
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc)
            target = next_dt.astimezone(timezone.utc) if next_dt.tzinfo else next_dt.replace(tzinfo=timezone.utc)
            return {"days": max(0, (target - now).days)}
        except Exception:  # noqa: BLE001
            return {"days": None}

    try:
        result = _earnings_cache.get_or_set(symbol, _fetch)
    except Exception:  # noqa: BLE001
        return None
    days = result.get("days") if isinstance(result, dict) else None
    return int(days) if isinstance(days, int) else None


def macro_regime(symbols: list[str], *, earnings_horizon_days: int = 7) -> dict[str, Any]:
    """VIX regime + symbols with earnings inside the horizon. Fail-soft per part."""
    out: dict[str, Any] = {"vix": _vix_regime()}
    soon: list[dict[str, Any]] = []
    for symbol in list(symbols or [])[:8]:
        days = _earnings_days_away(symbol)
        if days is not None and days <= earnings_horizon_days:
            soon.append({"symbol": str(symbol).strip().upper(), "days": days})
    if soon:
        out["earnings_soon"] = sorted(soon, key=lambda item: item["days"])
    return out

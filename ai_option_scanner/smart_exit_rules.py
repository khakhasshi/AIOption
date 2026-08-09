from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .time_utils import EASTERN, parse_datetime


def _trading_day_before(d: "date") -> "date":
    """The NYSE trading day strictly before ``d`` (skips weekends/holidays).

    "Day before expiry" must be a real trading day: a naive ``- 1 day`` on a
    Monday expiry yields Sunday (market closed, already past), which made the
    time-exit fire immediately / at the next open. Use the calendar instead.
    """
    from .market_calendar import previous_nyse_trading_day
    return previous_nyse_trading_day(d - timedelta(days=1))


UnderlyingQuoteFn = Callable[[str], dict[str, Any]]


def _hhmm_to_minutes(value: Any) -> int | None:
    """Parse "HH:MM" (or "H:MM") into minutes since midnight, else None.

    Used so time-of-day thresholds compare numerically, never lexicographically
    — "9:30" and "09:30" must both mean 570 minutes."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def normalize_exit_rules(
    *,
    raw_conditions: Any = None,
    latest_exit: str = "",
    invalidation: str = "",
    allow_overnight: bool | None = None,
    position: dict[str, Any] | None = None,
    default_no_overnight_time: str = "15:50",
) -> list[dict[str, Any]]:
    """Normalize AI and system exit inputs into one executable rule list."""
    position = position or {}
    rules: list[dict[str, Any]] = []
    if isinstance(raw_conditions, list):
        for item in raw_conditions:
            normalized = _normalize_condition(item, position)
            if normalized:
                rules.append(normalized)
    latest_exit = str(latest_exit or position.get("latest_exit") or "").strip()
    if latest_exit:
        exit_at = normalize_latest_exit(latest_exit, position)
        rules.append({"type": "time_exit", "exit_at": exit_at or latest_exit, "reason": latest_exit})
    invalidation = str(invalidation or position.get("invalidation") or position.get("underlying_invalidation") or "").strip()
    if invalidation:
        parsed = infer_invalidation_rule(invalidation, position)
        if parsed:
            rules.append(parsed)
    if allow_overnight is False:
        rules.append({"type": "no_overnight", "time_et": default_no_overnight_time, "reason": "计划禁止隔夜"})
    return _dedupe_rules(rules)


def evaluate_exit_rules(
    *,
    rules: list[dict[str, Any]],
    position: dict[str, Any],
    account_name: str = "",
    current_price: float = 0.0,
    entry_price: float = 0.0,
    current_pnl: float = 0.0,
    best_pnl: float = 0.0,
    underlying_quote: UnderlyingQuoteFn | None = None,
    option_quote: dict[str, Any] | None = None,
    now: datetime | None = None,
    position_opened_at: datetime | None = None,
) -> dict[str, Any] | None:
    now_et = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    current_time_et = now_et.strftime("%H:%M")
    best_pnl = max(float(best_pnl or current_pnl or 0), float(current_pnl or 0))
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get("type") or "").strip().lower()
        reason = str(rule.get("reason") or "").strip()
        if rule_type == "time_exit":
            exit_at = parse_exit_at(rule.get("exit_at"), position, now_et=now_et)
            if exit_at and now_et >= exit_at:
                # Skip stale time_exit: if the deadline already passed before the position was
                # even opened, the condition is meaningless (e.g. AI set "day before expiry"
                # for a 0DTE option — that date is already yesterday).
                if position_opened_at is not None:
                    opened_et = position_opened_at.astimezone(EASTERN)
                    if exit_at <= opened_et:
                        logging.getLogger(__name__).warning(
                            "smart_exit: skipping stale time_exit (exit_at=%s < opened_at=%s) reason=%r",
                            exit_at.isoformat(), opened_et.isoformat(), reason,
                        )
                        continue
                return _trigger("smart_time_exit", 0.0, exit_at.isoformat(), reason or "达到计划最晚退出时间")
        elif rule_type == "no_overnight":
            time_et = str(rule.get("time_et") or "15:50").strip() or "15:50"
            # Compare as minutes-since-midnight, NOT lexicographically: a string
            # compare like "10:00" >= "9:30" is False (because "1" < "9"), so an
            # unpadded single-digit-hour threshold would NEVER fire and the
            # position carries overnight unprotected.
            now_minutes = _hhmm_to_minutes(current_time_et)
            cutoff_minutes = _hhmm_to_minutes(time_et)
            if now_minutes is not None and cutoff_minutes is not None and now_minutes >= cutoff_minutes:
                return _trigger("smart_no_overnight_exit", 0.0, time_et, reason or "计划不允许隔夜")
        elif rule_type == "underlying_price":
            quote = underlying_quote(str(position.get("symbol") or "")) if underlying_quote else {}
            if not quote.get("available"):
                continue
            price = _num(quote.get("price") or quote.get("exit_price"))
            threshold = _num(rule.get("price"))
            operator = str(rule.get("operator") or "<=").strip()
            matched = (operator in {"<=", "<"} and price <= threshold) or (operator in {">=", ">"} and price >= threshold)
            if matched:
                return _trigger("smart_underlying_price_exit", threshold, price, reason or f"正股触发 {operator} {threshold:.2f}")
        elif rule_type == "pnl_giveback":
            min_profit = _num(rule.get("min_profit_pnl"))
            giveback_pct = max(1.0, min(_num(rule.get("giveback_pct") or 35), 100.0))
            if best_pnl >= min_profit:
                floor = best_pnl * (1 - giveback_pct / 100)
                if current_pnl <= floor:
                    return _trigger("smart_pnl_giveback_exit", round(floor, 2), round(best_pnl, 2), reason or f"盈利回吐超过 {giveback_pct:.0f}%")
        elif rule_type == "stop_loss_pnl":
            threshold = _num(rule.get("pnl"))
            if threshold < 0 and current_pnl <= threshold:
                return _trigger("stop", threshold, round(current_pnl, 2), reason or "达到组合止损")
        elif rule_type == "take_profit_pnl":
            threshold = _num(rule.get("pnl"))
            target = str(rule.get("target") or "tp").strip() or "tp"
            if threshold > 0 and current_pnl >= threshold:
                return _trigger(target, threshold, round(current_pnl, 2), reason or "达到组合止盈")
        elif rule_type == "option_price_stop":
            threshold = _num(rule.get("price"))
            if threshold > 0 and current_price > 0 and current_price <= threshold:
                return _trigger("smart_option_price_stop", threshold, current_price, reason or "期权价格触发止损")
        elif rule_type == "option_price_take_profit":
            threshold = _num(rule.get("price"))
            if threshold > 0 and current_price >= threshold:
                return _trigger("smart_option_price_take_profit", threshold, current_price, reason or "期权价格触发止盈")
        elif rule_type == "option_price_take_profit_pct":
            pct = _num(rule.get("pct"))
            threshold = entry_price * (1 + pct / 100) if entry_price > 0 and pct > 0 else 0.0
            if threshold > 0 and current_price >= threshold:
                return _trigger("smart_option_price_take_profit", round(threshold, 2), current_price, reason or f"期权价格达到止盈 {pct:.0f}%")
        elif rule_type == "option_greek":
            field = _normalize_greek_field(rule.get("field"))
            current = _option_metric(field, option_quote, position)
            threshold = _num(rule.get("value"))
            operator = str(rule.get("operator") or "<=").strip()
            if current is not None and _compare(current, threshold, operator):
                return _trigger(f"smart_option_{field}_exit", threshold, round(current, 6), reason or f"{field} 触发 {operator} {threshold:g}")
        elif rule_type == "option_greek_change":
            field = _normalize_greek_field(rule.get("field"))
            current = _option_metric(field, option_quote, position)
            reference = _greek_reference(field, rule, position)
            operator = str(rule.get("operator") or "<=").strip()
            threshold = _num(rule.get("change_pct")) if rule.get("change_pct") is not None else _num(rule.get("change"))
            if current is None or reference is None:
                continue
            change = current - reference
            value = (change / abs(reference) * 100) if rule.get("change_pct") is not None and abs(reference) > 1e-9 else change
            if _compare(value, threshold, operator):
                unit = "%" if rule.get("change_pct") is not None else ""
                return _trigger(f"smart_option_{field}_change_exit", threshold, round(value, 6), reason or f"{field} 变化触发 {operator} {threshold:g}{unit}")
    return None


def normalize_latest_exit(text: str, position: dict[str, Any] | None = None) -> str:
    parsed = parse_exit_at(text, position or {})
    return parsed.isoformat() if parsed else str(text or "").strip()


def parse_exit_at(value: Any, position: dict[str, Any] | None = None, *, now_et: datetime | None = None) -> datetime | None:
    position = position or {}
    parsed = parse_datetime(value, assume_tz=EASTERN)
    if parsed is not None:
        return parsed.astimezone(EASTERN)
    text = str(value or "").strip()
    if not text:
        return None
    base_now = (now_et or datetime.now(EASTERN)).astimezone(EASTERN)
    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        expiration = parse_datetime(position.get("expiration"), assume_tz=EASTERN)
        base = expiration.astimezone(EASTERN) if expiration else base_now
        if any(token in text for token in ("前一交易日", "到期前", "到期日前")):
            prior = _trading_day_before(base.date())
            base = base.replace(year=prior.year, month=prior.month, day=prior.day)
        elif "当日" in text or "收盘前" in text:
            base = base_now
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "收盘前" in text or "当日" in text:
        return base_now.replace(hour=15, minute=50, second=0, microsecond=0)
    if "到期前" in text and "1 个交易日" in text:
        expiration = parse_datetime(position.get("expiration"), assume_tz=EASTERN)
        if expiration is None:
            return None
        exp_et = expiration.astimezone(EASTERN)
        prior = _trading_day_before(exp_et.date())
        return exp_et.replace(year=prior.year, month=prior.month, day=prior.day, hour=15, minute=50, second=0, microsecond=0)
    return None


def infer_invalidation_rule(text: str, position: dict[str, Any] | None = None) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    threshold = _first_number(cleaned)
    if threshold is not None:
        if any(token in cleaned for token in ("跌破", "下方", "失守", "回落")):
            return {"type": "underlying_price", "operator": "<=", "price": threshold, "reason": cleaned}
        if any(token in cleaned for token in ("突破", "上方", "反弹回", "站上")):
            return {"type": "underlying_price", "operator": ">=", "price": threshold, "reason": cleaned}
    if "收盘" in cleaned or "到期" in cleaned:
        exit_at = normalize_latest_exit(str((position or {}).get("latest_exit") or ""), position or {})
        if exit_at:
            return {"type": "time_exit", "exit_at": exit_at, "reason": cleaned}
    return None


def _normalize_condition(item: Any, position: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    condition_type = str(item.get("type") or "").strip().lower()
    normalized: dict[str, Any] = {"type": condition_type}
    if condition_type == "time_exit":
        exit_at = str(item.get("exit_at") or item.get("time") or "").strip()
        if not exit_at and str(item.get("time_et") or "").strip():
            exit_at = normalize_latest_exit(str(item.get("time_et") or ""), position)
        normalized["exit_at"] = exit_at
    elif condition_type == "no_overnight":
        normalized["time_et"] = str(item.get("time_et") or "15:50").strip() or "15:50"
    elif condition_type == "underlying_price":
        normalized["operator"] = str(item.get("operator") or "<=").strip()
        normalized["price"] = _num(item.get("price"))
    elif condition_type == "pnl_giveback":
        normalized["min_profit_pnl"] = _num(item.get("min_profit_pnl"))
        normalized["giveback_pct"] = max(1.0, min(_num(item.get("giveback_pct") or 35), 100.0))
    elif condition_type in {"stop_loss_pnl", "take_profit_pnl"}:
        normalized["pnl"] = _num(item.get("pnl"))
        if item.get("target"):
            normalized["target"] = str(item.get("target"))
    elif condition_type in {"option_price_stop", "option_price_take_profit"}:
        normalized["price"] = _num(item.get("price"))
    elif condition_type == "option_price_take_profit_pct":
        normalized["pct"] = max(1.0, min(_num(item.get("pct") or item.get("take_profit_pct") or 30), 500.0))
    elif condition_type == "option_greek":
        normalized["field"] = _normalize_greek_field(item.get("field"))
        normalized["operator"] = str(item.get("operator") or "<=").strip()
        normalized["value"] = _num(item.get("value"))
    elif condition_type == "option_greek_change":
        normalized["field"] = _normalize_greek_field(item.get("field"))
        normalized["operator"] = str(item.get("operator") or "<=").strip()
        if item.get("change_pct") is not None:
            normalized["change_pct"] = _num(item.get("change_pct"))
        else:
            normalized["change"] = _num(item.get("change"))
        if item.get("reference") is not None:
            normalized["reference"] = _num(item.get("reference"))
    else:
        return None
    if item.get("reason"):
        normalized["reason"] = str(item.get("reason"))
    return normalized


def _dedupe_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for rule in rules:
        key = tuple(sorted(rule.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rule)
    return deduped


def _trigger(trigger: str, threshold: float, value: Any, reason: str) -> dict[str, Any]:
    return {"trigger": trigger, "threshold": threshold, "value": value, "reason": reason}


def _compare(actual: float, expected: float, operator: str) -> bool:
    return (
        (operator in {"<=", "<"} and actual <= expected)
        or (operator in {">=", ">"} and actual >= expected)
        or (operator in {"==", "="} and abs(actual - expected) < 1e-9)
    )


def _normalize_greek_field(value: Any) -> str:
    field = str(value or "").strip().lower()
    aliases = {
        "implied_volatility": "iv",
        "implied_vol": "iv",
        "volatility": "iv",
        "theta_per_day": "theta",
    }
    field = aliases.get(field, field)
    return field if field in {"delta", "gamma", "theta", "vega", "iv"} else "delta"


def _option_metric(field: str, quote: dict[str, Any] | None, position: dict[str, Any]) -> float | None:
    keys = _metric_keys(field)
    for container in (quote or {}, (quote or {}).get("raw") or {}, position or {}, (position.get("candidate") or {}) if isinstance(position.get("candidate"), dict) else {}):
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = _maybe_num(container.get(key))
            if value is not None:
                return value
        greeks = container.get("greeks")
        if isinstance(greeks, dict):
            for key in keys:
                value = _maybe_num(greeks.get(key))
                if value is not None:
                    return value
    return None


def _greek_reference(field: str, rule: dict[str, Any], position: dict[str, Any]) -> float | None:
    if rule.get("reference") is not None:
        return _maybe_num(rule.get("reference"))
    keys = []
    for key in _metric_keys(field):
        keys.extend([f"entry_{key}", f"initial_{key}", key])
    candidate = (position.get("candidate") or {}) if isinstance(position.get("candidate"), dict) else {}
    for container in (position, candidate):
        for key in keys:
            value = _maybe_num(container.get(key))
            if value is not None:
                return value
    return None


def _metric_keys(field: str) -> list[str]:
    if field == "iv":
        return ["iv", "implied_volatility", "implied_vol"]
    if field == "theta":
        return ["theta", "theta_per_day"]
    return [field]


def _first_number(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _maybe_num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

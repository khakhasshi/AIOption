from __future__ import annotations

import logging
import os
import re
import threading
import time as time_module
from dataclasses import is_dataclass, asdict
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable

from .account_store import get_account_sdk_credentials, resolve_account, touch_account, update_account_sdk_credentials
from .concurrency import longbridge_sdk_limiter
from .redis_runtime import redis_eval
from .ttl_cache import TTLCache

_LOG = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except (TypeError, ValueError):
        return float(default)


class LongbridgeSDKUnavailable(RuntimeError):
    pass


class LongbridgeSDKError(RuntimeError):
    pass


_lock = threading.Lock()
_option_quote_rate_lock = threading.Lock()
_option_quote_rate_window = 0
_option_quote_rate_used = 0
_quote_contexts: dict[str, Any] = {}
_trade_contexts: dict[str, Any] = {}
_content_contexts: dict[str, Any] = {}
_context_last_used: dict[str, float] = {}
# Per-account access-token refresh serialization + cooldown. Longbridge Legacy
# API-Key tokens expire; on a 401003 we refresh once and retry, but multiple
# concurrent calls must not stampede the refresh endpoint.
_token_refresh_lock = threading.Lock()
_token_refresh_locks: dict[str, threading.Lock] = {}
_token_refresh_last_at: dict[str, float] = {}
_quote_cache: TTLCache[dict[str, Any]] = TTLCache(_env_float("AI_OPTION_LONGBRIDGE_QUOTE_TTL_SECONDS", 3), maxsize=512, namespace="lb_quote")
_kline_cache: TTLCache[list[dict[str, Any]]] = TTLCache(_env_float("AI_OPTION_LONGBRIDGE_KLINE_TTL_SECONDS", 120), maxsize=256, namespace="lb_kline")
_intraday_cache: TTLCache[list[dict[str, Any]]] = TTLCache(_env_float("AI_OPTION_LONGBRIDGE_INTRADAY_TTL_SECONDS", 10), maxsize=256, namespace="lb_intraday")
_option_expirations_cache: TTLCache[list[str]] = TTLCache(
    _env_float("AI_OPTION_LONGBRIDGE_OPTION_EXPIRATIONS_TTL_SECONDS", 300),
    maxsize=512,
    namespace="lb_option_expirations",
)
_option_chain_cache: TTLCache[list[dict[str, Any]]] = TTLCache(
    _env_float("AI_OPTION_LONGBRIDGE_OPTION_CHAIN_TTL_SECONDS", 120),
    maxsize=1024,
    namespace="lb_option_chain",
)
_option_quote_cache: TTLCache[list[dict[str, Any]]] = TTLCache(
    _env_float("AI_OPTION_LONGBRIDGE_OPTION_QUOTE_TTL_SECONDS", 3),
    maxsize=2048,
    namespace="lb_option_quote",
)
_depth_cache: TTLCache[dict[str, Any]] = TTLCache(_env_float("AI_OPTION_LONGBRIDGE_DEPTH_TTL_SECONDS", 2), maxsize=2048, namespace="lb_depth")
_news_cache: TTLCache[list[dict[str, Any]]] = TTLCache(_env_float("AI_OPTION_LONGBRIDGE_NEWS_TTL_SECONDS", 60), maxsize=256, namespace="lb_news")
_option_quote_rate_lock = threading.Lock()
_option_quote_rate_window = 0
_option_quote_rate_used = 0
_OPTION_QUOTE_RATE_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if current + amount <= limit then
  local value = redis.call('INCRBY', KEYS[1], amount)
  redis.call('EXPIRE', KEYS[1], ttl)
  return {1, value, redis.call('TTL', KEYS[1])}
end
local remaining = redis.call('TTL', KEYS[1])
if remaining < 0 then
  redis.call('EXPIRE', KEYS[1], ttl)
  remaining = ttl
end
return {0, current, remaining}
"""
_SDK_ENUM_NAMES = {
    "AdjustType",
    "Market",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "OutsideRTH",
    "Period",
    "TimeInForceType",
    "TradeStatus",
    "TradeSessions",
    "OptionType",
    "OptionDirection",
}

_SDK_CONTEXT_TTL_SECONDS = 600.0
_SDK_CONTEXT_MAX_ACCOUNTS = 4


def sdk_backend_mode() -> str:
    return os.getenv("AI_OPTION_LONGBRIDGE_BACKEND", "sdk").strip().lower() or "sdk"


def sdk_is_allowed() -> bool:
    return sdk_backend_mode() in {"auto", "sdk", "python_sdk", "py-sdk"}


def sdk_is_required() -> bool:
    return sdk_backend_mode() in {"sdk", "python_sdk", "py-sdk"}


def sdk_is_configured(account_name: str | None = None) -> bool:
    try:
        _credentials(account_name)
    except LongbridgeSDKUnavailable:
        return False
    return True


def quote(symbol: str, account_name: str | None = None) -> dict[str, Any]:
    key = (_account_cache_key(account_name), _normalize_symbol(symbol))
    return _quote_cache.get_or_set(key, lambda: _quote_uncached(symbol, account_name))


def _quote_uncached(symbol: str, account_name: str | None = None) -> dict[str, Any]:
    rows = _call(account_name, lambda contexts: contexts.quote.quote([symbol]))
    return _quote_to_dict(rows[0]) if rows else {}


def kline(symbol: str, count: int = 80, account_name: str | None = None) -> list[dict[str, Any]]:
    key = (_account_cache_key(account_name), _normalize_symbol(symbol), int(count))
    return _kline_cache.get_or_set(key, lambda: _kline_uncached(symbol, count, account_name))


def _kline_uncached(symbol: str, count: int = 80, account_name: str | None = None) -> list[dict[str, Any]]:
    sdk = _sdk_imports()
    rows = _call(
        account_name,
        lambda contexts: contexts.quote.candlesticks(
            symbol,
            sdk.Period.Day,
            int(count),
            sdk.AdjustType.NoAdjust,
            sdk.TradeSessions.Intraday,
        ),
    )
    return [_candle_to_dict(row) for row in rows]


def intraday(symbol: str, account_name: str | None = None) -> list[dict[str, Any]]:
    key = (_account_cache_key(account_name), _normalize_symbol(symbol))
    return _intraday_cache.get_or_set(key, lambda: _intraday_uncached(symbol, account_name))


def _intraday_uncached(symbol: str, account_name: str | None = None) -> list[dict[str, Any]]:
    sdk = _sdk_imports()
    rows = _call(account_name, lambda contexts: contexts.quote.intraday(symbol, sdk.TradeSessions.Intraday))
    return [_intraday_to_dict(row) for row in rows]


def option_expirations(symbol: str, account_name: str | None = None) -> list[str]:
    key = (_account_cache_key(account_name), _normalize_symbol(symbol))
    return _option_expirations_cache.get_or_set(key, lambda: _option_expirations_uncached(symbol, account_name))


def _option_expirations_uncached(symbol: str, account_name: str | None = None) -> list[str]:
    rows = _call(account_name, lambda contexts: contexts.quote.option_chain_expiry_date_list(symbol))
    return [str(_plain(row)) for row in rows]


def option_chain_info(symbol: str, expiration: str | date, account_name: str | None = None) -> list[dict[str, Any]]:
    expiry_date = _parse_date(expiration)
    key = (_account_cache_key(account_name), _normalize_symbol(symbol), expiry_date.isoformat())
    return _option_chain_cache.get_or_set(key, lambda: _option_chain_info_uncached(symbol, expiry_date, account_name))


def _option_chain_info_uncached(symbol: str, expiration: str | date, account_name: str | None = None) -> list[dict[str, Any]]:
    expiry_date = _parse_date(expiration)
    rows = _call(account_name, lambda contexts: contexts.quote.option_chain_info_by_date(symbol, expiry_date))
    return [_option_chain_row_to_dict(row) for row in rows]


def option_quotes(symbols: list[str], account_name: str | None = None) -> list[dict[str, Any]]:
    cleaned = [str(symbol).strip() for symbol in symbols if str(symbol or "").strip()]
    if not cleaned:
        return []
    batch_size = _option_quote_batch_size()
    if len(cleaned) > batch_size:
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(cleaned), batch_size):
            rows.extend(option_quotes(cleaned[offset : offset + batch_size], account_name))
        return rows
    key = (_account_cache_key(account_name), tuple(_normalize_symbol(symbol) for symbol in cleaned))
    return _option_quote_cache.get_or_set(key, lambda: _option_quotes_uncached(cleaned, account_name))


def _option_quotes_uncached(symbols: list[str], account_name: str | None = None) -> list[dict[str, Any]]:
    _wait_for_option_quote_budget(len(symbols), account_name)
    rows = _call(account_name, lambda contexts: contexts.quote.option_quote(symbols))
    return [_option_quote_to_dict(row) for row in rows]


def depth(symbol: str, account_name: str | None = None) -> dict[str, Any]:
    key = (_account_cache_key(account_name), _normalize_symbol(symbol))
    return _depth_cache.get_or_set(key, lambda: _depth_uncached(symbol, account_name))


def _depth_uncached(symbol: str, account_name: str | None = None) -> dict[str, Any]:
    row = _call(account_name, lambda contexts: contexts.quote.depth(symbol))
    return _depth_to_dict(row)


def news(symbol: str, account_name: str | None = None) -> list[dict[str, Any]]:
    key = (_account_cache_key(account_name), _normalize_symbol(symbol))
    return _news_cache.get_or_set(key, lambda: _news_uncached(symbol, account_name))


def _news_uncached(symbol: str, account_name: str | None = None) -> list[dict[str, Any]]:
    rows = _call(account_name, lambda contexts: contexts.content.news(symbol))
    return [_plain(row) for row in rows]


def assets(account_name: str | None = None, currency: str = "USD") -> list[dict[str, Any]]:
    rows = _call(account_name, lambda contexts: contexts.trade.account_balance(currency))
    return [_asset_to_dict(row) for row in rows]


def executions(
    account_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    history: bool = False,
) -> list[dict[str, Any]]:
    if history:
        start_at = _parse_datetime(start)
        end_at = _parse_datetime(end, end_of_day=True)
        rows = _call(account_name, lambda contexts: contexts.trade.history_executions(start_at=start_at, end_at=end_at))
    else:
        rows = _call(account_name, lambda contexts: contexts.trade.today_executions())
    return [_execution_to_dict(row) for row in rows]


def today_orders(account_name: str | None = None) -> list[dict[str, Any]]:
    rows = _call(account_name, lambda contexts: contexts.trade.today_orders())
    return [_order_to_dict(row) for row in rows]


def cancel_order(order_id: str, account_name: str | None = None) -> dict[str, Any]:
    response = _call(account_name, lambda contexts: contexts.trade.cancel_order(order_id))
    data = _plain(response)
    if isinstance(data, dict):
        data.setdefault("order_id", order_id)
        return data
    return {"order_id": order_id, "raw": data}


def positions(account_name: str | None = None) -> list[dict[str, Any]]:
    response = _call(account_name, lambda contexts: contexts.trade.stock_positions())
    return _positions_to_rows(response)


def check(account_name: str | None = None) -> dict[str, Any]:
    contexts = _contexts(account_name)
    try:
        member_id = contexts.quote.member_id()
        level = contexts.quote.quote_level()
        account = resolve_account(account_name)
        touch_account(account.name)
        return {
            "connectivity": {"sdk": {"ok": True}},
            "region": {
                "active": os.getenv("LONGBRIDGE_REGION") or os.getenv("LONGPORT_REGION") or "auto",
                "cached": os.getenv("LONGBRIDGE_REGION") or os.getenv("LONGPORT_REGION") or "auto",
            },
            "session": {
                "token": "valid",
                "detail": (
                    "Python SDK API key configured; "
                    f"member_id={member_id}; quote_level={_summarize_quote_level(level)}"
                ),
            },
        }
    except Exception as exc:  # noqa: BLE001 - normalize SDK exceptions for existing callers.
        raise _sdk_error(exc) from exc


def _summarize_quote_level(level: Any) -> str:
    text = str(level).strip()
    if not text:
        return "unknown"
    profiles = [part for part in text.split(";") if part.strip()]
    if profiles:
        return f"{len(profiles)} market profiles"
    if len(text) <= 80:
        return text
    return f"{text[:77]}..."


def _option_quote_batch_size() -> int:
    limit = _option_quote_symbols_per_minute()
    default = min(80, limit) if limit > 0 else 80
    value = _env_int("AI_OPTION_LONGBRIDGE_OPTION_QUOTE_BATCH_SIZE", default, 1, 500)
    return min(value, limit) if limit > 0 else value


def _option_quote_symbols_per_minute() -> int:
    return _env_int("AI_OPTION_LONGBRIDGE_OPTION_QUOTE_SYMBOLS_PER_MINUTE", 420, 0, 20000)


def _wait_for_option_quote_budget(symbol_count: int, account_name: str | None = None) -> None:
    limit = _option_quote_symbols_per_minute()
    if limit <= 0 or symbol_count <= 0:
        return
    amount = min(int(symbol_count), limit)
    account_key = _account_cache_key(account_name)
    while True:
        window = int(time_module.time() // 60)
        redis_key = f"ai-option:rate:lb-option-quote:{account_key}:{window}"
        result = redis_eval(
            _OPTION_QUOTE_RATE_SCRIPT,
            [redis_key],
            [str(amount), str(limit), "70"],
        )
        if result is not None:
            allowed, _used, ttl = _decode_rate_result(result)
            if allowed:
                return
            time_module.sleep(max(1.0, min(float(ttl or 1), 8.0)))
            continue
        if _take_local_option_quote_budget(amount, limit, window):
            return
        seconds_left = 60 - (time_module.time() % 60)
        time_module.sleep(max(1.0, min(seconds_left, 8.0)))


def _decode_rate_result(result: Any) -> tuple[bool, int, int]:
    if isinstance(result, (list, tuple)) and len(result) >= 3:
        allowed = _plain_rate_int(result[0]) == 1
        used = _plain_rate_int(result[1])
        ttl = _plain_rate_int(result[2])
        return allowed, used, ttl
    return True, 0, 1


def _plain_rate_int(value: Any) -> int:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _take_local_option_quote_budget(amount: int, limit: int, window: int) -> bool:
    global _option_quote_rate_used, _option_quote_rate_window
    with _option_quote_rate_lock:
        if _option_quote_rate_window != window:
            _option_quote_rate_window = window
            _option_quote_rate_used = 0
        if _option_quote_rate_used + amount <= limit:
            _option_quote_rate_used += amount
            return True
    return False


def _env_int(name: str, default: int, min_value: int = 0, max_value: int = 20000) -> int:
    try:
        value = int(os.getenv(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(int(value), max_value))


_SDK_CONTEXT_TTL_SECONDS = max(_env_float("AI_OPTION_LONGBRIDGE_CONTEXT_TTL_SECONDS", 600), 30.0)
_SDK_CONTEXT_MAX_ACCOUNTS = max(_env_int("AI_OPTION_LONGBRIDGE_CONTEXT_MAX_ACCOUNTS", 4, 1, 64), 1)


def logout(account_name: str | None = None) -> dict[str, Any]:
    account = resolve_account(account_name)
    _drop_contexts(account.name)
    return {"message": "Python SDK API key mode has no local OAuth session to logout"}


def submit_buy_order(
    symbol: str,
    quantity: int,
    price: float | None,
    account_name: str | None = None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    sdk = _sdk_imports()
    normalized_order_type = _normalize_entry_order_type(order_type)
    submit_kwargs: dict[str, Any] = {
        "symbol": symbol,
        "order_type": sdk.OrderType.MO if normalized_order_type == "market" else sdk.OrderType.LO,
        "side": sdk.OrderSide.Buy,
        "submitted_quantity": Decimal(int(quantity)),
        "time_in_force": sdk.TimeInForceType.Day,
        "remark": (remark or "")[:64] or None,
    }
    if normalized_order_type == "limit":
        submit_kwargs["submitted_price"] = Decimal(_price(float(price or 0)))
    response = _call(
        account_name,
        lambda contexts: contexts.trade.submit_order(**submit_kwargs),
    )
    return _order_response_to_dict(response)


def submit_sell_order(
    symbol: str,
    quantity: int,
    price: float | None,
    account_name: str | None = None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    sdk = _sdk_imports()
    normalized_order_type = _normalize_entry_order_type(order_type)
    submit_kwargs: dict[str, Any] = {
        "symbol": symbol,
        "order_type": sdk.OrderType.MO if normalized_order_type == "market" else sdk.OrderType.LO,
        "side": sdk.OrderSide.Sell,
        "submitted_quantity": Decimal(int(quantity)),
        "time_in_force": sdk.TimeInForceType.Day,
        "remark": (remark or "")[:64] or None,
    }
    if normalized_order_type == "limit":
        submit_kwargs["submitted_price"] = Decimal(_price(float(price or 0)))
    response = _call(
        account_name,
        lambda contexts: contexts.trade.submit_order(**submit_kwargs),
    )
    return _order_response_to_dict(response)


def submit_market_order(
    symbol: str,
    quantity: Decimal | int | float | str,
    side: str,
    account_name: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    sdk = _sdk_imports()
    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise LongbridgeSDKError(f"unsupported order side `{side}`")
    response = _call(
        account_name,
        lambda contexts: contexts.trade.submit_order(
            symbol=symbol,
            order_type=sdk.OrderType.MO,
            side=sdk.OrderSide.Buy if normalized_side == "buy" else sdk.OrderSide.Sell,
            submitted_quantity=_quantity_decimal(quantity),
            time_in_force=sdk.TimeInForceType.Day,
            remark=(remark or "")[:64] or None,
        ),
    )
    return _order_response_to_dict(response)


def submit_stop_sell_order(
    symbol: str,
    quantity: int,
    trigger_price: float,
    account_name: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    sdk = _sdk_imports()
    response = _call(
        account_name,
        lambda contexts: contexts.trade.submit_order(
            symbol=symbol,
            order_type=sdk.OrderType.MIT,
            side=sdk.OrderSide.Sell,
            submitted_quantity=Decimal(int(quantity)),
            time_in_force=sdk.TimeInForceType.Day,
            trigger_price=Decimal(_price(trigger_price)),
            remark=(remark or "")[:64] or None,
        ),
    )
    return _order_response_to_dict(response)


def order_detail(order_id: str, account_name: str | None = None) -> dict[str, Any]:
    row = _call(account_name, lambda contexts: contexts.trade.order_detail(order_id))
    return _order_to_dict(row)


class _SDKImports:
    def __init__(self) -> None:
        try:
            from longbridge.openapi import (  # type: ignore[import-not-found]
                AdjustType,
                Config,
                ContentContext,
                OpenApiException,
                OrderSide,
                OrderType,
                Period,
                QuoteContext,
                TimeInForceType,
                TradeContext,
                TradeSessions,
            )
        except Exception as exc:  # noqa: BLE001
            raise LongbridgeSDKUnavailable("longbridge Python SDK is not installed") from exc

        self.AdjustType = AdjustType
        self.Config = Config
        self.ContentContext = ContentContext
        self.OpenApiException = OpenApiException
        self.OrderSide = OrderSide
        self.OrderType = OrderType
        self.Period = Period
        self.QuoteContext = QuoteContext
        self.TimeInForceType = TimeInForceType
        self.TradeContext = TradeContext
        self.TradeSessions = TradeSessions


_sdk_cache: _SDKImports | None = None


def _sdk_imports() -> _SDKImports:
    global _sdk_cache
    if _sdk_cache is None:
        _sdk_cache = _SDKImports()
    return _sdk_cache


class _Contexts:
    def __init__(self, quote_context: Any, trade_context: Any, content_context: Any) -> None:
        self.quote = quote_context
        self.trade = trade_context
        self.content = content_context


def _token_auto_refresh_enabled() -> bool:
    return (os.getenv("AI_OPTION_LONGBRIDGE_AUTO_REFRESH_TOKEN", "true") or "").strip().lower() in {"1", "true", "yes", "on"}


def _token_refresh_cooldown_seconds() -> float:
    return _env_float("AI_OPTION_LONGBRIDGE_TOKEN_REFRESH_COOLDOWN_SECONDS", 30.0)


def _is_token_expired_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code is not None and str(code).strip() == "401003":
        return True
    message = str(getattr(exc, "message", None) or exc).lower()
    return "token expired" in message or "401003" in message


def _account_refresh_lock(account_name: str) -> threading.Lock:
    with _token_refresh_lock:
        lock = _token_refresh_locks.get(account_name)
        if lock is None:
            lock = threading.Lock()
            _token_refresh_locks[account_name] = lock
        return lock


def refresh_account_access_token(account_name: str | None = None, *, force: bool = False) -> bool:
    """Refresh a Longbridge Legacy API-Key access token and persist it.

    Returns True if the token was just refreshed (or another thread refreshed it
    within the cooldown window, so a retry is worthwhile). Serialized per account
    with a cooldown so concurrent 401003s don't stampede the refresh endpoint.
    Refresh is only possible in from_apikey mode (needs app_key + app_secret).
    """
    account = resolve_account(account_name)
    lock = _account_refresh_lock(account.name)
    with lock:
        now = time_module.monotonic()
        last = _token_refresh_last_at.get(account.name)
        if not force and last is not None and (now - last) < _token_refresh_cooldown_seconds():
            # Another thread just refreshed — caller should retry with the new token.
            return True
        try:
            app_key, app_secret, access_token = _credentials(account.name)
        except LongbridgeSDKUnavailable:
            return False
        sdk = _sdk_imports()
        try:
            config = sdk.Config.from_apikey(app_key, app_secret, access_token, enable_print_quote_packages=False)
            new_token = config.refresh_access_token()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Longbridge token refresh failed for %s: %s", account.name, exc)
            return False
        if not new_token or not isinstance(new_token, str):
            _LOG.warning("Longbridge token refresh for %s returned no token", account.name)
            return False
        try:
            update_account_sdk_credentials(account.name, app_key, app_secret, new_token, owner_id=account.owner_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Longbridge token refresh persisted-store failure for %s: %s", account.name, exc)
            return False
        _drop_contexts(account.name)  # force rebuild with the new token
        _token_refresh_last_at[account.name] = time_module.monotonic()
        _LOG.info("Longbridge access token refreshed for %s", account.name)
        return True


def refresh_all_sdk_account_tokens() -> dict[str, Any]:
    """Proactively refresh every LB account that has SDK credentials.

    Legacy API-Key tokens expire (~90 days); refresh_access_token extends them
    another window. Running this well BEFORE expiry keeps tokens out of the
    dead zone where even the refresh call is rejected (401003) — the failure
    mode that reactive-only refresh cannot recover. Best-effort per account.
    """
    from .account_store import list_accounts

    refreshed: list[str] = []
    failed: list[str] = []
    try:
        accounts = [a for a in list_accounts() if getattr(a, "sdk_credentials_configured", False)]
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Longbridge proactive refresh: list_accounts failed: %s", exc)
        return {"refreshed": [], "failed": [], "error": str(exc)}
    for account in accounts:
        try:
            if refresh_account_access_token(account.name, force=True):
                refreshed.append(account.name)
            else:
                failed.append(account.name)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Longbridge proactive refresh failed for %s: %s", account.name, exc)
            failed.append(account.name)
    if refreshed or failed:
        _LOG.info("Longbridge proactive token refresh: %d ok, %d failed", len(refreshed), len(failed))
    return {"refreshed": refreshed, "failed": failed}


def _call(account_name: str | None, callback: Callable[[_Contexts], Any]) -> Any:
    with longbridge_sdk_limiter.acquire():
        try:
            result = callback(_contexts(account_name))
            account = resolve_account(account_name)
            touch_account(account.name)
            return result
        except (LongbridgeSDKUnavailable, LongbridgeSDKError):
            raise
        except Exception as exc:  # noqa: BLE001
            # Auto-refresh an expired Legacy API-Key token once, then retry.
            if _token_auto_refresh_enabled() and _is_token_expired_error(exc):
                if refresh_account_access_token(account_name):
                    try:
                        result = callback(_contexts(account_name))
                        account = resolve_account(account_name)
                        touch_account(account.name)
                        return result
                    except (LongbridgeSDKUnavailable, LongbridgeSDKError):
                        raise
                    except Exception as retry_exc:  # noqa: BLE001
                        raise _sdk_error(retry_exc) from retry_exc
            raise _sdk_error(exc) from exc



def _contexts(account_name: str | None = None) -> _Contexts:
    account = resolve_account(account_name)
    app_key, app_secret, access_token = _credentials(account.name)
    sdk = _sdk_imports()
    cache_key = f"{account.name}:{_fingerprint(app_key, access_token)}"
    now = time_module.monotonic()

    with _lock:
        quote_context = _quote_contexts.get(cache_key)
        trade_context = _trade_contexts.get(cache_key)
        content_context = _content_contexts.get(cache_key)
        if quote_context is None or trade_context is None or content_context is None:
            config = sdk.Config.from_apikey(app_key, app_secret, access_token, enable_print_quote_packages=False)
            quote_context = sdk.QuoteContext(config)
            trade_context = sdk.TradeContext(config)
            content_context = sdk.ContentContext(config)
            _quote_contexts[cache_key] = quote_context
            _trade_contexts[cache_key] = trade_context
            _content_contexts[cache_key] = content_context
        _context_last_used[cache_key] = now
        _evict_stale_contexts_locked(now)
    return _Contexts(quote_context, trade_context, content_context)


def _drop_contexts(account_name: str) -> None:
    with _lock:
        for cache in (_quote_contexts, _trade_contexts, _content_contexts):
            for key in list(cache):
                if key.startswith(f"{account_name}:"):
                    context = cache.pop(key, None)
                    _close_context(context)
                    _context_last_used.pop(key, None)


def _evict_stale_contexts_locked(now: float) -> None:
    if not _context_last_used:
        return
    stale_keys = [key for key, last_used in _context_last_used.items() if now - last_used > _SDK_CONTEXT_TTL_SECONDS]
    for key in stale_keys:
        _remove_context_key_locked(key)
    active_accounts = {
        key.split(":", 1)[0]
        for key in _context_last_used
    }
    if len(active_accounts) <= _SDK_CONTEXT_MAX_ACCOUNTS:
        return
    ordered = sorted(_context_last_used.items(), key=lambda item: item[1])
    for key, _last_used in ordered:
        if len({k.split(":", 1)[0] for k in _context_last_used}) <= _SDK_CONTEXT_MAX_ACCOUNTS:
            break
        _remove_context_key_locked(key)


def _remove_context_key_locked(cache_key: str) -> None:
    for cache in (_quote_contexts, _trade_contexts, _content_contexts):
        context = cache.pop(cache_key, None)
        _close_context(context)
    _context_last_used.pop(cache_key, None)


def _close_context(context: Any | None) -> None:
    if context is None:
        return
    close = getattr(context, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def sdk_pool_snapshot() -> dict[str, Any]:
    with _lock:
        active_accounts = {key.split(":", 1)[0] for key in _context_last_used}
        return {
            "accounts": len(active_accounts),
            "contexts": {
                "quote": len(_quote_contexts),
                "trade": len(_trade_contexts),
                "content": len(_content_contexts),
            },
            "ttl_seconds": _SDK_CONTEXT_TTL_SECONDS,
            "max_accounts": _SDK_CONTEXT_MAX_ACCOUNTS,
        }


def _credentials(account_name: str | None = None) -> tuple[str, str, str]:
    account = resolve_account(account_name)
    stored = get_account_sdk_credentials(account.name, owner_id=account.owner_id)
    if stored is not None:
        return stored
    safe = re.sub(r"[^A-Za-z0-9]+", "_", account.name).upper()
    app_key = _env_first(f"AI_OPTION_LONGBRIDGE_{safe}_APP_KEY", f"LONGBRIDGE_{safe}_APP_KEY", "LONGBRIDGE_APP_KEY", "LONGPORT_APP_KEY")
    app_secret = _env_first(
        f"AI_OPTION_LONGBRIDGE_{safe}_APP_SECRET",
        f"LONGBRIDGE_{safe}_APP_SECRET",
        "LONGBRIDGE_APP_SECRET",
        "LONGPORT_APP_SECRET",
    )
    access_token = _env_first(
        f"AI_OPTION_LONGBRIDGE_{safe}_ACCESS_TOKEN",
        f"LONGBRIDGE_{safe}_ACCESS_TOKEN",
        "LONGBRIDGE_ACCESS_TOKEN",
        "LONGPORT_ACCESS_TOKEN",
    )
    if not app_key or not app_secret or not access_token:
        raise LongbridgeSDKUnavailable(
            f"Python SDK API key credentials are not configured for Longbridge account `{account.name}`"
        )
    return app_key, app_secret, access_token


def _account_cache_key(account_name: str | None = None) -> str:
    return resolve_account(account_name).name


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _env_first(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _sdk_error(exc: Exception) -> LongbridgeSDKError:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    trace_id = getattr(exc, "trace_id", None)
    if code or trace_id:
        parts = [f"API error (code {code})" if code else "API error", str(message)]
        if trace_id:
            parts.append(f"trace_id: {trace_id}")
        return LongbridgeSDKError("\n  ".join(parts))
    return LongbridgeSDKError(str(message))


def _quote_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("last", _first(data, "last_done", "last"))
    data.setdefault("prev_close", _first(data, "prev_close_price", "prev_close"))
    return data


def _candle_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("time", _first(data, "timestamp", "time"))
    return data


def _intraday_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("time", _first(data, "timestamp", "time"))
    data.setdefault("price", _first(data, "price", "last_done", "close"))
    return data


def _option_chain_row_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("strike", _first(data, "price", "strike", "strike_price"))
    data.setdefault("call_symbol", _first(data, "call_symbol", "callSymbol"))
    data.setdefault("put_symbol", _first(data, "put_symbol", "putSymbol"))
    return data


def _option_quote_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("last_price", _first(data, "last_done", "last_price", "price", "close"))
    data.setdefault("last", _first(data, "last_done", "last_price", "last", "price", "close"))
    data.setdefault("prev_close", _first(data, "prev_close_price", "prev_close"))
    data.setdefault("open_interest", _first(data, "open_interest", "openInterest"))
    data.setdefault("implied_volatility", _first(data, "implied_volatility", "impliedVolatility", "iv"))
    data.setdefault("bid", _first(data, "bid", "bid_price", "bidPrice"))
    data.setdefault("ask", _first(data, "ask", "ask_price", "askPrice"))
    return data


def _depth_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    bids = data.get("bids") or data.get("bid") or data.get("buy") or []
    asks = data.get("asks") or data.get("ask") or data.get("sell") or []
    data.setdefault("bids", bids if isinstance(bids, list) else [])
    data.setdefault("asks", asks if isinstance(asks, list) else [])
    data.setdefault("best_bid", _best_depth_price(data["bids"]))
    data.setdefault("best_ask", _best_depth_price(data["asks"]))
    return data


def _best_depth_price(levels: Any) -> Any:
    if not isinstance(levels, list) or not levels:
        return None
    first = levels[0]
    if isinstance(first, dict):
        return _first(first, "price", "bid_price", "ask_price")
    if isinstance(first, (list, tuple)) and first:
        return first[0]
    return first


def _asset_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("net_assets", _first(data, "net_assets", "total_assets"))
    data.setdefault("total_cash", _first(data, "total_cash", "cash"))
    data.setdefault("buy_power", _first(data, "buy_power", "available_cash"))
    return data


def _execution_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("quantity", _first(data, "quantity", "executed_quantity", "executed_qty"))
    data.setdefault("price", _first(data, "price", "executed_price"))
    return data


def _position_to_dict(row: Any, channel: Any = None) -> dict[str, Any]:
    data = _plain(row)
    if isinstance(data, dict):
        data.setdefault("available_quantity", _first(data, "available_quantity", "available_qty"))
        data.setdefault("quantity", _first(data, "quantity", "qty"))
        if channel is not None:
            data.setdefault("account_channel", channel)
    return data if isinstance(data, dict) else {"raw": data}


def _positions_to_rows(response: Any) -> list[dict[str, Any]]:
    data = _plain(response)
    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        channels = data.get("channels")
        if isinstance(channels, list):
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                channel_name = channel.get("account_channel")
                positions_value = channel.get("positions")
                if isinstance(positions_value, list):
                    rows.extend(_position_to_dict(item, channel_name) for item in positions_value)
            return rows
        positions_value = data.get("positions")
        if isinstance(positions_value, list):
            return [_position_to_dict(item) for item in positions_value]
    if isinstance(data, list):
        return [_position_to_dict(item) for item in data]
    return rows


def _order_response_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    if "order_id" not in data:
        data["order_id"] = str(row)
    return data


def _order_to_dict(row: Any) -> dict[str, Any]:
    data = _plain(row)
    data.setdefault("executed_quantity", _first(data, "executed_qty", "executed_quantity", "filled_quantity"))
    data.setdefault("filled_quantity", _first(data, "executed_qty", "executed_quantity", "filled_quantity"))
    data.setdefault("executed_price", _first(data, "executed_price", "filled_price", "average_price", "avg_price"))
    data.setdefault("quantity", _first(data, "submitted_quantity", "quantity"))
    data.setdefault("price", _first(data, "submitted_price", "price"))
    return data


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _plain(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, type):
        return value.__name__
    if value.__class__.__name__ in _SDK_ENUM_NAMES:
        return str(value).split(".")[-1]
    if depth > 4:
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list | tuple | set):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item, depth + 1) for key, item in value.items()}
    if is_dataclass(value):
        return _plain(asdict(value), depth + 1)

    output: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        output[name] = _plain(attr, depth + 1)
    if output:
        return output
    return str(value)


def _parse_datetime(value: str | None, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for parser in (datetime.fromisoformat,):
        try:
            parsed = parser(text)
            if len(text) == 10 and end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return parsed
        except ValueError:
            continue
    return None


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise LongbridgeSDKError(f"invalid option expiration date `{value}`") from exc


def _price(value: float) -> str:
    return f"{max(float(value), 0.01):.2f}"


def _quantity_decimal(value: Decimal | int | float | str) -> Decimal:
    quantity = Decimal(str(value)).copy_abs()
    if quantity <= 0:
        raise LongbridgeSDKError("order quantity must be greater than zero")
    return quantity


def _normalize_entry_order_type(value: str | None) -> str:
    normalized = str(value or "market").strip().lower()
    return "market" if normalized in {"market", "mo"} else "limit"


def _fingerprint(app_key: str, access_token: str) -> str:
    return f"{app_key[-6:]}:{access_token[-8:]}"

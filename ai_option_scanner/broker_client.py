from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any

from . import alpaca_client
from . import longbridge_client
from . import usmart_client
from .account_store import resolve_account as resolve_longbridge_account
from .broker_store import BrokerAccount, broker_ref, normalize_broker, parse_broker_ref, resolve_broker_account
from .option_symbol_utils import option_symbol_for_longbridge, option_symbol_for_occ, option_symbol_for_usmart


class BrokerError(RuntimeError):
    pass


_check_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_assets_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 30.0  # broker check/assets cache TTL


def account_ref_for_config(config: dict[str, Any], owner_id: str | None = None) -> str:
    broker = normalize_broker(config.get("broker"))
    if broker == "alpaca":
        account = resolve_broker_account("alpaca", config.get("broker_account") or config.get("alpaca_account"), owner_id=owner_id)
        return account.ref
    if broker == "usmart":
        account = resolve_broker_account("usmart", config.get("broker_account") or config.get("usmart_account"), owner_id=owner_id)
        return account.ref
    try:
        account = resolve_longbridge_account(config.get("longbridge_account"), owner_id=owner_id)
        return broker_ref("longbridge", account.name, account.owner_id)
    except ValueError:
        legacy_name = str(config.get("longbridge_account") or "").strip()
        if legacy_name:
            return broker_ref("longbridge", legacy_name, owner_id)
        raise


def display_account_name(account_ref: str | None) -> str:
    broker, owner, name = parse_broker_ref(account_ref)
    return name or ""


def check(account_ref: str | None = None) -> dict[str, Any]:
    key = str(account_ref or "")
    now = time.monotonic()
    with _cache_lock:
        cached = _check_cache.get(key)
        if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]
    broker, account = _resolve(account_ref)
    if broker == "alpaca":
        result = alpaca_client.check(account)  # type: ignore[arg-type]
    elif broker == "usmart":
        result = usmart_client.check(account)  # type: ignore[arg-type]
    else:
        result = longbridge_client.check(account)  # type: ignore[arg-type]
    with _cache_lock:
        _check_cache[key] = (now, result)
    return result


def assets(account_ref: str | None = None, currency: str = "USD") -> list[dict[str, Any]]:
    key = f"{account_ref or ''}:{currency}"
    now = time.monotonic()
    with _cache_lock:
        cached = _assets_cache.get(key)
        if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]
    broker, account = _resolve(account_ref)
    if broker == "alpaca":
        result = alpaca_client.assets(account, currency)  # type: ignore[arg-type]
    elif broker == "usmart":
        result = usmart_client.assets(account, currency)  # type: ignore[arg-type]
    else:
        result = longbridge_client.assets(account, currency)  # type: ignore[arg-type]
    with _cache_lock:
        _assets_cache[key] = (now, result)
    return result


def positions(account_ref: str | None = None) -> list[dict[str, Any]]:
    broker, account = _resolve(account_ref)
    if broker == "alpaca":
        return alpaca_client.positions(account)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.positions(account)  # type: ignore[arg-type]
    return longbridge_client.positions(account)  # type: ignore[arg-type]


def today_orders(account_ref: str | None = None) -> list[dict[str, Any]]:
    broker, account = _resolve(account_ref)
    if broker == "alpaca":
        return alpaca_client.today_orders(account)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.today_orders(account)  # type: ignore[arg-type]
    return longbridge_client.today_orders(account)  # type: ignore[arg-type]


def executions(
    account_ref: str | None = None,
    start: str | None = None,
    end: str | None = None,
    history: bool = False,
) -> list[dict[str, Any]]:
    """Return broker fills when the configured broker exposes an execution feed.

    Longbridge is currently the only broker adapter with a historical execution
    API. Other brokers return an empty list so position reconciliation can still
    close stale local state without fabricating fill prices.
    """
    broker, account = _resolve(account_ref)
    if broker != "longbridge":
        return []
    return longbridge_client.executions(account, start, end, history)  # type: ignore[arg-type]


def cancel_order(order_id: str, account_ref: str | None = None) -> dict[str, Any]:
    broker, account = _resolve(account_ref)
    if broker == "alpaca":
        return alpaca_client.cancel_order(account, order_id)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.cancel_order(account, order_id)  # type: ignore[arg-type]
    return longbridge_client.cancel_order(order_id, account)  # type: ignore[arg-type]


def submit_buy_order(
    symbol: str,
    quantity: int,
    price: float | None,
    account_ref: str | None = None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    broker, account = _resolve_for_order(account_ref)
    symbol = _order_symbol_for_broker(symbol, broker)
    if broker == "alpaca":
        return alpaca_client.submit_buy_order(account, symbol, quantity, price, remark, order_type)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.submit_buy_order(account, symbol, quantity, price, remark, order_type)  # type: ignore[arg-type]
    return longbridge_client.submit_buy_order(symbol, quantity, price, account, remark, order_type)  # type: ignore[arg-type]


def submit_sell_order(
    symbol: str,
    quantity: int,
    price: float | None,
    account_ref: str | None = None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    broker, account = _resolve_for_order(account_ref)
    symbol = _order_symbol_for_broker(symbol, broker)
    if broker == "alpaca":
        return alpaca_client.submit_sell_order(account, symbol, quantity, price, remark, order_type)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.submit_sell_order(account, symbol, quantity, price, remark, order_type)  # type: ignore[arg-type]
    return longbridge_client.submit_sell_order(symbol, quantity, price, account, remark, order_type)  # type: ignore[arg-type]


def submit_market_order(
    symbol: str,
    quantity: Decimal | int | float | str,
    side: str,
    account_ref: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    broker, account = _resolve_for_order(account_ref)
    symbol = _order_symbol_for_broker(symbol, broker)
    if broker == "alpaca":
        return alpaca_client.submit_market_order(account, symbol, quantity, side, remark)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.submit_market_order(account, symbol, quantity, side, remark)  # type: ignore[arg-type]
    return longbridge_client.submit_market_order(symbol, quantity, side, account, remark)  # type: ignore[arg-type]


def submit_stop_sell_order(
    symbol: str,
    quantity: int,
    trigger_price: float,
    account_ref: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    broker, account = _resolve_for_order(account_ref)
    symbol = _order_symbol_for_broker(symbol, broker)
    if broker == "alpaca":
        return alpaca_client.submit_stop_sell_order(account, symbol, quantity, trigger_price, remark)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.submit_stop_sell_order(account, symbol, quantity, trigger_price, remark)  # type: ignore[arg-type]
    return longbridge_client.submit_stop_sell_order(symbol, quantity, trigger_price, account, remark)  # type: ignore[arg-type]


def order_detail(order_id: str, account_ref: str | None = None) -> dict[str, Any]:
    broker, account = _resolve(account_ref)
    if broker == "alpaca":
        return alpaca_client.order_detail(account, order_id)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.order_detail(account, order_id)  # type: ignore[arg-type]
    return longbridge_client.order_detail(order_id, account)  # type: ignore[arg-type]


def wait_for_order_fill(order_id: str, account_ref: str | None = None, timeout_seconds: float = 8) -> dict[str, Any]:
    broker, account = _resolve(account_ref)
    if broker == "alpaca":
        return alpaca_client.wait_for_order_fill(account, order_id, timeout_seconds)  # type: ignore[arg-type]
    if broker == "usmart":
        return usmart_client.wait_for_order_fill(account, order_id, timeout_seconds)  # type: ignore[arg-type]
    return longbridge_client.wait_for_order_fill(order_id, account, timeout_seconds)  # type: ignore[arg-type]


def option_order_symbol(yfinance_contract_symbol: str, account_ref: str | None = None) -> str:
    broker, _, _ = parse_broker_ref(account_ref)
    return _order_symbol_for_broker(yfinance_contract_symbol, broker)


# Quotes are intentionally NOT dispatched per broker (unlike order_detail /
# submit_* / positions above). Per ADR-001 market data and brokers are independent
# axes: Longbridge is the SDK market-data source regardless of which broker holds
# the account, and Alpaca/uSMART deliberately implement no quote() of their own.
# _longbridge_account_name() returns None for non-Longbridge refs so these fall back
# to the default Longbridge market-data account. Do NOT add broker branching here —
# there is no alpaca/usmart quote endpoint to branch to.
def quote(symbol: str, account_ref: str | None = None) -> dict[str, Any]:
    return longbridge_client.quote(symbol, _longbridge_account_name(account_ref))


def quote_option_contract(contract_symbol: str, account_ref: str | None = None) -> dict[str, Any]:
    return longbridge_client.quote_option_contract(contract_symbol, _longbridge_account_name(account_ref))


def _resolve(account_ref: str | None) -> tuple[str, str | BrokerAccount | None]:
    broker, owner_id, name = parse_broker_ref(account_ref)
    if broker == "alpaca":
        return broker, resolve_broker_account("alpaca", name, owner_id=owner_id)
    if broker == "usmart":
        return broker, resolve_broker_account("usmart", name, owner_id=owner_id)
    return "longbridge", name


def _resolve_for_order(account_ref: str | None) -> tuple[str, str | BrokerAccount | None]:
    """Resolve an account for ORDER PLACEMENT — fail-closed on a missing ref.

    For Longbridge, ``_resolve`` maps an empty/None ref to ``name=None``, which
    longbridge_client treats as "use the default account" — a fail-OPEN that
    routes a real order to whatever the default (often LIVE) account is when a
    caller loses or forgets the ref. Alpaca/uSMART already raise when their named
    row is missing; this makes Longbridge order placement equally strict. Read
    and cancel paths keep the lenient ``_resolve`` (default-account reads are
    harmless and some callers rely on them)."""
    if account_ref is None or not str(account_ref).strip():
        raise BrokerError("account_ref is required to place an order; refusing to route to a default broker account")
    broker, account = _resolve(account_ref)
    if broker == "longbridge" and (account is None or not str(account).strip()):
        raise BrokerError(f"could not resolve a Longbridge account from ref {account_ref!r}; refusing to use the default account for an order")
    return broker, account


def _longbridge_account_name(account_ref: str | None) -> str | None:
    broker, owner_id, name = parse_broker_ref(account_ref)
    return name if broker == "longbridge" else None


def _order_symbol_for_broker(symbol: str, broker: str) -> str:
    normalized = normalize_broker(broker)
    if normalized == "alpaca":
        return option_symbol_for_occ(symbol)
    if normalized == "usmart":
        return option_symbol_for_usmart(symbol)
    return option_symbol_for_longbridge(symbol)

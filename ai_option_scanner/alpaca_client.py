from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .broker_store import BrokerAccount, get_broker_credentials, touch_broker_account, update_broker_account_status
from .option_symbol_utils import option_symbol_for_occ


class AlpacaError(RuntimeError):
    pass


_account_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_account_cache_lock = threading.Lock()
_ACCOUNT_CACHE_TTL_SECONDS = 60.0


def _cached_account(account: BrokerAccount) -> dict[str, Any]:
    key = f"{account.broker}:{account.name}:{account.owner_id}"
    now = time.monotonic()
    with _account_cache_lock:
        cached = _account_cache.get(key)
        if cached is not None and (now - cached[0]) < _ACCOUNT_CACHE_TTL_SECONDS:
            return cached[1]
    payload = _request(account, "GET", "/v2/account")
    with _account_cache_lock:
        _account_cache[key] = (now, payload)
    return payload


def check(account: BrokerAccount) -> dict[str, Any]:
    payload = _cached_account(account)
    status = str(payload.get("status") or "unknown")
    update_broker_account_status(account, status, {"account_number": payload.get("account_number"), "trading_blocked": payload.get("trading_blocked")})
    return {"broker": "alpaca", "status": status, "account": payload, "session": {"token": "valid" if payload else "unknown"}}


def assets(account: BrokerAccount, currency: str = "USD") -> list[dict[str, Any]]:
    payload = _cached_account(account)
    return [
        {
            "currency": currency,
            "net_assets": payload.get("portfolio_value"),
            "total_cash": payload.get("cash"),
            "buy_power": payload.get("buying_power"),
            "risk_level": "blocked" if payload.get("trading_blocked") else "normal",
            "raw": payload,
        }
    ]


def positions(account: BrokerAccount) -> list[dict[str, Any]]:
    rows = _request(account, "GET", "/v2/positions")
    if not isinstance(rows, list):
        return []
    normalized = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
        quantity = row.get("qty")
        normalized.append(
            {
                **row,
                "symbol": symbol,
                "stock_symbol": symbol,
                "quantity": quantity,
                "available_quantity": row.get("qty_available") or quantity,
                "broker": "alpaca",
            }
        )
    return normalized


def today_orders(account: BrokerAccount) -> list[dict[str, Any]]:
    rows = _request(account, "GET", "/v2/orders?status=open&limit=500&nested=false")
    return rows if isinstance(rows, list) else []


def cancel_order(account: BrokerAccount, order_id: str) -> dict[str, Any]:
    _request(account, "DELETE", f"/v2/orders/{order_id}", expect_json=False)
    return {"order_id": order_id, "status": "cancel_requested", "broker": "alpaca"}


def submit_buy_order(
    account: BrokerAccount,
    symbol: str,
    quantity: int,
    price: float | None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    return submit_order(account, symbol, quantity, "buy", price=price, remark=remark, order_type=order_type)


def submit_sell_order(
    account: BrokerAccount,
    symbol: str,
    quantity: int,
    price: float | None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    return submit_order(account, symbol, quantity, "sell", price=price, remark=remark, order_type=order_type)


def submit_market_order(account: BrokerAccount, symbol: str, quantity: Decimal | int | float | str, side: str, remark: str | None = None) -> dict[str, Any]:
    return submit_order(account, symbol, quantity, side, price=None, remark=remark, order_type="market")


def submit_order(
    account: BrokerAccount,
    symbol: str,
    quantity: Decimal | int | float | str,
    side: str,
    *,
    price: float | None = None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise AlpacaError(f"unsupported order side `{side}`")
    normalized_order_type = _normalize_order_type(order_type)
    body: dict[str, Any] = {
        "symbol": _alpaca_symbol(symbol),
        "qty": _quantity_text(quantity),
        "side": normalized_side,
        "type": normalized_order_type,
        "time_in_force": "day",
    }
    if normalized_order_type == "limit":
        if not price or float(price) <= 0:
            raise AlpacaError("limit order requires a positive price")
        body["limit_price"] = _price(float(price))
    if remark:
        body["client_order_id"] = _client_order_id(remark)
    payload = _request(account, "POST", "/v2/orders", body=body)
    return _normalize_order(payload)


def submit_stop_sell_order(account: BrokerAccount, symbol: str, quantity: int, trigger_price: float, remark: str | None = None) -> dict[str, Any]:
    body = {
        "symbol": _alpaca_symbol(symbol),
        "qty": _quantity_text(quantity),
        "side": "sell",
        "type": "stop",
        "stop_price": _price(trigger_price),
        "time_in_force": "day",
    }
    if remark:
        body["client_order_id"] = _client_order_id(remark)
    return _normalize_order(_request(account, "POST", "/v2/orders", body=body))


def order_detail(account: BrokerAccount, order_id: str) -> dict[str, Any]:
    return _normalize_order(_request(account, "GET", f"/v2/orders/{order_id}"))


def wait_for_order_fill(account: BrokerAccount, order_id: str, timeout_seconds: float = 8) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    detail: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        detail = order_detail(account, order_id)
        status = str(detail.get("status") or "").lower()
        filled_quantity = float(detail.get("executed_quantity") or detail.get("filled_quantity") or 0)
        is_filled_status = "filled" in status and "unfilled" not in status
        if is_filled_status or filled_quantity > 0 or status in {"canceled", "cancelled", "rejected", "expired"}:
            return detail
        time.sleep(1)
    return detail


def _request(account: BrokerAccount, method: str, path: str, body: dict[str, Any] | None = None, expect_json: bool = True) -> Any:
    api_key, api_secret = get_broker_credentials(account)
    base_url = "https://paper-api.alpaca.markets" if account.paper else "https://api.alpaca.markets"
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read().decode("utf-8", errors="replace")
            touch_broker_account(account)
            if not expect_json or not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise AlpacaError(_error_message(raw) or f"Alpaca HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise AlpacaError(str(exc.reason)) from exc


def _normalize_order(payload: dict[str, Any]) -> dict[str, Any]:
    filled_quantity = payload.get("filled_qty") or payload.get("filled_quantity") or 0
    executed_price = payload.get("filled_avg_price") or payload.get("executed_price")
    return {
        **payload,
        "order_id": payload.get("id") or payload.get("order_id"),
        "order_status": payload.get("status"),
        "executed_quantity": filled_quantity,
        "filled_quantity": filled_quantity,
        "executed_price": executed_price,
        "broker": "alpaca",
    }


def _alpaca_symbol(symbol: str) -> str:
    return option_symbol_for_occ(symbol)


def _normalize_order_type(value: str | None) -> str:
    # An unrecognized NON-EMPTY order_type must not silently become "market":
    # that strips price protection (a typo'd "lmt"/"stop_limit" would fire a
    # market order at any slippage). None/"" keeps the documented "market"
    # default; known tokens map; anything else is rejected loudly.
    if value is None or not str(value).strip():
        return "market"
    normalized = str(value).strip().lower()
    if normalized in {"limit", "lo"}:
        return "limit"
    if normalized in {"market", "mo"}:
        return "market"
    raise AlpacaError(f"unsupported order_type `{value}`")


def _quantity_text(value: Decimal | int | float | str) -> str:
    try:
        quantity = Decimal(str(value)).copy_abs()
    except (InvalidOperation, ValueError) as exc:
        raise AlpacaError(f"invalid order quantity `{value}`") from exc
    if quantity <= 0:
        raise AlpacaError("order quantity must be greater than zero")
    if quantity == quantity.to_integral_value():
        return str(quantity.to_integral_value())
    return format(quantity.normalize(), "f")


def _price(value: float) -> str:
    # Decimal round-half-up so half-cents go the right way: float "%.2f" mis-rounds
    # e.g. 2.675 -> "2.67" (2.675 is not exactly representable in binary). Submitting
    # a limit price one cent off is a real fill-quality bug, and Decimal also matches
    # the convention in _quantity_text.
    try:
        quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        quantized = Decimal("0.01")
    return str(max(quantized, Decimal("0.01")))


def _client_order_id(remark: str) -> str:
    import hashlib
    import re

    text = str(remark or "")
    # If the caller embedded a stable idempotency key as "[cok:KEY]", use it
    # verbatim as the broker client_order_id so retries of the same logical
    # order dedup broker-side (the surrounding remark may carry a reprice
    # counter that must NOT change the id).
    match = re.search(r"\[cok:([A-Za-z0-9_.:-]{1,48})\]", text)
    if match:
        return match.group(1)[:48]
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", text)[:32].strip("-") or "AI-OPTION"
    suffix = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{cleaned}-{suffix}"[:48]


def _error_message(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or payload)
    return str(payload)

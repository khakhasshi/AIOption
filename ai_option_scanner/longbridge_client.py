from __future__ import annotations

import json
import subprocess
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .account_store import env_for_account, touch_account
from . import longbridge_sdk_client as sdk_client
from .concurrency import longbridge_cli_limiter
from .longbridge_option_tool import quote_option_contract as lb_quote_option_contract
from .option_symbol_utils import option_symbol_for_longbridge, option_symbol_for_occ


class LongbridgeError(RuntimeError):
    pass


_account_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_account_cache_lock = threading.Lock()
_ACCOUNT_CACHE_TTL_SECONDS = 30.0


def _cached_account_api(account_name: str | None, fetcher, *, key: str | None = None) -> Any:
    cache_key = str(key or account_name or "__default__")
    now = time.monotonic()
    with _account_cache_lock:
        cached = _account_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < _ACCOUNT_CACHE_TTL_SECONDS:
            return cached[1]
    result = fetcher()
    with _account_cache_lock:
        _account_cache[cache_key] = (now, result)
    return result


def run_longbridge(
    args: list[str],
    account_name: str | None = None,
    touch: bool = True,
    timeout: float = 25,
    retries: int = 0,
) -> Any:
    account, env = env_for_account(account_name)
    command = ["longbridge", *args, "--format", "json"]
    attempts = max(1, int(retries or 0) + 1)
    last_error = ""
    with longbridge_cli_limiter.acquire():
        for attempt in range(attempts):
            try:
                completed = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=env,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise LongbridgeError(f"`{' '.join(command)}` timed out after {timeout:g}s") from exc
            if completed.returncode == 0:
                break
            last_error = completed.stderr.strip() or completed.stdout.strip()
            if attempt >= attempts - 1 or not _is_connection_limit_error(last_error):
                raise LongbridgeError(last_error)
            time.sleep(min(2 + attempt * 2, 8))

    payload = _extract_json_payload(completed.stdout)
    if touch:
        touch_account(account.name)
    if not payload:
        return None
    return json.loads(payload)


def quote(symbol: str, account_name: str | None = None) -> dict[str, Any]:
    sdk_value = _try_sdk("quote", sdk_client.quote, symbol, account_name)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["quote", symbol], account_name=account_name, timeout=25, retries=3)
    return data[0] if isinstance(data, list) and data else {}


def kline(symbol: str, count: int = 80, account_name: str | None = None) -> list[dict[str, Any]]:
    sdk_value = _try_sdk("kline", sdk_client.kline, symbol, count, account_name)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["kline", symbol, "--period", "day", "--count", str(count)], account_name=account_name, timeout=25, retries=3)
    return data if isinstance(data, list) else []


def intraday(symbol: str, account_name: str | None = None) -> list[dict[str, Any]]:
    sdk_value = _try_sdk("intraday", sdk_client.intraday, symbol, account_name)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["intraday", symbol], account_name=account_name, timeout=25, retries=3)
    return data if isinstance(data, list) else []


def news(symbol: str, account_name: str | None = None) -> list[dict[str, Any]]:
    sdk_value = _try_sdk("news", sdk_client.news, symbol, account_name)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["news", symbol], account_name=account_name, timeout=30, retries=3)
    return data if isinstance(data, list) else []


def assets(account_name: str | None = None, currency: str = "USD") -> list[dict[str, Any]]:
    cache_key = f"assets:{account_name or ''}:{currency}"
    return _cached_account_api(account_name, lambda: _assets_uncached(account_name, currency), key=cache_key)

def _assets_uncached(account_name: str | None, currency: str) -> list[dict[str, Any]]:
    sdk_value = _try_sdk("assets", sdk_client.assets, account_name, currency)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["assets", "--currency", currency], account_name=account_name, timeout=25)
    return data if isinstance(data, list) else []


def executions(
    account_name: str | None = None,
    start: str | None = None,
    end: str | None = None,
    history: bool = False,
) -> list[dict[str, Any]]:
    sdk_value = _try_sdk("executions", sdk_client.executions, account_name, start, end, history)
    if sdk_value is not None:
        return sdk_value
    args = ["order", "executions"]
    if history:
        args.append("--history")
    if start:
        args.extend(["--start", start])
    if end:
        args.extend(["--end", end])
    data = run_longbridge(args, account_name=account_name, timeout=25)
    return data if isinstance(data, list) else []


def today_orders(account_name: str | None = None) -> list[dict[str, Any]]:
    sdk_value = _try_sdk("today_orders", sdk_client.today_orders, account_name)
    if sdk_value is not None:
        return sdk_value
    raise LongbridgeError("today_orders requires the Longbridge Python SDK backend")


def cancel_order(order_id: str, account_name: str | None = None) -> dict[str, Any]:
    sdk_value = _try_sdk("cancel_order", sdk_client.cancel_order, order_id, account_name)
    if sdk_value is not None:
        return sdk_value
    raise LongbridgeError("cancel_order requires the Longbridge Python SDK backend")


def positions(account_name: str | None = None) -> list[dict[str, Any]]:
    sdk_value = _try_sdk("positions", sdk_client.positions, account_name)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["positions"], account_name=account_name, timeout=25)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        rows = data.get("positions")
        return rows if isinstance(rows, list) else [data]
    return []


def check(account_name: str | None = None) -> dict[str, Any]:
    return _cached_account_api(account_name, lambda: _check_uncached(account_name), key=f"check:{account_name or ''}")

def _check_uncached(account_name: str | None) -> dict[str, Any]:
    sdk_value = _try_sdk("check", sdk_client.check, account_name)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["check"], account_name=account_name, touch=False, timeout=15)
    return data if isinstance(data, dict) else {}


def logout(account_name: str | None = None) -> dict[str, Any]:
    sdk_value = _try_sdk("logout", sdk_client.logout, account_name)
    if sdk_value is not None:
        return sdk_value
    account, env = env_for_account(account_name)
    try:
        completed = subprocess.run(
            ["longbridge", "auth", "logout"],
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise LongbridgeError("`longbridge auth logout` timed out after 15s") from exc
    if completed.returncode != 0:
        raise LongbridgeError(completed.stderr.strip() or completed.stdout.strip())
    touch_account(account.name)
    return {"message": completed.stdout.strip() or "logged out"}


def option_order_symbol(yfinance_contract_symbol: str) -> str:
    return option_symbol_for_longbridge(yfinance_contract_symbol)


def quote_option_contract(contract_symbol: str, account_name: str | None = None) -> dict[str, Any]:
    errors: dict[str, str] = {}
    try:
        from .thetadata_option_tool import quote_option_contract_live as theta_quote_option_contract

        theta_value = theta_quote_option_contract(_yfinance_contract_symbol(contract_symbol))
        if theta_value.get("available"):
            theta_value["source"] = "thetadata"
            theta_value.setdefault("provider_source", theta_value.get("pricing_source") or "thetadata_option_quote_live")
            return theta_value
        errors["thetadata"] = str(theta_value.get("error") or theta_value.get("quote_warning") or "ThetaData option quote unavailable")
    except Exception as exc:  # noqa: BLE001 - execution quotes can fall back to Longbridge.
        errors["thetadata"] = str(exc)

    try:
        sdk_value = _try_sdk(
            "quote_option_contract",
            lb_quote_option_contract,
            option_order_symbol(contract_symbol),
            account_name,
        )
    except Exception as exc:  # noqa: BLE001 - yfinance remains analysis-only fallback.
        sdk_value = None
        errors["longbridge"] = str(exc)
    if sdk_value is not None and sdk_value.get("available"):
        sdk_value["source"] = "longbridge_sdk"
        sdk_value["fallback_from"] = "thetadata"
        sdk_value["fallback_errors"] = dict(errors)
        sdk_value.setdefault("provider_source", sdk_value.get("pricing_source") or "longbridge_option_quote")
        return sdk_value

    try:
        from .yfinance_option_tool import quote_option_contract as yf_quote_option_contract

        yf_value = yf_quote_option_contract(_yfinance_contract_symbol(contract_symbol))
        if yf_value.get("available"):
            yf_value["source"] = "yfinance"
            yf_value["fallback_from"] = "thetadata,longbridge"
            yf_value["fallback_errors"] = dict(errors)
            yf_value["execution_trusted"] = False
            yf_value.setdefault("provider_source", yf_value.get("pricing_source") or "yfinance_option_quote")
            return yf_value
        errors["yfinance"] = str(yf_value.get("error") or yf_value.get("quote_warning") or "yfinance option quote unavailable")
    except Exception as exc:  # noqa: BLE001
        errors["yfinance"] = str(exc)
    raise LongbridgeError("; ".join(f"{source}: {error}" for source, error in errors.items()) or "option quote unavailable")


def _yfinance_contract_symbol(contract_symbol: str) -> str:
    return option_symbol_for_occ(contract_symbol)


def submit_buy_order(
    symbol: str,
    quantity: int,
    price: float | None,
    account_name: str | None = None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    sdk_value = _try_sdk("submit_buy_order", sdk_client.submit_buy_order, symbol, quantity, price, account_name, remark, order_type)
    if sdk_value is not None:
        return sdk_value
    normalized_order_type = _normalize_order_type(order_type)
    args = [
        "order",
        "buy",
        symbol,
        str(quantity),
        "--tif",
        "Day",
        "--yes",
    ]
    if normalized_order_type == "limit":
        args.extend([
            "--price",
            _price(float(price or 0)),
            "--order-type",
            "LO",
        ])
    else:
        args.extend([
            "--order-type",
            "MO",
        ])
    if remark:
        args.extend(["--remark", remark[:255]])
    data = run_longbridge(args, account_name=account_name, timeout=30)
    return data if isinstance(data, dict) else {"raw": data}


def submit_sell_order(
    symbol: str,
    quantity: int,
    price: float | None,
    account_name: str | None = None,
    remark: str | None = None,
    order_type: str = "market",
) -> dict[str, Any]:
    sdk_value = _try_sdk("submit_sell_order", sdk_client.submit_sell_order, symbol, quantity, price, account_name, remark, order_type)
    if sdk_value is not None:
        return sdk_value
    normalized_order_type = _normalize_order_type(order_type)
    args = [
        "order",
        "sell",
        symbol,
        str(quantity),
        "--tif",
        "Day",
        "--yes",
    ]
    if normalized_order_type == "limit":
        args.extend([
            "--price",
            _price(float(price or 0)),
            "--order-type",
            "LO",
        ])
    else:
        args.extend([
            "--order-type",
            "MO",
        ])
    if remark:
        args.extend(["--remark", remark[:255]])
    data = run_longbridge(args, account_name=account_name, timeout=30)
    return data if isinstance(data, dict) else {"raw": data}


def submit_market_order(
    symbol: str,
    quantity: Decimal | int | float | str,
    side: str,
    account_name: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise LongbridgeError(f"unsupported order side `{side}`")
    sdk_value = _try_sdk("submit_market_order", sdk_client.submit_market_order, symbol, quantity, normalized_side, account_name, remark)
    if sdk_value is not None:
        return sdk_value
    args = [
        "order",
        normalized_side,
        symbol,
        _quantity_text(quantity),
        "--order-type",
        "MO",
        "--tif",
        "Day",
        "--yes",
    ]
    if remark:
        args.extend(["--remark", remark[:255]])
    data = run_longbridge(args, account_name=account_name, timeout=30)
    return data if isinstance(data, dict) else {"raw": data}


def submit_stop_sell_order(
    symbol: str,
    quantity: int,
    trigger_price: float,
    account_name: str | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    sdk_value = _try_sdk("submit_stop_sell_order", sdk_client.submit_stop_sell_order, symbol, quantity, trigger_price, account_name, remark)
    if sdk_value is not None:
        return sdk_value
    args = [
        "order",
        "sell",
        symbol,
        str(quantity),
        "--order-type",
        "MIT",
        "--trigger-price",
        _price(trigger_price),
        "--tif",
        "Day",
        "--yes",
    ]
    if remark:
        args.extend(["--remark", remark[:255]])
    data = run_longbridge(args, account_name=account_name, timeout=30)
    return data if isinstance(data, dict) else {"raw": data}


def order_detail(order_id: str, account_name: str | None = None) -> dict[str, Any]:
    sdk_value = _try_sdk("order_detail", sdk_client.order_detail, order_id, account_name)
    if sdk_value is not None:
        return sdk_value
    data = run_longbridge(["order", "detail", order_id], account_name=account_name, timeout=20)
    return data if isinstance(data, dict) else {"raw": data}


def wait_for_order_fill(order_id: str, account_name: str | None = None, timeout_seconds: float = 8) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    detail: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        detail = order_detail(order_id, account_name)
        status = str(detail.get("status") or detail.get("order_status") or "").lower()
        filled_quantity = float(detail.get("executed_quantity") or detail.get("filled_quantity") or detail.get("filled_qty") or 0)
        if "filled" in status or filled_quantity > 0:
            return detail
        time.sleep(1)
    return detail


def _price(value: float) -> str:
    return f"{max(float(value), 0.01):.2f}"


def _quantity_text(value: Decimal | int | float | str) -> str:
    try:
        quantity = Decimal(str(value)).copy_abs()
    except (InvalidOperation, ValueError) as exc:
        raise LongbridgeError(f"invalid order quantity `{value}`") from exc
    if quantity <= 0:
        raise LongbridgeError("order quantity must be greater than zero")
    if quantity == quantity.to_integral_value():
        return str(quantity.to_integral_value())
    return format(quantity.normalize(), "f")


def _normalize_order_type(value: str | None) -> str:
    normalized = str(value or "market").strip().lower()
    return "market" if normalized in {"market", "mo"} else "limit"


def _extract_json_payload(stdout: str) -> str:
    payload = stdout.split("\nNew version", 1)[0].strip()
    if not payload:
        return ""
    try:
        json.loads(payload)
        return payload
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for marker in ("{", "[", '"'):
        index = payload.find(marker)
        if index < 0:
            continue
        try:
            _, end = decoder.raw_decode(payload[index:])
            return payload[index : index + end]
        except json.JSONDecodeError:
            continue

    for index, char in enumerate(payload):
        if not (char.isdigit() or char == "-"):
            continue
        try:
            _, end = decoder.raw_decode(payload[index:])
            return payload[index : index + end]
        except json.JSONDecodeError:
            continue
    return payload


def _is_connection_limit_error(message: str) -> bool:
    lowered = message.lower()
    return "connections limitation is hit" in lowered or "websocket error" in lowered and "code=403" in lowered


def _try_sdk(operation: str, callback: Any, *args: Any) -> Any:
    if not sdk_client.sdk_is_allowed():
        return None
    try:
        return callback(*args)
    except sdk_client.LongbridgeSDKUnavailable as exc:
        if sdk_client.sdk_is_required():
            raise LongbridgeError(str(exc)) from exc
        return None
    except sdk_client.LongbridgeSDKError as exc:
        raise LongbridgeError(str(exc)) from exc

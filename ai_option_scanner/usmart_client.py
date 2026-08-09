"""uSMART (盈立证券) Open API broker client.

Mirrors the alpaca_client surface (check / assets / positions / today_orders /
cancel_order / submit_* / order_detail / wait_for_order_fill) so broker_client
can dispatch to it like any other broker. Unlike Alpaca's static-header auth,
uSMART uses MD5withRSA request signing PLUS a stateful login token:

  1. Each request body is signed with the channel's RSA *signing* private key
     (MD5withRSA) → urlsafe-base64 → X-Sign header.
  2. Sensitive fields (password, phone) are RSA-encrypted with a *different*
     public key before going in the body.
  3. A login token (Authorization header) is obtained via /login + trade-login,
     cached with its expiration, and refreshed on demand.

Docs: https://api-doc.usmart8.com/zh-cn/ (trade.html / quote-base.html).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .broker_store import (
    BrokerAccount,
    get_usmart_credentials,
    touch_broker_account,
    update_broker_account_status,
)


class USmartError(RuntimeError):
    pass


_LOG = logging.getLogger(__name__)


# Base hosts (production). UAT hosts exist but production is the default; a paper
# account flag flips the trading host to the UAT endpoint for safe rehearsal.
_TRADE_HOST = "https://open-jy.yxzq.com"
_TRADE_HOST_UAT = "http://open-jy-uat.yxzq.com"

# Service path prefixes (module-split, per the uSMART spec).
_P_USER = "/user-server/open-api"
_P_ORDER = "/stock-order-server/open-api"
_P_ASSET = "/asset-center-server/open-api"

# Order endpoint paths. Equities use the entrust-order family. uSMART docs
# (v1.18, section 8) add SEPARATE option-trading endpoints whose exact paths were
# truncated in the published reference; until confirmed against the full section 8
# spec, options are routed through the same entrust-order family with
# exchangeType=51 (the asset feed already returns options that way). If uSMART's
# section 8 uses distinct paths, override these four constants — the request
# bodies are otherwise identical.
_PATH_PLACE = f"{_P_ORDER}/entrust-order"
_PATH_MODIFY = f"{_P_ORDER}/modify-order"
_PATH_DETAIL = f"{_P_ORDER}/order-detail"
_PATH_TODAY = f"{_P_ORDER}/today-entrust"
# TODO(usmart-section8): set distinct option paths once the full spec is supplied,
# e.g. _PATH_OPTION_PLACE = f"{_P_ORDER}/option-entrust-order". When set,
# _place_path()/_modify_path()/etc. branch on exchangeType == _EXCHANGE_OPTION and
# _guard_option_routing() goes quiet (it only fires while these are None). Web
# research could not recover the truncated section-8 paths; they require the
# authenticated uSMART API console. Until then option orders are REFUSED by default
# (fail-closed) — see _guard_option_routing.
_PATH_OPTION_PLACE: str | None = None
_PATH_OPTION_MODIFY: str | None = None
_PATH_OPTION_DETAIL: str | None = None
_PATH_OPTION_TODAY: str | None = None

# Exchange type codes.
_EXCHANGE_US = "5"
_EXCHANGE_HK = "0"
_EXCHANGE_OPTION = "51"
_EXCHANGE_ALL = "100"

# entrustType: 0 buy, 1 sell. entrustProp (US): "0" limit, "w" market.
_SIDE_BUY = 0
_SIDE_SELL = 1

_HTTP_TIMEOUT = 25.0

# Login token cache, keyed by account ref. Thread-safe; refreshed when expired.
_token_cache: dict[str, tuple[float, str]] = {}
_token_lock = threading.Lock()
# Refresh a bit before the stated expiration to avoid mid-flight expiry.
_TOKEN_REFRESH_SKEW_SECONDS = 60.0


# --------------------------------------------------------------------------- #
# Signing + headers
# --------------------------------------------------------------------------- #
def _load_private_key(pem: str):
    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as exc:  # noqa: BLE001
        raise USmartError(f"invalid uSMART signing private key: {exc}") from exc


def _load_public_key(pem: str):
    try:
        return serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise USmartError(f"invalid uSMART encrypt public key: {exc}") from exc


def _sign_body(body_text: str, sign_private_key_pem: str) -> str:
    """MD5withRSA over the request body → urlsafe base64, for the X-Sign header."""
    key = _load_private_key(sign_private_key_pem)
    signature = key.sign(body_text.encode("utf-8"), padding.PKCS1v15(), hashes.MD5())
    return base64.urlsafe_b64encode(signature).decode("ascii")


def _rsa_encrypt_field(plaintext: str, encrypt_public_key_pem: str) -> str:
    """RSA-encrypt a sensitive field (password / phone) with the encrypt public
    key (distinct from the signing key) → urlsafe base64."""
    key = _load_public_key(encrypt_public_key_pem)
    ciphertext = key.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())
    return base64.urlsafe_b64encode(ciphertext).decode("ascii")


def _request_id() -> str:
    # Spec wants a ~19-30 digit unique id (snowflake recommended). A zero-padded
    # uuid4 int, truncated to 30 digits, is unique enough and dependency-free.
    return f"{uuid.uuid4().int:030d}"[:30]


def _headers(cred: dict[str, str], token: str | None, body_text: str) -> dict[str, str]:
    return {
        "Authorization": token or "",
        "X-Lang": "3",  # English; the response envelope is language-agnostic.
        "X-Request-Id": _request_id(),
        "X-Channel": cred["channel"],
        "X-Time": str(int(time.time() * 1000)),
        "X-Dt": "t3",  # other/server
        "X-Type": "2",  # HK app type
        "X-Sign": _sign_body(body_text, cred["sign_private_key"]),
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }


# --------------------------------------------------------------------------- #
# HTTP + envelope
# --------------------------------------------------------------------------- #
def _host(account: BrokerAccount) -> str:
    return _TRADE_HOST_UAT if account.paper else _TRADE_HOST


def _post_raw(account: BrokerAccount, cred: dict[str, str], path: str, body: dict[str, Any], token: str | None) -> dict[str, Any]:
    """Single signed POST. Returns the parsed `{code,data,msg}` envelope or raises."""
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    request = urllib.request.Request(
        f"{_host(account)}{path}",
        data=body_text.encode("utf-8"),
        method="POST",
        headers=_headers(cred, token, body_text),
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise USmartError(_error_message(detail) or f"uSMART HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise USmartError(str(exc.reason)) from exc
    if not raw.strip():
        return {"code": 0, "data": {}, "msg": ""}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise USmartError(f"uSMART returned non-JSON response: {raw[:200]}") from exc


def _is_auth_error(envelope: dict[str, Any]) -> bool:
    """Token expired / unauthorized → triggers a single relogin+retry."""
    code = envelope.get("code")
    msg = str(envelope.get("msg") or "").lower()
    return code in {401, 1001, 1002} or "token" in msg and ("expire" in msg or "invalid" in msg or "unauth" in msg)


def _request(account: BrokerAccount, path: str, body: dict[str, Any] | None = None) -> Any:
    """Signed, authenticated POST with one automatic relogin on auth failure.
    Returns the `data` payload on success (code==0), else raises USmartError."""
    cred = get_usmart_credentials(account)
    payload = dict(body or {})
    token = _get_token(account, cred)
    envelope = _post_raw(account, cred, path, payload, token)
    if _is_auth_error(envelope):
        token = _get_token(account, cred, force=True)
        envelope = _post_raw(account, cred, path, payload, token)
    code = envelope.get("code")
    if code != 0:
        raise USmartError(f"uSMART {path} failed (code={code}): {envelope.get('msg') or 'unknown error'}")
    touch_broker_account(account)
    return envelope.get("data")


# --------------------------------------------------------------------------- #
# Login + token cache
# --------------------------------------------------------------------------- #
def _get_token(account: BrokerAccount, cred: dict[str, str], force: bool = False) -> str:
    key = f"{account.broker}:{account.owner_id}:{account.name}"
    now = time.monotonic()
    if not force:
        with _token_lock:
            cached = _token_cache.get(key)
        if cached is not None and now < cached[0]:
            return cached[1]
    token, ttl_seconds = _login(account, cred)
    expires_at = now + max(ttl_seconds - _TOKEN_REFRESH_SKEW_SECONDS, 30.0)
    with _token_lock:
        _token_cache[key] = (expires_at, token)
    return token


def _login(account: BrokerAccount, cred: dict[str, str]) -> tuple[str, float]:
    """Password login → token, then trade-login to unlock trading. Returns
    (token, ttl_seconds). Sensitive fields are RSA-encrypted with the encrypt key."""
    enc_pub = cred["encrypt_public_key"]
    body = {
        "areaCode": cred["area_code"],
        "phoneNumber": _rsa_encrypt_field(cred["phone"], enc_pub),
        "password": _rsa_encrypt_field(cred["trade_password"], enc_pub) if cred["trade_password"] else "",
    }
    # Login itself is signed but unauthenticated (no token yet).
    envelope = _post_raw(account, cred, f"{_P_USER}/login", body, token=None)
    if envelope.get("code") != 0:
        raise USmartError(f"uSMART login failed (code={envelope.get('code')}): {envelope.get('msg') or 'unknown'}")
    data = envelope.get("data") or {}
    token = str(data.get("token") or "")
    if not token:
        raise USmartError("uSMART login returned no token")
    ttl_seconds = _expiration_to_ttl(data.get("expiration"))
    # Best-effort trade unlock so subsequent order calls are permitted. The trade
    # password (if set) is RSA-encrypted; failures here surface on the order call.
    try:
        _post_raw(account, cred, f"{_P_USER}/trade-login", {
            "tradePassword": _rsa_encrypt_field(cred["trade_password"], enc_pub) if cred["trade_password"] else "",
        }, token=token)
    except USmartError:
        pass
    return token, ttl_seconds


def _expiration_to_ttl(expiration: Any) -> float:
    """uSMART `expiration` may be an epoch (ms or s) or a duration in seconds.
    Be conservative: derive a sane positive TTL, default 30 minutes."""
    try:
        value = float(expiration)
    except (TypeError, ValueError):
        return 1800.0
    now_s = time.time()
    if value > now_s * 1000:  # epoch milliseconds
        return max((value / 1000.0) - now_s, 60.0)
    if value > now_s:  # epoch seconds
        return max(value - now_s, 60.0)
    if value > 0:  # duration seconds
        return value
    return 1800.0


def check(account: BrokerAccount) -> dict[str, Any]:
    """Login + trade-status probe; updates the stored account status."""
    cred = get_usmart_credentials(account)
    try:
        token = _get_token(account, cred, force=True)
        status_data = _request(account, f"{_P_USER}/get-trade-status", {})
        unlocked = bool((status_data or {}).get("status"))
        status = "active" if unlocked else "locked"
    except USmartError as exc:
        update_broker_account_status(account, "error", {"detail": str(exc)})
        return {"broker": "usmart", "status": "error", "detail": str(exc), "session": {"token": "invalid"}}
    update_broker_account_status(account, status, {"trade_unlocked": unlocked})
    return {
        "broker": "usmart",
        "status": status,
        "supports_native_stop": False,
        "session": {"token": "valid" if token else "unknown"},
    }


def _error_message(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if isinstance(payload, dict):
        return str(payload.get("msg") or payload.get("message") or payload.get("error") or payload)
    return str(payload)


# --------------------------------------------------------------------------- #
# Symbol + exchange resolution
# --------------------------------------------------------------------------- #
def _is_option(symbol: str) -> bool:
    from .option_symbol_utils import parse_option_symbol

    return parse_option_symbol(symbol) is not None


def _stock_code_and_exchange(symbol: str) -> tuple[str, str]:
    """Map a unified symbol to uSMART (stockCode, exchangeType). Options use the
    OCC-style code with exchangeType 51; equities default to US."""
    from .option_symbol_utils import option_symbol_for_occ

    text = str(symbol or "").strip().upper()
    if _is_option(text):
        return option_symbol_for_occ(text), _EXCHANGE_OPTION
    # Equity: strip a Longbridge-style ".US"/".HK" suffix and pick the exchange.
    if text.endswith(".HK"):
        return text[:-3], _EXCHANGE_HK
    if text.endswith(".US"):
        return text[:-3], _EXCHANGE_US
    return text, _EXCHANGE_US


def _serial_no(remark: str | None) -> int:
    """Stable int64 serialNo for broker-side idempotency. If the caller embedded a
    [cok:KEY] idempotency key, hash it to a positive int64 so retries of the same
    logical order dedup; otherwise derive a unique-ish id from time+uuid."""
    import hashlib
    import re

    text = str(remark or "")
    match = re.search(r"\[cok:([A-Za-z0-9_.:-]{1,48})\]", text)
    if match:
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF
    return uuid.uuid4().int & 0x7FFFFFFFFFFFFFFF


def _quantity_int(value: Decimal | int | float | str) -> int:
    try:
        quantity = Decimal(str(value)).copy_abs()
    except (InvalidOperation, ValueError) as exc:
        raise USmartError(f"invalid order quantity `{value}`") from exc
    if quantity <= 0:
        raise USmartError("order quantity must be greater than zero")
    return int(quantity.to_integral_value())


def _price_text(value: float | None) -> str:
    return f"{max(float(value or 0), 0):.4f}".rstrip("0").rstrip(".") or "0"


def _option_paths_configured() -> bool:
    return bool(_PATH_OPTION_PLACE)


def _guard_option_routing(exchange_type: str) -> None:
    """Surface the section-8 gap instead of silently routing an option order
    through the equity entrust-order family.

    The distinct uSMART option endpoints (_PATH_OPTION_*) are unset because the
    published spec truncated section 8. Until they are configured, an option order
    would fall back to the equity path with exchangeType=51 — which the asset feed
    suggests is accepted, but is UNCONFIRMED for live order submission. Because this
    submits real broker orders, the safe default is fail-closed:
      - default (or AI_OPTION_USMART_OPTION_FAIL_CLOSED unset/true): refuse the
        order rather than route it through an unverified endpoint;
      - opt-in (AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK=true): allow the
        fallback but log a WARNING so it is visible in logs/audits.
    """
    if exchange_type != _EXCHANGE_OPTION or _option_paths_configured():
        return
    allow_fallback = (os.getenv("AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK", "false") or "").strip().lower() in {"1", "true", "yes", "on"}
    fail_closed_env = (os.getenv("AI_OPTION_USMART_OPTION_FAIL_CLOSED", "") or "").strip().lower()
    fail_closed = fail_closed_env in {"1", "true", "yes", "on"} or (fail_closed_env == "" and not allow_fallback)
    if fail_closed:
        raise USmartError(
            "usmart_option_path_unconfigured: distinct uSMART option endpoints are not set "
            "(section-8 spec missing), so an option order cannot be routed through a verified "
            "endpoint. Refusing by default — set AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK=true "
            "to accept the unconfirmed equity entrust-order path (exchangeType=51) at your own risk."
        )
    _LOG.warning(
        "uSMART option order routed through the equity entrust-order path (exchangeType=51); "
        "distinct option endpoints are unconfigured (section-8 spec missing) and "
        "AI_OPTION_USMART_OPTION_ALLOW_EQUITY_FALLBACK is on. Set _PATH_OPTION_* once the spec "
        "is available to route through the verified endpoint."
    )


def _place_path(exchange_type: str) -> str:
    if exchange_type == _EXCHANGE_OPTION and _PATH_OPTION_PLACE:
        return _PATH_OPTION_PLACE
    return _PATH_PLACE


def _modify_path(exchange_type: str) -> str:
    if exchange_type == _EXCHANGE_OPTION and _PATH_OPTION_MODIFY:
        return _PATH_OPTION_MODIFY
    return _PATH_MODIFY


def _detail_path(exchange_type: str) -> str:
    if exchange_type == _EXCHANGE_OPTION and _PATH_OPTION_DETAIL:
        return _PATH_OPTION_DETAIL
    return _PATH_DETAIL


# --------------------------------------------------------------------------- #
# Account / positions / orders
# --------------------------------------------------------------------------- #
def _asset_query(account: BrokerAccount, currency: str) -> dict[str, Any]:
    money_type = {"CNY": "0", "USD": "1", "HKD": "2"}.get(str(currency or "USD").upper(), "1")
    data = _request(account, f"{_P_ASSET}/open-assetQuery/v1", {"moneyType": money_type})
    return data if isinstance(data, dict) else {}


def assets(account: BrokerAccount, currency: str = "USD") -> list[dict[str, Any]]:
    data = _asset_query(account, currency)
    accounts = data.get("assetSingleInfoRespVOS") or []
    primary = accounts[0] if accounts else {}
    return [
        {
            "currency": currency,
            "net_assets": data.get("totalAssetValue") or primary.get("asset"),
            "total_cash": data.get("totalCashBalance") or primary.get("cashBalance"),
            "buy_power": primary.get("purchasePower") or primary.get("maxPurchasingPower"),
            "market_value": data.get("totalMarketValue") or primary.get("marketValue"),
            "risk_level": primary.get("riskStatusCode") or "normal",
            "raw": data,
        }
    ]


def positions(account: BrokerAccount) -> list[dict[str, Any]]:
    data = _asset_query(account, "USD")
    out: list[dict[str, Any]] = []
    for sub in data.get("assetSingleInfoRespVOS") or []:
        for hold in sub.get("holdInfos") or []:
            code = str(hold.get("code") or "").strip()
            quantity = hold.get("curHoldNum")
            out.append(
                {
                    **hold,
                    "symbol": code,
                    "stock_symbol": code,
                    "quantity": quantity,
                    "available_quantity": quantity,
                    "cost_price": hold.get("costPrice"),
                    "market_value": hold.get("marketValue"),
                    "multiplier": hold.get("multiplier"),
                    "broker": "usmart",
                }
            )
    return out


def today_orders(account: BrokerAccount) -> list[dict[str, Any]]:
    data = _request(account, _PATH_TODAY, {
        "exchangeType": _EXCHANGE_ALL,
        "pageNum": 1,
        "pageSize": 200,
    })
    rows = (data or {}).get("list") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    return [_normalize_order(row) for row in (rows or [])]


def cancel_order(account: BrokerAccount, order_id: str) -> dict[str, Any]:
    _request(account, _PATH_MODIFY, {
        "actionType": 0,  # cancel
        "entrustId": int(order_id),
        "entrustAmount": 0,
        "entrustPrice": 0,
    })
    return {"order_id": order_id, "status": "cancel_requested", "broker": "usmart"}


def modify_order(account: BrokerAccount, order_id: str, quantity: int, price: float) -> dict[str, Any]:
    data = _request(account, _PATH_MODIFY, {
        "actionType": 1,  # modify
        "entrustId": int(order_id),
        "entrustAmount": _quantity_int(quantity),
        "entrustPrice": _price_text(price),
    })
    return _normalize_order(data if isinstance(data, dict) else {"entrustId": order_id})


def submit_buy_order(account: BrokerAccount, symbol: str, quantity: int, price: float | None, remark: str | None = None, order_type: str = "market") -> dict[str, Any]:
    return submit_order(account, symbol, quantity, "buy", price=price, remark=remark, order_type=order_type)


def submit_sell_order(account: BrokerAccount, symbol: str, quantity: int, price: float | None, remark: str | None = None, order_type: str = "market") -> dict[str, Any]:
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
        raise USmartError(f"unsupported order side `{side}`")
    # Reject an unrecognized non-empty order_type rather than defaulting it: the
    # old `not in {market,mo}` treated a typo'd type as a LIMIT order (no fill if
    # mispriced), the mirror of Alpaca's market downgrade. None/"" → market.
    raw_type = str(order_type).strip().lower() if order_type is not None and str(order_type).strip() else "market"
    if raw_type not in {"market", "mo", "limit", "lo"}:
        raise USmartError(f"unsupported order_type `{order_type}`")
    is_market = raw_type in {"market", "mo"}
    stock_code, exchange_type = _stock_code_and_exchange(symbol)
    _guard_option_routing(exchange_type)
    # entrustProp: "w" market, "0" limit (US/options). entrustPrice 0 for market.
    entrust_prop = "w" if is_market else "0"
    if not is_market and (not price or float(price) <= 0):
        raise USmartError("limit order requires a positive price")
    body = {
        "serialNo": _serial_no(remark),
        "stockCode": stock_code,
        "exchangeType": exchange_type,
        "entrustType": _SIDE_BUY if normalized_side == "buy" else _SIDE_SELL,
        "entrustProp": entrust_prop,
        "entrustAmount": _quantity_int(quantity),
        "entrustPrice": 0 if is_market else _price_text(price),
    }
    data = _request(account, _place_path(exchange_type), body)
    return _normalize_order(data if isinstance(data, dict) else {})


def submit_stop_sell_order(account: BrokerAccount, symbol: str, quantity: int, trigger_price: float, remark: str | None = None) -> dict[str, Any]:
    """uSMART has no native stop order. Signal unsupported so the caller arms a
    software stop (trading_agent._arm_software_stop), matching the Alpaca-paper path."""
    raise USmartError("native_stop_unsupported: uSMART does not support broker-side stop orders")


def order_detail(account: BrokerAccount, order_id: str) -> dict[str, Any]:
    data = _request(account, _PATH_DETAIL, {"entrustId": int(order_id)})
    if isinstance(data, dict):
        detail_list = data.get("appEntrustRecordDetailInfoList")
        if isinstance(detail_list, list) and detail_list:
            merged = {**data, **detail_list[0]}
            return _normalize_order(merged)
        return _normalize_order(data)
    return _normalize_order({"entrustId": order_id})


def wait_for_order_fill(account: BrokerAccount, order_id: str, timeout_seconds: float = 8) -> dict[str, Any]:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    detail: dict[str, Any] = {}
    while time.monotonic() <= deadline:
        detail = order_detail(account, order_id)
        status = str(detail.get("order_status") or "").lower()
        filled = float(detail.get("filled_quantity") or 0)
        if _status_indicates_filled(status, filled) or status in {"canceled", "cancelled", "rejected", "expired", "已撤单", "已拒绝"}:
            return detail
        time.sleep(1)
    return detail


def trade_quantity(account: BrokerAccount, symbol: str, price: float | None = None) -> dict[str, Any]:
    """Max buy/sell quantity + purchasing power, for sizing."""
    stock_code, exchange_type = _stock_code_and_exchange(symbol)
    body: dict[str, Any] = {"stockCode": stock_code, "exchangeType": exchange_type, "entrustProp": "0"}
    if price and float(price) > 0:
        body["entrustPrice"] = _price_text(price)
    data = _request(account, f"{_P_ORDER}/trade-quantity", body)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Order normalization
# --------------------------------------------------------------------------- #
def _status_indicates_filled(status: str, filled_quantity: float) -> bool:
    """Token-based fill detection (NOT substring) — mirrors the trading-hardening
    rule so 'partially_filled'/'unfilled' aren't mistaken for filled."""
    text = str(status or "").strip().lower()
    if filled_quantity and filled_quantity > 0:
        return True
    filled_tokens = {"filled", "全部成交", "已成交", "成交"}
    not_filled = {"unfilled", "partially_filled", "partial", "部分成交", "待成交", "未成交"}
    if text in not_filled:
        return False
    return text in filled_tokens


def _normalize_order(payload: dict[str, Any]) -> dict[str, Any]:
    entrust_id = payload.get("entrustId") or payload.get("entrust_id") or payload.get("order_id")
    filled = payload.get("businessAmount") or payload.get("filled_quantity") or 0
    avg_price = payload.get("businessAveragePrice") or payload.get("executed_price")
    status = payload.get("statusName") or payload.get("orderStatusName") or payload.get("status")
    return {
        **payload,
        "order_id": str(entrust_id) if entrust_id is not None else None,
        "order_status": status,
        "executed_quantity": filled,
        "filled_quantity": filled,
        "executed_price": avg_price,
        "broker": "usmart",
    }

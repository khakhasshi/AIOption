from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from math import isfinite, log1p
from typing import Any, Callable, TypeVar

from .time_utils import et_today
from .ttl_cache import TTLCache
from .yfinance_option_tool import OptionCandidate


MARKET_DATA_TTL_SECONDS = float(os.getenv("AI_OPTION_THETADATA_MARKET_DATA_TTL_SECONDS") or 15)
OPTION_CHAIN_TTL_SECONDS = float(os.getenv("AI_OPTION_THETADATA_OPTION_CHAIN_TTL_SECONDS") or 15)
OPTION_EXPIRATIONS_TTL_SECONDS = float(os.getenv("AI_OPTION_THETADATA_EXPIRATIONS_TTL_SECONDS") or 300)
MAX_QUOTES_PER_EXPIRATION = max(int(os.getenv("AI_OPTION_THETADATA_OPTION_QUOTES_PER_EXPIRATION") or 96), 8)
# Concurrency for the per-strike historical fallback (see _history_chain_per_strike).
HISTORY_PER_STRIKE_WORKERS = max(int(os.getenv("AI_OPTION_THETADATA_HISTORY_WORKERS") or 8), 1)
# When the live option snapshot is empty/unavailable (markets closed — ThetaData's
# snapshot endpoints flake out on weekends/holidays), the scanner & opportunity radar
# fall back to the previous session's EOD chain so weekend scans still get real
# OI/IV/Greeks instead of nothing. Bounded so a weekend scan does not fan out into
# unbounded historical pulls across every expiration in range.
SCAN_EOD_FALLBACK = (os.getenv("AI_OPTION_THETADATA_SCAN_EOD_FALLBACK", "1").strip().lower() not in ("0", "false", "no", "off"))
SCAN_EOD_MAX_EXPIRATIONS = max(int(os.getenv("AI_OPTION_THETADATA_SCAN_EOD_MAX_EXPIRATIONS") or 4), 1)
SCAN_EOD_STRIKE_RANGE = max(int(os.getenv("AI_OPTION_THETADATA_SCAN_EOD_STRIKE_RANGE") or 24), 4)

_market_data_cache: TTLCache[dict[str, Any]] = TTLCache(MARKET_DATA_TTL_SECONDS, maxsize=128, namespace="theta_market")
_option_expirations_cache: TTLCache[list[str]] = TTLCache(OPTION_EXPIRATIONS_TTL_SECONDS, maxsize=256, namespace="theta_expirations")
_option_snapshot_cache: TTLCache[dict[str, dict[str, Any]]] = TTLCache(OPTION_CHAIN_TTL_SECONDS, maxsize=512, namespace="theta_option_snapshot")


class ThetaDataUnavailable(RuntimeError):
    pass


T = TypeVar("T")


def market_data(symbol: str, daily_count: int = 80) -> dict[str, Any]:
    clean = _clean_symbol(symbol)
    key = (clean, int(daily_count or 80))
    try:
        data = _market_data_cache.get_or_set_stale_while_revalidate(key, lambda: _market_data_uncached(clean, daily_count))
    except Exception:
        stale = _market_data_cache.get_stale(key)
        if stale is None:
            raise
        data = stale
        data.setdefault("cache", {})["stale"] = True
    data.setdefault("cache", {})["ttl_seconds"] = MARKET_DATA_TTL_SECONDS
    return data


def collect_candidates(
    symbol: str,
    spot: float,
    min_days: int,
    max_days: int,
    max_ask: float,
    lottery: bool,
    preferred_side: str | None,
    min_ask: float = 0.05,
    gex_mode: bool = False,
) -> list[OptionCandidate]:
    clean = _clean_symbol(symbol)
    today = et_today()
    candidates: list[OptionCandidate] = []
    eod_budget = [SCAN_EOD_MAX_EXPIRATIONS]
    for expiration in option_expirations(clean):
        expiry_date = date.fromisoformat(expiration)
        days = (expiry_date - today).days
        if days < min_days or days > max_days:
            continue
        rows = _scan_expiration_rows(clean, expiration, spot, eod_budget)
        if not gex_mode:
            rows = _trim_rows_for_quote(rows, spot, lottery)
        for row in rows:
            side = _side(row.get("right"))
            if preferred_side and side != preferred_side:
                continue
            strike = _num(row.get("strike"))
            if strike <= 0:
                continue
            bid = _num(row.get("bid") or row.get("market_bid"))
            ask = _num(row.get("ask") or row.get("market_ask"))
            last_price = _num(row.get("close") or row.get("market_price"))
            bid, ask, pricing_source, quote_warning = _normalize_option_quote(bid, ask, last_price)
            if not gex_mode and (ask < min_ask or ask > max_ask):
                continue
            volume = int(_num(row.get("volume")))
            open_interest = int(_num(row.get("open_interest")))
            implied_volatility = _num(row.get("implied_vol") or row.get("implied_volatility"))
            if gex_mode and (open_interest <= 0 or implied_volatility <= 0):
                continue
            if not gex_mode and (volume < 20 and open_interest < 100):
                continue
            moneyness_pct = (strike / spot - 1) * 100 if side == "call" else (1 - strike / spot) * 100
            if not gex_mode:
                if lottery and moneyness_pct < 1.0:
                    continue
                if not lottery and moneyness_pct < -2.0:
                    continue
            spread_pct = (ask - bid) / ask * 100 if ask else 100
            if not gex_mode and spread_pct > 45:
                continue
            liquidity_score = log1p(max(volume, 0)) + 0.55 * log1p(max(open_interest, 0))
            price_score = max(0.1, (max_ask - ask) / max_ask)
            lottery_score = min(max(moneyness_pct, 0), 8) / 8
            spread_score = max(0.0, 1 - spread_pct / 45)
            score = liquidity_score * 1.2 + price_score * 2.0 + spread_score * 2.0 + lottery_score * (3.0 if lottery else 1.0)
            candidates.append(
                OptionCandidate(
                    contract_symbol=_contract_symbol(clean, expiration, side, strike),
                    expiration=expiration,
                    side=side,
                    strike=strike,
                    last_price=last_price,
                    bid=bid,
                    ask=ask,
                    volume=volume,
                    open_interest=open_interest,
                    implied_volatility=implied_volatility,
                    in_the_money=(strike < spot if side == "call" else strike > spot),
                    moneyness_pct=moneyness_pct,
                    spread_pct=spread_pct,
                    score=score,
                    pricing_source=pricing_source,
                    quote_warning=quote_warning,
                )
            )
    return sorted(candidates, key=lambda item: item.score, reverse=True)


def _is_no_data_error(exc: Exception) -> bool:
    return exc.__class__.__name__ == "NoDataFoundError" or "no data found" in _error_message(exc).lower()


def _eod_as_of_date() -> date:
    """Most recent *completed* regular session, for the scan EOD fallback.

    On a closed day (weekend / holiday) this is the prior trading session; on a
    trading day it is the previous completed session (the intraday path normally
    has live snapshots, so this branch only matters if snapshots are unavailable).
    """
    from .market_calendar import is_nyse_trading_day, previous_nyse_trading_day

    today = et_today()
    if is_nyse_trading_day(today)[0]:
        return previous_nyse_trading_day(today - timedelta(days=1))
    return previous_nyse_trading_day(today)


def _scan_expiration_rows(
    clean: str,
    expiration: str,
    spot: float,
    eod_budget: list[int],
) -> list[dict[str, Any]]:
    """Snapshot-shaped option rows for one expiration used by ``collect_candidates``.

    Uses the live snapshot when available (intraday). When the snapshot is empty or
    ThetaData reports no data (markets closed — snapshots flake out on weekends),
    falls back to the previous session's EOD chain so weekend scans still yield real
    OI/IV/Greeks. The EOD fallback is bounded by ``eod_budget`` to cap historical
    pulls per scan.
    """
    try:
        snapshot = _option_snapshot(clean, expiration)
        rows = list(snapshot.values())
    except Exception as exc:  # noqa: BLE001
        if not _is_no_data_error(exc):
            raise
        rows = []
    if rows or not SCAN_EOD_FALLBACK or eod_budget[0] <= 0:
        return rows
    eod_budget[0] -= 1
    try:
        chain_rows, _meta = option_chain_rows(
            clean, expiration, spot=spot,
            as_of_date=_eod_as_of_date(), strike_range=SCAN_EOD_STRIKE_RANGE,
        )
    except Exception:  # noqa: BLE001 - EOD fallback is best-effort; never break the scan
        return []
    return [
        {
            "right": row.get("side"),
            "strike": row.get("strike"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "close": row.get("last_price"),
            "open_interest": row.get("open_interest"),
            "implied_vol": row.get("implied_volatility"),
            "volume": 0,
        }
        for row in chain_rows
    ]


def option_expirations(symbol: str) -> list[str]:
    clean = _clean_symbol(symbol)
    return _option_expirations_cache.get_or_set(clean, lambda: _option_expirations_uncached(clean))


def option_chain_rows(
    symbol: str,
    expiration: str,
    *,
    spot: float = 0.0,
    as_of_date: date | None = None,
    strike_range: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Per-strike option rows for one expiration.

    When ``as_of_date`` is ``None`` the live option snapshot is used (intraday /
    just-closed market). When ``as_of_date`` is set the historical EOD path is used
    instead — this is what the chat assistant calls on weekends / closed days so the
    chain reflects the previous trading day rather than empty realtime snapshots.

    ``spot`` (optional) centers the per-strike historical fallback window around the
    at-the-money strike; it is ignored on the live-snapshot path.

    Returns ``(rows, meta)`` where each row has ``strike``, ``side`` ('call'/'put'),
    ``implied_volatility``, ``open_interest``, ``bid``, ``ask`` and ``last_price``.
    ``meta`` carries ``as_of`` (ISO date or ``None``) and ``underlying_price`` (the
    EOD underlier price reported by ThetaData, only available on the historical path).
    """
    clean = _clean_symbol(symbol)
    expiry = date.fromisoformat(expiration) if isinstance(expiration, str) else expiration
    meta: dict[str, Any] = {"as_of": as_of_date.isoformat() if as_of_date else None, "underlying_price": None}
    rows: list[dict[str, Any]] = []

    if as_of_date is None:
        snapshot = _option_snapshot(clean, expiry.isoformat())
        for row in snapshot.values():
            strike = _num(row.get("strike"))
            if strike <= 0:
                continue
            rows.append(
                {
                    "strike": strike,
                    "side": _side(row.get("right")),
                    "implied_volatility": _num(row.get("implied_vol") or row.get("implied_volatility")),
                    "open_interest": int(_num(row.get("open_interest"))),
                    "bid": _num(row.get("bid") or row.get("market_bid")),
                    "ask": _num(row.get("ask") or row.get("market_ask")),
                    "last_price": _num(row.get("close") or row.get("market_price")),
                }
            )
        return rows, meta

    # Historical / weekend path. ThetaData's cloud MDDS rejects multi-contract
    # (strike="*") *historical* requests for some accounts with an INTERNAL
    # ProcessingError, even though single-contract historical requests and the live
    # snapshot (which also uses strike="*") succeed. So try the fast bulk call first
    # and fall back to per-strike historical calls when it errors or returns nothing.
    greeks_rows: list[dict[str, Any]] = []
    oi_rows: list[dict[str, Any]] = []
    try:
        greeks_rows, oi_rows = _with_session_retry(
            lambda client: _history_chain_bulk(client, clean, expiry, as_of_date, strike_range)
        )
    except Exception:  # noqa: BLE001 - bulk strike="*" can fail server-side; fall back below
        greeks_rows, oi_rows = [], []
    if not greeks_rows:
        greeks_rows, oi_rows = _history_chain_per_strike(clean, expiry, as_of_date, strike_range, spot)

    oi_map: dict[tuple[float, str], int] = {}
    for row in oi_rows:
        oi_map[(_num(row.get("strike")), _side(row.get("right")))] = int(_num(row.get("open_interest")))
    for row in greeks_rows:
        strike = _num(row.get("strike"))
        if strike <= 0:
            continue
        side = _side(row.get("right"))
        underlier = _num(row.get("underlying_price"))
        if underlier > 0 and not meta["underlying_price"]:
            meta["underlying_price"] = underlier
        rows.append(
            {
                "strike": strike,
                "side": side,
                "implied_volatility": _num(row.get("implied_vol") or row.get("implied_volatility")),
                "open_interest": oi_map.get((strike, side), 0),
                "bid": _num(row.get("bid")),
                "ask": _num(row.get("ask")),
                "last_price": _num(row.get("close")),
            }
        )
    return rows, meta


def _history_chain_bulk(
    client: Any,
    clean: str,
    expiry: date,
    as_of_date: date,
    strike_range: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One bulk Greeks-EOD + Open-Interest pull for the whole chain (strike="*")."""
    greeks = _records(
        client.option_history_greeks_eod(
            clean, expiry, start_date=as_of_date, end_date=as_of_date,
            strike="*", right="both", strike_range=strike_range,
        )
    )
    oi = _records(
        client.option_history_open_interest(
            clean, expiry, date=as_of_date,
            strike="*", right="both", strike_range=strike_range,
        )
    )
    return greeks, oi


def _history_chain_per_strike(
    clean: str,
    expiry: date,
    as_of_date: date,
    strike_range: int | None,
    spot: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-strike historical EOD fallback for when the bulk strike="*" call fails.

    ThetaData's cloud rejects multi-contract historical requests for some accounts,
    but single-contract historical requests succeed. The strike universe comes from
    the live snapshot (whose strike="*" call works); we fetch a window around the
    ATM strike concurrently, one contract at a time, and tolerate per-strike errors.
    """
    try:
        snapshot = _option_snapshot(clean, expiry.isoformat())
    except Exception:  # noqa: BLE001
        return [], []
    strikes = sorted({_num(row.get("strike")) for row in snapshot.values() if _num(row.get("strike")) > 0})
    if not strikes:
        return [], []
    if spot > 0:
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    else:
        atm_idx = len(strikes) // 2
    span = strike_range if strike_range and strike_range > 0 else 16
    window = strikes[max(0, atm_idx - span):atm_idx + span + 1]
    if not window:
        return [], []

    client = _client()

    def fetch(strike: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        code = f"{strike:g}"
        try:
            # These workers hit the shared client outside _with_session_retry, so
            # they must take the same request lock or they cross-talk (see its
            # definition). The pool stays for structure; the lock serializes it.
            with _request_lock:
                greeks = _records(
                    client.option_history_greeks_eod(
                        clean, expiry, start_date=as_of_date, end_date=as_of_date,
                        strike=code, right="both",
                    )
                )
                oi = _records(
                    client.option_history_open_interest(
                        clean, expiry, date=as_of_date, strike=code, right="both",
                    )
                )
            return greeks, oi
        except Exception:  # noqa: BLE001 - skip strikes with no data / transient errors
            return [], []

    greeks_rows: list[dict[str, Any]] = []
    oi_rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(HISTORY_PER_STRIKE_WORKERS, len(window))) as pool:
        for greeks, oi in pool.map(fetch, window):
            greeks_rows.extend(greeks)
            oi_rows.extend(oi)
    return greeks_rows, oi_rows


def quote_option_contract(contract_symbol: str) -> dict[str, Any]:
    return _quote_option_contract(contract_symbol, use_cache=True)


def quote_option_contract_live(contract_symbol: str) -> dict[str, Any]:
    return _quote_option_contract(contract_symbol, use_cache=False)


def _quote_option_contract(contract_symbol: str, *, use_cache: bool) -> dict[str, Any]:
    parsed = _parse_contract_symbol(contract_symbol)
    if not parsed:
        return {
            "contract_symbol": contract_symbol,
            "available": False,
            "error": "Unsupported option contract symbol format.",
        }
    snapshot = _option_snapshot(parsed["root"], parsed["expiration"]) if use_cache else _option_snapshot_uncached(parsed["root"], parsed["expiration"])
    row = snapshot.get(_snapshot_key(parsed["strike"], parsed["side"])) or {}
    if not row:
        return {
            "contract_symbol": contract_symbol,
            "available": False,
            "error": "Contract not found in ThetaData option snapshot.",
            **parsed,
        }
    bid = _num(row.get("bid") or row.get("market_bid"))
    ask = _num(row.get("ask") or row.get("market_ask"))
    last_price = _num(row.get("close") or row.get("market_price"))
    bid, ask, pricing_source, quote_warning = _normalize_option_quote(bid, ask, last_price)
    mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else 0.0
    limit_price = ask if ask > 0 else last_price
    return {
        "contract_symbol": contract_symbol,
        "available": limit_price > 0,
        "source": "thetadata",
        "root": parsed["root"],
        "expiration": parsed["expiration"],
        "side": parsed["side"],
        "strike": parsed["strike"],
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last_price": last_price,
        "limit_price": round(limit_price, 2) if limit_price > 0 else 0,
        "volume": int(_num(row.get("volume"))),
        "open_interest": int(_num(row.get("open_interest"))),
        "implied_volatility": _num(row.get("implied_vol") or row.get("implied_volatility")),
        "pricing_source": pricing_source,
        "quote_warning": quote_warning,
        "cache": {"used": bool(use_cache), "ttl_seconds": OPTION_CHAIN_TTL_SECONDS if use_cache else 0},
        "raw_quote": row,
    }


def account_capabilities() -> dict[str, Any]:
    client = _client()
    checks: list[dict[str, Any]] = []

    def probe(name: str, callback: Any) -> None:
        try:
            with _request_lock:  # diagnostic probes share the client; serialize them too
                frame = callback()
            checks.append({"name": name, "ok": True, "shape": _frame_shape(frame), "columns": _frame_columns(frame)})
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic boundary.
            checks.append({"name": name, "ok": False, "error": _error_message(exc)})

    sample_day = _last_weekday(et_today() - timedelta(days=1))
    probe("stock_snapshot_quote", lambda: client.stock_snapshot_quote("SPY"))
    probe("stock_history_ohlc_1m", lambda: client.stock_history_ohlc("SPY", date=sample_day, interval="1m"))
    probe("option_list_expirations", lambda: client.option_list_expirations("SPY"))
    probe("option_snapshot_quote", lambda: client.option_snapshot_quote("SPY", expiration="*", strike="*", right="both"))
    probe("option_snapshot_greeks_implied_volatility", lambda: client.option_snapshot_greeks_implied_volatility("SPY", expiration="*", strike="*", right="both"))
    probe("option_snapshot_greeks_all", lambda: client.option_snapshot_greeks_all("SPY", expiration="*", strike="*", right="both"))
    probe("index_snapshot_price", lambda: client.index_snapshot_price("SPX"))
    return {
        "provider": "thetadata",
        "configured": True,
        "checks": checks,
        "notes": [
            "ThetaData does not provide a news endpoint used by this app; news is returned as an empty list for this source.",
            "The scanner can use quote, OHLC, open interest and implied volatility snapshots; full Greeks may require a higher ThetaData plan.",
        ],
    }


def _market_data_uncached(symbol: str, daily_count: int = 80) -> dict[str, Any]:
    today = et_today()
    start = today - timedelta(days=220)

    def load(client: Any) -> tuple[Any, Any, Any, Any]:
        return (
            client.stock_history_eod(symbol, start_date=start, end_date=today),
            _latest_intraday_frame(client, symbol, today),
            client.stock_snapshot_quote(symbol),
            client.stock_snapshot_ohlc(symbol),
        )

    daily_frame, intraday_frame, quote_frame, ohlc_frame = _with_session_retry(load)
    daily = _daily_rows(daily_frame)[-int(daily_count or 80):]
    intraday = _intraday_rows(intraday_frame)
    quote_row = _first_row(quote_frame)
    ohlc_row = _first_row(ohlc_frame)
    bid = _num(quote_row.get("bid"))
    ask = _num(quote_row.get("ask"))
    last_price = _num(ohlc_row.get("close")) or ((bid + ask) / 2 if bid > 0 and ask > 0 else 0) or _last_price(daily, intraday)
    return {
        "quote": {
            "symbol": symbol,
            "last": last_price,
            "price": last_price,
            "bid": bid,
            "ask": ask,
            "time": _time_text(quote_row.get("timestamp") or ohlc_row.get("timestamp") or (intraday[-1]["time"] if intraday else "")),
            "source": "thetadata",
            "raw": {**ohlc_row, **quote_row},
        },
        "daily": daily,
        "intraday": intraday,
        "news": [],
        "latest_news_source": "none",
    }


def _option_expirations_uncached(symbol: str) -> list[str]:
    requested = _clean_symbol(symbol)
    frame = _with_session_retry(lambda client: client.option_list_expirations(symbol))
    rows = _records(frame)
    _guard_response_root(requested, rows, context="option_list_expirations")
    today = et_today()
    output: list[str] = []
    for row in rows:
        expiration = _date_text(row.get("expiration"))
        if not expiration:
            continue
        # Stale-response guard: ThetaData's shared gRPC session can hand back a
        # late response from an unrelated historical request (observed: a TSLA
        # chain returning AT&T's 2012 expirations). Such past-dated rows are not
        # tradable and are the fingerprint of a crossed response, so drop them.
        try:
            if date.fromisoformat(expiration) < today:
                continue
        except ValueError:
            continue
        output.append(expiration)
    if rows and not output:
        # Every row was past-dated — the response did not belong to this request.
        # Reset the session to flush the stale stream and refuse to cache nothing.
        _reset_client(_client_singleton)
        raise ThetaDataUnavailable(
            f"ThetaData returned only past-dated expirations for {requested}; suspected crossed response"
        )
    return sorted(set(output))


def _option_snapshot(symbol: str, expiration: str) -> dict[str, dict[str, Any]]:
    key = (_clean_symbol(symbol), expiration)
    return _option_snapshot_cache.get_or_set(key, lambda: _option_snapshot_uncached(symbol, expiration))


def _option_snapshot_uncached(symbol: str, expiration: str) -> dict[str, dict[str, Any]]:
    expiry = date.fromisoformat(expiration)

    def load(client: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        quote_rows = _records(client.option_snapshot_quote(symbol, expiration=expiry, strike="*", right="both"))
        oi_rows = _records(client.option_snapshot_open_interest(symbol, expiration=expiry, strike="*", right="both"))
        ohlc_rows = _records(client.option_snapshot_ohlc(symbol, expiration=expiry, strike="*", right="both"))
        try:
            iv_rows = _records(client.option_snapshot_greeks_implied_volatility(symbol, expiration=expiry, strike="*", right="both"))
        except Exception:
            iv_rows = []
        return quote_rows, oi_rows, ohlc_rows, iv_rows

    quote_rows, oi_rows, ohlc_rows, iv_rows = _with_session_retry(load)
    requested = _clean_symbol(symbol)
    for rows in (quote_rows, oi_rows, ohlc_rows, iv_rows):
        _guard_response_root(requested, rows, context="option_snapshot")
    merged: dict[str, dict[str, Any]] = {}
    for rows in (quote_rows, oi_rows, ohlc_rows, iv_rows):
        for row in rows:
            key = _snapshot_key(row.get("strike"), _side(row.get("right")))
            if not key:
                continue
            merged.setdefault(key, {}).update(_jsonish(row))
    return merged


# ThetaData v3 cloud permits only ONE active session per account: a second login
# invalidates the first ("Invalid session ID. This can occur if more than one
# terminal is running."). Concurrent gRPC *requests* on a single session are fine;
# concurrent *logins* are fatal. So the singleton client and every re-auth are
# serialized through this lock, and invalid-session recoveries collapse onto a
# single re-login shared by all racing threads (see _reset_client).
_client_lock = threading.RLock()
_client_singleton: Any = None
_client_credential_revision: str | None = None

# A single ThetaClient wraps one gRPC session to the local Theta Terminal. That
# session multiplexes responses by arrival order, NOT by request — so two threads
# issuing requests concurrently on the shared singleton can read each other's
# frames (observed: a whole scan universe returning AT&T's price for SPY/NVDA/…).
# Every actual data request is therefore serialized through this lock. It is a
# plain (non-reentrant) Lock on purpose: no code path legitimately re-enters a
# request while already holding it, so any accidental nesting surfaces as a
# deadlock in tests instead of silently reintroducing the cross-talk.
_request_lock = threading.Lock()


def _build_client(credentials: Any | None = None) -> Any:
    try:
        from thetadata import ThetaClient
    except Exception as exc:  # noqa: BLE001
        raise ThetaDataUnavailable("thetadata package is not installed; install thetadata on Python 3.12+") from exc
    if credentials is None:
        from .thetadata_store import resolve_thetadata_credentials

        credentials = resolve_thetadata_credentials()
    try:
        if credentials.email and credentials.password:
            return ThetaClient(email=credentials.email, password=credentials.password, dataframe_type="pandas")
        if credentials.credentials_file:
            return ThetaClient(creds_file=credentials.credentials_file, dataframe_type="pandas")
        return ThetaClient(dataframe_type="pandas")
    except Exception as exc:  # noqa: BLE001
        raise ThetaDataUnavailable(_error_message(exc)) from exc


def _client() -> Any:
    global _client_credential_revision, _client_singleton
    from .thetadata_store import resolve_thetadata_credentials

    credentials = resolve_thetadata_credentials()
    client = _client_singleton
    if client is not None and _client_credential_revision is None:
        # Tests and embedded deployments may inject an already-authenticated
        # client directly. Adopt it for the current credential revision instead
        # of creating a second login that would invalidate that live session.
        with _client_lock:
            if _client_singleton is client and _client_credential_revision is None:
                _client_credential_revision = credentials.revision
            return _client_singleton
    if client is not None and _client_credential_revision == credentials.revision:
        return client
    with _client_lock:
        if _client_singleton is None or _client_credential_revision != credentials.revision:
            _close_client(_client_singleton)
            _client_singleton = _build_client(credentials)
            _client_credential_revision = credentials.revision
        return _client_singleton


def _reset_client(stale: Any) -> Any:
    """Re-authenticate once, collapsing concurrent invalid-session recoveries.

    Only the first thread whose ``stale`` client is still the active singleton
    performs the re-login; every other racing thread receives the freshly built
    session instead of triggering its own login (which would invalidate the new
    session and cascade the failure across the scan universe).
    """
    global _client_credential_revision, _client_singleton
    with _client_lock:
        if _client_singleton is stale or _client_singleton is None:
            from .thetadata_store import resolve_thetadata_credentials

            credentials = resolve_thetadata_credentials()
            _close_client(_client_singleton)
            _client_singleton = _build_client(credentials)
            _client_credential_revision = credentials.revision
        return _client_singleton


def reset_client() -> None:
    """Drop the cached SDK session after an admin changes credentials."""

    global _client_credential_revision, _client_singleton
    with _client_lock:
        _close_client(_client_singleton)
        _client_singleton = None
        _client_credential_revision = None
    _market_data_cache.clear()
    _option_expirations_cache.clear()
    _option_snapshot_cache.clear()


def _close_client(client: Any | None) -> None:
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _with_session_retry(callback: Callable[[Any], T], attempts: int = 3) -> T:
    client = _client()
    last_exc: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            with _request_lock:  # serialize the request(s); released before any re-login
                return callback(client)
        except Exception as exc:  # noqa: BLE001
            if not _is_invalid_session_error(exc):
                raise
            last_exc = exc
            client = _reset_client(client)
    assert last_exc is not None
    raise last_exc


def _is_invalid_session_error(exc: Exception) -> bool:
    text = _error_message(exc).lower()
    return "invalid session id" in text or ("unauthenticated" in text and "session" in text)


def _latest_intraday_frame(client: Any, symbol: str, today: date) -> Any:
    cursor = today
    last_error: Exception | None = None
    for _ in range(8):
        cursor = _last_weekday(cursor)
        try:
            frame = client.stock_history_ohlc(symbol, date=cursor, interval="1m")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            cursor = cursor - timedelta(days=1)
            continue
        if _has_usable_intraday_prices(frame):
            return frame
        cursor = cursor - timedelta(days=1)
    if last_error:
        raise last_error
    raise ThetaDataUnavailable("ThetaData intraday history is unavailable")


def _daily_rows(frame: Any) -> list[dict[str, Any]]:
    rows = []
    for row in _records(frame):
        rows.append(
            {
                "time": _time_text(row.get("created") or row.get("timestamp") or row.get("date")),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "volume": int(_num(row.get("volume"))),
            }
        )
    return rows


def _intraday_rows(frame: Any) -> list[dict[str, Any]]:
    rows = []
    for row in _records(frame):
        close = _num(row.get("close"))
        if close <= 0:
            continue
        rows.append(
            {
                "time": _time_text(row.get("timestamp")),
                "price": close,
                "avg_price": _num(row.get("vwap")) or close,
                "volume": int(_num(row.get("volume"))),
            }
        )
    return rows


def _has_usable_intraday_prices(frame: Any) -> bool:
    usable = 0
    for row in _records(frame):
        if _num(row.get("close")) > 0:
            usable += 1
            if usable >= 2:
                return True
    return False


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        try:
            return [_jsonish(row) for row in frame.to_dict("records")]
        except TypeError:
            pass
    if isinstance(frame, list):
        return [_jsonish(row) for row in frame if isinstance(row, dict)]
    return []


def _first_row(frame: Any) -> dict[str, Any]:
    rows = _records(frame)
    return rows[0] if rows else {}


def _frame_shape(frame: Any) -> list[int]:
    shape = getattr(frame, "shape", None)
    if shape is None:
        return []
    return [int(item) for item in shape]


def _frame_columns(frame: Any) -> list[str]:
    columns = getattr(frame, "columns", None)
    if columns is None:
        return []
    return [str(item) for item in list(columns)[:20]]


def _jsonish(row: dict[str, Any]) -> dict[str, Any]:
    output = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):
            output[str(key)] = value.isoformat()
        else:
            output[str(key)] = value
    return output


def _trim_rows_for_quote(rows: list[dict[str, Any]], spot: float, lottery: bool) -> list[dict[str, Any]]:
    if len(rows) <= MAX_QUOTES_PER_EXPIRATION:
        return rows
    target_moneyness = 4.0 if lottery else 1.5

    def sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
        strike = _num(item.get("strike"))
        side = _side(item.get("right"))
        moneyness = (strike / spot - 1) * 100 if side == "call" else (1 - strike / spot) * 100
        return (abs(moneyness - target_moneyness), abs(strike - spot) / max(spot, 1), _snapshot_key(strike, side))

    return sorted(rows, key=sort_key)[:MAX_QUOTES_PER_EXPIRATION]


def _normalize_option_quote(bid: float, ask: float, last_price: float) -> tuple[float, float, str, str]:
    if ask > 0 and bid > 0:
        return bid, ask, "thetadata_bid_ask", ""
    if ask > 0:
        synthetic_bid = bid if bid > 0 else max(0.01, min(ask * 0.9, last_price if last_price > 0 else ask * 0.9))
        return synthetic_bid, ask, "thetadata_ask_only", "ThetaData bid is unavailable; using ask with an indicative bid."
    if last_price > 0:
        synthetic_bid = max(0.01, last_price * 0.9)
        return synthetic_bid, last_price, "thetadata_last_price_fallback", "ThetaData bid/ask are unavailable; using last price as an indicative option price."
    return bid, ask, "thetadata_unavailable", "ThetaData did not return a usable bid/ask or last price."


def _parse_contract_symbol(contract_symbol: str) -> dict[str, Any] | None:
    text = str(contract_symbol or "").strip()
    if text.endswith(".US"):
        text = text[:-3]
    marker_index = -1
    for index in range(len(text) - 9):
        if text[index : index + 6].isdigit() and text[index + 6] in {"C", "P"}:
            marker_index = index
            break
    if marker_index < 1:
        return None
    root = text[:marker_index].upper()
    expiry = text[marker_index : marker_index + 6]
    side = "call" if text[marker_index + 6] == "C" else "put"
    strike_code = text[marker_index + 7 :]
    if not strike_code.isdigit():
        return None
    return {
        "root": root,
        "expiration": f"20{expiry[:2]}-{expiry[2:4]}-{expiry[4:6]}",
        "side": side,
        "strike": int(strike_code) / 1000,
    }


def _contract_symbol(root: str, expiration: str, side: str, strike: float) -> str:
    expiry = expiration[2:4] + expiration[5:7] + expiration[8:10]
    side_code = "C" if side == "call" else "P"
    return f"{root}{expiry}{side_code}{int(round(strike * 1000)):08d}"


def _snapshot_key(strike: Any, side: str) -> str:
    return f"{_num(strike):.3f}:{_side(side)}"


def _side(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "put" if text.startswith("p") else "call"


def _clean_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    return text[:-3] if text.endswith(".US") else text


def _guard_response_root(requested: str, rows: list[dict[str, Any]], *, context: str) -> None:
    """Reject a ThetaData response whose rows carry a different root than requested.

    ThetaData's single gRPC session multiplexes responses by arrival order, so a
    late response from an unrelated request can surface on the next call and be
    cached under the wrong symbol (observed: a TSLA chain returning AT&T's "T"
    root). When a row exposes a root/symbol field and it disagrees with the
    requested symbol, the response is crossed: reset the session to flush the
    stale stream and raise so the bad data is never cached.
    """
    requested = _clean_symbol(requested)
    if not requested:
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in ("root", "symbol", "underlying", "option_symbol", "contract"):
            value = row.get(field)
            if not value:
                continue
            text = str(value).strip().upper()
            # For full contract symbols, the root is the leading alphabetic run
            # (e.g. "T260702C00022500" -> "T"); plain symbol fields are used as-is.
            root = text[: len(text) - len(text.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))] if field in {"option_symbol", "contract"} else text
            root = root or text
            if root and root != requested:
                _reset_client(_client_singleton)
                raise ThetaDataUnavailable(
                    f"ThetaData {context} returned root {root!r} for requested {requested!r}; suspected crossed response"
                )
            break  # one identifying field per row is enough


def _last_weekday(value: date) -> date:
    cursor = value
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _last_price(daily: list[dict[str, Any]], intraday: list[dict[str, Any]]) -> float:
    if intraday and _num(intraday[-1].get("price")) > 0:
        return _num(intraday[-1].get("price"))
    if daily and _num(daily[-1].get("close")) > 0:
        return _num(daily[-1].get("close"))
    return 0.0


def _date_text(value: Any) -> str:
    if hasattr(value, "date"):
        value = value.date()
    if hasattr(value, "isoformat"):
        return str(value.isoformat())[:10]
    text = str(value or "")
    return text[:10] if len(text) >= 10 else text


def _time_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _num(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if isfinite(number) else 0.0


def _error_message(exc: Exception) -> str:
    message = str(exc)
    secret = os.getenv("THETADATA_PASSWORD") or os.getenv("AI_OPTION_THETADATA_PASSWORD") or ""
    if not secret:
        try:
            from .thetadata_store import resolve_thetadata_credentials

            secret = resolve_thetadata_credentials().password or ""
        except Exception:
            secret = ""
    return message.replace(secret, "***") if secret else message

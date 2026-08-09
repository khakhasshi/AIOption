from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .broker_client import account_ref_for_config, assets, display_account_name
from .longbridge_client import executions
from .time_utils import normalize_time_fields, now_et
from .trading_store import get_trading_config, list_capital_snapshots, save_capital_snapshot


ET = ZoneInfo("America/New_York")


def trading_snapshots(owner_id: str, days: int = 30, refresh: bool = True) -> dict[str, Any]:
    config = get_trading_config(owner_id)
    account_name = account_ref_for_config(config, owner_id=owner_id)
    snapshot_date = now_et().date().isoformat()
    asset_rows: list[dict[str, Any]] = []
    execution_rows: list[dict[str, Any]] = []
    executions_source = "broker"
    error: str | None = None

    if refresh:
        try:
            asset_rows = assets(account_name, "USD")
            if str(account_name).startswith("alpaca:"):
                execution_rows = []
                executions_source = "unavailable_for_broker"
            else:
                execution_rows = _recent_executions(display_account_name(account_name), days)
            save_capital_snapshot(
                owner_id=owner_id,
                snapshot_date_et=snapshot_date,
                account_name=account_name,
                total_capital=float(config.get("total_capital") or 0),
                assets_payload=asset_rows,
                executions_payload=execution_rows,
            )
        except Exception as exc:  # noqa: BLE001 - surface broker snapshot errors without breaking the page.
            error = str(exc)

    curve = [row for row in list_capital_snapshots(owner_id, days) if row.get("account_name") == account_name]
    latest = curve[-1] if curve else None
    primary_assets = asset_rows[0] if asset_rows else ((latest or {}).get("assets") or [{}])[0]
    if str(account_name).startswith("alpaca:"):
        snapshot_executions = execution_rows
        executions_source = "unavailable_for_broker"
    else:
        snapshot_executions = execution_rows if execution_rows else ((latest or {}).get("executions") or [])
    return {
        "snapshot_date_et": snapshot_date,
        "account_name": account_name,
        "strategy": _strategy_snapshot(config, primary_assets),
        "assets": primary_assets or {},
        "executions": {
            "count": len(snapshot_executions),
            "notional": round(_execution_notional(snapshot_executions), 2),
            "rows": snapshot_executions[:50],
            "source": executions_source,
        },
        "curve": curve,
        "error": error,
    }


def _recent_executions(account_name: str, days: int) -> list[dict[str, Any]]:
    today = now_et().date()
    start = (today - timedelta(days=max(1, min(int(days or 30), 365)) - 1)).isoformat()
    rows = []
    rows.extend(executions(account_name, start=start, end=today.isoformat(), history=True))
    rows.extend(executions(account_name, history=False))
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("trade_id") or row.get("order_id") or row)
        deduped[key] = normalize_time_fields(row, ("trade_done_at", "submitted_at", "updated_at", "created_at"))
    return sorted(deduped.values(), key=lambda item: str(item.get("trade_done_at") or ""), reverse=True)


def _strategy_snapshot(config: dict[str, Any], assets_payload: dict[str, Any]) -> dict[str, Any]:
    total_capital = float(config.get("total_capital") or 0)
    buy_power = _coerce_number(assets_payload.get("buy_power")) or 0
    net_assets = _coerce_number(assets_payload.get("net_assets")) or 0
    total_cash = _coerce_number(assets_payload.get("total_cash")) or 0
    usable_capital = min(total_capital, buy_power) if total_capital > 0 else buy_power
    return {
        "total_capital": total_capital,
        "usable_capital": round(usable_capital, 2),
        "net_assets": net_assets,
        "total_cash": total_cash,
        "buy_power": buy_power,
        "risk_level": assets_payload.get("risk_level") or "--",
        "capital_coverage_pct": round((buy_power / total_capital) * 100, 2) if total_capital > 0 else None,
    }


def _execution_notional(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        price = _coerce_number(row.get("price")) or 0
        quantity = _coerce_number(row.get("quantity")) or 0
        multiplier = 100 if _looks_like_option(str(row.get("symbol") or "")) else 1
        total += abs(price * quantity * multiplier)
    return total


def _looks_like_option(symbol: str) -> bool:
    return any(marker in symbol for marker in ("C0", "P0")) or (len(symbol) > 12 and (".US" in symbol))


def _coerce_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

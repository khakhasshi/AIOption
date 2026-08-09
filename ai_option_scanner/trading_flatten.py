from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .broker_client import cancel_order, positions, submit_market_order, today_orders


CONFIRMATION_TEXT = "全平"


def flatten_all_positions(account_name: str) -> dict[str, Any]:
    canceled_orders, cancel_failed = _cancel_open_orders(account_name)
    rows = positions(account_name)
    submitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for row in rows:
        symbol = _symbol(row)
        net_quantity = _decimal(row.get("quantity"))
        available_quantity = _decimal(row.get("available_quantity"))
        if not symbol:
            skipped.append({"position": row, "reason": "symbol missing"})
            continue
        if net_quantity is None or net_quantity == 0:
            skipped.append({"symbol": symbol, "position": row, "reason": "net quantity is zero or unavailable"})
            continue
        if available_quantity is None:
            available_quantity = net_quantity
        close_quantity = abs(available_quantity)
        if close_quantity <= 0:
            skipped.append({"symbol": symbol, "position": row, "reason": "available quantity is zero"})
            continue
        side = "sell" if net_quantity > 0 else "buy"
        try:
            order = submit_market_order(
                symbol,
                close_quantity,
                side,
                account_name,
                f"AI_OPTION_FLATTEN {side.upper()}",
            )
            submitted.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": str(close_quantity.normalize()),
                    "net_quantity": str(net_quantity.normalize()),
                    "available_quantity": str(available_quantity.normalize()),
                    "order": order,
                    "position": row,
                }
            )
        except Exception as exc:  # noqa: BLE001 - report per-position failures without hiding other closes.
            failed.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": str(close_quantity.normalize()),
                    "net_quantity": str(net_quantity.normalize()),
                    "available_quantity": str(available_quantity.normalize()),
                    "error": str(exc),
                    "position": row,
                }
            )
    return {
        "account_name": account_name,
        "positions_count": len(rows),
        "canceled_order_count": len(canceled_orders),
        "cancel_failed_count": len(cancel_failed),
        "submitted_count": len(submitted),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "canceled_orders": canceled_orders,
        "cancel_failed": cancel_failed,
        "submitted": submitted,
        "failed": failed,
        "skipped": skipped,
        "status": "partial_failed" if failed or cancel_failed else "ok",
    }


def _cancel_open_orders(account_name: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    canceled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    try:
        orders = today_orders(account_name)
    except Exception as exc:  # noqa: BLE001 - a position close can still proceed if listing orders fails.
        return [], [{"error": str(exc), "reason": "failed to list open orders"}]
    for order in orders:
        if not _is_open_order(order):
            continue
        order_id = _order_id(order)
        if not order_id:
            failed.append({"order": order, "reason": "order id missing"})
            continue
        try:
            result = cancel_order(order_id, account_name)
            canceled.append({"order_id": order_id, "symbol": order.get("symbol"), "order": order, "result": result})
        except Exception as exc:  # noqa: BLE001
            failed.append({"order_id": order_id, "symbol": order.get("symbol"), "order": order, "error": str(exc)})
    return canceled, failed


def _is_open_order(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or order.get("order_status") or "").strip().lower()
    if not status:
        return False
    terminal = {"filled", "rejected", "canceled", "cancelled", "expired", "withdrawn"}
    return status not in terminal


def _order_id(order: dict[str, Any]) -> str:
    for key in ("order_id", "id", "orderId"):
        value = str(order.get(key) or "").strip()
        if value:
            return value
    return ""


def _symbol(row: dict[str, Any]) -> str:
    for key in ("symbol", "stock_symbol", "security_symbol"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

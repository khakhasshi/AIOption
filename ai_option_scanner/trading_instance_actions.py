from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .broker_client import cancel_order, option_order_symbol, positions, submit_market_order
from .trading_instance import append_instance_event, hydrate_trade_instance, refresh_protection_from_orders, sanitize_instance_orders
from .trading_locks import run_action_lock
from .trading_store import delete_trading_run, get_trading_run, mark_trading_run


INSTANCE_FLATTEN_CONFIRMATION = "平实例"
INSTANCE_CANCEL_CONFIRMATION = "撤实例"
INSTANCE_DELETE_CONFIRMATION = "删除实例"
INSTANCE_BULK_DELETE_CONFIRMATION = "批量删除实例"
INSTANCE_RISK_RESET_CONFIRMATION = "初始化风控"


class InstanceHasLiveBrokerStateError(RuntimeError):
    """Raised when a local-only delete would orphan resting broker orders or open
    positions. Carries the detected state so the API can surface it to the user."""

    def __init__(self, message: str, live_state: dict[str, Any]):
        super().__init__(message)
        self.live_state = live_state


def cancel_trade_instance_orders(run_id: str, owner_id: str, account_name: str) -> dict[str, Any]:
    with run_action_lock(run_id) as acquired:
        if not acquired:
            return {
                "run_id": run_id, "account_name": account_name, "status": "busy",
                "message": "another flatten/monitor action is in progress for this instance; try again shortly",
                "canceled": [], "failed": [], "canceled_count": 0, "failed_count": 0,
            }
        return _cancel_trade_instance_orders_locked(run_id, owner_id, account_name)


def _cancel_trade_instance_orders_locked(run_id: str, owner_id: str, account_name: str) -> dict[str, Any]:
    run = _load_run(run_id, owner_id)
    orders = run.get("orders") or []
    canceled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen_order_ids: set[str] = set()
    for order in orders:
        for order_ref in _known_order_refs(order):
            order_id = _order_id(order_ref.get("order") or {})
            if not order_id or order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)
            try:
                canceled.append({"order_id": order_id, "kind": order_ref["kind"], "result": cancel_order(order_id, account_name)})
            except Exception as exc:  # noqa: BLE001
                if _is_already_cancelled(exc):
                    canceled.append({"order_id": order_id, "kind": order_ref["kind"], "result": {"status": "already_cancelled"}})
                else:
                    failed.append({"order_id": order_id, "kind": order_ref["kind"], "error": str(exc)})
    instance = dict(run.get("trade_instance") or {})
    if instance:
        append_instance_event(
            instance,
            "instance_orders_cancel_requested",
            f"实例相关订单撤单完成：成功 {len(canceled)}，失败 {len(failed)}。",
            lifecycle_state=instance.get("lifecycle_state") or "monitoring",
            status="warning" if failed else "success",
            payload={"canceled": canceled, "failed": failed},
        )
        mark_trading_run(run_id, instance_json=instance)
    return {
        "run_id": run_id,
        "account_name": account_name,
        "canceled_count": len(canceled),
        "failed_count": len(failed),
        "canceled": canceled,
        "failed": failed,
        "status": "partial_failed" if failed else "ok",
    }


def flatten_trade_instance(run_id: str, owner_id: str, account_name: str) -> dict[str, Any]:
    """Idempotency guard: serialize flatten against the monitor and any
    concurrent flatten on the same run, so we can't read the same `remaining`
    twice and submit two market closes (oversell)."""
    with run_action_lock(run_id) as acquired:
        if not acquired:
            return {
                "run_id": run_id,
                "account_name": account_name,
                "status": "busy",
                "message": "another flatten/monitor action is in progress for this instance; try again shortly",
                "submitted": [], "strategy_submitted": [], "failed": [], "strategy_failed": [], "skipped": [],
                "submitted_count": 0, "strategy_submitted_count": 0, "failed_count": 0, "strategy_failed_count": 0, "skipped_count": 0,
            }
        return _flatten_trade_instance_locked(run_id, owner_id, account_name)


def _flatten_trade_instance_locked(run_id: str, owner_id: str, account_name: str) -> dict[str, Any]:
    run = _load_run(run_id, owner_id)
    orders = run.get("orders") or []
    cancel_result = _cancel_trade_instance_orders_locked(run_id, owner_id, account_name)
    position_rows = positions(account_name)
    position_by_symbol = {_position_symbol(row): row for row in position_rows if _position_symbol(row)}
    submitted: list[dict[str, Any]] = []
    strategy_submitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    strategy_failed: list[dict[str, Any]] = []

    desired_by_symbol = _desired_close_quantities(orders)
    strategy_desired_legs = _desired_strategy_close_legs(orders)
    if not desired_by_symbol and not strategy_desired_legs:
        instance = dict(run.get("trade_instance") or {})
        if instance:
            append_instance_event(
                instance,
                "manual_flatten_instance",
                "实例没有确认成交的未平数量，未提交平仓单。",
                lifecycle_state=instance.get("lifecycle_state") or "blocked",
                status="info",
                payload={"cancel_result": cancel_result},
            )
            refresh_protection_from_orders(instance, orders)
            mark_trading_run(run_id, orders_json=orders, instance_json=instance)
        return {
            "run_id": run_id,
            "account_name": account_name,
            "positions_count": 0,
            "canceled_order_count": cancel_result.get("canceled_count", 0),
            "cancel_failed_count": cancel_result.get("failed_count", 0),
            "submitted_count": 0,
            "strategy_submitted_count": 0,
            "failed_count": 0,
            "strategy_failed_count": 0,
            "skipped_count": 0,
            "submitted": [],
            "strategy_submitted": [],
            "failed": [],
            "strategy_failed": [],
            "skipped": [],
            "cancel_result": cancel_result,
            "status": "no_confirmed_instance_position" if not cancel_result.get("failed_count") else "partial_failed",
        }
    for symbol, desired_quantity in desired_by_symbol.items():
        row = position_by_symbol.get(symbol)
        if not row:
            skipped.append({"symbol": symbol, "reason": "position not found"})
            continue
        net_quantity = _decimal(row.get("quantity"))
        available_quantity = _decimal(row.get("available_quantity"))
        if net_quantity is None or net_quantity == 0:
            skipped.append({"symbol": symbol, "reason": "net quantity is zero or unavailable", "position": row})
            continue
        if available_quantity is None:
            available_quantity = net_quantity
        close_quantity = min(abs(available_quantity), Decimal(desired_quantity))
        if close_quantity <= 0:
            skipped.append({"symbol": symbol, "reason": "available quantity is zero", "position": row})
            continue
        side = "sell" if net_quantity > 0 else "buy"
        try:
            close_order = submit_market_order(
                symbol,
                close_quantity,
                side,
                account_name,
                f"AI_OPTION_INSTANCE_FLATTEN {run_id[:8]} {side.upper()}",
            )
            submitted.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": str(close_quantity.normalize()),
                    "desired_quantity": desired_quantity,
                    "net_quantity": str(net_quantity.normalize()),
                    "available_quantity": str(available_quantity.normalize()),
                    "order": close_order,
                }
            )
            _mark_orders_flattened(orders, symbol, int(close_quantity), close_order)
        except Exception as exc:  # noqa: BLE001
            failed.append({"symbol": symbol, "side": side, "quantity": str(close_quantity.normalize()), "error": str(exc)})

    for desired in strategy_desired_legs:
        order_symbol = str(desired.get("order_symbol") or "").strip()
        quantity = int(_number(desired.get("quantity")))
        close_side = str(desired.get("side") or "").strip().lower()
        if not order_symbol or quantity <= 0 or close_side not in {"buy", "sell"}:
            skipped.append({"reason": "invalid strategy close leg", "desired": desired})
            continue
        try:
            close_order = submit_market_order(
                order_symbol,
                Decimal(quantity),
                close_side,
                account_name,
                f"AI_OPTION_INSTANCE_FLATTEN {run_id[:8]} STRATEGY {close_side.upper()}",
            )
            strategy_submitted.append({**desired, "order": close_order})
            _mark_strategy_leg_flattened(orders, desired, close_order)
        except Exception as exc:  # noqa: BLE001
            strategy_failed.append({**desired, "error": str(exc)})

    instance = dict(run.get("trade_instance") or {})
    if instance:
        submitted_total = len(submitted) + len(strategy_submitted)
        failed_total = len(failed) + len(strategy_failed)
        append_instance_event(
            instance,
            "manual_flatten_instance",
            f"实例平仓提交：单腿 {len(submitted)} 笔，策略腿 {len(strategy_submitted)} 笔，失败 {failed_total}，跳过 {len(skipped)}。",
            lifecycle_state="exiting" if submitted_total else "manual_intervention_required" if failed_total else instance.get("lifecycle_state") or "monitoring",
            status="error" if failed_total else "warning" if skipped else "success",
            payload={
                "submitted": submitted,
                "strategy_submitted": strategy_submitted,
                "failed": failed,
                "strategy_failed": strategy_failed,
                "skipped": skipped,
                "cancel_result": cancel_result,
            },
        )
        refresh_protection_from_orders(instance, orders)
        mark_trading_run(run_id, orders_json=orders, instance_json=instance)
    else:
        mark_trading_run(run_id, orders_json=orders)

    return {
        "run_id": run_id,
        "account_name": account_name,
        "positions_count": len(position_rows),
        "canceled_order_count": cancel_result.get("canceled_count", 0),
        "cancel_failed_count": cancel_result.get("failed_count", 0),
        "submitted_count": len(submitted),
        "strategy_submitted_count": len(strategy_submitted),
        "failed_count": len(failed),
        "strategy_failed_count": len(strategy_failed),
        "skipped_count": len(skipped),
        "submitted": submitted,
        "strategy_submitted": strategy_submitted,
        "failed": failed,
        "strategy_failed": strategy_failed,
        "skipped": skipped,
        "cancel_result": cancel_result,
        "status": "partial_failed" if failed or strategy_failed or cancel_result.get("failed_count") else "ok",
    }


def _instance_live_broker_state(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect broker-side state an instance would orphan if deleted locally.

    A local-only delete removes the DB row but never touches the broker, so any
    resting order (unfilled entry, stop, take-profit, exit) keeps working and any
    open position keeps its market risk — with nothing left in the app to manage
    or even show them. This reads the local order records for both signals so the
    caller can refuse or warn before orphaning them.
    """
    resting_orders: list[dict[str, Any]] = []
    seen_order_ids: set[str] = set()
    for order in orders or []:
        for order_ref in _known_order_refs(order):
            order_id = _order_id(order_ref.get("order") or {})
            if not order_id or order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)
            resting_orders.append({"order_id": order_id, "kind": order_ref["kind"]})
    open_positions = _desired_close_quantities(orders or [])
    return {
        "resting_orders": resting_orders,
        "open_positions": open_positions,
        "has_live_state": bool(resting_orders or open_positions),
    }


def _live_state_message(live_state: dict[str, Any]) -> str:
    parts: list[str] = []
    resting = live_state.get("resting_orders") or []
    positions_map = live_state.get("open_positions") or {}
    if resting:
        parts.append(f"{len(resting)} 笔挂单")
    if positions_map:
        leg_desc = "、".join(f"{symbol}×{qty}" for symbol, qty in positions_map.items())
        parts.append(f"未平仓位（{leg_desc}）")
    detail = "、".join(parts) if parts else "券商侧未结状态"
    return (
        f"该实例仍有{detail}，本地删除不会撤单或平仓，会留下券商侧孤儿订单/仓位。"
        "请先撤实例订单并平仓，或在确认知情后强制删除。"
    )


def reset_trade_instance_risk(run_id: str, owner_id: str) -> dict[str, Any]:
    run = _load_run(run_id, owner_id)
    if run.get("status") in {"queued", "running"}:
        raise ValueError("交易实例仍在运行中，不能初始化风控状态")
    orders = sanitize_instance_orders(run.get("orders") or [])
    live_state = _instance_live_broker_state(orders)
    instance = hydrate_trade_instance(
        dict(run.get("trade_instance") or {}),
        run_id=run_id,
        owner_id=owner_id,
        config=run.get("config") or {},
        orders=orders,
        created_at=run.get("created_at"),
    )
    append_instance_event(
        instance,
        "risk_state_reinitialized",
        "已按当前订单记录重新初始化风控与保护状态。"
        + ("（注意：存在券商侧挂单/持仓，本次仅按本地记录重建，未与券商对账）" if live_state["has_live_state"] else ""),
        lifecycle_state=instance.get("lifecycle_state") or "created",
        status="warning" if (live_state["has_live_state"] or (instance.get("protection_status") or {}).get("requires_manual_attention")) else "success",
        payload={
            "order_count": len(orders),
            "protection_status": instance.get("protection_status") or {},
            "review_metrics": instance.get("review_metrics") or {},
            "broker_reconcile": "local_only",
            "live_broker_state": live_state,
        },
    )
    mark_trading_run(run_id, orders_json=orders, instance_json=instance)
    return {
        "run_id": run_id,
        "status": "ok",
        "order_count": len(orders),
        "lifecycle_state": instance.get("lifecycle_state"),
        "protection_status": instance.get("protection_status") or {},
        "review_metrics": instance.get("review_metrics") or {},
        "broker_reconcile": "local_only",
        "live_broker_state": live_state,
    }


def delete_trade_instance(run_id: str, owner_id: str, force: bool = False) -> dict[str, Any]:
    run = _load_run(run_id, owner_id)
    if run.get("status") in {"queued", "running"}:
        raise ValueError("交易实例仍在运行中，不能删除")
    live_state = _instance_live_broker_state(sanitize_instance_orders(run.get("orders") or []))
    if live_state["has_live_state"] and not force:
        raise InstanceHasLiveBrokerStateError(_live_state_message(live_state), live_state)
    deleted = delete_trading_run(run_id, owner_id)
    if not deleted:
        raise ValueError("交易实例不存在")
    return {
        "run_id": run_id,
        "status": "deleted",
        "deleted": True,
        "local_only": True,
        "forced": bool(force and live_state["has_live_state"]),
        "orphaned_broker_state": live_state if live_state["has_live_state"] else None,
    }


def bulk_delete_trade_instances(run_ids: list[str], owner_id: str, force: bool = False) -> dict[str, Any]:
    seen: set[str] = set()
    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for raw_id in run_ids:
        run_id = str(raw_id or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        try:
            result = delete_trade_instance(run_id, owner_id, force=force)
            deleted.append({"run_id": run_id, "status": result.get("status"), "forced": result.get("forced")})
        except InstanceHasLiveBrokerStateError as exc:
            # Don't fail the whole batch — surface which instances need attention.
            skipped.append({"run_id": run_id, "reason": "live_broker_state", "message": str(exc), "live_broker_state": exc.live_state})
        except Exception as exc:  # noqa: BLE001
            failed.append({"run_id": run_id, "error": str(exc)})
    return {
        "status": "partial_failed" if failed else "skipped_live_state" if skipped else "ok",
        "requested_count": len(seen),
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "deleted": deleted,
        "failed": failed,
        "skipped": skipped,
        "local_only": True,
    }


def _load_run(run_id: str, owner_id: str) -> dict[str, Any]:
    run = get_trading_run(run_id, owner_id)
    if run is None:
        raise ValueError("交易实例不存在")
    return run


def _known_order_refs(order: dict[str, Any]) -> list[dict[str, Any]]:
    refs = []
    quantity = int(_number(order.get("quantity")))
    if order.get("entry_order") and (quantity <= 0 or _confirmed_filled_quantity(order) < quantity):
        refs.append({"kind": "entry_order", "order": order["entry_order"]})
    for key in ("stop_order", "software_stop_order", "software_take_profit_order", "single_leg_smart_exit_order"):
        if order.get(key):
            refs.append({"kind": key, "order": order[key]})
    for stop_order in order.get("stop_orders") or []:
        refs.append({"kind": "stop_orders", "order": stop_order})
    for leg in order.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        leg_quantity = int(_number(leg.get("quantity")))
        if leg.get("entry_order") and (leg_quantity <= 0 or int(_number(leg.get("filled_quantity"))) < leg_quantity):
            refs.append({"kind": "strategy_leg_entry_order", "order": leg["entry_order"]})
        if leg.get("strategy_exit_order"):
            refs.append({"kind": "strategy_leg_strategy_exit_order", "order": leg["strategy_exit_order"]})
    return refs


def _desired_close_quantities(orders: list[dict[str, Any]]) -> dict[str, int]:
    output: dict[str, int] = {}
    for order in orders:
        symbol = str(order.get("order_symbol") or "")
        if not symbol:
            continue
        filled = _confirmed_filled_quantity(order)
        closed = _closed_or_pending_close_quantity(order)
        remaining = max(0, filled - closed)
        if remaining > 0:
            output[symbol] = output.get(symbol, 0) + remaining
    return output


def _mark_orders_flattened(orders: list[dict[str, Any]], symbol: str, quantity: int, close_order: dict[str, Any]) -> None:
    remaining = quantity
    for order in orders:
        if str(order.get("order_symbol") or "") != symbol or remaining <= 0:
            continue
        filled = _confirmed_filled_quantity(order)
        already_closed = _closed_or_pending_close_quantity(order)
        close_quantity = min(max(0, filled - already_closed), remaining)
        if close_quantity <= 0:
            continue
        order["instance_flatten_order"] = close_order
        order["instance_flatten_submitted_quantity"] = int(_number(order.get("instance_flatten_submitted_quantity"))) + close_quantity
        order["software_stop_active"] = False
        order["software_take_profit_active"] = False
        order["single_leg_smart_exit_active"] = False
        order["software_stop_quantity"] = 0
        order["software_take_profit_quantity"] = 0
        order["single_leg_smart_exit_quantity"] = 0
        order["status"] = "instance_flatten_submitted"
        order["monitor_status"] = "instance_flatten_submitted"
        remaining -= close_quantity


def _order_id(order: dict[str, Any]) -> str:
    for key in ("order_id", "id", "orderId"):
        value = str(order.get(key) or "").strip()
        if value:
            return value
    return ""


def _confirmed_filled_quantity(order: dict[str, Any]) -> int:
    explicit = order.get("entry_filled_quantity")
    if explicit is not None:
        return max(0, int(_number(explicit)))
    detail = order.get("entry_detail") if isinstance(order.get("entry_detail"), dict) else {}
    for key in ("executed_quantity", "filled_quantity", "quantity_filled", "filled"):
        quantity = int(_number(detail.get(key)))
        if quantity > 0:
            return quantity
    return 0


def _closed_or_pending_close_quantity(order: dict[str, Any]) -> int:
    tp_closed = int(_number(order.get("software_take_profit_closed_quantity")))
    tp_submitted = int(_number(order.get("software_take_profit_submitted_quantity")))
    stop_closed = int(_number(order.get("software_stop_closed_quantity")))
    stop_submitted = int(_number(order.get("software_stop_submitted_quantity")))
    smart_exit_closed = int(_number(order.get("single_leg_smart_exit_closed_quantity")))
    smart_exit_submitted = int(_number(order.get("single_leg_smart_exit_submitted_quantity")))
    flattened = int(_number(order.get("instance_flatten_closed_quantity")))
    flatten_submitted = int(_number(order.get("instance_flatten_submitted_quantity")))
    return (
        max(tp_closed, tp_submitted)
        + max(stop_closed, stop_submitted)
        + max(smart_exit_closed, smart_exit_submitted)
        + max(flattened, flatten_submitted)
    )


def _confirmed_leg_filled_quantity(order: dict[str, Any], entry: dict[str, Any]) -> int:
    """Confirmed fill quantity for one strategy leg, used to decide what to close.

    A leg counts as open ONLY when there is positive evidence it filled:
      - an explicit positive ``filled_quantity`` on the leg, OR
      - the parent combo order confirms a fill (``entry_filled_quantity`` > 0)
        AND the leg is not explicitly recorded as zero-filled.
    We must NOT fall back to the *planned* ``quantity`` for a leg that never
    filled — doing so would submit a close for a non-existent position and open
    a new naked leg in the opposite direction.
    """
    explicit = entry.get("filled_quantity")
    if explicit is not None:
        return max(0, int(_number(explicit)))
    # No per-leg fill recorded: only trust the planned quantity when the parent
    # combo confirms it entered (some execution paths stamp the fill only on the
    # parent order, not each leg).
    if int(_number(order.get("entry_filled_quantity"))) > 0:
        return max(0, int(_number(entry.get("quantity"))))
    return 0


def _desired_strategy_close_legs(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    desired: list[dict[str, Any]] = []
    for order in orders:
        tracking_id = str(order.get("tracking_id") or "")
        for index, entry in enumerate(order.get("legs") or []):
            if not isinstance(entry, dict):
                continue
            leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
            contract_symbol = str(entry.get("contract_symbol") or leg.get("contract_symbol") or "").strip()
            if not contract_symbol:
                continue
            filled = _confirmed_leg_filled_quantity(order, entry)
            exited = int(_number(entry.get("strategy_exit_quantity") or entry.get("strategy_exit_filled_quantity")))
            remaining = max(0, filled - exited)
            if remaining <= 0:
                continue
            action = str(leg.get("action") or entry.get("action") or "").strip().lower()
            close_side = "sell" if action == "buy" else "buy" if action == "sell" else ""
            order_symbol = str(entry.get("order_symbol") or option_order_symbol(contract_symbol)).strip()
            desired.append(
                {
                    "tracking_id": tracking_id,
                    "leg_index": index,
                    "contract_symbol": contract_symbol,
                    "order_symbol": order_symbol,
                    "side": close_side,
                    "quantity": remaining,
                    "filled_quantity": filled,
                    "already_exiting_quantity": exited,
                    "strategy_type": order.get("strategy_type"),
                }
            )
    return desired


def _mark_strategy_leg_flattened(orders: list[dict[str, Any]], desired: dict[str, Any], close_order: dict[str, Any]) -> None:
    tracking_id = str(desired.get("tracking_id") or "")
    leg_index = int(_number(desired.get("leg_index")))
    quantity = int(_number(desired.get("quantity")))
    for order in orders:
        if str(order.get("tracking_id") or "") != tracking_id:
            continue
        legs = order.get("legs") or []
        if leg_index >= len(legs) or not isinstance(legs[leg_index], dict):
            continue
        entry = legs[leg_index]
        entry["strategy_exit_order"] = close_order
        entry["strategy_exit_side"] = desired.get("side")
        entry["strategy_exit_quantity"] = int(_number(entry.get("strategy_exit_quantity"))) + quantity
        entry["strategy_exit_trigger"] = "instance_flatten"
        entry["strategy_exit_status"] = "submitted"
        order["strategy_exit_status"] = "submitted"
        order["strategy_exit_trigger"] = "instance_flatten"
        order["risk_tracking_active"] = False
        order["status"] = "strategy_auto_exit_submitted"
        order["monitor_status"] = "strategy_auto_exit_submitted"
        break


def _is_already_cancelled(exc: Exception) -> bool:
    message = str(exc).lower()
    return "601011" in message or "order has been cancelled" in message


def _position_symbol(row: dict[str, Any]) -> str:
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


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .account_store import utc_now
from . import adaptive_pricing
from .broker_client import account_ref_for_config, cancel_order, executions as broker_executions, option_order_symbol, order_detail, positions, quote, submit_buy_order, submit_market_order, submit_sell_order, submit_stop_sell_order, wait_for_order_fill
from .option_symbol_utils import option_symbol_aliases, option_symbol_for_occ
from .longbridge_option_tool import quote_option_contract as longbridge_sdk_option_quote
from .redis_runtime import redis_available, redis_del, redis_setnx
from .smart_exit_rules import evaluate_exit_rules, normalize_exit_rules
from .time_utils import EASTERN, now_et, parse_datetime
from .trading_locks import run_action_lock
from .trading_instance import annotate_strategy_order_fill_ledger, append_instance_event, refresh_protection_from_orders
from .trading_store import find_recent_order_journal, get_trading_run, list_monitorable_trading_runs, list_trading_runs as _list_trading_runs, mark_trading_run, record_order_journal
from .trading_idempotency import client_order_key, idempotency_enabled


_started = False
_lock = threading.Lock()
_monitor_lock = threading.Lock()
_LOG = logging.getLogger(__name__)
_redis_monitor_lock = "ai-option:trading-monitor-lock"
# Quote cache for the current monitor cycle — keyed by "contract_symbol|account_name|source".
# Cleared at the start of every monitor_pending_stops() call so stale prices never persist
# across cycles. Eliminates redundant API calls when multiple orders track the same contract.
_cycle_quote_cache: dict[str, dict[str, Any]] = {}
try:
    ORDER_MONITOR_INTERVAL_SECONDS = max(1.0, float(os.getenv("AI_OPTION_ORDER_MONITOR_INTERVAL_SECONDS") or 5))
except ValueError:
    ORDER_MONITOR_INTERVAL_SECONDS = 5.0
_last_monitor_snapshot: dict[str, Any] = {
    "status": "not_started",
    "started_at": None,
    "finished_at": None,
    "finished_monotonic": None,
    "summary": {},
}


def start_order_monitor() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="trading-order-monitor", daemon=True).start()


def _loop() -> None:
    while True:
        try:
            monitor_pending_stops()
        except Exception:
            # A persistent failure here silently disables software-stop/take-profit
            # protection for every instance; log it so the outage is observable.
            _LOG.exception("order monitor cycle failed")
        time.sleep(ORDER_MONITOR_INTERVAL_SECONDS)


def _monitor_process_one_run(run: dict[str, Any], summary: dict[str, Any]) -> None:
    """Process protection/exits for one trading run. Called under the per-run
    action lock so it cannot race a user-triggered flatten/cancel on the same
    instance. Extracted verbatim from the monitor loop body (outer-loop
    `continue` became `return`)."""
    orders = run.get("orders") or []
    instance = dict(run.get("trade_instance") or {})
    has_strategy_tracking = _has_monitorable_strategy(instance)
    if not _has_monitorable_order(orders) and not has_strategy_tracking:
        return
    summary["runs_checked"] += 1
    changed = False
    config = run.get("config") or {}
    try:
        account_name = account_ref_for_config(config, owner_id=run.get("owner_id"))
    except Exception as exc:  # noqa: BLE001 - surface account failures on the instance instead of silently skipping protection.
        if _handle_monitor_account_error(run, orders, instance, exc):
            summary["orders_changed"] += 1
        return
    market_data_source = _normalize_monitor_market_data_source(config.get("market_data_source"))
    exit_order_type = adaptive_pricing.normalize_order_type(config.get("exit_order_type"))
    for order in orders:
        order["market_data_source"] = _normalize_monitor_market_data_source(order.get("market_data_source") or market_data_source)
        order["exit_order_type"] = adaptive_pricing.normalize_order_type(order.get("exit_order_type") or exit_order_type)
        if order.get("status") not in {"entry_submitted_stop_pending_unfilled", "entry_partially_filled_stop_partial"}:
            pass
        else:
            changed = _try_attach_stop(
                order,
                account_name,
                bool((run.get("config") or {}).get("software_stop_enabled", True)),
                bool((run.get("config") or {}).get("software_take_profit_enabled", True)),
            ) or changed
        residual_position_result = _try_residual_position_reconcile(order, account_name)
        if residual_position_result.get("changed"):
            changed = True
        if residual_position_result.get("closed"):
            continue
        software_result = _try_software_stop(order, account_name)
        if software_result.get("changed"):
            changed = True
            summary["software_stop_triggered"] += int(software_result.get("triggered") or 0)
            summary["software_stop_failed"] += int(software_result.get("failed") or 0)
        if software_result.get("triggered") or software_result.get("failed") or software_result.get("stop_triggered"):
            continue
        take_profit_result = _try_software_take_profit(order, account_name)
        if take_profit_result.get("changed"):
            changed = True
            summary["software_take_profit_triggered"] += int(take_profit_result.get("triggered") or 0)
            summary["software_take_profit_failed"] += int(take_profit_result.get("failed") or 0)
        if take_profit_result.get("triggered") or take_profit_result.get("failed"):
            continue
        smart_exit_result = _try_single_leg_smart_exit(order, account_name)
        if smart_exit_result.get("changed"):
            changed = True
            summary["single_leg_smart_exit_triggered"] += int(smart_exit_result.get("triggered") or 0)
            summary["single_leg_smart_exit_failed"] += int(smart_exit_result.get("failed") or 0)
        single_leg_exit_result = _try_single_leg_exit_reconcile(order, account_name)
        if single_leg_exit_result.get("changed"):
            changed = True
            summary["software_stop_failed"] += int(single_leg_exit_result.get("software_stop_failed") or 0)
            summary["software_take_profit_failed"] += int(single_leg_exit_result.get("software_take_profit_failed") or 0)
            summary["single_leg_smart_exit_failed"] += int(single_leg_exit_result.get("single_leg_smart_exit_failed") or 0)
        strategy_exit_result = _try_strategy_exit_reconcile(order, instance, account_name)
        if strategy_exit_result.get("changed"):
            changed = True
            summary["strategy_auto_exit_failed"] += int(strategy_exit_result.get("failed") or 0)
        residual_exit_result = _try_residual_exit_reconcile(order, account_name)
        if residual_exit_result.get("changed"):
            changed = True
    if has_strategy_tracking:
        for position in ((instance.get("risk_plan") or {}).get("strategy_positions") or []):
            if isinstance(position, dict):
                position["market_data_source"] = _normalize_monitor_market_data_source(position.get("market_data_source") or market_data_source)
    strategy_result = _try_strategy_risk_tracking(instance, account_name, orders) if has_strategy_tracking else {"changed": False}
    if strategy_result.get("changed"):
        changed = True
        summary["strategy_stop_alerted"] += int(strategy_result.get("stop_alerted") or 0)
        summary["strategy_take_profit_alerted"] += int(strategy_result.get("take_profit_alerted") or 0)
        summary["strategy_smart_exit_alerted"] += int(strategy_result.get("smart_exit_alerted") or 0)
        summary["strategy_auto_exit_submitted"] += int(strategy_result.get("auto_exit_submitted") or 0)
        summary["strategy_auto_exit_failed"] += int(strategy_result.get("auto_exit_failed") or 0)
    if changed:
        summary["orders_changed"] += 1
        if instance:
            for order in orders:
                for event in order.pop("_instance_events", []) or []:
                    append_instance_event(instance, **event)
                event = order.pop("_instance_event", None)
                if event:
                    append_instance_event(instance, **event)
            if orders:
                refresh_protection_from_orders(instance, orders)
            mark_trading_run(run["id"], orders_json=orders, instance_json=instance)
        else:
            mark_trading_run(run["id"], orders_json=orders)


def monitor_pending_stops(limit: int = 100) -> dict[str, Any]:
    _cycle_quote_cache.clear()  # Invalidate quotes from the previous cycle
    has_redis = redis_available()
    started_at = utc_now()
    if has_redis and not redis_setnx(_redis_monitor_lock, "1", 60):
        return _record_monitor_result(
            "busy",
            started_at,
            {
            "status": "busy",
            "runs_checked": 0,
            "orders_changed": 0,
            "software_stop_triggered": 0,
            "software_stop_failed": 0,
            "software_take_profit_triggered": 0,
            "software_take_profit_failed": 0,
            "single_leg_smart_exit_triggered": 0,
            "single_leg_smart_exit_failed": 0,
            "strategy_stop_alerted": 0,
            "strategy_take_profit_alerted": 0,
            "strategy_smart_exit_alerted": 0,
            "strategy_auto_exit_submitted": 0,
            "strategy_auto_exit_failed": 0,
            },
        )
    if not _monitor_lock.acquire(blocking=False):
        if has_redis:
            redis_del(_redis_monitor_lock)
        return _record_monitor_result(
            "busy",
            started_at,
            {
            "status": "busy",
            "runs_checked": 0,
            "orders_changed": 0,
            "software_stop_triggered": 0,
            "software_stop_failed": 0,
            "software_take_profit_triggered": 0,
            "software_take_profit_failed": 0,
            "single_leg_smart_exit_triggered": 0,
            "single_leg_smart_exit_failed": 0,
            "strategy_stop_alerted": 0,
            "strategy_take_profit_alerted": 0,
            "strategy_smart_exit_alerted": 0,
            "strategy_auto_exit_submitted": 0,
            "strategy_auto_exit_failed": 0,
            },
        )
    summary = {
        "runs_checked": 0,
        "orders_changed": 0,
        "software_stop_triggered": 0,
        "software_stop_failed": 0,
        "software_take_profit_triggered": 0,
        "software_take_profit_failed": 0,
        "single_leg_smart_exit_triggered": 0,
        "single_leg_smart_exit_failed": 0,
        "strategy_stop_alerted": 0,
        "strategy_take_profit_alerted": 0,
        "strategy_smart_exit_alerted": 0,
        "strategy_auto_exit_submitted": 0,
        "strategy_auto_exit_failed": 0,
    }
    try:
        for run in list_trading_runs(None, limit, summary=False):
            try:
                orders = run.get("orders") or []
                instance = dict(run.get("trade_instance") or {})
                has_strategy_tracking = _has_monitorable_strategy(instance)
                if not _has_monitorable_order(orders) and not has_strategy_tracking:
                    continue
                # Serialize against user-triggered flatten/cancel on the same run so
                # the monitor's read-modify-write can't clobber a concurrent action
                # (or re-submit an exit a flatten just handled). If a user action
                # holds the lock, skip this run this cycle; we pick it up next tick.
                with run_action_lock(run["id"], timeout_seconds=0.5) as run_locked:
                    if not run_locked:
                        summary["runs_skipped_locked"] = summary.get("runs_skipped_locked", 0) + 1
                        continue
                    # Re-read under the lock so we act on the freshest persisted state.
                    fresh = get_trading_run(run["id"], run.get("owner_id"))
                    if fresh:
                        run = fresh
                    _monitor_process_one_run(run, summary)
            except Exception:  # noqa: BLE001 - one poison run must not skip the rest.
                # Without this, an exception on run N (malformed order JSON, a
                # broker helper throwing) propagates out of the loop and EVERY
                # later run is skipped this cycle — their stops/take-profits/smart
                # exits never run, so those live positions sit unprotected until
                # the bad run is cleared. Isolate per-run like the scheduler does.
                summary["runs_errored"] = summary.get("runs_errored", 0) + 1
                _LOG.exception("monitor: run %s failed; continuing", run.get("id"))
        return _record_monitor_result("ok", started_at, summary)
    finally:
        _monitor_lock.release()
        if has_redis:
            redis_del(_redis_monitor_lock)


def order_monitor_runtime_snapshot() -> dict[str, Any]:
    snapshot = dict(_last_monitor_snapshot)
    finished_monotonic = snapshot.pop("finished_monotonic", None)
    if finished_monotonic:
        snapshot["lag_seconds"] = round(time.monotonic() - float(finished_monotonic), 1)
    else:
        snapshot["lag_seconds"] = None
    snapshot["running"] = _monitor_lock.locked()
    return snapshot


def _record_monitor_result(status: str, started_at: str, summary: dict[str, Any]) -> dict[str, Any]:
    summary.setdefault("status", status)
    _last_monitor_snapshot.update(
        {
            "status": status,
            "started_at": started_at,
            "finished_at": utc_now(),
            "finished_monotonic": time.monotonic(),
            "summary": dict(summary),
        }
    )
    return summary


def _handle_monitor_account_error(run: dict[str, Any], orders: list[dict[str, Any]], instance: dict[str, Any], exc: Exception) -> bool:
    error_text = f"broker account unavailable: {exc}"
    changed = False
    for order in orders:
        if not isinstance(order, dict):
            continue
        if _has_monitorable_order([order]) and order.get("monitor_account_error") != error_text:
            order["monitor_account_error"] = error_text
            order["monitor_status"] = "broker_account_unavailable"
            changed = True
    risk_plan = instance.get("risk_plan") if isinstance(instance.get("risk_plan"), dict) else {}
    positions = risk_plan.get("strategy_positions") if isinstance(risk_plan.get("strategy_positions"), list) else []
    for position in positions:
        if not isinstance(position, dict) or not position.get("risk_tracking_active"):
            continue
        if position.get("last_error") == error_text and position.get("tracking_status") == "broker_account_unavailable":
            continue
        position["tracking_status"] = "broker_account_unavailable"
        position["last_error"] = error_text
        changed = True
    if positions:
        risk_plan["strategy_positions"] = positions
        instance["risk_plan"] = risk_plan
    if instance and changed:
        if orders:
            refresh_protection_from_orders(instance, orders)
        timeline = instance.get("event_timeline") if isinstance(instance.get("event_timeline"), list) else []
        last_event = timeline[-1] if timeline and isinstance(timeline[-1], dict) else {}
        if last_event.get("event_type") != "broker_account_unavailable" or (last_event.get("payload") or {}).get("error") != error_text:
            append_instance_event(
                instance,
                "broker_account_unavailable",
                "券商账号不可用，实盘保护监控无法提交或对账订单，需要检查账号配置。",
                lifecycle_state="manual_intervention_required",
                status="error",
                payload={"error": error_text},
            )
        mark_trading_run(run["id"], orders_json=orders, instance_json=instance)
    elif changed:
        mark_trading_run(run["id"], orders_json=orders)
    return changed


def list_trading_runs(owner_id: str | None = None, limit: int = 100, summary: bool = False) -> list[dict[str, Any]]:
    if summary:
        return _list_trading_runs(owner_id, limit, summary=True)
    return list_monitorable_trading_runs(limit)


def _try_attach_stop(
    order: dict[str, Any],
    account_name: str,
    software_stop_enabled: bool = True,
    software_take_profit_enabled: bool = True,
) -> bool:
    order_id = _order_id(order.get("entry_order") or {})
    if not order_id:
        order["monitor_error"] = "entry order id missing"
        return True
    filled_quantity = int(order.get("entry_filled_quantity") or 0)
    try:
        detail = order_detail(order_id, account_name)
        filled_quantity = _filled_quantity(detail)
        covered_quantity = int(order.get("covered_quantity") or 0)
        status = _order_status(detail)
        order["entry_detail"] = detail
        if filled_quantity <= covered_quantity:
            if filled_quantity < 1 and _is_terminal_unfilled_status(status):
                order["status"] = "entry_terminal_no_stop"
                order["monitor_status"] = "completed"
                order["message"] = f"entry order ended as `{status}` without any fill; no stop order was submitted"
                order.pop("monitor_error", None)
                return True
            order["monitor_status"] = "waiting_entry_fill"
            return True
        executed_price = _executed_price(detail)
        if executed_price > 0:
            stop_loss_pct = float(order.get("stop_loss_pct") or 25)
            order.setdefault("planned_entry_price", order.get("entry_price"))
            order["entry_price"] = executed_price
            order["actual_entry_price"] = executed_price
            order["entry_price_source"] = "executed_price"
            order["stop_trigger_price"] = round(executed_price * (1 - stop_loss_pct / 100), 2)
        if float(order.get("stop_trigger_price") or 0) <= 0:
            order["monitor_error"] = "invalid stop trigger price"
            return True
        order["entry_filled_quantity"] = max(int(order.get("entry_filled_quantity") or 0), filled_quantity)
        incremental_quantity = max(0, filled_quantity - covered_quantity)
        if incremental_quantity < 1:
            order["monitor_status"] = "waiting_entry_fill"
            return True
        stop_order = submit_stop_sell_order(
            order["order_symbol"],
            incremental_quantity,
            float(order.get("stop_trigger_price") or 0),
            account_name,
            f"AI_OPTION_STOP {order.get('symbol')} entry={order_id}",
        )
        stop_orders = order.setdefault("stop_orders", [])
        if isinstance(stop_orders, list):
            stop_orders.append(stop_order)
        else:
            order["stop_orders"] = [stop_order]
        if not order.get("stop_order"):
            order["stop_order"] = stop_order
        order["covered_quantity"] = covered_quantity + incremental_quantity
        _arm_software_take_profit(order, order["entry_filled_quantity"], software_take_profit_enabled)
        order.pop("monitor_error", None)
        total_quantity = int(order.get("quantity") or 0)
        if order["covered_quantity"] >= total_quantity or _is_terminal_status(status):
            order["status"] = "stop_submitted_after_fill"
            order["monitor_status"] = "completed"
        else:
            order["status"] = "entry_partially_filled_stop_partial"
            order["monitor_status"] = "waiting_additional_fill"
        return True
    except Exception as exc:  # noqa: BLE001 - broker-specific stop attach errors must not abort the monitor loop.
        if _is_stop_unsupported(exc):
            order["status"] = "entry_filled_stop_unsupported_paper"
            order["monitor_status"] = "completed"
            order["entry_filled_quantity"] = max(int(order.get("entry_filled_quantity") or 0), filled_quantity)
            order["covered_quantity"] = int(order.get("covered_quantity") or 0)
            order["stop_error"] = str(exc)
            order["message"] = "entry order filled, but this broker does not support automatic stop trigger orders; software protection is armed"
            _arm_software_stop(order, int(order.get("entry_filled_quantity") or 0), "broker_stop_unsupported", software_stop_enabled)
            _arm_software_take_profit(order, int(order.get("entry_filled_quantity") or 0), software_take_profit_enabled)
            order.pop("monitor_error", None)
            return True
        order["monitor_error"] = str(exc)
        return True


def _protection_quote_outage_threshold() -> int:
    try:
        return max(1, int(os.getenv("AI_OPTION_PROTECTION_QUOTE_OUTAGE_CYCLES", "5") or 5))
    except (TypeError, ValueError):
        return 5


def _residual_flat_confirm_cycles() -> int:
    try:
        return max(1, int(os.getenv("AI_OPTION_RESIDUAL_FLAT_CONFIRM_CYCLES", "2") or 2))
    except (TypeError, ValueError):
        return 2


def _software_stop_confirm_cycles() -> int:
    # Number of consecutive breach observations required before a software stop
    # fires its market sell. Debounces a single bad/spiked-down bid tick on the
    # exit quote. Default 2; set to 1 to fire on the first breach (legacy).
    try:
        return max(1, int(os.getenv("AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES", "2") or 2))
    except (TypeError, ValueError):
        return 2


def _note_protection_quote_outage(order: dict[str, Any], kind: str) -> None:
    """Track consecutive cycles where a protection quote was unavailable. After a
    threshold, flag the order for manual attention so an armed-but-unprotected
    position (quotes persistently failing → stop never evaluates) is surfaced
    instead of silently sitting unprotected."""
    key = f"{kind}_quote_outage_cycles"
    count = int(order.get(key) or 0) + 1
    order[key] = count
    if count >= _protection_quote_outage_threshold() and not order.get(f"{kind}_quote_outage_alerted"):
        order[f"{kind}_quote_outage_alerted"] = True
        order["requires_manual_attention"] = True
        order["_instance_event"] = {
            "event": "protection_quote_outage",
            "message": f"{kind} 保护行情连续 {count} 个监控周期不可用，可能无法按计划触发，请人工检查。",
            "status": "warning",
            "payload": {"kind": kind, "cycles": count},
        }


def _clear_protection_quote_outage(order: dict[str, Any], kind: str) -> None:
    if order.get(f"{kind}_quote_outage_cycles"):
        order[f"{kind}_quote_outage_cycles"] = 0
    order.pop(f"{kind}_quote_outage_alerted", None)


def _try_software_stop(order: dict[str, Any], account_name: str) -> dict[str, int | bool]:
    if not order.get("software_stop_active"):
        return {"changed": False, "triggered": 0, "failed": 0}
    if _retry_waiting(order, "software_stop"):
        return {"changed": False, "triggered": 0, "failed": 0}
    close_quantity = int(order.get("software_stop_quantity") or 0)
    remaining_quantity = _remaining_open_quantity(order)
    if remaining_quantity < close_quantity:
        close_quantity = remaining_quantity
        order["software_stop_quantity"] = close_quantity
    stop_price = float(order.get("stop_trigger_price") or 0)
    symbol = str(order.get("order_symbol") or "")
    if close_quantity < 1 or stop_price <= 0 or not symbol:
        order["software_stop_active"] = False
        order["software_stop_status"] = "not_armed_invalid_quantity_or_stop"
        order["software_stop_error"] = "invalid software stop quantity, symbol, or trigger price"
        order["status"] = "software_stop_failed"
        return {"changed": True, "triggered": 0, "failed": 1}
    quote_row = _software_stop_quote(order, account_name)
    order["software_stop_last_check_at"] = utc_now()
    order["software_stop_last_quote"] = quote_row
    if not quote_row.get("available"):
        order["software_stop_status"] = "quote_unavailable"
        order["software_stop_error"] = str(quote_row.get("error") or "software stop quote unavailable")
        _note_protection_quote_outage(order, "software_stop")
        return {"changed": True, "triggered": 0, "failed": 0}
    _clear_protection_quote_outage(order, "software_stop")
    current_price = float(quote_row.get("exit_price") or 0)
    if current_price <= 0 or current_price > stop_price:
        order["software_stop_status"] = "armed"
        order["software_stop_breach_cycles"] = 0
        order.pop("software_stop_error", None)
        return {"changed": True, "triggered": 0, "failed": 0}
    # Breach observed. Debounce a single spiked-down bid tick: require N
    # consecutive breach observations before the (irreversible) market sell.
    # Set AI_OPTION_SOFTWARE_STOP_CONFIRM_CYCLES=1 to restore instant firing.
    confirm_cycles = _software_stop_confirm_cycles()
    breach_cycles = int(order.get("software_stop_breach_cycles") or 0) + 1
    order["software_stop_breach_cycles"] = breach_cycles
    if breach_cycles < confirm_cycles:
        order["software_stop_status"] = "breach_pending_confirm"
        order["software_stop_breach_quote"] = current_price
        return {"changed": True, "triggered": 0, "failed": 0}
    order["software_stop_breach_cycles"] = 0
    try:
        close_order = submit_market_order(
            symbol,
            close_quantity,
            "sell",
            account_name,
            f"AI_OPTION_SW_STOP {order.get('symbol')} stop={stop_price:.2f}",
        )
        order["software_stop_active"] = False
        order["software_stop_status"] = "market_close_submitted"
        order["software_stop_triggered_at"] = utc_now()
        order["software_stop_trigger_price"] = stop_price
        order["software_stop_trigger_quote"] = current_price
        order["software_stop_order"] = close_order
        order["software_stop_submitted_quantity"] = close_quantity
        order["software_stop_quantity"] = 0
        order["status"] = "software_stop_submitted"
        order["monitor_status"] = "software_stop_submitted"
        _mark_residual_exit_submitted(order, "software_stop", close_order)
        if _remaining_open_quantity(order) <= 0:
            order["software_take_profit_active"] = False
            order["software_take_profit_quantity"] = 0
            order["software_take_profit_status"] = "completed_after_software_stop"
            order["single_leg_smart_exit_active"] = False
            order["single_leg_smart_exit_quantity"] = 0
            order["single_leg_smart_exit_status"] = "completed_after_software_stop"
        order.pop("software_stop_error", None)
        order["_instance_event"] = {
            "event_type": "software_stop_triggered",
            "message": f"软件止损触发：{symbol} 市价平仓 {close_quantity} 张，报价 {current_price:.2f} <= 止损 {stop_price:.2f}。",
            "lifecycle_state": "exiting",
            "status": "warning",
            "payload": {"symbol": symbol, "quantity": close_quantity, "quote": current_price, "stop_trigger_price": stop_price},
        }
        return {"changed": True, "triggered": 1, "failed": 0}
    except Exception as exc:  # noqa: BLE001 - preserve per-order failure for the dashboard.
        retry = _schedule_retry(order, "software_stop", str(exc))
        order["software_stop_active"] = bool(retry.get("will_retry"))
        order["software_stop_status"] = "market_close_retry_scheduled" if retry.get("will_retry") else "market_close_failed"
        order["software_stop_error"] = str(exc)
        order["status"] = "software_stop_retry_scheduled" if retry.get("will_retry") else "software_stop_failed"
        order["monitor_status"] = order["status"]
        order["_instance_event"] = {
            "event_type": "software_stop_retry_scheduled" if retry.get("will_retry") else "software_stop_failed",
            "message": f"软件止损触发但市价平仓失败：{symbol}。{'已安排重试。' if retry.get('will_retry') else '需要人工介入。'}",
            "lifecycle_state": "exiting" if retry.get("will_retry") else "manual_intervention_required",
            "status": "warning" if retry.get("will_retry") else "error",
            "payload": {"symbol": symbol, "quantity": close_quantity, "error": str(exc), **retry},
        }
        return {"changed": True, "triggered": 0, "failed": 0 if retry.get("will_retry") else 1, "stop_triggered": True}


def _submit_adaptive_exit_close(
    order: dict[str, Any],
    symbol: str,
    close_quantity: int,
    close_side: str,
    account_name: str,
    remark: str,
    cycle: int,
) -> tuple[dict[str, Any], float, bool]:
    """Submit a take-profit / long-leg close using the order's configured mode.

    Returns ``(close_order, limit_price, used_market)``. ``limit`` submits a
    marketable limit at the touch, while ``adaptive`` starts closer to mid and
    walks toward the touch. Both fall back to market when the exit walk is
    exhausted or quote data is unusable; adaptive also obeys its global
    kill-switch. A fresh full option quote is fetched here because the monitor's
    cached quote row does NOT carry bid/ask (only exit_price).
    """
    raw_quote: dict[str, Any] = {}
    exit_mode = adaptive_pricing.normalize_order_type(order.get("exit_order_type"))
    if exit_mode != "market" and (exit_mode != "adaptive" or adaptive_pricing.adaptive_exit_enabled()):
        try:
            raw_quote = longbridge_sdk_option_quote(symbol, account_name) or {}
        except Exception:  # noqa: BLE001 - quote failure just falls to market.
            raw_quote = {}
    limit_price, use_market = adaptive_pricing.adaptive_exit_decision(raw_quote, close_side, cycle, exit_mode)
    if use_market or limit_price <= 0:
        return submit_market_order(symbol, close_quantity, close_side, account_name, remark), 0.0, True
    submit_fn = submit_sell_order if close_side == "sell" else submit_buy_order
    close_order = submit_fn(symbol, close_quantity, limit_price, account_name, f"{remark} adl={limit_price:.2f}", order_type="limit")
    return close_order, limit_price, False


def _try_software_take_profit(order: dict[str, Any], account_name: str) -> dict[str, int | bool]:
    if not order.get("software_take_profit_active"):
        return {"changed": False, "triggered": 0, "failed": 0}
    if _retry_waiting(order, "software_take_profit"):
        return {"changed": False, "triggered": 0, "failed": 0}
    symbol = str(order.get("order_symbol") or "")
    targets = order.get("software_take_profit_targets") or []
    pending_targets = [
        target
        for target in targets
        if isinstance(target, dict) and target.get("status", "pending") == "pending" and float(target.get("price") or 0) > 0
    ]
    if not symbol or not pending_targets:
        order["software_take_profit_active"] = False
        order["software_take_profit_status"] = "completed" if symbol else "not_armed_missing_symbol"
        return {"changed": True, "triggered": 0, "failed": 0}
    quote_row = _software_stop_quote(order, account_name)
    order["software_take_profit_last_check_at"] = utc_now()
    order["software_take_profit_last_quote"] = quote_row
    if not quote_row.get("available"):
        order["software_take_profit_status"] = "quote_unavailable"
        order["software_take_profit_error"] = str(quote_row.get("error") or "software take profit quote unavailable")
        _note_protection_quote_outage(order, "software_take_profit")
        return {"changed": True, "triggered": 0, "failed": 0}
    _clear_protection_quote_outage(order, "software_take_profit")
    current_price = float(quote_row.get("exit_price") or 0)
    target = sorted(pending_targets, key=lambda item: float(item.get("price") or 0))[0]
    target_price = float(target.get("price") or 0)
    if current_price < target_price:
        order["software_take_profit_status"] = "armed"
        order.pop("software_take_profit_error", None)
        return {"changed": True, "triggered": 0, "failed": 0}

    close_quantity = _take_profit_close_quantity(order, target)
    if close_quantity < 1:
        target["status"] = "skipped_no_quantity"
        order["software_take_profit_quantity"] = _pending_take_profit_quantity(targets)
        order["software_take_profit_active"] = order["software_take_profit_quantity"] > 0
        order["software_take_profit_status"] = "armed" if order["software_take_profit_active"] else "completed"
        return {"changed": True, "triggered": 0, "failed": 0}

    cancel_result = _cancel_protective_orders(order, account_name)
    if cancel_result["failed"]:
        retry = _schedule_retry(order, "software_take_profit", "; ".join(item.get("error") or item.get("reason") or "cancel failed" for item in cancel_result["failed"])[:240])
        order["software_take_profit_active"] = bool(retry.get("will_retry"))
        order["software_take_profit_status"] = "cancel_stop_retry_scheduled" if retry.get("will_retry") else "cancel_stop_failed"
        order["software_take_profit_error"] = "; ".join(item.get("error") or item.get("reason") or "cancel failed" for item in cancel_result["failed"])[:240]
        order["status"] = "software_take_profit_retry_scheduled" if retry.get("will_retry") else "software_take_profit_failed"
        _append_order_event(
            order,
            {
                "event_type": "software_take_profit_retry_scheduled" if retry.get("will_retry") else "software_take_profit_failed",
                "message": f"软件止盈触发但撤保护单失败：{symbol}。{'已安排重试。' if retry.get('will_retry') else '需要人工介入。'}",
                "lifecycle_state": "monitoring" if retry.get("will_retry") else "manual_intervention_required",
                "status": "warning" if retry.get("will_retry") else "error",
                "payload": {"symbol": symbol, "quantity": close_quantity, "error": order["software_take_profit_error"], **retry},
            },
        )
        return {"changed": True, "triggered": 0, "failed": 0 if retry.get("will_retry") else 1}

    tp_cycle = int(target.get("adaptive_exit_cycle") or 0)
    try:
        close_order, tp_limit_price, tp_used_market = _submit_adaptive_exit_close(
            order,
            symbol,
            close_quantity,
            "sell",
            account_name,
            f"AI_OPTION_SW_TP {order.get('symbol')} {target.get('name')}={target_price:.2f}",
            tp_cycle,
        )
    except Exception as exc:  # noqa: BLE001
        retry = _schedule_retry(order, "software_take_profit", str(exc))
        order["software_take_profit_active"] = bool(retry.get("will_retry"))
        order["software_take_profit_status"] = "market_close_retry_scheduled" if retry.get("will_retry") else "market_close_failed"
        order["software_take_profit_error"] = str(exc)
        order["status"] = "software_take_profit_retry_scheduled" if retry.get("will_retry") else "software_take_profit_failed"
        order["monitor_status"] = order["status"]
        if cancel_result["canceled"] and _remaining_open_quantity(order) > 0:
            _arm_software_stop(order, _remaining_open_quantity(order), "take_profit_close_failed_after_stop_cancel", True)
        _append_order_event(
            order,
            {
                "event_type": "software_take_profit_retry_scheduled" if retry.get("will_retry") else "software_take_profit_failed",
                "message": f"软件止盈触发但市价平仓失败：{symbol}。{'已安排重试并保留软件止损兜底。' if retry.get('will_retry') else '需要人工介入。'}",
                "lifecycle_state": "monitoring" if retry.get("will_retry") else "manual_intervention_required",
                "status": "warning" if retry.get("will_retry") else "error",
                "payload": {"symbol": symbol, "quantity": close_quantity, "error": str(exc), **retry},
            },
        )
        return {"changed": True, "triggered": 0, "failed": 0 if retry.get("will_retry") else 1}

    target["status"] = "submitted"
    target["triggered_at"] = utc_now()
    target["trigger_quote"] = current_price
    target["order"] = close_order
    # Adaptive-exit bookkeeping: a resting mid-ward LIMIT (tp_used_market=False)
    # is walked toward the touch by the reconciler each cycle and falls to market
    # at the walk's end. A market close (tp_used_market=True) is terminal as before.
    target["adaptive_exit_resting"] = not tp_used_market
    target["adaptive_exit_cycle"] = tp_cycle
    if not tp_used_market:
        target["adaptive_exit_limit_price"] = tp_limit_price
    order["software_take_profit_targets"] = targets
    order["software_take_profit_submitted_quantity"] = int(order.get("software_take_profit_submitted_quantity") or 0) + close_quantity
    order["software_take_profit_quantity"] = _pending_take_profit_quantity(targets)
    order["software_take_profit_active"] = order["software_take_profit_quantity"] > 0
    order["software_take_profit_status"] = f"{target.get('name') or 'target'}_submitted"
    order["software_take_profit_order"] = close_order
    order["status"] = "software_take_profit_partial_submitted" if order["software_take_profit_active"] else "software_take_profit_submitted"
    order["monitor_status"] = "software_take_profit_submitted"
    order.pop("software_take_profit_error", None)
    if order.get("software_stop_active"):
        order["software_stop_quantity"] = max(0, int(order.get("software_stop_quantity") or 0) - close_quantity)
        if int(order.get("software_stop_quantity") or 0) <= 0:
            order["software_stop_active"] = False
            order["software_stop_status"] = "completed_after_take_profit"
    elif _remaining_open_quantity(order) > 0:
        _arm_software_stop(order, _remaining_open_quantity(order), "broker_stop_replaced_after_take_profit", True)
    _mark_residual_exit_submitted(order, "software_take_profit", close_order)
    if _remaining_open_quantity(order) <= 0:
        order["single_leg_smart_exit_active"] = False
        order["single_leg_smart_exit_quantity"] = 0
        order["single_leg_smart_exit_status"] = "completed_after_take_profit"
    _append_order_event(
        order,
        {
            "event_type": "take_profit_hit",
            "message": f"软件止盈触发：{symbol} {target.get('name')} 市价平仓 {close_quantity} 张，报价 {current_price:.2f} >= 目标 {target_price:.2f}。",
            "lifecycle_state": "exiting" if not order.get("software_take_profit_active") else "monitoring",
            "status": "success",
            "payload": {"symbol": symbol, "quantity": close_quantity, "quote": current_price, "target_price": target_price},
        },
    )
    return {"changed": True, "triggered": 1, "failed": 0}


def _try_single_leg_smart_exit(order: dict[str, Any], account_name: str) -> dict[str, int | bool]:
    if _retry_waiting(order, "single_leg_smart_exit"):
        return {"changed": False, "triggered": 0, "failed": 0}
    if not _single_leg_smart_exit_active(order):
        return {"changed": False, "triggered": 0, "failed": 0}
    close_quantity = _remaining_open_quantity(order)
    symbol = str(order.get("order_symbol") or "")
    if close_quantity < 1 or not symbol:
        order["single_leg_smart_exit_active"] = False
        order["single_leg_smart_exit_status"] = "not_armed_no_open_quantity"
        return {"changed": True, "triggered": 0, "failed": 0}

    quote_row = _software_stop_quote(order, account_name)
    order["single_leg_smart_exit_last_check_at"] = utc_now()
    order["single_leg_smart_exit_last_quote"] = quote_row
    current_price = float(quote_row.get("exit_price") or 0) if quote_row.get("available") else 0.0
    entry_price = float(order.get("entry_price") or order.get("original_entry_price") or 0)
    if current_price > 0 and entry_price > 0:
        current_pnl = (current_price - entry_price) * close_quantity * 100
        order["single_leg_last_pnl"] = round(current_pnl, 2)
        order["single_leg_best_pnl"] = round(max(float(order.get("single_leg_best_pnl") or current_pnl), current_pnl), 2)
        order["single_leg_worst_pnl"] = round(min(float(order.get("single_leg_worst_pnl") or current_pnl), current_pnl), 2)

    trigger = _single_leg_smart_exit_trigger(order, account_name, quote_row)
    if not trigger:
        if not quote_row.get("available"):
            order["single_leg_smart_exit_status"] = "quote_unavailable"
            order["single_leg_smart_exit_error"] = str(quote_row.get("error") or "single leg smart exit quote unavailable")
            return {"changed": True, "triggered": 0, "failed": 0}
        order["single_leg_smart_exit_status"] = "armed"
        order["single_leg_smart_exit_quantity"] = close_quantity
        order.pop("pending_smart_exit_trigger", None)
        order.pop("single_leg_smart_exit_last_pending_trigger", None)
        order.pop("single_leg_smart_exit_error", None)
        return {"changed": True, "triggered": 0, "failed": 0}

    if not _smart_exit_confirmed(order, trigger, status_key="single_leg_smart_exit_status"):
        order["single_leg_smart_exit_quantity"] = close_quantity
        order["single_leg_smart_exit_last_pending_trigger"] = trigger
        order.pop("single_leg_smart_exit_error", None)
        return {"changed": True, "triggered": 0, "failed": 0}

    cancel_result = _cancel_protective_orders(order, account_name)
    if cancel_result["failed"]:
        error_text = "; ".join(item.get("error") or item.get("reason") or "cancel failed" for item in cancel_result["failed"])[:240]
        retry = _schedule_retry(order, "single_leg_smart_exit", error_text)
        order["single_leg_smart_exit_active"] = bool(retry.get("will_retry"))
        order["single_leg_smart_exit_status"] = "cancel_stop_retry_scheduled" if retry.get("will_retry") else "cancel_stop_failed"
        order["single_leg_smart_exit_error"] = error_text
        order["status"] = "single_leg_smart_exit_retry_scheduled" if retry.get("will_retry") else "single_leg_smart_exit_failed"
        order["monitor_status"] = order["status"]
        order["single_leg_smart_exit_quantity"] = close_quantity
        order["_instance_event"] = {
            "event_type": "single_leg_smart_exit_retry_scheduled" if retry.get("will_retry") else "single_leg_smart_exit_failed",
            "message": f"单腿智能退出触发但撤保护单失败：{symbol}。{'已安排重试。' if retry.get('will_retry') else '需要人工介入。'}",
            "lifecycle_state": "monitoring" if retry.get("will_retry") else "manual_intervention_required",
            "status": "warning" if retry.get("will_retry") else "error",
            "payload": {"symbol": symbol, "quantity": close_quantity, "error": error_text, **trigger, **retry},
        }
        return {"changed": True, "triggered": 0, "failed": 0 if retry.get("will_retry") else 1}

    try:
        close_order = submit_market_order(
            symbol,
            close_quantity,
            "sell",
            account_name,
            f"AI_OPTION_SMART_EXIT {order.get('symbol')} {trigger['trigger']}",
        )
        order["single_leg_smart_exit_active"] = False
        order["single_leg_smart_exit_status"] = "market_close_submitted"
        order["single_leg_smart_exit_triggered_at"] = utc_now()
        order["single_leg_smart_exit_trigger"] = trigger["trigger"]
        order["single_leg_smart_exit_reason"] = trigger.get("reason") or ""
        order["single_leg_smart_exit_value"] = trigger.get("value")
        order["single_leg_smart_exit_trigger_quote"] = current_price
        order["single_leg_smart_exit_order"] = close_order
        order["single_leg_smart_exit_submitted_quantity"] = close_quantity
        order["single_leg_smart_exit_quantity"] = 0
        order.pop("single_leg_smart_exit_last_pending_trigger", None)
        order["status"] = "single_leg_smart_exit_submitted"
        order["monitor_status"] = "single_leg_smart_exit_submitted"
        _mark_residual_exit_submitted(order, "single_leg_smart_exit", close_order)
        if order.get("software_stop_active"):
            order["software_stop_active"] = False
            order["software_stop_status"] = "completed_after_smart_exit"
            order["software_stop_quantity"] = 0
        if order.get("software_take_profit_active"):
            order["software_take_profit_active"] = False
            order["software_take_profit_status"] = "completed_after_smart_exit"
            order["software_take_profit_quantity"] = 0
        order.pop("single_leg_smart_exit_error", None)
        order["_instance_event"] = {
            "event_type": "single_leg_smart_exit_triggered",
            "message": f"单腿智能退出触发：{symbol} 市价平仓 {close_quantity} 张，原因：{trigger.get('reason') or trigger['trigger']}。",
            "lifecycle_state": "exiting",
            "status": "warning",
            "payload": {"symbol": symbol, "quantity": close_quantity, **trigger},
        }
        return {"changed": True, "triggered": 1, "failed": 0}
    except Exception as exc:  # noqa: BLE001
        retry = _schedule_retry(order, "single_leg_smart_exit", str(exc))
        order["single_leg_smart_exit_active"] = bool(retry.get("will_retry"))
        order["single_leg_smart_exit_status"] = "market_close_retry_scheduled" if retry.get("will_retry") else "market_close_failed"
        order["single_leg_smart_exit_error"] = str(exc)
        order["status"] = "single_leg_smart_exit_retry_scheduled" if retry.get("will_retry") else "single_leg_smart_exit_failed"
        order["monitor_status"] = order["status"]
        order["single_leg_smart_exit_quantity"] = close_quantity
        order["_instance_event"] = {
            "event_type": "single_leg_smart_exit_retry_scheduled" if retry.get("will_retry") else "single_leg_smart_exit_failed",
            "message": f"单腿智能退出触发但市价平仓失败：{symbol}。{'已安排重试。' if retry.get('will_retry') else '需要人工介入。'}",
            "lifecycle_state": "exiting" if retry.get("will_retry") else "manual_intervention_required",
            "status": "warning" if retry.get("will_retry") else "error",
            "payload": {"symbol": symbol, "quantity": close_quantity, "error": str(exc), **trigger, **retry},
        }
        return {"changed": True, "triggered": 0, "failed": 0 if retry.get("will_retry") else 1}


def _software_stop_quote(order: dict[str, Any], account_name: str) -> dict[str, Any]:
    contract_symbol = str(order.get("contract_symbol") or "")
    if not contract_symbol:
        contract_symbol = str(order.get("order_symbol") or "")
    return _monitor_option_quote(contract_symbol, account_name, order.get("market_data_source"))


def _monitor_option_quote(contract_symbol: str, account_name: str, market_data_source: str | None = None) -> dict[str, Any]:
    contract_symbol = str(contract_symbol or "").strip()
    if not contract_symbol:
        return {"available": False, "source": "missing_symbol", "error": "missing option contract symbol"}
    requested = _normalize_monitor_market_data_source(market_data_source)
    cache_key = f"{contract_symbol}|{account_name}|{requested}"
    cached = _cycle_quote_cache.get(cache_key)
    if cached is not None:
        return cached
    errors: dict[str, str] = {}
    for source in _monitor_source_order(requested):
        row = _monitor_option_quote_from_source(contract_symbol, account_name, source)
        if row.get("available") and float(row.get("exit_price") or 0) > 0:
            if source != requested:
                row["fallback_from"] = requested
            if errors:
                row["fallback_errors"] = errors
            _cycle_quote_cache[cache_key] = row  # Cache successful quotes only
            return row
        errors[source] = str(row.get("error") or row.get("quote_warning") or f"{source} did not include a usable option price")
    return {
        "available": False,
        "source": "quote_fallback_failed",
        "requested_source": requested,
        "error": "; ".join(f"{source}: {error}" for source, error in errors.items()),
        "fallback_errors": errors,
    }


def _monitor_option_quote_from_source(contract_symbol: str, account_name: str, source: str) -> dict[str, Any]:
    try:
        if source == "thetadata":
            from .thetadata_option_tool import quote_option_contract as theta_option_quote

            row = theta_option_quote(contract_symbol)
            price = _quote_exit_price(row)
            return {
                "available": bool(row.get("available")) and price > 0,
                "source": "thetadata",
                "provider_source": row.get("pricing_source") or "thetadata_option_quote",
                "exit_price": price,
                **_quote_risk_metrics(row),
                "raw": row,
                "error": row.get("error") or row.get("quote_warning"),
            }
        if source == "longbridge":
            row = longbridge_sdk_option_quote(contract_symbol, account_name)
            price = _quote_exit_price(row)
            return {
                "available": bool(row.get("available")) and price > 0,
                "source": "longbridge_sdk",
                "provider_source": row.get("pricing_source") or "longbridge_option_quote",
                "exit_price": price,
                **_quote_risk_metrics(row),
                "raw": row,
                "error": row.get("error") or row.get("quote_warning"),
            }
        from .yfinance_option_tool import quote_option_contract as yfinance_option_quote

        yf_symbol = _yfinance_contract_symbol(contract_symbol)
        row = yfinance_option_quote(yf_symbol)
        price = _quote_exit_price(row)
        return {
            "available": bool(row.get("available")) and price > 0,
            "source": "yfinance",
            "provider_source": row.get("pricing_source") or "yfinance_option_quote",
            "exit_price": price,
            **_quote_risk_metrics(row),
            "raw": row,
            "error": row.get("error") or row.get("quote_warning"),
        }
    except Exception as exc:  # noqa: BLE001 - protection layer falls through to the next quote source.
        return {"available": False, "source": source, "error": str(exc)}


def _monitor_underlying_quote(symbol: str, account_name: str, market_data_source: str | None = None) -> dict[str, Any]:
    symbol = str(symbol or "").strip().upper()
    if not symbol:
        return {"available": False, "source": "missing_symbol", "error": "missing underlying symbol"}
    order_symbol = symbol if symbol.endswith(".US") else f"{symbol}.US"
    yf_symbol = symbol[:-3] if symbol.endswith(".US") else symbol
    requested = _normalize_monitor_market_data_source(market_data_source)
    errors: dict[str, str] = {}
    for source in _monitor_underlying_source_order(requested):
        try:
            if source == "thetadata":
                from .thetadata_option_tool import market_data as theta_market_data

                row = (theta_market_data(yf_symbol, daily_count=5).get("quote") or {})
                price = _quote_exit_price(row)
                if price > 0:
                    return {"available": True, "source": "thetadata", "provider_source": "thetadata_quote", "symbol": yf_symbol, "price": price, "raw": row, "fallback_from": requested if source != requested else None, "fallback_errors": errors}
                errors[source] = "ThetaData did not include a usable underlying price"
                continue
            if source == "longbridge":
                row = quote(order_symbol, account_name)
                price = _quote_exit_price(row)
                if price > 0:
                    return {"available": True, "source": "longbridge_sdk", "provider_source": "longbridge_quote", "symbol": order_symbol, "price": price, "raw": row, "fallback_from": requested if source != requested else None, "fallback_errors": errors}
                errors[source] = "Longbridge SDK did not include a usable underlying price"
                continue
            from .yfinance_option_tool import market_data as yfinance_market_data

            row = (yfinance_market_data(yf_symbol, daily_count=5).get("quote") or {})
            price = _quote_exit_price(row)
            if price > 0:
                return {"available": True, "source": "yfinance", "provider_source": "yfinance_quote", "symbol": yf_symbol, "price": price, "raw": row, "fallback_from": requested if source != requested else None, "fallback_errors": errors}
            errors[source] = "yfinance did not include a usable underlying price"
        except Exception as exc:  # noqa: BLE001
            errors[source] = str(exc)
    return {
        "available": False,
        "source": "quote_fallback_failed",
        "requested_source": requested,
        "error": "; ".join(f"{source}: {error}" for source, error in errors.items()),
        "fallback_errors": errors,
    }


def _quote_risk_metrics(row: dict[str, Any]) -> dict[str, Any]:
    greeks = row.get("greeks") if isinstance(row.get("greeks"), dict) else {}
    return {
        "delta": _quote_metric(row, greeks, "delta"),
        "gamma": _quote_metric(row, greeks, "gamma"),
        "theta": _quote_metric(row, greeks, "theta", "theta_per_day"),
        "vega": _quote_metric(row, greeks, "vega"),
        "iv": _quote_metric(row, greeks, "iv", "implied_volatility", "implied_vol"),
    }


def _quote_metric(row: dict[str, Any], greeks: dict[str, Any], *keys: str) -> float | None:
    for container in (row, greeks):
        for key in keys:
            try:
                return float(container[key])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _normalize_monitor_market_data_source(value: Any) -> str:
    source = str(value or "thetadata").strip().lower()
    return source if source in {"yfinance", "longbridge", "thetadata", "auto"} else "thetadata"


def _monitor_source_order(source: str) -> list[str]:
    return ["thetadata", "longbridge", "yfinance"]


def _monitor_underlying_source_order(source: str) -> list[str]:
    # Protection always prefers broker-grade sources. The scanner may still use
    # an explicitly selected yfinance source for analysis, but delayed YF quotes
    # are only the final monitor fallback and never drive live entry gates.
    return ["thetadata", "longbridge", "yfinance"]


def _yfinance_contract_symbol(contract_symbol: str) -> str:
    return option_symbol_for_occ(contract_symbol)


def _quote_exit_price(row: dict[str, Any]) -> float:
    """Sell-to-close trigger price for a long option position.

    Closing a long means selling at the BID, so the protective stop/take-profit
    gate must read the bid (or a last/mark print) — NOT the ask. Using the ask
    here overstated the realizable price, so a stop could fail to fire while the
    real bid had already collapsed. We therefore exclude ask from the trigger
    basis; if no bid/last/mark is available we return 0.0 so the caller treats
    the quote as unavailable (and can escalate) rather than acting on a
    misleading ask.
    """
    for key in ("bid", "last_done", "price", "last_price", "last", "mark", "close", "limit_price"):
        value = row.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def _arm_software_stop(order: dict[str, Any], quantity: int, reason: str, enabled: bool = True) -> None:
    if not enabled:
        order["software_stop_active"] = False
        order["software_stop_status"] = "disabled"
        return
    stop_price = float(order.get("stop_trigger_price") or 0)
    if quantity < 1 or stop_price <= 0:
        order["software_stop_active"] = False
        order["software_stop_status"] = "not_armed_invalid_quantity_or_stop"
        return
    order["software_stop_active"] = True
    order["software_stop_status"] = "armed"
    order["software_stop_quantity"] = int(quantity)
    order["software_stop_reason"] = reason
    order["software_stop_armed_at"] = utc_now()


def _arm_software_take_profit(order: dict[str, Any], quantity: int, enabled: bool = True) -> None:
    if not enabled:
        order["software_take_profit_active"] = False
        order["software_take_profit_status"] = "disabled"
        return
    if order.get("software_take_profit_active") and order.get("software_take_profit_targets"):
        return
    if quantity < 1:
        order["software_take_profit_active"] = False
        order["software_take_profit_status"] = "not_armed_no_filled_quantity"
        return
    entry_price = float(order.get("entry_price") or order.get("actual_entry_price") or order.get("original_entry_price") or 0)
    tiered = bool(order.get("tiered_take_profit_enabled"))
    take_profit_pct = max(1.0, min(float(order.get("take_profit_pct") or 30), 500.0))
    tp1_pct = max(1.0, min(float(order.get("take_profit_1_pct") or take_profit_pct or 20), 500.0))
    tp2_pct = max(tp1_pct, min(float(order.get("take_profit_2_pct") or 35), 500.0))
    tp1 = entry_price * (1 + (tp1_pct if tiered else take_profit_pct) / 100) if entry_price > 0 else 0
    tp2 = entry_price * (1 + tp2_pct / 100) if entry_price > 0 and tiered else 0
    targets: list[dict[str, Any]] = []
    if not tiered and tp1 > 0:
        targets.append({"name": "take_profit", "price": round(tp1, 2), "quantity": int(quantity), "status": "pending"})
    elif quantity == 1 and tp1 > 0:
        targets.append({"name": "tp1", "price": round(tp1, 2), "quantity": 1, "status": "pending"})
    elif quantity >= 2 and tp1 > 0:
        targets.append({"name": "tp1", "price": round(tp1, 2), "quantity": max(1, quantity // 2), "status": "pending"})
    remaining_quantity = int(quantity) - sum(int(item["quantity"]) for item in targets)
    if remaining_quantity > 0 and tp2 > 0:
        targets.append({"name": "tp2", "price": round(tp2, 2), "quantity": remaining_quantity, "status": "pending"})
    if not targets:
        order["software_take_profit_active"] = False
        order["software_take_profit_status"] = "not_armed_invalid_targets"
        return
    order["software_take_profit_active"] = True
    order["software_take_profit_status"] = "armed"
    order["software_take_profit_quantity"] = sum(int(item["quantity"]) for item in targets)
    order["software_take_profit_targets"] = targets
    order["software_take_profit_pct"] = take_profit_pct
    order["take_profit_1_pct"] = tp1_pct
    order["take_profit_2_pct"] = tp2_pct
    order["tiered_take_profit_enabled"] = tiered
    order["software_take_profit_source"] = "monitor_fill_recovery"
    order["software_take_profit_armed_at"] = utc_now()


def _cancel_protective_orders(order: dict[str, Any], account_name: str) -> dict[str, list[dict[str, Any]]]:
    canceled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    stop_orders = list(order.get("stop_orders") or ([] if not order.get("stop_order") else [order.get("stop_order")]))
    for stop_order in stop_orders:
        order_id = _order_id(stop_order or {})
        if not order_id:
            failed.append({"order": stop_order, "reason": "stop order id missing"})
            continue
        try:
            canceled.append({"order_id": order_id, "result": cancel_order(order_id, account_name)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"order_id": order_id, "error": str(exc)})
    if canceled and not failed:
        order["stop_orders_canceled_for_take_profit"] = canceled
        order["stop_orders"] = []
        order.pop("stop_order", None)
        order["covered_quantity"] = 0
    return {"canceled": canceled, "failed": failed}


def _take_profit_close_quantity(order: dict[str, Any], target: dict[str, Any]) -> int:
    target_quantity = int(float(target.get("quantity") or 0))
    return max(0, min(target_quantity, _remaining_open_quantity(order)))


def _remaining_open_quantity(order: dict[str, Any]) -> int:
    filled = int(order.get("entry_filled_quantity") or order.get("quantity") or 0)
    tp_closed = int(order.get("software_take_profit_closed_quantity") or 0)
    tp_submitted = int(order.get("software_take_profit_submitted_quantity") or 0)
    stop_closed = int(order.get("software_stop_closed_quantity") or 0)
    stop_submitted = int(order.get("software_stop_submitted_quantity") or 0)
    smart_exit_closed = int(order.get("single_leg_smart_exit_closed_quantity") or 0)
    smart_exit_submitted = int(order.get("single_leg_smart_exit_submitted_quantity") or 0)
    flattened = int(order.get("instance_flatten_closed_quantity") or 0)
    flatten_submitted = int(order.get("instance_flatten_submitted_quantity") or 0)
    pending_or_closed = max(tp_closed, tp_submitted) + max(stop_closed, stop_submitted) + max(smart_exit_closed, smart_exit_submitted) + max(flattened, flatten_submitted)
    return max(0, filled - pending_or_closed)


def _retry_waiting(order: dict[str, Any], prefix: str) -> bool:
    next_retry = str(order.get(f"{prefix}_next_retry_at") or "")
    if not next_retry:
        return False
    try:
        retry_at = datetime.fromisoformat(next_retry.replace("Z", "+00:00"))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return retry_at > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _schedule_retry(order: dict[str, Any], prefix: str, error: str, *, max_attempts: int = 3, delay_seconds: int = 45) -> dict[str, Any]:
    attempts = int(order.get(f"{prefix}_retry_count") or 0) + 1
    order[f"{prefix}_retry_count"] = attempts
    order[f"{prefix}_last_retry_error"] = error
    if attempts >= max_attempts:
        order.pop(f"{prefix}_next_retry_at", None)
        return {"retry_count": attempts, "max_retries": max_attempts, "will_retry": False}
    next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    order[f"{prefix}_next_retry_at"] = next_retry.isoformat()
    return {
        "retry_count": attempts,
        "max_retries": max_attempts,
        "will_retry": True,
        "next_retry_at": order[f"{prefix}_next_retry_at"],
    }


def _pending_take_profit_quantity(targets: list[dict[str, Any]]) -> int:
    return sum(int(float(target.get("quantity") or 0)) for target in targets if target.get("status", "pending") == "pending")


def _single_leg_smart_exit_active(order: dict[str, Any]) -> bool:
    if not order.get("residual_leg_tracking_active") and (str(order.get("strategy_order") or "").lower() == "true" or order.get("strategy_type")):
        return False
    if order.get("status") in {
        "single_leg_smart_exit_submitted",
        "software_stop_submitted",
        "instance_flatten_submitted",
        "entry_terminal_no_stop",
    }:
        return False
    if int(order.get("entry_filled_quantity") or order.get("covered_quantity") or 0) < 1:
        return False
    if order.get("single_leg_smart_exit_active") is False:
        return False
    return bool(_single_leg_exit_conditions(order))


def _single_leg_exit_conditions(order: dict[str, Any]) -> list[dict[str, Any]]:
    raw = order.get("single_leg_exit_conditions")
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)]
    risk_plan = ((order.get("candidate") or {}).get("risk_plan") or {}) if isinstance(order.get("candidate"), dict) else {}
    conditions = normalize_exit_rules(
        raw_conditions=risk_plan.get("exit_conditions"),
        latest_exit=str(order.get("latest_exit") or risk_plan.get("latest_exit") or ""),
        invalidation=str(order.get("underlying_invalidation") or order.get("invalidation") or risk_plan.get("invalidation") or ""),
        allow_overnight=order.get("allow_overnight", risk_plan.get("allow_overnight")),
        position=order,
    )
    if conditions:
        order["single_leg_smart_exit_active"] = True
        order["single_leg_exit_conditions"] = conditions
    return conditions


def _single_leg_smart_exit_trigger(order: dict[str, Any], account_name: str, quote_row: dict[str, Any]) -> dict[str, Any] | None:
    current_price = float(quote_row.get("exit_price") or 0)
    entry_price = float(order.get("entry_price") or order.get("original_entry_price") or 0)
    quantity = max(1, _remaining_open_quantity(order))
    current_pnl = (current_price - entry_price) * quantity * 100 if current_price > 0 and entry_price > 0 else float(order.get("single_leg_last_pnl") or 0)
    best_pnl = float(order.get("single_leg_best_pnl") or current_pnl)
    rules = list(_single_leg_exit_conditions(order))
    if order.get("software_stop_active") and float(order.get("stop_trigger_price") or 0) > 0:
        rules.append({"type": "option_price_stop", "price": float(order.get("stop_trigger_price") or 0), "reason": "软件止损"})
    if order.get("software_take_profit_active"):
        targets = [item for item in (order.get("software_take_profit_targets") or []) if isinstance(item, dict) and float(item.get("price") or 0) > 0]
        if targets:
            target = sorted(targets, key=lambda item: float(item.get("price") or 0))[0]
            rules.append({"type": "option_price_take_profit", "price": float(target.get("price") or 0), "reason": str(target.get("name") or "软件止盈")})
    if order.get("stop_loss_pct") is not None and entry_price > 0:
        rules.append({"type": "option_price_stop", "price": round(entry_price * (1 - float(order.get("stop_loss_pct") or 0) / 100), 2), "reason": "默认止损"})
    if order.get("take_profit_pct") is not None and entry_price > 0:
        rules.append({"type": "option_price_take_profit_pct", "pct": float(order.get("take_profit_pct") or 0), "reason": "默认止盈"})
    if not rules:
        return None
    opened_at = parse_datetime(order.get("entry_filled_at") or order.get("created_at") or order.get("submitted_at"))
    return evaluate_exit_rules(
        rules=rules,
        position=order,
        account_name=account_name,
        current_price=current_price,
        entry_price=entry_price,
        current_pnl=current_pnl,
        best_pnl=best_pnl,
        underlying_quote=lambda symbol: _monitor_underlying_quote(symbol, account_name, order.get("market_data_source")),
        option_quote=quote_row,
        now=now_et(),
        position_opened_at=opened_at,
    )


def _append_order_event(order: dict[str, Any], event: dict[str, Any]) -> None:
    events = order.setdefault("_instance_events", [])
    if isinstance(events, list):
        events.append(event)
    else:
        order["_instance_events"] = [event]


def _mark_residual_exit_submitted(order: dict[str, Any], source: str, close_order: dict[str, Any]) -> None:
    if not order.get("residual_leg_tracking_active"):
        return
    if _remaining_open_quantity(order) > 0:
        return
    order["residual_leg_tracking_active"] = False
    order["residual_leg_exit_source"] = source
    order["residual_leg_exit_submitted_at"] = utc_now()
    order["strategy_exit_status"] = "submitted"
    order["strategy_exit_order"] = close_order
    order["risk_tracking_active"] = False
    order["software_stop_active"] = False
    order["software_take_profit_active"] = False
    order["single_leg_smart_exit_active"] = False
    order.pop("strategy_exit_error", None)
    _append_order_event(
        order,
        {
            "event_type": "strategy_residual_exit_submitted",
            "message": f"残腿单腿退出已提交：{order.get('order_symbol') or order.get('contract_symbol')} {order.get('residual_leg_quantity') or order.get('quantity') or 0} 张。",
            "lifecycle_state": "exiting",
            "status": "warning",
            "payload": {
                "tracking_id": order.get("tracking_id"),
                "source": source,
                "order": close_order,
                "contract_symbol": order.get("contract_symbol"),
                "quantity": order.get("residual_leg_quantity") or order.get("quantity"),
            },
        },
    )


def _residual_exit_reconcile_needed(order: dict[str, Any]) -> bool:
    if not isinstance(order, dict):
        return False
    if not order.get("residual_strategy_tracking_id") and not order.get("residual_leg_contract_symbol"):
        return False
    if str(order.get("strategy_exit_status") or "") != "submitted":
        return False
    return bool(_residual_exit_order_id(order))


def _residual_exit_order_id(order: dict[str, Any]) -> str | None:
    source = str(order.get("residual_leg_exit_source") or "")
    keys = {
        "software_stop": "software_stop_order",
        "software_take_profit": "software_take_profit_order",
        "single_leg_smart_exit": "single_leg_smart_exit_order",
    }
    preferred = keys.get(source)
    if preferred:
        order_id = _order_id(order.get(preferred) or {})
        if order_id:
            return order_id
    for key in ("software_stop_order", "software_take_profit_order", "single_leg_smart_exit_order", "strategy_exit_order"):
        order_id = _order_id(order.get(key) or {})
        if order_id:
            return order_id
    return None


def _try_residual_exit_reconcile(order: dict[str, Any], account_name: str) -> dict[str, int | bool]:
    if not _residual_exit_reconcile_needed(order):
        return {"changed": False, "failed": 0}
    order_id = _residual_exit_order_id(order)
    if not order_id:
        return {"changed": False, "failed": 0}
    try:
        detail = order_detail(order_id, account_name)
    except Exception as exc:  # noqa: BLE001
        order["residual_leg_exit_detail_error"] = str(exc)
        return {"changed": True, "failed": 0}
    order["residual_leg_exit_detail"] = detail
    status = _order_status(detail)
    filled_quantity = _filled_quantity(detail)
    target_quantity = int(order.get("residual_leg_quantity") or order.get("quantity") or 0)
    if _is_filled_status_text(status) or (target_quantity > 0 and filled_quantity >= target_quantity):
        executed_price = _executed_price(detail)
        order["strategy_exit_status"] = "filled"
        order["strategy_exit_detail"] = detail
        order["strategy_exit_filled_quantity"] = filled_quantity or target_quantity
        if executed_price > 0:
            order["strategy_exit_executed_price"] = round(executed_price, 4)
        order["status"] = "strategy_auto_exit_filled"
        order["monitor_status"] = "strategy_auto_exit_filled"
        order["residual_leg_tracking_active"] = False
        _sync_residual_exit_to_strategy_leg(order, detail)
        annotate_strategy_order_fill_ledger(order)
        _append_order_event(
            order,
            {
                "event_type": "strategy_residual_exit_filled",
                "message": f"残腿单腿退出已成交：{order.get('order_symbol') or order.get('contract_symbol')} {filled_quantity or target_quantity} 张。",
                "lifecycle_state": "closed",
                "status": "success",
                "payload": {
                    "tracking_id": order.get("tracking_id"),
                    "order_id": order_id,
                    "executed_price": executed_price,
                    "quantity": filled_quantity or target_quantity,
                },
            },
        )
        return {"changed": True, "failed": 0}
    if _is_terminal_unfilled_status(status):
        error = str(detail.get("msg") or _history_message(detail) or f"residual exit order {status}").strip()
        order["status"] = "residual_exit_failed"
        order["monitor_status"] = "residual_exit_failed"
        order["strategy_exit_status"] = "failed"
        order["strategy_exit_error"] = error
        order["residual_leg_tracking_active"] = False
        order["software_stop_active"] = False
        order["software_take_profit_active"] = False
        order["single_leg_smart_exit_active"] = False
        _append_order_event(
            order,
            {
                "event_type": "strategy_residual_exit_failed",
                "message": f"残腿单腿退出被券商拒绝：{order.get('order_symbol') or order.get('contract_symbol')}。需要人工处理。",
                "lifecycle_state": "manual_intervention_required",
                "status": "error",
                "payload": {"tracking_id": order.get("tracking_id"), "order_id": order_id, "error": error},
            },
        )
        return {"changed": True, "failed": 1}
    order["residual_leg_exit_broker_status"] = status
    return {"changed": True, "failed": 0}


def _try_residual_position_reconcile(order: dict[str, Any], account_name: str) -> dict[str, int | bool]:
    if not order.get("residual_leg_tracking_active"):
        return {"changed": False, "closed": 0}
    contract_symbol = str(order.get("residual_leg_contract_symbol") or order.get("contract_symbol") or order.get("order_symbol") or "").strip()
    if not contract_symbol:
        return {"changed": False, "closed": 0}
    try:
        rows = positions(account_name)
    except Exception as exc:  # noqa: BLE001
        order["residual_position_check_error"] = str(exc)
        return {"changed": True, "closed": 0}
    match = _find_position_row(rows, contract_symbol)
    if match and _position_quantity(match) > 0:
        order["residual_position_last_check_at"] = utc_now()
        order["residual_position_quantity"] = _position_quantity(match)
        order.pop("residual_position_check_error", None)
        order.pop("residual_position_flat_cycles", None)
        return {"changed": True, "closed": 0}
    # Require N consecutive "not found / qty 0" cycles before declaring the leg
    # flat — a single incomplete positions() response (pagination, eventual
    # consistency right after a fill, an option-symbol alias miss in
    # _find_position_row) would otherwise permanently disarm the software
    # stop/take-profit on a STILL-OPEN residual leg. Mirror the quote-outage
    # confirmation pattern.
    cycles = int(order.get("residual_position_flat_cycles") or 0) + 1
    order["residual_position_flat_cycles"] = cycles
    order["residual_position_last_check_at"] = utc_now()
    if cycles < _residual_flat_confirm_cycles():
        return {"changed": True, "closed": 0}
    order.pop("residual_position_flat_cycles", None)
    _mark_residual_position_flat(order, match)
    return {"changed": True, "closed": 1}


def _single_leg_exit_reconcile_needed(order: dict[str, Any]) -> bool:
    if not isinstance(order, dict):
        return False
    if str(order.get("status") or "") not in {
        "software_stop_submitted",
        "software_stop_partial_filled",
        "software_take_profit_submitted",
        "software_take_profit_partial_submitted",
        "software_take_profit_partial_filled",
        "single_leg_smart_exit_submitted",
        "single_leg_smart_exit_partial_filled",
        "instance_flatten_submitted",
        "instance_flatten_partial_filled",
    }:
        return False
    return bool(_single_leg_exit_reconcile_orders(order))


def _single_leg_exit_reconcile_orders(order: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stop_id = _order_id(order.get("software_stop_order") or {})
    if stop_id and order.get("software_stop_exit_status") != "filled":
        rows.append({"source": "software_stop", "order_id": stop_id, "quantity": int(order.get("software_stop_submitted_quantity") or order.get("software_stop_closed_quantity") or 0)})
    smart_id = _order_id(order.get("single_leg_smart_exit_order") or {})
    if smart_id and order.get("single_leg_smart_exit_exit_status") != "filled":
        rows.append({"source": "single_leg_smart_exit", "order_id": smart_id, "quantity": int(order.get("single_leg_smart_exit_submitted_quantity") or order.get("single_leg_smart_exit_closed_quantity") or 0)})
    flatten_id = _order_id(order.get("instance_flatten_order") or {})
    if flatten_id and order.get("instance_flatten_exit_status") != "filled":
        rows.append({"source": "instance_flatten", "order_id": flatten_id, "quantity": int(order.get("instance_flatten_submitted_quantity") or order.get("instance_flatten_closed_quantity") or 0)})
    for target in order.get("software_take_profit_targets") or []:
        if not isinstance(target, dict) or target.get("status") != "submitted":
            continue
        order_id = _order_id(target.get("order") or {})
        if order_id:
            rows.append({"source": "software_take_profit", "order_id": order_id, "quantity": int(float(target.get("quantity") or 0)), "target": target})
    return [row for row in rows if row.get("order_id")]


def _try_single_leg_exit_reconcile(order: dict[str, Any], account_name: str) -> dict[str, int | bool]:
    rows = _single_leg_exit_reconcile_orders(order)
    if not rows:
        return {"changed": False, "software_stop_failed": 0, "software_take_profit_failed": 0, "single_leg_smart_exit_failed": 0}
    changed = False
    failed = {"software_stop_failed": 0, "software_take_profit_failed": 0, "single_leg_smart_exit_failed": 0}
    for row in rows:
        source = str(row.get("source") or "")
        order_id = str(row.get("order_id") or "")
        try:
            detail = order_detail(order_id, account_name)
        except Exception as exc:  # noqa: BLE001
            order[f"{source}_exit_detail_error"] = str(exc)
            changed = True
            continue
        order[f"{source}_exit_detail"] = detail
        status = _order_status(detail)
        filled_quantity = _filled_quantity(detail)
        target_quantity = int(row.get("quantity") or 0)
        if _is_filled_status_text(status) or (target_quantity > 0 and filled_quantity >= target_quantity):
            _mark_single_leg_exit_filled(order, row, detail)
            changed = True
            continue
        if _is_terminal_unfilled_status(status):
            _mark_single_leg_exit_failed(order, row, detail)
            failed[f"{source}_failed"] = int(failed.get(f"{source}_failed") or 0) + 1
            changed = True
            continue
        if filled_quantity > 0:
            _mark_single_leg_exit_partial(order, row, detail)
            changed = True
        else:
            # Still resting, unfilled. For an adaptive take-profit limit, walk it
            # one step more aggressive (or to market at the walk's end) so a
            # resting limit never strands a position that already hit its target.
            if source == "software_take_profit" and _escalate_adaptive_take_profit(order, row, account_name):
                changed = True
                continue
            order[f"{source}_exit_broker_status"] = status
            changed = True
    return {"changed": changed, **failed}


def _escalate_adaptive_take_profit(order: dict[str, Any], row: dict[str, Any], account_name: str) -> bool:
    """Walk a resting adaptive take-profit LIMIT one cycle more aggressive.

    Called from the reconciler when an adaptive TP limit is still unfilled. Cancels
    the resting order, re-checks it did not fill in the meantime (never over-close),
    bumps the cycle, and resubmits via ``_submit_adaptive_exit_close`` — a more
    aggressive mid-ward limit, or a guaranteed MARKET order once the walk is
    exhausted. Returns True if it acted (order was replaced). No-op (False) when
    the target isn't an adaptive resting limit, so plain-market TP is untouched.
    """
    target = row.get("target") if isinstance(row.get("target"), dict) else None
    if not target or not target.get("adaptive_exit_resting"):
        return False
    order_id = str(row.get("order_id") or "")
    symbol = str(order.get("order_symbol") or order.get("contract_symbol") or "")
    close_quantity = int(row.get("quantity") or 0)
    if not order_id or not symbol or close_quantity < 1:
        return False
    # Cancel the resting limit, then confirm via order_detail it did not fill in
    # the race window before we resubmit — otherwise we would double-close.
    try:
        cancel_order(order_id, account_name)
    except Exception as exc:  # noqa: BLE001 - a failed cancel means leave it resting this cycle.
        order["software_take_profit_adaptive_cancel_error"] = str(exc)
        return False
    try:
        post_cancel = order_detail(order_id, account_name)
        if _is_filled_status_text(_order_status(post_cancel)) or _filled_quantity(post_cancel) >= close_quantity:
            _mark_single_leg_exit_filled(order, row, post_cancel)
            return True
    except Exception:  # noqa: BLE001 - detail unavailable; proceed to resubmit conservatively.
        pass
    next_cycle = int(target.get("adaptive_exit_cycle") or 0) + 1
    try:
        close_order, limit_price, used_market = _submit_adaptive_exit_close(
            order, symbol, close_quantity, "sell",
            account_name, f"AI_OPTION_SW_TP {order.get('symbol')} {target.get('name')} walk{next_cycle}", next_cycle,
        )
    except Exception as exc:  # noqa: BLE001
        order["software_take_profit_adaptive_resubmit_error"] = str(exc)
        return False
    target["order"] = close_order
    target["adaptive_exit_cycle"] = next_cycle
    target["adaptive_exit_resting"] = not used_market
    if not used_market:
        target["adaptive_exit_limit_price"] = limit_price
    order["software_take_profit_order"] = close_order
    _append_order_event(
        order,
        {
            "event_type": "software_take_profit_adaptive_walk",
            "message": (
                f"止盈自适应挂单第{next_cycle}轮："
                + (f"市价兜底平仓 {close_quantity} 张。" if used_market else f"限价 {limit_price:.2f} 更靠近对手价重挂 {close_quantity} 张。")
            ),
            "lifecycle_state": "exiting",
            "status": "info",
            "payload": {"symbol": symbol, "quantity": close_quantity, "cycle": next_cycle, "limit_price": limit_price, "used_market": used_market},
        },
    )
    return True


def _escalate_adaptive_strategy_exit(order: dict[str, Any], entry: dict[str, Any], account_name: str) -> bool:
    """Walk a resting adaptive strategy LONG-leg exit one cycle more aggressive.

    Mirror of ``_escalate_adaptive_take_profit`` for the strategy-combo exit path
    (entry-dict shape, not the single-leg row shape). Only fires for a leg flagged
    ``adaptive_exit_resting`` — i.e. a long sell-to-close submitted as an adaptive
    limit (short covers stay market and are never flagged, so they're untouched).
    Cancels the resting limit, re-checks the fill post-cancel so it never
    double-closes, bumps the cycle, and resubmits more aggressive → guaranteed
    market at the walk's end. Returns True if it acted."""
    if not entry.get("adaptive_exit_resting"):
        return False
    order_id = _order_id(entry.get("strategy_exit_order") or {})
    contract_symbol = _entry_contract_symbol(entry)
    close_quantity = int(float(entry.get("strategy_exit_quantity") or 0))
    if not order_id or not contract_symbol or close_quantity < 1:
        return False
    close_side = str(entry.get("strategy_exit_side") or "sell").lower()
    order_symbol = option_order_symbol(contract_symbol)
    try:
        cancel_order(order_id, account_name)
    except Exception as exc:  # noqa: BLE001 - failed cancel: leave resting this cycle.
        entry["adaptive_exit_cancel_error"] = str(exc)
        return False
    try:
        post_cancel = order_detail(order_id, account_name)
        if _is_filled_status_text(_order_status(post_cancel)) or _filled_quantity(post_cancel) >= close_quantity:
            entry["strategy_exit_status"] = "filled"
            entry["strategy_exit_detail"] = post_cancel
            entry["strategy_exit_filled_quantity"] = _filled_quantity(post_cancel) or close_quantity
            executed_price = _executed_price(post_cancel)
            if executed_price > 0:
                entry["strategy_exit_executed_price"] = round(executed_price, 4)
            entry["adaptive_exit_resting"] = False
            return True
    except Exception:  # noqa: BLE001 - detail unavailable; resubmit conservatively.
        pass
    next_cycle = int(entry.get("adaptive_exit_cycle") or 0) + 1
    try:
        close_order, limit_price, used_market = _submit_adaptive_exit_close(
            order, order_symbol, close_quantity, close_side,
            account_name, f"AI_STRATEGY_EXIT {order.get('tracking_id') or ''} walk{next_cycle} {close_side.upper()}", next_cycle,
        )
    except Exception as exc:  # noqa: BLE001
        entry["adaptive_exit_resubmit_error"] = str(exc)
        return False
    entry["strategy_exit_order"] = close_order
    entry["adaptive_exit_cycle"] = next_cycle
    entry["adaptive_exit_resting"] = not used_market
    entry["strategy_exit_status"] = "submitted"
    if not used_market:
        entry["adaptive_exit_limit_price"] = limit_price
    _append_order_event(
        order,
        {
            "event_type": "strategy_exit_adaptive_walk",
            "message": (
                f"组合腿止盈/退出自适应第{next_cycle}轮："
                + (f"市价兜底平仓 {close_quantity} 张。" if used_market else f"限价 {limit_price:.2f} 更靠近对手价重挂 {close_quantity} 张。")
            ),
            "lifecycle_state": "exiting",
            "status": "info",
            "payload": {"contract_symbol": contract_symbol, "quantity": close_quantity, "cycle": next_cycle, "limit_price": limit_price, "used_market": used_market},
        },
    )
    return True


def _mark_single_leg_exit_filled(order: dict[str, Any], row: dict[str, Any], detail: dict[str, Any]) -> None:
    source = str(row.get("source") or "")
    filled_quantity = _filled_quantity(detail) or int(row.get("quantity") or 0)
    if source == "software_stop":
        order["software_stop_closed_quantity"] = max(int(order.get("software_stop_closed_quantity") or 0), filled_quantity)
    elif source == "single_leg_smart_exit":
        order["single_leg_smart_exit_closed_quantity"] = max(int(order.get("single_leg_smart_exit_closed_quantity") or 0), filled_quantity)
    elif source == "instance_flatten":
        order["instance_flatten_closed_quantity"] = max(int(order.get("instance_flatten_closed_quantity") or 0), filled_quantity)
        order["instance_flattened_at"] = order.get("instance_flattened_at") or utc_now()
    elif source == "software_take_profit":
        target_quantity = int(row.get("quantity") or 0)
        prior_submitted_total = int(order.get("software_take_profit_submitted_quantity") or 0)
        prior_closed_total = int(order.get("software_take_profit_closed_quantity") or 0)
        order["software_take_profit_closed_quantity"] = max(prior_closed_total, prior_submitted_total - target_quantity + filled_quantity)
    order[f"{source}_exit_status"] = "filled"
    order[f"{source}_exit_filled_quantity"] = filled_quantity
    order[f"{source}_exit_broker_status"] = _order_status(detail)
    executed_price = _executed_price(detail)
    if executed_price > 0:
        order[f"{source}_exit_executed_price"] = round(executed_price, 4)
    target = row.get("target")
    if isinstance(target, dict):
        target["status"] = "filled"
        target["filled_quantity"] = filled_quantity
        target["detail"] = detail
        if executed_price > 0:
            target["executed_price"] = round(executed_price, 4)
    if _remaining_open_quantity(order) <= 0:
        order["monitor_status"] = f"{source}_filled"
        order["status"] = f"{source}_filled"
        order["software_stop_active"] = False
        order["software_take_profit_active"] = False
        order["single_leg_smart_exit_active"] = False
        _append_order_event(
            order,
            {
                "event_type": f"{source}_filled",
                "message": f"单腿退出订单已成交：{order.get('order_symbol') or order.get('contract_symbol')} {filled_quantity} 张。",
                "lifecycle_state": "closed",
                "status": "success",
                "payload": {"order_id": row.get("order_id"), "source": source, "quantity": filled_quantity, "detail": detail},
            },
        )


def _mark_single_leg_exit_partial(order: dict[str, Any], row: dict[str, Any], detail: dict[str, Any]) -> None:
    source = str(row.get("source") or "")
    filled_quantity = _filled_quantity(detail)
    order[f"{source}_exit_status"] = "partial_filled"
    order[f"{source}_exit_filled_quantity"] = filled_quantity
    order[f"{source}_exit_broker_status"] = _order_status(detail)
    if source == "software_stop":
        order["software_stop_closed_quantity"] = filled_quantity
    elif source == "single_leg_smart_exit":
        order["single_leg_smart_exit_closed_quantity"] = filled_quantity
    elif source == "instance_flatten":
        order["instance_flatten_closed_quantity"] = filled_quantity
        order["instance_flattened_at"] = order.get("instance_flattened_at") or utc_now()
    elif source == "software_take_profit":
        target = row.get("target")
        if isinstance(target, dict):
            target["filled_quantity"] = filled_quantity
            target["status"] = "partial_filled"
            target["detail"] = detail
            executed_price = _executed_price(detail)
            if executed_price > 0:
                target["executed_price"] = round(executed_price, 4)
        order["software_take_profit_closed_quantity"] = max(0, int(order.get("software_take_profit_closed_quantity") or 0) - int(row.get("quantity") or 0) + filled_quantity)
    order["monitor_status"] = f"{source}_partial_filled"
    order["status"] = f"{source}_partial_filled"


def _mark_single_leg_exit_failed(order: dict[str, Any], row: dict[str, Any], detail: dict[str, Any]) -> None:
    source = str(row.get("source") or "")
    quantity = int(row.get("quantity") or 0)
    error = str(detail.get("msg") or _history_message(detail) or f"{source} exit order {_order_status(detail)}").strip()
    order[f"{source}_exit_status"] = "failed"
    order[f"{source}_exit_error"] = error
    order[f"{source}_exit_broker_status"] = _order_status(detail)
    if source == "software_stop":
        order["software_stop_closed_quantity"] = 0
        order["software_stop_status"] = "exit_rejected"
        order["software_stop_error"] = error
        order["status"] = "software_stop_failed"
    elif source == "single_leg_smart_exit":
        order["single_leg_smart_exit_closed_quantity"] = 0
        order["single_leg_smart_exit_status"] = "exit_rejected"
        order["single_leg_smart_exit_error"] = error
        order["status"] = "single_leg_smart_exit_failed"
    elif source == "instance_flatten":
        order["instance_flatten_closed_quantity"] = 0
        order["instance_flatten_error"] = error
        order["status"] = "instance_flatten_failed"
    elif source == "software_take_profit":
        target = row.get("target")
        if isinstance(target, dict):
            target["status"] = "failed"
            target["error"] = error
        order["software_take_profit_closed_quantity"] = max(0, int(order.get("software_take_profit_closed_quantity") or 0) - quantity)
        order["software_take_profit_status"] = "exit_rejected"
        order["software_take_profit_error"] = error
        order["status"] = "software_take_profit_failed"
    order["monitor_status"] = order["status"]
    order["software_stop_active"] = False
    order["software_take_profit_active"] = False
    order["single_leg_smart_exit_active"] = False
    _append_order_event(
        order,
        {
            "event_type": f"{source}_failed",
            "message": f"单腿退出订单被券商拒绝：{order.get('order_symbol') or order.get('contract_symbol')}。需要人工处理。",
            "lifecycle_state": "manual_intervention_required",
            "status": "error",
            "payload": {"order_id": row.get("order_id"), "source": source, "quantity": quantity, "error": error, "detail": detail},
        },
    )


def _mark_residual_position_flat(order: dict[str, Any], position_row: dict[str, Any] | None = None) -> None:
    quantity = int(order.get("residual_leg_quantity") or order.get("quantity") or order.get("entry_filled_quantity") or 0)
    now = utc_now()
    order["status"] = "strategy_manual_exit_detected"
    order["monitor_status"] = "strategy_manual_exit_detected"
    order["strategy_exit_status"] = "filled"
    order["strategy_exit_filled_quantity"] = quantity
    order["residual_leg_tracking_active"] = False
    order["residual_position_quantity"] = 0
    order["residual_position_flat_detected_at"] = now
    order["residual_position_last_check_at"] = now
    order["residual_leg_exit_source"] = "broker_position_reconcile"
    order["software_stop_active"] = False
    order["software_stop_quantity"] = 0
    order["software_stop_status"] = "completed_after_manual_flat"
    order["software_take_profit_active"] = False
    order["software_take_profit_quantity"] = 0
    order["software_take_profit_status"] = "completed_after_manual_flat"
    order["single_leg_smart_exit_active"] = False
    order["single_leg_smart_exit_quantity"] = 0
    order["single_leg_smart_exit_status"] = "completed_after_manual_flat"
    order["risk_tracking_active"] = False
    order.pop("strategy_exit_error", None)
    order.pop("residual_position_check_error", None)
    _sync_residual_manual_flat_to_strategy_leg(order, quantity, position_row)
    annotate_strategy_order_fill_ledger(order)
    _append_order_event(
        order,
        {
            "event_type": "strategy_residual_manual_flat_detected",
            "message": f"检测到券商侧残腿仓位已归零：{order.get('order_symbol') or order.get('contract_symbol')}，已停止软件止损/止盈追踪。",
            "lifecycle_state": "closed",
            "status": "success",
            "payload": {
                "tracking_id": order.get("tracking_id"),
                "contract_symbol": order.get("contract_symbol"),
                "order_symbol": order.get("order_symbol"),
                "quantity": quantity,
                "position_row": position_row or {},
            },
        },
    )


def _sync_residual_manual_flat_to_strategy_leg(order: dict[str, Any], quantity: int, position_row: dict[str, Any] | None = None) -> None:
    contract_symbol = str(order.get("residual_leg_contract_symbol") or order.get("contract_symbol") or "")
    if not contract_symbol:
        return
    for entry in order.get("legs") or []:
        if not isinstance(entry, dict) or _entry_contract_symbol(entry) != contract_symbol:
            continue
        entry["strategy_exit_status"] = "filled"
        entry["strategy_exit_filled_quantity"] = quantity
        entry["strategy_exit_quantity"] = max(int(float(entry.get("strategy_exit_quantity") or 0)), quantity)
        entry["strategy_exit_detail"] = {
            "status": "manual_flat_detected",
            "executed_quantity": quantity,
            "position_row": position_row or {},
        }
        entry["residual_leg_tracking_active"] = False
        entry.pop("strategy_exit_error", None)
        break


def _find_position_row(rows: list[dict[str, Any]], contract_symbol: str) -> dict[str, Any] | None:
    targets = _position_symbol_aliases(contract_symbol)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        aliases: set[str] = set()
        for key in ("symbol", "stock_symbol", "security_symbol", "instrument_symbol", "code", "ticker"):
            value = row.get(key)
            if value:
                aliases.update(_position_symbol_aliases(str(value)))
        if targets & aliases:
            return row
    return None


def _position_symbol_aliases(symbol: str) -> set[str]:
    return option_symbol_aliases(symbol)


def _position_quantity(row: dict[str, Any]) -> int:
    for key in ("quantity", "qty", "current_quantity", "holding_quantity", "available_quantity"):
        try:
            quantity = abs(int(float(row.get(key) or 0)))
        except (TypeError, ValueError):
            quantity = 0
        if quantity > 0:
            return quantity
    return 0


def _sync_residual_exit_to_strategy_leg(order: dict[str, Any], detail: dict[str, Any]) -> None:
    contract_symbol = str(order.get("residual_leg_contract_symbol") or order.get("contract_symbol") or "")
    if not contract_symbol:
        return
    filled_quantity = _filled_quantity(detail) or int(order.get("residual_leg_quantity") or order.get("quantity") or 0)
    executed_price = _executed_price(detail)
    for entry in order.get("legs") or []:
        if not isinstance(entry, dict) or _entry_contract_symbol(entry) != contract_symbol:
            continue
        entry["strategy_exit_status"] = "filled"
        entry["strategy_exit_detail"] = detail
        entry["strategy_exit_filled_quantity"] = filled_quantity
        entry["strategy_exit_quantity"] = max(int(float(entry.get("strategy_exit_quantity") or 0)), filled_quantity)
        source = str(order.get("residual_leg_exit_source") or "")
        source_order = order.get(f"{source}_order") if source else None
        if isinstance(source_order, dict):
            entry["strategy_exit_order"] = source_order
        if executed_price > 0:
            entry["strategy_exit_executed_price"] = round(executed_price, 4)
        entry["residual_leg_tracking_active"] = False
        entry.pop("strategy_exit_error", None)
        break


def _has_monitorable_order(orders: list[dict[str, Any]]) -> bool:
    return any(
        order.get("status") in {"entry_submitted_stop_pending_unfilled", "entry_partially_filled_stop_partial"}
        or bool(order.get("software_stop_active"))
        or bool(order.get("software_take_profit_active"))
        or _single_leg_smart_exit_active(order)
        or _single_leg_exit_reconcile_needed(order)
        or _strategy_exit_reconcile_needed(order)
        or _residual_exit_reconcile_needed(order)
        for order in orders
    )


def _strategy_exit_reconcile_needed(order: dict[str, Any]) -> bool:
    if not isinstance(order, dict):
        return False
    if str(order.get("strategy_exit_status") or "") not in {"submitted", "partial_submitted"}:
        return False
    has_exit_order = any(isinstance(entry, dict) and entry.get("strategy_exit_order") for entry in order.get("legs") or [])
    if not (order.get("strategy_auto_execute") or has_exit_order):
        return False
    for entry in order.get("legs") or []:
        if not isinstance(entry, dict) or not entry.get("strategy_exit_order"):
            continue
        if str(entry.get("strategy_exit_status") or "") in {"filled", "failed"}:
            continue
        return True
    return False


def _strategy_leg_entry_price(entry: dict[str, Any]) -> float:
    leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
    for value in (
        entry.get("actual_entry_price"),
        entry.get("entry_price"),
        entry.get("executed_price"),
        _executed_price(entry.get("entry_detail") or {}),
        leg.get("actual_entry_price"),
        leg.get("entry_price"),
        leg.get("price"),
    ):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0.0


def _strategy_leg_filled_quantity(entry: dict[str, Any], order: dict[str, Any] | None = None) -> int:
    # Prefer explicit per-leg / per-entry fill counts.
    for value in (entry.get("filled_quantity"), entry.get("entry_filled_quantity")):
        try:
            quantity = int(float(value or 0))
        except (TypeError, ValueError):
            quantity = 0
        if quantity > 0:
            return quantity
    # Only fall back to the *planned* leg quantity when the parent combo order
    # confirms it actually entered. Without this guard a leg that never filled
    # would be treated as an open position and "closed" into a new naked leg.
    if isinstance(order, dict) and int(float(order.get("entry_filled_quantity") or 0)) > 0:
        try:
            return max(0, int(float(entry.get("quantity") or 0)))
        except (TypeError, ValueError):
            return 0
    return 0


def _strategy_leg_remaining_quantity(entry: dict[str, Any], order: dict[str, Any] | None = None) -> int:
    filled = _strategy_leg_filled_quantity(entry, order)
    exited = int(float(entry.get("strategy_exit_filled_quantity") or 0))
    if str(entry.get("strategy_exit_status") or "") == "filled":
        exited = max(exited, int(float(entry.get("strategy_exit_quantity") or 0)))
    return max(0, filled - exited)


def _strategy_unclosed_exit_legs(order: dict[str, Any]) -> list[dict[str, Any]]:
    residual: list[dict[str, Any]] = []
    for entry in order.get("legs") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("strategy_exit_status") or "") == "filled":
            continue
        remaining = _strategy_leg_remaining_quantity(entry, order)
        if remaining < 1:
            continue
        leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
        residual.append(
            {
                "entry": entry,
                "leg": leg,
                "contract_symbol": _entry_contract_symbol(entry),
                "action": str(leg.get("action") or "").lower(),
                "side": str(leg.get("side") or "").lower(),
                "remaining_quantity": remaining,
                "entry_price": _strategy_leg_entry_price(entry),
            }
        )
    return residual


def _strategy_position_for_tracking(instance: dict[str, Any] | None, tracking_id: str) -> dict[str, Any]:
    if not isinstance(instance, dict):
        return {}
    risk_plan = instance.get("risk_plan") if isinstance(instance.get("risk_plan"), dict) else {}
    positions = risk_plan.get("strategy_positions") if isinstance(risk_plan.get("strategy_positions"), list) else []
    for position in positions:
        if isinstance(position, dict) and str(position.get("tracking_id") or "") == tracking_id:
            return position
    return {}


def _apply_residual_long_leg_tracking(
    order: dict[str, Any],
    instance: dict[str, Any] | None,
    residual_leg: dict[str, Any],
    failed_legs: list[dict[str, Any]],
) -> None:
    entry = residual_leg["entry"]
    leg = residual_leg["leg"]
    tracking_id = str(order.get("tracking_id") or "")
    position = _strategy_position_for_tracking(instance, tracking_id)
    contract_symbol = str(residual_leg.get("contract_symbol") or "").strip()
    order_symbol = option_order_symbol(contract_symbol) if contract_symbol else ""
    quantity = int(residual_leg.get("remaining_quantity") or 0)
    entry_price = float(residual_leg.get("entry_price") or 0)
    stop_pct = max(1.0, min(float(position.get("stop_loss_pct") or order.get("stop_loss_pct") or 25), 95.0))

    order["status"] = "strategy_residual_tracking"
    order["monitor_status"] = "strategy_residual_tracking"
    order["strategy_exit_status"] = "residual_tracking"
    order["residual_leg_tracking_active"] = True
    order["residual_leg_reason"] = "; ".join(item.get("error") or "" for item in failed_legs)[:240]
    order["residual_leg_contract_symbol"] = contract_symbol
    order["residual_leg_order_symbol"] = order_symbol
    order["residual_leg_quantity"] = quantity
    order["residual_leg_entry_price"] = round(entry_price, 4) if entry_price > 0 else None
    order["residual_strategy_tracking_id"] = tracking_id
    order["contract_symbol"] = contract_symbol
    order["order_symbol"] = order_symbol
    order["quantity"] = quantity
    order["entry_filled_quantity"] = quantity
    order["entry_price"] = round(entry_price, 4) if entry_price > 0 else order.get("entry_price")
    order["actual_entry_price"] = round(entry_price, 4) if entry_price > 0 else order.get("actual_entry_price")
    order["stop_loss_pct"] = stop_pct
    order["stop_trigger_price"] = round(entry_price * (1 - stop_pct / 100), 2) if entry_price > 0 else order.get("stop_trigger_price")
    order["take_profit_pct"] = position.get("take_profit_pct", order.get("take_profit_pct", 30))
    order["take_profit_1_pct"] = position.get("take_profit_1_pct", order.get("take_profit_1_pct"))
    order["take_profit_2_pct"] = position.get("take_profit_2_pct", order.get("take_profit_2_pct"))
    order["tiered_take_profit_enabled"] = bool(position.get("tiered_take_profit_enabled", order.get("tiered_take_profit_enabled")))
    order["software_stop_closed_quantity"] = 0
    order["software_take_profit_closed_quantity"] = 0
    order["single_leg_smart_exit_closed_quantity"] = 0
    order["risk_tracking_active"] = False
    order.pop("broker_combo_close_required", None)
    order.pop("broker_combo_close_reason", None)
    order["strategy_exit_error"] = order.get("residual_leg_reason")
    entry["residual_leg_tracking_active"] = True

    for key in ("latest_exit", "invalidation", "underlying_invalidation", "allow_overnight", "exit_conditions"):
        if position.get(key) is not None:
            order[key] = position.get(key)
    _single_leg_exit_conditions(order)
    _arm_software_stop(order, quantity, "strategy_residual_long_leg_after_combo_exit_reject", True)
    _arm_software_take_profit(order, quantity, True)
    if order.get("single_leg_exit_conditions"):
        order["single_leg_smart_exit_active"] = True
        order["single_leg_smart_exit_quantity"] = quantity
        order["single_leg_smart_exit_status"] = "armed"

    annotate_strategy_order_fill_ledger(order)
    if isinstance(instance, dict):
        _sync_strategy_position_exit_reconcile(instance, order, "residual_tracking", failed_legs)
    order["_instance_event"] = {
        "event_type": "strategy_residual_tracking_started",
        "message": f"策略组合退出部分成交后剩余 long leg 已切换为残腿单腿追踪：{order_symbol or contract_symbol} {quantity} 张。",
        "lifecycle_state": "monitoring",
        "status": "warning",
        "payload": {
            "tracking_id": tracking_id,
            "contract_symbol": contract_symbol,
            "quantity": quantity,
            "entry_price": entry_price,
            "source_strategy_type": order.get("strategy_type") or leg.get("strategy_type"),
            "failed_legs": failed_legs,
        },
    }


def _apply_broker_combo_close_required(
    order: dict[str, Any],
    instance: dict[str, Any] | None,
    failed_legs: list[dict[str, Any]],
    reason: str,
) -> None:
    order["status"] = "broker_combo_close_required"
    order["monitor_status"] = "broker_combo_close_required"
    order["strategy_exit_status"] = "broker_combo_required"
    order["broker_combo_close_required"] = True
    order["broker_combo_close_reason"] = reason[:240]
    order["risk_tracking_active"] = False
    order["strategy_exit_error"] = reason[:240]
    annotate_strategy_order_fill_ledger(order)
    if isinstance(instance, dict):
        _sync_strategy_position_exit_reconcile(instance, order, "broker_combo_required", failed_legs)
    order["_instance_event"] = {
        "event_type": "broker_combo_close_required",
        "message": "券商拒绝拆腿平仓，剩余腿包含 short leg 或多条残腿；需要在券商侧按组合平仓处理。",
        "lifecycle_state": "manual_intervention_required",
        "status": "error",
        "payload": {"tracking_id": order.get("tracking_id"), "failed_legs": failed_legs},
    }


def _handle_strategy_exit_failure(
    order: dict[str, Any],
    instance: dict[str, Any] | None,
    failed_legs: list[dict[str, Any]],
) -> str:
    residual = _strategy_unclosed_exit_legs(order)
    reason = "; ".join(item.get("error") or "" for item in failed_legs if item.get("error")) or "strategy exit failed"
    if len(residual) == 1 and residual[0].get("action") == "buy" and str(residual[0].get("side") or "") != "stock":
        _apply_residual_long_leg_tracking(order, instance, residual[0], failed_legs)
        return "residual_tracking"
    if residual:
        _apply_broker_combo_close_required(order, instance, failed_legs, reason)
        return "broker_combo_required"

    order["strategy_exit_status"] = "failed"
    order["status"] = "strategy_auto_exit_failed"
    order["risk_tracking_active"] = False
    order["strategy_exit_error"] = reason[:240]
    annotate_strategy_order_fill_ledger(order)
    if isinstance(instance, dict):
        _sync_strategy_position_exit_reconcile(instance, order, "failed", failed_legs)
    order["_instance_event"] = {
        "event_type": "strategy_auto_exit_failed",
        "message": f"策略退出订单被券商拒绝：{order.get('symbol')} {order.get('label') or order.get('strategy_type')}。需要人工检查剩余腿。",
        "lifecycle_state": "manual_intervention_required",
        "status": "error",
        "payload": {"tracking_id": order.get("tracking_id"), "failed_legs": failed_legs},
    }
    return "failed"


def _has_monitorable_strategy(instance: dict[str, Any]) -> bool:
    positions = ((instance.get("risk_plan") or {}).get("strategy_positions") or [])
    return any(isinstance(item, dict) and item.get("risk_tracking_active") for item in positions)


def _try_strategy_risk_tracking(instance: dict[str, Any], account_name: str, orders: list[dict[str, Any]] | None = None) -> dict[str, int | bool]:
    risk_plan = instance.get("risk_plan") if isinstance(instance.get("risk_plan"), dict) else {}
    strategy_positions = risk_plan.get("strategy_positions") if isinstance(risk_plan.get("strategy_positions"), list) else []
    strategy_orders = orders if isinstance(orders, list) else []
    changed = False
    stop_alerted = 0
    take_profit_alerted = 0
    smart_exit_alerted = 0
    auto_exit_submitted = 0
    auto_exit_failed = 0
    broker_positions: list[dict[str, Any]] | None = None
    broker_position_error = ""
    try:
        broker_positions = positions(account_name)
    except Exception as exc:  # noqa: BLE001 - quote protection continues when position reconciliation is unavailable.
        broker_position_error = str(exc)
    for position in strategy_positions:
        if not isinstance(position, dict) or not position.get("risk_tracking_active"):
            continue
        # Phantom-position guard: skip threshold checks when no units are known.
        # Without units, stop/take-profit PnL thresholds are meaningless and can
        # trigger false exits on broker-flattened or never-filled positions.
        if _strategy_position_units(position) <= 0:
            position["risk_tracking_active"] = False
            position["tracking_status"] = "zero_units_skip"
            changed = True
            continue
        if broker_positions is not None:
            reconcile = _reconcile_complete_strategy_position(
                instance,
                position,
                strategy_orders,
                broker_positions,
                account_name,
            )
            changed = bool(reconcile.get("changed")) or changed
            if reconcile.get("closed"):
                continue
        elif broker_position_error:
            position["broker_position_check_error"] = broker_position_error
            changed = True
        result = _strategy_position_mark(position, account_name)
        position["last_check_at"] = utc_now()
        position["last_quote_status"] = result.get("status")
        if not result.get("available"):
            position["tracking_status"] = "quote_unavailable"
            position["last_error"] = result.get("error")
            changed = True
            continue
        _ensure_strategy_total_threshold_basis(position)
        _refresh_strategy_thresholds_for_open_units(position)
        pnl = float(result.get("pnl") or 0)
        position["last_mark"] = round(float(result.get("mark") or 0), 2)
        position["last_pnl"] = round(pnl, 2)
        position["last_leg_quotes"] = result.get("leg_quotes") or []
        position["best_pnl"] = max(float(position.get("best_pnl") or pnl), pnl)
        position["worst_pnl"] = min(float(position.get("worst_pnl") or pnl), pnl)
        position.pop("last_error", None)
        stop_loss_pnl = float(position.get("stop_loss_pnl") or 0)
        tp1 = float(position.get("take_profit_1_pnl") or 0)
        tp2 = float(position.get("take_profit_2_pnl") or 0)
        if stop_loss_pnl < 0 and pnl <= stop_loss_pnl:
            if _strategy_stop_grace_active(position, instance):
                position["tracking_status"] = "stop_entry_grace"
                position["stop_grace_last_pnl"] = round(pnl, 2)
                position["stop_grace_threshold"] = round(stop_loss_pnl, 2)
                changed = True
                continue
            if not _strategy_pnl_trigger_confirmed(position, "stop", pnl, stop_loss_pnl):
                changed = True
                continue
            exit_result = _handle_strategy_exit_trigger(instance, position, strategy_orders, account_name, "stop", pnl, stop_loss_pnl)
            position["stop_triggered_at"] = utc_now()
            position["stop_trigger_pnl"] = round(pnl, 2)
            changed = True
            stop_alerted += 1
            auto_exit_submitted += int(exit_result.get("submitted") or 0)
            auto_exit_failed += int(exit_result.get("failed") or 0)
            continue
        smart_exit = _strategy_smart_exit_trigger(position, account_name, result, pnl, position_opened_at=parse_datetime(instance.get("started_at")))
        if smart_exit:
            if not _smart_exit_confirmed(position, smart_exit, status_key="tracking_status"):
                position["last_pending_smart_exit_trigger"] = smart_exit
                changed = True
                continue
            exit_result = _handle_strategy_exit_trigger(
                instance,
                position,
                strategy_orders,
                account_name,
                smart_exit["trigger"],
                pnl,
                smart_exit.get("threshold", pnl),
            )
            position["smart_exit_triggered_at"] = utc_now()
            position["smart_exit_trigger"] = smart_exit["trigger"]
            position["smart_exit_reason"] = smart_exit.get("reason") or ""
            position["smart_exit_value"] = smart_exit.get("value")
            position.pop("last_pending_smart_exit_trigger", None)
            changed = True
            smart_exit_alerted += 1
            auto_exit_submitted += int(exit_result.get("submitted") or 0)
            auto_exit_failed += int(exit_result.get("failed") or 0)
            continue
        if tp2 > 0 and pnl >= tp2:
            if not _strategy_pnl_trigger_confirmed(position, "tp2", pnl, tp2):
                changed = True
                continue
            exit_result = _handle_strategy_exit_trigger(instance, position, strategy_orders, account_name, "tp2", pnl, tp2)
            position["take_profit_2_status"] = "alerted"
            position["take_profit_2_triggered_at"] = utc_now()
            position["take_profit_2_trigger_pnl"] = round(pnl, 2)
            changed = True
            take_profit_alerted += 1
            auto_exit_submitted += int(exit_result.get("submitted") or 0)
            auto_exit_failed += int(exit_result.get("failed") or 0)
            continue
        if tp1 > 0 and pnl >= tp1 and position.get("take_profit_1_status", "pending") == "pending":
            if not _strategy_pnl_trigger_confirmed(position, "tp1", pnl, tp1):
                changed = True
                continue
            exit_result = _handle_strategy_exit_trigger(instance, position, strategy_orders, account_name, "tp1", pnl, tp1)
            position["take_profit_1_status"] = "alerted"
            position["take_profit_1_triggered_at"] = utc_now()
            position["take_profit_1_trigger_pnl"] = round(pnl, 2)
            changed = True
            take_profit_alerted += 1
            auto_exit_submitted += int(exit_result.get("submitted") or 0)
            auto_exit_failed += int(exit_result.get("failed") or 0)
            continue
        position["tracking_status"] = "armed"
        position.pop("pending_pnl_trigger", None)
        position.pop("pending_smart_exit_trigger", None)
        position.pop("last_pending_smart_exit_trigger", None)
        changed = True
    risk_plan["strategy_positions"] = strategy_positions
    instance["risk_plan"] = risk_plan
    return {
        "changed": changed,
        "stop_alerted": stop_alerted,
        "take_profit_alerted": take_profit_alerted,
        "smart_exit_alerted": smart_exit_alerted,
        "auto_exit_submitted": auto_exit_submitted,
        "auto_exit_failed": auto_exit_failed,
    }


def _strategy_stop_grace_active(position: dict[str, Any], instance: dict[str, Any]) -> bool:
    seconds = max(0, int(float(os.getenv("AI_OPTION_STRATEGY_STOP_GRACE_SECONDS") or 30)))
    if seconds <= 0:
        return False
    opened = parse_datetime(position.get("actual_entry_at") or position.get("opened_at"))
    if opened is None:
        return False
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - opened.astimezone(timezone.utc)).total_seconds() < seconds


def _strategy_stop_confirm_cycles() -> int:
    try:
        return max(1, min(int(os.getenv("AI_OPTION_STRATEGY_STOP_CONFIRM_CYCLES") or 2), 5))
    except ValueError:
        return 2


def _strategy_position_reconcile_grace_elapsed(position: dict[str, Any]) -> bool:
    try:
        grace = max(0, int(os.getenv("AI_OPTION_STRATEGY_POSITION_RECONCILE_GRACE_SECONDS") or 20))
    except ValueError:
        grace = 20
    opened = parse_datetime(position.get("actual_entry_at") or position.get("opened_at"))
    if opened is None:
        return True
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - opened.astimezone(timezone.utc)).total_seconds() >= grace


def _strategy_flat_confirm_cycles() -> int:
    try:
        return max(2, min(int(os.getenv("AI_OPTION_STRATEGY_FLAT_CONFIRM_CYCLES") or 2), 5))
    except ValueError:
        return 2


def _reconcile_complete_strategy_position(
    instance: dict[str, Any],
    position: dict[str, Any],
    strategy_orders: list[dict[str, Any]],
    broker_rows: list[dict[str, Any]],
    account_name: str,
) -> dict[str, int | bool]:
    if not _strategy_position_reconcile_grace_elapsed(position):
        return {"changed": False, "closed": 0}
    tracking_id = str(position.get("tracking_id") or "")
    order = next(
        (row for row in strategy_orders if isinstance(row, dict) and str(row.get("tracking_id") or "") == tracking_id),
        None,
    )
    if not isinstance(order, dict) or order.get("residual_leg_tracking_active"):
        return {"changed": False, "closed": 0}
    entries = [entry for entry in (order.get("legs") or []) if isinstance(entry, dict)]
    expected = [(entry, _strategy_leg_open_quantity(entry)) for entry in entries]
    expected = [(entry, quantity) for entry, quantity in expected if quantity > 0]
    if not expected:
        return {"changed": False, "closed": 0}

    present = []
    for entry, expected_quantity in expected:
        row = _find_position_row(broker_rows, _entry_contract_symbol(entry))
        present.append((entry, expected_quantity, row, _position_quantity(row or {})))
    position["broker_position_check_at"] = utc_now()
    if all(quantity > 0 for _, _, _, quantity in present):
        position.pop("broker_flat_confirm_cycles", None)
        position.pop("broker_position_check_error", None)
        return {"changed": True, "closed": 0}
    if any(quantity > 0 for _, _, _, quantity in present):
        cycles = int(position.get("broker_position_mismatch_cycles") or 0) + 1
        position["broker_position_mismatch_cycles"] = cycles
        if cycles >= _strategy_flat_confirm_cycles():
            position["tracking_status"] = "broker_position_mismatch"
            position["risk_tracking_active"] = False
            if order.get("monitor_status") != "broker_position_mismatch":
                order["monitor_status"] = "broker_position_mismatch"
                order["status"] = "broker_combo_close_required"
                order["broker_combo_close_required"] = True
                order["broker_combo_close_reason"] = "broker positions contain only part of the expected strategy legs"
                append_instance_event(
                    instance,
                    "strategy_broker_position_mismatch",
                    f"券商持仓与策略腿不一致：{position.get('symbol')} {position.get('strategy_type')}，已停止自动阈值退出并要求人工核对。",
                    lifecycle_state="manual_intervention_required",
                    status="error",
                    payload={"tracking_id": tracking_id},
                )
        return {"changed": True, "closed": 0}

    position.pop("broker_position_mismatch_cycles", None)
    cycles = int(position.get("broker_flat_confirm_cycles") or 0) + 1
    position["broker_flat_confirm_cycles"] = cycles
    if cycles < _strategy_flat_confirm_cycles():
        position["tracking_status"] = "broker_flat_pending_confirmation"
        return {"changed": True, "closed": 0}
    position.pop("broker_flat_confirm_cycles", None)
    fills = _manual_strategy_exit_fills(instance, order, expected, account_name)
    _mark_complete_strategy_manual_flat(instance, position, order, expected, fills)
    return {"changed": True, "closed": 1}


def _manual_strategy_exit_fills(
    instance: dict[str, Any],
    order: dict[str, Any],
    expected: list[tuple[dict[str, Any], int]],
    account_name: str,
) -> dict[str, dict[str, Any]]:
    opened = parse_datetime(order.get("actual_entry_at") or instance.get("started_at") or instance.get("created_at"))
    start_date = (opened or datetime.now(timezone.utc)).date().isoformat()
    end_date = datetime.now(timezone.utc).date().isoformat()
    rows: list[dict[str, Any]] = []
    try:
        rows.extend(broker_executions(account_name, start_date, end_date, history=True))
    except Exception:
        pass
    try:
        rows.extend(broker_executions(account_name, history=False))
    except Exception:
        pass
    known_ids = set()
    for entry in order.get("legs") or []:
        if not isinstance(entry, dict):
            continue
        for value in (entry.get("order_id"), _order_id(entry.get("entry_order") or {}), _order_id(entry.get("strategy_exit_order") or {})):
            if value:
                known_ids.add(str(value))
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("trade_id") or f"{row.get('order_id')}:{row.get('symbol')}:{row.get('quantity')}:{row.get('price')}:{row.get('trade_done_at')}")
        deduped[key] = row

    details: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    for row in deduped.values():
        order_id = str(row.get("order_id") or "")
        if not order_id or order_id in known_ids:
            continue
        trade_time = parse_datetime(row.get("trade_done_at"))
        if opened and trade_time:
            opened_cmp = opened if opened.tzinfo else opened.replace(tzinfo=timezone.utc)
            trade_cmp = trade_time if trade_time.tzinfo else trade_time.replace(tzinfo=opened_cmp.tzinfo)
            if trade_cmp < opened_cmp:
                continue
        if order_id not in details:
            try:
                details[order_id] = order_detail(order_id, account_name)
            except Exception:
                details[order_id] = {}
        detail = details[order_id]
        candidates.append({**row, "side": str(detail.get("side") or "").lower(), "detail": detail})

    matched: dict[str, dict[str, Any]] = {}
    for entry, expected_quantity in expected:
        contract_symbol = _entry_contract_symbol(entry)
        aliases = _position_symbol_aliases(contract_symbol)
        leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
        close_side = "sell" if str(leg.get("action") or "").lower() == "buy" else "buy"
        leg_rows = [
            row for row in candidates
            if close_side in str(row.get("side") or "")
            and aliases & _position_symbol_aliases(str(row.get("symbol") or ""))
        ]
        total_quantity = sum(int(float(row.get("quantity") or 0)) for row in leg_rows)
        if total_quantity < expected_quantity:
            continue
        total_value = sum(float(row.get("price") or 0) * int(float(row.get("quantity") or 0)) for row in leg_rows)
        matched[contract_symbol] = {
            "quantity": expected_quantity,
            "executed_price": round(total_value / total_quantity, 4) if total_quantity > 0 else 0,
            "order_ids": sorted({str(row.get("order_id") or "") for row in leg_rows if row.get("order_id")}),
            "trade_ids": sorted({str(row.get("trade_id") or "") for row in leg_rows if row.get("trade_id")}),
        }
    return matched


def _mark_complete_strategy_manual_flat(
    instance: dict[str, Any],
    position: dict[str, Any],
    order: dict[str, Any],
    expected: list[tuple[dict[str, Any], int]],
    fills: dict[str, dict[str, Any]],
) -> None:
    missing_prices = False
    for entry, quantity in expected:
        contract_symbol = _entry_contract_symbol(entry)
        fill = fills.get(contract_symbol) or {}
        entry["strategy_exit_status"] = "filled"
        entry["strategy_exit_quantity"] = quantity
        entry["strategy_exit_filled_quantity"] = quantity
        entry["strategy_exit_detail"] = {
            "status": "manual_flat_detected",
            "executed_quantity": quantity,
            "order_ids": fill.get("order_ids") or [],
            "trade_ids": fill.get("trade_ids") or [],
            "executed_price": fill.get("executed_price") or None,
        }
        if fill.get("executed_price"):
            entry["strategy_exit_executed_price"] = fill["executed_price"]
            if fill.get("order_ids"):
                entry["strategy_exit_order"] = {"order_id": fill["order_ids"][0], "manual": True}
        else:
            missing_prices = True
    order["status"] = "strategy_manual_exit_detected"
    order["monitor_status"] = "strategy_manual_exit_detected"
    order["strategy_exit_status"] = "filled"
    order["strategy_exit_filled_quantity"] = _strategy_position_units(position)
    order["risk_tracking_active"] = False
    position["risk_tracking_active"] = False
    position["tracking_status"] = "manual_exit_detected"
    position["strategy_exit_closed_units"] = _strategy_position_units(position)
    position["strategy_open_units"] = 0
    if missing_prices:
        warnings = set(order.get("pnl_warnings") or [])
        warnings.add("manual_exit_price_unavailable")
        order["pnl_warnings"] = sorted(warnings)
    ledger = annotate_strategy_order_fill_ledger(order)
    position["realized_pnl"] = ledger.get("realized_pnl")
    other_active = any(
        isinstance(row, dict)
        and str(row.get("tracking_id") or "") != str(position.get("tracking_id") or "")
        and bool(row.get("risk_tracking_active"))
        for row in ((instance.get("risk_plan") or {}).get("strategy_positions") or [])
    )
    append_instance_event(
        instance,
        "strategy_manual_flat_detected",
        f"检测到券商侧完整策略仓位已归零：{position.get('symbol')} {position.get('strategy_type')}，已停止自动监控并按成交流水回写。",
        lifecycle_state="monitoring" if other_active else "closed",
        status="success" if not missing_prices else "warning",
        payload={"tracking_id": position.get("tracking_id"), "fills_reconciled": len(fills), "expected_legs": len(expected)},
    )


def _strategy_pnl_trigger_confirmed(position: dict[str, Any], trigger: str, pnl: float, threshold: float) -> bool:
    required = _strategy_stop_confirm_cycles() if trigger == "stop" else 2
    pending = position.get("pending_pnl_trigger") if isinstance(position.get("pending_pnl_trigger"), dict) else {}
    if pending.get("trigger") == trigger:
        count = int(pending.get("count") or 0) + 1
    else:
        count = 1
    position["pending_pnl_trigger"] = {
        "trigger": trigger,
        "count": count,
        "first_seen_at": pending.get("first_seen_at") if pending.get("trigger") == trigger else utc_now(),
        "last_seen_at": utc_now(),
        "last_pnl": round(pnl, 2),
        "threshold": round(threshold, 2),
    }
    if count >= required:
        position.pop("pending_pnl_trigger", None)
        return True
    position["tracking_status"] = f"{trigger}_pending_confirmation"
    return False


def _smart_exit_requires_confirmation(trigger: str) -> bool:
    return str(trigger or "") not in {"smart_time_exit", "smart_no_overnight_exit"}


def _smart_exit_confirmed(container: dict[str, Any], trigger: dict[str, Any], *, status_key: str) -> bool:
    trigger_name = str(trigger.get("trigger") or "")
    if not _smart_exit_requires_confirmation(trigger_name):
        container.pop("pending_smart_exit_trigger", None)
        return True
    required = 2
    pending = container.get("pending_smart_exit_trigger") if isinstance(container.get("pending_smart_exit_trigger"), dict) else {}
    value = trigger.get("value")
    threshold = trigger.get("threshold")
    same_trigger = pending.get("trigger") == trigger_name
    count = int(pending.get("count") or 0) + 1 if same_trigger else 1
    container["pending_smart_exit_trigger"] = {
        "trigger": trigger_name,
        "count": count,
        "first_seen_at": pending.get("first_seen_at") if same_trigger else utc_now(),
        "last_seen_at": utc_now(),
        "value": value,
        "threshold": threshold,
        "reason": str(trigger.get("reason") or ""),
    }
    if count >= required:
        container.pop("pending_smart_exit_trigger", None)
        return True
    container[status_key] = f"{trigger_name}_pending_confirmation"
    return False


def _strategy_smart_exit_trigger(
    position: dict[str, Any],
    account_name: str,
    mark_result: dict[str, Any],
    pnl: float,
    position_opened_at: datetime | None = None,
) -> dict[str, Any] | None:
    rules = normalize_exit_rules(
        raw_conditions=position.get("exit_conditions"),
        latest_exit=str(position.get("latest_exit") or ""),
        invalidation=str(position.get("invalidation") or ""),
        allow_overnight=position.get("allow_overnight"),
        position=position,
    )
    best_pnl = float(position.get("best_pnl") or pnl)
    trigger = evaluate_exit_rules(
        rules=rules,
        position=position,
        account_name=account_name,
        current_price=float(mark_result.get("mark") or 0),
        entry_price=float(position.get("entry_price") or 0),
        current_pnl=pnl,
        best_pnl=best_pnl,
        underlying_quote=lambda symbol: _monitor_underlying_quote(symbol, account_name, position.get("market_data_source")),
        option_quote=mark_result,
        position_opened_at=position_opened_at,
    )
    return trigger


def _try_strategy_exit_reconcile(order: dict[str, Any], instance: dict[str, Any], account_name: str) -> dict[str, int | bool]:
    if not _strategy_exit_reconcile_needed(order):
        return {"changed": False, "failed": 0}
    changed = False
    failed_legs: list[dict[str, Any]] = []
    filled_legs = 0
    pending_legs = 0
    for entry in order.get("legs") or []:
        if not isinstance(entry, dict):
            continue
        exit_order_id = _order_id(entry.get("strategy_exit_order") or {})
        if not exit_order_id:
            continue
        try:
            detail = order_detail(exit_order_id, account_name)
        except Exception as exc:  # noqa: BLE001
            entry["strategy_exit_detail_error"] = str(exc)
            changed = True
            pending_legs += 1
            continue
        entry["strategy_exit_detail"] = detail
        status = _order_status(detail)
        filled_quantity = _filled_quantity(detail)
        entry["strategy_exit_broker_status"] = status
        entry["strategy_exit_filled_quantity"] = filled_quantity
        if _is_terminal_unfilled_status(status) and filled_quantity < 1:
            error = str(detail.get("msg") or _history_message(detail) or f"exit order {status}").strip()
            entry["strategy_exit_status"] = "failed"
            entry["strategy_exit_error"] = error
            failed_legs.append(
                {
                    "contract_symbol": _entry_contract_symbol(entry),
                    "order_id": exit_order_id,
                    "status": status,
                    "error": error,
                }
            )
            changed = True
        elif _is_filled_status_text(status) or filled_quantity >= int(float(entry.get("strategy_exit_quantity") or 0)):
            if entry.get("strategy_exit_status") != "filled":
                entry["strategy_exit_status"] = "filled"
                changed = True
            executed_price = _executed_price(detail)
            if executed_price > 0:
                entry["strategy_exit_executed_price"] = round(executed_price, 4)
            filled_legs += 1
        else:
            # Adaptive resting long-leg exit still unfilled → walk one cycle more
            # aggressive (guaranteed MARKET once the walk is exhausted). No-op for
            # market short covers (never flagged adaptive_exit_resting).
            if entry.get("adaptive_exit_resting") and _escalate_adaptive_strategy_exit(order, entry, account_name):
                changed = True
                if entry.get("strategy_exit_status") == "filled":
                    filled_legs += 1
                    continue
            pending_legs += 1
    if failed_legs:
        _handle_strategy_exit_failure(order, instance, failed_legs)
        return {"changed": True, "failed": 1}
    if pending_legs == 0 and filled_legs > 0:
        order["strategy_exit_status"] = "filled"
        order["status"] = "strategy_auto_exit_filled"
        order["risk_tracking_active"] = False
        annotate_strategy_order_fill_ledger(order)
        _sync_strategy_position_exit_reconcile(instance, order, "filled", [])
        return {"changed": True, "failed": 0}
    return {"changed": changed, "failed": 0}


def _sync_strategy_position_exit_reconcile(instance: dict[str, Any], order: dict[str, Any], status: str, failed_legs: list[dict[str, Any]]) -> None:
    tracking_id = str(order.get("tracking_id") or "")
    ledger = annotate_strategy_order_fill_ledger(order)
    risk_plan = instance.get("risk_plan") if isinstance(instance.get("risk_plan"), dict) else {}
    positions = risk_plan.get("strategy_positions") if isinstance(risk_plan.get("strategy_positions"), list) else []
    for position in positions:
        if not isinstance(position, dict) or str(position.get("tracking_id") or "") != tracking_id:
            continue
        if ledger.get("has_fills"):
            position["strategy_fill_ledger"] = ledger
            position["realized_pnl"] = ledger.get("realized_pnl")
            position["unrealized_pnl"] = ledger.get("unrealized_pnl")
            position["last_pnl"] = ledger.get("estimated_total_pnl")
            position["actual_exit_pnl"] = ledger.get("realized_pnl")
        if status == "failed":
            _exit_retry_count = int(position.get("auto_exit_retry_count") or 0) + 1
            position["auto_exit_retry_count"] = _exit_retry_count
            position["tracking_status"] = "auto_exit_failed"
            position["risk_tracking_active"] = _exit_retry_count < 3  # retry up to 3 times before abandoning
            position["strategy_exit_error"] = "; ".join(item["error"] for item in failed_legs)[:240]
            position["strategy_exit_failed_legs"] = failed_legs
        elif status == "residual_tracking":
            position["tracking_status"] = "residual_leg_tracking"
            position["risk_tracking_active"] = False
            position["residual_leg_tracking_active"] = True
            position["strategy_exit_error"] = "; ".join(item.get("error") or "" for item in failed_legs)[:240]
            position["strategy_exit_failed_legs"] = failed_legs
            position["strategy_fill_ledger"] = ledger
        elif status == "broker_combo_required":
            position["tracking_status"] = "broker_combo_close_required"
            position["risk_tracking_active"] = False
            position["broker_combo_close_required"] = True
            position["strategy_exit_error"] = "; ".join(item.get("error") or "" for item in failed_legs)[:240]
            position["strategy_exit_failed_legs"] = failed_legs
        elif status == "filled":
            position["tracking_status"] = "auto_exit_filled"
            position["risk_tracking_active"] = False
            if ledger.get("has_fills"):
                actual_pnl = ledger.get("realized_pnl") or 0
                trigger = str(position.get("smart_exit_trigger") or position.get("strategy_exit_trigger") or "")
                symbol = position.get("symbol") or ""
                label = position.get("label") or position.get("strategy_type") or ""
                append_instance_event(
                    instance,
                    "strategy_exit_pnl_confirmed",
                    f"策略平仓成交确认：{symbol} {label} 实际盈亏 {actual_pnl:+.2f}（触发原因：{trigger}）。",
                    status="success" if actual_pnl >= 0 else "warning",
                    payload={
                        "tracking_id": tracking_id,
                        "actual_exit_pnl": round(actual_pnl, 2),
                        "trigger": trigger,
                        "fill_ledger": ledger,
                    },
                )
        break
    risk_plan["strategy_positions"] = positions
    instance["risk_plan"] = risk_plan


def _history_message(detail: dict[str, Any]) -> str:
    history = detail.get("history")
    if not isinstance(history, list):
        return ""
    for item in history:
        if isinstance(item, dict) and item.get("msg"):
            return str(item.get("msg") or "")
    return ""


def _entry_contract_symbol(entry: dict[str, Any]) -> str:
    leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
    return str(entry.get("contract_symbol") or leg.get("contract_symbol") or "")


def _parse_exit_at(value: Any, position: dict[str, Any]) -> datetime | None:
    parsed = parse_datetime(value, assume_tz=EASTERN)
    if parsed is not None:
        return parsed.astimezone(EASTERN)
    text = str(value or "").strip()
    if not text:
        return None
    time_match = None
    if ":" in text:
        import re as _re

        time_match = _re.search(r"(\d{1,2}):(\d{2})", text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        if any(token in text for token in ("前一交易日", "到期前", "到期日前")):
            expiration = parse_datetime(position.get("expiration"), assume_tz=EASTERN)
            base = expiration.astimezone(EASTERN) if expiration else datetime.now(EASTERN)
            base = base - timedelta(days=1)
            return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        base = datetime.now(EASTERN)
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "到期前" in text and "1 个交易日" in text:
        expiration = parse_datetime(position.get("expiration"), assume_tz=EASTERN)
        if expiration is None:
            return None
        return expiration.astimezone(EASTERN).replace(hour=15, minute=50, second=0, microsecond=0) - timedelta(days=1)
    return None


def _infer_strategy_invalidation(text: str) -> dict[str, Any] | None:
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    threshold = _first_number(cleaned)
    if threshold is None:
        return None
    if any(token in cleaned for token in ("跌破", "下方", "失守", "回落")):
        return {"type": "underlying_price", "operator": "<=", "price": threshold, "reason": cleaned}
    if any(token in cleaned for token in ("突破", "上方", "反弹回", "站上")):
        return {"type": "underlying_price", "operator": ">=", "price": threshold, "reason": cleaned}
    return None


def _first_number(text: str) -> float | None:
    import re as _re

    match = _re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _handle_strategy_exit_trigger(
    instance: dict[str, Any],
    position: dict[str, Any],
    strategy_orders: list[dict[str, Any]],
    account_name: str,
    trigger: str,
    pnl: float,
    threshold: float,
) -> dict[str, int]:
    tracking_id = str(position.get("tracking_id") or "")
    is_stop = trigger == "stop"
    is_smart = trigger.startswith("smart_")
    is_final_take_profit = trigger == "tp2" or (trigger == "tp1" and not bool(position.get("tiered_take_profit_enabled")))
    event_type = "strategy_stop_alerted" if is_stop else "strategy_smart_exit_alerted" if is_smart else "strategy_take_profit_alerted"
    target_label = "止损" if is_stop else "智能退出" if is_smart else "第二止盈" if trigger == "tp2" else "止盈" if is_final_take_profit else "第一止盈"
    lifecycle = "manual_intervention_required"
    status = "warning" if is_stop else "success"
    if _strategy_auto_exit_allowed(instance, position):
        exit_result = _submit_strategy_auto_exit(position, strategy_orders, account_name, trigger, instance=instance)
        exit_result["trigger_mark_pnl"] = round(pnl, 2)
        exit_result["threshold_pnl"] = round(threshold, 2)
        submitted = int(exit_result.get("submitted") or 0)
        failed = int(exit_result.get("failed") or 0)
        partial = bool(exit_result.get("partial"))
        # Single-source-of-truth for risk_tracking_active — set exactly once below in each branch.
        position["strategy_exit_trigger"] = trigger
        position["strategy_exit_triggered_at"] = utc_now()
        position["strategy_exit_trigger_mark_pnl"] = round(pnl, 2)
        position["strategy_exit_threshold_pnl"] = round(threshold, 2)
        position["strategy_exit_result"] = exit_result
        if failed:
            _exit_retry_count = int(position.get("auto_exit_retry_count") or 0) + 1
            position["auto_exit_retry_count"] = _exit_retry_count
            position["tracking_status"] = "auto_exit_failed"
            position["strategy_exit_error"] = "; ".join(str(item.get("error") or "") for item in (exit_result.get("failed_legs") or []) if item.get("error"))[:240]
            position["strategy_exit_failed_legs"] = exit_result.get("failed_legs") or []
            if _exit_retry_count < 3:
                position["risk_tracking_active"] = True  # keep monitoring for retry
                lifecycle = "monitoring"
                status = "warning"
            else:
                # Hand off to manual intervention but keep tracking so operators see live state.
                position["risk_tracking_active"] = True
                lifecycle = "manual_intervention_required"
                status = "error"
        elif submitted:
            all_filled = int(exit_result.get("filled") or 0) >= submitted
            position["tracking_status"] = "auto_exit_partial_submitted" if partial else "auto_exit_filled" if all_filled else "auto_exit_submitted"
            if partial:
                lifecycle = "monitoring"
            elif all_filled:
                lifecycle = "monitoring" if _has_other_active_strategy_tracking(instance, tracking_id, strategy_orders) else "closed"
            else:
                lifecycle = "exiting"
            status = "warning" if is_stop else "success"
            # Keep tracking until broker confirms ALL submitted legs filled (fixes silent abandonment
            # when submission succeeds but broker fills are delayed beyond _wait_for_strategy_exit_detail).
            position["risk_tracking_active"] = not all_filled
            if partial:
                position["strategy_exit_closed_units"] = int(position.get("strategy_exit_closed_units") or 0) + int(exit_result.get("closed_units") or 0)
                position["strategy_open_units"] = max(0, _strategy_position_units(position) - int(position.get("strategy_exit_closed_units") or 0))
        else:
            position["tracking_status"] = "auto_exit_no_filled_legs"
            # Nothing submitted but a trigger fired — keep tracking so a retry or manual close can happen.
            position["risk_tracking_active"] = True
            lifecycle = "manual_intervention_required"
            status = "warning"
        _sync_strategy_order_exit(strategy_orders, tracking_id, exit_result)
        synced_order = next((item for item in strategy_orders if isinstance(item, dict) and str(item.get("tracking_id") or "") == tracking_id), {})
        if exit_result.get("failed") and isinstance(synced_order, dict):
            if synced_order.get("status") == "strategy_residual_tracking":
                position["tracking_status"] = "residual_leg_tracking"
                position["risk_tracking_active"] = False
                position["residual_leg_tracking_active"] = True
                lifecycle = "monitoring"
                status = "warning"
            elif synced_order.get("status") == "broker_combo_close_required":
                position["tracking_status"] = "broker_combo_close_required"
                position["risk_tracking_active"] = False
                position["broker_combo_close_required"] = True
                lifecycle = "manual_intervention_required"
                status = "error"
        _sync_strategy_position_exit_reconcile(instance, {"tracking_id": tracking_id, "legs": _strategy_filled_entry_legs(position, strategy_orders)}, "filled" if submitted and int(exit_result.get("filled") or 0) >= submitted and not failed else "submitted", [])
        append_instance_event(
            instance,
            event_type,
            f"策略{target_label}触发并自动退出：{position.get('symbol')} {position.get('label') or position.get('strategy_type')} 触发时行情浮亏 {pnl:+.2f}（已实现盈亏见成交确认），提交平仓 {submitted} 腿，失败 {failed} 腿。",
            lifecycle_state=lifecycle,
            status=status,
            payload={
                "tracking_id": tracking_id,
                "trigger_mark_pnl": round(pnl, 2),
                "threshold": threshold,
                "target": trigger,
                "exit_result": exit_result,
            },
        )
        return {"submitted": submitted, "failed": failed}

    position["tracking_status"] = "stop_alerted" if is_stop else "smart_exit_alerted" if is_smart else f"take_profit_{trigger[-1]}_alerted"
    position["risk_tracking_active"] = False if is_stop or is_smart or is_final_take_profit else bool(position.get("risk_tracking_active"))
    append_instance_event(
        instance,
        event_type,
        f"策略{target_label}触发：{position.get('symbol')} {position.get('label') or position.get('strategy_type')} PnL {pnl:.2f}。",
        lifecycle_state="manual_intervention_required" if is_stop or is_final_take_profit else "monitoring",
        status=status,
        payload={"tracking_id": tracking_id, "pnl": round(pnl, 2), "threshold": threshold, "target": trigger},
    )
    return {"submitted": 0, "failed": 0}


def _strategy_auto_exit_allowed(instance: dict[str, Any], position: dict[str, Any]) -> bool:
    risk_plan = instance.get("risk_plan") if isinstance(instance.get("risk_plan"), dict) else {}
    return bool(risk_plan.get("strategy_auto_execute_enabled") and position.get("execution_status") == "submitted")


def _has_other_active_strategy_tracking(instance: dict[str, Any], tracking_id: str, strategy_orders: list[dict[str, Any]]) -> bool:
    for order in strategy_orders:
        if not isinstance(order, dict) or str(order.get("tracking_id") or "") == tracking_id:
            continue
        if order.get("risk_tracking_active") or order.get("residual_leg_tracking_active") or order.get("broker_combo_close_required"):
            return True
    positions = (instance.get("risk_plan") or {}).get("strategy_positions") if isinstance(instance.get("risk_plan"), dict) else []
    for position in positions or []:
        if not isinstance(position, dict) or str(position.get("tracking_id") or "") == tracking_id:
            continue
        if position.get("risk_tracking_active") or position.get("residual_leg_tracking_active") or position.get("broker_combo_close_required"):
            return True
    return False


def _exit_close_short_legs_first_enabled() -> bool:
    # When auto-closing a multi-leg strategy, cover naked SHORT legs
    # (buy-to-close) BEFORE exiting LONG hedges (sell-to-close), and abort the
    # long exits if a short cover fails. Selling the long first — or continuing
    # to it after a failed short cover — strips the hedge off a still-open short
    # and leaves unbounded naked-short risk. Default ON; the legacy
    # stored-order, keep-closing behavior is available for parity/debugging.
    return (os.getenv("AI_OPTION_EXIT_CLOSE_SHORT_LEGS_FIRST", "true") or "").strip().lower() in {"1", "true", "yes", "on"}


def _exit_close_side(leg: dict[str, Any]) -> str:
    return "sell" if str(leg.get("action") or "").lower() == "buy" else "buy"


def _exit_ordered_entries(entry_legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order exit legs so short covers (buy-to-close) run before long exits
    (sell-to-close), independent of how the entry legs were stored. Stable sort
    preserves original relative order within each group."""
    def _key(entry: dict[str, Any]) -> int:
        leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
        return 0 if _exit_close_side(leg) == "buy" else 1
    return sorted(entry_legs, key=_key)


def _reconcile_monitor_submission(client_key: str, account_name: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    rows = find_recent_order_journal(client_key, within_seconds=900)
    if not rows:
        return None
    latest_after = next((row for row in rows if row.get("phase") == "after" and row.get("order_id")), None)
    latest_before = next((row for row in rows if row.get("phase") == "before"), None)
    if latest_before and (not latest_after or str(latest_before.get("created_at") or "") > str(latest_after.get("created_at") or "")):
        raise RuntimeError("unresolved pre-submit journal record; refusing duplicate strategy exit")
    if not latest_after:
        return None
    order_id = str(latest_after.get("order_id") or "")
    detail = order_detail(order_id, account_name)
    status = _order_status(detail)
    if _is_terminal_unfilled_status(status) and _filled_quantity(detail) <= 0:
        return None
    return {"order_id": order_id, "status": status, "reused_from_journal": True}, detail


def _submit_strategy_auto_exit(
    position: dict[str, Any],
    strategy_orders: list[dict[str, Any]],
    account_name: str,
    trigger: str,
    *,
    instance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    instance = instance if isinstance(instance, dict) else {}
    tracking_id = str(position.get("tracking_id") or "")
    strategy_order = next(
        (
            item
            for item in strategy_orders
            if isinstance(item, dict) and str(item.get("tracking_id") or "") == tracking_id
        ),
        {},
    )
    exit_order_type = adaptive_pricing.normalize_order_type(
        strategy_order.get("exit_order_type") or position.get("exit_order_type")
    )
    entry_legs = _strategy_filled_entry_legs(position, strategy_orders)
    target_units = _strategy_exit_target_units(position, entry_legs, trigger)
    submitted: list[dict[str, Any]] = []
    filled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    short_first = _exit_close_short_legs_first_enabled()
    ordered_entries = _exit_ordered_entries(entry_legs) if short_first else list(reversed(entry_legs))
    short_cover_failed = False
    run_id = str(instance.get("instance_id") or "")
    owner_id = str(instance.get("owner_id") or "local")
    for entry in ordered_entries:
        leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
        side = str(leg.get("side") or "").lower()
        if side == "stock":
            skipped.append({"reason": "stock leg is not auto-closed", "leg": leg})
            continue
        contract_symbol = str(leg.get("contract_symbol") or "").strip()
        if not contract_symbol:
            skipped.append({"reason": "missing contract symbol", "leg": leg})
            continue
        close_side = "sell" if str(leg.get("action") or "").lower() == "buy" else "buy"
        # Do NOT sell a long hedge while a short cover on this combo has failed —
        # that would strip the hedge and leave a naked short. Defer the long exit
        # so the next monitor cycle retries the short cover with both legs intact.
        if short_first and short_cover_failed and close_side == "sell":
            entry["strategy_exit_status"] = "deferred_short_cover_failed"
            skipped.append({"reason": "deferred long exit: short cover failed, holding hedge", "leg": leg})
            continue
        close_quantity = _strategy_leg_target_close_quantity(entry, target_units)
        if close_quantity < 1:
            skipped.append({"reason": "no open quantity", "leg": leg})
            continue
        order_symbol = option_order_symbol(contract_symbol)
        try:
            # LONG legs (sell-to-close) may walk adaptively — but ONLY when
            # short-first ordering is on (shorts already covered above, so a
            # resting long limit is a defined-risk long, never a naked short).
            # SHORT covers (buy-to-close) ALWAYS stay market (naked-short safety).
            strat_cycle = int(entry.get("adaptive_exit_cycle") or 0)
            use_priced_limit_leg = short_first and close_side == "sell" and exit_order_type != "market"
            exit_remark = f"AI_STRATEGY_EXIT {tracking_id} {trigger} {close_side.upper()}"
            cok = client_order_key(run_id, f"{tracking_id}:{contract_symbol}:{close_side}", f"strategy_exit:{trigger}") if idempotency_enabled() and run_id else ""
            reconciled = _reconcile_monitor_submission(cok, account_name) if cok else None
            if reconciled:
                close_order, reconciled_detail = reconciled
                strat_used_market = True
                strat_limit_price = 0.0
                entry["adaptive_exit_resting"] = False
            else:
                record_order_journal(
                    owner_id=owner_id, run_id=run_id or None, client_order_key=cok or None,
                    action=f"strategy_exit_{close_side}", phase="before", account_ref=account_name,
                    symbol=order_symbol, side=close_side, quantity=close_quantity,
                    detail={"tracking_id": tracking_id, "trigger": trigger},
                )
                reconciled_detail = {}
                if use_priced_limit_leg:
                    close_order, strat_limit_price, strat_used_market = _submit_adaptive_exit_close(
                        {"exit_order_type": exit_order_type},
                        order_symbol,
                        close_quantity,
                        close_side,
                        account_name,
                        exit_remark,
                        strat_cycle,
                    )
                    entry["adaptive_exit_resting"] = not strat_used_market
                    entry["adaptive_exit_cycle"] = strat_cycle
                    if not strat_used_market:
                        entry["adaptive_exit_limit_price"] = strat_limit_price
                else:
                    close_order = submit_market_order(order_symbol, close_quantity, close_side, account_name, exit_remark)
                    entry["adaptive_exit_resting"] = False
            close_order_id = _order_id(close_order)
            record_order_journal(
                owner_id=owner_id, run_id=run_id or None, client_order_key=cok or None,
                action=f"strategy_exit_{close_side}", phase="after", account_ref=account_name,
                symbol=order_symbol, side=close_side, quantity=close_quantity,
                order_id=close_order_id, status=_order_status(close_order),
            )
            entry["strategy_exit_order"] = close_order
            entry["strategy_exit_side"] = close_side
            entry["strategy_exit_quantity"] = int(float(entry.get("strategy_exit_quantity") or 0)) + close_quantity
            entry["strategy_exit_trigger"] = trigger
            entry["strategy_exit_submitted_at"] = utc_now()
            entry["strategy_exit_status"] = "submitted"
            detail = reconciled_detail or _wait_for_strategy_exit_detail(close_order, account_name)
            if detail:
                entry["strategy_exit_detail"] = detail
                broker_status = _order_status(detail)
                broker_filled_quantity = _filled_quantity(detail)
                entry["strategy_exit_broker_status"] = broker_status
                entry["strategy_exit_filled_quantity"] = broker_filled_quantity
                record_order_journal(
                    owner_id=owner_id, run_id=run_id or None, client_order_key=cok or None,
                    action=f"strategy_exit_{close_side}", phase="fill", account_ref=account_name,
                    symbol=order_symbol, side=close_side, quantity=close_quantity,
                    order_id=close_order_id, status=broker_status,
                    detail={"filled_quantity": broker_filled_quantity, "executed_price": _executed_price(detail)},
                )
                if _is_terminal_unfilled_status(broker_status) and broker_filled_quantity < 1:
                    error = str(detail.get("msg") or _history_message(detail) or f"exit order {broker_status}").strip()
                    entry["strategy_exit_status"] = "failed"
                    entry["strategy_exit_error"] = error
                    if close_side == "buy":
                        short_cover_failed = True
                    failed.append(
                        {
                            "contract_symbol": contract_symbol,
                            "order_symbol": order_symbol,
                            "side": close_side,
                            "quantity": close_quantity,
                            "order": close_order,
                            "status": broker_status,
                            "error": error,
                        }
                    )
                    continue
                if _is_filled_status_text(broker_status) or broker_filled_quantity >= close_quantity:
                    entry["strategy_exit_status"] = "filled"
                    executed_price = _executed_price(detail)
                    if executed_price > 0:
                        entry["strategy_exit_executed_price"] = round(executed_price, 4)
                    filled.append(
                        {
                            "contract_symbol": contract_symbol,
                            "order_symbol": order_symbol,
                            "side": close_side,
                            "quantity": close_quantity,
                            "order": close_order,
                            "status": broker_status,
                            "executed_price": executed_price,
                        }
                    )
            submitted.append(
                {
                    "contract_symbol": contract_symbol,
                    "order_symbol": order_symbol,
                    "side": close_side,
                    "quantity": close_quantity,
                    "order": close_order,
                }
            )
        except Exception as exc:  # noqa: BLE001
            entry["strategy_exit_status"] = "failed"
            entry["strategy_exit_error"] = str(exc)
            if close_side == "buy":
                short_cover_failed = True
            failed.append({"contract_symbol": contract_symbol, "order_symbol": order_symbol, "side": close_side, "quantity": close_quantity, "error": str(exc)})
    return {
        "trigger": trigger,
        "partial": trigger == "tp1" and bool(position.get("tiered_take_profit_enabled")),
        "closed_units": target_units,
        "submitted": len(submitted),
        "filled": len(filled),
        "failed": len(failed),
        "skipped": len(skipped),
        "submitted_legs": submitted,
        "filled_legs": filled,
        "failed_legs": failed,
        "skipped_legs": skipped,
    }


def _wait_for_strategy_exit_detail(close_order: dict[str, Any], account_name: str) -> dict[str, Any]:
    order_id = _order_id(close_order)
    if not order_id:
        return {}
    try:
        detail = wait_for_order_fill(order_id, account_name, timeout_seconds=3)
    except Exception as exc:  # noqa: BLE001
        return {"status": "detail_unavailable", "msg": str(exc)}
    return detail if isinstance(detail, dict) else {}


def _strategy_filled_entry_legs(position: dict[str, Any], strategy_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tracking_id = str(position.get("tracking_id") or "")
    for order in strategy_orders:
        if isinstance(order, dict) and str(order.get("tracking_id") or "") == tracking_id and isinstance(order.get("legs"), list):
            return [leg for leg in order["legs"] if isinstance(leg, dict)]
    rows = position.get("strategy_entry_orders") if isinstance(position.get("strategy_entry_orders"), list) else []
    return [leg for leg in rows if isinstance(leg, dict)]


def _strategy_leg_open_quantity(entry: dict[str, Any]) -> int:
    filled = int(float(entry.get("filled_quantity") or entry.get("quantity") or 0))
    exited = int(float(entry.get("strategy_exit_quantity") or 0)) if entry.get("strategy_exit_status") in {"submitted", "filled"} else 0
    return max(0, filled - exited)


def _strategy_exit_target_units(position: dict[str, Any], entry_legs: list[dict[str, Any]], trigger: str) -> int:
    open_units = _strategy_position_open_units(position)
    if open_units <= 0:
        open_units = _strategy_open_units_from_entries(entry_legs)
    if trigger == "tp1" and bool(position.get("tiered_take_profit_enabled")):
        return max(1, open_units // 2)
    return max(0, open_units)


def _strategy_open_units_from_entries(entry_legs: list[dict[str, Any]]) -> int:
    units: list[int] = []
    for entry in entry_legs:
        leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
        leg_qty = max(1, int(float(leg.get("qty") or 1)))
        units.append(_strategy_leg_open_quantity(entry) // leg_qty)
    return min(units) if units else 0


def _strategy_leg_target_close_quantity(entry: dict[str, Any], target_units: int) -> int:
    if target_units <= 0:
        return 0
    leg = entry.get("leg") if isinstance(entry.get("leg"), dict) else {}
    leg_qty = max(1, int(float(leg.get("qty") or 1)))
    return min(_strategy_leg_open_quantity(entry), max(1, target_units * leg_qty))


def _sync_strategy_order_exit(strategy_orders: list[dict[str, Any]], tracking_id: str, exit_result: dict[str, Any]) -> None:
    for order in strategy_orders:
        if not isinstance(order, dict) or str(order.get("tracking_id") or "") != tracking_id:
            continue
        order["strategy_exit_result"] = exit_result
        order["strategy_exit_trigger"] = exit_result.get("trigger")
        order["strategy_exit_trigger_mark_pnl"] = exit_result.get("trigger_mark_pnl")
        order["strategy_exit_threshold_pnl"] = exit_result.get("threshold_pnl")
        if exit_result.get("failed"):
            failed_legs = [item for item in (exit_result.get("failed_legs") or []) if isinstance(item, dict)]
            _handle_strategy_exit_failure(order, None, failed_legs)
            return
        partial = bool(exit_result.get("partial"))
        filled = int(exit_result.get("filled") or 0)
        submitted = int(exit_result.get("submitted") or 0)
        all_filled = submitted > 0 and filled >= submitted
        order["strategy_exit_status"] = "failed" if exit_result.get("failed") else "partial_submitted" if partial and submitted else "filled" if all_filled else "submitted" if submitted else "no_filled_legs"
        order["risk_tracking_active"] = bool(partial and not exit_result.get("failed"))
        order["status"] = "strategy_auto_exit_failed" if exit_result.get("failed") else "strategy_auto_exit_partial_submitted" if partial and submitted else "strategy_auto_exit_filled" if all_filled else "strategy_auto_exit_submitted" if submitted else order.get("status")
        annotate_strategy_order_fill_ledger(order)
        return


def _strategy_position_mark(position: dict[str, Any], account_name: str) -> dict[str, Any]:
    legs = position.get("legs") if isinstance(position.get("legs"), list) else []
    if not legs:
        return {"available": False, "status": "missing_legs", "error": "strategy position has no legs"}
    units = _strategy_position_open_units(position)
    mark = 0.0
    pnl = 0.0
    leg_quotes = []
    errors = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        quote_row = _strategy_leg_quote(position, leg, account_name)
        leg_quotes.append(quote_row)
        if not quote_row.get("available"):
            errors.append(str(quote_row.get("error") or "quote unavailable"))
            continue
        leg["last_leg_quote"] = quote_row
        current = _strategy_leg_close_price(quote_row, action=str(leg.get("action") or ""))
        entry = float(leg.get("price") or 0)
        qty = max(1, int(float(leg.get("qty") or 1))) * units
        multiplier = 1 if str(leg.get("side") or "").lower() == "stock" else 100
        action = str(leg.get("action") or "").lower()
        signed_mark = current * qty * multiplier
        mark += signed_mark if action == "buy" else -signed_mark
        if action == "buy":
            pnl += (current - entry) * qty * multiplier
        else:
            pnl += (entry - current) * qty * multiplier
    if errors:
        return {"available": False, "status": "partial_quote_unavailable", "error": "; ".join(errors)[:240], "leg_quotes": leg_quotes}
    return {"available": True, "status": "ok", "mark": mark, "pnl": pnl, "leg_quotes": leg_quotes}


def _strategy_position_units(position: dict[str, Any]) -> int:
    # Returns 0 when no positive unit field is found. Callers MUST guard against 0
    # before computing PnL thresholds — assuming 1 unit can fabricate phantom stops.
    for key in ("strategy_units", "units", "quantity", "entry_filled_quantity"):
        try:
            value = int(float(position.get(key) or 0))
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _strategy_position_open_units(position: dict[str, Any]) -> int:
    total_units = _strategy_position_units(position)
    try:
        explicit = int(float(position.get("strategy_open_units") or 0))
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    try:
        closed = int(float(position.get("strategy_exit_closed_units") or 0))
    except (TypeError, ValueError):
        closed = 0
    return max(0, total_units - closed)


def _ensure_strategy_total_threshold_basis(position: dict[str, Any]) -> None:
    units = _strategy_position_units(position)
    if units <= 1 or position.get("pnl_threshold_basis") == "total_position":
        return
    for key in ("stop_loss_pnl", "take_profit_1_pnl", "take_profit_2_pnl"):
        value = position.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        position.setdefault(f"per_unit_{key}", round(number, 2))
        position[key] = round(number * units, 2)
    if isinstance(position.get("ai_risk_plan"), dict):
        plan = dict(position["ai_risk_plan"])
        for key in ("stop_loss_pnl", "take_profit_1_pnl", "take_profit_2_pnl"):
            if key in position:
                plan[key] = position[key]
        plan["pnl_threshold_basis"] = "total_position"
        plan["strategy_units"] = units
        position["ai_risk_plan"] = plan
    position["pnl_threshold_basis"] = "total_position"
    position["pnl_threshold_units"] = units


def _refresh_strategy_thresholds_for_open_units(position: dict[str, Any]) -> None:
    if position.get("pnl_threshold_basis") != "total_position":
        return
    open_units = _strategy_position_open_units(position)
    if open_units <= 0 or int(position.get("pnl_threshold_units") or 0) == open_units:
        return
    for key in ("stop_loss_pnl", "take_profit_1_pnl", "take_profit_2_pnl"):
        try:
            per_unit = float(position.get(f"per_unit_{key}") or 0)
        except (TypeError, ValueError):
            per_unit = 0
        if per_unit:
            position[key] = round(per_unit * open_units, 2)
    position["pnl_threshold_units"] = open_units


def _strategy_leg_quote(position: dict[str, Any], leg: dict[str, Any], account_name: str) -> dict[str, Any]:
    side = str(leg.get("side") or "").lower()
    try:
        if side == "stock":
            symbol = str(position.get("symbol") or "").strip().upper()
            if not symbol:
                return {"available": False, "side": side, "error": "missing underlying symbol"}
            quote_row = _monitor_underlying_quote(symbol, account_name, position.get("market_data_source"))
            quote_row["side"] = side
            return quote_row
        contract_symbol = str(leg.get("contract_symbol") or "")
        if not contract_symbol:
            return {"available": False, "side": side, "error": "missing contract symbol"}
        order_symbol = option_order_symbol(contract_symbol)
        quote_row = _monitor_option_quote(order_symbol or contract_symbol, account_name, position.get("market_data_source"))
        quote_row["side"] = side
        quote_row["symbol"] = order_symbol
        quote_row["price"] = _strategy_leg_close_price(quote_row, action=str(leg.get("action") or ""))
        return quote_row
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "side": side, "error": str(exc)}


def _strategy_leg_close_price(quote_row: dict[str, Any], action: str) -> float:
    raw = quote_row.get("raw") if isinstance(quote_row.get("raw"), dict) else quote_row
    normalized_action = str(action or "").lower()
    keys = ("bid", "exit_price", "last_done", "price", "last_price", "ask") if normalized_action == "buy" else ("ask", "exit_price", "last_done", "price", "last_price", "bid")
    for key in keys:
        try:
            value = float(raw.get(key) if isinstance(raw, dict) else 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return float(quote_row.get("exit_price") or quote_row.get("price") or 0)


def _order_id(payload: dict[str, Any]) -> str | None:
    for key in ("order_id", "id", "orderId"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _filled_quantity(payload: dict[str, Any]) -> int:
    for key in ("executed_quantity", "filled_quantity", "filled_qty", "quantity_filled"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    status = str(payload.get("status") or payload.get("order_status") or "").lower()
    if "filled" in status:
        for key in ("quantity", "submitted_quantity", "qty"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
    return 0


def _executed_price(payload: dict[str, Any]) -> float:
    for key in ("executed_price", "filled_avg_price", "filled_price", "average_price", "avg_price", "price"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _order_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or payload.get("order_status") or "").strip()


def _is_terminal_unfilled_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {"rejected", "canceled", "cancelled", "expired"}


def _is_filled_status_text(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {"filled", "fullfilled", "fully_filled"} or ("filled" in normalized and "partial" not in normalized)


def _is_terminal_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {"filled", "rejected", "canceled", "cancelled", "expired"}


def _is_stop_unsupported(error: Exception | str) -> bool:
    # Mirror of trading_agent._is_stop_unsupported: Alpaca paper (604050) and
    # uSMART (native_stop_unsupported) both lack broker-side stops and must fall
    # back to software protection rather than surfacing as a monitor error.
    message = str(error).lower()
    return ("604050" in message and "paper account" in message) or "native_stop_unsupported" in message

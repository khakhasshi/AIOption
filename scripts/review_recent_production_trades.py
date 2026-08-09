#!/usr/bin/env python3
"""Read-only production trading audit with compact broker-truth reconciliation."""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from ai_option_scanner.db import connect
from ai_option_scanner.trade_review import build_facts_from_run


OPTION_ROOT = re.compile(r"^([A-Z.]+?)(?:\d{6})[CP]\d{8}(?:\.US)?$")


def loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int:
    result = number(value)
    return int(result) if result is not None else 0


def timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def option_root(symbol: Any) -> str:
    value = str(symbol or "").upper().replace(" ", "")
    match = OPTION_ROOT.match(value)
    return match.group(1) if match else value.split(".", 1)[0]


def first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((row.get(key) for key in keys if row.get(key) not in (None, "")), None)


def leg_payload(row: dict[str, Any]) -> dict[str, Any]:
    leg = row.get("leg") if isinstance(row.get("leg"), dict) else row
    entry_detail = row.get("entry_detail") if isinstance(row.get("entry_detail"), dict) else {}
    exit_detail = row.get("strategy_exit_detail") if isinstance(row.get("strategy_exit_detail"), dict) else {}
    filled = integer(first_value(row, ("filled_quantity", "entry_filled_quantity", "executed_quantity")))
    if filled <= 0 and str(row.get("status") or "").lower() == "filled":
        filled = integer(row.get("quantity"))
    exited = integer(first_value(row, ("strategy_exit_filled_quantity", "strategy_exit_quantity")))
    if exited <= 0 and str(row.get("strategy_exit_status") or "").lower() == "filled":
        exited = integer(exit_detail.get("executed_quantity"))
    return {
        "contract_symbol": leg.get("contract_symbol"),
        "symbol": option_root(leg.get("contract_symbol")),
        "action": leg.get("action"),
        "side": leg.get("side"),
        "strike": number(leg.get("strike")),
        "expiration": leg.get("expiration"),
        "ratio": integer(leg.get("qty")) or 1,
        "entry_order_id": row.get("order_id") or (row.get("entry_order") or {}).get("order_id"),
        "entry_status": row.get("status"),
        "entry_quantity": integer(row.get("quantity")),
        "filled_quantity": filled,
        "entry_price": number(first_value(row, ("entry_price", "actual_entry_price"))) or number(entry_detail.get("executed_price")),
        "exit_order_id": (row.get("strategy_exit_order") or {}).get("order_id"),
        "exit_status": row.get("strategy_exit_status"),
        "exit_quantity": exited,
        "exit_price": number(first_value(row, ("strategy_exit_executed_price", "strategy_exit_price"))) or number(exit_detail.get("executed_price")),
        "exit_error": row.get("strategy_exit_error"),
    }


def compact_order(order: dict[str, Any]) -> dict[str, Any]:
    legs = [leg_payload(row) for row in order.get("legs") or [] if isinstance(row, dict)]
    quantity = integer(first_value(order, ("entry_filled_quantity", "filled_quantity", "executed_quantity")))
    if quantity <= 0 and str(order.get("status") or "").lower() in {"filled", "strategy_auto_exit_filled"}:
        quantity = integer(order.get("quantity"))
    symbols = sorted({row["symbol"] for row in legs if row.get("symbol")})
    if not symbols:
        symbols = [option_root(first_value(order, ("contract_symbol", "symbol")))]
    order_ids = [
        order.get("order_id"),
        (order.get("entry_order") or {}).get("order_id"),
        (order.get("software_stop_order") or {}).get("order_id"),
        (order.get("software_take_profit_order") or {}).get("order_id"),
        (order.get("single_leg_smart_exit_order") or {}).get("order_id"),
    ]
    order_ids.extend(row.get("entry_order_id") for row in legs)
    order_ids.extend(row.get("exit_order_id") for row in legs)
    errors = [
        order.get(key) for key in (
            "error", "monitor_error", "stop_error", "software_stop_error",
            "software_take_profit_error", "single_leg_smart_exit_error", "strategy_exit_error",
        ) if order.get(key)
    ]
    return {
        "strategy_type": order.get("strategy_type") or order.get("family") or "single_leg",
        "symbols": symbols,
        "status": order.get("status"),
        "quantity": integer(order.get("quantity")),
        "filled_quantity": quantity or min((row["filled_quantity"] for row in legs), default=0),
        "entry_time": first_value(order, ("actual_entry_at", "entry_executed_at", "entry_time", "filled_at")),
        "entry_price": number(first_value(order, ("actual_entry_price", "entry_price", "executed_price"))),
        "strategy_entry_net": number(first_value(order, ("strategy_entry_net", "entry_net", "net_debit"))),
        "exit_time": first_value(order, ("exit_filled_at", "single_leg_exit_filled_at", "software_stop_filled_at", "strategy_exit_filled_at")),
        "exit_reason": first_value(order, ("single_leg_smart_exit_reason", "smart_exit_reason", "exit_source", "residual_leg_exit_source", "strategy_exit_trigger")),
        "strategy_exit_status": order.get("strategy_exit_status"),
        "realized_pnl": number(first_value(order, ("strategy_realized_pnl", "realized_pnl"))),
        "residual_leg_tracking_active": bool(order.get("residual_leg_tracking_active")),
        "errors": [str(value)[:300] for value in errors],
        "order_ids": sorted({str(value) for value in order_ids if value}),
        "legs": legs,
    }


def blocked_reason(run: dict[str, Any], instance: dict[str, Any], orders: list[dict[str, Any]]) -> str:
    if run.get("error"):
        return str(run["error"]).splitlines()[0][:240]
    for order in orders:
        for error in order.get("errors") or []:
            if error:
                return str(error)[:240]
    for event in reversed(instance.get("event_timeline") or []):
        if not isinstance(event, dict):
            continue
        message = event.get("message") or event.get("event_type")
        if message:
            return str(message)[:240]
    return "unknown"


def normalize_reason(value: Any) -> str:
    text = str(value or "unknown").lower()
    rules = (
        ("contract_root_mismatch", "contract_root_mismatch"), ("数据完整性阻断", "contract_root_mismatch"),
        ("no candidate", "no_candidates"), ("没有候选", "no_candidates"),
        ("capital", "capital_or_buying_power"), ("资金", "capital_or_buying_power"),
        ("buying power", "capital_or_buying_power"), ("insufficient", "capital_or_buying_power"),
        ("no executable", "no_executable_selection"), ("不可执行", "no_executable_selection"),
        ("outside", "market_session_or_schedule"), ("market", "market_session_or_schedule"),
        ("timeout", "timeout_or_network"), ("network", "timeout_or_network"),
        ("quote", "quote_or_market_data"), ("行情", "quote_or_market_data"),
        ("order", "broker_order_failure"), ("broker", "broker_order_failure"),
    )
    return next((label for token, label in rules if token in text), text[:120])


def summarize_trade(run: dict[str, Any]) -> dict[str, Any]:
    config = run["config"]
    instance = run["trade_instance"]
    orders = run["orders"]
    facts = build_facts_from_run(run)
    metrics = facts.get("metrics") or {}
    symbols = sorted({symbol for order in orders for symbol in order.get("symbols") or [] if symbol})
    strategies = sorted({str(order.get("strategy_type")) for order in orders if order.get("strategy_type")})
    has_fills = any(order.get("filled_quantity", 0) > 0 or any(leg.get("filled_quantity", 0) > 0 for leg in order.get("legs") or []) for order in orders)
    events = [event for event in instance.get("event_timeline") or [] if isinstance(event, dict)]
    event_types = [event.get("event_type") for event in events]
    entry_times = [timestamp(order.get("entry_time")) for order in orders if timestamp(order.get("entry_time"))]
    exit_times = [
        timestamp(event.get("time")) for event in events
        if event.get("event_type") in {"strategy_exit_pnl_confirmed", "strategy_take_profit_alerted", "strategy_stop_alerted", "protection_status_changed"}
        and timestamp(event.get("time"))
    ]
    computed_holding = None
    if entry_times and exit_times:
        elapsed = (max(exit_times) - min(entry_times)).total_seconds() / 60
        computed_holding = max(0, int(elapsed))
    order_exit_reasons = [str(order["exit_reason"]) for order in orders if order.get("exit_reason")]
    return {
        "locator_id": run.get("locator_id"), "created_at": run.get("created_at"),
        "status": run.get("status"), "lifecycle_state": run.get("lifecycle_state"),
        "protection_state": run.get("protection_state"), "stage": run.get("stage"),
        "symbols": symbols, "strategies": strategies, "has_confirmed_fills": has_fills,
        "broker": config.get("broker"), "broker_account": config.get("broker_account") or config.get("longbridge_account"),
        "slot_id": config.get("schedule_slot_id") or config.get("slot_id") or (config.get("schedule_context") or {}).get("slot_id"),
        "ai_enabled": bool(config.get("use_ai") or config.get("ai_enabled")),
        "council_enabled": bool(config.get("council") or config.get("three_advisors")),
        "realized_pnl": metrics.get("realized_pnl"), "return_pct": metrics.get("return_pct"),
        "holding_minutes": metrics.get("holding_minutes") or computed_holding,
        "exit_reason": metrics.get("exit_reason") or metrics.get("first_exit_trigger") or "+".join(sorted(set(order_exit_reasons))) or None,
        "error": run.get("error"), "event_types": event_types, "orders": orders,
    }


def counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in counter.most_common()]


def aggregate(trades: list[dict[str, Any]]) -> dict[str, Any]:
    filled = [trade for trade in trades if trade["has_confirmed_fills"]]
    known = [trade for trade in filled if number(trade.get("realized_pnl")) is not None]
    pnls = [float(trade["realized_pnl"]) for trade in known]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: {"orders": 0, "known_pnl": 0, "pnl": 0.0})
    by_strategy: dict[str, dict[str, float]] = defaultdict(lambda: {"orders": 0, "known_pnl": 0, "pnl": 0.0})
    filled_orders = []
    for trade in filled:
        run_filled_orders = [order for order in trade["orders"] if order.get("filled_quantity", 0) > 0 or any(leg.get("filled_quantity", 0) > 0 for leg in order.get("legs") or [])]
        for order in run_filled_orders:
            order_pnl = number(order.get("realized_pnl"))
            if order_pnl is None and len(run_filled_orders) == 1:
                order_pnl = number(trade.get("realized_pnl"))
            filled_orders.append({**order, "account": trade.get("broker_account"), "run_pnl": trade.get("realized_pnl")})
            for key in order.get("symbols") or ["unknown"]:
                by_symbol[key]["orders"] += 1
                if order_pnl is not None:
                    by_symbol[key]["known_pnl"] += 1; by_symbol[key]["pnl"] += order_pnl
            key = str(order.get("strategy_type") or "unknown")
            by_strategy[key]["orders"] += 1
            if order_pnl is not None:
                by_strategy[key]["known_pnl"] += 1; by_strategy[key]["pnl"] += order_pnl
    holdings = [integer(trade.get("holding_minutes")) for trade in known if integer(trade.get("holding_minutes")) > 0]
    account_breakdown = {}
    for account in sorted({str(trade.get("broker_account") or "unknown") for trade in filled}):
        account_trades = [trade for trade in filled if str(trade.get("broker_account") or "unknown") == account]
        account_pnls = [float(trade["realized_pnl"]) for trade in account_trades if number(trade.get("realized_pnl")) is not None]
        account_wins = [value for value in account_pnls if value > 0]
        account_losses = [value for value in account_pnls if value < 0]
        account_breakdown[account] = {
            "runs": len(account_trades),
            "filled_orders": sum(1 for order in filled_orders if order["account"] == account),
            "total_realized_pnl": round(sum(account_pnls), 2),
            "wins": len(account_wins), "losses": len(account_losses), "flat": len(account_pnls)-len(account_wins)-len(account_losses),
            "win_rate_pct": round(len(account_wins)/len(account_pnls)*100, 2) if account_pnls else None,
            "profit_factor": round(sum(account_wins)/abs(sum(account_losses)), 3) if account_losses else None,
            "median_pnl": round(statistics.median(account_pnls), 2) if account_pnls else None,
        }
    return {
        "confirmed_fill_runs": len(filled), "filled_strategy_orders": len(filled_orders), "known_pnl_runs": len(known),
        "total_realized_pnl": round(sum(pnls), 2), "wins": len(wins), "losses": len(losses), "flat": len(pnls)-len(wins)-len(losses),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 2) if pnls else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
        "average_pnl": round(statistics.mean(pnls), 2) if pnls else None,
        "median_pnl": round(statistics.median(pnls), 2) if pnls else None,
        "best_trade": max(known, key=lambda row: row["realized_pnl"]) if known else None,
        "worst_trade": min(known, key=lambda row: row["realized_pnl"]) if known else None,
        "max_cumulative_drawdown_usd": round(max_drawdown, 2),
        "average_holding_minutes": round(statistics.mean(holdings), 1) if holdings else None,
        "holding_time_coverage": len(holdings),
        "by_account": account_breakdown,
        "by_symbol": {key: {**value, "pnl": round(value["pnl"], 2)} for key, value in sorted(by_symbol.items())},
        "by_strategy": {key: {**value, "pnl": round(value["pnl"], 2)} for key, value in sorted(by_strategy.items())},
        "exit_reasons": counter_rows(Counter(str(order.get("exit_reason") or "unknown") for order in filled_orders)),
        "slots": counter_rows(Counter(str(trade.get("slot_id") or "unknown") for trade in filled)),
        "ai_usage": {"enabled": sum(trade["ai_enabled"] for trade in filled), "disabled": sum(not trade["ai_enabled"] for trade in filled)},
        "closed_but_run_failed": sum(trade["status"] == "failed" and trade["lifecycle_state"] == "closed" for trade in filled),
        "manual_intervention": sum(trade["lifecycle_state"] == "manual_intervention_required" for trade in trades),
        "residual_leg_tracking": sum(any(order.get("residual_leg_tracking_active") for order in trade["orders"]) for trade in filled),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-06-22T00:00:00-04:00")
    parser.add_argument("--until", default="2026-07-23T00:00:00-04:00")
    args = parser.parse_args()
    with connect() as db:
        raw_runs = db.execute(
            """SELECT * FROM trading_runs WHERE created_at >= ? AND created_at < ? ORDER BY created_at""",
            (args.since, args.until),
        ).fetchall()
        snapshots = db.execute(
            """SELECT snapshot_date_et, account_name, created_at, executions_json FROM trading_capital_snapshots
               WHERE snapshot_date_et >= ? AND snapshot_date_et < ? ORDER BY snapshot_date_et""",
            (args.since[:10], args.until[:10]),
        ).fetchall()
        fires = db.execute(
            """SELECT trade_date_et, slot_id, status, run_id, allocated_capital, gate_result_json, retry_count, error
               FROM trading_schedule_fires WHERE trade_date_et >= ? AND trade_date_et < ? ORDER BY trade_date_et, scheduled_time_et""",
            (args.since[:10], args.until[:10]),
        ).fetchall()
    runs = []
    for raw in raw_runs:
        row = dict(raw)
        row["config"] = loads(row.pop("config_json", None), {})
        row["scan_results"] = loads(row.pop("scan_results_json", None), [])
        row["council"] = loads(row.pop("council_json", None), {})
        row["selections"] = loads(row.pop("selections_json", None), [])
        row["orders"] = [compact_order(order) for order in loads(row.pop("orders_json", None), []) if isinstance(order, dict)]
        row["trade_instance"] = loads(row.pop("instance_json", None), {})
        runs.append(row)
    trades = [summarize_trade(run) for run in runs]
    broker_executions: dict[str, dict[str, Any]] = {}
    since_dt, until_dt = timestamp(args.since), timestamp(args.until)
    for snapshot in snapshots:
        for execution in loads(snapshot["executions_json"], []):
            if not isinstance(execution, dict):
                continue
            when = timestamp(execution.get("trade_done_at_et") or execution.get("trade_done_at"))
            if when and since_dt and until_dt and since_dt <= when < until_dt:
                key = str(execution.get("trade_id") or f"{execution.get('order_id')}:{when}:{execution.get('price')}")
                broker_executions[key] = {
                    "trade_id": execution.get("trade_id"), "order_id": str(execution.get("order_id") or ""),
                    "symbol": execution.get("symbol"), "quantity": integer(execution.get("quantity")),
                    "price": number(execution.get("price")), "trade_done_at_et": when.isoformat(),
                }
    filled_trades = [trade for trade in trades if trade["has_confirmed_fills"]]
    known_order_ids = {
        order_id for trade in filled_trades for order in trade["orders"]
        if order.get("filled_quantity", 0) > 0 or any(leg.get("filled_quantity", 0) > 0 for leg in order.get("legs") or [])
        for order_id in order.get("order_ids") or []
    }
    execution_order_ids = {execution["order_id"] for execution in broker_executions.values()}
    matched_order_ids = known_order_ids & execution_order_ids
    matched = sum(execution["order_id"] in known_order_ids for execution in broker_executions.values())
    blocked = [trade for trade in trades if not trade["has_confirmed_fills"]]
    raw_reason_counter = Counter(blocked_reason(run, run["trade_instance"], run["orders"]) for run, trade in zip(runs, trades, strict=True) if not trade["has_confirmed_fills"])
    normalized = Counter()
    for reason, count in raw_reason_counter.items():
        normalized[normalize_reason(reason)] += count
    output = {
        "window": {"since": args.since, "until": args.until},
        "run_summary": {
            "total": len(runs), "owners": len({run["owner_id"] for run in runs}),
            "with_order_objects": sum(bool(run["orders"]) for run in runs),
            "status": counter_rows(Counter(str(run.get("status")) for run in runs)),
            "lifecycle": counter_rows(Counter(str(run.get("lifecycle_state")) for run in runs)),
            "protection": counter_rows(Counter(str(run.get("protection_state")) for run in runs)),
        },
        "trade_performance": aggregate(trades),
        "blocked": {"count": len(blocked), "normalized_reasons": counter_rows(normalized), "top_raw_reasons": counter_rows(raw_reason_counter)[:30]},
        "schedule": {
            "fires": len(fires),
            "status": counter_rows(Counter(str(row["status"]) for row in fires)),
            "slots": counter_rows(Counter(str(row["slot_id"]) for row in fires)),
            "retries": sum(integer(row["retry_count"]) for row in fires),
        },
        "broker_reconciliation": {
            "unique_executions": len(broker_executions), "matched_to_window_runs": matched,
            "unmatched": len(broker_executions)-matched,
            "known_filled_order_ids": len(known_order_ids), "matched_filled_order_ids": len(matched_order_ids),
            "missing_filled_order_ids": len(known_order_ids-matched_order_ids),
            "snapshot_accounts": sorted({str(row["account_name"]) for row in snapshots}),
            "snapshot_date_start": min((str(row["snapshot_date_et"]) for row in snapshots), default=None),
            "snapshot_date_end": max((str(row["snapshot_date_et"]) for row in snapshots), default=None),
            "executions": list(broker_executions.values()),
        },
        "trades": trades,
    }
    print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
from typing import Any

from .longbridge_sdk_client import order_detail, positions
from .time_utils import to_et_iso
from .trading_agent import _strategy_auto_execute_outcome
from .trading_instance import append_instance_event, hydrate_trade_instance, sanitize_instance_orders
from .trading_store import _connect, mark_trading_run


def _load_run(locator_id: str) -> dict[str, Any]:
    with _connect() as db:
        row = db.execute(
            """
            SELECT id, locator_id, owner_id, created_at, config_json, orders_json, instance_json, status, stage, error
            FROM trading_runs
            WHERE locator_id = ?
            """,
            (locator_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Trading run `{locator_id}` not found")
    return dict(row)


def _compact_order_detail(order_id: str, account_name: str) -> dict[str, Any]:
    detail = order_detail(order_id, account_name)
    return detail if isinstance(detail, dict) else {"order_id": order_id}


def _apply_a0a5421e0e0f_broker_truth(orders: list[dict[str, Any]], account_name: str) -> list[dict[str, Any]]:
    if not orders:
        raise ValueError("TRD-A0A5421E0E0F has no orders to correct")

    buy_2475 = _compact_order_detail("1246906101849391104", account_name)
    sell_245 = _compact_order_detail("1246906105343266816", account_name)
    unwind_2475 = _compact_order_detail("1246906140797702144", account_name)

    strategy = dict(orders[0])
    legs = [dict(item) for item in (strategy.get("legs") or []) if isinstance(item, dict)]
    if len(legs) < 2:
        raise ValueError("TRD-A0A5421E0E0F strategy-1 is missing expected legs")

    long_leg = legs[0]
    short_leg = legs[1]

    long_leg["status"] = "filled"
    long_leg["filled_quantity"] = 23
    long_leg["entry_detail"] = buy_2475
    long_leg["entry_price"] = 3.3
    long_leg["actual_entry_price"] = 3.3
    long_leg["entry_price_source"] = "executed_price"
    long_leg["strategy_exit_status"] = "filled"
    long_leg["strategy_exit_quantity"] = 23
    long_leg["strategy_exit_filled_quantity"] = 23
    long_leg["strategy_exit_detail"] = unwind_2475
    long_leg["strategy_exit_error"] = None

    short_leg["status"] = "filled"
    short_leg["filled_quantity"] = 23
    short_leg["entry_detail"] = sell_245
    short_leg["entry_price"] = 5.05
    short_leg["actual_entry_price"] = 5.05
    short_leg["entry_price_source"] = "executed_price"

    strategy["legs"] = [long_leg, short_leg]
    strategy["status"] = "strategy_residual_tracking"
    strategy["entry_filled_quantity"] = 23
    strategy["residual_leg_tracking_active"] = True
    strategy["residual_leg_contract_symbol"] = "AMZN260605C00245000"
    strategy["residual_leg_order_symbol"] = "AMZN260605C245000.US"
    strategy["residual_leg_quantity"] = 23
    strategy["residual_legs"] = [
        {
            "contract_symbol": "AMZN260605C00245000",
            "order_symbol": "AMZN260605C245000.US",
            "action": "sell",
            "filled_quantity": 23,
            "entry_price": 5.05,
            "order_id": "1246906105343266816",
        }
    ]
    strategy["unwind"] = [
        {
            "contract_symbol": "AMZN260605C00247500",
            "side": "sell",
            "quantity": 23,
            "order": {"order_id": "1246906140797702144"},
            "order_id": "1246906140797702144",
            "status": "filled",
            "filled_quantity": 23,
            "entry_price": 3.3,
            "actual_entry_price": 3.3,
            "entry_detail": unwind_2475,
        }
    ]
    strategy["message"] = "broker truth confirmed short 245 leg filled; long 247.5 leg was unwound and closed"
    strategy["error"] = "broker-confirmed short leg remained open after unwind closed the long hedge"

    corrected = [strategy]
    corrected.extend(dict(item) for item in orders[1:])
    return corrected


def correct_trd_a0a5421e0e0f(account_name: str, apply: bool = False) -> dict[str, Any]:
    locator_id = "TRD-A0A5421E0E0F"
    run = _load_run(locator_id)
    config = json.loads(run["config_json"] or "{}")
    config.setdefault("locator_id", locator_id)
    orders = sanitize_instance_orders(json.loads(run["orders_json"] or "[]"))
    raw_instance = json.loads(run["instance_json"] or "{}") if run.get("instance_json") else {}

    corrected_orders = _apply_a0a5421e0e0f_broker_truth(orders, account_name)
    instance = hydrate_trade_instance(
        raw_instance,
        run_id=run["id"],
        owner_id=run["owner_id"],
        config=config,
        orders=corrected_orders,
        created_at=to_et_iso(run["created_at"]),
    )
    append_instance_event(
        instance,
        "manual_broker_truth_correction",
        "已按 Longbridge 实际成交校正：247.5C 买入后已 unwind 卖出，245C 空腿真实留仓 23 张。",
        lifecycle_state=instance.get("lifecycle_state"),
        status="warning",
        payload={
            "account_name": account_name,
            "locator_id": locator_id,
            "remaining_symbol": "AMZN260605C245000.US",
            "remaining_quantity": 23,
            "closed_symbol": "AMZN260605C247500.US",
            "closed_quantity": 23,
        },
    )
    outcome = _strategy_auto_execute_outcome(corrected_orders)

    result = {
        "locator_id": locator_id,
        "status": outcome.get("status") or run.get("status"),
        "stage": outcome.get("stage") or run.get("stage"),
        "error": outcome.get("error"),
        "review_metrics": instance.get("review_metrics") or {},
        "protection_status": instance.get("protection_status") or {},
        "orders": corrected_orders,
    }
    if apply:
        mark_trading_run(
            run["id"],
            status=result["status"],
            stage=result["stage"],
            error=result["error"],
            orders_json=corrected_orders,
            instance_json=instance,
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual broker-truth corrections for audited trading runs")
    parser.add_argument("--account-name", default="ue8823a1a37c3_demo")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rows = positions(args.account_name)
    amzn_rows = [row for row in rows if row.get("symbol") in {"AMZN260605C245000.US", "AMZN260605C247500.US"}]
    print(json.dumps({"positions": amzn_rows}, ensure_ascii=False, indent=2))
    result = correct_trd_a0a5421e0e0f(args.account_name, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
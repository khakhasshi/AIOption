"""One-off reconcile for a strategy run left stuck in strategy_residual_tracking
after its filled leg was already unwound (closed) at the broker but never
reconciled back into instance state (pre-fix ghost residual).

Broker ground truth for TRD-AA3C17485EE5 (verified via order_detail):
  entry  BUY  GOOGL260626P00337500 x5 @ 2.48  order 1255158066895159296 Filled
  unwind SELL GOOGL260626P00337500 x5 @ 2.40  order 1255158077288615936 Filled
  => flat at broker, realized = (2.40 - 2.48) * 5 * 100 = -40.00 USD

DRY-RUN by default. Set COMMIT=1 to persist.
"""
from __future__ import annotations

import json
import os
import sys

from ai_option_scanner.trading_store import get_trading_run, mark_trading_run
from ai_option_scanner.trading_instance import (
    annotate_strategy_order_fill_ledger,
    refresh_protection_from_orders,
)

LOCATOR = os.environ.get("RUN_LOCATOR", "TRD-AA3C17485EE5")
UNWIND_ORDER_ID = "1255158077288615936"
UNWIND_QTY = 5
UNWIND_PRICE = 2.40
COMMIT = os.environ.get("COMMIT") == "1"


def main() -> int:
    run = get_trading_run(LOCATOR)
    if not run:
        print(f"run {LOCATOR} not found")
        return 1
    orders = run["orders"]
    instance = run["trade_instance"]

    target = None
    for order in orders:
        if str(order.get("status")) == "strategy_residual_tracking" or order.get("residual_leg_tracking_active"):
            target = order
            break
    if target is None:
        print("no residual-tracking order found; nothing to reconcile")
        return 0

    print("=== BEFORE ===")
    print("run.status=", run["status"], "stage=", run["stage"])
    print("order.status=", target.get("status"), "residual_active=", target.get("residual_leg_tracking_active"),
          "residual_qty=", target.get("residual_leg_quantity"))
    print("protection.state=", (instance.get("protection_status") or {}).get("state"),
          "manual_attention=", (instance.get("protection_status") or {}).get("requires_manual_attention"))

    # Annotate the filled leg with the confirmed unwind close (broker truth).
    closed = False
    for leg in target.get("legs") or []:
        if int(leg.get("filled_quantity") or 0) <= 0:
            continue
        leg["strategy_exit_status"] = "filled"
        leg["strategy_exit_filled_quantity"] = UNWIND_QTY
        leg["strategy_exit_quantity"] = UNWIND_QTY
        leg["strategy_exit_executed_price"] = UNWIND_PRICE
        leg["strategy_exit_price"] = UNWIND_PRICE
        leg["strategy_exit_detail"] = {
            "status": "filled",
            "executed_quantity": UNWIND_QTY,
            "executed_price": UNWIND_PRICE,
            "order_id": UNWIND_ORDER_ID,
        }
        leg["strategy_exit_reason"] = "unwind"
        closed = True
    if not closed:
        print("no filled leg found to mark closed; aborting")
        return 1

    # Order-level reconcile to flat (same shape the fixed agent now produces).
    target["status"] = "failed"
    target["residual_leg_tracking_active"] = False
    target["residual_legs"] = []
    target["residual_leg_quantity"] = 0
    target["strategy_exit_status"] = "filled"
    target["strategy_exit_reason"] = "unwind"
    target["message"] = "strategy leg unwound to flat; reconciled from broker fills (manual one-off)"
    annotate_strategy_order_fill_ledger(target)

    refresh_protection_from_orders(instance, orders)

    print("=== AFTER ===")
    print("order.status=", target.get("status"), "residual_active=", target.get("residual_leg_tracking_active"),
          "strategy_exit_status=", target.get("strategy_exit_status"))
    print("realized_pnl=", target.get("strategy_realized_pnl"),
          "open_units=", (target.get("strategy_fill_ledger") or {}).get("open_units"))
    print("protection.state=", instance["protection_status"]["state"],
          "manual_attention=", instance["protection_status"].get("requires_manual_attention"))
    print("lifecycle_state=", instance.get("lifecycle_state"))

    if not COMMIT:
        print("\n[DRY-RUN] no write performed. Set COMMIT=1 to persist.")
        return 0

    mark_trading_run(
        run["id"],
        orders_json=orders,
        instance_json=instance,
        status="failed",
        error=target.get("error"),
    )
    print("\n[COMMITTED] run reconciled and persisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

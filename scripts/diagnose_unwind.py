"""Read-only diagnostic for strategy runs that unwound a single filled leg into
an instant spread-cost loss. Prints the inter-leg recheck payloads, unwind
records, and per-leg fill facts already persisted on each run — no broker calls,
no writes.

Usage (inside the app container on the primary):
  RUN_LOCATORS="TRD-05B0F946D641 TRD-9409D22F35B5 TRD-BF427A300F21 TRD-772C1174F426" \
    python /tmp/diagnose_unwind.py
"""
from __future__ import annotations

import json
import os

from ai_option_scanner.trading_store import get_trading_run

LOCATORS = (os.environ.get("RUN_LOCATORS") or "").split()


def _f(v):
    try:
        return round(float(v), 4)
    except Exception:
        return v


def dump_run(loc: str) -> None:
    print("\n" + "=" * 70)
    print("RUN", loc)
    run = get_trading_run(loc)
    if not run:
        print("  NOT FOUND")
        return
    inst = run.get("trade_instance") or {}
    prot = inst.get("protection_status") or {}
    print("  run.status=", run.get("status"), "stage=", run.get("stage"))
    print("  lifecycle=", inst.get("lifecycle_state"),
          "protection=", prot.get("state"),
          "manual_attention=", prot.get("requires_manual_attention"))
    print("  symbol=", inst.get("symbol"), "config.entry_order_type=",
          (inst.get("config") or {}).get("entry_order_type"))
    for i, order in enumerate(run.get("orders") or []):
        if not (order.get("legs") or order.get("strategy_inter_leg_rechecks")
                or order.get("unwind") or order.get("strategy_net_price_gate")):
            continue
        print(f"  --- order[{i}] status={order.get('status')} "
              f"entry_status={order.get('strategy_entry_status')} "
              f"exit_status={order.get('strategy_exit_status')} ---")
        print("    error:", order.get("error"))
        print("    message:", order.get("message"))
        gate = order.get("strategy_net_price_gate") or {}
        if gate:
            print("    net_price_gate: passed=", gate.get("passed"),
                  "expected_net=", _f(gate.get("expected_net")),
                  "actual_net=", _f(gate.get("actual_net")),
                  "tol_pct=", gate.get("tolerance_pct"),
                  "issues=", gate.get("issues"))
        for r in order.get("strategy_inter_leg_rechecks") or []:
            print("    INTER-LEG RECHECK: expected_net=", _f(r.get("expected_net")),
                  "actual_net=", _f(r.get("actual_net")),
                  "issues=", r.get("issues"),
                  "quote_errors=", r.get("quote_errors"))
        for j, leg in enumerate(order.get("legs") or []):
            print(f"    leg[{j}] action={leg.get('action')} "
                  f"contract={leg.get('contract_symbol')} "
                  f"status={leg.get('status')} "
                  f"filled_qty={leg.get('filled_quantity')} "
                  f"price={_f(leg.get('price'))} "
                  f"entry_price={_f(leg.get('entry_price'))} "
                  f"executed_price={_f(leg.get('executed_price'))} "
                  f"exit_status={leg.get('strategy_exit_status')} "
                  f"exit_price={_f(leg.get('strategy_exit_executed_price'))}")
        for u in order.get("unwind") or []:
            print("    UNWIND: confirmed=", u.get("confirmed"),
                  "action=", u.get("action"),
                  "contract=", u.get("contract_symbol"),
                  "qty=", u.get("filled_quantity") or u.get("quantity"),
                  "price=", _f(u.get("executed_price") or u.get("price")),
                  "order_id=", u.get("order_id"))


def main() -> int:
    if not LOCATORS:
        print("set RUN_LOCATORS")
        return 1
    for loc in LOCATORS:
        dump_run(loc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

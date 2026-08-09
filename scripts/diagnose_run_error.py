"""Read-only diagnostic for FAILED trading runs — dumps run status, error,
stage, and the event timeline so broker-auth / gate / submit failures that
happen BEFORE any leg is placed are visible (unlike diagnose_unwind.py which
only prints orders that already have legs). No broker calls, no writes.

Usage (inside the app container on the primary):
  RUN_LOCATORS="TRD-3D8609232576 TRD-7F039A351115" python /tmp/diagnose_run_error.py
"""
from __future__ import annotations

import json
import os

from ai_option_scanner.trading_store import get_trading_run

LOCATORS = (os.environ.get("RUN_LOCATORS") or "").split()


def _short(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def dump_run(loc: str) -> None:
    print("\n" + "=" * 70)
    print("RUN", loc)
    run = get_trading_run(loc)
    if not run:
        print("  NOT FOUND")
        return
    inst = run.get("trade_instance") or {}
    prot = inst.get("protection_status") or {}
    cfg = inst.get("config") or run.get("config") or {}
    print("  run.status=", run.get("status"), "stage=", run.get("stage"),
          "progress=", run.get("progress"))
    print("  lifecycle=", inst.get("lifecycle_state"),
          "protection=", prot.get("state"),
          "manual_attention=", prot.get("requires_manual_attention"))
    print("  symbol=", inst.get("symbol"),
          "broker=", cfg.get("broker"),
          "broker_account=", cfg.get("broker_account"),
          "lb_account=", cfg.get("longbridge_account"))
    print("  run.error=", _short(run.get("error")))
    print("  decision_bucket=", run.get("decision_bucket"))
    # top-level error-ish fields
    for k in ("failure_reason", "block_reason", "message", "readiness"):
        if run.get(k):
            print(f"  {k}=", _short(run.get(k)))
    # order-level errors (may be zero legs)
    for i, order in enumerate(run.get("orders") or []):
        print(f"  order[{i}] status={order.get('status')} "
              f"entry_status={order.get('strategy_entry_status')} "
              f"legs={len(order.get('legs') or [])}")
        if order.get("error"):
            print("    error:", _short(order.get("error")))
        if order.get("message"):
            print("    message:", _short(order.get("message")))
    # event timeline — where broker-auth failures surface
    timeline = inst.get("event_timeline") or run.get("event_timeline") or []
    print(f"  event_timeline ({len(timeline)} events, last 12):")
    for ev in timeline[-12:]:
        if not isinstance(ev, dict):
            print("    -", _short(ev))
            continue
        print("    -", ev.get("event_type") or ev.get("type"),
              "|", ev.get("created_at") or ev.get("at") or "",
              "|", _short(ev.get("message") or ev.get("payload") or "", 220))


def main() -> int:
    if not LOCATORS:
        print("set RUN_LOCATORS")
        return 1
    for loc in LOCATORS:
        dump_run(loc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

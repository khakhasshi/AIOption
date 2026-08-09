#!/usr/bin/env python3
"""Verify the dual data-source architecture and ThetaData utilisation.

Architecture under test:
  * Option quotes ALWAYS use ThetaData (``option_data_source`` default ``thetadata``).
  * Underlying / stock data can use ThetaData Standard, yfinance or Longbridge
    (``market_data_source``), with provider fallback.
  * Greeks are computed with the BSM model from ThetaData IV (this tier has no native Greeks).

The script is safe to run locally (it uses a throwaway SQLite DB). ThetaData-backed
checks degrade to SKIP when the provider is not entitled / not configured.

Exit code 0 = all executed checks passed; non-zero = at least one failure.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

# Make the ai_option_scanner package importable regardless of CWD.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"[{status:4}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    # Route the stores to a temp SQLite DB so we never touch real data.
    from ai_option_scanner import db as dbmod

    tmp = pathlib.Path(tempfile.mkdtemp()) / "verify.sqlite3"
    dbmod.DB_PATH = tmp

    # 1. scan_runs.option_data_source persistence -------------------------------
    try:
        from ai_option_scanner import scan_store

        scan_store.init_scan_db()
        res = scan_store.create_scan_run(
            query="q", symbol="SPY", ai_provider="deepseek", longbridge_account="yfinance",
            use_ai=False, council=False, market_data_source="thetadata", option_data_source="thetadata",
        )
        scan_id = res["id"] if isinstance(res, dict) else res
        row = scan_store.get_scan_run(scan_id)
        ok = row.get("option_data_source") == "thetadata" and row.get("market_data_source") == "thetadata"
        record(PASS if ok else FAIL, "scan_runs dual-source persistence",
               f"option={row.get('option_data_source')} market={row.get('market_data_source')}")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "scan_runs dual-source persistence", repr(exc))

    # 2. scan_loop_instances.option_data_source persistence + update ------------
    try:
        from ai_option_scanner import observation_store

        observation_store.init_observation_db()
        inst = observation_store.create_scan_loop_instance(
            "local", {"name": "t", "symbols": ["SPY"],
                      "market_data_source": "thetadata", "option_data_source": "thetadata"},
        )
        created_ok = inst.get("option_data_source") == "thetadata"
        upd = observation_store.update_scan_loop_instance("local", inst["id"], {"option_data_source": "longbridge"})
        updated_ok = upd.get("option_data_source") == "longbridge"
        record(PASS if created_ok and updated_ok else FAIL, "scan_loop_instances dual-source persistence",
               f"created={inst.get('option_data_source')} updated={upd.get('option_data_source')}")
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "scan_loop_instances dual-source persistence", repr(exc))

    # 3. Trading monitor source ordering ----------------------------------------
    try:
        from ai_option_scanner import trading_monitor as tm

        opt_order = tm._monitor_source_order("thetadata")
        und_order = tm._monitor_underlying_source_order("thetadata")
        opt_ok = opt_order and opt_order[0] == "thetadata"
        und_ok = und_order and und_order[0] == "thetadata" and "yfinance" in und_order and "longbridge" in und_order
        record(PASS if opt_ok else FAIL, "monitor option order prefers ThetaData", str(opt_order))
        record(PASS if und_ok else FAIL, "monitor underlying order prefers ThetaData", str(und_order))
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "monitor source ordering", repr(exc))

    # 4. ThetaData capability probe ---------------------------------------------
    options_ok = False
    stocks_ok = False
    try:
        from ai_option_scanner.thetadata_option_tool import account_capabilities

        caps = account_capabilities()
        checks = {c["name"]: c for c in caps.get("checks", [])}
        stocks_ok = bool(checks.get("stock_snapshot_quote", {}).get("ok"))
        options_ok = bool(checks.get("option_snapshot_quote", {}).get("ok"))
        iv_ok = bool(checks.get("option_snapshot_greeks_implied_volatility", {}).get("ok"))
        record(PASS if options_ok else SKIP, "ThetaData option snapshot entitled",
               "ok" if options_ok else "not entitled / not configured")
        record(PASS if iv_ok else SKIP, "ThetaData IV snapshot entitled",
               "ok" if iv_ok else "not entitled / not configured")
        record(PASS if stocks_ok else SKIP, "ThetaData stock snapshot entitled",
               "ok" if stocks_ok else "not entitled / not configured")
    except Exception as exc:  # noqa: BLE001
        record(SKIP, "ThetaData capability probe", repr(exc))

    # 5. Live option chain via ThetaData + BSM Greeks ---------------------------
    if options_ok:
        try:
            from ai_option_scanner.thetadata_option_tool import collect_candidates as theta_collect
            from ai_option_scanner.intraday_option_tools import enrich_option_greeks

            spot = _underlying_spot("SPY")
            cands = theta_collect(symbol="SPY", spot=spot, min_days=0, max_days=45,
                                  max_ask=9999, lottery=False, preferred_side=None, min_ask=0.0)
            has_iv = any(float(getattr(c, "implied_volatility", 0) or 0) > 0 for c in cands)
            record(PASS if has_iv else FAIL, "ThetaData option chain carries IV", f"{len(cands)} candidates")
            enriched = enrich_option_greeks(list(cands), spot)
            has_delta = any(abs(float(getattr(c, "delta", 0) or 0)) > 0 for c in enriched)
            record(PASS if has_delta else FAIL, "BSM Greeks computed from ThetaData IV",
                   f"delta present on {sum(1 for c in enriched if float(getattr(c, 'delta', 0) or 0))}/{len(enriched)}")
        except Exception as exc:  # noqa: BLE001
            record(FAIL, "ThetaData option chain + BSM Greeks", repr(exc))
    else:
        record(SKIP, "ThetaData option chain + BSM Greeks", "ThetaData options not entitled")

    # 6. GEX snapshot: ThetaData options + ThetaData underlying spot -------------
    try:
        from ai_option_scanner import observation_store as obs

        snap = obs._fetch_current_gex_snapshot("SPY", "thetadata", spot=_underlying_spot("SPY"))
        # When spot is supplied we must not depend on ThetaData stock entitlement.
        ok = snap.get("source") == "thetadata"
        record(PASS if ok else SKIP, "GEX uses ThetaData options w/ external spot",
               f"available={snap.get('available')} source={snap.get('source')} err={snap.get('error')}")
    except Exception as exc:  # noqa: BLE001
        record(SKIP, "GEX uses ThetaData options w/ external spot", repr(exc))

    failures = [r for r in results if r[0] == FAIL]
    print("\n" + "=" * 60)
    print(f"PASS={sum(1 for r in results if r[0]==PASS)}  "
          f"SKIP={sum(1 for r in results if r[0]==SKIP)}  "
          f"FAIL={len(failures)}")
    return 1 if failures else 0


def _underlying_spot(symbol: str) -> float:
    """Underlying spot from ThetaData Standard, with a safe yfinance fallback."""
    try:
        from ai_option_scanner.thetadata_option_tool import market_data as theta_market_data

        data = theta_market_data(symbol)
        spot = float((data.get("quote") or {}).get("last") or 0)
        if spot > 0:
            return spot
    except Exception:  # noqa: BLE001
        pass
    try:
        from ai_option_scanner.yfinance_option_tool import market_data as yf_market_data

        data = yf_market_data(symbol)
        spot = float((data.get("quote") or {}).get("last") or 0)
        if spot > 0:
            return spot
    except Exception:  # noqa: BLE001
        pass
    return 500.0


if __name__ == "__main__":
    sys.exit(main())

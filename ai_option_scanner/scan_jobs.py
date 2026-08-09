from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .account_store import normalize_owner_id, preferred_sdk_account, resolve_account
from .concurrency import env_int
from .scan_service import run_scan
from .scan_store import (
    create_scan_run,
    get_scan_run,
    mark_scan_failed,
    mark_scan_running,
    mark_scan_stage,
    mark_scan_succeeded,
)
from .redis_queue import enqueue_scan, redis_queue_enabled


SCAN_WORKERS = env_int("AI_OPTION_SCAN_WORKERS", 2, 1, 16)
SCAN_QUEUE_LIMIT = env_int("AI_OPTION_SCAN_QUEUE_LIMIT", SCAN_WORKERS * 8, SCAN_WORKERS, 256)
_executor = ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="ai-option-scan")
_queue_slots = threading.BoundedSemaphore(SCAN_QUEUE_LIMIT)


def submit_scan(
    query: str,
    symbol: str | None,
    ai_provider: str,
    longbridge_account: str | None,
    use_ai: bool,
    council: bool,
    analysis_modules: dict[str, Any] | None = None,
    strategy_modes: list[str] | None = None,
    market_data_source: str = "auto",
    option_data_source: str = "thetadata",
    owner_id: str | None = None,
    ai_provider_owner: str | None = None,
    source_type: str = "scan",
    source_id: str | None = None,
    scan_loop_instance_id: str | None = None,
) -> dict[str, Any]:
    requested_source = _normalize_market_data_source(market_data_source)
    option_source = _normalize_option_data_source(option_data_source)
    owner = normalize_owner_id(owner_id)
    has_account = bool(longbridge_account and str(longbridge_account).strip().lower() != "yfinance")
    preferred_account = preferred_sdk_account(owner)
    if requested_source == "thetadata":
        account_name = "thetadata"
        run_owner = owner
        source = "thetadata"
    elif requested_source == "auto":
        account_name = "thetadata"
        run_owner = owner
        source = "thetadata"
    elif requested_source == "longbridge":
        account = resolve_account(longbridge_account, owner_id=owner) if has_account else preferred_account
        if account is None or not account.sdk_credentials_configured:
            account = preferred_account
        if account is None:
            account = resolve_account(longbridge_account, owner_id=owner)
        account_name = account.name
        run_owner = account.owner_id
        source = "longbridge"
    else:
        account_name = "yfinance"
        run_owner = owner
        source = "yfinance"
    row = create_scan_run(
        query=query,
        symbol=symbol,
        ai_provider=ai_provider,
        longbridge_account=account_name,
        use_ai=use_ai,
        council=council,
        analysis_modules=analysis_modules,
        strategy_modes=strategy_modes,
        market_data_source=source,
        option_data_source=option_source,
        owner_id=run_owner,
        ai_provider_owner=ai_provider_owner or run_owner,
        source_type=source_type,
        source_id=source_id,
        scan_loop_instance_id=scan_loop_instance_id,
    )
    if redis_queue_enabled():
        enqueue_scan(row["id"])
    else:
        if not _queue_slots.acquire(blocking=False):
            mark_scan_failed(row["id"], f"scan queue is full; retry later (limit={SCAN_QUEUE_LIMIT})")
            return get_scan_run(row["id"], owner_id=run_owner) or row
        _executor.submit(_run_scan_job, row["id"], True)
    return row


def _run_scan_job(scan_id: str, release_local_slot: bool = False) -> None:
    try:
        row = get_scan_run(scan_id)
        if row is None:
            return
        if row.get("status") not in {"queued", "running"}:
            return
        mark_scan_running(scan_id)
        try:
            result = run_scan(
                query=row["query"],
                symbol=row["symbol"],
                ai_provider=row["ai_provider"],
                longbridge_account=row["longbridge_account"],
                use_ai=row["use_ai"],
                council=row["council"],
                analysis_modules=row.get("analysis_modules"),
                strategy_modes=row.get("strategy_modes"),
                market_data_source=row.get("market_data_source") or "auto",
                option_data_source=row.get("option_data_source") or "thetadata",
                progress_callback=lambda stage, progress: mark_scan_stage(scan_id, stage, progress),
                ai_provider_owner=row.get("ai_provider_owner") or row.get("owner_id"),
                scan_id=scan_id,
                scan_loop_instance_id=row.get("scan_loop_instance_id") or "",
                source_type=row.get("source_type") or "scan",
            )
        except Exception as exc:
            mark_scan_failed(scan_id, str(exc))
            return
        mark_scan_succeeded(scan_id, result)
    finally:
        if release_local_slot:
            _queue_slots.release()


def _normalize_market_data_source(value: str | None) -> str:
    source = str(value or "thetadata").strip().lower()
    if source in {"auto", "longbridge", "yfinance", "thetadata"}:
        return source
    return "thetadata"


def _normalize_option_data_source(value: str | None) -> str:
    source = str(value or "thetadata").strip().lower()
    if source == "auto":
        return "thetadata"
    if source in {"longbridge", "yfinance", "thetadata"}:
        return source
    return "thetadata"

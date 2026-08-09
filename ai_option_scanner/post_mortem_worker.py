"""Background worker that produces AI post-mortems for closed trading runs.

Polls every `AI_OPTION_POST_MORTEM_WORKER_SECONDS` (default 120s, range
30-3600). For each closed run without a review, it builds a facts
snapshot, calls the AI provider, and stores the result.

Activation rule (skip otherwise): trade had submitted positions AND
(|realized_pnl| >= threshold OR holding_minutes >= threshold). See
`trade_review.should_skip_review` and the related env vars.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Any

from .ai_providers import DEFAULT_PROVIDER_NAME
from .trade_review import build_facts_from_run, generate_review, should_skip_review
from .trade_review_store import (
    REVIEW_STATUS_FAILED,
    get_trade_review,
    list_unreviewed_closed_run_ids,
    list_pending_review_run_ids,
    mark_review_completed,
    mark_review_failed,
    mark_review_processing,
    mark_review_skipped,
    upsert_pending_review,
)
from .trading_store import get_trading_run


_started = False
_lock = threading.Lock()


def start_post_mortem_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_post_mortem_loop,
        name="post-mortem-worker-loop",
        daemon=True,
    ).start()


def run_post_mortem_worker_once(
    discover_limit: int | None = None,
    process_limit: int | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Discover closed-but-unreviewed runs, enqueue them, then process pending."""
    discover_cap = discover_limit or _env_int("AI_OPTION_POST_MORTEM_DISCOVER_LIMIT", 20, 1, 200)
    process_cap = process_limit or _env_int("AI_OPTION_POST_MORTEM_PROCESS_LIMIT", 5, 1, 50)
    attempts_cap = max_attempts or _env_int("AI_OPTION_POST_MORTEM_MAX_ATTEMPTS", 3, 1, 10)
    age_hours = _env_int("AI_OPTION_POST_MORTEM_MAX_AGE_HOURS", 168, 1, 24 * 90)

    discovered = 0
    enqueued = 0
    skipped = 0
    succeeded = 0
    failed = 0

    for run_id in list_unreviewed_closed_run_ids(limit=discover_cap, max_age_hours=age_hours):
        discovered += 1
        run = get_trading_run(run_id)
        if not run:
            continue
        facts = build_facts_from_run(run)
        skip_reason = should_skip_review(facts)
        upsert_pending_review(
            run_id=run_id,
            owner_id=run.get("owner_id"),
            locator_id=run.get("locator_id"),
            lifecycle_state=(run.get("trade_instance") or {}).get("lifecycle_state"),
            facts=facts,
            exit_reason=(facts.get("metrics") or {}).get("exit_reason"),
            realized_pnl=(facts.get("metrics") or {}).get("realized_pnl"),
            return_pct=(facts.get("metrics") or {}).get("return_pct"),
            holding_minutes=(facts.get("metrics") or {}).get("holding_minutes"),
        )
        if skip_reason:
            mark_review_skipped(run_id, skip_reason)
            skipped += 1
        else:
            enqueued += 1

    pending_ids = list_pending_review_run_ids(limit=process_cap, max_attempts=attempts_cap)
    for run_id in pending_ids:
        review_row = get_trade_review(run_id)
        if not review_row:
            continue
        owner_id = review_row.get("owner_id")
        facts = review_row.get("facts") or {}
        mark_review_processing(run_id)
        review, error = generate_review(
            facts,
            owner_id=owner_id,
            provider_name=_provider_for_run(facts),
        )
        if review is None:
            mark_review_failed(run_id, error or "unknown_error")
            failed += 1
            continue
        mark_review_completed(
            run_id,
            review=review,
            ai_provider=(facts.get("config") or {}).get("ai_provider") or DEFAULT_PROVIDER_NAME,
            ai_model=None,
        )
        succeeded += 1

    return {
        "discovered": discovered,
        "enqueued": enqueued,
        "skipped": skipped,
        "succeeded": succeeded,
        "failed": failed,
        "pending_after": max(0, len(pending_ids) - succeeded - failed),
    }


def _provider_for_run(facts: dict[str, Any]) -> str:
    provider = (facts.get("config") or {}).get("ai_provider")
    if provider and isinstance(provider, str):
        return provider
    return DEFAULT_PROVIDER_NAME


def _post_mortem_loop() -> None:
    interval = _env_int("AI_OPTION_POST_MORTEM_WORKER_SECONDS", 120, 30, 3600)
    while True:
        try:
            run_post_mortem_worker_once()
        except Exception:
            traceback.print_exc(limit=6)
        time.sleep(interval)


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    return max(low, min(value, high))


# expose constant for callers / tests
__all__ = [
    "start_post_mortem_worker",
    "run_post_mortem_worker_once",
    "REVIEW_STATUS_FAILED",
]

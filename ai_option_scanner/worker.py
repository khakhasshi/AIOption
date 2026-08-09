from __future__ import annotations

import os
import signal
import threading
import time
import traceback
from pathlib import Path

from .ai_client import load_dotenv
from .concurrency import env_int
from .redis_queue import acquire_requeue_lock, enqueue_scan, pop_scan, redis_queue_enabled, release_requeue_lock
from .scan_jobs import _run_scan_job
from .scan_store import list_pending_scan_ids
from .notification_worker import start_notification_worker
from .observation_scheduler import start_observation_scheduler
from .auto_trade_scheduler import start_auto_trade_scheduler
from .post_mortem_worker import start_post_mortem_worker
from .trading_monitor import start_order_monitor
from .trading_scheduler import start_trading_scheduler


APP_ROOT = Path(__file__).resolve().parents[1]
_stop = threading.Event()


def main() -> None:
    load_dotenv(APP_ROOT / ".env")
    if not redis_queue_enabled():
        raise SystemExit("AI_OPTION_REDIS_URL is required for worker mode")
    _install_signals()
    _requeue_pending_scans()
    if _env_bool("AI_OPTION_ENABLE_TRADING_SCHEDULER", False):
        start_trading_scheduler()
    if _env_bool("AI_OPTION_ENABLE_ORDER_MONITOR", False):
        start_order_monitor()
    if _env_bool("AI_OPTION_ENABLE_OBSERVATION_SCHEDULER", True):
        start_observation_scheduler()
    if _env_bool("AI_OPTION_ENABLE_AUTO_TRADE_SCHEDULER", False):
        start_auto_trade_scheduler()
    if _env_bool("AI_OPTION_ENABLE_NOTIFICATION_WORKER", True):
        start_notification_worker()
    if _env_bool("AI_OPTION_ENABLE_POST_MORTEM_WORKER", True):
        start_post_mortem_worker()
    thread_count = env_int("AI_OPTION_WORKER_SCAN_THREADS", int(os.getenv("AI_OPTION_SCAN_WORKERS") or 2), 1, 16)
    threads = [_spawn_scan_thread(index) for index in range(thread_count)]
    while not _stop.is_set():
        for index, thread in enumerate(list(threads)):
            if not thread.is_alive() and not _stop.is_set():
                threads[index] = _spawn_scan_thread(index)
        time.sleep(1)


def _spawn_scan_thread(index: int) -> threading.Thread:
    thread = threading.Thread(target=_scan_loop, args=(index,), name=f"redis-scan-worker-{index + 1}", daemon=True)
    thread.start()
    return thread


def _scan_loop(index: int) -> None:
    while not _stop.is_set():
        try:
            scan_id = pop_scan(timeout=5)
            if not scan_id:
                continue
            _run_scan_job(scan_id, release_local_slot=False)
        except Exception:
            traceback.print_exc(limit=8)
            time.sleep(2)


def _requeue_pending_scans() -> None:
    if not acquire_requeue_lock():
        return
    try:
        for scan_id in list_pending_scan_ids(limit=500):
            enqueue_scan(scan_id)
    finally:
        release_requeue_lock()


def _install_signals() -> None:
    def stop(_signum: int, _frame: object) -> None:
        _stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    main()

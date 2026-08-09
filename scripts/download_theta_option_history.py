#!/usr/bin/env python3
"""Download a resumable ThetaData option backtest dataset to local Parquet files."""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import polars as pl
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_option_scanner.market_calendar import is_nyse_trading_day, previous_nyse_trading_day
from ai_option_scanner.time_utils import now_et
from ai_option_scanner.thetadata_option_tool import _with_session_retry


DEFAULT_SYMBOLS = ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "QQQ", "SPY")
WORKER_ID = "main"
REQUEST_TIMEOUT_SECONDS = 120


@dataclass
class DownloadStats:
    files_written: int = 0
    files_skipped: int = 0
    empty_responses: int = 0
    rows_written: int = 0
    bytes_written: int = 0
    errors: int = 0


def _assigned_tasks(
    symbols: tuple[str, ...],
    days: list[date],
    shard_count: int,
    shard_index: int,
) -> list[tuple[date, str]]:
    """Return a deterministic, balanced subset of symbol/date partitions."""
    if shard_count < 1:
        raise ValueError("task shard count must be at least 1")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("task shard index must be within task shard count")
    # Interleave dates within each symbol so every worker receives a slice of
    # every underlying. This spreads heavy chains such as QQQ and SPY across
    # workers instead of pinning each heavy symbol to one worker.
    tasks = ((day, symbol) for symbol in symbols for day in days)
    return [
        (day, symbol)
        for ordinal, (day, symbol) in enumerate(tasks)
        if ordinal % shard_count == shard_index
    ]


def completed_trading_days(count: int, end: date | None = None) -> list[date]:
    candidate = end or date.today()
    if not is_nyse_trading_day(candidate)[0]:
        candidate = previous_nyse_trading_day(candidate - timedelta(days=1))
    elif end is None and now_et().hour < 17:
        candidate = previous_nyse_trading_day(candidate - timedelta(days=1))
    days: list[date] = []
    while len(days) < count:
        if is_nyse_trading_day(candidate)[0]:
            days.append(candidate)
        candidate -= timedelta(days=1)
    return sorted(days)


def _records(frame: Any) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()
    if isinstance(frame, pd.DataFrame):
        return frame.copy()
    return pd.DataFrame(frame)


def _write_parquet(frame: pd.DataFrame, path: Path) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    normalized = frame.reset_index(drop=True)
    pl.DataFrame(normalized.to_dict(orient="list"), strict=False).write_parquet(
        temporary,
        compression="zstd",
        compression_level=6,
        statistics=True,
    )
    temporary.replace(path)
    return len(normalized), path.stat().st_size


def _valid_parquet(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    try:
        return pl.scan_parquet(path).select(pl.len()).collect().item() > 0
    except Exception:  # noqa: BLE001 - corrupt partial output must be downloaded again.
        return False


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise TimeoutError(f"ThetaData request exceeded {REQUEST_TIMEOUT_SECONDS}s")


def _client_call(operation: Callable[[Any], Any], attempts: int = 4) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            previous = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, REQUEST_TIMEOUT_SECONDS)
            try:
                return _with_session_retry(operation)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
        except Exception:  # noqa: BLE001
            if attempt >= attempts:
                raise
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError("unreachable")


def _record_event(root: Path, event: dict[str, Any]) -> None:
    event = {"at": datetime.now().astimezone().isoformat(), "worker": WORKER_ID, **event}
    with (root / f"download-{WORKER_ID}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def _check_capacity(root: Path, max_bytes: int, reserve_bytes: int) -> None:
    usage = shutil.disk_usage(root)
    current = sum(
        int(marker.read_text().strip())
        for marker in root.glob(".dataset_bytes*")
        if marker.is_file() and marker.read_text().strip().isdigit()
    )
    if current >= max_bytes:
        raise RuntimeError(f"dataset size cap reached: {current:,} >= {max_bytes:,} bytes")
    if usage.free <= reserve_bytes:
        raise RuntimeError(f"disk reserve reached: {usage.free:,} <= {reserve_bytes:,} bytes")


def _add_size(root: Path, byte_count: int) -> None:
    marker = root / f".dataset_bytes.{WORKER_ID}"
    current = int(marker.read_text().strip()) if marker.exists() else 0
    marker.write_text(str(current + byte_count), encoding="ascii")


def _download_frame(
    root: Path,
    relative_path: Path,
    fetch: Callable[[], Any],
    stats: DownloadStats,
    max_bytes: int,
    reserve_bytes: int,
) -> None:
    target = root / relative_path
    if _valid_parquet(target):
        stats.files_skipped += 1
        return
    _check_capacity(root, max_bytes, reserve_bytes)
    try:
        frame = _records(fetch())
    except Exception as exc:  # noqa: BLE001 - ThetaData uses an exception for empty partitions.
        if exc.__class__.__name__ != "NoDataFoundError" and "no data found" not in str(exc).lower():
            raise
        frame = pd.DataFrame()
    if frame.empty:
        stats.empty_responses += 1
        _record_event(root, {"status": "empty", "path": str(relative_path)})
        return
    rows, byte_count = _write_parquet(frame, target)
    _add_size(root, byte_count)
    stats.files_written += 1
    stats.rows_written += rows
    stats.bytes_written += byte_count
    _record_event(root, {"status": "written", "path": str(relative_path), "rows": rows, "bytes": byte_count})


def download_day(
    root: Path,
    symbol: str,
    day: date,
    stats: DownloadStats,
    max_bytes: int,
    reserve_bytes: int,
) -> None:
    contracts = _records(
        _client_call(lambda client: client.option_list_contracts("quote", day, symbol=symbol))
    )
    if contracts.empty or "expiration" not in contracts:
        _record_event(root, {"status": "no_contracts", "symbol": symbol, "date": day})
        return
    contracts = contracts[contracts["symbol"].astype(str).str.upper() == symbol]
    expirations = sorted({
        expiration
        for value in contracts["expiration"]
        if (expiration := date.fromisoformat(str(value)[:10])) >= day
    })
    _record_event(root, {
        "status": "inventory",
        "symbol": symbol,
        "date": day,
        "contracts": len(contracts),
        "expirations": len(expirations),
    })

    _download_frame(
        root,
        Path("underlying") / f"symbol={symbol}" / f"date={day.isoformat()}" / "ohlc.parquet",
        lambda: _client_call(lambda client: client.stock_history_ohlc(symbol, date=day, interval="1m")),
        stats,
        max_bytes,
        reserve_bytes,
    )
    for expiration in expirations:
        base = Path("options") / f"symbol={symbol}" / f"date={day.isoformat()}" / f"expiration={expiration.isoformat()}"
        _download_frame(
            root,
            base / "quote_1m.parquet",
            lambda expiration=expiration: _client_call(
                lambda client: client.option_history_quote(
                    symbol,
                    expiration=expiration,
                    interval="1m",
                    date=day,
                    strike="*",
                    right="both",
                )
            ),
            stats,
            max_bytes,
            reserve_bytes,
        )
        _download_frame(
            root,
            base / "open_interest.parquet",
            lambda expiration=expiration: _client_call(
                lambda client: client.option_history_open_interest(
                    symbol,
                    expiration=expiration,
                    date=day,
                    strike="*",
                    right="both",
                )
            ),
            stats,
            max_bytes,
            reserve_bytes,
        )


def main() -> None:
    global REQUEST_TIMEOUT_SECONDS, WORKER_ID
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--trading-days", type=int, default=50)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path, default=Path.home() / "MarketData" / "thetadata-options")
    parser.add_argument("--max-gb", type=float, default=90.0)
    parser.add_argument("--reserve-gb", type=float, default=80.0)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--worker-id", default="main")
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument(
        "--task-shards",
        type=int,
        default=1,
        help="Number of deterministic symbol/date partitions to divide across workers",
    )
    parser.add_argument(
        "--task-shard-index",
        type=int,
        default=0,
        help="Zero-based task partition assigned to this worker",
    )
    parser.add_argument(
        "--partition-retries",
        type=int,
        default=2,
        help="Additional attempts for a failed symbol/date partition",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file)
    WORKER_ID = "".join(character for character in args.worker_id if character.isalnum() or character in "-_") or "main"
    REQUEST_TIMEOUT_SECONDS = max(30, args.request_timeout)
    symbols = tuple(dict.fromkeys(str(value).strip().upper() for value in args.symbols if str(value).strip()))
    days = completed_trading_days(args.trading_days, args.end_date)
    tasks = _assigned_tasks(symbols, days, args.task_shards, args.task_shard_index)
    root = args.output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    metadata = {
        "version": 1,
        "symbols": symbols,
        "start_date": days[0].isoformat(),
        "end_date": days[-1].isoformat(),
        "trading_days": len(days),
        "datasets": ["option_quote_1m", "option_open_interest_daily", "underlying_ohlc_1m"],
        "partitioning": "symbol/date/expiration",
        "task_shards": args.task_shards,
        "task_shard_index": args.task_shard_index,
        "assigned_tasks": len(tasks),
    }
    worker_metadata = {**metadata, "worker_id": WORKER_ID}
    (root / f"dataset-{WORKER_ID}.json").write_text(json.dumps(worker_metadata, indent=2), encoding="utf-8")
    stats = DownloadStats()
    max_bytes = int(args.max_gb * 1_000_000_000)
    reserve_bytes = int(args.reserve_gb * 1_000_000_000)
    pending = tasks
    for retry_round in range(max(0, args.partition_retries) + 1):
        failed: list[tuple[date, str]] = []
        for day, symbol in pending:
            try:
                download_day(root, symbol, day, stats, max_bytes, reserve_bytes)
            except Exception as exc:  # noqa: BLE001 - continue other partitions and retain retry log.
                stats.errors += 1
                failed.append((day, symbol))
                _record_event(root, {
                    "status": "error",
                    "symbol": symbol,
                    "date": day,
                    "attempt": retry_round + 1,
                    "error": str(exc),
                })
                if "cap reached" in str(exc) or "reserve reached" in str(exc):
                    raise
        if not failed:
            break
        pending = failed
        if retry_round < max(0, args.partition_retries):
            time.sleep(min(5 * (retry_round + 1), 15))
    summary = {**metadata, **stats.__dict__, "sha256": hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()}
    (root / f"summary-{WORKER_ID}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

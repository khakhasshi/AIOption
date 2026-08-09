#!/usr/bin/env python3
"""Replay filled two-leg strategies with alternative, no-lookahead entry rules.

Run this inside the production app container, where the trading database and
ThetaData credentials are available. The script is read-only: it loads historic
run metadata and minute NBBO/underlying bars, then writes JSON and Markdown.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ai_option_scanner.db import connect
from ai_option_scanner.option_symbol_utils import parse_option_symbol
from ai_option_scanner.thetadata_option_tool import _with_session_retry
from ai_option_scanner.time_utils import EASTERN, parse_datetime


@dataclass
class Leg:
    contract_symbol: str
    action: str
    ratio: int
    root: str
    expiration: str
    strike: float
    right: str


@dataclass
class Sample:
    locator_id: str
    created_at: str
    start_at: datetime
    symbol: str
    strategy_type: str
    direction: str
    units: int
    stop_loss_pct: float
    take_profit_pct: float
    legs: list[Leg]


@dataclass
class ReplayResult:
    locator_id: str
    symbol: str
    strategy_type: str
    direction: str
    policy: str
    candidate_at: str
    entered: bool
    skipped_reason: str | None
    entry_at: str | None
    wait_minutes: float | None
    entry_value: float | None
    friction_loss: float | None
    friction_to_stop: float | None
    stop_threshold: float | None
    take_profit_threshold: float | None
    exit_at: str | None
    exit_reason: str | None
    pnl_per_unit: float | None
    return_on_risk_pct: float | None
    mfe_per_unit: float | None
    mae_per_unit: float | None
    stopped_then_profitable: bool


POLICIES = ("immediate", "friction_guard", "trend_confirm", "vwap_reclaim")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _et(value: Any) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN)


def _direction(strategy_type: str) -> str:
    value = str(strategy_type or "").lower()
    if value.startswith("bull_"):
        return "bullish"
    if value.startswith("bear_"):
        return "bearish"
    return "neutral"


def _entry_start(run: dict[str, Any], order: dict[str, Any]) -> datetime | None:
    candidates = [
        order.get("actual_entry_at"),
        run.get("finished_at"),
        run.get("started_at"),
        run.get("created_at"),
    ]
    parsed = next((_et(value) for value in candidates if value and _et(value)), None)
    return parsed.replace(second=0, microsecond=0) if parsed else None


def _sample_from_order(run: dict[str, Any], order: dict[str, Any]) -> Sample | None:
    raw_legs = [row for row in (order.get("legs") or []) if isinstance(row, dict)]
    if len(raw_legs) != 2:
        return None
    legs: list[Leg] = []
    units: list[int] = []
    for row in raw_legs:
        filled = int(_num(row.get("filled_quantity")))
        leg = row.get("leg") if isinstance(row.get("leg"), dict) else row
        parsed = parse_option_symbol(leg.get("contract_symbol"))
        ratio = max(1, int(_num(leg.get("qty"), 1)))
        if filled <= 0 or parsed is None:
            return None
        units.append(filled // ratio)
        legs.append(
            Leg(
                contract_symbol=parsed.occ_symbol,
                action=str(leg.get("action") or "").lower(),
                ratio=ratio,
                root=parsed.root,
                expiration=f"20{parsed.expiry[:2]}-{parsed.expiry[2:4]}-{parsed.expiry[4:6]}",
                strike=parsed.strike_millis / 1000,
                right="call" if parsed.side_code == "C" else "put",
            )
        )
    if not units or min(units) <= 0 or len({leg.root for leg in legs}) != 1:
        return None
    strategy_type = str(order.get("strategy_type") or "")
    direction = _direction(strategy_type)
    start_at = _entry_start(run, order)
    if direction == "neutral" or start_at is None:
        return None
    config = run.get("config") or {}
    return Sample(
        locator_id=str(run.get("locator_id") or run.get("id") or ""),
        created_at=str(run.get("created_at") or ""),
        start_at=start_at,
        symbol=legs[0].root,
        strategy_type=strategy_type,
        direction=direction,
        units=min(units),
        stop_loss_pct=max(1.0, _num(order.get("stop_loss_pct"), _num(config.get("default_stop_loss_pct"), 20.0))),
        take_profit_pct=max(1.0, _num(order.get("take_profit_pct"), _num(config.get("default_take_profit_pct"), 20.0))),
        legs=legs,
    )


def _sample_signature(sample: Sample) -> tuple[Any, ...]:
    legs = tuple(
        sorted((leg.contract_symbol, leg.action, leg.ratio) for leg in sample.legs)
    )
    return sample.start_at.date(), sample.symbol, sample.strategy_type, legs


def load_samples(
    owner_id: str,
    since: date,
    *,
    keep_duplicates: bool = False,
) -> tuple[list[Sample], dict[str, int]]:
    with connect() as db:
        rows = db.execute(
            """
            SELECT id, locator_id, created_at, started_at, finished_at,
                   config_json, orders_json
            FROM trading_runs
            WHERE owner_id = ? AND created_at >= ? AND orders_json IS NOT NULL
            ORDER BY created_at
            """,
            (owner_id, since.isoformat()),
        ).fetchall()
    samples: list[Sample] = []
    stats = defaultdict(int)
    for raw in rows:
        run = dict(raw)
        run["config"] = _loads(run.pop("config_json", None), {})
        orders = _loads(run.pop("orders_json", None), [])
        stats["runs_with_orders"] += 1
        for order in orders if isinstance(orders, list) else []:
            if not isinstance(order, dict):
                continue
            stats["orders_seen"] += 1
            sample = _sample_from_order(run, order)
            if sample is None:
                stats["orders_excluded"] += 1
                continue
            stats["eligible_two_leg_orders"] += 1
            samples.append(sample)
    if not keep_duplicates:
        unique: dict[tuple[Any, ...], Sample] = {}
        for sample in samples:
            signature = _sample_signature(sample)
            if signature in unique:
                stats["duplicate_structures_excluded"] += 1
                continue
            unique[signature] = sample
        samples = list(unique.values())
    stats["research_samples"] = len(samples)
    return samples, dict(stats)


class ThetaHistory:
    def __init__(self) -> None:
        self.option_cache: dict[tuple[Any, ...], pd.DataFrame] = {}
        self.stock_cache: dict[tuple[str, date], pd.DataFrame] = {}

    def option(self, leg: Leg, day: date) -> pd.DataFrame:
        key = (leg.root, leg.expiration, leg.strike, leg.right, day)
        if key not in self.option_cache:
            frame = _with_session_retry(
                lambda client: client.option_history_quote(
                    leg.root,
                    expiration=leg.expiration,
                    interval="1m",
                    date=day,
                    strike=str(leg.strike),
                    right=leg.right,
                    start_time="09:30:00",
                    end_time="16:00:00",
                )
            )
            self.option_cache[key] = _normalize_option_frame(frame)
        return self.option_cache[key]

    def stock(self, symbol: str, day: date) -> pd.DataFrame:
        key = (symbol, day)
        if key not in self.stock_cache:
            frame = _with_session_retry(
                lambda client: client.stock_history_ohlc(symbol, date=day, interval="1m")
            )
            self.stock_cache[key] = _normalize_stock_frame(frame)
        return self.stock_cache[key]


def _normalize_option_frame(frame: Any) -> pd.DataFrame:
    if frame is None or not hasattr(frame, "copy"):
        return pd.DataFrame(columns=["timestamp", "bid", "ask"])
    result = frame.copy()
    required = {"timestamp", "bid", "ask"}
    if not required.issubset(result.columns):
        return pd.DataFrame(columns=["timestamp", "bid", "ask"])
    result = result[["timestamp", "bid", "ask"]]
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True).dt.tz_convert(EASTERN)
    result["bid"] = pd.to_numeric(result["bid"], errors="coerce")
    result["ask"] = pd.to_numeric(result["ask"], errors="coerce")
    result = result[(result.bid > 0) & (result.ask > 0) & (result.ask >= result.bid)]
    return result.drop_duplicates("timestamp", keep="last").sort_values("timestamp")


def _normalize_stock_frame(frame: Any) -> pd.DataFrame:
    if frame is None or not hasattr(frame, "copy"):
        return pd.DataFrame()
    result = frame.copy()
    if "timestamp" not in result.columns or "close" not in result.columns:
        return pd.DataFrame()
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True).dt.tz_convert(EASTERN)
    for key in ("open", "high", "low", "close", "volume", "vwap"):
        if key in result.columns:
            result[key] = pd.to_numeric(result[key], errors="coerce")
    result = result[result.close > 0].drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    result["ema9"] = result.close.ewm(span=9, adjust=False).mean()
    result["ema20"] = result.close.ewm(span=20, adjust=False).mean()
    if "vwap" not in result.columns or not (result.vwap > 0).any():
        volume = result.get("volume", pd.Series(1.0, index=result.index)).fillna(0).clip(lower=0)
        typical = result[[key for key in ("high", "low", "close") if key in result.columns]].mean(axis=1)
        cumulative_volume = volume.cumsum().replace(0, math.nan)
        result["vwap"] = (typical * volume).cumsum() / cumulative_volume
    result["vwap"] = result.vwap.ffill()
    return result


def strategy_frame(sample: Sample, history: ThetaHistory) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    day = sample.start_at.date()
    for index, leg in enumerate(sample.legs):
        frame = history.option(leg, day).rename(columns={"bid": f"bid_{index}", "ask": f"ask_{index}"})
        frames.append(frame)
    if any(frame.empty for frame in frames):
        return pd.DataFrame()
    merged = frames[0]
    for frame in frames[1:]:
        merged = merged.merge(frame, on="timestamp", how="inner")
    return merged.sort_values("timestamp")


def _entry_cash(row: pd.Series, sample: Sample) -> float:
    cash = 0.0
    for index, leg in enumerate(sample.legs):
        price = float(row[f"ask_{index}"] if leg.action == "buy" else row[f"bid_{index}"])
        cash += (-price if leg.action == "buy" else price) * leg.ratio * 100
    return cash


def _close_cash(row: pd.Series, sample: Sample) -> float:
    cash = 0.0
    for index, leg in enumerate(sample.legs):
        price = float(row[f"bid_{index}"] if leg.action == "buy" else row[f"ask_{index}"])
        cash += (price if leg.action == "buy" else -price) * leg.ratio * 100
    return cash


def _risk_and_profit_basis(sample: Sample, entry_cash: float) -> tuple[float, float]:
    if entry_cash < 0:
        debit = abs(entry_cash)
        return debit, debit
    strikes = sorted({leg.strike for leg in sample.legs})
    width = (strikes[-1] - strikes[0]) * 100 if len(strikes) >= 2 else entry_cash
    max_loss = max(width - entry_cash, 1.0)
    return max_loss, max(entry_cash, 1.0)


def _window_end(start: datetime, max_wait_minutes: int) -> datetime:
    hard_close = datetime.combine(start.date(), time(14, 30), tzinfo=EASTERN)
    return min(start + timedelta(minutes=max_wait_minutes), hard_close)


def _direction_condition(row: pd.Series, previous: pd.Series, direction: str) -> bool:
    if any(pd.isna(row.get(key)) for key in ("close", "vwap", "ema9", "ema20")):
        return False
    if direction == "bullish":
        return row.close > row.vwap and row.ema9 > row.ema20 and row.ema9 > previous.ema9
    return row.close < row.vwap and row.ema9 < row.ema20 and row.ema9 < previous.ema9


def _entry_time_for_policy(
    sample: Sample,
    stock: pd.DataFrame,
    policy: str,
    max_wait_minutes: int,
) -> datetime | None:
    if policy in {"immediate", "friction_guard"}:
        return sample.start_at
    end = _window_end(sample.start_at, max_wait_minutes)
    if end <= sample.start_at:
        return None
    rows = stock[(stock.timestamp >= sample.start_at) & (stock.timestamp <= end)].reset_index(drop=True)
    if len(rows) < 4:
        return None
    conditions: list[bool] = []
    crossed_opposite = False
    for index, row in rows.iterrows():
        previous = rows.iloc[max(0, index - 3)]
        condition = _direction_condition(row, previous, sample.direction)
        conditions.append(condition)
        if sample.direction == "bullish" and row.close <= row.vwap:
            crossed_opposite = True
        if sample.direction == "bearish" and row.close >= row.vwap:
            crossed_opposite = True
        required = 3 if policy == "trend_confirm" else 2
        if len(conditions) < required or not all(conditions[-required:]):
            continue
        if policy == "vwap_reclaim" and not crossed_opposite:
            continue
        return row.timestamp.to_pydatetime()
    return None


def _row_at_or_after(frame: pd.DataFrame, moment: datetime) -> pd.Series | None:
    rows = frame[frame.timestamp >= moment]
    return None if rows.empty else rows.iloc[0]


def replay_sample(
    sample: Sample,
    history: ThetaHistory,
    policy: str,
    friction_limit: float,
    max_wait_minutes: int,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> ReplayResult:
    empty = {
        "locator_id": sample.locator_id,
        "symbol": sample.symbol,
        "strategy_type": sample.strategy_type,
        "direction": sample.direction,
        "policy": policy,
        "candidate_at": sample.start_at.isoformat(),
    }
    try:
        options = strategy_frame(sample, history)
        stock = history.stock(sample.symbol, sample.start_at.date())
    except Exception as exc:  # noqa: BLE001 - research records data failures per sample.
        return ReplayResult(**empty, entered=False, skipped_reason=f"data_error:{exc}", entry_at=None, wait_minutes=None, entry_value=None, friction_loss=None, friction_to_stop=None, stop_threshold=None, take_profit_threshold=None, exit_at=None, exit_reason=None, pnl_per_unit=None, return_on_risk_pct=None, mfe_per_unit=None, mae_per_unit=None, stopped_then_profitable=False)
    if options.empty or stock.empty:
        return ReplayResult(**empty, entered=False, skipped_reason="missing_history", entry_at=None, wait_minutes=None, entry_value=None, friction_loss=None, friction_to_stop=None, stop_threshold=None, take_profit_threshold=None, exit_at=None, exit_reason=None, pnl_per_unit=None, return_on_risk_pct=None, mfe_per_unit=None, mae_per_unit=None, stopped_then_profitable=False)
    entry_time = _entry_time_for_policy(sample, stock, policy, max_wait_minutes)
    if entry_time is None:
        return ReplayResult(**empty, entered=False, skipped_reason="trigger_not_confirmed", entry_at=None, wait_minutes=None, entry_value=None, friction_loss=None, friction_to_stop=None, stop_threshold=None, take_profit_threshold=None, exit_at=None, exit_reason=None, pnl_per_unit=None, return_on_risk_pct=None, mfe_per_unit=None, mae_per_unit=None, stopped_then_profitable=False)
    entry_row = _row_at_or_after(options, entry_time)
    if entry_row is None:
        return ReplayResult(**empty, entered=False, skipped_reason="entry_quote_unavailable", entry_at=None, wait_minutes=None, entry_value=None, friction_loss=None, friction_to_stop=None, stop_threshold=None, take_profit_threshold=None, exit_at=None, exit_reason=None, pnl_per_unit=None, return_on_risk_pct=None, mfe_per_unit=None, mae_per_unit=None, stopped_then_profitable=False)
    actual_entry_time = entry_row.timestamp.to_pydatetime()
    entry_cash = _entry_cash(entry_row, sample)
    risk_basis, profit_basis = _risk_and_profit_basis(sample, entry_cash)
    stop_pct = sample.stop_loss_pct if stop_loss_pct is None else stop_loss_pct
    profit_pct = sample.take_profit_pct if take_profit_pct is None else take_profit_pct
    stop = -risk_basis * stop_pct / 100
    take_profit = profit_basis * profit_pct / 100
    friction = entry_cash + _close_cash(entry_row, sample)
    friction_ratio = abs(friction) / abs(stop) if stop else math.inf
    if policy != "immediate" and friction_ratio > friction_limit:
        return ReplayResult(**empty, entered=False, skipped_reason="friction_exceeds_stop_budget", entry_at=actual_entry_time.isoformat(), wait_minutes=round((actual_entry_time - sample.start_at).total_seconds() / 60, 2), entry_value=round(entry_cash, 2), friction_loss=round(friction, 2), friction_to_stop=round(friction_ratio, 3), stop_threshold=round(stop, 2), take_profit_threshold=round(take_profit, 2), exit_at=None, exit_reason=None, pnl_per_unit=None, return_on_risk_pct=None, mfe_per_unit=None, mae_per_unit=None, stopped_then_profitable=False)

    close_at = datetime.combine(sample.start_at.date(), time(15, 50), tzinfo=EASTERN)
    path = options[(options.timestamp >= actual_entry_time) & (options.timestamp <= close_at)].copy()
    if path.empty:
        return ReplayResult(**empty, entered=False, skipped_reason="empty_post_entry_path", entry_at=actual_entry_time.isoformat(), wait_minutes=None, entry_value=round(entry_cash, 2), friction_loss=round(friction, 2), friction_to_stop=round(friction_ratio, 3), stop_threshold=round(stop, 2), take_profit_threshold=round(take_profit, 2), exit_at=None, exit_reason=None, pnl_per_unit=None, return_on_risk_pct=None, mfe_per_unit=None, mae_per_unit=None, stopped_then_profitable=False)
    path["pnl"] = path.apply(lambda row: entry_cash + _close_cash(row, sample), axis=1)
    exit_row = None
    exit_reason = "time_exit"
    for _, row in path.iterrows():
        if row.pnl <= stop:
            exit_row = row
            exit_reason = "stop"
            break
        if row.pnl >= take_profit:
            exit_row = row
            exit_reason = "take_profit"
            break
    if exit_row is None:
        exit_row = path.iloc[-1]
    exit_index = path.index.get_loc(exit_row.name)
    remaining = path.iloc[exit_index + 1 :]
    stopped_then_profitable = bool(exit_reason == "stop" and not remaining.empty and remaining.pnl.max() > 0)
    pnl = float(exit_row.pnl)
    return ReplayResult(
        **empty,
        entered=True,
        skipped_reason=None,
        entry_at=actual_entry_time.isoformat(),
        wait_minutes=round((actual_entry_time - sample.start_at).total_seconds() / 60, 2),
        entry_value=round(entry_cash, 2),
        friction_loss=round(friction, 2),
        friction_to_stop=round(friction_ratio, 3),
        stop_threshold=round(stop, 2),
        take_profit_threshold=round(take_profit, 2),
        exit_at=exit_row.timestamp.isoformat(),
        exit_reason=exit_reason,
        pnl_per_unit=round(pnl, 2),
        return_on_risk_pct=round(pnl / risk_basis * 100, 2) if risk_basis else None,
        mfe_per_unit=round(float(path.pnl.max()), 2),
        mae_per_unit=round(float(path.pnl.min()), 2),
        stopped_then_profitable=stopped_then_profitable,
    )


def _max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return round(drawdown, 2)


def summarize(results: list[ReplayResult]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for policy in POLICIES:
        rows = [row for row in results if row.policy == policy]
        entered = [row for row in rows if row.entered and row.pnl_per_unit is not None]
        pnls = [float(row.pnl_per_unit or 0) for row in entered]
        returns = [float(row.return_on_risk_pct or 0) for row in entered]
        summary[policy] = {
            "candidates": len(rows),
            "entered": len(entered),
            "skipped": len(rows) - len(entered),
            "wins": sum(value > 0 for value in pnls),
            "losses": sum(value < 0 for value in pnls),
            "win_rate_pct": round(sum(value > 0 for value in pnls) / len(pnls) * 100, 2) if pnls else None,
            "total_pnl_per_unit": round(sum(pnls), 2),
            "avg_pnl_per_unit": round(statistics.mean(pnls), 2) if pnls else None,
            "median_pnl_per_unit": round(statistics.median(pnls), 2) if pnls else None,
            "avg_return_on_risk_pct": round(statistics.mean(returns), 2) if returns else None,
            "max_drawdown_per_unit": _max_drawdown(pnls),
            "stops": sum(row.exit_reason == "stop" for row in entered),
            "take_profits": sum(row.exit_reason == "take_profit" for row in entered),
            "time_exits": sum(row.exit_reason == "time_exit" for row in entered),
            "stopped_then_profitable": sum(row.stopped_then_profitable for row in entered),
            "avg_wait_minutes": round(statistics.mean(float(row.wait_minutes or 0) for row in entered), 2) if entered else None,
            "skip_reasons": _counts(row.skipped_reason for row in rows if not row.entered),
        }
    return summary


def sensitivity_matrix(samples: list[Sample], history: ThetaHistory) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for friction_limit in (0.30, 0.40, 0.50, 0.60):
        for wait_minutes in (30, 60, 90):
            results = [
                replay_sample(sample, history, "vwap_reclaim", friction_limit, wait_minutes)
                for sample in samples
            ]
            entered = [row for row in results if row.entered and row.pnl_per_unit is not None]
            pnls = [float(row.pnl_per_unit or 0) for row in entered]
            rows.append(
                {
                    "friction_limit": friction_limit,
                    "max_wait_minutes": wait_minutes,
                    "entered": len(entered),
                    "wins": sum(value > 0 for value in pnls),
                    "losses": sum(value < 0 for value in pnls),
                    "total_pnl_per_unit": round(sum(pnls), 2),
                    "avg_pnl_per_unit": round(statistics.mean(pnls), 2) if pnls else None,
                    "max_drawdown_per_unit": _max_drawdown(pnls),
                }
            )
    return rows


def stop_sensitivity(samples: list[Sample], history: ThetaHistory) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for policy, friction_limit, wait_minutes in (
        ("immediate", 0.30, 60),
        ("vwap_reclaim", 0.30, 60),
    ):
        for stop_pct in (20, 25, 30, 35, 40):
            results = [
                replay_sample(
                    sample,
                    history,
                    policy,
                    friction_limit,
                    wait_minutes,
                    stop_loss_pct=stop_pct,
                    take_profit_pct=20,
                )
                for sample in samples
            ]
            entered = [row for row in results if row.entered and row.pnl_per_unit is not None]
            pnls = [float(row.pnl_per_unit or 0) for row in entered]
            rows.append(
                {
                    "policy": policy,
                    "stop_loss_pct": stop_pct,
                    "take_profit_pct": 20,
                    "entered": len(entered),
                    "wins": sum(value > 0 for value in pnls),
                    "stops": sum(row.exit_reason == "stop" for row in entered),
                    "stopped_then_profitable": sum(row.stopped_then_profitable for row in entered),
                    "total_pnl_per_unit": round(sum(pnls), 2),
                    "avg_return_on_risk_pct": round(
                        statistics.mean(float(row.return_on_risk_pct or 0) for row in entered), 2
                    ) if entered else None,
                    "max_drawdown_per_unit": _max_drawdown(pnls),
                }
            )
    return rows


def period_summary(results: list[ReplayResult], split_date: date = date(2026, 7, 1)) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for label, predicate in (
        ("before_split", lambda day: day < split_date),
        ("on_or_after_split", lambda day: day >= split_date),
    ):
        output[label] = {}
        for policy in POLICIES:
            rows = [
                row for row in results
                if row.policy == policy
                and row.entered
                and row.pnl_per_unit is not None
                and predicate(datetime.fromisoformat(row.candidate_at).date())
            ]
            pnls = [float(row.pnl_per_unit or 0) for row in rows]
            output[label][policy] = {
                "entered": len(rows),
                "wins": sum(value > 0 for value in pnls),
                "total_pnl_per_unit": round(sum(pnls), 2),
                "max_drawdown_per_unit": _max_drawdown(pnls),
            }
    output["split_date"] = split_date.isoformat()
    return output


def _counts(values: Any) -> dict[str, int]:
    output: dict[str, int] = defaultdict(int)
    for value in values:
        output[str(value or "unknown")] += 1
    return dict(sorted(output.items()))


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Adaptive Entry Replay",
        "",
        f"- Owner: `{payload['owner_id']}`",
        f"- Since: `{payload['since']}`",
        f"- Eligible two-leg samples: **{payload['sample_count']}**",
        f"- Friction limit: **{payload['friction_limit']:.0%} of stop budget**",
        f"- Confirmation window: **{payload['max_wait_minutes']} minutes**, no new confirmation after 14:30 ET",
        "",
        "## Summary",
        "",
        "| Policy | Entered | Skipped | Win rate | Total PnL/unit | Avg return/risk | Max drawdown | Stops then profitable | Avg wait |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "immediate": "Immediate",
        "friction_guard": "Immediate + friction gate",
        "trend_confirm": "3m trend confirmation",
        "vwap_reclaim": "VWAP reclaim/reject",
    }
    for policy in POLICIES:
        row = payload["summary"][policy]
        lines.append(
            f"| {labels[policy]} | {row['entered']} | {row['skipped']} | {_fmt(row['win_rate_pct'], '%')} | "
            f"{_fmt(row['total_pnl_per_unit'], '$')} | {_fmt(row['avg_return_on_risk_pct'], '%')} | "
            f"{_fmt(row['max_drawdown_per_unit'], '$')} | {row['stopped_then_profitable']} | {_fmt(row['avg_wait_minutes'], 'm')} |"
        )
    lines.extend([
        "",
        "## VWAP Sensitivity",
        "",
        "| Friction/stop cap | Wait window | Entered | Wins | Total PnL/unit | Max drawdown |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for row in payload["sensitivity"]:
        lines.append(
            f"| {row['friction_limit']:.0%} | {row['max_wait_minutes']}m | {row['entered']} | {row['wins']} | "
            f"{_fmt(row['total_pnl_per_unit'], '$')} | {_fmt(row['max_drawdown_per_unit'], '$')} |"
        )
    lines.extend([
        "",
        "## Stop Sensitivity",
        "",
        "Take profit remains 20%. Adaptive rows use a 30% friction cap and 60-minute wait.",
        "",
        "| Policy | Stop | Entered | Wins | Stops | Stops then profitable | Total PnL/unit | Max drawdown |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in payload["stop_sensitivity"]:
        lines.append(
            f"| {row['policy']} | {row['stop_loss_pct']}% | {row['entered']} | {row['wins']} | "
            f"{row['stops']} | {row['stopped_then_profitable']} | "
            f"{_fmt(row['total_pnl_per_unit'], '$')} | {_fmt(row['max_drawdown_per_unit'], '$')} |"
        )
    lines.extend(["", "## Time Split", "", f"Split date: `{payload['period_summary']['split_date']}`", ""])
    for period in ("before_split", "on_or_after_split"):
        row = payload["period_summary"][period]["vwap_reclaim"]
        lines.append(
            f"- `{period}`: entered {row['entered']}, wins {row['wins']}, "
            f"PnL/unit {_fmt(row['total_pnl_per_unit'], '$')}, drawdown {_fmt(row['max_drawdown_per_unit'], '$')}."
        )
    lines.extend(["", "## Sample Detail", "", "| Run | Candidate | Strategy | Policy | Entry | Exit | PnL/unit | MFE | MAE | Friction/stop |", "|---|---|---|---|---|---|---:|---:|---:|---:|"])
    for row in payload["results"]:
        entry = row["entry_at"][11:16] if row.get("entry_at") else f"skip:{row.get('skipped_reason')}"
        exit_text = f"{row['exit_reason']} {row['exit_at'][11:16]}" if row.get("exit_at") else "--"
        lines.append(
            f"| {row['locator_id']} | {row['candidate_at'][11:16]} | {row['symbol']} {row['strategy_type']} | {row['policy']} | "
            f"{entry} | {exit_text} | {_fmt(row.get('pnl_per_unit'), '$')} | {_fmt(row.get('mfe_per_unit'), '$')} | "
            f"{_fmt(row.get('mae_per_unit'), '$')} | {_fmt((row.get('friction_to_stop') or 0) * 100 if row.get('friction_to_stop') is not None else None, '%')} |"
        )
    lines.extend(["", "## Notes", "", "- Entry and exit prices use executable option NBBO sides, not midpoint.", "- Confirmation indicators use only underlying bars available at or before the decision minute.", "- Results are normalized to one strategy unit and exclude commissions and broker-specific fill improvement/slippage.", "- This is a timing-policy comparison on historically selected strategies, not an independent strategy-selection backtest."])
    return "\n".join(lines) + "\n"


def _fmt(value: Any, suffix: str) -> str:
    if value is None:
        return "--"
    number = float(value)
    if suffix == "$":
        return f"${number:.2f}"
    return f"{number:.2f}{suffix}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--since", default=(date.today() - timedelta(days=60)).isoformat())
    parser.add_argument("--friction-limit", type=float, default=0.40)
    parser.add_argument("--max-wait-minutes", type=int, default=90)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--keep-duplicates", action="store_true")
    args = parser.parse_args()

    samples, extraction = load_samples(
        args.owner,
        date.fromisoformat(args.since),
        keep_duplicates=args.keep_duplicates,
    )
    history = ThetaHistory()
    results: list[ReplayResult] = []
    for sample in samples:
        for policy in POLICIES:
            results.append(replay_sample(sample, history, policy, args.friction_limit, args.max_wait_minutes))
    payload = {
        "generated_at": datetime.now(tz=EASTERN).isoformat(),
        "owner_id": args.owner,
        "since": args.since,
        "sample_count": len(samples),
        "extraction": extraction,
        "friction_limit": args.friction_limit,
        "max_wait_minutes": args.max_wait_minutes,
        "summary": summarize(results),
        "sensitivity": sensitivity_matrix(samples, history),
        "stop_sensitivity": stop_sensitivity(samples, history),
        "period_summary": period_summary(results),
        "samples": [{**asdict(sample), "start_at": sample.start_at.isoformat()} for sample in samples],
        "results": [asdict(result) for result in results],
    }
    markdown = render_markdown(payload)
    if args.json_output:
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.write_text(markdown, encoding="utf-8")
    print(json.dumps({"sample_count": len(samples), "extraction": extraction, "summary": payload["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

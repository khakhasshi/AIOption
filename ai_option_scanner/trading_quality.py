from __future__ import annotations

from typing import Any

from .account_store import normalize_owner_id
from .trading_store import list_trading_runs


CONFIDENCE_BUCKETS = (
    ("high", "高信心", 70.0, 101.0),
    ("mid", "中信心", 40.0, 70.0),
    ("low", "低信心", 0.0, 40.0),
)


def ai_decision_quality(owner_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    runs = list_trading_runs(normalize_owner_id(owner_id), limit, summary=False)
    rows = []
    for run in runs:
        instance = run.get("trade_instance") or {}
        metrics = instance.get("review_metrics") or {}
        confidence = _number_or_none(metrics.get("ai_confidence_avg"))
        return_pct = _number_or_none(metrics.get("return_pct"))
        pnl = _number_or_none(metrics.get("estimated_total_pnl"))
        if confidence is None or return_pct is None:
            continue
        rows.append(
            {
                "run_id": run.get("id"),
                "created_at": run.get("created_at"),
                "lifecycle_state": instance.get("lifecycle_state"),
                "confidence": confidence,
                "return_pct": return_pct,
                "estimated_total_pnl": pnl,
                "win_loss": metrics.get("win_loss"),
                "selection_count": (instance.get("ai_decision") or {}).get("selection_count"),
            }
        )

    buckets = []
    for key, label, lower, upper in CONFIDENCE_BUCKETS:
        bucket_rows = [row for row in rows if lower <= row["confidence"] < upper]
        buckets.append(_quality_bucket(key, label, bucket_rows))

    return {
        "limit": max(1, min(int(limit or 50), 100)),
        "sample_size": len(rows),
        "avg_confidence": _avg(row["confidence"] for row in rows),
        "avg_return_pct": _avg(row["return_pct"] for row in rows),
        "avg_confidence_vs_return": _avg(row["return_pct"] - row["confidence"] for row in rows),
        "win_rate": _win_rate(rows),
        "buckets": buckets,
        "recent": rows[:8],
    }


def _quality_bucket(key: str, label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "count": len(rows),
        "avg_confidence": _avg(row["confidence"] for row in rows),
        "avg_return_pct": _avg(row["return_pct"] for row in rows),
        "win_rate": _win_rate(rows),
    }


def _win_rate(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    wins = sum(1 for row in rows if float(row.get("estimated_total_pnl") or 0) > 0 or row.get("win_loss") == "win")
    return round(wins / len(rows) * 100, 2)


def _avg(values: Any) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 2)


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number

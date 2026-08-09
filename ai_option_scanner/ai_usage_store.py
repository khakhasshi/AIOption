from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .account_store import LOCAL_OWNER_ID, normalize_owner_id, utc_now
from .db import connect, ensure_column, run_db_init_once
from .time_utils import to_et_iso


DEEPSEEK_PRICE_PER_M_TOKENS: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {
        "input_cache_hit_cny": 0.02,
        "input_cache_miss_cny": 1.0,
        "output_cny": 2.0,
        "input_cache_hit_usd": 0.0028,
        "input_cache_miss_usd": 0.14,
        "output_usd": 0.28,
    },
    "deepseek-chat": {
        "input_cache_hit_cny": 0.5,
        "input_cache_miss_cny": 2.0,
        "output_cny": 8.0,
        "input_cache_hit_usd": 0.07,
        "input_cache_miss_usd": 0.28,
        "output_usd": 1.10,
    },
    "deepseek-reasoner": {
        "input_cache_hit_cny": 0.5,
        "input_cache_miss_cny": 2.0,
        "output_cny": 8.0,
        "input_cache_hit_usd": 0.07,
        "input_cache_miss_usd": 0.28,
        "output_usd": 1.10,
    },
}
DEFAULT_PRICE = DEEPSEEK_PRICE_PER_M_TOKENS["deepseek-v4-flash"]


def init_ai_usage_db() -> None:
    run_db_init_once("ai_usage_store", _init_ai_usage_db)


def _init_ai_usage_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_usage_events (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                provider_type TEXT NOT NULL DEFAULT 'openai',
                source_type TEXT,
                source_id TEXT,
                scan_id TEXT,
                scan_loop_instance_id TEXT,
                symbol TEXT,
                request_role TEXT,
                council_mode INTEGER NOT NULL DEFAULT 0,
                radar_scan INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'succeeded',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_cny REAL NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0,
                price_snapshot_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        for column, declaration in {
            "provider_type": "TEXT NOT NULL DEFAULT 'openai'",
            "source_type": "TEXT",
            "source_id": "TEXT",
            "scan_id": "TEXT",
            "scan_loop_instance_id": "TEXT",
            "symbol": "TEXT",
            "request_role": "TEXT",
            "council_mode": "INTEGER NOT NULL DEFAULT 0",
            "radar_scan": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'succeeded'",
            "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
            "prompt_cache_hit_tokens": "INTEGER NOT NULL DEFAULT 0",
            "prompt_cache_miss_tokens": "INTEGER NOT NULL DEFAULT 0",
            "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
            "total_tokens": "INTEGER NOT NULL DEFAULT 0",
            "estimated_cost_cny": "REAL NOT NULL DEFAULT 0",
            "estimated_cost_usd": "REAL NOT NULL DEFAULT 0",
            "price_snapshot_json": "TEXT",
            "error": "TEXT",
            "created_at": "TEXT",
        }.items():
            ensure_column(db, "ai_usage_events", column, declaration)
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_owner_created ON ai_usage_events(owner_id, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage_events(created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_scan ON ai_usage_events(scan_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_scan_loop ON ai_usage_events(scan_loop_instance_id)")


def record_ai_usage_event(
    *,
    owner_id: str | None,
    provider: str,
    model: str,
    provider_type: str = "openai",
    usage: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    status: str = "succeeded",
    error: str | None = None,
) -> dict[str, Any]:
    init_ai_usage_db()
    normalized_usage = _normalize_usage(usage or {})
    price_snapshot = _price_for(provider, model)
    costs = estimate_ai_cost(provider=provider, model=model, usage=normalized_usage)
    event = {
        "id": uuid.uuid4().hex,
        "owner_id": normalize_owner_id(owner_id or LOCAL_OWNER_ID),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "provider_type": str(provider_type or "openai"),
        "source_type": str((context or {}).get("source_type") or ""),
        "source_id": str((context or {}).get("source_id") or ""),
        "scan_id": str((context or {}).get("scan_id") or ""),
        "scan_loop_instance_id": str((context or {}).get("scan_loop_instance_id") or ""),
        "symbol": str((context or {}).get("symbol") or "").upper(),
        "request_role": str((context or {}).get("request_role") or ""),
        "council_mode": bool((context or {}).get("council_mode")),
        "radar_scan": bool((context or {}).get("radar_scan")),
        "status": status,
        "prompt_tokens": normalized_usage["prompt_tokens"],
        "prompt_cache_hit_tokens": normalized_usage["prompt_cache_hit_tokens"],
        "prompt_cache_miss_tokens": normalized_usage["prompt_cache_miss_tokens"],
        "completion_tokens": normalized_usage["completion_tokens"],
        "reasoning_tokens": normalized_usage["reasoning_tokens"],
        "total_tokens": normalized_usage["total_tokens"],
        "estimated_cost_cny": costs["estimated_cost_cny"],
        "estimated_cost_usd": costs["estimated_cost_usd"],
        "price_snapshot": price_snapshot,
        "error": error or "",
        "created_at": utc_now(),
    }
    with connect() as db:
        db.execute(
            """
            INSERT INTO ai_usage_events (
                id, owner_id, provider, model, provider_type, source_type, source_id,
                scan_id, scan_loop_instance_id, symbol, request_role, council_mode, radar_scan,
                status, prompt_tokens, prompt_cache_hit_tokens, prompt_cache_miss_tokens,
                completion_tokens, reasoning_tokens, total_tokens, estimated_cost_cny,
                estimated_cost_usd, price_snapshot_json, error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                event["owner_id"],
                event["provider"],
                event["model"],
                event["provider_type"],
                event["source_type"],
                event["source_id"],
                event["scan_id"],
                event["scan_loop_instance_id"],
                event["symbol"],
                event["request_role"],
                1 if event["council_mode"] else 0,
                1 if event["radar_scan"] else 0,
                event["status"],
                event["prompt_tokens"],
                event["prompt_cache_hit_tokens"],
                event["prompt_cache_miss_tokens"],
                event["completion_tokens"],
                event["reasoning_tokens"],
                event["total_tokens"],
                event["estimated_cost_cny"],
                event["estimated_cost_usd"],
                json.dumps(price_snapshot, ensure_ascii=False),
                event["error"],
                event["created_at"],
            ),
        )
    return event


def estimate_ai_cost(*, provider: str, model: str, usage: dict[str, Any]) -> dict[str, float]:
    price = _price_for(provider, model)
    hit_tokens = _int(usage.get("prompt_cache_hit_tokens"))
    miss_tokens = _int(usage.get("prompt_cache_miss_tokens"))
    prompt_tokens = _int(usage.get("prompt_tokens"))
    completion_tokens = _int(usage.get("completion_tokens"))
    if not hit_tokens and not miss_tokens:
        miss_tokens = prompt_tokens
    cost_cny = (
        hit_tokens / 1_000_000 * float(price["input_cache_hit_cny"])
        + miss_tokens / 1_000_000 * float(price["input_cache_miss_cny"])
        + completion_tokens / 1_000_000 * float(price["output_cny"])
    )
    cost_usd = (
        hit_tokens / 1_000_000 * float(price["input_cache_hit_usd"])
        + miss_tokens / 1_000_000 * float(price["input_cache_miss_usd"])
        + completion_tokens / 1_000_000 * float(price["output_usd"])
    )
    return {"estimated_cost_cny": round(cost_cny, 8), "estimated_cost_usd": round(cost_usd, 8)}


def ai_usage_summary(owner_id: str | None = None, *, days: int = 30, limit: int = 80) -> dict[str, Any]:
    init_ai_usage_db()
    safe_days = max(1, min(int(days or 30), 366))
    since = (datetime.now(timezone.utc) - timedelta(days=safe_days)).isoformat()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    month = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    owner_clause = ""
    params: list[Any] = [since]
    owner = normalize_owner_id(owner_id) if owner_id else None
    if owner:
        owner_clause = "AND owner_id = ?"
        params.append(owner)
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT * FROM ai_usage_events
            WHERE created_at >= ? {owner_clause}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [*params, max(1, min(int(limit or 80), 500))],
        ).fetchall()
        aggregate_rows = db.execute(
            f"""
            SELECT provider, model, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens,
                   SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,
                   SUM(estimated_cost_cny) AS cost_cny, SUM(estimated_cost_usd) AS cost_usd
            FROM ai_usage_events
            WHERE created_at >= ? {owner_clause}
            GROUP BY provider, model
            ORDER BY cost_cny DESC
            """,
            params,
        ).fetchall()
        daily_rows = db.execute(
            f"""
            SELECT SUBSTR(created_at, 1, 10) AS day, COUNT(*) AS calls, SUM(total_tokens) AS total_tokens,
                   SUM(estimated_cost_cny) AS cost_cny, SUM(estimated_cost_usd) AS cost_usd
            FROM ai_usage_events
            WHERE created_at >= ? {owner_clause}
            GROUP BY SUBSTR(created_at, 1, 10)
            ORDER BY day DESC
            """,
            params,
        ).fetchall()
        totals = {
            "today": _aggregate_period(db, today, owner),
            "week": _aggregate_period(db, week, owner),
            "month": _aggregate_period(db, month, owner),
            f"{safe_days}d": _aggregate_period(db, since, owner),
        }
    return {
        "owner_id": owner or "",
        "days": safe_days,
        "totals": totals,
        "by_model": [_aggregate_row(row) for row in aggregate_rows],
        "daily": [_daily_row(row) for row in daily_rows],
        "recent": [_event_row(row) for row in rows],
        "price_table": DEEPSEEK_PRICE_PER_M_TOKENS,
        "balance": deepseek_balance(),
    }


def deepseek_balance() -> dict[str, Any]:
    import urllib.error
    import urllib.request

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return {"available": False, "error": "DEEPSEEK_API_KEY is not configured"}
    request = urllib.request.Request(
        f"{os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')}/user/balance",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"available": False, "error": f"HTTP {exc.code}: {exc.reason}"}
    except Exception as exc:
        return {"available": False, "error": str(exc)}
    return {"available": True, **data}


def _aggregate_period(db: Any, since: str, owner: str | None) -> dict[str, Any]:
    owner_clause = ""
    params: list[Any] = [since]
    if owner:
        owner_clause = "AND owner_id = ?"
        params.append(owner)
    row = db.execute(
        f"""
        SELECT COUNT(*) AS calls, SUM(total_tokens) AS total_tokens, SUM(prompt_tokens) AS prompt_tokens,
               SUM(prompt_cache_hit_tokens) AS cache_hit_tokens, SUM(prompt_cache_miss_tokens) AS cache_miss_tokens,
               SUM(completion_tokens) AS completion_tokens, SUM(reasoning_tokens) AS reasoning_tokens,
               SUM(estimated_cost_cny) AS cost_cny, SUM(estimated_cost_usd) AS cost_usd
        FROM ai_usage_events
        WHERE created_at >= ? {owner_clause}
        """,
        params,
    ).fetchone()
    return _period_row(row)


def _normalize_usage(usage: dict[str, Any]) -> dict[str, int]:
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    prompt_tokens = _int(usage.get("prompt_tokens"))
    completion_tokens = _int(usage.get("completion_tokens"))
    total_tokens = _int(usage.get("total_tokens")) or prompt_tokens + completion_tokens
    cache_hit_tokens = _int(usage.get("prompt_cache_hit_tokens") or prompt_details.get("cached_tokens"))
    cache_miss_tokens = _int(usage.get("prompt_cache_miss_tokens"))
    if prompt_tokens and cache_hit_tokens and not cache_miss_tokens:
        cache_miss_tokens = max(prompt_tokens - cache_hit_tokens, 0)
    return {
        "prompt_tokens": prompt_tokens,
        "prompt_cache_hit_tokens": cache_hit_tokens,
        "prompt_cache_miss_tokens": cache_miss_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": _int(usage.get("reasoning_tokens") or details.get("reasoning_tokens")),
        "total_tokens": total_tokens,
    }


def _price_for(provider: str, model: str) -> dict[str, float]:
    provider_name = str(provider or "").lower()
    model_name = str(model or "").lower()
    if "deepseek" in provider_name or "deepseek" in model_name:
        return dict(DEEPSEEK_PRICE_PER_M_TOKENS.get(model_name, DEFAULT_PRICE))
    return dict(DEFAULT_PRICE)


def _period_row(row: Any) -> dict[str, Any]:
    return {
        "calls": _row_int(row, "calls"),
        "total_tokens": _row_int(row, "total_tokens"),
        "prompt_tokens": _row_int(row, "prompt_tokens"),
        "cache_hit_tokens": _row_int(row, "cache_hit_tokens"),
        "cache_miss_tokens": _row_int(row, "cache_miss_tokens"),
        "completion_tokens": _row_int(row, "completion_tokens"),
        "reasoning_tokens": _row_int(row, "reasoning_tokens"),
        "estimated_cost_cny": round(_row_float(row, "cost_cny"), 6),
        "estimated_cost_usd": round(_row_float(row, "cost_usd"), 6),
    }


def _aggregate_row(row: Any) -> dict[str, Any]:
    return {
        "provider": _row_str(row, "provider"),
        "model": _row_str(row, "model"),
        "calls": _row_int(row, "calls"),
        "total_tokens": _row_int(row, "total_tokens"),
        "prompt_tokens": _row_int(row, "prompt_tokens"),
        "completion_tokens": _row_int(row, "completion_tokens"),
        "estimated_cost_cny": round(_row_float(row, "cost_cny"), 6),
        "estimated_cost_usd": round(_row_float(row, "cost_usd"), 6),
    }


def _daily_row(row: Any) -> dict[str, Any]:
    return {
        "day": _row_str(row, "day"),
        "calls": _row_int(row, "calls"),
        "total_tokens": _row_int(row, "total_tokens"),
        "estimated_cost_cny": round(_row_float(row, "cost_cny"), 6),
        "estimated_cost_usd": round(_row_float(row, "cost_usd"), 6),
    }


def _event_row(row: Any) -> dict[str, Any]:
    return {
        "id": _row_str(row, "id"),
        "owner_id": _row_str(row, "owner_id"),
        "provider": _row_str(row, "provider"),
        "model": _row_str(row, "model"),
        "provider_type": _row_str(row, "provider_type"),
        "source_type": _row_str(row, "source_type"),
        "source_id": _row_str(row, "source_id"),
        "scan_id": _row_str(row, "scan_id"),
        "scan_loop_instance_id": _row_str(row, "scan_loop_instance_id"),
        "symbol": _row_str(row, "symbol"),
        "request_role": _row_str(row, "request_role"),
        "council_mode": bool(_row_int(row, "council_mode")),
        "radar_scan": bool(_row_int(row, "radar_scan")),
        "status": _row_str(row, "status"),
        "prompt_tokens": _row_int(row, "prompt_tokens"),
        "prompt_cache_hit_tokens": _row_int(row, "prompt_cache_hit_tokens"),
        "prompt_cache_miss_tokens": _row_int(row, "prompt_cache_miss_tokens"),
        "completion_tokens": _row_int(row, "completion_tokens"),
        "reasoning_tokens": _row_int(row, "reasoning_tokens"),
        "total_tokens": _row_int(row, "total_tokens"),
        "estimated_cost_cny": round(_row_float(row, "estimated_cost_cny"), 8),
        "estimated_cost_usd": round(_row_float(row, "estimated_cost_usd"), 8),
        "error": _row_str(row, "error"),
        "created_at": to_et_iso(_row_str(row, "created_at")),
    }


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key, None)


def _row_int(row: Any, key: str) -> int:
    return _int(_row_get(row, key))


def _row_float(row: Any, key: str) -> float:
    try:
        return float(_row_get(row, key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_str(row: Any, key: str) -> str:
    return str(_row_get(row, key) or "")


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0

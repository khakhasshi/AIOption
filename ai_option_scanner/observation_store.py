from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import smtplib
import time
import traceback
import uuid
from datetime import datetime, time as datetime_time, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .account_store import LOCAL_OWNER_ID, normalize_owner_id, utc_now
from .ai_client import ask_ai
from .ai_decision_guard import extract_json_object
from .db import connect, ensure_column, is_postgres, run_db_init_once
from .market_calendar import ET, MARKET_CLOSE, MARKET_OPEN, market_clock, next_nyse_trading_day, next_regular_open_after
from .scan_jobs import submit_scan
from .scan_store import get_scan_run
from .strategy_structures import normalize_strategy_modes
from .time_utils import parse_datetime, to_et_iso


DEFAULT_SYMBOLS = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "MSFT", "META"]
OPPORTUNITY_TERMINAL_STATUSES = {"take_profit_zone", "stop_loss_zone", "invalidated", "expired", "archived"}
OPPORTUNITY_ACTIVE_STATUSES = {"created", "watching_entry", "triggered", "active_reference", "tracking_reference"}
SCAN_LOOP_AI_REPORT_PROMPT = """你是 AI Option 的美股期权盘中交易台分析员。请只根据 payload 中的数据写一段中文雷达报告，禁止编造 HIRO、库存主体、真实订单或未给出的价格。

输出 JSON：
{
  "text": "完整报告文本",
  "state_label": "简短状态标签",
  "decision": "观望/追踪/触发复核/撤销剧本",
  "reuse_hint": "后续什么情况下可以复用"
}

文本风格要求：
- 保持交易台快照格式，包含：操作状态、基准/次情形/偏强/真弱、IV/Smile、HIRO、关键结构位、库存异常价位、状态标签。
- 没有的数据必须写“未接入/待量化”，不能补假数。
- 如果预筛未通过但结构没有明显变化，要强调“保持上一剧本/观望，不追单”，不要制造新交易信号。
- 如果有 demo_tracking，只能叫 demo/参考跟踪，不能说已真实成交。
- 简洁，适合 Telegram/Discord，一般 800-1400 中文字以内。
"""


def init_observation_db() -> None:
    run_db_init_once("observation_store", _init_observation_db)


def _ensure_scan_marks_trigram_indexes(db: Any) -> None:
    # Postgres-only: pg_trgm GIN indexes for the note/tags_json LIKE filters in
    # list_scan_runs_with_marks. SQLite ignores the call (LIKE '%foo%' can't use
    # b-tree indexes there anyway).
    if not is_postgres():
        return
    try:
        db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception:
        # Managed Postgres without privilege — queries still work, just unindexed.
        return
    for column, default in (("note", "''"), ("tags_json", "'[]'")):
        index_name = f"idx_scan_marks_{column}_trgm"
        try:
            db.execute(
                f"CREATE INDEX IF NOT EXISTS {index_name} "
                f"ON scan_marks USING GIN (LOWER(COALESCE({column}, {default})) gin_trgm_ops)"
            )
        except Exception:
            continue


def _init_observation_db() -> None:
    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_marks (
                owner_id TEXT NOT NULL,
                scan_id TEXT NOT NULL,
                starred INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, scan_id)
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_marks_owner_starred ON scan_marks(owner_id, starred, updated_at DESC)")
        _ensure_scan_marks_trigram_indexes(db)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_channels (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                type TEXT NOT NULL,
                label TEXT NOT NULL,
                config_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_notification_channels_owner ON notification_channels(owner_id, enabled)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_events (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                channel_id TEXT,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT
            )
            """
        )
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_events_dedupe ON notification_events(owner_id, dedupe_key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_notification_events_owner_created ON notification_events(owner_id, created_at DESC)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_delivery_logs (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                channel_id TEXT,
                channel_type TEXT NOT NULL,
                provider TEXT,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                request_preview_json TEXT NOT NULL DEFAULT '{}',
                response_summary TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_notification_delivery_logs_owner_created ON notification_delivery_logs(owner_id, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_notification_delivery_logs_channel ON notification_delivery_logs(owner_id, channel_id, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_notification_delivery_logs_event ON notification_delivery_logs(owner_id, event_id, created_at DESC)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_triggers (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                scan_id TEXT,
                locator_id TEXT,
                opportunity_id TEXT,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                notification_channel_ids_json TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                check_interval_seconds INTEGER NOT NULL DEFAULT 300,
                cooldown_seconds INTEGER NOT NULL DEFAULT 1800,
                max_trigger_count INTEGER NOT NULL DEFAULT 3,
                trigger_count INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_triggered_at TEXT,
                next_check_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                market_policy TEXT NOT NULL DEFAULT 'regular_only',
                opening_grace_minutes INTEGER NOT NULL DEFAULT 10,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_column(db, "scan_triggers", "market_policy", "TEXT NOT NULL DEFAULT 'regular_only'")
        ensure_column(db, "scan_triggers", "opening_grace_minutes", "INTEGER NOT NULL DEFAULT 10")
        ensure_column(db, "scan_triggers", "opportunity_id", "TEXT")
        ensure_column(db, "notification_channels", "last_error", "TEXT")
        ensure_column(db, "notification_channels", "last_test_at", "TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_triggers_owner_next ON scan_triggers(owner_id, enabled, next_check_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_triggers_owner_opportunity ON scan_triggers(owner_id, opportunity_id, updated_at DESC)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlists (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                symbols_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_watchlists_owner ON watchlists(owner_id, updated_at DESC)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_loop_instances (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                watchlist_id TEXT,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                symbols_snapshot_json TEXT NOT NULL,
                schedule_json TEXT NOT NULL,
                market_session TEXT NOT NULL DEFAULT 'regular',
                eod_review_enabled INTEGER NOT NULL DEFAULT 0,
                eod_run_time_et TEXT,
                weekend_review_enabled INTEGER NOT NULL DEFAULT 0,
                weekend_run_time_local TEXT,
                market_data_source TEXT NOT NULL DEFAULT 'yfinance',
                ai_provider TEXT NOT NULL DEFAULT 'deepseek',
                use_ai INTEGER NOT NULL DEFAULT 1,
                council INTEGER NOT NULL DEFAULT 1,
                analysis_modules_json TEXT NOT NULL,
                strategy_modes_json TEXT NOT NULL,
                prompt_template TEXT NOT NULL,
                prefilter_rules_json TEXT NOT NULL,
                alert_rules_json TEXT NOT NULL,
                alert_mode TEXT NOT NULL DEFAULT 'best_per_run',
                notification_channel_ids_json TEXT NOT NULL DEFAULT '[]',
                max_alerts_per_day INTEGER NOT NULL DEFAULT 5,
                max_ai_scans_per_day INTEGER NOT NULL DEFAULT 10,
                ai_scan_policy TEXT NOT NULL DEFAULT 'prefilter_matched',
                ai_scan_top_n INTEGER NOT NULL DEFAULT 3,
                symbol_cooldown_minutes INTEGER NOT NULL DEFAULT 30,
                run_timeout_seconds INTEGER NOT NULL DEFAULT 600,
                expires_at TEXT,
                last_eod_review_date TEXT,
                last_weekend_review_key TEXT,
                last_run_at TEXT,
                next_run_at TEXT,
                last_market_state TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_loop_instances_owner_status ON scan_loop_instances(owner_id, status, next_run_at)")
        ensure_column(db, "scan_loop_instances", "eod_review_enabled", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "scan_loop_instances", "eod_run_time_et", "TEXT")
        ensure_column(db, "scan_loop_instances", "weekend_review_enabled", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "scan_loop_instances", "weekend_run_time_local", "TEXT")
        ensure_column(db, "scan_loop_instances", "last_eod_review_date", "TEXT")
        ensure_column(db, "scan_loop_instances", "last_weekend_review_key", "TEXT")
        ensure_column(db, "scan_loop_instances", "ai_scan_policy", "TEXT NOT NULL DEFAULT 'prefilter_matched'")
        ensure_column(db, "scan_loop_instances", "ai_scan_top_n", "INTEGER NOT NULL DEFAULT 3")
        ensure_column(db, "scan_loop_instances", "option_data_source", "TEXT NOT NULL DEFAULT 'thetadata'")
        ensure_column(db, "scan_loop_instances", "ai_report_cache_json", "TEXT NOT NULL DEFAULT '{}'")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_loop_runs (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                watchlist_id TEXT,
                status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                scanned_count INTEGER NOT NULL DEFAULT 0,
                matched_count INTEGER NOT NULL DEFAULT 0,
                alerted_count INTEGER NOT NULL DEFAULT 0,
                market_state TEXT,
                data_freshness_json TEXT,
                summary_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_loop_runs_owner_created ON scan_loop_runs(owner_id, created_at DESC)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_loop_run_items (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                watchlist_id TEXT,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                prefilter_status TEXT,
                prefilter_result_json TEXT,
                data_timestamp TEXT,
                data_freshness TEXT,
                scan_id TEXT,
                triggered INTEGER NOT NULL DEFAULT 0,
                trigger_reasons_json TEXT NOT NULL DEFAULT '[]',
                score REAL,
                recommendation_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_scan_loop_items_run_symbol ON scan_loop_run_items(run_id, symbol)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_instances (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                scan_id TEXT,
                scan_loop_instance_id TEXT,
                watchlist_id TEXT,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                title TEXT NOT NULL,
                direction TEXT,
                strategy_structure TEXT,
                entry_reference_json TEXT NOT NULL,
                risk_plan_json TEXT NOT NULL,
                trigger_snapshot_json TEXT NOT NULL,
                gex_snapshot_json TEXT NOT NULL DEFAULT '{}',
                notification_channel_ids_json TEXT NOT NULL DEFAULT '[]',
                followup_enabled INTEGER NOT NULL DEFAULT 1,
                followup_interval_seconds INTEGER NOT NULL DEFAULT 300,
                cooldown_seconds INTEGER NOT NULL DEFAULT 1800,
                max_followup_alerts INTEGER NOT NULL DEFAULT 6,
                followup_alert_count INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                next_check_at TEXT,
                last_alert_at TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_owner_status ON opportunity_instances(owner_id, status, created_at DESC)")
        ensure_column(db, "opportunity_instances", "contract_symbol", "TEXT")
        ensure_column(db, "opportunity_instances", "strategy_type", "TEXT")
        ensure_column(db, "opportunity_instances", "ai_direction", "TEXT")
        ensure_column(db, "opportunity_instances", "derived_direction", "TEXT")
        ensure_column(db, "opportunity_instances", "thesis", "TEXT")
        ensure_column(db, "opportunity_instances", "legs_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(db, "opportunity_instances", "payoff_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(db, "opportunity_instances", "validation_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(db, "opportunity_instances", "notification_channel_ids_json", "TEXT NOT NULL DEFAULT '[]'")
        ensure_column(db, "opportunity_instances", "followup_enabled", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(db, "opportunity_instances", "followup_interval_seconds", "INTEGER NOT NULL DEFAULT 300")
        ensure_column(db, "opportunity_instances", "cooldown_seconds", "INTEGER NOT NULL DEFAULT 1800")
        ensure_column(db, "opportunity_instances", "max_followup_alerts", "INTEGER NOT NULL DEFAULT 6")
        ensure_column(db, "opportunity_instances", "followup_alert_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "opportunity_instances", "last_checked_at", "TEXT")
        ensure_column(db, "opportunity_instances", "next_check_at", "TEXT")
        ensure_column(db, "opportunity_instances", "last_alert_at", "TEXT")
        ensure_column(db, "opportunity_instances", "expires_at", "TEXT")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_events (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_opportunity_events_opportunity_created ON opportunity_events(opportunity_id, created_at DESC)")


def mark_scan(owner_id: str, scan_id: str, *, starred: bool, note: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    cleaned_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    now = utc_now()
    with connect() as db:
        existing = db.execute("SELECT created_at FROM scan_marks WHERE owner_id = ? AND scan_id = ?", (owner, scan_id)).fetchone()
        db.execute(
            """
            INSERT INTO scan_marks (owner_id, scan_id, starred, note, tags_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_id, scan_id) DO UPDATE SET
                starred = excluded.starred,
                note = excluded.note,
                tags_json = excluded.tags_json,
                updated_at = excluded.updated_at
            """,
            (owner, scan_id, int(starred), note or "", json.dumps(cleaned_tags, ensure_ascii=False), existing["created_at"] if existing else now, now),
        )
    return get_scan_mark(owner, scan_id) or {}


def get_scan_mark(owner_id: str, scan_id: str) -> dict[str, Any] | None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute("SELECT * FROM scan_marks WHERE owner_id = ? AND scan_id = ?", (owner, scan_id)).fetchone()
    return _mark_row(row) if row else None


def list_scan_marks(owner_id: str) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM scan_marks WHERE owner_id = ? ORDER BY starred DESC, updated_at DESC",
            (owner,),
        ).fetchall()
    return [_mark_row(row) for row in rows]


def list_starred_scan_runs(owner_id: str, *, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    return list_scan_runs_with_marks(owner_id, limit=limit, offset=offset, starred=True)


def list_scan_runs_with_marks(
    owner_id: str,
    *,
    limit: int = 30,
    offset: int = 0,
    starred: bool = False,
    query: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, int(offset or 0))
    filters = ["s.owner_id = ?"]
    params: list[Any] = [owner]
    if starred:
        filters.append("COALESCE(m.starred, 0) = 1")
    normalized_query = str(query or "").strip().lower()
    if normalized_query:
        like_query = f"%{normalized_query}%"
        filters.append(
            """
            (
                LOWER(COALESCE(s.query, '')) LIKE ?
                OR LOWER(COALESCE(s.symbol, '')) LIKE ?
                OR LOWER(COALESCE(s.locator_id, '')) LIKE ?
                OR LOWER(COALESCE(m.note, '')) LIKE ?
                OR LOWER(COALESCE(m.tags_json, '[]')) LIKE ?
            )
            """
        )
        params.extend([like_query, like_query, like_query, like_query, like_query])
    normalized_tag = str(tag or "").strip().lower()
    if normalized_tag:
        filters.append("LOWER(COALESCE(m.tags_json, '[]')) LIKE ?")
        params.append(f"%{normalized_tag}%")
    params.extend([safe_limit, safe_offset])
    where_clause = " AND ".join(filters)
    order_clause = "COALESCE(m.updated_at, s.created_at) DESC" if starred else "s.created_at DESC"
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT s.id, s.locator_id, s.owner_id, s.ai_provider_owner, s.status, s.stage, s.progress, s.created_at, s.started_at, s.finished_at,
                   s.query, s.symbol, s.ai_provider, s.longbridge_account, s.market_data_source, s.use_ai,
                   s.council, s.analysis_modules_json, s.strategy_modes_json, s.mode, s.used_ai, s.error,
                   COALESCE(m.starred, 0) AS starred,
                   COALESCE(m.note, '') AS note,
                   COALESCE(m.tags_json, '[]') AS tags_json,
                   COALESCE(m.updated_at, s.created_at) AS mark_updated_at
            FROM scan_runs s
            LEFT JOIN scan_marks m ON m.owner_id = s.owner_id AND m.scan_id = s.id
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ?
            OFFSET ?
            """,
            tuple(params),
        ).fetchall()
    return [_scan_summary_row(row) for row in rows]


def create_notification_channel(owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    channel_id = payload.get("id") or uuid.uuid4().hex
    channel_type, label, config = _normalize_notification_channel_payload(payload)
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO notification_channels (id, owner_id, type, label, config_json, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (channel_id, owner, channel_type, label, json.dumps(config, ensure_ascii=False), int(bool(payload.get("enabled", True))), now, now),
        )
    return get_notification_channel(owner, channel_id) or {}


def update_notification_channel(owner_id: str, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    current = get_notification_channel(owner, channel_id, include_sensitive=True)
    if not current:
        raise ValueError("notification channel not found")
    merged = {
        "type": current.get("type"),
        "label": current.get("label"),
        "enabled": current.get("enabled"),
        "config": current.get("config") or {},
    }
    merged.update({key: value for key, value in payload.items() if value is not None})
    if payload.get("config"):
        merged["config"] = {**(current.get("config") or {}), **(payload.get("config") or {})}
    if current.get("type") == "webhook":
        current_config = current.get("config") or {}
        for sensitive_key in ("secret", "bot_token", "access_token"):
            if not _payload_has_nonempty_sensitive_value(payload, sensitive_key):
                merged[sensitive_key] = current_config.get(sensitive_key) or ""
    if current.get("type") == "webhook" and "template_variables" not in payload and "template_variables" not in (payload.get("config") or {}):
        merged["template_variables"] = (current.get("config") or {}).get("template_variables") or []
    channel_type, label, config = _normalize_notification_channel_payload(merged)
    with connect() as db:
        db.execute(
            """
            UPDATE notification_channels
            SET type = ?, label = ?, config_json = ?, enabled = ?, updated_at = ?
            WHERE owner_id = ? AND id = ?
            """,
            (channel_type, label, json.dumps(config, ensure_ascii=False), int(bool(merged.get("enabled", True))), utc_now(), owner, channel_id),
        )
    return get_notification_channel(owner, channel_id) or {}


def _payload_has_nonempty_sensitive_value(payload: dict[str, Any], key: str) -> bool:
    if key in payload:
        return bool(str(payload.get(key) or "").strip())
    config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
    if key in config:
        return bool(str(config.get(key) or "").strip())
    return False


def delete_notification_channel(owner_id: str, channel_id: str) -> None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        cursor = db.execute("DELETE FROM notification_channels WHERE owner_id = ? AND id = ?", (owner, channel_id))
    if cursor.rowcount == 0:
        raise ValueError("notification channel not found")


def _normalize_notification_channel_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    channel_type = str(payload.get("type") or "email").strip().lower()
    if channel_type not in {"email", "webhook"}:
        raise ValueError("unsupported notification channel type")
    raw_config = payload.get("config") or {}
    if channel_type == "email":
        label = str(payload.get("label") or payload.get("email") or "Email").strip()[:120]
        config = {"email": str(payload.get("email") or raw_config.get("email") or "").strip()}
        if not config["email"] or "@" not in config["email"]:
            raise ValueError("valid email is required")
    else:
        provider = _normalize_webhook_provider(payload.get("provider") or raw_config.get("provider"))
        url = str(payload.get("url") or raw_config.get("url") or "").strip()
        bot_token = str(payload.get("bot_token") or raw_config.get("bot_token") or "").strip()
        chat_id = str(payload.get("chat_id") or raw_config.get("chat_id") or "").strip()
        phone_number_id = str(payload.get("phone_number_id") or raw_config.get("phone_number_id") or "").strip()
        access_token = str(payload.get("access_token") or raw_config.get("access_token") or "").strip()
        to = str(payload.get("to") or raw_config.get("to") or "").strip()
        template_name = str(payload.get("template_name") or raw_config.get("template_name") or "").strip()
        template_language = str(payload.get("template_language") or raw_config.get("template_language") or "en_US").strip()
        template_variables = _normalize_template_variables(payload.get("template_variables", raw_config.get("template_variables")))
        if provider == "telegram" and bot_token and chat_id:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        if provider == "whatsapp" and phone_number_id and access_token and to:
            url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
        if not url.startswith(("https://", "http://")):
            raise ValueError("valid webhook url is required")
        label = str(payload.get("label") or raw_config.get("label") or _webhook_provider_label(provider)).strip()[:120]
        config = {
            "provider": provider,
            "url": url,
            "secret": str(payload.get("secret") or raw_config.get("secret") or "").strip(),
            "header_name": str(payload.get("header_name") or raw_config.get("header_name") or "X-AI-Option-Signature").strip()[:80],
            "bot_token": bot_token,
            "chat_id": chat_id,
            "phone_number_id": phone_number_id,
            "access_token": access_token,
            "to": to,
            "template_name": template_name,
            "template_language": template_language,
            "template_variables": template_variables,
        }
    return channel_type, label, config


def _normalize_template_variables(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or "")
    return [part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip()]


def send_test_notification_channel(owner_id: str, channel_id: str) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    channel = get_notification_channel(owner, channel_id, include_sensitive=True)
    if not channel:
        raise ValueError("notification channel not found")
    if not channel.get("enabled"):
        raise ValueError("notification channel is disabled")
    event = create_notification_event(
        owner,
        source_type="notification_channel",
        source_id=channel_id,
        dedupe_key=f"notification-channel-test:{channel_id}:{uuid.uuid4().hex}",
        title="AI Option 观察提醒测试",
        body=(
            "这是 AI Option 观察型机会雷达的通知渠道测试。\n\n"
            "如果你收到它，说明该渠道已经可以用于价格提醒、重新扫描评分提醒和股票池循环扫描提醒。\n\n"
            "此提醒仅用于研究辅助，不构成投资建议、交易建议或收益承诺。"
        ),
        payload={"channel_id": channel_id, "kind": "test"},
        channel_id=channel_id,
    )
    sent = send_notification_event(owner, event["id"])
    if sent.get("status") == "sent":
        _mark_channel_verified(owner, channel_id)
        _mark_channel_delivery(owner, channel_id, None, tested=True)
    else:
        _mark_channel_delivery(owner, channel_id, sent.get("last_error") or "notification test failed", tested=True)
    return {"channel": get_notification_channel(owner, channel_id), "event": sent}


def list_notification_channels(owner_id: str) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM notification_channels WHERE owner_id = ? ORDER BY updated_at DESC",
            (owner,),
        ).fetchall()
    return [_channel_row(row) for row in rows]


def get_notification_channel(owner_id: str, channel_id: str, *, include_sensitive: bool = False) -> dict[str, Any] | None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute("SELECT * FROM notification_channels WHERE owner_id = ? AND id = ?", (owner, channel_id)).fetchone()
    return _channel_row(row, include_sensitive=include_sensitive) if row else None


def create_notification_event(
    owner_id: str,
    *,
    source_type: str,
    source_id: str,
    dedupe_key: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    event_id = uuid.uuid4().hex
    now = utc_now()
    try:
        with connect() as db:
            db.execute(
                """
                INSERT INTO notification_events
                    (id, owner_id, channel_id, source_type, source_id, dedupe_key, title, body, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    event_id,
                    owner,
                    channel_id,
                    source_type,
                    source_id,
                    dedupe_key,
                    title[:220],
                    body,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                ),
            )
    except Exception:
        existing = get_notification_event_by_dedupe(owner, dedupe_key)
        if existing:
            return existing
        raise
    return get_notification_event(owner, event_id) or {}


def create_notification_events(
    owner_id: str,
    *,
    source_type: str,
    source_id: str,
    dedupe_key: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
    channel_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    cleaned_channel_ids = [str(channel_id).strip() for channel_id in (channel_ids or []) if str(channel_id).strip()]
    if not cleaned_channel_ids:
        return [
            create_notification_event(
                owner_id,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key,
                title=title,
                body=body,
                payload=payload,
                channel_id=None,
            )
        ]
    if len(cleaned_channel_ids) == 1:
        return [
            create_notification_event(
                owner_id,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key,
                title=title,
                body=body,
                payload=payload,
                channel_id=cleaned_channel_ids[0],
            )
        ]
    events: list[dict[str, Any]] = []
    for channel_id in cleaned_channel_ids:
        events.append(
            create_notification_event(
                owner_id,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=f"{dedupe_key}:channel:{channel_id}",
                title=title,
                body=body,
                payload=payload,
                channel_id=channel_id,
            )
        )
    return events


def list_notification_events(owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    safe_limit = max(1, min(limit, 200))
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM notification_events WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
            (owner, safe_limit),
        ).fetchall()
    return [_event_row(row) for row in rows]


def list_notification_delivery_logs(owner_id: str, *, channel_id: str | None = None, event_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    safe_limit = max(1, min(limit, 300))
    filters = ["owner_id = ?"]
    params: list[Any] = [owner]
    if channel_id:
        filters.append("channel_id = ?")
        params.append(channel_id)
    if event_id:
        filters.append("event_id = ?")
        params.append(event_id)
    params.append(safe_limit)
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT * FROM notification_delivery_logs
            WHERE {' AND '.join(filters)}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_delivery_log_row(row) for row in rows]


def list_pending_notification_events(owner_id: str | None = None, limit: int = 50, retry_after_seconds: int = 300, max_attempts: int = 3) -> list[dict[str, Any]]:
    init_observation_db()
    safe_limit = max(1, min(limit, 200))
    owners = [normalize_owner_id(owner_id)] if owner_id else None
    with connect() as db:
        if owners is None:
            rows = db.execute(
                """
                SELECT * FROM notification_events
                WHERE status IN ('queued', 'failed')
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (safe_limit * 5,),
            ).fetchall()
        else:
            rows = []
            for owner in owners:
                rows.extend(
                    db.execute(
                        """
                        SELECT * FROM notification_events
                        WHERE owner_id = ? AND status IN ('queued', 'failed')
                        ORDER BY created_at ASC
                        LIMIT ?
                        """,
                        (owner, safe_limit * 5),
                    ).fetchall()
                )
    now = parse_datetime(utc_now())
    if now is None:
        now = datetime.now(timezone.utc)
    pending: list[dict[str, Any]] = []
    for row in rows:
        event = _event_row(row)
        if event["status"] == "queued":
            pending.append(event)
            continue
        if event["status"] != "failed" or int(event.get("attempts") or 0) >= max_attempts:
            continue
        created_at = parse_datetime(event.get("created_at"))
        if created_at is None:
            continue
        elapsed = (now.astimezone(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds()
        if elapsed >= retry_after_seconds:
            pending.append(event)
    return pending[:safe_limit]


def get_notification_event(owner_id: str, event_id: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM notification_events WHERE owner_id = ? AND id = ?", (normalize_owner_id(owner_id), event_id)).fetchone()
    return _event_row(row) if row else None


def get_notification_event_by_dedupe(owner_id: str, dedupe_key: str) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute("SELECT * FROM notification_events WHERE owner_id = ? AND dedupe_key = ?", (normalize_owner_id(owner_id), dedupe_key)).fetchone()
    return _event_row(row) if row else None


def send_notification_event(owner_id: str, event_id: str) -> dict[str, Any]:
    event = get_notification_event(owner_id, event_id)
    if not event:
        raise ValueError("notification event not found")
    if event["status"] == "sent":
        return event
    channel = get_notification_channel(owner_id, event.get("channel_id") or "", include_sensitive=True) if event.get("channel_id") else _default_email_channel(owner_id)
    attempt = int(event.get("attempts") or 0) + 1
    request_preview: dict[str, Any] = {}
    try:
        if channel is None:
            raise RuntimeError("no notification channel configured")
        request_preview = build_notification_payload_preview(channel, event)
        _send_channel(channel, event)
        _record_delivery_log(owner_id, event_id, channel, "sent", attempt, request_preview, response_summary="accepted")
        _mark_event_status(owner_id, event_id, "sent", None)
        _mark_channel_delivery(owner_id, channel["id"], None, tested=False)
    except Exception as exc:  # noqa: BLE001 - user-facing notification failure should be persisted.
        if channel is not None:
            if not request_preview:
                try:
                    request_preview = build_notification_payload_preview(channel, event)
                except Exception:
                    request_preview = {}
            _record_delivery_log(owner_id, event_id, channel, "failed", attempt, request_preview, error=str(exc))
        _mark_event_status(owner_id, event_id, "failed", str(exc))
        if channel is not None:
            _mark_channel_delivery(owner_id, channel["id"], str(exc), tested=False)
    return get_notification_event(owner_id, event_id) or event


def prune_observation_history(retention_days: int | None = None) -> dict[str, int]:
    """Delete rows older than the retention window from the append-only event/log
    tables. These are written on every radar/notification/opportunity cycle and are
    only ever read with `ORDER BY created_at DESC LIMIT n`, so old rows are dead
    weight that only inflate table/index size. Reads stay correct; this just caps
    growth. Returns per-table deleted counts. Best-effort: never raises into callers.

    Retention defaults to AI_OPTION_OBSERVATION_RETENTION_DAYS (90), clamped [7, 3650].
    """
    if retention_days is None:
        try:
            retention_days = int(os.getenv("AI_OPTION_OBSERVATION_RETENTION_DAYS") or 90)
        except ValueError:
            retention_days = 90
    retention_days = max(7, min(int(retention_days), 3650))
    cutoff = (parse_datetime(utc_now()).astimezone(timezone.utc) - timedelta(days=retention_days)).isoformat()
    # (table, timestamp column) — all created_at except where noted.
    targets = [
        ("notification_delivery_logs", "created_at"),
        ("notification_events", "created_at"),
        ("opportunity_events", "created_at"),
        ("scan_loop_run_items", "created_at"),
        ("scan_loop_runs", "created_at"),
    ]
    deleted: dict[str, int] = {}
    try:
        with connect() as db:
            for table, column in targets:
                try:
                    cursor = db.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                    deleted[table] = int(getattr(cursor, "rowcount", 0) or 0)
                except Exception:  # noqa: BLE001 - a missing table or per-table error must not abort the rest.
                    traceback.print_exc(limit=4)
    except Exception:  # noqa: BLE001 - pruning is maintenance; never break the caller.
        traceback.print_exc(limit=4)
    return deleted


def process_notification_events(owner_id: str | None = None, limit: int = 50, retry_after_seconds: int = 300, max_attempts: int = 3) -> dict[str, Any]:
    pending = list_pending_notification_events(owner_id, limit=limit, retry_after_seconds=retry_after_seconds, max_attempts=max_attempts)
    sent = 0
    failed = 0
    skipped = 0
    processed: list[dict[str, Any]] = []
    for event in pending:
        try:
            owner = event["owner_id"]
            updated = send_notification_event(owner, event["id"])
            processed.append(updated)
            if updated.get("status") == "sent":
                sent += 1
            elif updated.get("status") == "failed":
                failed += 1
            else:
                skipped += 1
        except Exception:
            skipped += 1
    return {
        "queued": len(pending),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "events": processed,
    }


def _dispatch_notification_event(owner_id: str, event: dict[str, Any]) -> dict[str, Any]:
    if not event or not event.get("id"):
        return event
    if not event.get("channel_id") and _default_email_channel(owner_id) is None:
        return event
    return send_notification_event(owner_id, event["id"])


def _dispatch_notification_events(owner_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_dispatch_notification_event(owner_id, event) for event in events if event]


def create_scan_trigger(owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    condition = _normalize_trigger_condition(payload.get("condition") or {}, payload.get("symbol"))
    symbol = str(payload.get("symbol") or condition.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    opportunity_id = str(payload.get("opportunity_id") or "").strip() or None
    if opportunity_id and get_opportunity(owner, opportunity_id) is None:
        raise ValueError("opportunity not found")
    trigger_id = payload.get("id") or uuid.uuid4().hex
    market_policy = _normalize_market_policy(payload.get("market_policy") or (payload.get("condition") or {}).get("market_policy"))
    opening_grace = int(payload.get("opening_grace_minutes") or (payload.get("condition") or {}).get("opening_grace_minutes") or 10)
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO scan_triggers
                (id, owner_id, scan_id, locator_id, opportunity_id, name, symbol, condition_json, notification_channel_ids_json,
                 enabled, expires_at, check_interval_seconds, cooldown_seconds, max_trigger_count, next_check_at,
                 market_policy, opening_grace_minutes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trigger_id,
                owner,
                payload.get("scan_id"),
                payload.get("locator_id"),
                opportunity_id,
                str(payload.get("name") or f"{symbol} Wait Trigger")[:160],
                symbol,
                json.dumps(condition, ensure_ascii=False),
                json.dumps(payload.get("notification_channel_ids") or [], ensure_ascii=False),
                int(bool(payload.get("enabled", True))),
                payload.get("expires_at"),
                int(payload.get("check_interval_seconds") or 300),
                int(payload.get("cooldown_seconds") or 1800),
                int(payload.get("max_trigger_count") or 3),
                payload.get("next_check_at") or now,
                market_policy,
                max(0, min(opening_grace, 120)),
                now,
                now,
            ),
        )
    return get_scan_trigger(owner, trigger_id) or {}


def update_scan_trigger(owner_id: str, trigger_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    current = get_scan_trigger(owner, trigger_id)
    if not current:
        raise ValueError("trigger not found")
    condition = _normalize_trigger_condition(payload.get("condition") or current["condition"], payload.get("symbol") or current["symbol"])
    symbol = str(payload.get("symbol") or condition.get("symbol") or current["symbol"]).strip().upper()
    if "opportunity_id" in payload:
        opportunity_id = str(payload.get("opportunity_id") or "").strip() or None
    else:
        opportunity_id = current.get("opportunity_id")
    if opportunity_id and get_opportunity(owner, opportunity_id) is None:
        raise ValueError("opportunity not found")
    market_policy = _normalize_market_policy(payload.get("market_policy", current.get("market_policy")))
    opening_grace = int(payload.get("opening_grace_minutes") or current.get("opening_grace_minutes") or 10)
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE scan_triggers
            SET name = ?, symbol = ?, condition_json = ?, notification_channel_ids_json = ?,
                enabled = ?, expires_at = ?, check_interval_seconds = ?, cooldown_seconds = ?,
                max_trigger_count = ?, status = ?, market_policy = ?, opening_grace_minutes = ?, opportunity_id = ?, updated_at = ?
            WHERE owner_id = ? AND id = ?
            """,
            (
                str(payload.get("name") or current["name"])[:160],
                symbol,
                json.dumps(condition, ensure_ascii=False),
                json.dumps(payload.get("notification_channel_ids", current.get("notification_channel_ids") or []), ensure_ascii=False),
                int(bool(payload.get("enabled", current["enabled"]))),
                payload.get("expires_at", current.get("expires_at")),
                int(payload.get("check_interval_seconds") or current["check_interval_seconds"]),
                int(payload.get("cooldown_seconds") or current["cooldown_seconds"]),
                int(payload.get("max_trigger_count") or current["max_trigger_count"]),
                str(payload.get("status") or ("active" if payload.get("enabled", current["enabled"]) else "paused")),
                market_policy,
                max(0, min(opening_grace, 120)),
                opportunity_id,
                now,
                owner,
                trigger_id,
            ),
        )
    return get_scan_trigger(owner, trigger_id) or current


def delete_scan_trigger(owner_id: str, trigger_id: str) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        cursor = db.execute("DELETE FROM scan_triggers WHERE owner_id = ? AND id = ?", (owner, trigger_id))
    if cursor.rowcount == 0:
        raise ValueError("trigger not found")
    return {"deleted": True, "id": trigger_id}


def list_scan_triggers(owner_id: str) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute("SELECT * FROM scan_triggers WHERE owner_id = ? ORDER BY updated_at DESC", (owner,)).fetchall()
    return [_trigger_row(row) for row in rows]


def get_scan_trigger(owner_id: str, trigger_id: str) -> dict[str, Any] | None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute("SELECT * FROM scan_triggers WHERE owner_id = ? AND id = ?", (owner, trigger_id)).fetchone()
    return _trigger_row(row) if row else None


def check_scan_trigger(owner_id: str, trigger_id: str, *, current_value: float | None = None, quote_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    trigger = get_scan_trigger(owner_id, trigger_id)
    if not trigger:
        raise ValueError("trigger not found")
    if not trigger.get("enabled"):
        return {"trigger": trigger, "matched": False, "reason": "disabled"}
    if _is_trigger_expired(trigger):
        update_scan_trigger(owner_id, trigger_id, {"enabled": False, "status": "expired"})
        return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "expired"}
    if int(trigger.get("trigger_count") or 0) >= int(trigger.get("max_trigger_count") or 0):
        update_scan_trigger(owner_id, trigger_id, {"enabled": False, "status": "maxed_out"})
        return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "max_trigger_count_reached"}
    clock = market_clock()
    condition = trigger["condition"]
    market_block = _trigger_market_policy_block(trigger, clock, current_value=current_value)
    if market_block:
        _update_trigger_check(owner_id, trigger_id, status=market_block["status"], next_check_at=market_block["next_check_at"])
        return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, **market_block, "market_clock": clock}
    if condition.get("type") == "rescan_score":
        return _check_rescan_score_trigger(owner_id, trigger, current_value=current_value, market_clock_snapshot=clock)
    if condition.get("type") in {"technical_indicator", "option_quote"}:
        if quote_snapshot is None and current_value is None:
            quote_snapshot = build_scan_trigger_quote_snapshot(trigger)
        return _check_snapshot_field_trigger(owner_id, trigger, current_value=current_value, quote_snapshot=quote_snapshot, market_clock_snapshot=clock)
    quote = current_value if current_value is not None else _fetch_last_price(trigger["symbol"])
    matched = _compare(quote, str(condition.get("operator") or ">="), float(condition.get("value") or 0))
    if matched:
        cooldown_remaining = _trigger_cooldown_remaining_seconds(trigger)
        if cooldown_remaining > 0:
            _update_trigger_check(owner_id, trigger_id, status="cooldown", next_check_at=_utc_after_seconds(cooldown_remaining))
            return {
                "trigger": get_scan_trigger(owner_id, trigger_id),
                "matched": True,
                "suppressed": True,
                "reason": "cooldown",
                "cooldown_remaining_seconds": cooldown_remaining,
                "market_clock": clock,
            }
        events = create_notification_events(
            owner_id,
            source_type="scan_trigger",
            source_id=trigger_id,
            dedupe_key=f"trigger:{trigger_id}:{trigger['trigger_count'] + 1}",
            title=f"{trigger['symbol']} 触发观察条件",
            body=f"{trigger['name']} 已触发。当前值 {quote}，条件 {condition.get('operator')} {condition.get('value')}。仅供研究提醒。",
            payload={"trigger_id": trigger_id, "opportunity_id": trigger.get("opportunity_id"), "current_value": quote, "market_clock": clock},
            channel_ids=trigger.get("notification_channel_ids") or [],
        )
        events = _dispatch_notification_events(owner_id, events)
        event = events[0] if events else None
        _update_trigger_check(owner_id, trigger_id, triggered=True)
        _record_trigger_opportunity_event(
            owner_id,
            get_scan_trigger(owner_id, trigger_id),
            event_type="trigger_matched",
            title=f"{trigger['symbol']} Trigger 已命中",
            body=f"{trigger['name']} 命中，当前值 {quote:g}，条件 {condition.get('operator')} {condition.get('value')}。",
            payload={"trigger_id": trigger_id, "current_value": quote, "condition": condition, "notification_event_ids": [item.get("id") for item in events if item]},
        )
        return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": True, "notification_event": event, "notification_events": events, "market_clock": clock}
    _update_trigger_check(owner_id, trigger_id, status="active")
    return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "current_value": quote, "market_clock": clock}


def test_scan_trigger(owner_id: str, trigger_id: str, *, current_value: float | None = None, quote_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    trigger = get_scan_trigger(owner_id, trigger_id)
    if not trigger:
        raise ValueError("trigger not found")
    condition = trigger.get("condition") or {}
    clock = market_clock()
    trigger_type = condition.get("type")
    if trigger_type == "rescan_score" and current_value is None:
        return {
            "trigger": trigger,
            "matched": False,
            "reason": "rescan_test_requires_score_or_check",
            "market_clock": clock,
        }
    if trigger_type in {"technical_indicator", "option_quote"}:
        snapshot = quote_snapshot if quote_snapshot is not None else build_scan_trigger_quote_snapshot(trigger)
        return _evaluate_snapshot_field_trigger(trigger, current_value=current_value, quote_snapshot=snapshot, market_clock_snapshot=clock)
    snapshot = quote_snapshot if quote_snapshot is not None else build_scan_trigger_quote_snapshot(trigger)
    data_quality = _snapshot_data_quality(
        snapshot,
        symbol=str(trigger.get("symbol") or condition.get("symbol") or ""),
        market_data_source=str(condition.get("market_data_source") or snapshot.get("source") or "thetadata"),
        market_state=str(clock.get("market_state") or ""),
        review_only=False,
        uses_gex=False,
        trigger_type=str(trigger_type or ""),
    )
    snapshot["data_quality"] = data_quality
    value = current_value if current_value is not None else _numeric(snapshot.get("last"))
    if value is None:
        return {"trigger": trigger, "matched": False, "reason": "price_unavailable", "snapshot": snapshot, "data_quality": data_quality, "market_clock": clock}
    matched = _compare(float(value), str(condition.get("operator") or ">="), float(condition.get("value") or 0))
    return {"trigger": trigger, "matched": matched, "current_value": float(value), "snapshot": snapshot, "data_quality": data_quality, "market_clock": clock}


def _check_snapshot_field_trigger(
    owner_id: str,
    trigger: dict[str, Any],
    *,
    current_value: float | None,
    quote_snapshot: dict[str, Any] | None,
    market_clock_snapshot: dict[str, Any],
) -> dict[str, Any]:
    trigger_id = trigger["id"]
    evaluated = _evaluate_snapshot_field_trigger(trigger, current_value=current_value, quote_snapshot=quote_snapshot, market_clock_snapshot=market_clock_snapshot)
    field = evaluated.get("field")
    if evaluated.get("reason") in {"field_unavailable", "field_not_numeric"}:
        _update_trigger_check(owner_id, trigger_id, status="active")
        return {**evaluated, "trigger": get_scan_trigger(owner_id, trigger_id)}
    condition = trigger["condition"]
    actual_value = float(evaluated["current_value"])
    if evaluated.get("matched"):
        cooldown_remaining = _trigger_cooldown_remaining_seconds(trigger)
        if cooldown_remaining > 0:
            _update_trigger_check(owner_id, trigger_id, status="cooldown", next_check_at=_utc_after_seconds(cooldown_remaining))
            return {
                "trigger": get_scan_trigger(owner_id, trigger_id),
                "matched": True,
                "suppressed": True,
                "reason": "cooldown",
                "cooldown_remaining_seconds": cooldown_remaining,
                "current_value": actual_value,
                "field": field,
                "snapshot": evaluated.get("snapshot"),
                "market_clock": market_clock_snapshot,
            }
        events = create_notification_events(
            owner_id,
            source_type="scan_trigger",
            source_id=trigger_id,
            dedupe_key=f"trigger:{trigger_id}:{trigger['trigger_count'] + 1}",
            title=f"{trigger['symbol']} {condition.get('label') or field} 触发",
            body=(
                f"{trigger['name']} 已触发。{condition.get('label') or field} 当前值 {actual_value:g}，"
                f"条件 {condition.get('operator')} {condition.get('value')}。仅供研究提醒。"
            ),
            payload={
                "trigger_id": trigger_id,
                "opportunity_id": trigger.get("opportunity_id"),
                "field": field,
                "current_value": actual_value,
                "snapshot": evaluated.get("snapshot") or {},
                "market_clock": market_clock_snapshot,
            },
            channel_ids=trigger.get("notification_channel_ids") or [],
        )
        events = _dispatch_notification_events(owner_id, events)
        event = events[0] if events else None
        _update_trigger_check(owner_id, trigger_id, triggered=True)
        _record_trigger_opportunity_event(
            owner_id,
            get_scan_trigger(owner_id, trigger_id),
            event_type="trigger_matched",
            title=f"{trigger['symbol']} Trigger 已命中",
            body=(
                f"{trigger['name']} 命中。{condition.get('label') or field} 当前值 {actual_value:g}，"
                f"条件 {condition.get('operator')} {condition.get('value')}。"
            ),
            payload={
                "trigger_id": trigger_id,
                "field": field,
                "current_value": actual_value,
                "condition": condition,
                "snapshot": evaluated.get("snapshot") or {},
                "notification_event_ids": [item.get("id") for item in events if item],
            },
        )
        return {
            "trigger": get_scan_trigger(owner_id, trigger_id),
            "matched": True,
            "current_value": actual_value,
            "field": field,
            "snapshot": evaluated.get("snapshot"),
            "notification_event": event,
            "notification_events": events,
            "market_clock": market_clock_snapshot,
        }
    _update_trigger_check(owner_id, trigger_id, status="active")
    return {**evaluated, "trigger": get_scan_trigger(owner_id, trigger_id)}


def _evaluate_snapshot_field_trigger(
    trigger: dict[str, Any],
    *,
    current_value: float | None,
    quote_snapshot: dict[str, Any] | None,
    market_clock_snapshot: dict[str, Any],
) -> dict[str, Any]:
    condition = trigger["condition"]
    snapshot = quote_snapshot or {}
    data_quality = _snapshot_data_quality(
        snapshot,
        symbol=str(trigger.get("symbol") or condition.get("symbol") or ""),
        market_data_source=str(condition.get("market_data_source") or snapshot.get("source") or "thetadata"),
        market_state=str((market_clock_snapshot or {}).get("market_state") or ""),
        review_only=False,
        uses_gex=False,
        trigger_type=str(condition.get("type") or ""),
    )
    snapshot["data_quality"] = data_quality
    field = str(condition.get("field") or "")
    actual = current_value if current_value is not None else _deep_get(snapshot, field)
    if actual is None:
        return {
            "trigger": trigger,
            "matched": False,
            "reason": "field_unavailable",
            "field": field,
            "snapshot": snapshot,
            "data_quality": data_quality,
            "market_clock": market_clock_snapshot,
        }
    actual_value = _numeric(actual)
    if actual_value is None:
        return {
            "trigger": trigger,
            "matched": False,
            "reason": "field_not_numeric",
            "field": field,
            "snapshot": snapshot,
            "data_quality": data_quality,
            "market_clock": market_clock_snapshot,
        }
    matched = _compare(actual_value, str(condition.get("operator") or ">="), float(condition.get("value") or 0))
    return {
        "trigger": trigger,
        "matched": matched,
        "current_value": actual_value,
        "field": field,
        "snapshot": snapshot,
        "data_quality": data_quality,
        "market_clock": market_clock_snapshot,
    }


def _check_rescan_score_trigger(
    owner_id: str,
    trigger: dict[str, Any],
    *,
    current_value: float | None,
    market_clock_snapshot: dict[str, Any],
) -> dict[str, Any]:
    trigger_id = trigger["id"]
    condition = trigger["condition"]
    score: float | None = float(current_value) if current_value is not None else None
    rescan_scan_id = str(condition.get("rescan_scan_id") or "").strip()
    score_source: dict[str, Any] = {"type": "manual"} if current_value is not None else {}
    if score is None and rescan_scan_id:
        rescan = get_scan_run(rescan_scan_id, owner_id=normalize_owner_id(owner_id))
        if not rescan:
            _set_trigger_condition_metadata(owner_id, trigger_id, {"last_error": "rescan not found"}, remove_keys=["rescan_scan_id", "rescan_requested_at"])
            _update_trigger_check(owner_id, trigger_id, status="active")
            return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "rescan_not_found", "market_clock": market_clock_snapshot}
        if rescan["status"] in {"queued", "running"}:
            _update_trigger_check(owner_id, trigger_id, status="rescan_pending")
            return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "rescan_pending", "rescan_scan": _scan_reference(rescan), "market_clock": market_clock_snapshot}
        if rescan["status"] == "failed":
            _set_trigger_condition_metadata(
                owner_id,
                trigger_id,
                {"last_rescan_scan_id": rescan_scan_id, "last_error": rescan.get("error") or "rescan failed"},
                remove_keys=["rescan_scan_id", "rescan_requested_at"],
            )
            _update_trigger_check(owner_id, trigger_id, status="active")
            return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "rescan_failed", "rescan_scan": _scan_reference(rescan), "market_clock": market_clock_snapshot}
        score = _extract_rescan_score(rescan, str(condition.get("score_field") or "decision_score"))
        score_source = {"type": "rescan", "scan": _scan_reference(rescan)}
        if score is None:
            _set_trigger_condition_metadata(
                owner_id,
                trigger_id,
                {"last_rescan_scan_id": rescan_scan_id, "last_error": "score unavailable"},
                remove_keys=["rescan_scan_id", "rescan_requested_at"],
            )
            _update_trigger_check(owner_id, trigger_id, status="active")
            return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "score_unavailable", "rescan_scan": _scan_reference(rescan), "market_clock": market_clock_snapshot}
    if score is None:
        rescan = _submit_rescan_for_trigger(owner_id, trigger)
        _set_trigger_condition_metadata(owner_id, trigger_id, {"rescan_scan_id": rescan["id"], "rescan_requested_at": utc_now()}, remove_keys=["last_error"])
        _update_trigger_check(owner_id, trigger_id, status="rescan_pending")
        return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "rescan_submitted", "rescan_scan": _scan_reference(rescan), "market_clock": market_clock_snapshot}

    matched = _compare(score, str(condition.get("operator") or ">="), float(condition.get("value") or 0))
    if matched:
        cooldown_remaining = _trigger_cooldown_remaining_seconds(trigger)
        if cooldown_remaining > 0:
            _set_trigger_condition_metadata(owner_id, trigger_id, {"last_score": score, "last_score_at": utc_now()}, remove_keys=["rescan_scan_id", "rescan_requested_at"])
            _update_trigger_check(owner_id, trigger_id, status="cooldown", next_check_at=_utc_after_seconds(cooldown_remaining))
            return {
                "trigger": get_scan_trigger(owner_id, trigger_id),
                "matched": True,
                "suppressed": True,
                "reason": "cooldown",
                "cooldown_remaining_seconds": cooldown_remaining,
                "score": score,
                "score_source": score_source,
                "market_clock": market_clock_snapshot,
            }
        events = create_notification_events(
            owner_id,
            source_type="scan_trigger",
            source_id=trigger_id,
            dedupe_key=f"trigger:{trigger_id}:{trigger['trigger_count'] + 1}",
            title=f"{trigger['symbol']} 重新扫描评分触发",
            body=(
                f"{trigger['name']} 已触发。重新扫描评分 {score:.1f}，"
                f"条件 {condition.get('operator')} {condition.get('value')}。此提醒仅用于研究辅助。"
            ),
            payload={"trigger_id": trigger_id, "opportunity_id": trigger.get("opportunity_id"), "score": score, "score_source": score_source, "market_clock": market_clock_snapshot},
            channel_ids=trigger.get("notification_channel_ids") or [],
        )
        events = _dispatch_notification_events(owner_id, events)
        event = events[0] if events else None
        _set_trigger_condition_metadata(owner_id, trigger_id, {"last_score": score, "last_score_at": utc_now()}, remove_keys=["rescan_scan_id", "rescan_requested_at", "last_error"])
        _update_trigger_check(owner_id, trigger_id, triggered=True)
        _record_trigger_opportunity_event(
            owner_id,
            get_scan_trigger(owner_id, trigger_id),
            event_type="trigger_matched",
            title=f"{trigger['symbol']} Trigger 已命中",
            body=f"{trigger['name']} 命中，重新扫描评分 {score:.1f}，条件 {condition.get('operator')} {condition.get('value')}。",
            payload={
                "trigger_id": trigger_id,
                "score": score,
                "score_source": score_source,
                "condition": condition,
                "notification_event_ids": [item.get("id") for item in events if item],
            },
        )
        return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": True, "score": score, "score_source": score_source, "notification_event": event, "notification_events": events, "market_clock": market_clock_snapshot}

    _set_trigger_condition_metadata(owner_id, trigger_id, {"last_score": score, "last_score_at": utc_now()}, remove_keys=["rescan_scan_id", "rescan_requested_at", "last_error"])
    _update_trigger_check(owner_id, trigger_id, status="active")
    return {"trigger": get_scan_trigger(owner_id, trigger_id), "matched": False, "reason": "score_below_threshold", "score": score, "score_source": score_source, "market_clock": market_clock_snapshot}


def create_watchlist(owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    symbols = _normalize_symbols(payload.get("symbols") or DEFAULT_SYMBOLS)
    if not symbols:
        raise ValueError("watchlist requires at least one symbol")
    watchlist_id = payload.get("id") or uuid.uuid4().hex
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO watchlists (id, owner_id, name, description, symbols_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (watchlist_id, owner, str(payload.get("name") or "观察股票池")[:160], payload.get("description"), json.dumps(symbols), now, now),
        )
    return get_watchlist(owner, watchlist_id) or {}


def list_watchlists(owner_id: str) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute("SELECT * FROM watchlists WHERE owner_id = ? ORDER BY updated_at DESC", (owner,)).fetchall()
    return [_watchlist_row(row) for row in rows]


def get_watchlist(owner_id: str, watchlist_id: str) -> dict[str, Any] | None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute("SELECT * FROM watchlists WHERE owner_id = ? AND id = ?", (owner, watchlist_id)).fetchone()
    return _watchlist_row(row) if row else None


def update_watchlist(owner_id: str, watchlist_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_watchlist(owner_id, watchlist_id)
    if not current:
        raise ValueError("watchlist not found")
    symbols = _normalize_symbols(payload.get("symbols") if "symbols" in payload else current["symbols"])
    with connect() as db:
        db.execute(
            """
            UPDATE watchlists
            SET name = ?, description = ?, symbols_json = ?, updated_at = ?
            WHERE owner_id = ? AND id = ?
            """,
            (
                str(payload.get("name") or current["name"])[:160],
                payload.get("description", current.get("description")),
                json.dumps(symbols),
                utc_now(),
                normalize_owner_id(owner_id),
                watchlist_id,
            ),
        )
    return get_watchlist(owner_id, watchlist_id) or current


def delete_watchlist(owner_id: str, watchlist_id: str) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        db.execute("DELETE FROM watchlists WHERE owner_id = ? AND id = ?", (owner, watchlist_id))
    return {"deleted": True, "id": watchlist_id}


def create_scan_loop_instance(owner_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    watchlist = get_watchlist(owner, str(payload.get("watchlist_id") or "")) if payload.get("watchlist_id") else None
    symbols = _normalize_symbols(payload.get("symbols") or (watchlist or {}).get("symbols") or DEFAULT_SYMBOLS)
    if not symbols:
        raise ValueError("scan loop instance requires symbols")
    instance_id = payload.get("id") or uuid.uuid4().hex
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            INSERT INTO scan_loop_instances
                (id, owner_id, watchlist_id, name, description, status, symbols_snapshot_json, schedule_json, market_session,
                 eod_review_enabled, eod_run_time_et, weekend_review_enabled, weekend_run_time_local,
                 market_data_source, option_data_source, ai_provider, use_ai, council, analysis_modules_json, strategy_modes_json, prompt_template,
                 prefilter_rules_json, alert_rules_json, alert_mode, notification_channel_ids_json,
                 max_alerts_per_day, max_ai_scans_per_day, ai_scan_policy, ai_scan_top_n,
                 symbol_cooldown_minutes, run_timeout_seconds, expires_at,
                 next_run_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id,
                owner,
                (watchlist or {}).get("id"),
                str(payload.get("name") or "循环扫描实例")[:160],
                payload.get("description"),
                str(payload.get("status") or "active"),
                json.dumps(symbols),
                json.dumps(payload.get("schedule") or {"type": "interval_minutes", "interval_minutes": 30, "skip_missed_runs": True}, ensure_ascii=False),
                str(payload.get("market_session") or "regular"),
                int(bool(payload.get("eod_review_enabled", False))),
                str(payload.get("eod_run_time_et") or "16:20")[:16] if payload.get("eod_review_enabled", False) else None,
                int(bool(payload.get("weekend_review_enabled", False))),
                str(payload.get("weekend_run_time_local") or "Sunday 18:00")[:32] if payload.get("weekend_review_enabled", False) else None,
                str(payload.get("market_data_source") or "yfinance"),
                str(payload.get("option_data_source") or "thetadata"),
                str(payload.get("ai_provider") or "deepseek"),
                int(bool(payload.get("use_ai", True))),
                int(bool(payload.get("council", True))),
                json.dumps(payload.get("analysis_modules") or {}, ensure_ascii=False),
                json.dumps(payload.get("strategy_modes") or ["single_leg", "spread"], ensure_ascii=False),
                str(payload.get("prompt_template") or "扫描{symbol}最近日K、今天分时、新闻和期权链，寻找高赔率但风险可控的期权方案。"),
                json.dumps(payload.get("prefilter_rules") or {"logic": "and", "conditions": []}, ensure_ascii=False),
                json.dumps(payload.get("alert_rules") or {"logic": "and", "conditions": []}, ensure_ascii=False),
                str(payload.get("alert_mode") or "best_per_run"),
                json.dumps(payload.get("notification_channel_ids") or [], ensure_ascii=False),
                int(payload.get("max_alerts_per_day") or 5),
                int(payload.get("max_ai_scans_per_day") or 10),
                _normalize_ai_scan_policy(payload.get("ai_scan_policy")),
                _scan_loop_ai_top_n(payload.get("ai_scan_top_n")),
                int(payload.get("symbol_cooldown_minutes") or 30),
                int(payload.get("run_timeout_seconds") or 600),
                payload.get("expires_at"),
                payload.get("next_run_at") or now,
                now,
                now,
            ),
        )
    return get_scan_loop_instance(owner, instance_id) or {}


def list_scan_loop_instances(owner_id: str) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute("SELECT * FROM scan_loop_instances WHERE owner_id = ? ORDER BY updated_at DESC", (owner,)).fetchall()
    return [_instance_row(row) for row in rows]


def get_scan_loop_instance(owner_id: str, instance_id: str) -> dict[str, Any] | None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute("SELECT * FROM scan_loop_instances WHERE owner_id = ? AND id = ?", (owner, instance_id)).fetchone()
    return _instance_row(row) if row else None


def update_scan_loop_instance(owner_id: str, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = get_scan_loop_instance(owner_id, instance_id)
    if not current:
        raise ValueError("scan loop instance not found")
    merged = {**current, **payload}
    target_watchlist = get_watchlist(owner_id, str(merged.get("watchlist_id") or "")) if merged.get("watchlist_id") else None
    if payload.get("symbols") is None and payload.get("watchlist_id") and target_watchlist:
        symbols = _normalize_symbols(target_watchlist.get("symbols"))
    else:
        symbols = _normalize_symbols(merged.get("symbols") or merged.get("symbols_snapshot"))
    with connect() as db:
        db.execute(
            """
            UPDATE scan_loop_instances
            SET watchlist_id = ?, name = ?, description = ?, status = ?, symbols_snapshot_json = ?, schedule_json = ?,
                market_session = ?, eod_review_enabled = ?, eod_run_time_et = ?,
                weekend_review_enabled = ?, weekend_run_time_local = ?,
                market_data_source = ?, ai_provider = ?, use_ai = ?, council = ?, analysis_modules_json = ?,
                strategy_modes_json = ?, prompt_template = ?, prefilter_rules_json = ?, alert_rules_json = ?,
                alert_mode = ?, notification_channel_ids_json = ?, max_alerts_per_day = ?, max_ai_scans_per_day = ?,
                ai_scan_policy = ?, ai_scan_top_n = ?, option_data_source = ?, updated_at = ?
            WHERE owner_id = ? AND id = ?
            """,
            (
                (target_watchlist or {}).get("id"),
                str(merged.get("name") or current["name"])[:160],
                merged.get("description"),
                str(merged.get("status") or "active"),
                json.dumps(symbols),
                json.dumps(merged.get("schedule") or current["schedule"], ensure_ascii=False),
                str(merged.get("market_session") or current.get("market_session") or "regular"),
                int(bool(merged.get("eod_review_enabled", current.get("eod_review_enabled", False)))),
                str(merged.get("eod_run_time_et") or current.get("eod_run_time_et") or "16:20")[:16] if bool(merged.get("eod_review_enabled", current.get("eod_review_enabled", False))) else None,
                int(bool(merged.get("weekend_review_enabled", current.get("weekend_review_enabled", False)))),
                str(merged.get("weekend_run_time_local") or current.get("weekend_run_time_local") or "Sunday 18:00")[:32] if bool(merged.get("weekend_review_enabled", current.get("weekend_review_enabled", False))) else None,
                str(merged.get("market_data_source") or "thetadata"),
                str(merged.get("ai_provider") or "deepseek"),
                int(bool(merged.get("use_ai", True))),
                int(bool(merged.get("council", True))),
                json.dumps(merged.get("analysis_modules") or {}, ensure_ascii=False),
                json.dumps(merged.get("strategy_modes") or ["single_leg"], ensure_ascii=False),
                str(merged.get("prompt_template") or current["prompt_template"]),
                json.dumps(merged.get("prefilter_rules") or {"logic": "and", "conditions": []}, ensure_ascii=False),
                json.dumps(merged.get("alert_rules") or {"logic": "and", "conditions": []}, ensure_ascii=False),
                str(merged.get("alert_mode") or "best_per_run"),
                json.dumps(merged.get("notification_channel_ids") or [], ensure_ascii=False),
                int(merged.get("max_alerts_per_day") or current.get("max_alerts_per_day") or 5),
                int(merged.get("max_ai_scans_per_day") or current.get("max_ai_scans_per_day") or 10),
                _normalize_ai_scan_policy(merged.get("ai_scan_policy") or current.get("ai_scan_policy")),
                _scan_loop_ai_top_n(merged.get("ai_scan_top_n") or current.get("ai_scan_top_n")),
                str(merged.get("option_data_source") or current.get("option_data_source") or "thetadata"),
                utc_now(),
                normalize_owner_id(owner_id),
                instance_id,
            ),
        )
    return get_scan_loop_instance(owner_id, instance_id) or current


def delete_scan_loop_instance(owner_id: str, instance_id: str) -> dict[str, Any]:
    with connect() as db:
        db.execute("DELETE FROM scan_loop_instances WHERE owner_id = ? AND id = ?", (normalize_owner_id(owner_id), instance_id))
    return {"deleted": True, "id": instance_id}


def run_due_scan_loop_instance(instance: dict[str, Any], *, submit_scans: bool = True) -> dict[str, Any]:
    clock = market_clock()
    market_state = _market_state(clock)
    if market_state == "regular_open":
        return run_scan_loop_instance(instance["owner_id"], instance["id"], allow_non_regular=False, submit_scans=submit_scans)
    if _scan_loop_review_due(instance, market_state, clock):
        return run_scan_loop_instance(
            instance["owner_id"],
            instance["id"],
            allow_non_regular=True,
            submit_scans=False,
            review_only=True,
        )
    return run_scan_loop_instance(instance["owner_id"], instance["id"], allow_non_regular=False, submit_scans=False)


def run_scan_loop_instance(
    owner_id: str,
    instance_id: str,
    *,
    quote_snapshots: dict[str, dict[str, Any]] | None = None,
    allow_non_regular: bool = False,
    submit_scans: bool = True,
    review_only: bool = False,
) -> dict[str, Any]:
    instance = get_scan_loop_instance(owner_id, instance_id)
    if not instance:
        raise ValueError("scan loop instance not found")
    owner = normalize_owner_id(owner_id)
    clock = market_clock()
    market_state = _market_state(clock)
    run_id = uuid.uuid4().hex
    now = utc_now()
    review_only_active = bool(review_only and market_state != "regular_open")
    if market_state != "regular_open" and not allow_non_regular:
        summary = {"reason": "market_not_regular_open", "market_clock": clock}
        _insert_loop_run(run_id, owner, instance, "skipped", now, now, 0, 0, 0, market_state, summary)
        with connect() as db:
            db.execute(
                "UPDATE scan_loop_instances SET last_run_at = ?, next_run_at = ?, last_market_state = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
                (now, _next_scan_loop_run_at(instance, clock, market_state), market_state, now, owner, instance_id),
            )
        return get_scan_loop_run(owner, run_id) or {}

    symbols = instance["symbols"]
    matched_count = 0
    alerted_count = 0
    unavailable_count = 0
    daily_usage = _scan_loop_daily_usage(owner, instance_id)
    remaining_alerts = max(int(instance.get("max_alerts_per_day") or 0) - daily_usage["alerts"], 0)
    remaining_ai_scans = max(int(instance.get("max_ai_scans_per_day") or 0) - daily_usage["ai_scans"], 0)
    alert_mode = str(instance.get("alert_mode") or "best_per_run").strip().lower()
    ai_scan_policy = _normalize_ai_scan_policy(instance.get("ai_scan_policy"))
    ai_scan_top_n = _scan_loop_ai_top_n(instance.get("ai_scan_top_n"))
    evaluated_items: list[dict[str, Any]] = []
    report_items: list[dict[str, Any]] = []
    _insert_loop_run(
        run_id,
        owner,
        instance,
        "running",
        now,
        None,
        len(symbols),
        0,
        0,
        market_state,
        {
            "market_clock": clock,
            "review_only": review_only_active,
            "notification_policy": "suppressed_non_regular_review" if review_only_active else "real_time_alerts",
            "ai_scan_policy": ai_scan_policy,
            "ai_scan_top_n": ai_scan_top_n,
        },
    )
    run_data_quality: list[dict[str, Any]] = []
    for symbol in symbols:
        snapshot = (quote_snapshots or {}).get(symbol) or _fetch_quote_snapshot(symbol)
        if _scan_loop_uses_gex(instance) and _snapshot_data_available(snapshot) and not _gex_snapshot_available(snapshot):
            snapshot.update(_scan_loop_gex_fields(_fetch_current_gex_snapshot(symbol, str(instance.get("option_data_source") or "thetadata"), _numeric(snapshot.get("last")))))
        data_quality = _snapshot_data_quality(
            snapshot,
            symbol=symbol,
            market_data_source=str(instance.get("market_data_source") or "thetadata"),
            market_state=market_state,
            review_only=review_only_active,
            uses_gex=_scan_loop_uses_gex(instance),
        )
        snapshot["data_quality"] = data_quality
        run_data_quality.append(data_quality)
        if not _snapshot_data_available(snapshot):
            unavailable_count += 1
            prefilter = {
                "matched": False,
                "logic": "and",
                "checks": [],
                "snapshot": snapshot,
                "reason": "data_unavailable",
                "data_quality": data_quality,
            }
            evaluated_items.append(
                {
                    "symbol": symbol,
                    "snapshot": snapshot,
                    "prefilter": prefilter,
                    "alert_result": {"matched": False, "reason": "data_unavailable"},
                    "alert_matched": False,
                    "scan_id": None,
                    "status": "data_unavailable",
                    "alert_suppressed_reason": "data_unavailable",
                    "ai_scan_suppressed_reason": None,
                    "error": str(snapshot.get("error") or "quote data unavailable"),
                }
            )
            continue
        prefilter = evaluate_rule_group(instance["prefilter_rules"], snapshot)
        prefilter["data_quality"] = data_quality
        matched = bool(prefilter["matched"])
        scan_id = None
        alert_result = {"matched": False, "reason": "prefilter_not_matched"} if not matched else evaluate_rule_group(instance["alert_rules"], snapshot)
        alert_result["data_quality"] = data_quality
        alert_matched = bool(alert_result["matched"])
        alert_suppressed_reason = None
        ai_scan_suppressed_reason = None
        status = "matched" if matched else "filtered"
        if matched:
            matched_count += 1
        evaluated_items.append(
            {
                "symbol": symbol,
                "snapshot": snapshot,
                "prefilter": prefilter,
                "alert_result": alert_result,
                "alert_matched": alert_matched,
                "scan_id": scan_id,
                "status": status,
                "alert_suppressed_reason": alert_suppressed_reason,
                "ai_scan_suppressed_reason": ai_scan_suppressed_reason,
                "error": None,
            }
        )

    ai_selection = _scan_loop_ai_selection(evaluated_items, ai_scan_policy, remaining_ai_scans, ai_scan_top_n)
    _annotate_scan_loop_ai_decisions(evaluated_items, ai_selection, ai_scan_policy, ai_scan_top_n)
    if submit_scans:
        for index in ai_selection["selected_indexes"]:
            item = evaluated_items[index]
            symbol = item["symbol"]
            scan = submit_scan(
                query=instance["prompt_template"].replace("{symbol}", symbol),
                symbol=symbol,
                ai_provider=instance["ai_provider"],
                longbridge_account="yfinance",
                use_ai=instance["use_ai"],
                council=instance["council"],
                analysis_modules=instance["analysis_modules"],
                strategy_modes=instance["strategy_modes"],
                market_data_source=instance["market_data_source"],
                option_data_source=instance.get("option_data_source") or "thetadata",
                owner_id=owner,
                ai_provider_owner=owner,
                source_type="scan_loop",
                source_id=run_id,
                scan_loop_instance_id=instance_id,
            )
            item["scan_id"] = scan["id"]
            remaining_ai_scans -= 1
        for index, reason in ai_selection["suppressed_reasons"].items():
            evaluated_items[index]["ai_scan_suppressed_reason"] = reason

    eligible_alert_indexes = [
        index
        for index, item in enumerate(evaluated_items)
        if item["alert_matched"] and item["status"] not in {"data_unavailable", "filtered"}
    ]
    selected_alert_indexes: set[int] = set()
    if not review_only_active and alert_mode == "all_matches":
        selected_alert_indexes = set(eligible_alert_indexes)
    elif not review_only_active and alert_mode == "best_per_run" and eligible_alert_indexes:
        selected_alert_indexes = {max(eligible_alert_indexes, key=lambda index: _scan_loop_alert_score(evaluated_items[index]["snapshot"]))}

    digest_candidates: list[dict[str, Any]] = []
    for index, item in enumerate(evaluated_items):
        symbol = item["symbol"]
        snapshot = item["snapshot"]
        prefilter = item["prefilter"]
        alert_result = item["alert_result"]
        alert_matched = bool(item["alert_matched"])
        alert_suppressed_reason = item["alert_suppressed_reason"]
        ai_scan_suppressed_reason = item["ai_scan_suppressed_reason"]
        status = item["status"]
        scan_id = item["scan_id"]
        triggered = False
        opportunity_id = None
        if alert_matched and status not in {"data_unavailable", "filtered"}:
            if review_only_active:
                alert_suppressed_reason = "review_only_non_regular"
                status = "reviewed"
            elif alert_mode == "daily_digest":
                alert_suppressed_reason = f"alert_mode_{alert_mode}"
                if remaining_alerts <= 0:
                    alert_suppressed_reason = "max_alerts_per_day"
                    status = "alert_suppressed"
                else:
                    status = "digest_pending"
                    triggered = True
                    opportunity = create_lightweight_opportunity(owner, instance, run_id, symbol, scan_id, snapshot)
                    opportunity_id = opportunity.get("id")
                    digest_candidates.append(
                        {
                            "symbol": symbol,
                            "scan_id": scan_id,
                            "snapshot": snapshot,
                            "prefilter": prefilter,
                            "alert": alert_result,
                            "ai_scan_suppressed_reason": ai_scan_suppressed_reason,
                            "opportunity_id": opportunity_id,
                            "score": _scan_loop_alert_score(snapshot),
                        }
                    )
            elif alert_mode == "silent_log":
                alert_suppressed_reason = f"alert_mode_{alert_mode}"
                status = "alert_suppressed"
            elif index not in selected_alert_indexes:
                alert_suppressed_reason = "alert_mode_best_per_run"
                status = "alert_suppressed"
            elif remaining_alerts <= 0:
                alert_suppressed_reason = "max_alerts_per_day"
                status = "alert_suppressed"
            else:
                cooldown_hit = _scan_loop_symbol_cooldown_active(owner, instance_id, symbol, int(instance.get("symbol_cooldown_minutes") or 0))
                if cooldown_hit:
                    alert_suppressed_reason = "cooldown"
                    status = "alert_suppressed"
                else:
                    body_scan_text = "已提交完整扫描。" if scan_id else "AI 精扫未提交，请查看运行日志。"
                    events = create_notification_events(
                        owner,
                        source_type="scan_loop_run",
                        source_id=run_id,
                        dedupe_key=f"scan-loop:{instance_id}:{run_id}:{symbol}",
                        title=f"{symbol} 命中观察池提醒",
                        body=(
                            f"{instance['name']} 中 {symbol} 命中预筛并满足提醒条件，{body_scan_text}"
                            "此提醒仅用于研究辅助。"
                        ),
                        payload={
                            "run_id": run_id,
                            "instance_id": instance_id,
                            "symbol": symbol,
                            "scan_id": scan_id,
                            "prefilter": prefilter,
                            "alert": alert_result,
                            "market_clock": clock,
                            "alert_mode": alert_mode,
                            "ai_scan_suppressed_reason": ai_scan_suppressed_reason,
                        },
                        channel_ids=instance.get("notification_channel_ids") or [],
                    )
                    events = _dispatch_notification_events(owner, events)
                    event = events[0] if events else None
                    alerted_count += 1 if event else 0
                    remaining_alerts -= 1
                    triggered = True
                    opportunity = create_lightweight_opportunity(owner, instance, run_id, symbol, scan_id, snapshot)
                    opportunity_id = opportunity.get("id")
                    status = "alerted"
        elif status not in {"data_unavailable", "filtered"}:
            alert_suppressed_reason = "alert_not_matched" if prefilter.get("matched") else None
        _insert_loop_item(
            run_id,
            owner,
            instance,
            symbol,
            status,
            prefilter,
            snapshot,
            scan_id,
            triggered,
            {
                "alert_result": alert_result,
                "alert_matched": alert_matched,
                "alert_suppressed_reason": alert_suppressed_reason,
                "ai_scan_suppressed_reason": ai_scan_suppressed_reason,
                "ai_scan_decision": item.get("ai_scan_decision") or {},
                "alert_mode": alert_mode,
            },
            error=item["error"],
        )
        report_items.append(
            {
                "symbol": symbol,
                "snapshot": snapshot,
                "prefilter": prefilter,
                "alert_result": alert_result,
                "status": status,
                "triggered": triggered,
                "scan_id": scan_id,
                "opportunity_id": opportunity_id,
                "score": _scan_loop_alert_score(snapshot),
                "alert_matched": alert_matched,
                "alert_suppressed_reason": alert_suppressed_reason,
                "ai_scan_suppressed_reason": ai_scan_suppressed_reason,
                "data_quality": snapshot.get("data_quality") or {},
                "error": item["error"],
            }
        )
    digest_event = None
    if digest_candidates and not review_only_active and alert_mode == "daily_digest" and remaining_alerts > 0:
        digest_events = _create_scan_loop_digest_notification(owner, instance, run_id, digest_candidates, clock)
        digest_events = _dispatch_notification_events(owner, digest_events)
        digest_event = digest_events[0] if digest_events else None
        alerted_count += 1 if digest_event else 0
        remaining_alerts -= 1
    report_events = _create_scan_loop_report_notification(
        owner,
        instance,
        run_id,
        report_items,
        clock,
        review_only=review_only_active,
    )
    report_events = _dispatch_notification_events(owner, report_events) if report_events else []
    finished = utc_now()
    run_status = "partial_failed" if unavailable_count else "reviewed" if review_only_active else "succeeded"
    summary = {
        "market_clock": clock,
        "matched_count": matched_count,
        "alerted_count": alerted_count,
        "data_unavailable_count": unavailable_count,
        "review_only": review_only_active,
        "notification_policy": "suppressed_non_regular_review" if review_only_active else "real_time_alerts",
        "alert_mode": alert_mode,
        "ai_scan_policy": ai_scan_policy,
        "ai_scan_top_n": ai_scan_top_n,
        "ai_scan_candidate_count": ai_selection["candidate_count"],
        "ai_scan_selected_count": len(ai_selection["selected_indexes"]) if submit_scans else 0,
        "ai_scan_budget": {
            "max_per_day": int(instance.get("max_ai_scans_per_day") or 0),
            "used_before_run": int(daily_usage.get("ai_scans") or 0),
            "remaining_before_run": int(ai_selection.get("remaining_before_run") or 0),
            "used_this_run": len(ai_selection["selected_indexes"]) if submit_scans else 0,
            "remaining_after_run": remaining_ai_scans,
        },
        "ai_cost_projection": _scan_loop_cost_projection(len(ai_selection["selected_indexes"]) if submit_scans else 0),
        "daily_usage_start": daily_usage,
        "remaining_alerts": remaining_alerts,
        "remaining_ai_scans": remaining_ai_scans,
        "digest_notification_event_id": (digest_event or {}).get("id"),
        "digest_count": len(digest_candidates),
        "report_notification_event_ids": [event.get("id") for event in report_events if event],
        "report_notification_count": len(report_events),
        "data_quality": run_data_quality,
    }
    freshness_status = "partial_failed" if unavailable_count else "stale" if review_only_active else "fresh"
    freshness_explanations = [item["explanation"] for item in run_data_quality if item.get("explanation")]
    with connect() as db:
        db.execute(
            """
            UPDATE scan_loop_runs
            SET status = ?, finished_at = ?, matched_count = ?, alerted_count = ?, data_freshness_json = ?, summary_json = ?
            WHERE owner_id = ? AND id = ?
            """,
            (
                run_status,
                finished,
                matched_count,
                alerted_count,
                json.dumps(
                    {
                        "freshness_status": freshness_status,
                        "data_unavailable_count": unavailable_count,
                        "review_only": review_only_active,
                        "explanations": freshness_explanations[:6],
                        "sources": sorted({str(item.get("source") or "") for item in run_data_quality if item.get("source")}),
                        "items": run_data_quality,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(summary, ensure_ascii=False),
                owner,
                run_id,
            ),
        )
        db.execute(
            """
            UPDATE scan_loop_instances
            SET last_run_at = ?, next_run_at = ?, last_market_state = ?,
                last_eod_review_date = COALESCE(?, last_eod_review_date),
                last_weekend_review_key = COALESCE(?, last_weekend_review_key),
                updated_at = ?
            WHERE owner_id = ? AND id = ?
            """,
            (
                finished,
                _next_scan_loop_run_at(instance, clock, market_state),
                market_state,
                str(clock.get("date_et") or "") if review_only_active and market_state == "closed_today" else None,
                _weekend_review_key(clock) if review_only_active and market_state in {"weekend", "holiday"} else None,
                finished,
                owner,
                instance_id,
            ),
        )
    return get_scan_loop_run(owner, run_id) or {}


def test_scan_loop_instance(
    owner_id: str,
    instance_id: str,
    *,
    quote_snapshots: dict[str, dict[str, Any]] | None = None,
    allow_non_regular: bool = True,
) -> dict[str, Any]:
    instance = get_scan_loop_instance(owner_id, instance_id)
    if not instance:
        raise ValueError("scan loop instance not found")
    owner = normalize_owner_id(owner_id)
    clock = market_clock()
    market_state = _market_state(clock)
    now = utc_now()
    market_blocked = market_state != "regular_open"
    daily_usage = _scan_loop_daily_usage(owner, instance_id)
    remaining_alerts = max(int(instance.get("max_alerts_per_day") or 0) - daily_usage["alerts"], 0)
    remaining_ai_scans = max(int(instance.get("max_ai_scans_per_day") or 0) - daily_usage["ai_scans"], 0)
    alert_mode = str(instance.get("alert_mode") or "best_per_run").strip().lower()
    ai_scan_policy = _normalize_ai_scan_policy(instance.get("ai_scan_policy"))
    ai_scan_top_n = _scan_loop_ai_top_n(instance.get("ai_scan_top_n"))
    uses_gex = _scan_loop_uses_gex(instance)
    evaluated_items: list[dict[str, Any]] = []
    run_data_quality: list[dict[str, Any]] = []

    for symbol in instance.get("symbols") or []:
        snapshot = dict((quote_snapshots or {}).get(symbol) or _fetch_quote_snapshot(symbol))
        if uses_gex and _snapshot_data_available(snapshot) and not _gex_snapshot_available(snapshot):
            snapshot.update(_scan_loop_gex_fields(_fetch_current_gex_snapshot(symbol, str(instance.get("option_data_source") or "thetadata"), _numeric(snapshot.get("last")))))
        data_quality = _snapshot_data_quality(
            snapshot,
            symbol=symbol,
            market_data_source=str(instance.get("market_data_source") or "thetadata"),
            market_state=market_state,
            review_only=market_state != "regular_open",
            uses_gex=uses_gex,
        )
        snapshot["data_quality"] = data_quality
        run_data_quality.append(data_quality)
        if not _snapshot_data_available(snapshot):
            prefilter = {
                "matched": False,
                "logic": str((instance.get("prefilter_rules") or {}).get("logic") or "and"),
                "checks": [],
                "snapshot": snapshot,
                "reason": "data_unavailable",
                "data_quality": data_quality,
            }
            evaluated_items.append(
                _scan_loop_test_item(
                    owner,
                    instance,
                    symbol,
                    snapshot,
                    prefilter,
                    {"matched": False, "reason": "data_unavailable", "data_quality": data_quality},
                    market_state=market_state,
                    alert_mode=alert_mode,
                    selected=False,
                    remaining_alerts=remaining_alerts,
                    remaining_ai_scans=remaining_ai_scans,
                    market_blocked=market_blocked,
                    data_unavailable=True,
                )
            )
            continue
        prefilter = evaluate_rule_group(instance.get("prefilter_rules"), snapshot)
        prefilter["data_quality"] = data_quality
        alert_result = {"matched": False, "reason": "prefilter_not_matched", "data_quality": data_quality}
        if prefilter.get("matched"):
            alert_result = evaluate_rule_group(instance.get("alert_rules"), snapshot)
            alert_result["data_quality"] = data_quality
        evaluated_items.append(
            {
                "symbol": symbol,
                "snapshot": snapshot,
                "prefilter": prefilter,
                "alert_result": alert_result,
                "score": _scan_loop_alert_score(snapshot),
                "data_quality": data_quality,
            }
        )

    eligible_alert_indexes = [
        index
        for index, item in enumerate(evaluated_items)
        if bool(item.get("alert_result", {}).get("matched")) and bool(item.get("prefilter", {}).get("matched"))
    ]
    selected_alert_indexes: set[int] = set()
    if not market_blocked and alert_mode == "all_matches":
        selected_alert_indexes = set(eligible_alert_indexes)
    elif not market_blocked and alert_mode == "daily_digest":
        selected_alert_indexes = set(eligible_alert_indexes)
    elif not market_blocked and alert_mode == "best_per_run" and eligible_alert_indexes:
        selected_alert_indexes = {max(eligible_alert_indexes, key=lambda index: float(evaluated_items[index].get("score") or 0))}

    ai_selection = _scan_loop_ai_selection(evaluated_items, ai_scan_policy, remaining_ai_scans, ai_scan_top_n)
    _annotate_scan_loop_ai_decisions(evaluated_items, ai_selection, ai_scan_policy, ai_scan_top_n)
    result_items: list[dict[str, Any]] = []
    for index, item in enumerate(evaluated_items):
        if "status" in item:
            result_items.append(item)
            continue
        result_items.append(
            _scan_loop_test_item(
                owner,
                instance,
                item["symbol"],
                item["snapshot"],
                item["prefilter"],
                item["alert_result"],
                market_state=market_state,
                alert_mode=alert_mode,
                selected=index in selected_alert_indexes,
                remaining_alerts=remaining_alerts,
                remaining_ai_scans=remaining_ai_scans,
                market_blocked=market_blocked,
                ai_selected=index in ai_selection["selected_indexes"],
                ai_suppressed_reason=ai_selection["suppressed_reasons"].get(index),
                ai_scan_decision=item.get("ai_scan_decision") or {},
            )
        )

    prefilter_matched = sum(1 for item in result_items if item.get("prefilter_matched"))
    alert_matched = sum(1 for item in result_items if item.get("alert_matched"))
    would_submit_ai = sum(1 for item in result_items if item.get("would_submit_ai"))
    would_notify = sum(1 for item in result_items if item.get("would_notify"))
    data_unavailable = sum(1 for item in result_items if item.get("status") == "data_unavailable")
    return {
        "instance_id": instance_id,
        "instance_name": instance.get("name"),
        "generated_at": now,
        "market_state": market_state,
        "market_clock": clock,
        "allow_non_regular": allow_non_regular,
        "notification_policy": "blocked_market_not_regular" if market_blocked else "real_time_if_enabled",
        "daily_usage": daily_usage,
        "limits": {
            "max_alerts_per_day": int(instance.get("max_alerts_per_day") or 0),
            "max_ai_scans_per_day": int(instance.get("max_ai_scans_per_day") or 0),
            "remaining_alerts": remaining_alerts,
            "remaining_ai_scans": remaining_ai_scans,
            "symbol_cooldown_minutes": int(instance.get("symbol_cooldown_minutes") or 0),
            "ai_scan_policy": ai_scan_policy,
            "ai_scan_top_n": ai_scan_top_n,
        },
        "summary": {
            "symbols": len(instance.get("symbols") or []),
            "prefilter_matched": prefilter_matched,
            "alert_matched": alert_matched,
            "would_submit_ai": would_submit_ai,
            "would_notify": would_notify,
            "data_unavailable": data_unavailable,
            "market_blocked": market_blocked,
            "alert_mode": alert_mode,
            "ai_scan_policy": ai_scan_policy,
            "ai_scan_top_n": ai_scan_top_n,
            "ai_scan_candidate_count": ai_selection["candidate_count"],
            "ai_scan_selected_count": len(ai_selection["selected_indexes"]),
            "ai_scan_budget": {
                "max_per_day": int(instance.get("max_ai_scans_per_day") or 0),
                "used_before_run": int(daily_usage.get("ai_scans") or 0),
                "remaining_before_run": int(ai_selection.get("remaining_before_run") or 0),
                "used_this_run": len(ai_selection["selected_indexes"]),
                "remaining_after_run": max(remaining_ai_scans - len(ai_selection["selected_indexes"]), 0),
            },
        },
        "items": result_items,
        "data_quality": {
            "freshness_status": "partial_failed" if data_unavailable else "stale" if market_state != "regular_open" else "fresh",
            "explanations": [item["explanation"] for item in run_data_quality if item.get("explanation")][:6],
            "sources": sorted({str(item.get("source") or "") for item in run_data_quality if item.get("source")}),
            "items": run_data_quality,
        },
    }


def _scan_loop_test_item(
    owner: str,
    instance: dict[str, Any],
    symbol: str,
    snapshot: dict[str, Any],
    prefilter: dict[str, Any],
    alert_result: dict[str, Any],
    *,
    market_state: str,
    alert_mode: str,
    selected: bool,
    remaining_alerts: int,
    remaining_ai_scans: int,
    market_blocked: bool,
    data_unavailable: bool = False,
    ai_selected: bool = False,
    ai_suppressed_reason: str | None = None,
    ai_scan_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prefilter_matched = bool(prefilter.get("matched"))
    alert_matched = bool(alert_result.get("matched"))
    missing_fields = _rule_missing_fields(prefilter) + _rule_missing_fields(alert_result)
    suppressed_reasons: list[str] = []
    would_submit_ai = False
    would_notify = False
    status = "filtered"
    label = "未命中"
    if data_unavailable:
        status = "data_unavailable"
        label = "缺数据"
        suppressed_reasons.append("data_unavailable")
    elif not prefilter_matched:
        status = "prefilter_not_matched"
        label = "预筛未命中"
    else:
        would_submit_ai = bool(ai_selected)
        if not would_submit_ai:
            suppressed_reasons.append(ai_suppressed_reason or ("max_ai_scans_per_day" if remaining_ai_scans <= 0 else "ai_scan_policy_not_selected"))
        status = "prefilter_matched"
        label = "进入 AI 精扫" if would_submit_ai else "预筛命中"
        if not alert_matched:
            suppressed_reasons.append("alert_not_matched")
        elif market_blocked:
            suppressed_reasons.append("market_not_regular_open")
            status = "market_blocked"
            label = "市场未开"
        elif alert_mode == "silent_log":
            suppressed_reasons.append("alert_mode_silent_log")
            status = "silent_log"
            label = "只记录"
        elif alert_mode == "best_per_run" and not selected:
            suppressed_reasons.append("alert_mode_best_per_run")
            status = "best_per_run_not_selected"
            label = "非本轮最佳"
        elif remaining_alerts <= 0:
            suppressed_reasons.append("max_alerts_per_day")
            status = "limit_blocked"
            label = "额度限制"
        elif _scan_loop_symbol_cooldown_active(owner, str(instance.get("id") or ""), symbol, int(instance.get("symbol_cooldown_minutes") or 0)):
            suppressed_reasons.append("cooldown")
            status = "cooldown"
            label = "冷却中"
        else:
            would_notify = True
            status = "would_notify"
            label = "会提醒"
            if alert_mode == "daily_digest":
                label = "进入汇总"
    return {
        "symbol": symbol,
        "status": status,
        "label": label,
        "prefilter_matched": prefilter_matched,
        "alert_matched": alert_matched,
        "would_submit_ai": would_submit_ai,
        "would_notify": would_notify,
        "would_notify_when_regular": bool(alert_matched and prefilter_matched and not data_unavailable),
        "suppressed_reasons": suppressed_reasons,
        "missing_fields": sorted(set(missing_fields)),
        "score": _scan_loop_alert_score(snapshot),
        "ai_scan_decision": ai_scan_decision or {},
        "snapshot_summary": _scan_loop_snapshot_summary(snapshot),
        "data_quality": prefilter.get("data_quality") or alert_result.get("data_quality") or snapshot.get("data_quality") or {},
        "prefilter_result": _rule_group_public(prefilter),
        "alert_result": _rule_group_public(alert_result),
        "explanation": _scan_loop_test_explanation(label, suppressed_reasons, missing_fields),
    }


def _rule_missing_fields(result: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for check in result.get("checks") or []:
        if check.get("reason") == "unknown" or check.get("actual") is None:
            missing.append(str(check.get("field") or "unknown"))
    if result.get("reason") == "data_unavailable":
        missing.append("quote")
    return missing


def _rule_group_public(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "matched": bool(result.get("matched")),
        "logic": result.get("logic"),
        "reason": result.get("reason"),
        "checks": result.get("checks") or [],
    }


def _scan_loop_snapshot_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "last",
        "price",
        "rvol",
        "vwap",
        "underlying_vs_vwap_pct",
        "orb_high",
        "orb_low",
        "ema_20",
        "ema_50",
        "rsi",
        "atr",
        "ask",
        "bid",
        "bid_ask_spread_pct",
        "volume",
        "open_interest",
        "rv20",
        "rv60",
        "rv_rank",
        "volume_profile_poc",
        "volume_profile_value_area_low",
        "volume_profile_value_area_high",
        "volume_profile_position",
        "volume_profile_low_volume_room_up_pct",
        "volume_profile_low_volume_room_down_pct",
        "gex_regime",
        "gex_nearest_wall",
        "gex_nearest_wall_distance_pct",
    ]
    return {field: snapshot.get(field) for field in fields if snapshot.get(field) is not None}


def _scan_loop_test_explanation(label: str, suppressed_reasons: list[str], missing_fields: list[str]) -> str:
    if missing_fields:
        return f"{label}：缺少 {', '.join(sorted(set(missing_fields)))}。"
    if suppressed_reasons:
        labels = {
            "max_ai_scans_per_day": "AI 精扫额度不足",
            "alert_not_matched": "提醒规则未命中",
            "market_not_regular_open": "当前非常规交易时段",
            "alert_mode_silent_log": "实例设置为只记录",
            "alert_mode_best_per_run": "best_per_run 只选择本轮最高分",
            "max_alerts_per_day": "今日提醒额度已用完",
            "cooldown": "同一标的仍在冷却期",
            "data_unavailable": "行情数据不可用",
            "ai_scan_policy_alert_not_matched": "AI 策略要求提醒规则命中后才精扫",
            "ai_scan_policy_top_n": "未进入本轮 Top N 精扫池",
            "ai_scan_policy_smart_budget": "智能预算优先级不足",
            "ai_scan_policy_not_selected": "未被当前 AI 精扫策略选中",
        }
        return f"{label}：{' / '.join(labels.get(reason, reason) for reason in suppressed_reasons)}。"
    return f"{label}：规则链路完整。"


def list_scan_loop_runs(owner_id: str, instance_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    safe_limit = max(1, min(limit, 200))
    params: tuple[Any, ...]
    clause = "owner_id = ?"
    params = (owner, safe_limit)
    if instance_id:
        clause += " AND instance_id = ?"
        params = (owner, instance_id, safe_limit)
    with connect() as db:
        rows = db.execute(
            f"SELECT * FROM scan_loop_runs WHERE {clause} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_run_row(row) for row in rows]


def get_scan_loop_run(owner_id: str, run_id: str) -> dict[str, Any] | None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute("SELECT * FROM scan_loop_runs WHERE owner_id = ? AND id = ?", (owner, run_id)).fetchone()
        items = db.execute("SELECT * FROM scan_loop_run_items WHERE owner_id = ? AND run_id = ? ORDER BY created_at ASC", (owner, run_id)).fetchall() if row else []
    if not row:
        return None
    result = _run_row(row)
    result["items"] = [_run_item_row(item) for item in items]
    return result


def create_lightweight_opportunity(
    owner_id: str,
    instance: dict[str, Any],
    run_id: str,
    symbol: str,
    scan_id: str | None,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    opportunity_id = uuid.uuid4().hex
    now = utc_now()
    direction = _opportunity_direction(snapshot)
    strategy = _opportunity_strategy_structure(instance, direction, snapshot)
    entry_reference = _opportunity_entry_reference(snapshot, direction, strategy)
    risk_plan = _opportunity_risk_plan(snapshot, direction, strategy, entry_reference)
    legs = _opportunity_legs(snapshot, direction, strategy, entry_reference)
    payoff = _opportunity_payoff(snapshot, direction, strategy, entry_reference, risk_plan, legs)
    validation = _opportunity_validation(strategy, legs, payoff, _opportunity_gex_snapshot(snapshot))
    title = f"{symbol} {_opportunity_direction_label(direction)} {_opportunity_strategy_label(strategy)}"
    opportunity_payload = {
        "direction": direction,
        "strategy_structure": strategy,
        "strategy_type": strategy,
        "thesis": f"{symbol} {direction} / {strategy}",
        "contract_symbol": None,
        "legs_json": json.dumps(legs, ensure_ascii=False),
        "payoff_json": json.dumps(payoff, ensure_ascii=False),
        "validation_json": json.dumps(validation, ensure_ascii=False),
        "notification_channel_ids_json": json.dumps(instance.get("notification_channel_ids") or [], ensure_ascii=False),
        "followup_enabled": 1,
        "followup_interval_seconds": int(instance.get("followup_interval_seconds") or 300),
        "cooldown_seconds": int(instance.get("cooldown_seconds") or 1800),
        "max_followup_alerts": int(instance.get("max_followup_alerts") or 6),
        "expires_at": instance.get("expires_at"),
    }
    with connect() as db:
        db.execute(
            """
            INSERT INTO opportunity_instances
                (id, owner_id, source_type, source_id, scan_id, scan_loop_instance_id, watchlist_id, symbol, title,
                 direction, strategy_structure, contract_symbol, strategy_type, ai_direction, derived_direction,
                 thesis, legs_json, payoff_json, validation_json, entry_reference_json, risk_plan_json,
                 trigger_snapshot_json, gex_snapshot_json, notification_channel_ids_json, followup_enabled,
                 followup_interval_seconds, cooldown_seconds, max_followup_alerts, followup_alert_count,
                 last_checked_at, next_check_at, last_alert_at, expires_at, created_at, updated_at)
            VALUES (?, ?, 'scan_loop_run', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_id,
                normalize_owner_id(owner_id),
                run_id,
                scan_id,
                instance["id"],
                instance.get("watchlist_id"),
                symbol,
                title,
                direction,
                strategy,
                opportunity_payload["contract_symbol"],
                opportunity_payload["strategy_type"],
                None,
                None,
                opportunity_payload["thesis"],
                opportunity_payload["legs_json"],
                opportunity_payload["payoff_json"],
                opportunity_payload["validation_json"],
                json.dumps(entry_reference, ensure_ascii=False),
                json.dumps(risk_plan, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
                json.dumps(_opportunity_gex_snapshot(snapshot), ensure_ascii=False),
                opportunity_payload["notification_channel_ids_json"],
                opportunity_payload["followup_enabled"],
                opportunity_payload["followup_interval_seconds"],
                opportunity_payload["cooldown_seconds"],
                opportunity_payload["max_followup_alerts"],
                0,
                None,
                None,
                None,
                opportunity_payload["expires_at"],
                now,
                now,
            ),
        )
    create_opportunity_event(
        owner_id,
        opportunity_id,
        "created",
        f"{symbol} 机会已创建",
        f"{title} 已记录，后续可重点跟踪。",
        {"source_id": run_id, "symbol": symbol, "direction": direction, "strategy_structure": strategy},
    )
    return {
        "id": opportunity_id,
        "symbol": symbol,
        "title": title,
        "direction": direction,
        "strategy_structure": strategy,
        "entry_reference": entry_reference,
        "risk_plan": risk_plan,
        "legs": legs,
        "payoff": payoff,
        "validation": validation,
    }


def list_opportunities(owner_id: str, limit: int = 30) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    safe_limit = max(1, min(limit, 200))
    fetch_limit = max(safe_limit, min(200, safe_limit * 3))
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM opportunity_instances WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?",
            (owner, fetch_limit),
        ).fetchall()
        items = [_opportunity_row(row) for row in rows]
        for item in items:
            event = db.execute(
                "SELECT * FROM opportunity_events WHERE owner_id = ? AND opportunity_id = ? ORDER BY created_at DESC LIMIT 1",
                (owner, item["id"]),
            ).fetchone()
            item["latest_event"] = _opportunity_event_row(event) if event else None
    items.sort(key=lambda item: (int((item.get("action_priority") or {}).get("score") or 0), item.get("created_at") or ""), reverse=True)
    return items[:safe_limit]


def list_due_opportunities(owner_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    init_observation_db()
    safe_limit = max(1, min(limit, 200))
    now = utc_now()
    params: list[Any] = []
    terminal_statuses = "', '".join(sorted(OPPORTUNITY_TERMINAL_STATUSES))
    clause = f"followup_enabled = 1 AND status NOT IN ('{terminal_statuses}')"
    if owner_id:
        clause += " AND owner_id = ?"
        params.append(normalize_owner_id(owner_id))
    params.extend([now, safe_limit])
    with connect() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM opportunity_instances
            WHERE {clause}
              AND (next_check_at IS NULL OR next_check_at <= ?)
            ORDER BY COALESCE(next_check_at, created_at) ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_opportunity_row(row) for row in rows]


def get_opportunity(owner_id: str, opportunity_id: str) -> dict[str, Any] | None:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        row = db.execute("SELECT * FROM opportunity_instances WHERE owner_id = ? AND id = ?", (owner, opportunity_id)).fetchone()
        events = db.execute(
            "SELECT * FROM opportunity_events WHERE owner_id = ? AND opportunity_id = ? ORDER BY created_at DESC",
            (owner, opportunity_id),
        ).fetchall() if row else []
    if not row:
        return None
    result = _opportunity_row(row)
    result["events"] = [_opportunity_event_row(event) for event in events]
    return result


def list_opportunity_events(owner_id: str, opportunity_id: str, limit: int = 50) -> list[dict[str, Any]]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM opportunity_events WHERE owner_id = ? AND opportunity_id = ? ORDER BY created_at DESC LIMIT ?",
            (owner, opportunity_id, max(1, min(limit, 200))),
        ).fetchall()
    return [_opportunity_event_row(row) for row in rows]


def update_opportunity(owner_id: str, opportunity_id: str, payload: dict[str, Any], *, record_event: bool = True) -> dict[str, Any]:
    current = get_opportunity(owner_id, opportunity_id)
    if not current:
        raise ValueError("opportunity not found")
    merged = {**current, **payload}
    now = utc_now()
    with connect() as db:
        db.execute(
            """
            UPDATE opportunity_instances
            SET status = ?, title = ?, thesis = ?, risk_plan_json = ?, notification_channel_ids_json = ?, followup_enabled = ?,
                followup_interval_seconds = ?, cooldown_seconds = ?, max_followup_alerts = ?, followup_alert_count = ?,
                last_checked_at = ?, next_check_at = ?, last_alert_at = ?, expires_at = ?, updated_at = ?
            WHERE owner_id = ? AND id = ?
            """,
            (
                str(merged.get("status") or current["status"]),
                str(merged.get("title") or current["title"])[:200],
                merged.get("thesis", current.get("thesis")),
                json.dumps(merged.get("risk_plan") or current.get("risk_plan") or {}, ensure_ascii=False),
                json.dumps(merged.get("notification_channel_ids") or current.get("notification_channel_ids") or [], ensure_ascii=False),
                int(bool(merged.get("followup_enabled", current.get("followup_enabled", 1)))),
                int(merged.get("followup_interval_seconds") or current.get("followup_interval_seconds") or 300),
                int(merged.get("cooldown_seconds") or current.get("cooldown_seconds") or 1800),
                int(merged.get("max_followup_alerts") or current.get("max_followup_alerts") or 6),
                int(merged.get("followup_alert_count") or current.get("followup_alert_count") or 0),
                merged.get("last_checked_at", current.get("last_checked_at")),
                merged.get("next_check_at", current.get("next_check_at")),
                merged.get("last_alert_at", current.get("last_alert_at")),
                merged.get("expires_at", current.get("expires_at")),
                now,
                normalize_owner_id(owner_id),
                opportunity_id,
            ),
        )
    updated = get_opportunity(owner_id, opportunity_id) or current
    if record_event:
        create_opportunity_event(
            owner_id,
            opportunity_id,
            "updated",
            f"{updated['symbol']} 机会已更新",
            f"状态更新为 {updated['status']}。",
            {"status": updated["status"], "followup_enabled": updated.get("followup_enabled")},
        )
    return updated


def check_opportunity_followup(owner_id: str, opportunity_id: str, quote_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    opportunity = get_opportunity(owner_id, opportunity_id)
    if not opportunity:
        raise ValueError("opportunity not found")
    snapshot = quote_snapshot or opportunity.get("trigger_snapshot") or {}
    now = utc_now()
    current_price = _opportunity_price_from_snapshot(snapshot)
    entry_price = _opportunity_entry_price(opportunity)
    direction = str(opportunity.get("direction") or "unknown").strip().lower()
    status_before = str(opportunity.get("status") or "created")
    market_state = str(snapshot.get("market_state") or snapshot.get("session_state") or "regular_open").strip().lower()
    gex_changes = _opportunity_gex_changes(opportunity, snapshot)
    expired = _opportunity_expired(opportunity, now)
    invalidated, invalidation_reason = _opportunity_invalidated(opportunity, snapshot, current_price)
    zone = "review"
    body = "机会仍有效，但请重新确认成交价格。"
    status_after = status_before
    event_type = "review"
    if expired:
        zone = "expired"
        body = "机会已超过参考有效期，建议生成复盘。"
        status_after = "expired"
        event_type = "expired"
    elif invalidated:
        zone = "invalidated"
        body = f"原始机会假设失效：{invalidation_reason}"
        status_after = "invalidated"
        event_type = "invalidated"
    elif current_price is not None and market_state in {"regular_open", "open", "intraday"}:
        tp_zone, tp_label = _opportunity_take_profit_zone(opportunity, current_price)
        sl_hit, sl_label = _opportunity_stop_loss_hit(opportunity, current_price)
        if tp_zone:
            zone = "take_profit_zone"
            body = f"达到参考止盈区间，{tp_label}。"
            status_after = "take_profit_zone"
            event_type = "take_profit"
        elif sl_hit:
            zone = "stop_loss_zone"
            body = f"触及参考止损条件，{sl_label}。"
            status_after = "stop_loss_zone"
            event_type = "stop_loss"
        elif gex_changes:
            zone = "gex_shift"
            body = f"GEX 结构发生变化：{_opportunity_gex_change_summary(gex_changes)}。请重新评估原机会假设。"
            status_after = _opportunity_continuation_status(status_before)
            event_type = "gex_change"
        else:
            zone = _opportunity_continuation_status(status_before)
            body = "机会仍有效，但请重新确认成交价格。"
            status_after = zone
    elif market_state in {"closed", "closed_today", "afterhours"}:
        zone = "eod_review"
        body = _opportunity_eod_plan_body(opportunity, snapshot, gex_changes)
        status_after = status_before if status_before not in {"created"} else "watching_entry"
        event_type = "eod_review"
    elif market_state in {"weekend", "holiday", "non_trading_day"}:
        zone = "weekend_plan"
        body = _opportunity_weekend_plan_body(opportunity, snapshot, gex_changes)
        status_after = status_before if status_before not in {"created"} else "watching_entry"
        event_type = "weekend_plan"
    elif gex_changes:
        zone = "gex_shift"
        body = f"GEX 结构发生变化：{_opportunity_gex_change_summary(gex_changes)}。请重新评估原机会假设。"
        status_after = _opportunity_continuation_status(status_before)
        event_type = "gex_change"
    payload = {
        "quote_snapshot": snapshot,
        "current_price": current_price,
        "entry_price": entry_price,
        "direction": direction,
        "market_state": market_state,
        "status_before": status_before,
        "status_after": status_after,
        "zone": zone,
        "gex_initial": opportunity.get("gex_snapshot") or {},
        "gex_current": _opportunity_gex_snapshot(snapshot),
        "gex_changes": gex_changes,
    }
    if event_type == "weekend_plan":
        payload["weekend_plan"] = _opportunity_weekend_plan(opportunity, snapshot, gex_changes)
    elif event_type == "eod_review":
        payload["next_session_plan"] = _opportunity_next_session_plan(opportunity, snapshot, gex_changes)
    dedupe_key = str(snapshot.get("followup_dedupe_key") or snapshot.get("_followup_dedupe_key") or "").strip()
    if dedupe_key:
        payload["dedupe_key"] = dedupe_key
        if _opportunity_event_dedupe_exists(owner_id, opportunity_id, dedupe_key):
            return {"opportunity": opportunity, "event": None, "zone": zone, "body": body, "current_price": current_price, "entry_price": entry_price, "market_state": market_state, "skipped": True, "reason": "deduped"}
    event = create_opportunity_event(owner_id, opportunity_id, event_type, f"{opportunity['symbol']} 机会{_opportunity_zone_label(zone)}", body, payload)
    notification_result = _create_opportunity_followup_notification(
        owner_id,
        opportunity,
        event,
        event_type,
        zone,
        body,
        payload,
        dedupe_key,
    )
    updates: dict[str, Any] = {
        "status": status_after,
        "last_checked_at": now,
        "next_check_at": None if status_after in OPPORTUNITY_TERMINAL_STATUSES else _utc_after_seconds(int(opportunity.get("followup_interval_seconds") or 300)),
        "last_alert_at": now if event_type != "review" else opportunity.get("last_alert_at"),
    }
    if event_type != "review":
        updates["followup_alert_count"] = int(opportunity.get("followup_alert_count") or 0) + 1
    updated = update_opportunity(owner_id, opportunity_id, updates, record_event=False)
    result = {"opportunity": updated, "event": event, "zone": zone, "body": body, "current_price": current_price, "entry_price": entry_price, "market_state": market_state}
    if notification_result.get("event"):
        result["notification_event"] = notification_result["event"]
    if notification_result.get("suppressed_reason"):
        result["notification_suppressed_reason"] = notification_result["suppressed_reason"]
    return result


def _create_opportunity_followup_notification(
    owner_id: str,
    opportunity: dict[str, Any],
    event: dict[str, Any],
    event_type: str,
    zone: str,
    body: str,
    payload: dict[str, Any],
    followup_dedupe_key: str,
) -> dict[str, Any]:
    if event_type == "review":
        return {"event": None, "suppressed_reason": "review_only"}
    max_alerts = int(opportunity.get("max_followup_alerts") or 0)
    current_count = int(opportunity.get("followup_alert_count") or 0)
    if max_alerts > 0 and current_count >= max_alerts:
        return {"event": None, "suppressed_reason": "max_followup_alerts"}
    if not event or not event.get("id"):
        return {"event": None, "suppressed_reason": "missing_opportunity_event"}

    dedupe_token = followup_dedupe_key or event.get("id")
    notification_events = create_notification_events(
        owner_id,
        source_type="opportunity_followup",
        source_id=str(opportunity.get("id") or event.get("opportunity_id") or ""),
        dedupe_key=f"opportunity-followup:{opportunity.get('id')}:{event_type}:{dedupe_token}",
        title=str(event.get("title") or f"{opportunity.get('symbol', '')} 机会提醒")[:220],
        body=f"{body}\n\n此提醒仅用于研究辅助，不构成投资建议、交易建议或收益承诺。",
        payload={
            "opportunity_id": opportunity.get("id"),
            "opportunity_event_id": event.get("id"),
            "event_type": event_type,
            "zone": zone,
            "symbol": opportunity.get("symbol"),
            "status_before": payload.get("status_before"),
            "status_after": payload.get("status_after"),
            "market_state": payload.get("market_state"),
            "current_price": payload.get("current_price"),
            "entry_price": payload.get("entry_price"),
            "direction": payload.get("direction"),
            "strategy_structure": opportunity.get("strategy_structure"),
            "gex_changes": payload.get("gex_changes") or [],
            "gex_initial": payload.get("gex_initial") or {},
            "gex_current": payload.get("gex_current") or {},
            "weekend_plan": payload.get("weekend_plan"),
            "next_session_plan": payload.get("next_session_plan"),
            "followup_dedupe_key": followup_dedupe_key,
        },
        channel_ids=opportunity.get("notification_channel_ids") or [],
    )
    notification_events = _dispatch_notification_events(owner_id, notification_events)
    return {"event": notification_events[0] if notification_events else None, "notification_events": notification_events}


def process_due_opportunity_followups(owner_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    opportunities = list_due_opportunities(owner_id, limit=limit)
    clock = market_clock()
    market_state = _market_state(clock)
    processed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    snapshot_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for opportunity in opportunities:
        symbol = str(opportunity.get("symbol") or "").strip().upper()
        if not symbol:
            skipped.append({"id": opportunity.get("id"), "reason": "missing_symbol"})
            continue
        owner = str(opportunity.get("owner_id") or owner_id or "local")
        instance = get_scan_loop_instance(owner, str(opportunity.get("scan_loop_instance_id") or "")) if opportunity.get("scan_loop_instance_id") else None
        source = str((instance or {}).get("option_data_source") or "thetadata").strip().lower()
        cache_key = (symbol, source)
        if market_state == "regular_open":
            snapshot = snapshot_cache.get(cache_key)
            if snapshot is None:
                snapshot = _fetch_quote_snapshot(symbol)
                snapshot.update(_fetch_current_gex_snapshot(symbol, source, _numeric(snapshot.get("last"))))
                snapshot_cache[cache_key] = snapshot
        else:
            snapshot = dict(opportunity.get("trigger_snapshot") or {})
        snapshot["symbol"] = symbol
        snapshot["market_state"] = market_state
        snapshot["market_clock"] = clock
        snapshot["followup_source"] = "opportunity_scheduler"
        if market_state != "regular_open" or _opportunity_gex_changes(opportunity, snapshot):
            snapshot["followup_dedupe_key"] = _opportunity_followup_dedupe_key(opportunity, snapshot, market_state)
        result = check_opportunity_followup(owner, opportunity["id"], quote_snapshot=snapshot)
        if result.get("skipped"):
            skipped.append({"id": opportunity["id"], "symbol": symbol, "reason": result.get("reason"), "zone": result.get("zone")})
        else:
            processed.append({"id": opportunity["id"], "symbol": symbol, "zone": result.get("zone"), "event_type": (result.get("event") or {}).get("event_type")})
    return {
        "market_state": market_state,
        "checked_count": len(opportunities),
        "processed_count": len(processed),
        "skipped_count": len(skipped),
        "processed": processed,
        "skipped": skipped,
    }


def pause_opportunity(owner_id: str, opportunity_id: str) -> dict[str, Any]:
    opportunity = get_opportunity(owner_id, opportunity_id)
    if not opportunity:
        raise ValueError("opportunity not found")
    return update_opportunity(owner_id, opportunity_id, {"followup_enabled": False})


def resume_opportunity(owner_id: str, opportunity_id: str) -> dict[str, Any]:
    opportunity = get_opportunity(owner_id, opportunity_id)
    if not opportunity:
        raise ValueError("opportunity not found")
    next_status = opportunity["status"] if opportunity["status"] in {"watching_entry", "triggered", "active_reference", "tracking_reference"} else "watching_entry"
    return update_opportunity(owner_id, opportunity_id, {"followup_enabled": True, "status": next_status})


def archive_opportunity(owner_id: str, opportunity_id: str) -> dict[str, Any]:
    opportunity = get_opportunity(owner_id, opportunity_id)
    if not opportunity:
        raise ValueError("opportunity not found")
    return update_opportunity(owner_id, opportunity_id, {"status": "archived", "followup_enabled": False})


def create_opportunity_event(
    owner_id: str,
    opportunity_id: str,
    event_type: str,
    title: str,
    body: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_observation_db()
    owner = normalize_owner_id(owner_id)
    event_id = uuid.uuid4().hex
    now = utc_now()
    payload = payload or {}
    with connect() as db:
        db.execute(
            """
            INSERT INTO opportunity_events (id, owner_id, opportunity_id, event_type, title, body, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                owner,
                opportunity_id,
                str(event_type or "note"),
                str(title or "机会事件"),
                body,
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
    return {"id": event_id, "owner_id": owner, "opportunity_id": opportunity_id, "event_type": event_type, "title": title, "body": body, "payload": payload, "created_at": now}


def _record_trigger_opportunity_event(
    owner_id: str,
    trigger: dict[str, Any] | None,
    *,
    event_type: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not trigger:
        return None
    opportunity_id = str(trigger.get("opportunity_id") or "").strip()
    if not opportunity_id:
        return None
    opportunity = get_opportunity(owner_id, opportunity_id)
    if not opportunity:
        return None
    now = utc_now()
    status_before = str(opportunity.get("status") or "created")
    status_after = "triggered" if status_before not in OPPORTUNITY_TERMINAL_STATUSES else status_before
    event_payload = {
        "trigger_id": trigger.get("id"),
        "trigger_name": trigger.get("name"),
        "trigger_status": trigger.get("status"),
        "symbol": trigger.get("symbol"),
        "opportunity_id": opportunity_id,
        "opportunity_title": opportunity.get("title"),
        "status_before": status_before,
        "status_after": status_after,
        "lifecycle_phase": _opportunity_lifecycle_phase(status_after),
        "triggered_at": now,
    }
    if payload:
        event_payload.update(payload)
    event = create_opportunity_event(owner_id, opportunity_id, event_type, title, body, event_payload)
    if event_type == "trigger_matched" and status_after == "triggered":
        update_opportunity(
            owner_id,
            opportunity_id,
            {
                "status": "triggered",
                "followup_enabled": True,
                "last_alert_at": now,
                "next_check_at": _utc_after_seconds(_triggered_followup_seconds(opportunity)),
            },
            record_event=False,
        )
    return event


def _triggered_followup_seconds(opportunity: dict[str, Any]) -> int:
    interval = int(opportunity.get("followup_interval_seconds") or 300)
    return max(30, min(interval, 120))


def _opportunity_continuation_status(status_before: str) -> str:
    if status_before == "triggered":
        return "tracking_reference"
    if status_before in {"created", "watching_entry"}:
        return "active_reference"
    return status_before or "active_reference"


def _opportunity_event_dedupe_exists(owner_id: str, opportunity_id: str, dedupe_key: str) -> bool:
    if not dedupe_key:
        return False
    for event in list_opportunity_events(owner_id, opportunity_id, limit=100):
        if str((event.get("payload") or {}).get("dedupe_key") or "") == dedupe_key:
            return True
    return False


def _opportunity_price_from_snapshot(snapshot: dict[str, Any]) -> float | None:
    for field in ("last", "close", "mid", "mark", "price", "ask", "bid"):
        value = _numeric(snapshot.get(field))
        if value is not None:
            return value
    return None


def _opportunity_entry_price(opportunity: dict[str, Any]) -> float | None:
    entry = opportunity.get("entry_reference") or {}
    for field in ("underlying_reference", "entry_reference"):
        value = _numeric(entry.get(field))
        if value is not None:
            return value
    return _numeric((opportunity.get("trigger_snapshot") or {}).get("last"))


def _opportunity_take_profit_zone(opportunity: dict[str, Any], current_price: float) -> tuple[bool, str]:
    risk = opportunity.get("risk_plan") or {}
    entry_price = _opportunity_entry_price(opportunity) or current_price
    direction = str(opportunity.get("direction") or "unknown").strip().lower()
    levels = (risk.get("take_profit") or {}).get("levels") or []
    labels: list[str] = []
    for level in levels:
        threshold = _numeric((level or {}).get("underlying_reference"))
        if threshold is None and _numeric((level or {}).get("pct")) is not None:
            threshold = _opportunity_price_from_pct(entry_price, direction, _numeric((level or {}).get("pct")) or 0.0)
        if threshold is None:
            continue
        label = str((level or {}).get("label") or "TP").strip() or "TP"
        labels.append(label)
        if direction == "bearish" and current_price <= threshold:
            return True, f"{label} 已达成"
        if direction != "bearish" and current_price >= threshold:
            return True, f"{label} 已达成"
    return False, "未达成"


def _opportunity_stop_loss_hit(opportunity: dict[str, Any], current_price: float) -> tuple[bool, str]:
    risk = opportunity.get("risk_plan") or {}
    stop = (risk.get("stop_loss") or {})
    threshold = _numeric(stop.get("underlying_reference"))
    if threshold is None and _numeric(stop.get("pct")) is not None:
        threshold = _opportunity_price_from_pct(_opportunity_entry_price(opportunity) or current_price, str(opportunity.get("direction") or "unknown"), _numeric(stop.get("pct")) or 0.0)
    if threshold is None:
        return False, "未定义止损"
    direction = str(opportunity.get("direction") or "unknown").strip().lower()
    if direction == "bearish":
        return current_price >= threshold, f"参考止损位 {threshold:.2f}"
    return current_price <= threshold, f"参考止损位 {threshold:.2f}"


def _opportunity_invalidated(opportunity: dict[str, Any], snapshot: dict[str, Any], current_price: float | None) -> tuple[bool, str]:
    if _numeric(snapshot.get("decision_score")) is not None and _numeric(snapshot.get("decision_score")) < 65:
        return True, "决策分数低于阈值"
    vwap = _numeric(snapshot.get("vwap"))
    if vwap is not None and current_price is not None:
        delta_pct = (current_price / vwap - 1) * 100
        direction = str(opportunity.get("direction") or "unknown").strip().lower()
        if direction == "bearish" and delta_pct >= 0.4:
            return True, "跌回 VWAP/ORB 假设失效"
        if direction != "bearish" and delta_pct <= -0.4:
            return True, "跌回 VWAP/ORB 假设失效"
    invalidation = (opportunity.get("risk_plan") or {}).get("invalidation")
    if isinstance(invalidation, list):
        for rule in invalidation:
            if not isinstance(rule, dict):
                continue
            field = str(rule.get("field") or "")
            operator = str(rule.get("operator") or "")
            value = _numeric(rule.get("value"))
            if field == "decision_score" and value is not None:
                score = _numeric(snapshot.get("decision_score"))
                if score is not None and operator == "<" and score < value:
                    return True, "决策分数低于阈值"
            if field == "underlying_vs_vwap_pct" and value is not None:
                delta = _numeric(snapshot.get("underlying_vs_vwap_pct"))
                if delta is None and _numeric(snapshot.get("vwap")) is not None and current_price is not None:
                    delta = (current_price / _numeric(snapshot.get("vwap")) - 1) * 100
                if delta is not None and operator in {"<=", "<"} and delta <= value:
                    return True, "跌回 VWAP/ORB 假设失效"
    return False, "未失效"


def _opportunity_price_from_pct(entry_price: float, direction: str, pct: float) -> float:
    if direction == "bearish":
        return round(entry_price * (1 - pct / 100), 2)
    return round(entry_price * (1 + pct / 100), 2)


def _opportunity_expired(opportunity: dict[str, Any], now: str) -> bool:
    expires_at = opportunity.get("expires_at")
    parsed = parse_datetime(expires_at)
    if parsed is None:
        return False
    current = parse_datetime(now)
    if current is None:
        return False
    return current.astimezone(timezone.utc) > parsed.astimezone(timezone.utc)


def _opportunity_zone_label(zone: str) -> str:
    return {
        "review": "复盘",
        "gex_shift": "GEX 变化提醒",
        "eod_review": "收盘复盘",
        "weekend_plan": "周末计划",
        "take_profit_zone": "止盈提醒",
        "stop_loss_zone": "止损提醒",
        "invalidated": "失效提醒",
        "expired": "过期提醒",
        "active_reference": "有效提醒",
        "tracking_reference": "追踪提醒",
    }.get(zone, "复盘")


def _opportunity_status_label(status: str) -> str:
    return {
        "created": "新机会",
        "watching_entry": "观望入场",
        "triggered": "已触发",
        "active_reference": "有效参考",
        "tracking_reference": "追踪中",
        "take_profit_zone": "止盈区",
        "stop_loss_zone": "止损区",
        "invalidated": "假设失效",
        "expired": "已过期",
        "archived": "已归档",
    }.get(status, status or "未知")


def _opportunity_lifecycle_phase(status: str) -> str:
    if status in {"created", "watching_entry"}:
        return "watching"
    if status == "triggered":
        return "triggered"
    if status in {"active_reference", "tracking_reference"}:
        return "tracking"
    if status in OPPORTUNITY_TERMINAL_STATUSES:
        return "exited"
    return "watching"


def _opportunity_lifecycle_step(status: str) -> int:
    phase = _opportunity_lifecycle_phase(status)
    return {"watching": 1, "triggered": 2, "tracking": 3, "exited": 4}.get(phase, 1)


def _opportunity_next_action(status: str) -> str:
    return {
        "created": "等待预设 Trigger 或手动标记为观察。",
        "watching_entry": "等待入场触发，命中后复核价格、IV、价差和事件风险。",
        "triggered": "触发已命中，优先复核入场条件并进入高频追踪。",
        "active_reference": "机会仍有效，继续等待更好的触发或人工确认。",
        "tracking_reference": "按止盈、止损、GEX 变化和最晚退出持续复核。",
        "take_profit_zone": "到达参考止盈区，记录结果并决定是否归档。",
        "stop_loss_zone": "触及参考止损区，停止追踪并复盘触发质量。",
        "invalidated": "原始假设失效，停止追踪并复盘失效原因。",
        "expired": "机会过期，归档或重新扫描生成新机会。",
        "archived": "已归档，不再自动追踪。",
    }.get(status, "复核机会状态和风险计划。")


def _opportunity_action_priority(
    status: str,
    *,
    followup_enabled: bool,
    next_check_at: str | None,
    last_alert_at: str | None,
    trigger_snapshot: dict[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    score = {
        "triggered": 88,
        "tracking_reference": 76,
        "take_profit_zone": 92,
        "stop_loss_zone": 95,
        "invalidated": 84,
        "active_reference": 64,
        "watching_entry": 56,
        "created": 48,
        "expired": 24,
        "archived": 8,
    }.get(status, 40)
    if status in {"triggered", "take_profit_zone", "stop_loss_zone", "invalidated"}:
        reasons.append(_opportunity_status_label(status))
    if not followup_enabled:
        score -= 22
        reasons.append("跟踪已暂停")
    due = _opportunity_followup_due(next_check_at, followup_enabled, status)
    if due:
        score += 12
        reasons.append("复核到期")
    freshness = str(
        trigger_snapshot.get("freshness_status")
        or trigger_snapshot.get("data_status")
        or (trigger_snapshot.get("data_quality") or {}).get("status")
        or ""
    ).strip().lower()
    if freshness in {"data_unavailable", "unavailable", "failed"}:
        score -= 28
        reasons.append("行情不可用")
    elif freshness == "stale":
        score -= 8
        reasons.append("行情延迟")
    spread = _numeric(trigger_snapshot.get("bid_ask_spread_pct"))
    if spread is not None and spread >= 15:
        score += 6
        reasons.append("期权价差偏宽")
    event_risk = str(trigger_snapshot.get("event_risk") or trigger_snapshot.get("earnings_risk") or "").strip().lower()
    if event_risk in {"high", "earnings", "event", "major_event"}:
        score += 7
        reasons.append("事件风险")
    if last_alert_at and status in {"triggered", "take_profit_zone", "stop_loss_zone", "invalidated"}:
        reasons.append("已有提醒")
    score = max(0, min(100, int(score)))
    label = "紧急复核" if score >= 88 else "优先跟踪" if score >= 70 else "正常观察" if score >= 40 else "低优先级"
    return {
        "score": score,
        "label": label,
        "reasons": reasons[:5],
        "followup_due": due,
    }


def _opportunity_followup_due(next_check_at: str | None, followup_enabled: bool, status: str) -> bool:
    if not followup_enabled or status in OPPORTUNITY_TERMINAL_STATUSES:
        return False
    if not next_check_at:
        return True
    parsed = parse_datetime(next_check_at)
    current = parse_datetime(utc_now())
    if parsed is None or current is None:
        return False
    return parsed.astimezone(timezone.utc) <= current.astimezone(timezone.utc)


def _scan_loop_symbol_cooldown_active(owner_id: str, instance_id: str, symbol: str, cooldown_minutes: int) -> bool:
    if cooldown_minutes <= 0:
        return False
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)).isoformat()
    with connect() as db:
        rows = db.execute(
            """
            SELECT payload_json
            FROM notification_events
            WHERE owner_id = ? AND source_type = 'scan_loop_run' AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (normalize_owner_id(owner_id), threshold),
        ).fetchall()
    normalized_symbol = str(symbol or "").strip().upper()
    for row in rows:
        payload = _loads(row["payload_json"], {})
        if str(payload.get("instance_id") or "") == instance_id and str(payload.get("symbol") or "").upper() == normalized_symbol:
            return True
    return False


def _scan_loop_daily_usage(owner_id: str, instance_id: str) -> dict[str, int]:
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with connect() as db:
        row = db.execute(
            """
            SELECT
                SUM(CASE WHEN scan_id IS NOT NULL AND scan_id != '' THEN 1 ELSE 0 END) AS ai_scans,
                SUM(CASE WHEN triggered = 1 THEN 1 ELSE 0 END) AS alerts
            FROM scan_loop_run_items
            WHERE owner_id = ? AND instance_id = ? AND created_at >= ?
            """,
            (normalize_owner_id(owner_id), instance_id, day_start),
        ).fetchone()
    return {
        "ai_scans": int(row["ai_scans"] or 0) if row else 0,
        "alerts": int(row["alerts"] or 0) if row else 0,
    }


def _normalize_ai_scan_policy(value: Any) -> str:
    policy = str(value or "prefilter_matched").strip().lower()
    aliases = {
        "prefilter": "prefilter_matched",
        "prefilter_only": "prefilter_matched",
        "alert": "alert_matched",
        "alert_only": "alert_matched",
        "top_n": "top_n_per_run",
        "top_n_per_run": "top_n_per_run",
        "smart": "smart_budget",
        "smart_budget": "smart_budget",
        "every": "always",
        "every_scan": "always",
        "all": "always",
        "always": "always",
    }
    return aliases.get(policy, policy if policy in {"prefilter_matched", "alert_matched", "always"} else "prefilter_matched")


def _scan_loop_ai_top_n(value: Any) -> int:
    try:
        number = int(value or 3)
    except (TypeError, ValueError):
        number = 3
    return max(1, min(number, 50))


def _scan_loop_ai_selection(
    evaluated_items: list[dict[str, Any]],
    policy: str,
    remaining_ai_scans: int,
    top_n: int,
) -> dict[str, Any]:
    normalized_policy = _normalize_ai_scan_policy(policy)
    safe_remaining = max(int(remaining_ai_scans or 0), 0)
    matched_indexes = [
        index
        for index, item in enumerate(evaluated_items)
        if item.get("status") not in {"data_unavailable", "filtered"} and bool((item.get("prefilter") or {}).get("matched"))
    ]
    suppressed: dict[int, str] = {}
    if safe_remaining <= 0:
        return {
            "selected_indexes": set(),
            "suppressed_reasons": {index: "max_ai_scans_per_day" for index in matched_indexes},
            "candidate_indexes": matched_indexes,
            "ranked_indexes": matched_indexes,
            "candidate_count": len(matched_indexes),
            "remaining_before_run": safe_remaining,
        }
    if normalized_policy == "always":
        # Bypass prefilter — every evaluable symbol gets fresh AI participation
        # subject only to the per-day budget. Designed for single-symbol radars
        # (e.g. SPY 通用盘中) where the user wants continuous AI narration.
        candidates = [
            index
            for index, item in enumerate(evaluated_items)
            if item.get("status") not in {"data_unavailable", "filtered"}
        ]
        selected = set(candidates[:safe_remaining])
        for index in candidates[safe_remaining:]:
            suppressed[index] = "max_ai_scans_per_day"
        return {
            "selected_indexes": selected,
            "suppressed_reasons": suppressed,
            "candidate_indexes": candidates,
            "ranked_indexes": candidates,
            "candidate_count": len(candidates),
            "remaining_before_run": safe_remaining,
        }
    if normalized_policy == "alert_matched":
        candidates = [index for index in matched_indexes if _scan_loop_item_alert_matched(evaluated_items[index])]
        for index in matched_indexes:
            if index not in candidates:
                suppressed[index] = "ai_scan_policy_alert_not_matched"
        selected = set(candidates[:safe_remaining])
        for index in candidates[safe_remaining:]:
            suppressed[index] = "max_ai_scans_per_day"
        return {
            "selected_indexes": selected,
            "suppressed_reasons": suppressed,
            "candidate_indexes": candidates,
            "ranked_indexes": candidates,
            "candidate_count": len(candidates),
            "remaining_before_run": safe_remaining,
        }
    if normalized_policy == "top_n_per_run":
        ranked = sorted(matched_indexes, key=lambda index: _scan_loop_alert_score(evaluated_items[index].get("snapshot") or {}), reverse=True)
        eligible = ranked[:_scan_loop_ai_top_n(top_n)]
        selected = set(eligible[:safe_remaining])
        for index in ranked:
            if index in selected:
                continue
            suppressed[index] = "max_ai_scans_per_day" if index in eligible else "ai_scan_policy_top_n"
        return {
            "selected_indexes": selected,
            "suppressed_reasons": suppressed,
            "candidate_indexes": eligible,
            "ranked_indexes": ranked,
            "candidate_count": len(eligible),
            "remaining_before_run": safe_remaining,
        }
    if normalized_policy == "smart_budget":
        alert_ranked = sorted(
            [index for index in matched_indexes if _scan_loop_item_alert_matched(evaluated_items[index])],
            key=lambda index: _scan_loop_alert_score(evaluated_items[index].get("snapshot") or {}),
            reverse=True,
        )
        other_ranked = sorted(
            [index for index in matched_indexes if not _scan_loop_item_alert_matched(evaluated_items[index])],
            key=lambda index: _scan_loop_alert_score(evaluated_items[index].get("snapshot") or {}),
            reverse=True,
        )
        ranked = alert_ranked + other_ranked
        run_cap = min(safe_remaining, _scan_loop_ai_top_n(top_n))
        selected = set(ranked[:run_cap])
        eligible = set(ranked[:_scan_loop_ai_top_n(top_n)])
        for index in ranked:
            if index in selected:
                continue
            suppressed[index] = "max_ai_scans_per_day" if index in eligible else "ai_scan_policy_smart_budget"
        return {
            "selected_indexes": selected,
            "suppressed_reasons": suppressed,
            "candidate_indexes": list(eligible),
            "ranked_indexes": ranked,
            "candidate_count": len(eligible),
            "remaining_before_run": safe_remaining,
        }
    selected = set(matched_indexes[:safe_remaining])
    for index in matched_indexes[safe_remaining:]:
        suppressed[index] = "max_ai_scans_per_day"
    return {
        "selected_indexes": selected,
        "suppressed_reasons": suppressed,
        "candidate_indexes": matched_indexes,
        "ranked_indexes": matched_indexes,
        "candidate_count": len(matched_indexes),
        "remaining_before_run": safe_remaining,
    }


def _scan_loop_item_alert_matched(item: dict[str, Any]) -> bool:
    return bool(item.get("alert_matched")) or bool((item.get("alert_result") or {}).get("matched"))


def _annotate_scan_loop_ai_decisions(
    evaluated_items: list[dict[str, Any]],
    selection: dict[str, Any],
    policy: str,
    top_n: int,
) -> None:
    selected = set(selection.get("selected_indexes") or set())
    candidates = set(selection.get("candidate_indexes") or [])
    ranked = list(selection.get("ranked_indexes") or [])
    suppressed = selection.get("suppressed_reasons") or {}
    rank_by_index = {index: rank + 1 for rank, index in enumerate(ranked)}
    for index, item in enumerate(evaluated_items):
        prefilter_matched = bool((item.get("prefilter") or {}).get("matched"))
        decision = {
            "policy": _normalize_ai_scan_policy(policy),
            "top_n": _scan_loop_ai_top_n(top_n),
            "prefilter_matched": prefilter_matched,
            "alert_matched": _scan_loop_item_alert_matched(item),
            "score": _scan_loop_alert_score(item.get("snapshot") or {}),
            "candidate": index in candidates,
            "selected": index in selected,
            "rank": rank_by_index.get(index),
            "suppressed_reason": suppressed.get(index),
        }
        if item.get("status") == "data_unavailable":
            decision["reason"] = "data_unavailable"
        elif not prefilter_matched:
            decision["reason"] = "prefilter_not_matched"
        elif index in selected:
            decision["reason"] = "selected_for_ai_scan"
        else:
            decision["reason"] = suppressed.get(index) or "ai_scan_policy_not_selected"
        item["ai_scan_decision"] = decision


def _scan_loop_alert_score(snapshot: dict[str, Any]) -> float:
    for field in ("score", "decision_score", "rvol", "last"):
        value = _numeric(snapshot.get(field))
        if value is not None:
            return value
    return 0.0


def _create_scan_loop_report_notification(
    owner_id: str,
    instance: dict[str, Any],
    run_id: str,
    items: list[dict[str, Any]],
    clock: dict[str, Any],
    *,
    review_only: bool = False,
) -> list[dict[str, Any]]:
    channel_ids = instance.get("notification_channel_ids") or []
    if not channel_ids:
        return []
    report_items = [_scan_loop_report_item(instance, item, clock) for item in items]
    ordered = sorted(report_items, key=lambda item: (item.get("priority") or 0, float(item.get("score") or 0)), reverse=True)
    ordered = _apply_scan_loop_ai_report_cache(owner_id, instance, ordered, clock)
    scanned_count = len(report_items)
    matched_count = sum(1 for item in report_items if item.get("prefilter_matched"))
    triggered_count = sum(1 for item in report_items if item.get("alert_matched"))
    unavailable_count = sum(1 for item in report_items if item.get("status") == "data_unavailable")
    mode_label = "复盘" if review_only else "实时"
    lines = [
        f"**本轮{mode_label}雷达报告**",
        f"扫描 {scanned_count} · 预筛 {matched_count} · 触发 {triggered_count} · 缺数据 {unavailable_count}",
        "",
    ]
    for index, item in enumerate(ordered[:8]):
        lines.append(_scan_loop_report_line(item, detailed=index < 3))
    if len(ordered) > 8:
        lines.append(f"> 另有 {len(ordered) - 8} 个标的已写入本轮扫描记录。")
    lines.append("_仅用于研究辅助，不构成投资建议、交易建议或收益承诺。_")
    return create_notification_events(
        owner_id,
        source_type="scan_loop_report",
        source_id=run_id,
        dedupe_key=f"scan-loop-report:{instance['id']}:{run_id}",
        title=f"{instance['name']} 本轮雷达扫描报告",
        body="\n".join(lines),
        payload={
            "run_id": run_id,
            "instance_id": instance["id"],
            "watchlist_id": instance.get("watchlist_id"),
            "market_clock": clock,
            "review_only": review_only,
            "scanned_count": scanned_count,
            "matched_count": matched_count,
            "triggered_count": triggered_count,
            "data_unavailable_count": unavailable_count,
            "items": [{key: value for key, value in item.items() if key != "priority"} for item in ordered],
        },
        channel_ids=channel_ids,
    )


def _scan_loop_report_item(instance: dict[str, Any], item: dict[str, Any], clock: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = item.get("snapshot") or {}
    status = str(item.get("status") or "")
    prefilter_matched = bool((item.get("prefilter") or {}).get("matched"))
    alert_matched = bool((item.get("alert_result") or {}).get("matched"))
    data_available = _snapshot_data_available(snapshot)
    direction = _opportunity_direction(snapshot) if data_available else "unknown"
    strategy = _opportunity_strategy_structure(instance, direction, snapshot) if data_available else "unknown"
    entry_reference: dict[str, Any] = {}
    risk_plan: dict[str, Any] = {}
    if data_available and prefilter_matched:
        entry_reference = _opportunity_entry_reference(snapshot, direction, strategy)
        risk_plan = _opportunity_risk_plan(snapshot, direction, strategy, entry_reference)
    decision = _scan_loop_report_decision(status, prefilter_matched, alert_matched, item)
    conclusion = _scan_loop_report_conclusion(status, prefilter_matched, alert_matched, direction, strategy, item)
    observation = _scan_loop_report_observation(snapshot, item)
    scenario_analysis = _scan_loop_scenario_analysis(snapshot, item, direction, entry_reference, risk_plan)
    demo_tracking = _scan_loop_demo_tracking(item, snapshot, entry_reference, risk_plan, direction)
    market_pulse = _scan_loop_market_pulse(instance, item, snapshot, clock or {}, scenario_analysis, demo_tracking, observation, conclusion, decision)
    return {
        "symbol": item.get("symbol") or snapshot.get("symbol"),
        "status": status,
        "score": item.get("score"),
        "prefilter_matched": prefilter_matched,
        "alert_matched": alert_matched,
        "triggered": bool(item.get("triggered")),
        "scan_id": item.get("scan_id"),
        "opportunity_id": item.get("opportunity_id"),
        "observation": observation,
        "conclusion": conclusion,
        "decision": decision,
        "scenario_analysis": scenario_analysis,
        "demo_tracking": demo_tracking,
        "market_pulse": market_pulse,
        "direction": direction,
        "strategy_structure": strategy,
        "entry_reference": entry_reference,
        "risk_plan": risk_plan,
        "take_profit": (risk_plan.get("take_profit") or {}).get("levels") or [],
        "stop_loss": (risk_plan.get("stop_loss") or {}).get("underlying_reference"),
        "snapshot_summary": _scan_loop_snapshot_summary(snapshot),
        "data_quality": item.get("data_quality") or snapshot.get("data_quality") or {},
        "priority": _scan_loop_report_priority(status, prefilter_matched, alert_matched),
    }


def _scan_loop_report_priority(status: str, prefilter_matched: bool, alert_matched: bool) -> int:
    if status in {"alerted", "digest_pending"} or alert_matched:
        return 4
    if prefilter_matched:
        return 3
    if status == "data_unavailable":
        return 1
    return 2


def _scan_loop_report_observation(snapshot: dict[str, Any], item: dict[str, Any]) -> str:
    if not _snapshot_data_available(snapshot):
        quality = item.get("data_quality") or snapshot.get("data_quality") or {}
        return str(quality.get("explanation") or item.get("error") or snapshot.get("error") or "行情数据不可用")
    parts = [f"现价 {fmt_float(snapshot.get('last') or snapshot.get('price'))}"]
    rvol = _numeric(snapshot.get("rvol"))
    if rvol is not None:
        parts.append(f"RVOL {rvol:.2f}")
    vs_vwap = _numeric(snapshot.get("underlying_vs_vwap_pct"))
    if vs_vwap is not None:
        parts.append(f"VWAP 偏离 {vs_vwap:.2f}%")
    rv_rank = _numeric(snapshot.get("rv_rank"))
    if rv_rank is not None:
        parts.append(f"RV Rank {rv_rank:.2f}")
    gex = _opportunity_gex_snapshot(snapshot)
    if gex.get("regime") and gex.get("regime") != "unknown":
        parts.append(f"GEX {gex.get('regime')}")
    source = snapshot.get("source") or snapshot.get("pricing_source")
    if source:
        parts.append(f"源 {source}")
    return "，".join(parts)


def _scan_loop_scenario_analysis(
    snapshot: dict[str, Any],
    item: dict[str, Any],
    direction: str,
    entry_reference: dict[str, Any],
    risk_plan: dict[str, Any],
) -> dict[str, Any]:
    if not _snapshot_data_available(snapshot):
        return {"available": False, "summary": "行情不可用，无法生成情景基准。", "scenarios": []}
    last = _numeric(snapshot.get("last") or snapshot.get("price") or snapshot.get("close"))
    if last is None:
        return {"available": False, "summary": "缺少现价，无法生成情景基准。", "scenarios": []}
    vwap = _numeric(snapshot.get("vwap"))
    vs_vwap = _numeric(snapshot.get("underlying_vs_vwap_pct"))
    if vs_vwap is None and vwap not in (None, 0):
        vs_vwap = (last / float(vwap) - 1) * 100
    gex = _opportunity_gex_snapshot(snapshot)
    support = _scan_loop_support_levels(snapshot, last, gex, risk_plan)
    resistance = _scan_loop_resistance_levels(snapshot, last, gex, risk_plan)
    center = vwap or _numeric(snapshot.get("volume_profile_poc")) or last
    lower = support[0]["level"] if support else round(min(last, center) * 0.997, 2)
    upper = resistance[0]["level"] if resistance else round(max(last, center) * 1.003, 2)
    if lower >= upper:
        lower, upper = round(min(last, center) * 0.997, 2), round(max(last, center) * 1.003, 2)
    hard_support = support[-1]["level"] if support else round(last * 0.99, 2)
    first_support = support[0]["level"] if support else round(last * 0.997, 2)
    first_resistance = resistance[0]["level"] if resistance else round(last * 1.003, 2)
    upper_wall = _scan_loop_level_label(resistance[0]) if resistance else fmt_float(first_resistance)
    lower_wall = _scan_loop_level_label(support[0]) if support else fmt_float(first_support)
    probs = _scan_loop_scenario_probabilities(snapshot, gex, vs_vwap, direction)
    center_label = "VWAP" if vwap else "POC" if snapshot.get("volume_profile_poc") else "现价"
    center_text = fmt_float(center)
    health = "健康回踩" if vwap and abs((vs_vwap or 0)) <= 0.35 else "中枢争夺"
    if (vs_vwap or 0) < -0.35:
        health = "VWAP 下方承压"
    elif (vs_vwap or 0) > 0.35:
        health = "VWAP 上方偏强"
    scenarios = [
        {
            "key": "base",
            "label": "基准",
            "probability_pct": probs["base"],
            "range": [round(lower, 2), round(upper, 2)],
            "body": (
                f"{center_label} {center_text} 附近作为中枢，{fmt_float(lower)}-{fmt_float(upper)} 区间震荡概率最高。"
                f"价格回到中枢属于{health}；下方 {lower_wall} 是第一接盘/支撑观察位。"
            ),
        },
        {
            "key": "downside_test",
            "label": "次情形",
            "probability_pct": probs["downside"],
            "levels": [round(first_support, 2), round(hard_support, 2)],
            "body": (
                f"若 {center_label} {center_text} 失守，先测 {fmt_float(first_support)}，再看 {fmt_float(hard_support)}。"
                f"{_scan_loop_gex_support_phrase(gex, support)}"
            ),
        },
        {
            "key": "upside_reclaim",
            "label": "偏强",
            "probability_pct": probs["upside"],
            "levels": [round(first_resistance, 2)],
            "body": (
                f"重新站回 {fmt_float(first_resistance)} 才能打开上攻窗口；上方 {upper_wall} 可能形成压力墙。"
                f"{_scan_loop_gex_resistance_phrase(gex, resistance)}"
            ),
        },
        {
            "key": "true_weak",
            "label": "真弱",
            "probability_pct": probs["tail"],
            "levels": [round(hard_support, 2)],
            "body": (
                f"跌破 {fmt_float(hard_support)} 才进入真弱情形，需要 RVOL 放大或 GEX 结构转空确认。"
                "若没有放量，优先按假跌破/低吸复核处理。"
            ),
        },
    ]
    return {
        "available": True,
        "center": round(center, 2),
        "center_type": center_label.lower(),
        "last": round(last, 2),
        "vs_vwap_pct": round(vs_vwap, 2) if vs_vwap is not None else None,
        "support_levels": support,
        "resistance_levels": resistance,
        "gex_regime": gex.get("regime") or "unknown",
        "summary": _scan_loop_scenario_summary(scenarios),
        "scenarios": scenarios,
        "source": "scan_loop_snapshot",
    }


def _scan_loop_support_levels(snapshot: dict[str, Any], last: float, gex: dict[str, Any], risk_plan: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        ("VWAP", snapshot.get("vwap")),
        ("VAL", snapshot.get("volume_profile_value_area_low")),
        ("POC", snapshot.get("volume_profile_poc")),
        ("ORB Low", snapshot.get("orb_low")),
        ("Put Wall", gex.get("put_wall")),
        ("Gamma Flip", gex.get("gamma_flip")),
        ("Stop", (risk_plan.get("stop_loss") or {}).get("underlying_reference")),
    ]
    return _scan_loop_nearby_levels(candidates, last, below=True)


def _scan_loop_resistance_levels(snapshot: dict[str, Any], last: float, gex: dict[str, Any], risk_plan: dict[str, Any]) -> list[dict[str, Any]]:
    targets = [(level or {}).get("underlying_reference") for level in (risk_plan.get("take_profit") or {}).get("levels") or []]
    candidates = [
        ("VWAP", snapshot.get("vwap")),
        ("VAH", snapshot.get("volume_profile_value_area_high")),
        ("POC", snapshot.get("volume_profile_poc")),
        ("ORB High", snapshot.get("orb_high")),
        ("Call Wall", gex.get("call_wall")),
        ("Gamma Flip", gex.get("gamma_flip")),
    ] + [(f"TP{index}", target) for index, target in enumerate(targets, start=1)]
    return _scan_loop_nearby_levels(candidates, last, below=False)


def _scan_loop_nearby_levels(candidates: list[tuple[str, Any]], last: float, *, below: bool) -> list[dict[str, Any]]:
    levels: list[dict[str, Any]] = []
    seen: set[float] = set()
    for label, raw in candidates:
        level = _numeric(raw)
        if level is None or level <= 0:
            continue
        if below and level > last * 1.002:
            continue
        if not below and level < last * 0.998:
            continue
        rounded = round(level, 2)
        if rounded in seen:
            continue
        seen.add(rounded)
        distance_pct = abs(level / last - 1) * 100 if last else 0.0
        levels.append({"label": label, "level": rounded, "distance_pct": round(distance_pct, 2)})
    return sorted(levels, key=lambda row: row["distance_pct"])[:4]


def _scan_loop_scenario_probabilities(snapshot: dict[str, Any], gex: dict[str, Any], vs_vwap: float | None, direction: str) -> dict[str, int]:
    base, downside, upside, tail = 58, 22, 15, 5
    rvol = _numeric(snapshot.get("rvol"))
    regime = str(gex.get("regime") or "unknown")
    if regime == "negative_gamma" and rvol is not None and rvol >= 1.5:
        base, downside, upside, tail = 46, 28, 18, 8
    elif regime == "positive_gamma":
        base, downside, upside, tail = 62, 20, 13, 5
    if vs_vwap is not None and vs_vwap <= -0.5:
        base -= 5
        downside += 5
    elif vs_vwap is not None and vs_vwap >= 0.5:
        base -= 4
        upside += 4
    if direction == "bearish":
        downside += 3
        upside = max(8, upside - 3)
    elif direction == "bullish":
        upside += 3
        downside = max(10, downside - 3)
    total = base + downside + upside + tail
    return {
        "base": round(base / total * 100),
        "downside": round(downside / total * 100),
        "upside": round(upside / total * 100),
        "tail": max(1, 100 - round(base / total * 100) - round(downside / total * 100) - round(upside / total * 100)),
    }


def _scan_loop_scenario_summary(scenarios: list[dict[str, Any]]) -> str:
    parts = []
    for scenario in scenarios:
        parts.append(f"{scenario.get('label')}~{scenario.get('probability_pct')}%：{scenario.get('body')}")
    return " ".join(parts)


def _scan_loop_level_label(level: dict[str, Any]) -> str:
    label = str(level.get("label") or "Level")
    value = fmt_float(level.get("level"))
    return f"{value}({label})"


def _scan_loop_gex_support_phrase(gex: dict[str, Any], support: list[dict[str, Any]]) -> str:
    if not support:
        return " 暂无明确 GEX 支撑位，需用 VWAP/成交密集区复核。"
    regime = str(gex.get("regime") or "unknown")
    label = _scan_loop_level_label(support[0])
    if regime == "positive_gamma":
        return f" {label} 附近正 gamma 更容易接住回踩。"
    if regime == "negative_gamma":
        return f" {label} 是第一防线，但负 gamma 下破后会放大波动。"
    return f" {label} 是第一参考支撑。"


def _scan_loop_gex_resistance_phrase(gex: dict[str, Any], resistance: list[dict[str, Any]]) -> str:
    if not resistance:
        return " 暂无明确上方 GEX 压力位。"
    regime = str(gex.get("regime") or "unknown")
    label = _scan_loop_level_label(resistance[0])
    if regime == "positive_gamma":
        return f" 正 gamma 下 {label} 容易压制追涨。"
    if regime == "negative_gamma":
        return f" 若放量突破 {label}，负 gamma 可能放大顺势。"
    return f" {label} 是第一参考压力。"


def _scan_loop_demo_tracking(
    item: dict[str, Any],
    snapshot: dict[str, Any],
    entry_reference: dict[str, Any],
    risk_plan: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    opportunity_id = str(item.get("opportunity_id") or "").strip()
    if not opportunity_id:
        return {"enabled": False, "status": "not_created", "summary": "未触发机会，暂不创建 demo 跟踪。"}
    entry = _numeric(entry_reference.get("underlying_reference")) or _numeric(snapshot.get("last"))
    stop = _numeric((risk_plan.get("stop_loss") or {}).get("underlying_reference"))
    targets = [
        _numeric((level or {}).get("underlying_reference"))
        for level in (risk_plan.get("take_profit") or {}).get("levels") or []
    ]
    targets = [target for target in targets if target is not None]
    status = "entry_confirmed" if item.get("triggered") else "watching_entry"
    demo_id = f"DEMO-{opportunity_id[:8].upper()}"
    parts = [f"{demo_id} {status.replace('_', ' ')}"]
    if entry is not None:
        parts.append(f"入场参考 {fmt_float(entry)}")
    if targets:
        parts.append(f"止盈 {'/'.join(fmt_float(target) for target in targets[:2])}")
    if stop is not None:
        parts.append(f"止损 {fmt_float(stop)}")
    return {
        "enabled": True,
        "demo_order_id": demo_id,
        "status": status,
        "opportunity_id": opportunity_id,
        "direction": direction,
        "entry_reference": round(entry, 2) if entry is not None else None,
        "take_profit": [round(target, 2) for target in targets[:2]],
        "stop_loss": round(stop, 2) if stop is not None else None,
        "followup_interval_seconds": 300,
        "summary": " · ".join(parts),
        "source": "opportunity_followup_reference",
    }


def _scan_loop_report_conclusion(
    status: str,
    prefilter_matched: bool,
    alert_matched: bool,
    direction: str,
    strategy: str,
    item: dict[str, Any],
) -> str:
    if status == "data_unavailable":
        return "数据不足，无法形成有效结论"
    if alert_matched:
        return f"提醒条件成立，方向 {_opportunity_direction_label(direction)}，结构 {_opportunity_strategy_label(strategy)}"
    if prefilter_matched:
        return f"进入观察池但提醒条件未完全成立，方向 {_opportunity_direction_label(direction)}，结构 {_opportunity_strategy_label(strategy)}"
    reason = _scan_loop_report_first_failed_reason(item.get("prefilter") or {})
    return f"预筛未通过，暂不形成交易计划{f'：{reason}' if reason else ''}"


def _scan_loop_report_decision(status: str, prefilter_matched: bool, alert_matched: bool, item: dict[str, Any]) -> str:
    if status == "data_unavailable":
        return "暂停判断，等待可用行情"
    if status == "reviewed":
        return "非实时复盘，仅保留下一交易时段参考"
    if status == "alerted":
        return "触发提醒，可进入人工复核或 AI 精扫"
    if status == "digest_pending":
        return "纳入本轮汇总，继续跟踪"
    if alert_matched:
        reason = item.get("alert_suppressed_reason")
        return f"条件成立但提醒被抑制：{reason}" if reason else "条件成立，等待提醒窗口"
    if prefilter_matched:
        return "继续观察，等待提醒规则确认"
    return "观望，不追单"


def _scan_loop_report_first_failed_reason(result: dict[str, Any]) -> str:
    for check in result.get("checks") or []:
        if check.get("matched"):
            continue
        field = str(check.get("field") or "unknown")
        actual = check.get("actual")
        operator = check.get("operator")
        expected = check.get("expected")
        if actual is None:
            return f"{field} 缺失"
        return f"{field}={actual} 未满足 {operator} {expected}"
    return ""


def _scan_loop_report_line(item: dict[str, Any], *, detailed: bool = True) -> str:
    ai_report = item.get("ai_report") or {}
    if detailed and ai_report.get("text"):
        return f"{ai_report.get('text')}\n"
    pulse = item.get("market_pulse") or {}
    if detailed and pulse.get("text"):
        return f"{pulse.get('text')}\n"
    symbol = item.get("symbol") or "--"
    entry = (item.get("entry_reference") or {}).get("underlying_reference")
    take_profit = item.get("take_profit") or []
    tp_text = "/".join(fmt_float(level.get("underlying_reference")) for level in take_profit[:2] if isinstance(level, dict)) or "--"
    stop_text = fmt_float(item.get("stop_loss")) if item.get("stop_loss") is not None else "--"
    entry_text = fmt_float(entry) if entry is not None else "--"
    scenario = item.get("scenario_analysis") or {}
    scenario_lines = _scan_loop_report_scenario_lines(scenario)
    demo = item.get("demo_tracking") or {}
    demo_line = f"> Demo：{demo.get('summary')}" if demo.get("enabled") else ""
    lines = [
        f"**{symbol}** · {item.get('decision') or '--'}",
        f"> 观察：{item.get('observation') or '--'}",
        f"> 结论：{item.get('conclusion') or '--'}",
        f"> 决策：{item.get('decision') or '--'}",
    ]
    lines.extend(scenario_lines)
    lines.append(f"> 风控：入场 {entry_text} · 止盈 {tp_text} · 止损 {stop_text}")
    if demo_line:
        lines.append(demo_line)
    lines.append("")
    return "\n".join(
        lines
    )


def _scan_loop_report_scenario_lines(scenario: dict[str, Any]) -> list[str]:
    if not scenario.get("available"):
        return [f"> 情景：{scenario.get('summary') or '--'}"]
    lines = []
    for row in (scenario.get("scenarios") or [])[:4]:
        if not isinstance(row, dict):
            continue
        lines.append(f"> {row.get('label')}~{row.get('probability_pct')}%：{row.get('body')}")
    return lines


def _create_scan_loop_digest_notification(
    owner_id: str,
    instance: dict[str, Any],
    run_id: str,
    candidates: list[dict[str, Any]],
    clock: dict[str, Any],
) -> list[dict[str, Any]]:
    sorted_candidates = sorted(candidates, key=lambda item: float(item.get("score") or 0), reverse=True)
    symbols = [str(item.get("symbol") or "") for item in sorted_candidates if item.get("symbol")]
    preview = "、".join(symbols[:5])
    if len(symbols) > 5:
        preview += f" 等 {len(symbols)} 个"
    body = (
        f"{instance['name']} 本轮观察池汇总：{preview or '无标的'} 满足提醒条件。"
        "已记录为研究机会，请在机会雷达中复核入场、止盈、止损、GEX 和数据新鲜度。"
        "此提醒仅用于研究辅助，不构成投资建议、交易建议或收益承诺。"
    )
    return create_notification_events(
        owner_id,
        source_type="scan_loop_digest",
        source_id=run_id,
        dedupe_key=f"scan-loop-digest:{instance['id']}:{run_id}",
        title=f"{instance['name']} 每日观察汇总",
        body=body,
        payload={
            "run_id": run_id,
            "instance_id": instance["id"],
            "watchlist_id": instance.get("watchlist_id"),
            "alert_mode": "daily_digest",
            "symbols": symbols,
            "count": len(symbols),
            "market_clock": clock,
            "items": [
                {
                    "symbol": item.get("symbol"),
                    "scan_id": item.get("scan_id"),
                    "opportunity_id": item.get("opportunity_id"),
                    "score": item.get("score"),
                    "alert": item.get("alert"),
                    "prefilter": item.get("prefilter"),
                    "ai_scan_suppressed_reason": item.get("ai_scan_suppressed_reason"),
                }
                for item in sorted_candidates
            ],
        },
        channel_ids=instance.get("notification_channel_ids") or [],
    )


def _scan_loop_uses_gex(instance: dict[str, Any]) -> bool:
    for group_name in ("prefilter_rules", "alert_rules"):
        group = instance.get(group_name) or {}
        for condition in group.get("conditions") or []:
            if not isinstance(condition, dict):
                continue
            field = str(condition.get("field") or "")
            if field.startswith("gex_") or field.startswith("gex."):
                return True
    return False


def _gex_snapshot_available(snapshot: dict[str, Any]) -> bool:
    gex = _opportunity_gex_snapshot(snapshot)
    return bool(gex.get("available") or gex.get("regime") not in {None, "", "unknown"} or gex.get("nearest_wall"))


def _scan_loop_gex_fields(context: dict[str, Any]) -> dict[str, Any]:
    gex = _opportunity_gex_snapshot(context)
    return {
        "gex": gex,
        "gex_available": bool(gex.get("available")),
        "gex_regime": gex.get("regime") or "unknown",
        "gex_nearest_wall": gex.get("nearest_wall") or "",
        "gex_nearest_wall_distance_pct": gex.get("nearest_wall_distance_pct"),
        "gex_call_wall": gex.get("call_wall"),
        "gex_put_wall": gex.get("put_wall"),
        "gex_gamma_flip": gex.get("gamma_flip"),
        "gex_pinning_risk": gex.get("pinning_risk") or "unknown",
        "gex_trend_acceleration_risk": gex.get("trend_acceleration_risk") or "unknown",
        "gex_tailwind": gex.get("tailwind") or context.get("gex_alignment") or context.get("alignment") or "",
        "gex_snapshot": gex,
    }


def _scan_loop_market_pulse(
    instance: dict[str, Any],
    item: dict[str, Any],
    snapshot: dict[str, Any],
    clock: dict[str, Any],
    scenario: dict[str, Any],
    demo_tracking: dict[str, Any],
    observation: str,
    conclusion: str,
    decision: str,
) -> dict[str, Any]:
    if not _snapshot_data_available(snapshot):
        return {"available": False, "text": "行情不可用，无法生成盘中交易台快照。"}
    symbol = str(item.get("symbol") or snapshot.get("symbol") or "--").upper()
    last = _numeric(snapshot.get("last") or snapshot.get("price") or snapshot.get("close"))
    interval_minutes = max(1, round(_instance_interval_seconds(instance) / 60))
    now_label = _scan_loop_pulse_time_label(clock)
    session_label = _scan_loop_session_label(clock)
    phase = _scan_loop_pulse_phase(clock, snapshot)
    vwap = _numeric(snapshot.get("vwap"))
    scenario_rows = [row for row in (scenario.get("scenarios") or []) if isinstance(row, dict)]
    base = scenario_rows[0] if scenario_rows else {}
    downside = scenario_rows[1] if len(scenario_rows) > 1 else {}
    upside = scenario_rows[2] if len(scenario_rows) > 2 else {}
    tail = scenario_rows[3] if len(scenario_rows) > 3 else {}
    trigger_line = _scan_loop_trigger_line(scenario)
    status_line = _scan_loop_operation_status(item, scenario, trigger_line)
    option_pulse = _scan_loop_option_microstructure(snapshot, scenario)
    hiro = _scan_loop_hiro_summary(snapshot)
    structure = _scan_loop_key_structure(snapshot, scenario)
    inventory = _scan_loop_inventory_ladder(snapshot, scenario, last)
    demo = demo_tracking.get("summary") if demo_tracking.get("enabled") else ""
    lines = [
        f"📊 {symbol} {interval_minutes}分钟 │ {now_label} [{phase}]",
        f"SPOT {fmt_float(last)}｜{session_label}｜{_scan_loop_session_discipline(clock, scenario)}",
        "",
        f"操作状态: {status_line}",
        f"观察: {observation or '--'}",
        f"结论: {conclusion or '--'}",
        f"决策: {decision or '--'}",
        f"基准(~{base.get('probability_pct', '--')}%)——{base.get('body') or '--'}",
        f"次情形(~{downside.get('probability_pct', '--')}%)——{downside.get('body') or '--'}",
        f"偏强(~{upside.get('probability_pct', '--')}%)——{upside.get('body') or '--'}",
        f"真弱(~{tail.get('probability_pct', '--')}%)——{tail.get('body') or '--'}",
        "",
        "📐 IV / Smile",
        f"  {option_pulse}",
        "",
        "期权对冲资金流向（HIRO）",
        f"  {hiro}",
        "",
        "📌 关键结构位",
        structure,
        "",
        "库存异常价位",
        inventory,
    ]
    if demo:
        lines.extend(["", f"Demo订单跟踪: {demo}"])
    lines.append(f"状态标签: {_scan_loop_state_tags(item, scenario, vwap)}")
    return {
        "available": True,
        "text": "\n".join(lines),
        "interval_minutes": interval_minutes,
        "phase": phase,
        "trigger_line": trigger_line,
        "option_microstructure": option_pulse,
        "hiro": hiro,
    }


def _scan_loop_pulse_time_label(clock: dict[str, Any]) -> str:
    raw = str(clock.get("now_et") or "").strip()
    try:
        current = datetime.fromisoformat(raw).astimezone(ET)
    except ValueError:
        current = datetime.now(ET)
    return f"{current:%Y-%m-%d %H:%M} NY"


def _scan_loop_session_label(clock: dict[str, Any]) -> str:
    raw = str(clock.get("now_et") or "").strip()
    try:
        current = datetime.fromisoformat(raw).astimezone(ET)
        return f"RTH {current:%H:%M} ET"
    except ValueError:
        return str(clock.get("session_state") or "RTH")


def _scan_loop_pulse_phase(clock: dict[str, Any], snapshot: dict[str, Any]) -> str:
    raw = str(clock.get("now_et") or "").strip()
    try:
        current = datetime.fromisoformat(raw).astimezone(ET)
        if datetime_time(11, 30) <= current.time() <= datetime_time(13, 45):
            return "LULL"
        if current.time() < datetime_time(10, 15):
            return "OPEN"
        if current.time() >= datetime_time(15, 15):
            return "POWER"
    except ValueError:
        pass
    rvol = _numeric(snapshot.get("rvol"))
    return "ACTIVE" if rvol is not None and rvol >= 1.5 else "NORMAL"


def _scan_loop_session_discipline(clock: dict[str, Any], scenario: dict[str, Any]) -> str:
    phase = _scan_loop_pulse_phase(clock, {})
    center = fmt_float(scenario.get("center"))
    if phase == "LULL":
        return f"中午纪律: 中枢/Pin带内不追新仓，先看 {center}"
    if phase == "OPEN":
        return "开盘纪律: 先确认 ORB/VWAP 接受，不抢第一根"
    if phase == "POWER":
        return "尾盘纪律: 只处理确认信号和风控，不扩大隔夜风险"
    return f"盘中纪律: 围绕 {center} 做确认，不追单"


def _scan_loop_trigger_line(scenario: dict[str, Any]) -> str:
    support = scenario.get("support_levels") or []
    resistance = scenario.get("resistance_levels") or []
    if support and isinstance(support[0], dict):
        return fmt_float(support[0].get("level"))
    if resistance and isinstance(resistance[0], dict):
        return fmt_float(resistance[0].get("level"))
    return fmt_float(scenario.get("center"))


def _scan_loop_operation_status(item: dict[str, Any], scenario: dict[str, Any], trigger_line: str) -> str:
    if item.get("alert_matched"):
        action = "触发复核"
    elif item.get("prefilter_matched"):
        action = "追踪"
    else:
        action = "观望"
    base_range = ((scenario.get("scenarios") or [{}])[0] or {}).get("range") or []
    range_text = f"{fmt_float(base_range[0])}-{fmt_float(base_range[1])}" if len(base_range) >= 2 else "核心区间"
    return f"{action}｜{range_text} 核心Pin带内｜触发线 {trigger_line}"


def _scan_loop_option_microstructure(snapshot: dict[str, Any], scenario: dict[str, Any]) -> str:
    iv = _numeric(snapshot.get("implied_volatility") or snapshot.get("iv"))
    iv_change = _numeric(snapshot.get("iv_change_3m") or snapshot.get("iv_change"))
    skew = _numeric(snapshot.get("skew_25d") or snapshot.get("put_call_skew") or snapshot.get("skew"))
    smile_low = _numeric(snapshot.get("smile_low") or scenario.get("center"))
    gamma_peak = _numeric(snapshot.get("gamma_peak") or _scan_loop_gamma_peak_from_scenario(scenario))
    parts = []
    if iv is not None:
        iv_pct = iv * 100 if iv < 3 else iv
        change = f"，较上一拍{iv_change:+.2f}点" if iv_change is not None else ""
        parts.append(f"波动率 {iv_pct:.2f}%{change}")
    else:
        parts.append("波动率未接入本轮快照")
    parts.append(f"25Δ Skew {skew:.2f}点" if skew is not None else "25Δ Skew 未接入")
    if smile_low is not None:
        last = _numeric(snapshot.get("last") or snapshot.get("price"))
        distance = f"（距现价约 {abs(smile_low - last):.0f} 点）" if last is not None else ""
        parts.append(f"Smile低点 {fmt_float(smile_low)}{distance}")
    if gamma_peak is not None:
        parts.append(f"γ峰位 {fmt_float(gamma_peak)}")
    parts.append(_scan_loop_premium_chase_read(snapshot))
    return "；".join(parts)


def _scan_loop_gamma_peak_from_scenario(scenario: dict[str, Any]) -> float | None:
    levels = (scenario.get("support_levels") or []) + (scenario.get("resistance_levels") or [])
    if not levels:
        return _numeric(scenario.get("center"))
    closest = min((row for row in levels if isinstance(row, dict)), key=lambda row: float(row.get("distance_pct") or 999), default={})
    return _numeric(closest.get("level"))


def _scan_loop_premium_chase_read(snapshot: dict[str, Any]) -> str:
    spread = _numeric(snapshot.get("bid_ask_spread_pct"))
    iv_edge = str(snapshot.get("iv_edge_state") or "").strip()
    if spread is not None and spread >= 12:
        return f"权利金追价: 盘口偏宽({spread:.1f}%)，不追市价"
    if "expensive" in iv_edge or "crush" in iv_edge:
        return "权利金追价: IV 偏贵，方向对也要防 IV 压缩"
    if "cheap" in iv_edge:
        return "权利金追价: IV 相对便宜，但仍需等现货确认"
    return "权利金追价: 未见明显扩张确认，先等现货/VWAP接受"


def _scan_loop_hiro_summary(snapshot: dict[str, Any]) -> str:
    hiro = snapshot.get("hiro") if isinstance(snapshot.get("hiro"), dict) else {}
    if not hiro:
        return "HIRO/实时对冲流未接入本轮快照；以现货 VWAP、RVOL、GEX 与库存结构替代。"
    fields = []
    for key, label in (("flow_30m", "30分钟"), ("flow_intraday", "日内累计"), ("flow_recent", "最近一拍")):
        value = _numeric(hiro.get(key))
        if value is not None:
            fields.append(f"{label}: {fmt_float(value)}")
    return "｜".join(fields) if fields else "HIRO 数据为空；以结构位复核。"


def _scan_loop_key_structure(snapshot: dict[str, Any], scenario: dict[str, Any]) -> str:
    resistance = scenario.get("resistance_levels") or []
    support = scenario.get("support_levels") or []
    ceiling = (resistance[0] if resistance else {}).get("level") if isinstance(resistance[0] if resistance else {}, dict) else None
    floor = (support[-1] if support else {}).get("level") if isinstance(support[-1] if support else {}, dict) else None
    center = scenario.get("center")
    gamma_peak = _scan_loop_gamma_peak_from_scenario(scenario)
    return "\n".join(
        [
            f"  King Ceiling 上方天花板: {fmt_float(ceiling)}",
            f"  King Floor 下方支撑:     {fmt_float(floor)}",
            f"  中枢:                    {fmt_float(center)}",
            f"  Barney短γ触发:           {fmt_float(gamma_peak)}",
            "  盘中剧本追踪: touch 不是接受，带内震荡正常；确认要看 VWAP/成交/期权追价。",
        ]
    )


def _scan_loop_inventory_ladder(snapshot: dict[str, Any], scenario: dict[str, Any], last: float | None) -> str:
    if last is None:
        return "  暂无 spot，无法展开库存阶梯。"
    rows = []
    levels = []
    for row in (scenario.get("resistance_levels") or [])[:4]:
        if isinstance(row, dict):
            levels.append(("resistance", row))
    for row in (scenario.get("support_levels") or [])[:4]:
        if isinstance(row, dict):
            levels.append(("support", row))
    if not levels:
        return "  暂无库存异常价位；等待 GEX/Volume Profile 更新。"
    ordered = sorted(levels, key=lambda item: float(item[1].get("level") or 0), reverse=True)
    inserted_spot = False
    for kind, row in ordered:
        level = _numeric(row.get("level"))
        if level is None:
            continue
        if not inserted_spot and level < last:
            rows.append(f"  ━━━━━━ spot {fmt_float(last)} ━━━━━━")
            inserted_spot = True
        badge = "🔸 🔸 弱阻力" if kind == "resistance" else "🛡 🛡 支撑"
        gex_text = _scan_loop_level_gex_text(snapshot, level, row.get("label") or "")
        rows.append(f"  {fmt_float(level):>7} │ {badge}（{row.get('label') or '结构位'}） │ {gex_text}")
    if not inserted_spot:
        rows.append(f"  ━━━━━━ spot {fmt_float(last)} ━━━━━━")
    return "\n".join(rows[:10])


def _scan_loop_level_gex_text(snapshot: dict[str, Any], level: float, label: str = "") -> str:
    call_wall = _numeric(snapshot.get("gex_call_wall") or snapshot.get("call_wall"))
    put_wall = _numeric(snapshot.get("gex_put_wall") or snapshot.get("put_wall"))
    gamma_flip = _numeric(snapshot.get("gex_gamma_flip") or snapshot.get("gamma_flip"))
    labels = []
    if call_wall is not None and abs(call_wall - level) <= max(0.5, level * 0.001):
        labels.append("Call Wall")
    if put_wall is not None and abs(put_wall - level) <= max(0.5, level * 0.001):
        labels.append("Put Wall")
    if gamma_flip is not None and abs(gamma_flip - level) <= max(0.5, level * 0.001):
        labels.append("Gamma Flip")
    if labels:
        return " / ".join(labels)
    # Non-GEX structural levels (VWAP/POC/VAH/VAL/ORB/Stop/TP) intentionally do
    # not carry a GEX magnitude. Surface their structural family instead of the
    # misleading "GEX 待量化" placeholder.
    label_clean = str(label or "").strip()
    label_upper = label_clean.upper()
    if any(token in label_upper for token in ("VWAP",)):
        return "VWAP 锚"
    if any(token in label_upper for token in ("POC", "VAH", "VAL")):
        return "成交筹码"
    if "ORB" in label_upper:
        return "ORB 阶梯"
    if any(token in label_clean for token in ("Stop", "止损")):
        return "风控位"
    if any(token in label_clean for token in ("TP1", "TP2", "目标")):
        return "目标位"
    return "技术结构"


def _scan_loop_state_tags(item: dict[str, Any], scenario: dict[str, Any], vwap: float | None) -> str:
    tags = []
    if item.get("alert_matched"):
        tags.append("✅ 条件触发")
    elif item.get("prefilter_matched"):
        tags.append("🟡 观察池")
    else:
        tags.append("⚪ 观望")
    if vwap is not None:
        tags.append(f"VWAP {fmt_float(vwap)}")
    center_type = str(scenario.get("center_type") or "").upper()
    if center_type:
        tags.append(f"{center_type} 中枢")
    return "｜".join(tags)


def _apply_scan_loop_ai_report_cache(
    owner_id: str,
    instance: dict[str, Any],
    ordered_items: list[dict[str, Any]],
    clock: dict[str, Any],
) -> list[dict[str, Any]]:
    if not ordered_items or not instance.get("use_ai", True):
        return ordered_items
    # `always` policy is explicitly opt-in for fresh AI narration every scan.
    # The previous text is still injected into the AI prompt via `previous_report`
    # for context continuity, but the cached text itself is never reused.
    if _normalize_ai_scan_policy(instance.get("ai_scan_policy")) == "always":
        return ordered_items
    max_items = max(0, min(int(os.getenv("AI_OPTION_SCAN_LOOP_AI_REPORT_TOP_N", "1") or 1), 3))
    if max_items <= 0:
        return ordered_items
    cache = dict(instance.get("ai_report_cache") or {})
    updated_cache = dict(cache)
    changed = False
    output: list[dict[str, Any]] = []
    for index, item in enumerate(ordered_items):
        if index >= max_items:
            output.append(item)
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            output.append(item)
            continue
        signature = _scan_loop_ai_report_signature(item)
        cached = cache.get(symbol) if isinstance(cache.get(symbol), dict) else {}
        reuse_reason = _scan_loop_ai_report_reuse_reason(cached, signature)
        if reuse_reason:
            text = str(cached.get("text") or "").strip()
            ai_report = {
                "available": bool(text),
                "source": "cache",
                "reused": True,
                "reuse_reason": reuse_reason,
                "generated_at": cached.get("generated_at"),
                "signature": signature,
                "text": _scan_loop_ai_cached_text(text, item, reuse_reason),
            }
        else:
            ai_report = _generate_scan_loop_ai_report(owner_id, instance, item, clock, signature, cached)
            if ai_report.get("text"):
                updated_cache[symbol] = {
                    "text": ai_report["text"],
                    "signature": signature,
                    "generated_at": utc_now(),
                    "state_label": ai_report.get("state_label") or "",
                    "decision": ai_report.get("decision") or "",
                    "reuse_hint": ai_report.get("reuse_hint") or "",
                }
                changed = True
        row = dict(item)
        row["ai_report"] = ai_report
        output.append(row)
    if changed:
        _update_scan_loop_ai_report_cache(owner_id, str(instance.get("id") or ""), updated_cache)
    return output


def _scan_loop_ai_report_signature(item: dict[str, Any]) -> dict[str, Any]:
    scenario = item.get("scenario_analysis") or {}
    snapshot = item.get("snapshot_summary") or {}
    support = scenario.get("support_levels") or []
    resistance = scenario.get("resistance_levels") or []
    return {
        "symbol": item.get("symbol"),
        "status": item.get("status"),
        "prefilter_matched": bool(item.get("prefilter_matched")),
        "alert_matched": bool(item.get("alert_matched")),
        "decision": item.get("decision"),
        "last": _signature_num(snapshot.get("last") or snapshot.get("price") or scenario.get("last"), 0.05),
        "vwap": _signature_num(snapshot.get("vwap") or (scenario.get("center") if str(scenario.get("center_type") or "") == "vwap" else None), 0.05),
        "vs_vwap_pct": _signature_num((scenario.get("vs_vwap_pct") if isinstance(scenario, dict) else None), 0.05),
        "rvol_bucket": _rvol_bucket(snapshot.get("rvol")),
        "gex_regime": scenario.get("gex_regime") or "unknown",
        "support": [_signature_num((row or {}).get("level"), 0.25) for row in support[:2] if isinstance(row, dict)],
        "resistance": [_signature_num((row or {}).get("level"), 0.25) for row in resistance[:2] if isinstance(row, dict)],
    }

def _signature_num(value: Any, step: float) -> float | None:
    numeric = _numeric(value)
    if numeric is None:
        return None
    if step <= 0:
        return round(numeric, 4)
    return round(round(numeric / step) * step, 4)


def _rvol_bucket(value: Any) -> str:
    numeric = _numeric(value)
    if numeric is None:
        return "unknown"
    if numeric < 0.8:
        return "quiet"
    if numeric < 1.3:
        return "normal"
    if numeric < 1.8:
        return "active"
    return "hot"


def _scan_loop_ai_report_reuse_reason(cached: dict[str, Any], signature: dict[str, Any]) -> str:
    if not cached or not cached.get("text") or not isinstance(cached.get("signature"), dict):
        return ""
    previous = cached.get("signature") or {}
    hard_fields = ("status", "prefilter_matched", "alert_matched", "decision", "rvol_bucket", "gex_regime")
    for field in hard_fields:
        if previous.get(field) != signature.get(field):
            return ""
    for field, threshold in (("last", 0.35), ("vwap", 0.25), ("vs_vwap_pct", 0.25)):
        prev = _numeric(previous.get(field))
        current = _numeric(signature.get(field))
        if prev is None or current is None:
            if prev != current:
                return ""
            continue
        if field in {"last", "vwap"}:
            if prev and abs(current / prev - 1) * 100 > threshold:
                return ""
        elif abs(current - prev) > threshold:
            return ""
    if previous.get("support") != signature.get("support") or previous.get("resistance") != signature.get("resistance"):
        return ""
    generated_at = parse_datetime(str(cached.get("generated_at") or ""))
    if generated_at:
        age_minutes = (datetime.now(timezone.utc) - generated_at.astimezone(timezone.utc)).total_seconds() / 60
        max_age = int(os.getenv("AI_OPTION_SCAN_LOOP_AI_REPORT_MAX_AGE_MINUTES", "45") or 45)
        if age_minutes > max_age:
            return ""
    return "结构参数相似，沿用上一版 AI 盘中剧本"


def _generate_scan_loop_ai_report(
    owner_id: str,
    instance: dict[str, Any],
    item: dict[str, Any],
    clock: dict[str, Any],
    signature: dict[str, Any],
    cached: dict[str, Any],
) -> dict[str, Any]:
    fallback = ((item.get("market_pulse") or {}).get("text") or "").strip()
    payload = {
        "instance": {
            "id": instance.get("id"),
            "name": instance.get("name"),
            "market_data_source": instance.get("market_data_source"),
            "strategy_modes": instance.get("strategy_modes") or [],
        },
        "clock": clock,
        "symbol": item.get("symbol"),
        "observation": item.get("observation"),
        "conclusion": item.get("conclusion"),
        "decision": item.get("decision"),
        "scenario_analysis": item.get("scenario_analysis") or {},
        "demo_tracking": item.get("demo_tracking") or {},
        "market_pulse_draft": fallback,
        "signature": signature,
        "previous_report": {
            "text": cached.get("text") if isinstance(cached, dict) else "",
            "signature": cached.get("signature") if isinstance(cached, dict) else {},
            "generated_at": cached.get("generated_at") if isinstance(cached, dict) else "",
        },
    }
    try:
        answer = ask_ai(
            SCAN_LOOP_AI_REPORT_PROMPT,
            payload,
            str(instance.get("ai_provider") or "deepseek"),
            owner_id=owner_id,
            temperature=0.15,
            response_format={"type": "json_object"},
        )
        parsed = extract_json_object(answer)
    except Exception as exc:
        return {"available": False, "source": "fallback", "error": str(exc), "signature": signature, "text": fallback}
    text = str((parsed or {}).get("text") or "").strip()
    if not text:
        return {"available": False, "source": "fallback", "signature": signature, "text": fallback}
    return {
        "available": True,
        "source": "ai",
        "reused": False,
        "signature": signature,
        "text": text,
        "state_label": (parsed or {}).get("state_label") or "",
        "decision": (parsed or {}).get("decision") or "",
        "reuse_hint": (parsed or {}).get("reuse_hint") or "",
    }


def _scan_loop_ai_cached_text(text: str, item: dict[str, Any], reason: str) -> str:
    if not text:
        return ""
    live_line = f"报告状态: {reason}；本拍 {item.get('observation') or '--'}；{item.get('decision') or '--'}。"
    return f"{text}\n\n{live_line}"


def _update_scan_loop_ai_report_cache(owner_id: str, instance_id: str, cache: dict[str, Any]) -> None:
    if not instance_id:
        return
    with connect() as db:
        db.execute(
            "UPDATE scan_loop_instances SET ai_report_cache_json = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
            (json.dumps(cache, ensure_ascii=False), utc_now(), normalize_owner_id(owner_id), instance_id),
        )


def _opportunity_direction(snapshot: dict[str, Any]) -> str:
    for field in ("direction", "derived_direction", "ai_direction", "stance", "bias", "signal_direction", "trend"):
        raw = str(snapshot.get(field) or "").strip().lower()
        if not raw:
            continue
        if any(token in raw for token in ("bull", "long", "up", "call", "rally", "positive")):
            return "bullish"
        if any(token in raw for token in ("bear", "short", "down", "put", "fade", "negative")):
            return "bearish"
        if any(token in raw for token in ("neutral", "range", "sideways", "volatility")):
            return "neutral"
    last = _numeric(snapshot.get("last"))
    open_price = _numeric(snapshot.get("open"))
    if last is not None and open_price not in (None, 0):
        change_pct = (last - open_price) / open_price * 100
        if change_pct >= 0.5:
            return "bullish"
        if change_pct <= -0.5:
            return "bearish"
        return "neutral"
    return "unknown"


def _opportunity_strategy_structure(instance: dict[str, Any], direction: str, snapshot: dict[str, Any]) -> str:
    snapshot_strategy = str(snapshot.get("strategy_structure") or snapshot.get("strategy_type") or "").strip().lower()
    if snapshot_strategy:
        mapped = {
            "bull_call_spread": "debit_call_spread",
            "bear_put_spread": "debit_put_spread",
            "bull_put_spread": "credit_put_spread",
            "bear_call_spread": "credit_call_spread",
        }.get(snapshot_strategy)
        if mapped:
            return mapped
        return snapshot_strategy
    modes = [str(mode or "").strip().lower() for mode in (instance.get("strategy_modes") or []) if str(mode or "").strip()]
    primary_mode = modes[0] if modes else "single_leg"
    if primary_mode == "single_leg":
        return "single_leg_call" if direction == "bullish" else "single_leg_put" if direction == "bearish" else "single_leg"
    if primary_mode == "spread":
        return "debit_call_spread" if direction == "bullish" else "debit_put_spread" if direction == "bearish" else "custom_multi_leg"
    if primary_mode == "credit_spread":
        return "credit_put_spread" if direction == "bullish" else "credit_call_spread" if direction == "bearish" else "custom_multi_leg"
    if primary_mode in {"straddle", "strangle", "iron_condor", "butterfly", "calendar", "diagonal", "covered_call", "cash_secured_put", "collar"}:
        return primary_mode
    return "custom_multi_leg"


def _opportunity_entry_reference(snapshot: dict[str, Any], direction: str, strategy_structure: str) -> dict[str, Any]:
    underlying_reference = _numeric(snapshot.get("last"))
    if underlying_reference is None:
        underlying_reference = _numeric(snapshot.get("close"))
    entry_side = "reference"
    if strategy_structure.startswith("debit") or strategy_structure in {"single_leg_call", "long_straddle", "long_strangle"}:
        entry_side = "debit"
    elif strategy_structure.startswith("credit") or strategy_structure in {"covered_call", "cash_secured_put"}:
        entry_side = "credit"
    return {
        "underlying_reference": round(underlying_reference, 2) if underlying_reference is not None else None,
        "entry_side": entry_side,
        "entry_reference": round(underlying_reference, 2) if underlying_reference is not None else None,
        "direction": direction,
        "data_timestamp": snapshot.get("data_timestamp"),
        "source": "scan_loop_snapshot",
    }


def _opportunity_risk_plan(snapshot: dict[str, Any], direction: str, strategy_structure: str, entry_reference: dict[str, Any]) -> dict[str, Any]:
    underlying_reference = _numeric(entry_reference.get("underlying_reference")) or _numeric(snapshot.get("last"))
    if underlying_reference is None:
        underlying_reference = 0.0
    direction_multiplier = 1 if direction == "bullish" else -1 if direction == "bearish" else 0
    tp1 = round(underlying_reference * (1 + direction_multiplier * 0.02), 2) if direction_multiplier else round(underlying_reference * 1.02, 2)
    tp2 = round(underlying_reference * (1 + direction_multiplier * 0.04), 2) if direction_multiplier else round(underlying_reference * 1.04, 2)
    stop = round(underlying_reference * (1 - direction_multiplier * 0.02), 2) if direction_multiplier else round(underlying_reference * 0.98, 2)
    vah = _numeric(snapshot.get("volume_profile_value_area_high"))
    val = _numeric(snapshot.get("volume_profile_value_area_low"))
    if direction == "bullish":
        if vah and vah > underlying_reference:
            tp1 = round(vah, 2)
        if val and val < underlying_reference:
            stop = round(val, 2)
    elif direction == "bearish":
        if val and val < underlying_reference:
            tp1 = round(val, 2)
        if vah and vah > underlying_reference:
            stop = round(vah, 2)
    invalidation = "trend_fades" if direction == "bullish" else "trend_breaks" if direction == "bearish" else "structure_not_confirmed"
    if strategy_structure.startswith("credit"):
        stop = round(underlying_reference * (1 - direction_multiplier * 0.03), 2) if direction_multiplier else round(underlying_reference * 1.03, 2)
    return {
        "take_profit": {
            "type": "reference",
            "levels": [
                {"label": "TP1", "underlying_reference": tp1, "note": "第一参考止盈"},
                {"label": "TP2", "underlying_reference": tp2, "note": "第二参考止盈"},
            ],
        },
        "stop_loss": {
            "type": "reference",
            "underlying_reference": stop,
            "note": "参考止损",
        },
        "invalidation": invalidation,
        "latest_exit": "收盘前复核",
        "volatility": {
            "rv20": snapshot.get("rv20"),
            "rv60": snapshot.get("rv60"),
            "rv_rank": snapshot.get("rv_rank"),
            "note": "雷达机会以标的 RV 为背景；进入期权精扫后再用 IV/RV 与盘口确认。",
        },
        "volume_profile": {
            "poc": snapshot.get("volume_profile_poc"),
            "value_area_low": snapshot.get("volume_profile_value_area_low"),
            "value_area_high": snapshot.get("volume_profile_value_area_high"),
            "position": snapshot.get("volume_profile_position"),
            "note": "止损/止盈优先参考 Value Area 边界，避免在筹码峰阻力/支撑前追单。",
        },
        "source": "scan_loop_snapshot",
    }


def _opportunity_direction_label(direction: str) -> str:
    return {
        "bullish": "看涨",
        "bearish": "看跌",
        "neutral": "中性",
        "volatility_long": "做多波动",
        "volatility_short": "做空波动",
        "income": "收租",
        "hedge": "对冲",
    }.get(direction, "待判断")


def _opportunity_strategy_label(strategy_structure: str) -> str:
    return {
        "single_leg_call": "单腿 Call",
        "single_leg_put": "单腿 Put",
        "debit_call_spread": "Call Debit Spread",
        "debit_put_spread": "Put Debit Spread",
        "credit_call_spread": "Call Credit Spread",
        "credit_put_spread": "Put Credit Spread",
        "long_straddle": "Long Straddle",
        "long_strangle": "Long Strangle",
        "short_straddle": "Short Straddle",
        "short_strangle": "Short Strangle",
        "iron_condor": "Iron Condor",
        "butterfly": "Butterfly",
        "calendar": "Calendar",
        "diagonal": "Diagonal",
        "covered_call": "Covered Call",
        "cash_secured_put": "Cash Secured Put",
        "collar": "Collar",
    }.get(strategy_structure, "观察结构")


def _opportunity_legs(snapshot: dict[str, Any], direction: str, strategy_structure: str, entry_reference: dict[str, Any]) -> list[dict[str, Any]]:
    reference = _numeric(entry_reference.get("underlying_reference")) or _numeric(snapshot.get("last")) or _numeric(snapshot.get("close")) or 0.0
    strike_step = _opportunity_strike_step(reference)
    center_strike = _round_to_step(reference, strike_step)
    expiration = _opportunity_expiration(snapshot)

    def option_leg(action: str, right: str, role: str, strike: float | None, quantity_ratio: int = 1) -> dict[str, Any]:
        return {
            "asset_type": "option",
            "action": action,
            "right": right,
            "expiration": expiration,
            "strike": round(strike, 2) if strike is not None else None,
            "quantity_ratio": quantity_ratio,
            "role": role,
        }

    def underlying_leg(action: str, role: str) -> dict[str, Any]:
        return {
            "asset_type": "underlying",
            "action": action,
            "right": None,
            "expiration": None,
            "strike": None,
            "quantity_ratio": 1,
            "role": role,
        }

    if strategy_structure == "single_leg_call":
        return [option_leg("buy", "call", "long_call", center_strike)]
    if strategy_structure == "single_leg_put":
        return [option_leg("buy", "put", "long_put", center_strike)]
    if strategy_structure == "debit_call_spread":
        return [option_leg("buy", "call", "long_call", center_strike), option_leg("sell", "call", "short_call", center_strike + strike_step)]
    if strategy_structure == "debit_put_spread":
        return [option_leg("buy", "put", "long_put", center_strike), option_leg("sell", "put", "short_put", center_strike - strike_step)]
    if strategy_structure == "credit_call_spread":
        return [option_leg("sell", "call", "short_call", center_strike), option_leg("buy", "call", "long_call", center_strike + strike_step)]
    if strategy_structure == "credit_put_spread":
        return [option_leg("sell", "put", "short_put", center_strike), option_leg("buy", "put", "long_put", center_strike - strike_step)]
    if strategy_structure == "long_straddle":
        return [option_leg("buy", "call", "long_call", center_strike), option_leg("buy", "put", "long_put", center_strike)]
    if strategy_structure == "long_strangle":
        return [option_leg("buy", "call", "long_call", center_strike + strike_step), option_leg("buy", "put", "long_put", center_strike - strike_step)]
    if strategy_structure == "short_straddle":
        return [option_leg("sell", "call", "short_call", center_strike), option_leg("sell", "put", "short_put", center_strike)]
    if strategy_structure == "short_strangle":
        return [option_leg("sell", "call", "short_call", center_strike + strike_step), option_leg("sell", "put", "short_put", center_strike - strike_step)]
    if strategy_structure == "iron_condor":
        return [
            option_leg("sell", "put", "short_put", center_strike - strike_step),
            option_leg("buy", "put", "long_put", center_strike - strike_step * 2),
            option_leg("sell", "call", "short_call", center_strike + strike_step),
            option_leg("buy", "call", "long_call", center_strike + strike_step * 2),
        ]
    if strategy_structure == "butterfly":
        return [
            option_leg("buy", "call", "long_wing", center_strike - strike_step),
            option_leg("sell", "call", "short_body", center_strike, quantity_ratio=2),
            option_leg("buy", "call", "long_wing", center_strike + strike_step),
        ]
    if strategy_structure == "calendar":
        return [option_leg("buy", "call", "long_calendar_leg", center_strike), option_leg("sell", "call", "short_calendar_leg", center_strike)]
    if strategy_structure == "diagonal":
        return [option_leg("buy", "call", "long_diagonal_leg", center_strike - strike_step), option_leg("sell", "call", "short_diagonal_leg", center_strike + strike_step)]
    if strategy_structure == "covered_call":
        return [underlying_leg("buy", "long_stock"), option_leg("sell", "call", "short_call", center_strike + strike_step)]
    if strategy_structure == "cash_secured_put":
        return [option_leg("sell", "put", "short_put", center_strike - strike_step)]
    if strategy_structure == "collar":
        return [
            underlying_leg("buy", "long_stock"),
            option_leg("buy", "put", "protective_put", center_strike - strike_step),
            option_leg("sell", "call", "covered_call", center_strike + strike_step),
        ]
    if strategy_structure == "custom_multi_leg":
        return [option_leg("buy", "call", "long_call_reference", center_strike), option_leg("sell", "call", "short_call_reference", center_strike + strike_step)]
    if direction == "bearish":
        return [option_leg("buy", "put", "long_put", center_strike)]
    if direction == "income":
        return [option_leg("sell", "put", "short_put", center_strike - strike_step)]
    if direction == "hedge":
        return [underlying_leg("buy", "long_stock")]
    return [option_leg("buy", "call", "long_call", center_strike)]


def _opportunity_payoff(
    snapshot: dict[str, Any],
    direction: str,
    strategy_structure: str,
    entry_reference: dict[str, Any],
    risk_plan: dict[str, Any],
    legs: list[dict[str, Any]],
) -> dict[str, Any]:
    entry_price = _numeric(entry_reference.get("entry_reference")) or _numeric(entry_reference.get("underlying_reference")) or _numeric(snapshot.get("last")) or 0.0
    stop_reference = _numeric((risk_plan.get("stop_loss") or {}).get("underlying_reference"))
    target_reference = None
    for level in (risk_plan.get("take_profit") or {}).get("levels") or []:
        next_reference = _numeric((level or {}).get("underlying_reference"))
        if next_reference is not None:
            target_reference = next_reference
    breakeven_shift = _opportunity_breakeven_shift(strategy_structure, direction)
    breakeven_points = [round(entry_price + breakeven_shift, 2)] if breakeven_shift is not None else []
    risk_value = abs(entry_price - stop_reference) if stop_reference is not None else 0.0
    reward_value = abs(target_reference - entry_price) if target_reference is not None else 0.0
    defined_risk = _opportunity_defined_risk_estimate(strategy_structure, legs, snapshot)
    scenario_table = _opportunity_payoff_scenarios(strategy_structure, legs, snapshot, entry_price, defined_risk)
    return {
        "valuation_mode": "reference_price",
        "entry_side": entry_reference.get("entry_side"),
        "entry_reference": round(entry_price, 2) if entry_price else None,
        "max_loss_reference": round(stop_reference, 2) if stop_reference is not None else None,
        "max_profit_reference": round(target_reference, 2) if target_reference is not None else None,
        "breakeven_points": breakeven_points,
        "profit_zone": _opportunity_profit_zone(direction, breakeven_points, target_reference),
        "loss_zone": _opportunity_loss_zone(direction, breakeven_points, stop_reference),
        "risk_reward_ratio": round(reward_value / risk_value, 2) if risk_value > 0 and reward_value > 0 else None,
        "legs_count": len(legs),
        "defined_risk_estimate": defined_risk,
        "scenario_table": scenario_table,
        "payoff_curve": scenario_table,
    }


def _opportunity_defined_risk_estimate(strategy_structure: str, legs: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
    option_legs = [leg for leg in legs if leg.get("asset_type") == "option" and _numeric(leg.get("strike")) is not None]
    strikes = sorted({_numeric(leg.get("strike")) for leg in option_legs if _numeric(leg.get("strike")) is not None})
    contract_multiplier = int(_numeric(snapshot.get("contract_multiplier")) or 100)
    if strategy_structure in {"debit_call_spread", "debit_put_spread", "credit_call_spread", "credit_put_spread"} and len(strikes) >= 2:
        width = abs(float(strikes[-1]) - float(strikes[0]))
        entry_side = "credit" if strategy_structure.startswith("credit") else "debit"
        premium = _numeric(snapshot.get("net_credit" if entry_side == "credit" else "net_debit"))
        if premium is None:
            premium = round(width * (0.35 if entry_side == "credit" else 0.4), 2)
        if entry_side == "credit":
            max_profit = premium * contract_multiplier
            max_loss = max(width - premium, 0) * contract_multiplier
        else:
            max_loss = premium * contract_multiplier
            max_profit = max(width - premium, 0) * contract_multiplier
        return {
            "mode": "vertical_spread",
            "entry_side": entry_side,
            "width": round(width, 2),
            "estimated_premium": round(premium, 2),
            "max_profit_per_contract": round(max_profit, 2),
            "max_loss_per_contract": round(max_loss, 2),
            "risk_reward_ratio": round(max_profit / max_loss, 2) if max_loss > 0 else None,
        }
    if strategy_structure == "iron_condor" and len(strikes) >= 4:
        put_width = abs(float(strikes[1]) - float(strikes[0]))
        call_width = abs(float(strikes[-1]) - float(strikes[-2]))
        width = max(put_width, call_width)
        credit = _numeric(snapshot.get("net_credit")) or round(width * 0.3, 2)
        max_profit = credit * contract_multiplier
        max_loss = max(width - credit, 0) * contract_multiplier
        return {
            "mode": "iron_condor",
            "entry_side": "credit",
            "width": round(width, 2),
            "estimated_premium": round(credit, 2),
            "breakeven_points": [round(float(strikes[1]) - credit, 2), round(float(strikes[-2]) + credit, 2)],
            "max_profit_per_contract": round(max_profit, 2),
            "max_loss_per_contract": round(max_loss, 2),
            "risk_reward_ratio": round(max_profit / max_loss, 2) if max_loss > 0 else None,
        }
    if strategy_structure == "butterfly" and len(strikes) >= 3:
        lower, body, upper = float(strikes[0]), float(strikes[1]), float(strikes[-1])
        width = min(abs(body - lower), abs(upper - body))
        debit = _numeric(snapshot.get("net_debit")) or _numeric(snapshot.get("estimated_premium")) or round(width * 0.25, 2)
        max_loss = debit * contract_multiplier
        max_profit = max(width - debit, 0) * contract_multiplier
        return {
            "mode": "butterfly",
            "entry_side": "debit",
            "width": round(width, 2),
            "body_strike": round(body, 2),
            "estimated_premium": round(debit, 2),
            "breakeven_points": [round(lower + debit, 2), round(upper - debit, 2)],
            "max_profit_per_contract": round(max_profit, 2),
            "max_loss_per_contract": round(max_loss, 2),
            "risk_reward_ratio": round(max_profit / max_loss, 2) if max_loss > 0 else None,
        }
    if strategy_structure in {"calendar", "diagonal"} and len(option_legs) >= 2:
        width = abs(float(strikes[-1]) - float(strikes[0])) if len(strikes) >= 2 else 0.0
        debit = _numeric(snapshot.get("net_debit")) or _numeric(snapshot.get("estimated_premium"))
        if debit is None:
            debit = round(max(width, _opportunity_strike_step(_numeric(snapshot.get("last")) or 100.0)) * 0.25, 2)
        mode = "diagonal_reference" if strategy_structure == "diagonal" else "calendar_reference"
        return {
            "mode": mode,
            "entry_side": "debit",
            "width": round(width, 2) if width else None,
            "estimated_premium": round(debit, 2),
            "max_profit_per_contract": None,
            "max_loss_per_contract": round(debit * contract_multiplier, 2),
            "risk_reward_ratio": None,
            "valuation_note": "calendar/diagonal 的远月剩余时间价值无法仅用到期内在价值锁定，最大盈利作为情景参考展示。",
        }
    if strategy_structure in {"long_straddle", "long_strangle", "short_straddle", "short_strangle"} and len(option_legs) >= 2:
        short_vol = strategy_structure.startswith("short")
        width = abs(float(strikes[-1]) - float(strikes[0])) if len(strikes) >= 2 else 0.0
        premium = _numeric(snapshot.get("net_credit" if short_vol else "net_debit")) or _numeric(snapshot.get("estimated_premium"))
        if premium is None:
            reference = _numeric(snapshot.get("last")) or _numeric(snapshot.get("close")) or 100.0
            premium = round(max(width, reference * 0.035), 2)
        max_profit = premium * contract_multiplier if short_vol else None
        max_loss = None if short_vol else premium * contract_multiplier
        return {
            "mode": "volatility_combo",
            "entry_side": "credit" if short_vol else "debit",
            "width": round(width, 2) if width else None,
            "estimated_premium": round(premium, 2),
            "max_profit_per_contract": round(max_profit, 2) if max_profit is not None else None,
            "max_loss_per_contract": round(max_loss, 2) if max_loss is not None else None,
            "risk_reward_ratio": None,
        }
    if strategy_structure in {"single_leg_call", "single_leg_put"}:
        premium = _numeric(snapshot.get("ask")) or _numeric(snapshot.get("option_ask")) or _numeric(snapshot.get("estimated_premium"))
        return {
            "mode": "single_leg",
            "entry_side": "debit",
            "estimated_premium": round(premium, 2) if premium is not None else None,
            "max_loss_per_contract": round(premium * contract_multiplier, 2) if premium is not None else None,
            "max_profit_per_contract": None,
            "risk_reward_ratio": None,
        }
    return {"mode": "reference_only"}


def _opportunity_payoff_scenarios(
    strategy_structure: str,
    legs: list[dict[str, Any]],
    snapshot: dict[str, Any],
    entry_price: float,
    defined_risk: dict[str, Any],
) -> list[dict[str, Any]]:
    option_legs = [leg for leg in legs if leg.get("asset_type") == "option" and _numeric(leg.get("strike")) is not None]
    if not option_legs or entry_price <= 0:
        return []
    contract_multiplier = int(_numeric(snapshot.get("contract_multiplier")) or 100)
    premium = _numeric(defined_risk.get("estimated_premium"))
    entry_side = str(defined_risk.get("entry_side") or "debit")
    premium_cashflow = 0.0
    if premium is not None:
        premium_cashflow = premium * contract_multiplier * (1 if entry_side == "credit" else -1)
    strikes = sorted({_numeric(leg.get("strike")) for leg in option_legs if _numeric(leg.get("strike")) is not None})
    raw_points: list[tuple[str, float]] = [
        ("-10%", entry_price * 0.9),
        ("-5%", entry_price * 0.95),
        ("参考价", entry_price),
        ("+5%", entry_price * 1.05),
        ("+10%", entry_price * 1.1),
    ]
    for strike in strikes:
        raw_points.append((f"行权价 {float(strike):.2f}", float(strike)))
    for breakeven in defined_risk.get("breakeven_points") or []:
        numeric_breakeven = _numeric(breakeven)
        if numeric_breakeven is not None:
            raw_points.append((f"BE {numeric_breakeven:.2f}", numeric_breakeven))

    deduped: dict[float, str] = {}
    for label, point in raw_points:
        rounded = round(point, 2)
        if rounded <= 0:
            continue
        deduped.setdefault(rounded, label)

    scenarios: list[dict[str, Any]] = []
    for underlying in sorted(deduped):
        intrinsic_value = 0.0
        for leg in option_legs:
            strike = _numeric(leg.get("strike"))
            if strike is None:
                continue
            right = str(leg.get("right") or "call")
            action = str(leg.get("action") or "buy")
            quantity = abs(int(_numeric(leg.get("quantity_ratio")) or 1))
            intrinsic = max(underlying - strike, 0.0) if right == "call" else max(strike - underlying, 0.0)
            sign = 1 if action == "buy" else -1
            intrinsic_value += intrinsic * sign * quantity * contract_multiplier
        pnl = intrinsic_value + premium_cashflow
        scenarios.append(
            {
                "label": deduped[underlying],
                "underlying": underlying,
                "intrinsic_value": round(intrinsic_value, 2),
                "pnl_per_contract": round(pnl, 2),
                "zone": "profit" if pnl > 0 else "loss" if pnl < 0 else "flat",
                "valuation_mode": "expiration_intrinsic_approx",
                "note": "calendar/diagonal 为近月到期内在价值近似，未计远月剩余时间价值。"
                if strategy_structure in {"calendar", "diagonal"}
                else None,
            }
        )
    return scenarios[:15]


def _opportunity_validation(strategy_structure: str, legs: list[dict[str, Any]], payoff: dict[str, Any], gex_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    expected_legs = {
        "single_leg_call": 1,
        "single_leg_put": 1,
        "single_leg": 1,
        "debit_call_spread": 2,
        "debit_put_spread": 2,
        "credit_call_spread": 2,
        "credit_put_spread": 2,
        "long_straddle": 2,
        "long_strangle": 2,
        "short_straddle": 2,
        "short_strangle": 2,
        "iron_condor": 4,
        "butterfly": 3,
        "calendar": 2,
        "diagonal": 2,
        "covered_call": 2,
        "cash_secured_put": 1,
        "collar": 3,
    }.get(strategy_structure)
    warnings: list[str] = []
    missing_fields: list[str] = []
    if expected_legs is not None and len(legs) != expected_legs:
        warnings.append(f"expected_{expected_legs}_legs")
    for leg in legs:
        for field in ("action", "role", "asset_type"):
            if leg.get(field) in (None, ""):
                missing_fields.append(field)
        if leg.get("asset_type") == "option":
            if leg.get("strike") in (None, ""):
                missing_fields.append("strike")
            if leg.get("expiration") in (None, ""):
                missing_fields.append("expiration")
    if not payoff.get("breakeven_points"):
        warnings.append("no_breakeven_reference")
    warnings.extend(_opportunity_leg_structure_warnings(strategy_structure, legs))
    warnings.extend(_opportunity_gex_strategy_warnings(strategy_structure, gex_snapshot or {}))
    return {
        "status": "complete" if not warnings and not missing_fields else "reference_only",
        "expected_leg_count": expected_legs,
        "actual_leg_count": len(legs),
        "warnings": warnings,
        "missing_fields": sorted(set(missing_fields)),
        "payoff_mode": payoff.get("valuation_mode") or "unknown",
        "defined_risk_mode": (payoff.get("defined_risk_estimate") or {}).get("mode"),
    }


def _opportunity_leg_structure_warnings(strategy_structure: str, legs: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    option_legs = [leg for leg in legs if leg.get("asset_type") == "option"]
    if strategy_structure in {"debit_call_spread", "credit_call_spread"}:
        if {leg.get("right") for leg in option_legs} != {"call"}:
            warnings.append("call_spread_requires_call_legs")
        if {leg.get("action") for leg in option_legs} != {"buy", "sell"}:
            warnings.append("vertical_requires_long_and_short")
    if strategy_structure in {"debit_put_spread", "credit_put_spread"}:
        if {leg.get("right") for leg in option_legs} != {"put"}:
            warnings.append("put_spread_requires_put_legs")
        if {leg.get("action") for leg in option_legs} != {"buy", "sell"}:
            warnings.append("vertical_requires_long_and_short")
    if strategy_structure == "iron_condor":
        rights = [leg.get("right") for leg in option_legs]
        actions = [leg.get("action") for leg in option_legs]
        if rights.count("call") != 2 or rights.count("put") != 2:
            warnings.append("iron_condor_requires_two_calls_two_puts")
        if actions.count("buy") != 2 or actions.count("sell") != 2:
            warnings.append("iron_condor_requires_two_longs_two_shorts")
    return warnings


def _opportunity_gex_strategy_warnings(strategy_structure: str, gex_snapshot: dict[str, Any]) -> list[str]:
    regime = str((gex_snapshot or {}).get("regime") or "unknown")
    nearest_wall = str((gex_snapshot or {}).get("nearest_wall") or "")
    distance = _numeric((gex_snapshot or {}).get("nearest_wall_distance_pct"))
    warnings: list[str] = []
    if regime == "positive_gamma" and strategy_structure in {"long_straddle", "long_strangle"}:
        warnings.append("positive_gamma_may_dampen_long_vol")
    if regime == "negative_gamma" and strategy_structure in {"iron_condor", "short_straddle", "short_strangle", "credit_call_spread", "credit_put_spread"}:
        warnings.append("negative_gamma_raises_short_vol_risk")
    if nearest_wall == "call_wall" and strategy_structure in {"single_leg_call", "debit_call_spread"} and distance is not None and distance <= 1.5:
        warnings.append("near_call_wall_caps_upside_reference")
    if nearest_wall == "put_wall" and strategy_structure in {"single_leg_put", "debit_put_spread"} and distance is not None and distance <= 1.5:
        warnings.append("near_put_wall_caps_downside_reference")
    return warnings


def _opportunity_expiration(snapshot: dict[str, Any]) -> str | None:
    for field in ("expiration", "expires_at", "option_expiration", "expiry"):
        raw = snapshot.get(field)
        if raw:
            parsed = parse_datetime(str(raw))
            if parsed:
                return parsed.date().isoformat()
            raw_text = str(raw).strip()
            if len(raw_text) >= 10:
                return raw_text[:10]
    return None


def _opportunity_strike_step(reference: float) -> float:
    if reference >= 500:
        return 5.0
    if reference >= 100:
        return 2.5
    if reference >= 20:
        return 1.0
    return 0.5


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(round(value / step) * step, 2)


def _opportunity_breakeven_shift(strategy_structure: str, direction: str) -> float:
    if strategy_structure.startswith("credit") or strategy_structure in {"short_straddle", "short_strangle", "iron_condor"}:
        return -1.0 if direction != "bearish" else 1.0
    if direction == "bearish":
        return -1.0
    if direction == "neutral":
        return 0.0
    return 1.0


def _opportunity_profit_zone(direction: str, breakeven_points: list[float], target_reference: float | None) -> str:
    if direction == "bearish":
        return f"underlying_below_{breakeven_points[0]:.2f}_at_reference" if breakeven_points else "underlying_below_reference_at_expiration"
    if direction == "neutral":
        return "underlying_within_expected_range"
    if direction == "income":
        return "premium_decay_and_underlying_stable"
    if direction == "hedge":
        return "risk_boundary_protected"
    if breakeven_points:
        return f"underlying_above_{breakeven_points[0]:.2f}_at_reference"
    if target_reference is not None:
        return f"underlying_above_{target_reference:.2f}_at_reference"
    return "underlying_above_reference_at_expiration"


def _opportunity_loss_zone(direction: str, breakeven_points: list[float], stop_reference: float | None) -> str:
    if direction == "bearish":
        return f"underlying_above_{breakeven_points[0]:.2f}_at_reference" if breakeven_points else "underlying_above_reference_at_expiration"
    if direction == "neutral":
        return "underlying_outside_expected_range"
    if direction == "income":
        return "premium_risk_expands"
    if direction == "hedge":
        return "hedge_boundary_broken"
    if breakeven_points:
        return f"underlying_below_{breakeven_points[0]:.2f}_at_reference"
    if stop_reference is not None:
        return f"underlying_below_{stop_reference:.2f}_at_reference"
    return "underlying_below_reference_at_expiration"


def _opportunity_gex_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    gex = snapshot.get("gex_snapshot") if isinstance(snapshot.get("gex_snapshot"), dict) else {}
    if not gex and isinstance(snapshot.get("gex"), dict):
        gex = snapshot.get("gex") or {}
    current = {
        "available": bool(snapshot.get("gex_available") or gex.get("available")),
        "regime": str(snapshot.get("gex_regime") or snapshot.get("regime") or gex.get("regime") or "unknown"),
        "nearest_wall": str(snapshot.get("gex_nearest_wall") or snapshot.get("nearest_wall") or gex.get("nearest_wall") or ""),
        "nearest_wall_distance_pct": _numeric(snapshot.get("gex_nearest_wall_distance_pct")) if snapshot.get("gex_nearest_wall_distance_pct") is not None else (_numeric(snapshot.get("nearest_wall_distance_pct")) if snapshot.get("nearest_wall_distance_pct") is not None else (_numeric(gex.get("nearest_wall_distance_pct")) or 0.0)),
        "call_wall": _numeric(snapshot.get("gex_call_wall")) if snapshot.get("gex_call_wall") is not None else (_numeric(snapshot.get("call_wall")) if snapshot.get("call_wall") is not None else (_numeric(gex.get("call_wall")) or 0.0)),
        "put_wall": _numeric(snapshot.get("gex_put_wall")) if snapshot.get("gex_put_wall") is not None else (_numeric(snapshot.get("put_wall")) if snapshot.get("put_wall") is not None else (_numeric(gex.get("put_wall")) or 0.0)),
        "gamma_flip": _numeric(snapshot.get("gex_gamma_flip")) if snapshot.get("gex_gamma_flip") is not None else (_numeric(snapshot.get("gamma_flip")) if snapshot.get("gamma_flip") is not None else (_numeric(gex.get("gamma_flip")) or 0.0)),
        "tailwind": str(snapshot.get("gex_tailwind") or snapshot.get("tailwind") or gex.get("tailwind") or ""),
        "pinning_risk": str(snapshot.get("gex_pinning_risk") or snapshot.get("pinning_risk") or gex.get("pinning_risk") or ""),
        "trend_acceleration_risk": str(snapshot.get("gex_trend_acceleration_risk") or snapshot.get("trend_acceleration_risk") or gex.get("trend_acceleration_risk") or ""),
    }
    risk = _derive_gex_structural_risk(current)
    current["pinning_risk"] = current["pinning_risk"] or risk["pinning_risk"]
    current["trend_acceleration_risk"] = current["trend_acceleration_risk"] or risk["trend_acceleration_risk"]
    if not current["available"] and current["regime"] == "unknown" and not current["nearest_wall"]:
        return {"available": False, "regime": "unknown"}
    return current


def _derive_gex_structural_risk(gex: dict[str, Any]) -> dict[str, str]:
    regime = str(gex.get("regime") or "unknown")
    tailwind = str(gex.get("tailwind") or "")
    distance = _numeric(gex.get("nearest_wall_distance_pct"))
    near_wall = distance is not None and distance > 0 and distance <= 1.5
    very_near_wall = distance is not None and distance > 0 and distance <= 0.75
    pinning_risk = "unknown"
    trend_acceleration_risk = "unknown"
    if regime == "positive_gamma" or tailwind == "pinning_and_mean_reversion":
        pinning_risk = "high" if very_near_wall else "medium" if near_wall else "medium"
        trend_acceleration_risk = "low"
    elif regime == "negative_gamma" or tailwind == "short_gamma_acceleration":
        pinning_risk = "medium" if near_wall else "low"
        trend_acceleration_risk = "high" if near_wall else "medium"
    elif regime in {"neutral", "mixed"}:
        pinning_risk = "medium" if near_wall else "low"
        trend_acceleration_risk = "medium" if near_wall else "low"
    return {"pinning_risk": pinning_risk, "trend_acceleration_risk": trend_acceleration_risk}


def _opportunity_gex_changes(opportunity: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    initial = _opportunity_gex_snapshot(opportunity.get("gex_snapshot") or {})
    current = _opportunity_gex_snapshot(snapshot or {})
    if current.get("regime") == "unknown" and not current.get("nearest_wall") and not current.get("available"):
        return []
    changes: list[dict[str, Any]] = []
    if initial.get("regime") != "unknown" and current.get("regime") != "unknown" and initial.get("regime") != current.get("regime"):
        changes.append({"type": "regime_change", "from": initial.get("regime"), "to": current.get("regime")})
    if initial.get("nearest_wall") and current.get("nearest_wall") and initial.get("nearest_wall") != current.get("nearest_wall"):
        changes.append({"type": "nearest_wall_change", "from": initial.get("nearest_wall"), "to": current.get("nearest_wall")})
    initial_wall = _numeric(initial.get("call_wall")) if initial.get("call_wall") not in (None, "") else None
    current_wall = _numeric(current.get("call_wall")) if current.get("call_wall") not in (None, "") else None
    if initial_wall is not None and current_wall is not None and abs(initial_wall - current_wall) >= max(0.5, abs(initial_wall) * 0.01):
        changes.append({"type": "call_wall_change", "from": round(initial_wall, 2), "to": round(current_wall, 2)})
    initial_put = _numeric(initial.get("put_wall")) if initial.get("put_wall") not in (None, "") else None
    current_put = _numeric(current.get("put_wall")) if current.get("put_wall") not in (None, "") else None
    if initial_put is not None and current_put is not None and abs(initial_put - current_put) >= max(0.5, abs(initial_put) * 0.01):
        changes.append({"type": "put_wall_change", "from": round(initial_put, 2), "to": round(current_put, 2)})
    initial_flip = _numeric(initial.get("gamma_flip")) if initial.get("gamma_flip") not in (None, "") else None
    current_flip = _numeric(current.get("gamma_flip")) if current.get("gamma_flip") not in (None, "") else None
    if initial_flip is not None and current_flip is not None and abs(initial_flip - current_flip) >= max(0.5, abs(initial_flip) * 0.01):
        changes.append({"type": "gamma_flip_change", "from": round(initial_flip, 2), "to": round(current_flip, 2)})
    return changes


def _opportunity_gex_change_summary(changes: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for change in changes[:3]:
        change_type = str(change.get("type") or "").replace("_", " ")
        parts.append(f"{change_type}: {change.get('from')} -> {change.get('to')}")
    return "；".join(parts) if parts else "结构未变化"


def _opportunity_eod_plan_body(opportunity: dict[str, Any], snapshot: dict[str, Any], gex_changes: list[dict[str, Any]]) -> str:
    parts = [f"{opportunity['symbol']} 收盘复盘：机会仍在跟踪中。"]
    if gex_changes:
        parts.append(f"GEX 变化：{_opportunity_gex_change_summary(gex_changes)}。")
    plan = _opportunity_next_session_plan(opportunity, snapshot, gex_changes)
    parts.append(f"明日重点：{'; '.join(plan.get('checklist') or [])}。")
    return " ".join(parts)


def _opportunity_weekend_plan_body(opportunity: dict[str, Any], snapshot: dict[str, Any], gex_changes: list[dict[str, Any]]) -> str:
    parts = [f"{opportunity['symbol']} 周末计划：该机会暂停实时跟踪，保留复盘参考。"]
    if gex_changes:
        parts.append(f"周末前 GEX 变化：{_opportunity_gex_change_summary(gex_changes)}。")
    plan = _opportunity_weekend_plan(opportunity, snapshot, gex_changes)
    parts.append(f"下周重点：{'; '.join(plan.get('checklist') or [])}。")
    return " ".join(parts)


def _opportunity_next_session_plan(opportunity: dict[str, Any], snapshot: dict[str, Any], gex_changes: list[dict[str, Any]]) -> dict[str, Any]:
    risk = opportunity.get("risk_plan") or {}
    entry = opportunity.get("entry_reference") or {}
    checklist = [
        f"开盘后确认标的是否仍围绕参考价 {fmt_float(entry.get('underlying_reference'))}",
        f"复核止损 {fmt_float((risk.get('stop_loss') or {}).get('underlying_reference'))}",
        "重新查看成交价、价差和数据新鲜度",
    ]
    if gex_changes:
        checklist.append("优先确认 GEX regime/wall 是否继续变化")
    return {
        "mode": "next_session",
        "symbol": opportunity.get("symbol"),
        "checklist": checklist,
        "trigger_levels": _opportunity_plan_levels(opportunity),
        "gex_changes": gex_changes,
        "disclaimer": "仅用于研究复盘，不代表实时可执行机会。",
    }


def _opportunity_weekend_plan(opportunity: dict[str, Any], snapshot: dict[str, Any], gex_changes: list[dict[str, Any]]) -> dict[str, Any]:
    strategy = str(opportunity.get("strategy_structure") or "unknown")
    checklist = [
        "周一开盘后等待前 10 分钟数据稳定再复核",
        "核对期权链 bid/ask、OI、volume 和 IV 是否仍可接受",
        "若开盘跳空穿越止损/止盈参考位，先重新扫描而不是沿用旧方案",
    ]
    if strategy in {"debit_call_spread", "single_leg_call"}:
        checklist.append("观察上方 call wall 是否限制原止盈空间")
    elif strategy in {"debit_put_spread", "single_leg_put"}:
        checklist.append("观察下方 put wall 是否限制原下行空间")
    elif strategy in {"iron_condor", "credit_call_spread", "credit_put_spread"}:
        checklist.append("确认负 gamma 环境是否抬高短波动结构风险")
    if gex_changes:
        checklist.append("把 GEX 变化作为重新定价和缩短持有时间的触发条件")
    return {
        "mode": "weekend",
        "symbol": opportunity.get("symbol"),
        "strategy_structure": strategy,
        "checklist": checklist,
        "trigger_levels": _opportunity_plan_levels(opportunity),
        "key_levels": _opportunity_weekend_key_levels(opportunity, snapshot),
        "suggested_triggers": _opportunity_weekend_trigger_suggestions(opportunity),
        "priority": _opportunity_weekend_priority(opportunity, snapshot, gex_changes),
        "gex_initial": opportunity.get("gex_snapshot") or {},
        "gex_current": _opportunity_gex_snapshot(snapshot),
        "gex_changes": gex_changes,
        "next_action": "next_open_rescan",
        "disclaimer": "周末计划使用历史/收盘参考，不代表实时可执行机会。",
    }


def _opportunity_weekend_key_levels(opportunity: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    risk = opportunity.get("risk_plan") or {}
    entry = opportunity.get("entry_reference") or {}
    levels = [
        {"label": "参考入场", "value": entry.get("underlying_reference"), "kind": "entry"},
        {"label": "参考止损", "value": (risk.get("stop_loss") or {}).get("underlying_reference"), "kind": "stop_loss"},
    ]
    for level in (risk.get("take_profit") or {}).get("levels") or []:
        levels.append({"label": level.get("label") or "止盈", "value": level.get("underlying_reference"), "kind": "take_profit"})
    gex = _opportunity_gex_snapshot(snapshot)
    if gex.get("gamma_flip"):
        levels.append({"label": "Gamma Flip", "value": gex.get("gamma_flip"), "kind": "gex"})
    if gex.get("call_wall"):
        levels.append({"label": "Call Wall", "value": gex.get("call_wall"), "kind": "gex"})
    if gex.get("put_wall"):
        levels.append({"label": "Put Wall", "value": gex.get("put_wall"), "kind": "gex"})
    return [item for item in levels if _numeric(item.get("value")) is not None]


def _opportunity_weekend_trigger_suggestions(opportunity: dict[str, Any]) -> list[dict[str, Any]]:
    symbol = opportunity.get("symbol")
    levels = _opportunity_plan_levels(opportunity)
    suggestions: list[dict[str, Any]] = []
    entry = _numeric(levels.get("entry_reference"))
    stop = _numeric(levels.get("stop_loss"))
    if entry is not None:
        suggestions.append({"type": "underlying_price", "symbol": symbol, "operator": ">=", "value": round(entry, 2), "label": "开盘重新站回参考价"})
    if stop is not None:
        suggestions.append({"type": "underlying_price", "symbol": symbol, "operator": "<=", "value": round(stop, 2), "label": "跌破参考止损"})
    suggestions.append({"type": "technical_indicator", "symbol": symbol, "field": "underlying_vs_vwap_pct", "operator": ">=", "value": 0, "label": "重新站上 VWAP"})
    suggestions.append({"type": "technical_indicator", "symbol": symbol, "field": "rvol", "operator": ">=", "value": 1.3, "label": "周一放量确认"})
    return suggestions


def _opportunity_weekend_priority(opportunity: dict[str, Any], snapshot: dict[str, Any], gex_changes: list[dict[str, Any]]) -> str:
    current = _numeric(snapshot.get("last")) or _numeric((opportunity.get("trigger_snapshot") or {}).get("last"))
    stop = _numeric(((opportunity.get("risk_plan") or {}).get("stop_loss") or {}).get("underlying_reference"))
    if stop is not None and current is not None:
        direction = opportunity.get("direction")
        if direction == "bullish" and current <= stop:
            return "invalidated_risk"
        if direction == "bearish" and current >= stop:
            return "invalidated_risk"
    if gex_changes:
        return "recheck_first"
    return "continue_watch"


def _opportunity_plan_levels(opportunity: dict[str, Any]) -> dict[str, Any]:
    risk = opportunity.get("risk_plan") or {}
    return {
        "entry_reference": (opportunity.get("entry_reference") or {}).get("underlying_reference"),
        "take_profit": (risk.get("take_profit") or {}).get("levels") or [],
        "stop_loss": (risk.get("stop_loss") or {}).get("underlying_reference"),
        "latest_exit": risk.get("latest_exit"),
    }


def fmt_float(value: Any) -> str:
    number = _numeric(value)
    return f"{number:.2f}" if number is not None else "--"


def _opportunity_followup_dedupe_key(opportunity: dict[str, Any], snapshot: dict[str, Any], market_state: str) -> str:
    gex_changes = _opportunity_gex_changes(opportunity, snapshot)
    gex_sig = "|".join(f"{item.get('type')}={item.get('from')}->{item.get('to')}" for item in gex_changes) or "no-gex-change"
    timestamp = str(snapshot.get("data_timestamp") or snapshot.get("market_clock", {}).get("date_et") or utc_now())[:10]
    return f"opp:{opportunity.get('id')}:{market_state}:{timestamp}:{gex_sig}"


def _fetch_current_gex_snapshot(
    symbol: str,
    market_data_source: str,
    spot: float | None = None,
    owner_id: str | None = None,
    max_days: int = 120,
) -> dict[str, Any]:
    market_data_source = str(market_data_source or "thetadata").strip().lower()
    max_days = max(int(max_days or 120), 1)
    try:
        if market_data_source == "longbridge":
            from .account_store import preferred_sdk_account
            from .longbridge_client import quote as lb_quote
            from .longbridge_option_tool import collect_candidates as lb_collect_candidates

            account = preferred_sdk_account(owner_id=owner_id)
            account_name = account.name if account and account.sdk_credentials_configured else None
            if account_name is None:
                return {"available": False, "regime": "unknown", "source": "longbridge", "error": "no_longbridge_account"}
            lb_symbol = symbol if symbol.endswith(".US") else f"{symbol}.US"
            current_spot = float(spot or 0)
            if current_spot <= 0:
                quote_row = lb_quote(lb_symbol, account_name)
                current_spot = float(quote_row.get("last") or quote_row.get("price") or 0)
            if current_spot <= 0:
                return {"available": False, "regime": "unknown", "source": "longbridge", "error": "no_spot"}
            candidates = lb_collect_candidates(
                symbol=symbol.replace(".US", ""),
                spot=current_spot,
                min_days=0,
                max_days=max_days,
                max_ask=9999,
                lottery=False,
                preferred_side=None,
                min_ask=0.0,
                account_name=account_name,
                gex_mode=True,
            )
            context = _build_gex_context_from_candidates(candidates, current_spot)
            context["source"] = "longbridge"
            return context
        if market_data_source == "thetadata":
            from .thetadata_option_tool import collect_candidates as theta_collect_candidates
            from .thetadata_option_tool import market_data as theta_market_data

            current_spot = float(spot or 0)
            if current_spot <= 0:
                try:
                    data = theta_market_data(symbol)
                    current_spot = float(data.get("quote", {}).get("last") or 0)
                except Exception:  # noqa: BLE001 - fall through to the free stock-data fallback.
                    current_spot = 0.0
                if current_spot <= 0:
                    from .yfinance_option_tool import market_data as yf_market_data

                    data = yf_market_data(symbol)
                    current_spot = float(data.get("quote", {}).get("last") or 0)
            if current_spot <= 0:
                return {"available": False, "regime": "unknown", "source": "thetadata", "error": "no_spot"}
            candidates = theta_collect_candidates(
                symbol=symbol,
                spot=current_spot,
                min_days=0,
                max_days=max_days,
                max_ask=9999,
                lottery=False,
                preferred_side=None,
                min_ask=0.0,
                gex_mode=True,
            )
            context = _build_gex_context_from_candidates(candidates, current_spot, supplement_legacy_greeks=True)
            context["source"] = "thetadata"
            return context
        from .yfinance_option_tool import collect_candidates as yf_collect_candidates
        from .yfinance_option_tool import market_data as yf_market_data

        current_spot = float(spot or 0)
        if current_spot <= 0:
            data = yf_market_data(symbol)
            current_spot = float(data.get("quote", {}).get("last") or 0)
        if current_spot <= 0:
            return {"available": False, "regime": "unknown", "source": "yfinance", "error": "no_spot"}
        candidates = yf_collect_candidates(
            symbol=symbol,
            spot=current_spot,
            min_days=0,
            max_days=max_days,
            max_ask=9999,
            lottery=False,
            preferred_side=None,
            min_ask=0.0,
            gex_mode=True,
        )
        context = _build_gex_context_from_candidates(candidates, current_spot)
        context["source"] = "yfinance"
        return context
    except Exception as exc:  # noqa: BLE001 - follow-up monitoring must degrade gracefully.
        return {"available": False, "regime": "unknown", "source": market_data_source, "error": str(exc)}


def _build_gex_context_from_candidates(candidates: list[Any], spot: float, supplement_legacy_greeks: bool = False) -> dict[str, Any]:
    from .intraday_option_tools import build_gex_context, enrich_option_greeks, supplement_option_greek_inputs_from_yfinance

    rows = list(candidates or [])
    if supplement_legacy_greeks and rows and spot > 0:
        rows = supplement_option_greek_inputs_from_yfinance(rows, spot)
    enriched = enrich_option_greeks(rows, spot) if rows and spot > 0 else []
    context = build_gex_context(enriched, spot)
    context["candidate_count"] = len(rows)
    context["greeks_enriched_count"] = len(enriched)
    return context


def evaluate_rule_group(rule_group: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    rule_group = rule_group or {"logic": "and", "conditions": []}
    conditions = [condition for condition in rule_group.get("conditions") or [] if isinstance(condition, dict)]
    logic = str(rule_group.get("logic") or "and").lower()
    checks = [_evaluate_condition(condition, snapshot) for condition in conditions]
    if not checks:
        matched = True
    elif logic == "or":
        matched = any(item["matched"] for item in checks)
    else:
        matched = all(item["matched"] for item in checks)
    return {"matched": matched, "logic": logic, "checks": checks, "snapshot": snapshot}


def _evaluate_condition(condition: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    field = str(condition.get("field") or "")
    operator = str(condition.get("operator") or "==")
    expected = condition.get("value")
    actual = _deep_get(snapshot, field)
    if actual is None:
        return {"field": field, "operator": operator, "expected": expected, "actual": None, "matched": False, "reason": "unknown"}
    try:
        if operator == "in":
            matched = actual in (expected or [])
        elif operator == "==":
            matched = actual == expected
        elif operator == "!=":
            matched = actual != expected
        else:
            matched = _compare(float(actual), operator, float(expected))
    except Exception:
        matched = False
    return {"field": field, "operator": operator, "expected": expected, "actual": actual, "matched": bool(matched)}


def _snapshot_data_available(snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return False
    freshness = str(snapshot.get("freshness_status") or snapshot.get("data_status") or "").strip().lower()
    if freshness in {"data_unavailable", "unavailable", "failed", "error"}:
        return False
    if snapshot.get("error"):
        return False
    return _numeric(snapshot.get("last")) is not None


def _snapshot_data_quality(
    snapshot: dict[str, Any] | None,
    *,
    symbol: str = "",
    market_data_source: str = "",
    market_state: str = "",
    review_only: bool = False,
    uses_gex: bool = False,
    trigger_type: str = "",
) -> dict[str, Any]:
    snapshot = snapshot or {}
    freshness = str(snapshot.get("freshness_status") or snapshot.get("data_status") or "unknown").strip().lower() or "unknown"
    source = str(snapshot.get("source") or snapshot.get("pricing_source") or market_data_source or "unknown").strip() or "unknown"
    error = str(snapshot.get("error") or snapshot.get("quote_warning") or "").strip()
    last_available = _numeric(snapshot.get("last") or snapshot.get("price") or snapshot.get("mid") or snapshot.get("ask") or snapshot.get("bid")) is not None
    option_quote = trigger_type == "option_quote" or snapshot.get("contract_symbol")
    gex = _opportunity_gex_snapshot(snapshot)
    gex_available = bool(gex.get("available") or gex.get("regime") not in {None, "", "unknown"} or gex.get("nearest_wall"))
    status = freshness
    label = "数据状态未知"
    severity = "warning"
    if freshness in {"data_unavailable", "unavailable", "failed", "error"} or error or not last_available:
        status = "data_unavailable"
        label = "数据不可用"
        severity = "error"
    elif freshness in {"fresh", "ok"} and last_available and not error:
        status = "fresh"
        label = "数据新鲜"
        severity = "ok"
    elif freshness in {"stale", "delayed"} or review_only or market_state in {"closed_today", "weekend", "holiday"}:
        status = "stale"
        label = "延迟/复盘数据"
        severity = "warning"
    explanation = error
    if not explanation:
        if status == "fresh":
            explanation = "行情可用于本轮规则判断。"
        elif status == "stale":
            explanation = "当前处于非实时场景或数据可能延迟，只用于复盘/计划，不作为实时机会提醒。"
        elif option_quote:
            explanation = "期权报价缺失或合约码不可用，无法判断 ask、bid/ask spread、volume、OI。"
        elif not last_available:
            explanation = "缺少 last/price 字段，无法判断价格与技术指标。"
        else:
            explanation = "行情源未返回可用数据。"
    if uses_gex and not gex_available:
        explanation = f"{explanation} GEX 数据不可用，相关规则按 unknown 处理。"
    return {
        "symbol": symbol or snapshot.get("symbol"),
        "status": status,
        "label": label,
        "severity": severity,
        "source": source,
        "data_timestamp": snapshot.get("data_timestamp"),
        "last_available": last_available,
        "option_quote_available": bool(snapshot.get("available")) if option_quote else None,
        "gex_available": gex_available,
        "market_state": market_state,
        "review_only": review_only,
        "explanation": explanation,
    }


def _insert_loop_run(
    run_id: str,
    owner: str,
    instance: dict[str, Any],
    status: str,
    started_at: str | None,
    finished_at: str | None,
    scanned_count: int,
    matched_count: int,
    alerted_count: int,
    market_state: str,
    summary: dict[str, Any],
) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO scan_loop_runs
                (id, owner_id, instance_id, watchlist_id, status, started_at, finished_at, scanned_count,
                 matched_count, alerted_count, market_state, data_freshness_json, summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                owner,
                instance["id"],
                instance.get("watchlist_id"),
                status,
                started_at,
                finished_at,
                scanned_count,
                matched_count,
                alerted_count,
                market_state,
                json.dumps({"freshness_status": "fresh" if market_state == "regular_open" else "stale"}, ensure_ascii=False),
                json.dumps(summary, ensure_ascii=False),
                utc_now(),
            ),
        )


def _insert_loop_item(
    run_id: str,
    owner: str,
    instance: dict[str, Any],
    symbol: str,
    status: str,
    prefilter: dict[str, Any],
    snapshot: dict[str, Any],
    scan_id: str | None,
    triggered: bool,
    recommendation: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO scan_loop_run_items
                (id, run_id, owner_id, instance_id, watchlist_id, symbol, status, prefilter_status, prefilter_result_json,
                 data_timestamp, data_freshness, scan_id, triggered, trigger_reasons_json, score, recommendation_json, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                run_id,
                owner,
                instance["id"],
                instance.get("watchlist_id"),
                symbol,
                status,
                "matched" if prefilter.get("matched") else "filtered",
                json.dumps(prefilter, ensure_ascii=False),
                snapshot.get("data_timestamp"),
                snapshot.get("freshness_status") or "unknown",
                scan_id,
                int(triggered),
                json.dumps([check for check in prefilter.get("checks", []) if check.get("matched")], ensure_ascii=False),
                snapshot.get("score"),
                json.dumps(recommendation or {}, ensure_ascii=False),
                error,
                utc_now(),
            ),
        )


def _update_trigger_check(owner_id: str, trigger_id: str, *, triggered: bool = False, status: str = "active", next_check_at: str | None = None) -> None:
    now = utc_now()
    trigger = get_scan_trigger(owner_id, trigger_id)
    next_at = next_check_at or _utc_after_seconds(int((trigger or {}).get("check_interval_seconds") or 300))
    with connect() as db:
        if triggered:
            db.execute(
                """
                UPDATE scan_triggers
                SET last_checked_at = ?, last_triggered_at = ?, next_check_at = ?, trigger_count = trigger_count + 1, status = ?, updated_at = ?
                WHERE owner_id = ? AND id = ?
                """,
                (now, now, next_at, status, now, normalize_owner_id(owner_id), trigger_id),
            )
        else:
            db.execute(
                "UPDATE scan_triggers SET last_checked_at = ?, next_check_at = ?, status = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
                (now, next_at, status, now, normalize_owner_id(owner_id), trigger_id),
            )


def _set_trigger_condition_metadata(owner_id: str, trigger_id: str, updates: dict[str, Any], *, remove_keys: list[str] | None = None) -> None:
    trigger = get_scan_trigger(owner_id, trigger_id)
    if not trigger:
        return
    condition = dict(trigger.get("condition") or {})
    for key in remove_keys or []:
        condition.pop(key, None)
    for key, value in updates.items():
        if value is None:
            condition.pop(key, None)
        else:
            condition[key] = value
    with connect() as db:
        db.execute(
            "UPDATE scan_triggers SET condition_json = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
            (json.dumps(condition, ensure_ascii=False), utc_now(), normalize_owner_id(owner_id), trigger_id),
        )


def _mark_event_status(owner_id: str, event_id: str, status: str, error: str | None) -> None:
    with connect() as db:
        db.execute(
            """
            UPDATE notification_events
            SET status = ?, attempts = attempts + 1, last_error = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END
            WHERE owner_id = ? AND id = ?
            """,
            (status, error, status, utc_now(), normalize_owner_id(owner_id), event_id),
        )


def _mark_channel_verified(owner_id: str, channel_id: str) -> None:
    now = utc_now()
    with connect() as db:
        db.execute(
            "UPDATE notification_channels SET verified_at = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
            (now, now, normalize_owner_id(owner_id), channel_id),
        )


def _mark_channel_delivery(owner_id: str, channel_id: str, error: str | None, *, tested: bool) -> None:
    now = utc_now()
    with connect() as db:
        if tested:
            db.execute(
                "UPDATE notification_channels SET last_error = ?, last_test_at = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
                (error, now, now, normalize_owner_id(owner_id), channel_id),
            )
        else:
            db.execute(
                "UPDATE notification_channels SET last_error = ?, updated_at = ? WHERE owner_id = ? AND id = ?",
                (error, now, normalize_owner_id(owner_id), channel_id),
            )


def _default_email_channel(owner_id: str) -> dict[str, Any] | None:
    channels = [channel for channel in list_notification_channels(owner_id) if channel["type"] == "email" and channel["enabled"]]
    return channels[0] if channels else None


def _send_channel(channel: dict[str, Any], event: dict[str, Any]) -> None:
    if channel.get("type") == "webhook":
        _send_webhook(channel, event)
        return
    _send_email(channel, event)


def _send_email(channel: dict[str, Any], event: dict[str, Any]) -> None:
    host = os.getenv("AI_OPTION_SMTP_HOST")
    if not host:
        raise RuntimeError("AI_OPTION_SMTP_HOST is not configured")
    port = int(os.getenv("AI_OPTION_SMTP_PORT") or 587)
    sender = os.getenv("AI_OPTION_SMTP_FROM") or os.getenv("AI_OPTION_SMTP_USER") or "noreply@ai-option.local"
    username = os.getenv("AI_OPTION_SMTP_USER")
    password = os.getenv("AI_OPTION_SMTP_PASSWORD")
    recipient = channel.get("config", {}).get("email")
    if not recipient:
        raise RuntimeError("channel email is missing")
    message = EmailMessage()
    message["Subject"] = event["title"]
    message["From"] = sender
    message["To"] = recipient
    message.set_content(event["body"])
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if os.getenv("AI_OPTION_SMTP_TLS", "1").strip().lower() not in {"0", "false", "no"}:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)


def _send_webhook(channel: dict[str, Any], event: dict[str, Any]) -> None:
    config = channel.get("config") or {}
    request_payload = build_notification_payload_preview(channel, event)
    url = str(config.get("url") or "").strip()
    if config.get("provider") == "telegram" and config.get("bot_token") and config.get("chat_id"):
        url = f"https://api.telegram.org/bot{str(config.get('bot_token') or '').strip()}/sendMessage"
    if config.get("provider") == "whatsapp" and config.get("phone_number_id") and config.get("access_token") and config.get("to"):
        url = f"https://graph.facebook.com/v20.0/{str(config.get('phone_number_id') or '').strip()}/messages"
    if not url:
        raise RuntimeError("webhook url is missing")
    body = json.dumps(request_payload.get("body") or {}, ensure_ascii=False).encode("utf-8")
    headers = {**(request_payload.get("headers") or {})}
    if config.get("provider") == "whatsapp" and config.get("access_token"):
        headers["Authorization"] = f"Bearer {str(config.get('access_token') or '').strip()}"
    secret = str(config.get("secret") or "").strip()
    if secret and config.get("provider") != "feishu":
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers[str(config.get("header_name") or "X-AI-Option-Signature")] = f"sha256={signature}"
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=15) as response:  # noqa: S310 - user configured webhook endpoint.
            if int(response.status) >= 400:
                response_body = response.read(2048).decode("utf-8", errors="replace")
                raise RuntimeError(_platform_webhook_error(config.get("provider"), response.status, response_body))
    except urlerror.HTTPError as exc:
        response_body = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(_platform_webhook_error(config.get("provider"), exc.code, response_body)) from exc


def _platform_webhook_error(provider: Any, status: int | str, response_body: str = "") -> str:
    normalized = _normalize_webhook_provider(provider)
    prefix = {
        "feishu": "Feishu webhook",
        "whatsapp": "WhatsApp Cloud API",
        "telegram": "Telegram Bot API",
        "slack": "Slack webhook",
        "discord": "Discord webhook",
    }.get(normalized, "Webhook")
    detail = str(response_body or "").strip()
    if detail:
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, dict):
                if isinstance(parsed.get("error"), dict):
                    error = parsed["error"]
                    message = error.get("message") or error.get("error_user_msg") or error.get("error_user_title")
                    code = error.get("code") or error.get("type")
                    if message:
                        detail = f"{message}{f' ({code})' if code else ''}"
                elif parsed.get("msg") or parsed.get("message"):
                    detail = str(parsed.get("msg") or parsed.get("message"))
        except Exception:
            detail = detail[:500]
    return f"{prefix} returned HTTP {status}{f': {detail}' if detail else ''}"


def build_notification_payload_preview(channel: dict[str, Any], event: dict[str, Any] | None = None) -> dict[str, Any]:
    event = event or {
        "id": "preview",
        "title": "AI Option 观察提醒测试",
        "body": "SPY 命中观察条件。此提醒仅用于研究辅助，不构成投资建议。",
        "source_type": "preview",
        "source_id": "preview",
        "dedupe_key": "preview",
        "created_at": utc_now(),
        "payload": {"symbol": "SPY", "score": 78, "kind": "preview"},
    }
    if channel.get("type") == "email":
        return {
            "transport": "email",
            "to": (channel.get("config") or {}).get("email"),
            "subject": event.get("title"),
            "body": event.get("body"),
        }
    config = channel.get("config") or {}
    provider = _normalize_webhook_provider(config.get("provider"))
    base_payload = {
        "id": event.get("id"),
        "title": event.get("title"),
        "body": event.get("body"),
        "source_type": event.get("source_type"),
        "source_id": event.get("source_id"),
        "dedupe_key": event.get("dedupe_key"),
        "created_at": event.get("created_at"),
        "payload": event.get("payload") or {},
    }
    body: dict[str, Any]
    if provider == "slack":
        body = {"text": f"*{event.get('title')}*\n{event.get('body')}"}
    elif provider == "discord":
        body = _discord_notification_payload(event)
    elif provider == "feishu":
        body = {"msg_type": "text", "content": {"text": f"{event.get('title')}\n{event.get('body')}"}}
    elif provider == "telegram":
        body = {
            "chat_id": str(config.get("chat_id") or "").strip(),
            "text": f"{event.get('title')}\n\n{event.get('body')}",
            "disable_web_page_preview": True,
        }
    elif provider == "whatsapp":
        template_name = str(config.get("template_name") or "").strip()
        if template_name:
            variables = [event.get("title"), event.get("body")] + list(config.get("template_variables") or [])
            body = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": str(config.get("to") or "").strip(),
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": str(config.get("template_language") or "en_US").strip()},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": str(value)}
                                for value in variables
                                if str(value).strip()
                            ],
                        }
                    ],
                },
            }
        else:
            body = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": str(config.get("to") or "").strip(),
                "type": "text",
                "text": {
                    "preview_url": False,
                    "body": f"{event.get('title')}\n\n{event.get('body')}",
                },
            }
    else:
        body = base_payload
    headers = {"Content-Type": "application/json", "User-Agent": "AIOptionScanner/1.0"}
    safe_url = str(config.get("url") or "").strip()
    if provider == "feishu" and config.get("secret"):
        timestamp = str(int(time.time()))
        sign = _feishu_sign(str(config.get("secret") or ""), timestamp)
        body["timestamp"] = timestamp
        body["sign"] = sign
    if provider == "telegram" and config.get("bot_token"):
        safe_url = "https://api.telegram.org/bot***/sendMessage"
    if provider == "whatsapp" and config.get("phone_number_id"):
        safe_url = f"https://graph.facebook.com/v20.0/{str(config.get('phone_number_id') or '').strip()}/messages"
        headers["Authorization"] = "Bearer ***"
    return {
        "transport": "webhook",
        "provider": provider,
        "url": safe_url,
        "headers": headers,
        "body": body,
        "signature_header": None if provider == "feishu" else str(config.get("header_name") or "X-AI-Option-Signature") if config.get("secret") else None,
    }


def _discord_notification_payload(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("source_type") != "scan_loop_report":
        return {"content": f"**{event.get('title')}**\n{event.get('body')}"}
    payload = event.get("payload") or {}
    scanned = int(payload.get("scanned_count") or 0)
    matched = int(payload.get("matched_count") or 0)
    triggered = int(payload.get("triggered_count") or 0)
    unavailable = int(payload.get("data_unavailable_count") or 0)
    embeds: list[dict[str, Any]] = []
    for item in (payload.get("items") or [])[:8]:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "--")
        decision = str(item.get("decision") or "--")
        color = 0x2ECC71 if item.get("alert_matched") else 0xF1C40F if item.get("prefilter_matched") else 0x95A5A6
        embeds.append(
            {
                "title": f"{symbol} · {decision}",
                "description": str(item.get("observation") or "--")[:350],
                "color": color,
                "fields": [
                    {"name": "结论", "value": _discord_field_value(item.get("conclusion")), "inline": False},
                    {"name": "决策", "value": _discord_field_value(item.get("decision")), "inline": False},
                    {"name": "情景基准", "value": _discord_field_value(_scan_loop_report_scenario_summary(item)), "inline": False},
                    {"name": "风控", "value": _discord_field_value(_scan_loop_report_risk_summary(item)), "inline": False},
                ],
            }
        )
    return {
        "content": f"**{event.get('title')}**\n扫描 `{scanned}` · 预筛 `{matched}` · 触发 `{triggered}` · 缺数据 `{unavailable}`",
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }


def _discord_field_value(value: Any) -> str:
    text = str(value or "--").strip() or "--"
    return text[:1024]


def _scan_loop_report_risk_summary(item: dict[str, Any]) -> str:
    entry = (item.get("entry_reference") or {}).get("underlying_reference")
    take_profit = item.get("take_profit") or []
    tp_text = "/".join(fmt_float(level.get("underlying_reference")) for level in take_profit[:2] if isinstance(level, dict)) or "--"
    stop_text = fmt_float(item.get("stop_loss")) if item.get("stop_loss") is not None else "--"
    entry_text = fmt_float(entry) if entry is not None else "--"
    demo = item.get("demo_tracking") or {}
    demo_text = f" · Demo {demo.get('demo_order_id')} {demo.get('status')}" if demo.get("enabled") else ""
    return f"入场 {entry_text} · 止盈 {tp_text} · 止损 {stop_text}{demo_text}"


def _scan_loop_report_scenario_summary(item: dict[str, Any]) -> str:
    scenario = item.get("scenario_analysis") or {}
    rows = [row for row in (scenario.get("scenarios") or []) if isinstance(row, dict)]
    if not rows:
        return scenario.get("summary") or "--"
    return "\n".join(f"{row.get('label')}~{row.get('probability_pct')}%：{row.get('body')}" for row in rows[:2])


def _normalize_webhook_provider(provider: Any) -> str:
    value = str(provider or "generic").strip().lower()
    aliases = {"lark": "feishu", "飞书": "feishu", "telegram_bot": "telegram", "tg": "telegram", "wa": "whatsapp", "whatsapp_business": "whatsapp"}
    value = aliases.get(value, value)
    return value if value in {"generic", "slack", "discord", "telegram", "whatsapp", "feishu"} else "generic"


def _feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _webhook_provider_label(provider: str) -> str:
    return {
        "slack": "Slack Webhook",
        "discord": "Discord Webhook",
        "telegram": "Telegram Bot",
        "whatsapp": "WhatsApp Business",
        "feishu": "飞书 Webhook",
    }.get(provider, "Webhook")


def _record_delivery_log(
    owner_id: str,
    event_id: str,
    channel: dict[str, Any],
    status: str,
    attempt: int,
    request_preview: dict[str, Any],
    *,
    response_summary: str | None = None,
    error: str | None = None,
) -> None:
    init_observation_db()
    config = channel.get("config") or {}
    provider = _normalize_webhook_provider(config.get("provider")) if channel.get("type") == "webhook" else "email"
    with connect() as db:
        db.execute(
            """
            INSERT INTO notification_delivery_logs
                (id, owner_id, event_id, channel_id, channel_type, provider, status, attempt, request_preview_json, response_summary, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                normalize_owner_id(owner_id),
                event_id,
                channel.get("id"),
                channel.get("type") or "unknown",
                provider,
                status,
                int(attempt or 1),
                json.dumps(request_preview or {}, ensure_ascii=False),
                response_summary,
                error,
                utc_now(),
            ),
        )


def _fetch_last_price(symbol: str) -> float:
    snapshot = _fetch_quote_snapshot(symbol)
    value = snapshot.get("last")
    if value is None:
        raise RuntimeError(f"no quote available for {symbol}")
    return float(value)


def build_scan_trigger_quote_snapshot(trigger: dict[str, Any]) -> dict[str, Any]:
    condition = trigger.get("condition") or {}
    trigger_type = str(condition.get("type") or "underlying_price")
    symbol = str(condition.get("symbol") or trigger.get("symbol") or "").strip().upper()
    if trigger_type == "option_quote":
        return _fetch_option_quote_trigger_snapshot(condition)
    return _fetch_technical_indicator_snapshot(symbol, condition)


def _fetch_quote_snapshot(symbol: str) -> dict[str, Any]:
    try:
        technical = _fetch_technical_indicator_snapshot(symbol, {"market_data_source": "yfinance"})
        if _snapshot_data_available(technical):
            return technical
    except Exception:
        pass
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        history = ticker.history(period="1d", interval="5m")
        if history is None or history.empty:
            return {"symbol": symbol, "freshness_status": "data_unavailable", "data_timestamp": utc_now(), "error": "empty quote history"}
        row = history.iloc[-1]
        volume = float(row.get("Volume") or 0)
        return {
            "symbol": symbol,
            "last": float(row.get("Close")),
            "open": float(row.get("Open")),
            "high": float(row.get("High")),
            "low": float(row.get("Low")),
            "volume": volume,
            "rvol": 1.0 if volume else 0,
            "freshness_status": "fresh",
            "data_timestamp": utc_now(),
        }
    except Exception as exc:  # noqa: BLE001 - quote fallback is represented as data status.
        return {"symbol": symbol, "freshness_status": "data_unavailable", "data_timestamp": utc_now(), "error": str(exc)}


def _fetch_technical_indicator_snapshot(symbol: str, condition: dict[str, Any] | None = None) -> dict[str, Any]:
    condition = condition or {}
    source = str(condition.get("market_data_source") or "yfinance").strip().lower()
    account_name = str(condition.get("longbridge_account") or "").strip() or None
    try:
        data = _fetch_trigger_market_data(symbol, source, account_name)
        quote = data.get("quote") or {}
        daily = data.get("daily") or []
        intraday = data.get("intraday") or []
        prices = [_numeric(_intraday_price(item)) for item in intraday]
        prices = [value for value in prices if value is not None and value > 0]
        volumes = [_numeric(item.get("volume")) or 0.0 for item in intraday]
        last = _numeric(quote.get("last") or quote.get("price")) or (prices[-1] if prices else None)
        if last is None:
            return {"symbol": symbol, "freshness_status": "data_unavailable", "data_timestamp": utc_now(), "source": source, "error": "last price unavailable"}
        vwap = _last_intraday_vwap(intraday) or _computed_vwap(prices, volumes)
        orb_high, orb_low = _opening_range_levels(intraday, minutes=15)
        rvol = _relative_intraday_volume(daily, volumes)
        from .market_structure import build_volume_profile, realized_volatility_context

        rv = realized_volatility_context(daily)
        volume_profile = build_volume_profile(intraday, daily, last)
        snapshot = {
            "symbol": symbol,
            "last": round(last, 4),
            "price": round(last, 4),
            "vwap": round(vwap, 4) if vwap is not None else None,
            "underlying_vs_vwap_pct": round((last / vwap - 1) * 100, 4) if vwap and vwap > 0 else None,
            "orb_high": round(orb_high, 4) if orb_high is not None else None,
            "orb_low": round(orb_low, 4) if orb_low is not None else None,
            "ema_20": _round_optional(_ema(prices, 20)),
            "ema_50": _round_optional(_ema(prices, 50)),
            "ema_200": _round_optional(_ema(prices, 200)),
            "rsi": _round_optional(_rsi(prices)),
            "atr": _round_optional(_atr(daily)),
            "rvol": round(rvol, 4) if rvol is not None else None,
            "rv20": _round_optional(rv.get("rv20"), 6),
            "rv60": _round_optional(rv.get("rv60"), 6),
            "rv_rank": _round_optional(rv.get("rv_rank"), 4),
            "volume_profile": volume_profile,
            "volume_profile_poc": volume_profile.get("poc"),
            "volume_profile_value_area_low": volume_profile.get("value_area_low"),
            "volume_profile_value_area_high": volume_profile.get("value_area_high"),
            "volume_profile_position": volume_profile.get("position"),
            "volume_profile_low_volume_room_up_pct": volume_profile.get("low_volume_room_up_pct"),
            "volume_profile_low_volume_room_down_pct": volume_profile.get("low_volume_room_down_pct"),
            "freshness_status": "fresh",
            "data_timestamp": utc_now(),
            "source": source,
        }
        return snapshot
    except Exception as exc:  # noqa: BLE001 - trigger checks should degrade into unavailable data.
        return {"symbol": symbol, "freshness_status": "data_unavailable", "data_timestamp": utc_now(), "source": source, "error": str(exc)}


def _fetch_option_quote_trigger_snapshot(condition: dict[str, Any]) -> dict[str, Any]:
    contract_symbol = str(condition.get("contract_symbol") or "").strip().upper()
    symbol = str(condition.get("symbol") or "").strip().upper()
    source = str(condition.get("option_data_source") or condition.get("market_data_source") or "thetadata").strip().lower()
    account_name = str(condition.get("longbridge_account") or "").strip() or None
    if not contract_symbol:
        return {
            "symbol": symbol,
            "freshness_status": "data_unavailable",
            "data_timestamp": utc_now(),
            "source": source,
            "error": "contract_symbol is required for option quote triggers",
        }
    try:
        if source == "longbridge":
            from .longbridge_option_tool import quote_option_contract

            quote = quote_option_contract(contract_symbol, account_name)
        elif source == "thetadata":
            from .thetadata_option_tool import quote_option_contract

            quote = quote_option_contract(contract_symbol)
        else:
            from .yfinance_option_tool import quote_option_contract

            quote = quote_option_contract(contract_symbol)
        bid = _numeric(quote.get("bid"))
        ask = _numeric(quote.get("ask"))
        mid = _numeric(quote.get("mid"))
        if mid is None and bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2
        last = _numeric(quote.get("last") or quote.get("last_price") or quote.get("limit_price"))
        spread_pct = None
        if bid is not None and ask is not None and mid is not None and mid > 0:
            spread_pct = (ask - bid) / mid * 100
        return {
            "symbol": symbol,
            "contract_symbol": contract_symbol,
            "available": bool(quote.get("available")),
            "ask": ask,
            "bid": bid,
            "mid": _round_optional(mid, 4),
            "last": last,
            "bid_ask_spread_pct": _round_optional(spread_pct, 4),
            "volume": _numeric(quote.get("volume")),
            "open_interest": _numeric(quote.get("open_interest")),
            "delta": _numeric(quote.get("delta")),
            "gamma": _numeric(quote.get("gamma")),
            "theta": _numeric(quote.get("theta") or quote.get("theta_per_day")),
            "vega": _numeric(quote.get("vega")),
            "iv": _numeric(quote.get("iv") or quote.get("implied_volatility") or quote.get("implied_vol")),
            "implied_volatility": _numeric(quote.get("implied_volatility") or quote.get("iv") or quote.get("implied_vol")),
            "freshness_status": "fresh" if quote.get("available") else "data_unavailable",
            "data_timestamp": utc_now(),
            "source": source,
            "pricing_source": quote.get("pricing_source"),
            "quote_warning": quote.get("quote_warning") or quote.get("error") or "",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "symbol": symbol,
            "contract_symbol": contract_symbol,
            "freshness_status": "data_unavailable",
            "data_timestamp": utc_now(),
            "source": source,
            "error": str(exc),
        }


def _fetch_trigger_market_data(symbol: str, source: str, account_name: str | None = None) -> dict[str, Any]:
    if source == "longbridge":
        from .longbridge_option_tool import market_data as lb_market_data

        return lb_market_data(symbol, account_name=account_name)
    if source == "thetadata":
        from .thetadata_option_tool import market_data as theta_market_data

        try:
            data = theta_market_data(symbol)
            quote = data.get("quote") or {}
            if quote.get("last") or quote.get("price") or data.get("daily") or data.get("intraday"):
                return data
        except Exception:  # noqa: BLE001 - below uses the established yfinance fallback.
            pass
    from .yfinance_option_tool import market_data as yf_market_data

    data = yf_market_data(symbol)
    if source == "thetadata":
        data["market_data_fallback_from"] = "thetadata"
    return data


def _intraday_price(item: dict[str, Any]) -> Any:
    return item.get("price") or item.get("close") or item.get("last") or item.get("current_price")


def _last_intraday_vwap(intraday: list[dict[str, Any]]) -> float | None:
    for item in reversed(intraday):
        value = _numeric(item.get("avg_price") or item.get("vwap"))
        if value is not None and value > 0:
            return value
    return None


def _computed_vwap(prices: list[float], volumes: list[float]) -> float | None:
    if not prices:
        return None
    if len(volumes) == len(prices) and sum(volumes) > 0:
        return sum(price * volume for price, volume in zip(prices, volumes)) / sum(volumes)
    return sum(prices) / len(prices)


def _opening_range_levels(intraday: list[dict[str, Any]], *, minutes: int = 15) -> tuple[float | None, float | None]:
    bars = max(1, minutes // 5)
    rows = intraday[:bars]
    if not rows:
        return None, None
    highs = [_numeric(item.get("high") or _intraday_price(item)) for item in rows]
    lows = [_numeric(item.get("low") or _intraday_price(item)) for item in rows]
    highs = [value for value in highs if value is not None and value > 0]
    lows = [value for value in lows if value is not None and value > 0]
    return (max(highs) if highs else None, min(lows) if lows else None)


def _relative_intraday_volume(daily: list[dict[str, Any]], volumes: list[float]) -> float | None:
    current_volume = sum(volume for volume in volumes if volume > 0)
    daily_volumes = [_numeric(item.get("volume")) for item in daily[-30:] if _numeric(item.get("volume")) is not None and _numeric(item.get("volume")) > 0]
    if current_volume <= 0 or not daily_volumes:
        return None
    average_daily_volume = sum(daily_volumes) / len(daily_volumes)
    elapsed_fraction = min(max(len(volumes), 1) / 78, 1)
    expected_volume = average_daily_volume * elapsed_fraction
    return current_volume / expected_volume if expected_volume > 0 else None


def _ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    alpha = 2 / (period + 1)
    ema_value = values[0]
    for value in values[1:]:
        ema_value = value * alpha + ema_value * (1 - alpha)
    return ema_value


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
    recent = deltas[-period:]
    gains = [delta for delta in recent if delta > 0]
    losses = [-delta for delta in recent if delta < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(daily: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(daily) < 2:
        return None
    ranges: list[float] = []
    rows = daily[-(period + 1) :]
    for index in range(1, len(rows)):
        high = _numeric(rows[index].get("high"))
        low = _numeric(rows[index].get("low"))
        previous_close = _numeric(rows[index - 1].get("close"))
        if high is None or low is None:
            continue
        if previous_close is None:
            ranges.append(high - low)
        else:
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges) / len(ranges) if ranges else None


def _round_optional(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _submit_rescan_for_trigger(owner_id: str, trigger: dict[str, Any]) -> dict[str, Any]:
    condition = trigger.get("condition") or {}
    source_scan_id = str(trigger.get("scan_id") or condition.get("scan_id") or "").strip()
    source = get_scan_run(source_scan_id, owner_id=normalize_owner_id(owner_id)) if source_scan_id else None
    symbol = str(condition.get("symbol") or trigger.get("symbol") or "").strip().upper()
    query = str(condition.get("query") or (source or {}).get("query") or f"重新扫描 {symbol} 的期权机会，输出当前最值得观察的方案和决策评分。")
    strategy_modes = _rescan_strategy_modes(condition, source)
    return submit_scan(
        query=query,
        symbol=symbol,
        ai_provider=str(condition.get("ai_provider") or (source or {}).get("ai_provider") or "deepseek"),
        longbridge_account=str(condition.get("longbridge_account") or (source or {}).get("longbridge_account") or "yfinance"),
        use_ai=bool(condition.get("use_ai", (source or {}).get("use_ai", True))),
        council=bool(condition.get("council", (source or {}).get("council", True))),
        analysis_modules=condition.get("analysis_modules") or (source or {}).get("analysis_modules") or {},
        strategy_modes=strategy_modes,
        market_data_source=str(condition.get("market_data_source") or (source or {}).get("market_data_source") or "yfinance"),
        option_data_source=str(condition.get("option_data_source") or (source or {}).get("option_data_source") or "thetadata"),
        owner_id=normalize_owner_id(owner_id),
        ai_provider_owner=normalize_owner_id(owner_id),
        source_type="trigger_rescan",
        source_id=trigger.get("id") or source_scan_id or symbol,
    )


def _extract_rescan_score(scan: dict[str, Any], preferred_field: str = "decision_score") -> float | None:
    payload = scan.get("payload") or {}
    modes = normalize_strategy_modes(scan.get("strategy_modes") or (payload.get("intent") or {}).get("strategy_modes"))
    if modes and "single_leg" not in modes:
        scores: list[float] = []
        for container in (payload.get("primary_strategy"),):
            if isinstance(container, dict):
                scores.extend(_candidate_scores(container))
        for item in payload.get("strategy_candidates") or []:
            if isinstance(item, dict):
                scores.extend(_candidate_scores(item))
        decision_gate = payload.get("decision_gate") or {}
        if isinstance(decision_gate, dict):
            gate_score = _numeric(decision_gate.get("final_score"))
            if gate_score is None:
                gate_score = _numeric(decision_gate.get("score"))
            if gate_score is not None:
                scores.append(gate_score)
        valid_scores = [score for score in scores if score is not None]
        return max(valid_scores) if valid_scores else None
    direct = _numeric(_deep_get(payload, preferred_field))
    if direct is not None:
        return direct
    scores: list[float] = []
    for container in (payload.get("primary_candidate"), payload.get("ai_selected_candidate"), payload.get("primary_strategy")):
        if isinstance(container, dict):
            scores.extend(_candidate_scores(container))
    for key in ("option_candidates", "strategy_candidates"):
        for item in payload.get(key) or []:
            if isinstance(item, dict):
                scores.extend(_candidate_scores(item))
    decision_gate = payload.get("decision_gate") or {}
    if isinstance(decision_gate, dict):
        gate_score = _numeric(decision_gate.get("final_score"))
        if gate_score is None:
            gate_score = _numeric(decision_gate.get("score"))
        if gate_score is not None:
            scores.append(gate_score)
    valid_scores = [score for score in scores if score is not None]
    return max(valid_scores) if valid_scores else None


def _rescan_strategy_modes(condition: dict[str, Any], source: dict[str, Any] | None) -> list[str]:
    if condition.get("strategy_modes") is not None and condition.get("strategy_modes") != "":
        return normalize_strategy_modes(condition.get("strategy_modes"))
    if source and source.get("strategy_modes") is not None and source.get("strategy_modes") != "":
        return normalize_strategy_modes(source.get("strategy_modes"))
    return ["single_leg", "spread"]


def _candidate_scores(candidate: dict[str, Any]) -> list[float]:
    fields = ("decision_score", "analysis_score", "score", "alpha_score", "reward_risk_score")
    return [score for score in (_numeric(candidate.get(field)) for field in fields) if score is not None]


def _numeric(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _scan_reference(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": scan.get("id"),
        "locator_id": scan.get("locator_id"),
        "status": scan.get("status"),
        "stage": scan.get("stage"),
        "progress": scan.get("progress"),
        "error": scan.get("error"),
    }


def _market_state(clock: dict[str, Any]) -> str:
    state = str(clock.get("market_state") or "").strip().lower()
    if state in {"regular_open", "closed_today", "weekend", "holiday"}:
        return state
    session_state = str(clock.get("session_state") or "").strip().lower()
    if session_state == "regular_open":
        return "regular_open"
    if session_state in {"weekend", "holiday"}:
        return session_state
    if clock.get("is_market_open_regular"):
        return "regular_open"
    if not clock.get("is_trading_day"):
        reason = str(clock.get("trading_day_reason") or "").lower()
        return "weekend" if "weekend" in reason else "holiday"
    return "closed_today"


def _scan_loop_review_due(instance: dict[str, Any], market_state: str, clock: dict[str, Any]) -> bool:
    if market_state == "closed_today":
        if not instance.get("eod_review_enabled"):
            return False
        date_et = str(clock.get("date_et") or "")
        if not date_et or instance.get("last_eod_review_date") == date_et:
            return False
        return _clock_after_et_time(clock, str(instance.get("eod_run_time_et") or "16:20"))
    if market_state in {"weekend", "holiday"}:
        if not instance.get("weekend_review_enabled"):
            return False
        key = _weekend_review_key(clock)
        return bool(key and instance.get("last_weekend_review_key") != key)
    return False


def _clock_after_et_time(clock: dict[str, Any], hhmm: str) -> bool:
    now_value = str(clock.get("now_et") or "")
    try:
        now_time = datetime.fromisoformat(now_value).astimezone(ET).time()
    except ValueError:
        return True
    try:
        hour, minute = [int(part) for part in str(hhmm or "16:20").split(":", 1)]
    except (TypeError, ValueError):
        hour, minute = 16, 20
    target = now_time.replace(hour=max(0, min(hour, 23)), minute=max(0, min(minute, 59)), second=0, microsecond=0)
    return now_time >= target


def _weekend_review_key(clock: dict[str, Any]) -> str:
    date_value = str(clock.get("date_et") or "")
    try:
        current = datetime.fromisoformat(date_value).date()
    except ValueError:
        try:
            current = datetime.fromisoformat(str(clock.get("now_et") or "")).astimezone(ET).date()
        except ValueError:
            return ""
    year, week, _ = current.isocalendar()
    return f"{year}-W{week:02d}"


def _compare(left: float, operator: str, right: float) -> bool:
    if operator == ">=":
        return left >= right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == "<":
        return left < right
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    raise ValueError(f"unsupported operator: {operator}")


def _normalize_market_policy(value: Any) -> str:
    policy = str(value or "regular_only").strip().lower()
    if policy in {"regular", "regular_only"}:
        return "regular_only"
    if policy in {"always", "always_calendar"}:
        return "always_calendar"
    if policy in {"include_extended", "next_open", "eod_review"}:
        return policy
    return "regular_only"


def _trigger_market_policy_block(trigger: dict[str, Any], clock: dict[str, Any], *, current_value: float | None) -> dict[str, Any] | None:
    if current_value is not None:
        return None
    condition = trigger.get("condition") or {}
    if str(condition.get("market_session") or "").strip().lower() == "always":
        return None
    policy = _normalize_market_policy(trigger.get("market_policy") or condition.get("market_policy"))
    if policy == "always_calendar":
        return None
    if policy == "include_extended" and condition.get("type") != "option_quote":
        return None
    if clock.get("is_market_open_regular"):
        return None
    next_open = _next_regular_open_after(clock, int(trigger.get("opening_grace_minutes") or 10))
    reason = "market_not_regular_open"
    status = "waiting_market"
    if policy == "eod_review":
        reason = "market_closed_eod_review_only"
        status = "eod_waiting"
    if condition.get("type") == "option_quote":
        reason = "option_quote_stale_outside_regular"
        status = "waiting_market"
    return {
        "reason": reason,
        "status": status,
        "next_check_at": next_open,
        "market_policy": policy,
    }


def _next_regular_open_after(clock: dict[str, Any], opening_grace_minutes: int = 10) -> str:
    try:
        now_value = datetime.fromisoformat(str(clock.get("now_et") or "")).astimezone(ET)
    except ValueError:
        now_value = datetime.now(ET)
    next_open = next_regular_open_after(now_value, grace_minutes=max(0, opening_grace_minutes))
    return next_open.astimezone(timezone.utc).isoformat()


def _normalize_trigger_condition(condition: dict[str, Any], fallback_symbol: Any = None) -> dict[str, Any]:
    trigger_type = str(condition.get("type") or "underlying_price").strip().lower()
    if trigger_type not in {"underlying_price", "rescan_score", "technical_indicator", "option_quote"}:
        raise ValueError("unsupported trigger type")
    symbol = str(condition.get("symbol") or fallback_symbol or "").strip().upper()
    if not symbol:
        raise ValueError("condition.symbol is required")
    operator = str(condition.get("operator") or ">=").strip()
    if operator not in {">=", "<=", ">", "<", "==", "!="}:
        raise ValueError("unsupported trigger operator")
    try:
        value = float(condition.get("value"))
    except Exception as exc:
        raise ValueError("condition.value must be numeric") from exc
    market_session = str(condition.get("market_session") or "regular").strip().lower()
    if market_session not in {"regular", "always"}:
        market_session = "regular"
    normalized = {
        "type": trigger_type,
        "symbol": symbol,
        "operator": operator,
        "value": value,
        "market_session": market_session,
    }
    if trigger_type in {"technical_indicator", "option_quote"}:
        field = str(condition.get("field") or "").strip()
        allowed_fields = {
            "technical_indicator": {
                "last",
                "rvol",
                "vwap",
                "underlying_vs_vwap_pct",
                "orb_high",
                "orb_low",
                "ema_20",
                "ema_50",
                "ema_200",
                "rsi",
                "atr",
                "rv20",
                "rv60",
                "rv_rank",
                "volume_profile_poc",
                "volume_profile_value_area_low",
                "volume_profile_value_area_high",
                "volume_profile_low_volume_room_up_pct",
                "volume_profile_low_volume_room_down_pct",
            },
            "option_quote": {
                "ask",
                "bid",
                "mid",
                "last",
                "bid_ask_spread_pct",
                "volume",
                "open_interest",
                "delta",
                "gamma",
                "theta",
                "vega",
                "iv",
                "implied_volatility",
            },
        }[trigger_type]
        if field not in allowed_fields:
            raise ValueError(f"unsupported {trigger_type} trigger field")
        normalized["field"] = field
        normalized["label"] = str(condition.get("label") or field).strip()[:80]
        normalized["market_data_source"] = str(condition.get("market_data_source") or "thetadata").strip().lower() or "thetadata"
        if condition.get("longbridge_account"):
            normalized["longbridge_account"] = str(condition.get("longbridge_account") or "").strip()
        if trigger_type == "option_quote" and condition.get("contract_symbol"):
            normalized["contract_symbol"] = str(condition.get("contract_symbol") or "").strip().upper()
    if trigger_type == "rescan_score":
        normalized["score_field"] = str(condition.get("score_field") or "decision_score").strip() or "decision_score"
        for key in (
            "scan_id",
            "query",
            "ai_provider",
            "longbridge_account",
            "market_data_source",
            "rescan_scan_id",
            "rescan_requested_at",
            "last_rescan_scan_id",
            "last_score",
            "last_score_at",
            "last_error",
        ):
            if condition.get(key) not in {None, ""}:
                normalized[key] = condition.get(key)
        for key in ("use_ai", "council", "analysis_modules", "strategy_modes"):
            if key in condition:
                normalized[key] = condition.get(key)
    return normalized


def _trigger_cooldown_remaining_seconds(trigger: dict[str, Any]) -> int:
    last_triggered_at = _parse_dt(trigger.get("last_triggered_at"))
    if last_triggered_at is None:
        return 0
    cooldown = int(trigger.get("cooldown_seconds") or 0)
    elapsed = (datetime.now(timezone.utc) - last_triggered_at).total_seconds()
    return max(int(cooldown - elapsed), 0)


def _is_trigger_expired(trigger: dict[str, Any]) -> bool:
    expires_at = _parse_dt(trigger.get("expires_at"))
    return bool(expires_at and expires_at <= datetime.now(timezone.utc))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def list_due_scan_loop_instances(owner_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    init_observation_db()
    safe_limit = max(1, min(limit, 200))
    now = utc_now()
    params: list[Any] = [now, safe_limit]
    clause = "status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?"
    if owner_id is not None:
        clause += " AND owner_id = ?"
        params = [now, normalize_owner_id(owner_id), safe_limit]
    with connect() as db:
        rows = db.execute(
            f"SELECT * FROM scan_loop_instances WHERE {clause} ORDER BY next_run_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_instance_row(row) for row in rows]


def list_due_scan_triggers(owner_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    init_observation_db()
    safe_limit = max(1, min(limit, 200))
    now = utc_now()
    params: list[Any] = [now, safe_limit]
    clause = "enabled = 1 AND status IN ('active', 'waiting_market', 'cooldown', 'rescan_pending') AND next_check_at IS NOT NULL AND next_check_at <= ?"
    if owner_id is not None:
        clause += " AND owner_id = ?"
        params = [now, normalize_owner_id(owner_id), safe_limit]
    with connect() as db:
        rows = db.execute(
            f"SELECT * FROM scan_triggers WHERE {clause} ORDER BY next_check_at ASC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [_trigger_row(row) for row in rows]


def observation_due_snapshot(owner_id: str, *, preview_limit: int = 5) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    safe_limit = max(1, min(preview_limit, 20))
    due_scan_loops = list_due_scan_loop_instances(owner, limit=50)
    due_triggers = list_due_scan_triggers(owner, limit=50)
    due_opportunities = list_due_opportunities(owner, limit=50)
    instances = list_scan_loop_instances(owner)
    triggers = list_scan_triggers(owner)
    opportunities = list_opportunities(owner, limit=200)
    active_instances = [item for item in instances if item.get("status") == "active"]
    active_triggers = [item for item in triggers if item.get("enabled")]
    active_opportunities = [
        item
        for item in opportunities
        if item.get("followup_enabled") and item.get("status") not in OPPORTUNITY_TERMINAL_STATUSES
    ]
    next_scan_loops = [_scan_loop_preview(item) for item in _sort_by_time(active_instances, "next_run_at")[:safe_limit]]
    next_triggers = [_trigger_preview(item) for item in _sort_by_time(active_triggers, "next_check_at")[:safe_limit]]
    next_opportunities = [_opportunity_preview(item) for item in _sort_by_time(active_opportunities, "next_check_at")[:safe_limit]]
    return {
        "owner_id": owner,
        "generated_at": utc_now(),
        "next_scan_at": (next_scan_loops[0] or {}).get("next_run_at") if next_scan_loops else None,
        "next_trigger_check_at": (next_triggers[0] or {}).get("next_check_at") if next_triggers else None,
        "next_opportunity_check_at": (next_opportunities[0] or {}).get("next_check_at") if next_opportunities else None,
        "counts": {
            "scan_loops_total": len(instances),
            "scan_loops_active": len(active_instances),
            "triggers_total": len(triggers),
            "triggers_enabled": len(active_triggers),
            "opportunities_total": len(opportunities),
            "opportunities_followed": len(active_opportunities),
            "due_scan_loops": len(due_scan_loops),
            "due_triggers": len(due_triggers),
            "due_opportunities": len(due_opportunities),
        },
        "due": {
            "scan_loops": [_scan_loop_preview(item) for item in due_scan_loops[:safe_limit]],
            "triggers": [_trigger_preview(item) for item in due_triggers[:safe_limit]],
            "opportunities": [_opportunity_preview(item) for item in due_opportunities[:safe_limit]],
        },
        "next": {
            "scan_loops": next_scan_loops,
            "triggers": next_triggers,
            "opportunities": next_opportunities,
        },
        "last_failures": _observation_recent_failures(owner, limit=safe_limit),
    }


def _sort_by_time(items: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: item.get(field) or "9999-12-31T23:59:59")


def _scan_loop_preview(instance: dict[str, Any]) -> dict[str, Any]:
    schedule = instance.get("schedule") or {}
    return {
        "id": instance.get("id"),
        "name": instance.get("name"),
        "status": instance.get("status"),
        "symbols": (instance.get("symbols") or [])[:8],
        "next_run_at": instance.get("next_run_at"),
        "last_run_at": instance.get("last_run_at"),
        "last_market_state": instance.get("last_market_state"),
        "interval_minutes": schedule.get("interval_minutes"),
        "market_session": instance.get("market_session"),
        "ai_scan_policy": instance.get("ai_scan_policy"),
        "ai_scan_top_n": instance.get("ai_scan_top_n"),
    }


def _trigger_preview(trigger: dict[str, Any]) -> dict[str, Any]:
    condition = trigger.get("condition") or {}
    return {
        "id": trigger.get("id"),
        "name": trigger.get("name"),
        "symbol": trigger.get("symbol"),
        "status": trigger.get("status"),
        "enabled": trigger.get("enabled"),
        "type": condition.get("type"),
        "field": condition.get("field"),
        "operator": condition.get("operator"),
        "value": condition.get("value"),
        "next_check_at": trigger.get("next_check_at"),
        "last_checked_at": trigger.get("last_checked_at"),
        "last_triggered_at": trigger.get("last_triggered_at"),
        "market_policy": trigger.get("market_policy"),
    }


def _opportunity_preview(opportunity: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": opportunity.get("id"),
        "symbol": opportunity.get("symbol"),
        "title": opportunity.get("title"),
        "status": opportunity.get("status"),
        "status_label": opportunity.get("status_label") or _opportunity_status_label(str(opportunity.get("status") or "")),
        "lifecycle_phase": opportunity.get("lifecycle_phase") or _opportunity_lifecycle_phase(str(opportunity.get("status") or "")),
        "lifecycle_step": opportunity.get("lifecycle_step") or _opportunity_lifecycle_step(str(opportunity.get("status") or "")),
        "next_action": opportunity.get("next_action") or _opportunity_next_action(str(opportunity.get("status") or "")),
        "direction": opportunity.get("direction"),
        "strategy_structure": opportunity.get("strategy_structure"),
        "followup_enabled": opportunity.get("followup_enabled"),
        "next_check_at": opportunity.get("next_check_at"),
        "last_checked_at": opportunity.get("last_checked_at"),
        "followup_alert_count": opportunity.get("followup_alert_count"),
        "max_followup_alerts": opportunity.get("max_followup_alerts"),
    }


def _observation_recent_failures(owner_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    with connect() as db:
        try:
            rows = db.execute(
                """
                SELECT id, 'scan_loop_run' AS source_type, instance_id AS source_id, status, error, created_at
                FROM scan_loop_runs
                WHERE owner_id = ? AND (status IN ('failed', 'partial_failed') OR error IS NOT NULL)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
            failures.extend(
                {
                    "id": row["id"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "status": row["status"],
                    "error": row["error"],
                    "created_at": to_et_iso(row["created_at"]),
                }
                for row in rows
            )
        except Exception:
            pass
        try:
            rows = db.execute(
                """
                SELECT id, source_type, source_id, status, last_error AS error, created_at
                FROM notification_events
                WHERE owner_id = ? AND status = 'failed'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
            failures.extend(
                {
                    "id": row["id"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "status": row["status"],
                    "error": row["error"],
                    "created_at": to_et_iso(row["created_at"]),
                }
                for row in rows
            )
        except Exception:
            pass
    return sorted(failures, key=lambda item: item.get("created_at") or "", reverse=True)[:limit]


def _utc_after_seconds(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds or 1)))).isoformat()


def _next_scan_loop_run_at(instance: dict[str, Any], clock: dict[str, Any], market_state: str) -> str:
    if market_state == "regular_open":
        return _utc_after_seconds(_instance_interval_seconds(instance))
    now_et_value = str(clock.get("now_et") or "")
    try:
        now_et = datetime.fromisoformat(now_et_value).astimezone(ET)
    except ValueError:
        now_et = datetime.now(ET)
    next_open = next_regular_open_after(now_et, grace_minutes=10)
    return next_open.astimezone(timezone.utc).isoformat()


def _instance_interval_seconds(instance: dict[str, Any]) -> int:
    schedule = instance.get("schedule") or {}
    if schedule.get("type") == "interval_hours":
        return int(schedule.get("interval_hours") or 1) * 3600
    if schedule.get("type") == "interval_days":
        return int(schedule.get("interval_days") or 1) * 86400
    return int(schedule.get("interval_minutes") or 30) * 60


def _normalize_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    else:
        raw = list(value or [])
    seen: set[str] = set()
    symbols: list[str] = []
    for item in raw:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols[:100]


def _deep_get(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _loads(raw: Any, fallback: Any = None) -> Any:
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _row_has(row: Any, key: str) -> bool:
    try:
        return key in row.keys()
    except Exception:
        return key in row


def _mark_row(row: Any) -> dict[str, Any]:
    return {
        "owner_id": row["owner_id"],
        "scan_id": row["scan_id"],
        "starred": bool(row["starred"]),
        "note": row["note"] or "",
        "tags": _loads(row["tags_json"], []),
        "created_at": to_et_iso(row["created_at"]),
        "updated_at": to_et_iso(row["updated_at"]),
    }


def _scan_summary_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "locator_id": row["locator_id"],
        "owner_id": row["owner_id"],
        "ai_provider_owner": row["ai_provider_owner"] if _row_has(row, "ai_provider_owner") else row["owner_id"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": int(row["progress"] or 0),
        "created_at": to_et_iso(row["created_at"]),
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "query": row["query"],
        "symbol": row["symbol"],
        "ai_provider": row["ai_provider"],
        "longbridge_account": row["longbridge_account"],
        "market_data_source": row["market_data_source"] if _row_has(row, "market_data_source") else "longbridge",
        "use_ai": bool(row["use_ai"]),
        "council": bool(row["council"]),
        "analysis_modules": _loads(row["analysis_modules_json"], {}),
        "strategy_modes": _loads(row["strategy_modes_json"], []),
        "mode": row["mode"],
        "used_ai": bool(row["used_ai"]) if row["used_ai"] is not None else None,
        "error": row["error"],
        "mark": {
            "starred": bool(row["starred"]),
            "note": row["note"] or "",
            "tags": _loads(row["tags_json"], []),
            "updated_at": to_et_iso(row["mark_updated_at"]),
        },
        "result": None,
    }


def _channel_row(row: Any, *, include_sensitive: bool = False) -> dict[str, Any]:
    config = _loads(row["config_json"], {})
    if row["type"] == "webhook" and not include_sensitive:
        config = dict(config)
        secret = str(config.pop("secret", "") or "")
        bot_token = str(config.pop("bot_token", "") or "")
        access_token = str(config.pop("access_token", "") or "")
        config["secret_configured"] = bool(secret)
        config["bot_token_configured"] = bool(bot_token)
        config["access_token_configured"] = bool(access_token)
        if config.get("provider") == "telegram" and bot_token:
            config["url"] = "https://api.telegram.org/bot***/sendMessage"
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "type": row["type"],
        "label": row["label"],
        "config": config,
        "enabled": bool(row["enabled"]),
        "verified_at": to_et_iso(row["verified_at"]) if row["verified_at"] else None,
        "last_error": row["last_error"] if _row_has(row, "last_error") else None,
        "last_test_at": to_et_iso(row["last_test_at"]) if _row_has(row, "last_test_at") and row["last_test_at"] else None,
        "created_at": to_et_iso(row["created_at"]),
        "updated_at": to_et_iso(row["updated_at"]),
    }


def _delivery_log_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "event_id": row["event_id"],
        "channel_id": row["channel_id"],
        "channel_type": row["channel_type"],
        "provider": row["provider"],
        "status": row["status"],
        "attempt": int(row["attempt"] or 1),
        "request_preview": _loads(row["request_preview_json"], {}),
        "response_summary": row["response_summary"],
        "error": row["error"],
        "created_at": to_et_iso(row["created_at"]),
    }


def _event_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "channel_id": row["channel_id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "dedupe_key": row["dedupe_key"],
        "title": row["title"],
        "body": row["body"],
        "payload": _loads(row["payload_json"], {}),
        "status": row["status"],
        "attempts": int(row["attempts"] or 0),
        "last_error": row["last_error"],
        "created_at": to_et_iso(row["created_at"]),
        "sent_at": to_et_iso(row["sent_at"]) if row["sent_at"] else None,
    }


def _trigger_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "scan_id": row["scan_id"],
        "locator_id": row["locator_id"],
        "opportunity_id": row["opportunity_id"] if _row_has(row, "opportunity_id") else None,
        "name": row["name"],
        "symbol": row["symbol"],
        "condition": _loads(row["condition_json"], {}),
        "notification_channel_ids": _loads(row["notification_channel_ids_json"], []),
        "enabled": bool(row["enabled"]),
        "expires_at": to_et_iso(row["expires_at"]) if row["expires_at"] else None,
        "check_interval_seconds": int(row["check_interval_seconds"] or 300),
        "cooldown_seconds": int(row["cooldown_seconds"] or 1800),
        "max_trigger_count": int(row["max_trigger_count"] or 3),
        "trigger_count": int(row["trigger_count"] or 0),
        "last_checked_at": to_et_iso(row["last_checked_at"]) if row["last_checked_at"] else None,
        "last_triggered_at": to_et_iso(row["last_triggered_at"]) if row["last_triggered_at"] else None,
        "next_check_at": to_et_iso(row["next_check_at"]) if row["next_check_at"] else None,
        "status": row["status"],
        "market_policy": row["market_policy"] if _row_has(row, "market_policy") else "regular_only",
        "opening_grace_minutes": int(row["opening_grace_minutes"] or 10) if _row_has(row, "opening_grace_minutes") else 10,
        "created_at": to_et_iso(row["created_at"]),
        "updated_at": to_et_iso(row["updated_at"]),
    }


def _watchlist_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "name": row["name"],
        "description": row["description"],
        "symbols": _loads(row["symbols_json"], []),
        "created_at": to_et_iso(row["created_at"]),
        "updated_at": to_et_iso(row["updated_at"]),
    }


def _instance_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "watchlist_id": row["watchlist_id"],
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
        "symbols": _loads(row["symbols_snapshot_json"], []),
        "schedule": _loads(row["schedule_json"], {}),
        "market_session": row["market_session"],
        "eod_review_enabled": bool(row["eod_review_enabled"]) if "eod_review_enabled" in row.keys() else False,
        "eod_run_time_et": row["eod_run_time_et"] if "eod_run_time_et" in row.keys() else None,
        "weekend_review_enabled": bool(row["weekend_review_enabled"]) if "weekend_review_enabled" in row.keys() else False,
        "weekend_run_time_local": row["weekend_run_time_local"] if "weekend_run_time_local" in row.keys() else None,
        "market_data_source": row["market_data_source"],
        "option_data_source": row["option_data_source"] if _row_has(row, "option_data_source") else "thetadata",
        "ai_provider": row["ai_provider"],
        "use_ai": bool(row["use_ai"]),
        "council": bool(row["council"]),
        "analysis_modules": _loads(row["analysis_modules_json"], {}),
        "strategy_modes": _loads(row["strategy_modes_json"], []),
        "prompt_template": row["prompt_template"],
        "prefilter_rules": _loads(row["prefilter_rules_json"], {"logic": "and", "conditions": []}),
        "alert_rules": _loads(row["alert_rules_json"], {"logic": "and", "conditions": []}),
        "alert_mode": row["alert_mode"],
        "notification_channel_ids": _loads(row["notification_channel_ids_json"], []),
        "max_alerts_per_day": int(row["max_alerts_per_day"] or 5),
        "max_ai_scans_per_day": int(row["max_ai_scans_per_day"] or 10),
        "ai_scan_policy": _normalize_ai_scan_policy(row["ai_scan_policy"] if "ai_scan_policy" in row.keys() else None),
        "ai_scan_top_n": _scan_loop_ai_top_n(row["ai_scan_top_n"] if "ai_scan_top_n" in row.keys() else None),
        "symbol_cooldown_minutes": int(row["symbol_cooldown_minutes"] or 30),
        "run_timeout_seconds": int(row["run_timeout_seconds"] or 600),
        "expires_at": to_et_iso(row["expires_at"]) if row["expires_at"] else None,
        "last_eod_review_date": row["last_eod_review_date"] if "last_eod_review_date" in row.keys() else None,
        "last_weekend_review_key": row["last_weekend_review_key"] if "last_weekend_review_key" in row.keys() else None,
        "last_run_at": to_et_iso(row["last_run_at"]) if row["last_run_at"] else None,
        "next_run_at": to_et_iso(row["next_run_at"]) if row["next_run_at"] else None,
        "last_market_state": row["last_market_state"],
        "ai_report_cache": _loads(row["ai_report_cache_json"], {}) if _row_has(row, "ai_report_cache_json") else {},
        "created_at": to_et_iso(row["created_at"]),
        "updated_at": to_et_iso(row["updated_at"]),
    }


def _run_row(row: Any) -> dict[str, Any]:
    summary = _loads(row["summary_json"], {})
    summary["ai_cost_actual"] = _scan_loop_ai_cost_actual(row["owner_id"], row["id"])
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "instance_id": row["instance_id"],
        "watchlist_id": row["watchlist_id"],
        "status": row["status"],
        "started_at": to_et_iso(row["started_at"]) if row["started_at"] else None,
        "finished_at": to_et_iso(row["finished_at"]) if row["finished_at"] else None,
        "scanned_count": int(row["scanned_count"] or 0),
        "matched_count": int(row["matched_count"] or 0),
        "alerted_count": int(row["alerted_count"] or 0),
        "market_state": row["market_state"],
        "data_freshness": _loads(row["data_freshness_json"], {}),
        "summary": summary,
        "error": row["error"],
        "created_at": to_et_iso(row["created_at"]),
    }


def _scan_loop_cost_projection(selected_count: int) -> dict[str, Any]:
    count = max(int(selected_count or 0), 0)
    # Rough pre-run planning estimate for one full scan: planner + decision + explanation.
    estimated_cost_cny = count * 0.035
    estimated_cost_usd = count * 0.0049
    return {
        "estimated_ai_calls": count * 3,
        "estimated_scan_count": count,
        "estimated_cost_cny": round(estimated_cost_cny, 4),
        "estimated_cost_usd": round(estimated_cost_usd, 4),
        "assumption": "rough_deepseek_v4_flash_3_calls_per_scan",
    }


def _scan_loop_ai_cost_actual(owner_id: str, run_id: str) -> dict[str, Any]:
    try:
        with connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS calls, SUM(total_tokens) AS total_tokens,
                       SUM(estimated_cost_cny) AS cost_cny, SUM(estimated_cost_usd) AS cost_usd
                FROM ai_usage_events
                WHERE owner_id = ? AND source_type = 'scan_loop' AND source_id = ?
                """,
                (normalize_owner_id(owner_id), run_id),
            ).fetchone()
    except Exception:
        return {"calls": 0, "total_tokens": 0, "estimated_cost_cny": 0, "estimated_cost_usd": 0}
    return {
        "calls": int((row or {}).get("calls") or 0) if hasattr(row, "get") else int(row["calls"] or 0),
        "total_tokens": int((row or {}).get("total_tokens") or 0) if hasattr(row, "get") else int(row["total_tokens"] or 0),
        "estimated_cost_cny": round(float(((row or {}).get("cost_cny") if hasattr(row, "get") else row["cost_cny"]) or 0), 6),
        "estimated_cost_usd": round(float(((row or {}).get("cost_usd") if hasattr(row, "get") else row["cost_usd"]) or 0), 6),
    }


def _run_item_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "owner_id": row["owner_id"],
        "instance_id": row["instance_id"],
        "watchlist_id": row["watchlist_id"],
        "symbol": row["symbol"],
        "status": row["status"],
        "prefilter_status": row["prefilter_status"],
        "prefilter_result": _loads(row["prefilter_result_json"], {}),
        "data_timestamp": to_et_iso(row["data_timestamp"]) if row["data_timestamp"] else None,
        "data_freshness": row["data_freshness"],
        "scan_id": row["scan_id"],
        "triggered": bool(row["triggered"]),
        "trigger_reasons": _loads(row["trigger_reasons_json"], []),
        "score": row["score"],
        "recommendation": _loads(row["recommendation_json"], {}),
        "error": row["error"],
        "created_at": to_et_iso(row["created_at"]),
    }


def _opportunity_row(row: Any) -> dict[str, Any]:
    status = str(row["status"] or "created")
    risk_plan = _loads(row["risk_plan_json"], {})
    entry_reference = _loads(row["entry_reference_json"], {})
    trigger_snapshot = _loads(row["trigger_snapshot_json"], {})
    followup_enabled = bool(row["followup_enabled"]) if "followup_enabled" in row.keys() else True
    next_check_at = row["next_check_at"] if "next_check_at" in row.keys() and row["next_check_at"] else None
    last_alert_at = row["last_alert_at"] if "last_alert_at" in row.keys() and row["last_alert_at"] else None
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "scan_id": row["scan_id"],
        "scan_loop_instance_id": row["scan_loop_instance_id"],
        "watchlist_id": row["watchlist_id"],
        "symbol": row["symbol"],
        "status": status,
        "status_label": _opportunity_status_label(status),
        "lifecycle_phase": _opportunity_lifecycle_phase(status),
        "lifecycle_step": _opportunity_lifecycle_step(status),
        "next_action": _opportunity_next_action(status),
        "action_priority": _opportunity_action_priority(
            status,
            followup_enabled=followup_enabled,
            next_check_at=next_check_at,
            last_alert_at=last_alert_at,
            trigger_snapshot=trigger_snapshot,
        ),
        "title": row["title"],
        "direction": row["direction"],
        "strategy_structure": row["strategy_structure"],
        "contract_symbol": row["contract_symbol"] if "contract_symbol" in row.keys() else None,
        "strategy_type": row["strategy_type"] if "strategy_type" in row.keys() else row["strategy_structure"],
        "ai_direction": row["ai_direction"] if "ai_direction" in row.keys() else None,
        "derived_direction": row["derived_direction"] if "derived_direction" in row.keys() else None,
        "thesis": row["thesis"] if "thesis" in row.keys() else None,
        "legs": _loads(row["legs_json"], []) if "legs_json" in row.keys() else [],
        "payoff": _loads(row["payoff_json"], {}) if "payoff_json" in row.keys() else {},
        "validation": _loads(row["validation_json"], {}) if "validation_json" in row.keys() else {},
        "entry_reference": entry_reference,
        "risk_plan": risk_plan,
        "trigger_snapshot": trigger_snapshot,
        "gex_snapshot": _loads(row["gex_snapshot_json"], {}),
        "notification_channel_ids": _loads(row["notification_channel_ids_json"], []) if "notification_channel_ids_json" in row.keys() else [],
        "followup_enabled": followup_enabled,
        "followup_interval_seconds": int(row["followup_interval_seconds"] or 300) if "followup_interval_seconds" in row.keys() else 300,
        "cooldown_seconds": int(row["cooldown_seconds"] or 1800) if "cooldown_seconds" in row.keys() else 1800,
        "max_followup_alerts": int(row["max_followup_alerts"] or 6) if "max_followup_alerts" in row.keys() else 6,
        "followup_alert_count": int(row["followup_alert_count"] or 0) if "followup_alert_count" in row.keys() else 0,
        "last_checked_at": to_et_iso(row["last_checked_at"]) if "last_checked_at" in row.keys() and row["last_checked_at"] else None,
        "next_check_at": to_et_iso(next_check_at) if next_check_at else None,
        "last_alert_at": to_et_iso(last_alert_at) if last_alert_at else None,
        "expires_at": to_et_iso(row["expires_at"]) if "expires_at" in row.keys() and row["expires_at"] else None,
        "created_at": to_et_iso(row["created_at"]),
        "updated_at": to_et_iso(row["updated_at"]),
    }


def _opportunity_event_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "opportunity_id": row["opportunity_id"],
        "event_type": row["event_type"],
        "title": row["title"],
        "body": row["body"],
        "payload": _loads(row["payload_json"], {}),
        "created_at": to_et_iso(row["created_at"]),
    }

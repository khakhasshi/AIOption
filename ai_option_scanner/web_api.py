from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .account_store import (
    accounts_as_rows,
    create_account,
    get_account,
    preferred_sdk_account,
    normalize_owner_id,
    resolve_account,
    set_default_account,
    update_account_sdk_credentials,
)
from .app_auth import (
    COOKIE_NAME,
    auth_enabled,
    auth_user_permissions,
    auth_user_resource_usage,
    auth_users_as_rows,
    clear_login_failures,
    cookie_options,
    create_auth_user,
    create_session_token,
    delete_auth_user,
    get_auth_user,
    login_allowed,
    provision_oauth_user,
    record_login_failure,
    update_auth_user_permissions,
    user_has_password,
    validate_username,
    verify_login,
    verify_session_token,
)
from .oauth_login import (
    OAuthError,
    normalize_provider,
    oauth_public_config,
    verify_id_token,
)
from .turnstile import (
    TurnstileError,
    turnstile_public_config,
    verify_turnstile,
)
from .oauth_store import (
    count_identities_for_user,
    find_username_by_identity,
    link_identity,
    list_identities_for_user,
    unlink_identity,
)
from .ai_client import ask_ai, chat_with_tools, get_last_ai_error, load_dotenv, resolve_chat_provider, stream_ask_ai, stream_chat_messages
from .ai_providers import DEFAULT_PROVIDER_NAME as DEFAULT_AI_PROVIDER
from .ai_providers import add_provider, delete_provider, provider_config_path, providers_as_rows
from .ai_provider_store import delete_user_provider, upsert_user_provider, user_providers_as_rows
from .ai_usage_store import ai_usage_summary
from .broker_client import account_ref_for_config
from .broker_store import (
    broker_accounts_as_rows,
    create_broker_account,
    create_usmart_account,
    delete_broker_account,
    resolve_broker_account,
    set_broker_account_default,
)
from .db import connect, database_backend, db_pool_snapshot
from .intent_planner import analysis_presets_for_ui
from .longbridge_auth import auth_manager
from .longbridge_client import kline as longbridge_kline, quote as longbridge_quote
from .market_calendar import market_clock, market_environment, previous_nyse_trading_day
from .time_utils import et_today
from .thetadata_store import (
    delete_thetadata_credentials,
    save_thetadata_credentials,
    thetadata_config_status,
)
from .yfinance_option_tool import market_data as yf_market_data
from .longbridge_option_tool import collect_candidates as lb_collect_candidates, option_chain_info, option_expirations
from .owner_migration import migrate_browser_owner_to_user
from .observation_store import (
     _fetch_current_gex_snapshot as fetch_gex,
    archive_opportunity,
    build_notification_payload_preview,
    check_opportunity_followup,
    check_scan_trigger,
    create_notification_channel,
    create_scan_loop_instance,
    create_scan_trigger,
    create_watchlist,
    delete_notification_channel,
    delete_scan_loop_instance,
    delete_watchlist,
    get_notification_channel,
    get_scan_loop_instance,
    get_scan_loop_run,
    list_notification_channels,
    list_notification_delivery_logs,
    list_notification_events,
    list_opportunities,
    list_scan_loop_instances,
    list_scan_loop_runs,
    list_scan_marks,
    list_scan_runs_with_marks,
    list_scan_triggers,
    list_watchlists,
    mark_scan,
    observation_due_snapshot,
    run_scan_loop_instance,
    send_test_notification_channel,
    send_notification_event,
    process_notification_events,
    test_scan_trigger,
    test_scan_loop_instance,
    delete_scan_trigger,
    update_notification_channel,
    update_scan_trigger,
    update_scan_loop_instance,
    update_watchlist,
    get_opportunity,
    list_opportunity_events,
    pause_opportunity,
    process_due_opportunity_followups,
    resume_opportunity,
    update_opportunity,
)
from .observation_scheduler import observation_scheduler_runtime_snapshot, run_observation_due_cycle
from .beta_lottery import admin_action as beta_lottery_admin_action
from .beta_lottery import admin_rows as beta_lottery_admin_rows
from .beta_lottery import enter_lottery as beta_lottery_enter
from .beta_lottery import finalize_draw as beta_lottery_finalize_draw
from .beta_lottery import public_status as beta_lottery_public_status
from . import chat_store
from .scan_jobs import submit_scan
from .scan_service import run_scan
from .scan_events import iter_scan_events
from .scan_store import get_scan_run, mark_interrupted_scan_runs
from .trading_agent import TradingRunBlockedError, recent_trading_runs, start_trading_run, trading_run_detail, validate_trading_readiness
from .trading_flatten import CONFIRMATION_TEXT, flatten_all_positions
from .trading_instance_actions import (
    INSTANCE_CANCEL_CONFIRMATION,
    INSTANCE_BULK_DELETE_CONFIRMATION,
    INSTANCE_DELETE_CONFIRMATION,
    INSTANCE_FLATTEN_CONFIRMATION,
    INSTANCE_RISK_RESET_CONFIRMATION,
    InstanceHasLiveBrokerStateError,
    bulk_delete_trade_instances,
    cancel_trade_instance_orders,
    delete_trade_instance,
    flatten_trade_instance,
    reset_trade_instance_risk,
)
from .trading_monitor import monitor_pending_stops, order_monitor_runtime_snapshot, start_order_monitor
from .trading_quality import ai_decision_quality
from .trading_scheduler import next_config_run_preview, scheduler_status, start_trading_scheduler
from .trading_snapshot import trading_snapshots
from .trading_store import get_trading_config, list_schedule_fires, mark_interrupted_trading_runs, save_trading_config, schedule_runtime_snapshot, trading_runtime_counts
from .auto_trade_store import (
    create_auto_trade_instance,
    delete_auto_trade_instance,
    get_auto_trade_cycle,
    get_auto_trade_instance,
    list_auto_trade_cycles,
    list_auto_trade_instances,
    update_auto_trade_instance,
)
from .auto_trade_config import preset_caps
from .time_utils import now_et_iso
from .redis_runtime import process_role, redis_available, redis_llen, redis_pool_snapshot, web_enabled, worker_enabled
from .redis_queue import SCAN_QUEUE_KEY
from .longbridge_sdk_client import option_quotes, sdk_pool_snapshot
from .trade_review_store import get_trade_review, list_recent_trade_reviews
from .post_mortem_worker import start_post_mortem_worker


APP_ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = APP_ROOT / "web" / "dist"
APP_STARTED_AT = time.monotonic()

load_dotenv(APP_ROOT / ".env")


def _cors_origins() -> list[str]:
    defaults = [
        "http://localhost:7001",
        "http://127.0.0.1:7001",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    extra = [
        item.strip()
        for item in str(os.getenv("AI_OPTION_CORS_ORIGINS") or "").split(",")
        if item.strip()
    ]
    return list(dict.fromkeys([*defaults, *extra]))


app = FastAPI(title="AI Option Scanner", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["*"],
    allow_headers=["Content-Type", "X-AI-Option-User"],
    allow_credentials=True,
)


class ScanRequest(BaseModel):
    query: str
    symbol: str | None = None
    ai_provider: str = "deepseek"
    longbridge_account: str | None = None
    market_data_source: str = "yfinance"
    option_data_source: str = "thetadata"
    use_ai: bool = True
    council: bool = False
    analysis_modules: dict[str, bool] | None = None
    strategy_modes: list[str] | None = None


class ChatMessage(BaseModel):
    role: str
    text: str = ""


class ChatRequest(BaseModel):
    message: str
    ai_provider: str = "deepseek"
    longbridge_account: str | None = None
    tools: list[str] | None = None
    history: list[ChatMessage] | None = None
    session_id: str | None = None
    instance_id: str | None = None
    locale: str | None = None


class ChatSessionCreateRequest(BaseModel):
    title: str | None = None
    provider: str | None = None
    account: str | None = None
    tools: list[str] | None = None


class ChatSessionUpdateRequest(BaseModel):
    title: str | None = None
    provider: str | None = None
    account: str | None = None
    tools: list[str] | None = None


class BetaLotteryActionRequest(BaseModel):
    action: str
    announce_at: str | None = None
    registration_start_at: str | None = None
    slot_count: int | None = None
    user_valid_days: float | None = None
    limits: dict[str, int] | None = None


class AccountRequest(BaseModel):
    name: str
    label: str | None = None
    set_default: bool = False
    app_key: str | None = None
    app_secret: str | None = None
    access_token: str | None = None


class AccountCredentialsRequest(BaseModel):
    app_key: str
    app_secret: str
    access_token: str


class ThetaDataCredentialsRequest(BaseModel):
    email: str
    password: str


class ThetaDataTestRequest(BaseModel):
    symbol: str = "SPY"


class BrokerAccountRequest(BaseModel):
    broker: str = "alpaca"
    name: str
    label: str | None = None
    # Alpaca uses key/secret; uSMART uses the channel/RSA fields below. Make the
    # key/secret optional so a uSMART account can omit them.
    api_key: str | None = None
    api_secret: str | None = None
    paper: bool = True
    set_default: bool = False
    # uSMART-only credential fields.
    channel: str | None = None
    sign_private_key: str | None = None
    encrypt_public_key: str | None = None
    phone: str | None = None
    area_code: str = "852"
    trade_password: str | None = None


class ProviderRequest(BaseModel):
    name: str
    base_url: str
    model: str
    api_key_env: str
    temperature: float = 0.25
    provider_type: str = "openai"


class UserProviderRequest(BaseModel):
    name: str
    label: str | None = None
    base_url: str
    model: str
    api_key: str
    temperature: float = 0.25
    provider_type: str = "openai"
    is_default: bool = False


class ScanMarkRequest(BaseModel):
    starred: bool = True
    note: str | None = None
    tags: list[str] | None = None


class NotificationChannelRequest(BaseModel):
    type: str = "email"
    label: str | None = None
    provider: str | None = None
    email: str | None = None
    url: str | None = None
    secret: str | None = None
    header_name: str | None = None
    bot_token: str | None = None
    chat_id: str | None = None
    phone_number_id: str | None = None
    access_token: str | None = None
    to: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_variables: list[str] | None = None
    enabled: bool = True


class NotificationChannelUpdateRequest(BaseModel):
    type: str | None = None
    label: str | None = None
    provider: str | None = None
    email: str | None = None
    url: str | None = None
    secret: str | None = None
    header_name: str | None = None
    bot_token: str | None = None
    chat_id: str | None = None
    phone_number_id: str | None = None
    access_token: str | None = None
    to: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    template_variables: list[str] | None = None
    enabled: bool | None = None


class ScanTriggerRequest(BaseModel):
    name: str | None = None
    symbol: str | None = None
    scan_id: str | None = None
    locator_id: str | None = None
    opportunity_id: str | None = None
    condition: dict[str, Any]
    notification_channel_ids: list[str] | None = None
    enabled: bool = True
    expires_at: str | None = None
    check_interval_seconds: int = 300
    cooldown_seconds: int = 1800
    max_trigger_count: int = 3
    market_policy: str | None = None
    opening_grace_minutes: int | None = None


class ScanTriggerCheckRequest(BaseModel):
    current_value: float | None = None
    quote_snapshot: dict[str, Any] | None = None


class ScanTriggerUpdateRequest(BaseModel):
    name: str | None = None
    symbol: str | None = None
    opportunity_id: str | None = None
    condition: dict[str, Any] | None = None
    notification_channel_ids: list[str] | None = None
    enabled: bool | None = None
    expires_at: str | None = None
    check_interval_seconds: int | None = None
    cooldown_seconds: int | None = None
    max_trigger_count: int | None = None
    market_policy: str | None = None
    opening_grace_minutes: int | None = None
    status: str | None = None


class WatchlistRequest(BaseModel):
    name: str
    description: str | None = None
    symbols: list[str] | str


class ScanLoopInstanceRequest(BaseModel):
    watchlist_id: str | None = None
    name: str
    description: str | None = None
    status: str = "active"
    symbols: list[str] | str | None = None
    schedule: dict[str, Any] | None = None
    market_session: str = "regular"
    eod_review_enabled: bool = False
    eod_run_time_et: str | None = None
    weekend_review_enabled: bool = False
    weekend_run_time_local: str | None = None
    market_data_source: str = "yfinance"
    option_data_source: str = "thetadata"
    ai_provider: str = "deepseek"
    use_ai: bool = True
    council: bool = True
    analysis_modules: dict[str, bool] | None = None
    strategy_modes: list[str] | None = None
    prompt_template: str | None = None
    prefilter_rules: dict[str, Any] | None = None
    alert_rules: dict[str, Any] | None = None
    alert_mode: str = "best_per_run"
    notification_channel_ids: list[str] | None = None
    max_alerts_per_day: int = 5
    max_ai_scans_per_day: int = 10
    ai_scan_policy: str = "prefilter_matched"
    ai_scan_top_n: int = 3


class ScanLoopRunRequest(BaseModel):
    quote_snapshots: dict[str, dict[str, Any]] | None = None
    allow_non_regular: bool = False
    submit_scans: bool = True
    review_only: bool = False


class OpportunityUpdateRequest(BaseModel):
    status: str | None = None
    title: str | None = None
    thesis: str | None = None
    risk_plan: dict[str, Any] | None = None
    notification_channel_ids: list[str] | None = None
    followup_enabled: bool | None = None
    followup_interval_seconds: int | None = None
    cooldown_seconds: int | None = None
    max_followup_alerts: int | None = None
    expires_at: str | None = None


class OpportunityCheckRequest(BaseModel):
    quote_snapshot: dict[str, Any] | None = None


class OpportunityFollowupProcessRequest(BaseModel):
    limit: int = 50


class ObservationDueCycleRequest(BaseModel):
    scan_limit: int = 5
    trigger_limit: int = 20
    opportunity_limit: int = 20


class LoginRequest(BaseModel):
    username: str
    password: str
    accepted_terms: bool = False
    route_mode: str = "auto"
    turnstile_token: str | None = None


class OAuthLoginRequest(BaseModel):
    provider: str
    credential: str
    nonce: str | None = None
    accepted_terms: bool = False
    route_mode: str = "auto"
    turnstile_token: str | None = None


class OAuthLinkRequest(BaseModel):
    provider: str
    credential: str
    nonce: str | None = None


class AuthUserCreateRequest(BaseModel):
    username: str
    password: str
    can_analyze: bool = True
    can_trade: bool = False
    is_admin: bool = False
    remaining_days: float = 7
    max_daily_scans: int | None = None
    max_daily_ai_scans: int | None = None
    max_daily_ai_chat: int | None = None
    max_watchlists: int | None = None
    max_scan_loop_instances: int | None = None
    max_notification_channels: int | None = None
    max_longbridge_accounts: int | None = None


class AuthUserUpdateRequest(BaseModel):
    can_analyze: bool | None = None
    can_trade: bool | None = None
    is_admin: bool | None = None
    remaining_days: float | None = None
    max_daily_scans: int | None = None
    max_daily_ai_scans: int | None = None
    max_daily_ai_chat: int | None = None
    max_watchlists: int | None = None
    max_scan_loop_instances: int | None = None
    max_notification_channels: int | None = None
    max_longbridge_accounts: int | None = None


class TradingConfigRequest(BaseModel):
    live_enabled: bool = False
    total_capital: float = 10000
    run_time_et: str = "10:30"
    single_instance_enabled: bool | None = None
    multi_instance_enabled: bool = False
    schedule_profile: str = "single_run"
    schedule_slots: list[dict[str, Any]] | None = None
    universe: list[str] | str | None = None
    prompt_template: str | None = None
    top_n: int = 5
    default_stop_loss_pct: float = 25
    default_take_profit_pct: float = 30
    tiered_take_profit_enabled: bool = False
    default_take_profit_1_pct: float = 20
    default_take_profit_2_pct: float = 35
    use_ai: bool = True
    council: bool = True
    ai_adjust_allocation: bool = False
    ai_adjust_stop_loss: bool = True
    ai_adjust_take_profit: bool = False
    software_stop_enabled: bool = True
    software_take_profit_enabled: bool = True
    risk_max_daily_runs: int = 3
    risk_max_consecutive_failures: int = 2
    risk_max_unprotected_quantity: int = 0
    risk_max_single_stop_loss_pct: float = 45
    risk_require_protection_for_market_order: bool = True
    low_gate_enabled: bool = False
    ai_provider: str = "deepseek"
    broker: str = "longbridge"
    broker_account: str | None = None
    longbridge_account: str | None = None
    market_data_source: str = "yfinance"
    option_data_source: str = "thetadata"
    analysis_modules: dict[str, bool] | None = None
    strategy_modes: list[str] | None = None
    strategy_auto_execute_enabled: bool = False
    strategy_unwind_on_failure: bool = True
    wait_for_fill_seconds: int = 8
    entry_order_type: str = "market"
    exit_order_type: str = "market"


class FlattenPositionsRequest(BaseModel):
    confirmation: str


class InstanceActionRequest(BaseModel):
    confirmation: str
    force: bool = False


class InstanceBulkDeleteRequest(BaseModel):
    confirmation: str
    run_ids: list[str]
    force: bool = False


class AutoTradeInstanceRequest(BaseModel):
    name: str | None = None
    use_broker: bool = False
    broker: str | None = None
    broker_account: str | None = None
    ai_provider: str | None = None
    symbols: list[str] = []
    interval_minutes: int = 5
    risk_preset: str = "conservative"
    total_capital: float = 3000
    session_policy: str = "regular_only"
    config: dict[str, Any] = {}


class AutoTradeStartRequest(BaseModel):
    confirmation: str | None = None


class BetaLotteryEnterRequest(BaseModel):
    nickname: str
    contact: str
    entry_token: str | None = None
    fingerprint: str | None = None


def _account_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _broker_api_enabled() -> bool:
    return _env_bool("AI_OPTION_ENABLE_BROKER_API", False)


def _is_broker_or_trading_path(path: str) -> bool:
    return path.startswith(("/api/trading/", "/api/auto-trade/", "/api/brokers/", "/api/longbridge/"))


def _route_mode(value: str | None) -> str:
    cleaned = str(value or "auto").strip().lower()
    return cleaned if cleaned in {"auto", "primary", "secondary"} else "auto"


def _request_is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = str(request.headers.get("x-forwarded-proto") or "").lower()
    if "https" in {item.strip() for item in forwarded.split(",")}:
        return True
    cf_visitor = str(request.headers.get("cf-visitor") or "").lower()
    return '"scheme":"https"' in cf_visitor or '"scheme": "https"' in cf_visitor


@app.middleware("http")
async def require_authenticated_api(request: Request, call_next):
    if request.method.upper() == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    public_api_paths = {
        "/api/health",
        "/api/market-clock",
        "/api/market-environment",
        "/api/beta-lottery/status",
        "/api/beta-lottery/enter",
    }
    if path.startswith("/api/") and path not in public_api_paths and not path.startswith("/api/auth/"):
        if not _broker_api_enabled() and _is_broker_or_trading_path(path):
            return JSONResponse({"detail": "broker and trading APIs are disabled on this node"}, status_code=503)
        username = verify_session_token(request.cookies.get(COOKIE_NAME))
        if auth_enabled() and username is None:
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        if username:
            request.state.auth_username = username
            _override_owner_header(request, username)
            permissions = auth_user_permissions(username)
            if _can_analyze_path(path) and not permissions["can_analyze"]:
                return JSONResponse({"detail": "analysis permission required"}, status_code=403)
        if path.startswith("/api/trading/") or path.startswith("/api/longbridge/") or path.startswith("/api/auto-trade/"):
            permissions = auth_user_permissions(username)
            if not permissions["can_trade"]:
                return JSONResponse({"detail": "trade permission required"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, Any]:
    # Shallow dependency probe: enough to tell a live-but-broken process from a
    # healthy one, but fast and unauthenticated. Always returns HTTP 200 so the
    # load balancer keeps routing on a single degraded dependency (a web node
    # without Redis can still serve part of the API); inspect the `status`
    # field for ok|degraded. Error details are intentionally omitted here and
    # left to the admin-gated /api/admin/server-health snapshot.
    db = _db_health()
    redis_ok = redis_available()
    redis_latency_ms = _redis_latency_ms() if redis_ok else None
    status = "ok" if db["ok"] and redis_ok else "degraded"
    return {
        "status": status,
        "database": {"ok": db["ok"], "backend": db.get("backend"), "latency_ms": db.get("latency_ms")},
        "redis": {"ok": redis_ok, "latency_ms": redis_latency_ms},
        "role": process_role(),
    }


@app.get("/api/beta-lottery/status")
def beta_lottery_status(entry_token: str | None = None) -> dict[str, Any]:
    return beta_lottery_public_status(entry_token)


@app.post("/api/beta-lottery/enter")
def beta_lottery_register(request: Request, payload: BetaLotteryEnterRequest) -> dict[str, Any]:
    request_meta = {
        "client_host": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent", ""),
        "route_mode": request.cookies.get("ai_option_route", "auto"),
        "fingerprint": payload.fingerprint or "",
        "headers": dict(request.headers),
    }
    try:
        return beta_lottery_enter(
            nickname=payload.nickname,
            contact=payload.contact,
            request_meta=request_meta,
            entry_token=payload.entry_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/beta-lottery/admin")
def beta_lottery_admin(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return beta_lottery_admin_rows()


@app.post("/api/beta-lottery/finalize")
def beta_lottery_finalize(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return beta_lottery_finalize_draw()


@app.post("/api/beta-lottery/admin/action")
def beta_lottery_admin_action_endpoint(request: Request, payload: BetaLotteryActionRequest) -> dict[str, Any]:
    _require_admin(request)
    try:
        return beta_lottery_admin_action(
            payload.action,
            announce_at=payload.announce_at,
            registration_start_at=payload.registration_start_at,
            slot_count=payload.slot_count,
            user_valid_days=payload.user_valid_days,
            limits=payload.limits,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/server-health")
def admin_server_health(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return _server_health_snapshot()


@app.get("/api/admin/ai-usage")
def admin_ai_usage(request: Request, owner_id: str | None = None, days: int = 30, limit: int = 80) -> dict[str, Any]:
    _require_admin(request)
    return ai_usage_summary(owner_id=owner_id, days=days, limit=limit)


@app.get("/api/ai-usage/me")
def my_ai_usage(request: Request, days: int = 30, limit: int = 60) -> dict[str, Any]:
    _require_admin(request)
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    return ai_usage_summary(owner_id=username, days=days, limit=limit)


@app.get("/api/auth/me/usage")
def my_resource_usage(request: Request) -> dict[str, Any]:
    """Current user's per-resource quota usage and limits (ET date).  Any
    authenticated user can read their own usage — not admin-gated."""
    if not auth_enabled():
        return {"resources": [], "note": "auth disabled"}
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    permissions = auth_user_permissions(username)
    limits = permissions.get("limits") if isinstance(permissions, dict) else {}
    usage = auth_user_resource_usage(username) if username else {}
    # Build a readable list of resource entries for the UI.
    resources = [
        {"key": "daily_scans", "label": "每日扫描", "usage": usage.get("daily_scans", 0), "limit": limits.get("max_daily_scans", -1)},
        {"key": "daily_ai_scans", "label": "每日 AI 精扫", "usage": usage.get("daily_ai_scans", 0), "limit": limits.get("max_daily_ai_scans", -1)},
        {"key": "daily_ai_chat", "label": "每日 AI 对话", "usage": usage.get("daily_ai_chat", 0), "limit": limits.get("max_daily_ai_chat", -1)},
        {"key": "watchlists", "label": "股票池", "usage": usage.get("watchlists", 0), "limit": limits.get("max_watchlists", -1)},
        {"key": "scan_loop_instances", "label": "雷达实例", "usage": usage.get("scan_loop_instances", 0), "limit": limits.get("max_scan_loop_instances", -1)},
        {"key": "notification_channels", "label": "通知渠道", "usage": usage.get("notification_channels", 0), "limit": limits.get("max_notification_channels", -1)},
        {"key": "longbridge_accounts", "label": "Longbridge 账户", "usage": usage.get("longbridge_accounts", 0), "limit": limits.get("max_longbridge_accounts", -1)},
    ]
    return {
        "resources": resources,
        "can_analyze": permissions.get("can_analyze"),
        "can_trade": permissions.get("can_trade"),
        "is_admin": permissions.get("is_admin"),
        "expired": permissions.get("expired"),
    }


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    if not auth_enabled():
        return {"authenticated": True, "username": "local", "auth_enabled": False, "can_trade": True, "is_admin": True, "broker_api_enabled": _broker_api_enabled()}
    username = verify_session_token(request.cookies.get(COOKIE_NAME))
    return {"authenticated": bool(username), "username": username, "auth_enabled": True, "broker_api_enabled": _broker_api_enabled(), **auth_user_permissions(username)}


@app.post("/api/auth/login")
def auth_login(request: Request, response: Response, payload: LoginRequest) -> dict[str, Any]:
    if not auth_enabled():
        return {"authenticated": True, "username": "local", "auth_enabled": False, "can_trade": True, "is_admin": True, "broker_api_enabled": _broker_api_enabled()}
    if not payload.accepted_terms:
        raise HTTPException(status_code=400, detail="terms acceptance required")
    client_ip = request.client.host if request.client else "unknown"
    try:
        verify_turnstile(payload.turnstile_token, client_ip)
    except TurnstileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        username = validate_username(payload.username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid username or password") from exc
    password = str(payload.password or "")
    if len(password) > 256:
        raise HTTPException(status_code=400, detail="invalid username or password")
    if not login_allowed(client_ip, username):
        raise HTTPException(status_code=429, detail="too many login attempts; try again later")
    if not verify_login(username, password):
        record_login_failure(client_ip, username)
        raise HTTPException(status_code=401, detail="invalid username or password")
    clear_login_failures(client_ip, username)
    migration = migrate_browser_owner_to_user(request.headers.get("x-ai-option-user"), username)
    is_https = _request_is_https(request)
    response.set_cookie(COOKIE_NAME, create_session_token(username), **cookie_options(is_https))
    response.set_cookie(
        "ai_option_route",
        _route_mode(payload.route_mode),
        max_age=60 * 60 * 24 * 365,
        path="/",
        secure=is_https,
        httponly=False,
        samesite="lax",
    )
    return {"authenticated": True, "username": username, "auth_enabled": True, "broker_api_enabled": _broker_api_enabled(), "owner_migration": migration, **auth_user_permissions(username)}


@app.post("/api/auth/logout")
def auth_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(COOKIE_NAME, path="/")
    response.delete_cookie("ai_option_route", path="/")
    return {"authenticated": False}


@app.get("/api/auth/oauth/config")
def auth_oauth_config() -> dict[str, Any]:
    """Public: which Sign-in-with providers are configured (for login buttons)."""
    return oauth_public_config()


@app.get("/api/auth/turnstile/config")
def auth_turnstile_config() -> dict[str, Any]:
    """Public: whether the Cloudflare Turnstile widget should render, plus its
    site key.  Disabled (no key) means the login page submits without a token."""
    return turnstile_public_config()


def _resolve_oauth_username(identity: dict[str, Any]) -> str:
    """Map a verified OAuth identity to an app username, provisioning if needed.

    Order: (1) an existing binding on (provider, sub); (2) a verified email that
    already matches an app_users row — the provider has proven ownership, so we
    adopt and bind it; (3) otherwise provision a fresh trial account.  The chosen
    username is always (re)bound to the identity so future logins hit case (1)."""
    provider = identity["provider"]
    sub = identity["sub"]
    email = identity.get("email") or ""

    username = find_username_by_identity(provider, sub)
    if username is None:
        if not identity.get("email_verified") or not email:
            raise HTTPException(status_code=400, detail="provider did not supply a verified email")
        try:
            candidate = validate_username(email)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="email is not a usable account name") from exc
        existing = get_auth_user(candidate)
        username = existing.username if existing is not None else provision_oauth_user(email).username
    link_identity(provider, sub, username, email)
    return username


@app.post("/api/auth/oauth/login")
def auth_oauth_login(request: Request, response: Response, payload: OAuthLoginRequest) -> dict[str, Any]:
    if not auth_enabled():
        return {"authenticated": True, "username": "local", "auth_enabled": False, "can_trade": True, "is_admin": True, "broker_api_enabled": _broker_api_enabled()}
    if not payload.accepted_terms:
        raise HTTPException(status_code=400, detail="terms acceptance required")
    client_ip = request.client.host if request.client else "unknown"
    try:
        verify_turnstile(payload.turnstile_token, client_ip)
    except TurnstileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        provider = normalize_provider(payload.provider)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rate_key = f"oauth:{provider}"
    if not login_allowed(client_ip, rate_key):
        raise HTTPException(status_code=429, detail="too many login attempts; try again later")
    try:
        identity = verify_id_token(provider, payload.credential, payload.nonce)
    except OAuthError as exc:
        record_login_failure(client_ip, rate_key)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    clear_login_failures(client_ip, rate_key)
    username = _resolve_oauth_username(identity)
    migration = migrate_browser_owner_to_user(request.headers.get("x-ai-option-user"), username)
    _issue_session_cookies(request, response, username, payload.route_mode)
    return {
        "authenticated": True,
        "username": username,
        "auth_enabled": True,
        "broker_api_enabled": _broker_api_enabled(),
        "owner_migration": migration,
        **auth_user_permissions(username),
    }


@app.get("/api/auth/oauth/links")
def auth_oauth_links(request: Request) -> dict[str, Any]:
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    return {
        "identities": list_identities_for_user(username),
        "has_password": user_has_password(username),
    }


@app.post("/api/auth/oauth/links")
def auth_oauth_link(request: Request, payload: OAuthLinkRequest) -> dict[str, Any]:
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        provider = normalize_provider(payload.provider)
        identity = verify_id_token(provider, payload.credential, payload.nonce)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    existing_owner = find_username_by_identity(provider, identity["sub"])
    if existing_owner is not None and existing_owner != username:
        raise HTTPException(status_code=409, detail="this account is already linked to another user")
    link_identity(provider, identity["sub"], username, identity.get("email"))
    return {"identities": list_identities_for_user(username), "has_password": user_has_password(username)}


@app.delete("/api/auth/oauth/links/{provider}")
def auth_oauth_unlink(request: Request, provider: str) -> dict[str, Any]:
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        provider = normalize_provider(provider)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Lock-out guard: refuse to remove the last login method on an account that
    # has no usable password.
    if not user_has_password(username) and count_identities_for_user(username) <= 1:
        raise HTTPException(status_code=400, detail="cannot remove the only sign-in method; set a password first")
    unlink_identity(provider, username)
    return {"identities": list_identities_for_user(username), "has_password": user_has_password(username)}


def _issue_session_cookies(request: Request, response: Response, username: str, route_mode: str) -> None:
    is_https = _request_is_https(request)
    response.set_cookie(COOKIE_NAME, create_session_token(username), **cookie_options(is_https))
    response.set_cookie(
        "ai_option_route",
        _route_mode(route_mode),
        max_age=60 * 60 * 24 * 365,
        path="/",
        secure=is_https,
        httponly=False,
        samesite="lax",
    )


def _current_username(request: Request) -> str | None:
    if not auth_enabled():
        return "local"
    return verify_session_token(request.cookies.get(COOKIE_NAME))


def _override_owner_header(request: Request, username: str) -> None:
    owner = normalize_owner_id(username)
    headers = [(key, value) for key, value in request.scope.get("headers", []) if key.lower() != b"x-ai-option-user"]
    headers.append((b"x-ai-option-user", owner.encode("utf-8")))
    request.scope["headers"] = headers


def _require_admin(request: Request) -> str:
    username = _current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="authentication required")
    permissions = auth_user_permissions(username)
    if not permissions["is_admin"]:
        raise HTTPException(status_code=403, detail="admin permission required")
    return username


ANALYZE_ROUTE_PREFIXES = (
    "/api/scan",
    "/api/scans",
    "/api/scan-marks",
    "/api/watchlists",
    "/api/scan-triggers",
    "/api/scan-loop",
    "/api/scan-loop-instances",
    "/api/notification",
    "/api/opportunities",
    "/api/user-providers",
)


def _can_analyze_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in ANALYZE_ROUTE_PREFIXES)


def _resource_count(owner: str, name: str) -> int:
    try:
        from .time_utils import EASTERN

        with connect() as db:
            if name in ("daily_scans", "daily_ai_scans", "daily_ai_chat"):
                day_start = datetime.now(EASTERN).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            if name == "daily_scans":
                row = db.execute(
                    "SELECT COUNT(*) AS count FROM scan_runs WHERE owner_id = ? AND created_at >= ?",
                    (owner, day_start),
                ).fetchone()
            elif name == "daily_ai_scans":
                row = db.execute(
                    "SELECT COUNT(*) AS count FROM scan_runs WHERE owner_id = ? AND created_at >= ? AND use_ai = 1",
                    (owner, day_start),
                ).fetchone()
            elif name == "daily_ai_chat":
                row = db.execute(
                    "SELECT COUNT(*) AS count FROM chat_messages WHERE owner_id = ? AND role = 'user' AND created_at >= ?",
                    (owner, day_start),
                ).fetchone()
            elif name == "watchlists":
                row = db.execute("SELECT COUNT(*) AS count FROM watchlists WHERE owner_id = ?", (owner,)).fetchone()
            elif name == "scan_loop_instances":
                row = db.execute("SELECT COUNT(*) AS count FROM scan_loop_instances WHERE owner_id = ?", (owner,)).fetchone()
            elif name == "notification_channels":
                row = db.execute("SELECT COUNT(*) AS count FROM notification_channels WHERE owner_id = ?", (owner,)).fetchone()
            elif name == "longbridge_accounts":
                row = db.execute("SELECT COUNT(*) AS count FROM longbridge_accounts WHERE owner_id = ?", (owner,)).fetchone()
            else:
                return 0
    except Exception:
        return 0
    if row is None:
        return 0
    try:
        return int(row["count"])
    except Exception:
        return int(row[0] or 0)


def _enforce_user_limit(request: Request, owner: str, resource_name: str) -> None:
    username = _current_username(request)
    if not username or not auth_enabled():
        return
    permissions = auth_user_permissions(username)
    limits = permissions.get("limits") or {}
    limit = int(limits.get(f"max_{resource_name}", -1))
    if limit < 0:
        return
    used = _resource_count(owner, resource_name)
    if used >= limit:
        raise HTTPException(status_code=403, detail=f"{resource_name.replace('_', ' ')} limit reached")


def _auth_user_limit_payload(payload: AuthUserCreateRequest | AuthUserUpdateRequest, *, partial: bool = False) -> dict[str, int | None]:
    fields = (
        "max_daily_scans",
        "max_daily_ai_scans",
        "max_daily_ai_chat",
        "max_watchlists",
        "max_scan_loop_instances",
        "max_notification_channels",
        "max_longbridge_accounts",
    )
    values: dict[str, int | None] = {}
    for field in fields:
        value = getattr(payload, field)
        if value is None and partial:
            continue
        values[field] = value
    return values


def _server_health_snapshot() -> dict[str, Any]:
    db_status = _db_health()
    redis_ok = redis_available()
    redis_latency_ms = _redis_latency_ms()
    queue_snapshot = _queue_snapshot(redis_ok=redis_ok)
    runtime_snapshot = order_monitor_runtime_snapshot()
    trading_counts = trading_runtime_counts()
    schedule_snapshot = schedule_runtime_snapshot()
    scheduler = scheduler_status()
    return {
        "status": "ok" if db_status["ok"] and redis_ok else "degraded",
        "generated_at_et": now_et_iso(),
        "app": {
            "pid": os.getpid(),
            "process_role": process_role(),
            "web_enabled": web_enabled(),
            "worker_enabled": worker_enabled(),
            "uptime_seconds": round(time.monotonic() - APP_STARTED_AT, 1),
            "broker_api_enabled": _broker_api_enabled(),
        },
        "database": db_status,
        "database_pool": db_pool_snapshot(),
        "redis": {
            "ok": redis_ok,
            "enabled": bool(os.getenv("AI_OPTION_REDIS_URL") or os.getenv("REDIS_URL")),
            "latency_ms": redis_latency_ms,
            "availability_pct": 100 if redis_ok else 0,
            "pool": redis_pool_snapshot(),
        },
        "scheduler": scheduler,
        "order_monitor": {
            "enabled": _env_bool("AI_OPTION_ENABLE_ORDER_MONITOR", False),
            "poll_seconds": 20,
            "status": runtime_snapshot.get("status"),
            "last_run_at": runtime_snapshot.get("finished_at"),
            "lag_seconds": runtime_snapshot.get("lag_seconds"),
            "running": runtime_snapshot.get("running"),
        },
        "trading": trading_counts,
        "schedule": schedule_snapshot,
        "runtime": queue_snapshot,
        "longbridge_sdk": sdk_pool_snapshot(),
    }


def _redis_latency_ms() -> float | None:
    if not redis_available():
        return None
    started = time.monotonic()
    try:
        from .redis_runtime import redis_client
        client = redis_client()
        if client is None:
            return None
        client.ping()
        return round((time.monotonic() - started) * 1000, 2)
    except Exception:
        return None


def _queue_snapshot(redis_ok: bool | None = None) -> dict[str, Any]:
    redis_ready = redis_ok if redis_ok is not None else redis_available()
    runtime: dict[str, Any] = {
        "scan_queue_backlog": redis_llen(SCAN_QUEUE_KEY) if redis_ready else None,
        "scan_queue_source": "redis" if redis_ready else "local",
        "trading_queued": 0,
        "trading_running": 0,
        "notification_queued": 0,
        "notification_failed": 0,
        "recent_failure_reasons": [],
    }
    try:
        with connect() as db:
            trading_counts = db.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running
                FROM trading_runs
                """,
            ).fetchone()
            runtime["trading_queued"] = int((trading_counts["queued"] if trading_counts else 0) or 0)
            runtime["trading_running"] = int((trading_counts["running"] if trading_counts else 0) or 0)
            try:
                notification_counts = db.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                    FROM notification_events
                    """
                ).fetchone()
                runtime["notification_queued"] = int((notification_counts["queued"] if notification_counts else 0) or 0)
                runtime["notification_failed"] = int((notification_counts["failed"] if notification_counts else 0) or 0)
            except Exception:
                pass
            rows = db.execute(
                """
                SELECT error, instance_json, orders_json, created_at
                FROM trading_runs
                WHERE status = 'failed' OR error IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 50
                """
            ).fetchall()
        reasons = Counter()
        for row in rows:
            error = str(row["error"] or "").strip()
            if error:
                reasons[_normalize_failure_reason(error)] += 1
            instance = _loads_json(row["instance_json"])
            if isinstance(instance, dict):
                for event in reversed(instance.get("event_timeline") or []):
                    if not isinstance(event, dict):
                        continue
                    message = str(event.get("message") or event.get("event_type") or "").strip()
                    status = str(event.get("status") or "").lower()
                    if status == "error" or "failed" in message.lower():
                        reasons[_normalize_failure_reason(message)] += 1
                        break
            orders = _loads_json(row["orders_json"])
            if isinstance(orders, list):
                for order in orders:
                    if not isinstance(order, dict):
                        continue
                    for key in ("error", "monitor_error", "stop_error", "software_stop_error", "software_take_profit_error", "single_leg_smart_exit_error"):
                        value = str(order.get(key) or "").strip()
                        if value:
                            reasons[_normalize_failure_reason(value)] += 1
                            break
        runtime["recent_failure_reasons"] = [
            {"reason": reason, "count": count} for reason, count in reasons.most_common(5)
        ]
    except Exception:
        pass
    return runtime


def _normalize_failure_reason(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return "unknown"
    cleaned = cleaned.replace("\n", " ")
    return cleaned[:120]


def _loads_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return None


def _db_health() -> dict[str, Any]:
    started = time.monotonic()
    try:
        with connect() as db:
            db.execute("SELECT 1").fetchone()
        return {
            "ok": True,
            "backend": database_backend(),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except Exception as exc:  # noqa: BLE001 - admin health should surface backend failures.
        return {
            "ok": False,
            "backend": database_backend(),
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "error": str(exc),
        }


@app.get("/api/market-clock")
def get_market_clock() -> dict[str, Any]:
    return market_clock()


@app.get("/api/market-environment")
def get_market_environment() -> dict[str, Any]:
    return market_environment()


@app.get("/api/trading/readiness")
def trading_readiness(owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    config = get_trading_config(owner)
    preview = next_config_run_preview(config)
    schedule_preview = _schedule_preview(owner, config)
    try:
        readiness = validate_trading_readiness(owner, config, require_ai=False, force_session_check=False)
    except ValueError as exc:
        readiness = {
            "ok": False,
            "issues": [str(exc)],
            "warnings": [],
            "account_name": config.get("longbridge_account"),
            "session": {"token": "invalid"},
            "config": config,
        }
    return {
        "now_et": now_et_iso(),
        "market_clock": market_clock(),
        "scheduler": scheduler_status(),
        **preview,
        "schedule_preview": schedule_preview,
        "readiness": readiness,
    }


def _schedule_preview(owner_id: str, config: dict[str, Any]) -> dict[str, Any]:
    today_et = now_et_iso()[:10]
    if not config.get("multi_instance_enabled"):
        return {
            "enabled": False,
            "profile_id": "single_run",
            "slots": [],
        }
    slots = config.get("schedule_slots") or []
    return {
        "enabled": True,
        "profile_id": str(config.get("schedule_profile") or "multi"),
        "slots": [
            {
                "slot_id": str(slot.get("slot_id") or ""),
                "label": str(slot.get("label") or ""),
                "time_et": str(slot.get("time_et") or ""),
                "action": str(slot.get("action") or ""),
                "strategy_modes": list(slot.get("strategy_modes") or []),
                "capital_pct": float(slot.get("capital_pct") or 0),
                "gate_profile": str(slot.get("gate_profile") or ""),
                "allow_new_positions": bool(slot.get("allow_new_positions", True)),
                "force_no_overnight": bool(slot.get("force_no_overnight", False)),
                "enabled": bool(slot.get("enabled", True)),
            }
            for slot in slots
            if isinstance(slot, dict)
        ],
        "fires_today": list_schedule_fires(owner_id=owner_id, trade_date_et=today_et, limit=10),
    }


def _is_admin_request(request: Request) -> bool:
    username = _current_username(request)
    if not username:
        return False
    return bool(auth_user_permissions(username)["is_admin"])


def _provider_owner(request: Request, owner_id: str | None) -> str:
    username = _current_username(request)
    if username and auth_enabled():
        return normalize_owner_id(username)
    return normalize_owner_id(owner_id or username)


def _request_owner(request: Request, owner_id: str | None) -> str:
    username = _current_username(request)
    if username and auth_enabled():
        return normalize_owner_id(username)
    return normalize_owner_id(owner_id or username)


@app.get("/api/providers")
def list_providers(request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    rows = providers_as_rows(provider_config_path(), include_api_key_env=_is_admin_request(request))
    rows.extend(user_providers_as_rows(_provider_owner(request, owner_id)))
    return rows


@app.get("/api/auth/users")
def list_auth_users(request: Request) -> list[dict[str, Any]]:
    _require_admin(request)
    return auth_users_as_rows()


@app.post("/api/auth/users")
def create_auth_user_route(request: Request, payload: AuthUserCreateRequest) -> list[dict[str, Any]]:
    _require_admin(request)
    try:
        create_auth_user(
            payload.username,
            payload.password,
            can_analyze=payload.can_analyze,
            can_trade=payload.can_trade,
            is_admin=payload.is_admin,
            remaining_days=payload.remaining_days,
            resource_limits=_auth_user_limit_payload(payload),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return auth_users_as_rows()


@app.patch("/api/auth/users/{username}")
def update_auth_user_route(request: Request, username: str, payload: AuthUserUpdateRequest) -> list[dict[str, Any]]:
    _require_admin(request)
    try:
        update_auth_user_permissions(
            username,
            can_analyze=payload.can_analyze,
            can_trade=payload.can_trade,
            is_admin=payload.is_admin,
            remaining_days=payload.remaining_days,
            resource_limits=_auth_user_limit_payload(payload, partial=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return auth_users_as_rows()


@app.delete("/api/auth/users/{username}")
def delete_auth_user_route(request: Request, username: str) -> list[dict[str, Any]]:
    _require_admin(request)
    try:
        delete_auth_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return auth_users_as_rows()


@app.get("/api/thetadata/config")
def get_thetadata_config(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return thetadata_config_status()


@app.put("/api/thetadata/config")
def update_thetadata_config(request: Request, payload: ThetaDataCredentialsRequest) -> dict[str, Any]:
    _require_admin(request)
    try:
        result = save_thetadata_credentials(payload.email, payload.password)
        from .thetadata_option_tool import reset_client

        reset_client()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/thetadata/config")
def remove_thetadata_config(request: Request) -> dict[str, Any]:
    _require_admin(request)
    result = delete_thetadata_credentials()
    from .thetadata_option_tool import reset_client

    reset_client()
    return result


@app.post("/api/thetadata/config/test")
def test_thetadata_config(request: Request, payload: ThetaDataTestRequest) -> dict[str, Any]:
    _require_admin(request)
    symbol = re.sub(r"[^A-Za-z0-9.^-]", "", str(payload.symbol or "SPY").upper())[:24] or "SPY"
    try:
        from .thetadata_option_tool import market_data as theta_market_data

        data = theta_market_data(symbol)
        quote = data.get("quote") if isinstance(data, dict) else {}
        spot = float((quote or {}).get("last") or 0)
        if spot <= 0:
            raise ValueError(f"ThetaData returned no usable quote for {symbol}")
        return {
            "ok": True,
            "symbol": symbol,
            "price": spot,
            "source": str((quote or {}).get("source") or "thetadata"),
            "config": thetadata_config_status(),
        }
    except Exception as exc:  # noqa: BLE001 - return a bounded provider diagnostic to the admin.
        raise HTTPException(status_code=502, detail=f"ThetaData connection failed: {str(exc)[:240]}") from exc


@app.get("/api/longbridge/accounts")
def list_longbridge_accounts(owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return accounts_as_rows(normalize_owner_id(owner_id))


@app.post("/api/longbridge/accounts")
def create_longbridge_account(
    http_request: Request,
    request: AccountRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = _request_owner(http_request, owner_id)
    _enforce_user_limit(http_request, owner, "longbridge_accounts")
    try:
        create_account(
            request.name,
            request.label,
            set_default=request.set_default,
            owner_id=owner,
            app_key=request.app_key,
            app_secret=request.app_secret,
            access_token=request.access_token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return accounts_as_rows(owner)


@app.put("/api/longbridge/accounts/{name}/credentials")
def update_longbridge_account_credentials(
    name: str,
    request: AccountCredentialsRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    try:
        update_account_sdk_credentials(
            name,
            request.app_key,
            request.app_secret,
            request.access_token,
            owner_id=owner,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    auth_manager.status(name, owner_id=owner, force=True)
    return accounts_as_rows(owner)


@app.post("/api/longbridge/accounts/{name}/default")
def make_longbridge_account_default(
    name: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    try:
        set_default_account(name, owner_id=owner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return accounts_as_rows(owner)


@app.delete("/api/longbridge/accounts/{name}")
def remove_longbridge_account(
    name: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    try:
        auth_manager.remove_account(name, owner_id=owner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return accounts_as_rows(owner)


@app.get("/api/longbridge/status")
def longbridge_status(
    account: str | None = None,
    force: bool = False,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        return auth_manager.status(account, owner_id=normalize_owner_id(owner_id), force=force)
    except ValueError as exc:
        raise _account_error(exc) from exc


@app.get("/api/brokers/accounts")
def list_broker_accounts_route(
    broker: str | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    return broker_accounts_as_rows(normalize_owner_id(owner_id), broker=broker)


@app.post("/api/brokers/accounts")
def create_broker_account_route(
    http_request: Request,
    request: BrokerAccountRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = _request_owner(http_request, owner_id)
    _enforce_user_limit(http_request, owner, "longbridge_accounts")
    try:
        if str(request.broker or "").strip().lower() == "usmart":
            create_usmart_account(
                request.name,
                label=request.label,
                channel=request.channel or "",
                sign_private_key=request.sign_private_key or "",
                encrypt_public_key=request.encrypt_public_key or "",
                phone=request.phone or "",
                area_code=request.area_code or "852",
                trade_password=request.trade_password or "",
                paper=request.paper,
                set_default=request.set_default,
                owner_id=owner,
            )
        else:
            create_broker_account(
                request.broker,
                request.name,
                label=request.label,
                api_key=request.api_key or "",
                api_secret=request.api_secret or "",
                paper=request.paper,
                set_default=request.set_default,
                owner_id=owner,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return broker_accounts_as_rows(owner)


@app.post("/api/brokers/accounts/{broker}/{name}/default")
def make_broker_account_default_route(
    broker: str,
    name: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    try:
        set_broker_account_default(broker, name, owner_id=owner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return broker_accounts_as_rows(owner)


@app.delete("/api/brokers/accounts/{broker}/{name}")
def delete_broker_account_route(
    broker: str,
    name: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    try:
        delete_broker_account(broker, name, owner_id=owner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return broker_accounts_as_rows(owner)


@app.post("/api/providers")
def create_provider(http_request: Request, request: ProviderRequest) -> list[dict[str, Any]]:
    _require_admin(http_request)
    add_provider(
        name=request.name,
        base_url=request.base_url,
        model=request.model,
        api_key_env=request.api_key_env,
        temperature=request.temperature,
        provider_type=request.provider_type,
        path=provider_config_path(),
    )
    return providers_as_rows(provider_config_path(), include_api_key_env=True)


@app.delete("/api/providers/{name}")
def remove_provider(http_request: Request, name: str) -> list[dict[str, Any]]:
    _require_admin(http_request)
    try:
        delete_provider(name, provider_config_path())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return providers_as_rows(provider_config_path(), include_api_key_env=True)


@app.post("/api/user-providers")
def create_user_provider(
    http_request: Request,
    request: UserProviderRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    try:
        return upsert_user_provider(
            _provider_owner(http_request, owner_id),
            name=request.name,
            label=request.label,
            base_url=request.base_url,
            model=request.model,
            api_key=request.api_key,
            temperature=request.temperature,
            provider_type=request.provider_type,
            is_default=request.is_default,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/user-providers/{name}")
def remove_user_provider(
    http_request: Request,
    name: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    try:
        return delete_user_provider(_provider_owner(http_request, owner_id), name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


CHAT_SYSTEM_PROMPT = """\
You are an AI trading analyst assistant embedded in the AI Option Scanner platform.
Your users are options traders who ask you about market analysis, strategy advice, and trade ideas.

CRITICAL RULES — follow strictly:
1. ALL price levels, support/resistance, and numerical data MUST come from the provided "实时市场数据" section below.
   If the data shows SPY at 715, you MUST use numbers near 715 — NEVER invent prices like 570-585.
2. Begin your response by citing the real-time data so the user knows you used it:
   e.g. "根据实时数据，SPY 当前报 xxx，近15日区间 xxx–xxx"
3. If no market data is provided for a symbol, state clearly that real-time data is unavailable for that ticker.
4. LANGUAGE: Reply in the SAME language the user wrote their question in.
   If the user asks in English, answer entirely in English; if in Chinese,
   answer in Chinese. Do NOT switch languages regardless of the data language.
5. Use Markdown: **bold**, `code`, bullet lists, headings. Keep responses under 800 words.
6. Be concise and actionable. Give bull / bear / neutral multi-directional analysis.
7. You can reference indicators: VWAP, ORB, EMA, MACD, RVOL, GEX, Delta, Theta, IV.
8. Use the "当前日期与交易日" section as the source of truth for today's date and
   trading status. Compute days-to-expiry from that date — NEVER guess the date.
9. The option chain block reports which expiration was actually analyzed
   (分析到期). If the user asked for a specific expiry that is not listed, you
   analyzed the closest available one — say so and list 其它可用到期日.
10. Whenever you name or recommend a specific option contract, you MUST state its
    full expiration date (e.g. 2026-06-20) and strike — never give a contract pick
    without an explicit expiry. Use the "分析到期" date from the option chain block;
    if that date is unavailable, say the expiry is unavailable rather than omitting it.
11. When an "IV Skew" table is present, use the per-strike call/put IV and OI to
    judge real mispricing (skew steepness, put/call IV gap, OI concentration).
    Do NOT fabricate strike-level IV that is not in the data; if a strike's IV is
    "-" it was unavailable.
12. If any "数据可用性约束" line says option quote data is unavailable (权限/限流/错误),
    you MUST NOT output specific strike-level "被低估/被高估" conclusions or concrete
    contract picks. Explain the limitation first, then provide only conditional
    framework-level analysis and what data must be restored.
"""

# Catalog of investigation tools the chat assistant can run before answering.
# `requires_account` means the tool needs a configured Longbridge SDK account.
CHAT_TOOLS: list[dict[str, Any]] = [
    {"id": "longbridge_quote", "label": "Longbridge 实时报价", "description": "实时最新价、涨跌幅", "requires_account": True},
    {"id": "longbridge_kline", "label": "Longbridge 日K线", "description": "近15日区间与均价", "requires_account": True},
    {"id": "thetadata_stock_market", "label": "ThetaData 正股行情", "description": "实时快照、分钟线和日线", "requires_account": False},
    {"id": "yfinance_market", "label": "YFinance 行情", "description": "备用报价与日K线", "requires_account": False},
    {"id": "gex_snapshot", "label": "GEX / Gamma 暴露", "description": "Gamma 暴露、Gamma Flip、Call/Put Wall", "requires_account": False},
    {"id": "lb_option_chain", "label": "Longbridge 期权链", "description": "指定/最近到期的 OI、PCR、ATM 隐波、BSM Greeks 与 IV Skew", "requires_account": True},
    {"id": "thetadata_option_chain", "label": "ThetaData 期权链", "description": "ThetaData 期权 OI/IV/Greeks/GEX；周末或休市自动回退到上一交易日 EOD", "requires_account": False},
]
_CHAT_TOOL_IDS = {tool["id"] for tool in CHAT_TOOLS}

_STOCK_SYMBOL_RE = re.compile(r'''(?:^|[\s,，。；;：:！!？?、()（）""''""「」【】《》\-—]|(?<=[^\x00-\x7F]))[A-Z]{1,5}(?:\.[A-Z]{2})?(?![A-Za-z])''')

_SKIP_WORDS = {
    'CALL','PUT','ETF','USD','HKD','CNY','NYSE','NASDAQ','SPX','NDX','VIX','DOW','DJIA',
    'A','I','O','AT','TO','BY','OR','IN','AN','IT','SO','NO','GO','DO','WE','ME','HE','US',
    'BE','UP','ON','IF','IS','AM','PM','CEO','CFO','IPO','GDP','CPI','FOMC','YTD',
    # Option / market jargon that is uppercase but never a ticker we want to scan.
    'GEX','IV','OI','PCR','ATM','OTM','ITM','DTE','RVOL','HV','RV','BSM','VWAP','RSI',
    'MACD','EMA','SMA','PE','PB','EPS','ROE','ROI','TP','SL','AI','API','SDK','FAQ',
    'GAMMA','DELTA','THETA','VEGA','RHO','SKEW',
    'THE','AND','FOR','ARE','NOT','HAS','CAN','WAS','HAD','ITS','NEW','ALL','ONE','OUR',
    'OUT','HIS','HER','THEM','THEY','WILL','WOULD','COULD','SHOULD','FROM','WITH','THIS',
    'THAT','THAN','THEN','WHEN','WHAT','WHICH','THERE','THEIR','ABOUT','AFTER','BEFORE',
    # Common English request verbs/words that are not tickers. Without these the
    # regex extractor mis-reads "study on SPY" → "STUDY", driving bad lookups.
    'STUDY','SCAN','FIND','LOOK','SHOW','CHECK','ANALYZE','ANALYSE','REVIEW','HELP',
    'GIVE','TELL','WANT','NEED','MAKE','TAKE','PICK','PLAN','TRADE','BUY','SELL','HOLD',
    'LONG','SHORT','NEAR','FAR','CHEAP','RISK','PLEASE','ABOUT','SOME','ANY','MY','ME',
    'NOW','TODAY','SOON','GOOD','BEST','HOW','WHY','WHO','LET','SEE','GET','USE','TRY',
    'OPTION','OPTIONS','STOCK','PRICE','CHART','LEAP','LEAPS','DAYS','DAY','WEEK','MONTH',
    'OPEN','CLOSE','HIGH','LOW','CHAIN','DATA','VOL','BULL','BEAR','OK','YES','RUN',
}


def _clean_chat_symbol(value: Any) -> str | None:
    """Normalize an LLM- or regex-provided ticker candidate; drop obvious
    non-tickers (stopwords, wrong shape). Keeps an optional .XX market suffix."""
    text = str(value or "").upper().strip()
    text = text.split(".")[0] if text.endswith(".US") else text
    if not re.fullmatch(r"[A-Z]{1,5}", text):
        return None
    if text in _SKIP_WORDS or len(text) <= 1:
        return None
    return text


def _extract_symbols(text: str) -> list[str]:
    """Extract US stock tickers from text, even when adjacent to CJK characters."""
    seen = set()
    result = []
    upper = text.upper()
    # Primary: CJK-safe regex with lookbehind
    for m in _STOCK_SYMBOL_RE.finditer(upper):
        sym = m.group(0).lstrip(' -（(【《""''""—').strip()
        # Strip any leading non-ASCII (from lookbehind not consuming)
        sym = ''.join(c for c in sym if c.isascii())
        if sym in _SKIP_WORDS or len(sym) <= 1:
            continue
        if sym not in seen:
            seen.add(sym)
            result.append(sym)
    # Fallback: standalone all-caps words
    if not result:
        for m in re.finditer(r'\b[A-Z]{1,5}\b', upper):
            sym = m.group(0)
            if sym in _SKIP_WORDS or len(sym) <= 1:
                continue
            if sym not in seen:
                seen.add(sym)
                result.append(sym)
    return result[:5]


_CN_MONTHS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
_CN_DAYS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "十六": 16, "十七": 17, "十八": 18,
    "十九": 19, "二十": 20, "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24, "二十五": 25,
    "二十六": 26, "二十七": 27, "二十八": 28, "二十九": 29, "三十": 30, "三十一": 31,
}


def _reply_language_directive(text: str) -> str:
    """Build an explicit reply-language instruction based on the language the
    user actually wrote in. The chat data/prompt is heavily Chinese, which biases
    the model toward Chinese answers even for English questions — so we detect CJK
    in the question itself and pin the answer language to match it."""
    has_cjk = bool(re.search(r"[一-鿿]", text or ""))
    if has_cjk:
        return "\n\n=== 回答语言 ===\n用户用中文提问，请用简体中文回答。"
    return (
        "\n\n=== Reply language ===\n"
        "The user asked in English. Reply ENTIRELY in English, even though the "
        "market data and tool output below are labelled in Chinese. Translate any "
        "data labels you cite into English."
    )


def _extract_requested_expiry(text: str) -> str | None:
    """Detect a specific option expiration date in the user's message (ISO or Chinese)."""
    if not text:
        return None
    # ISO-like: 2026-06-09, 2026/6/9
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    # Chinese: 6月9日 / 6月9号 / 六月九日 (year optional)
    m = re.search(r"(?:(20\d{2})\s*年)?\s*([0-9]{1,2}|[一二三四五六七八九十]+)\s*月\s*([0-9]{1,2}|[一二三四五六七八九十]+)\s*[日号]", text)
    if m:
        year_str, mo_str, d_str = m.group(1), m.group(2), m.group(3)
        mo = int(mo_str) if mo_str.isdigit() else _CN_MONTHS.get(mo_str)
        d = int(d_str) if d_str.isdigit() else _CN_DAYS.get(d_str)
        if not mo or not d:
            return None
        year = int(year_str) if year_str else et_today().year
        try:
            target = date(year, mo, d)
        except ValueError:
            return None
        # No explicit year: roll to next occurrence if the date already passed.
        if not year_str and target < et_today():
            try:
                target = date(year + 1, mo, d)
            except ValueError:
                return None
        return target.isoformat()
    return None


def _format_trace(kind: str, data: dict[str, Any]) -> str:
    return json.dumps({"type": "trace", "kind": kind, "data": data}, ensure_ascii=False)


def _format_token(token: str) -> str:
    return json.dumps({"type": "token", "text": token}, ensure_ascii=False)


def _format_done() -> str:
    return json.dumps({"type": "done"}, ensure_ascii=False)



from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
import threading

# Max seconds to wait for per-symbol investigation tools before starting the AI answer.
_CHAT_TOOL_DEADLINE = float(os.getenv("AI_OPTION_CHAT_TOOL_DEADLINE", "15"))
# Heartbeat interval (seconds) for SSE keep-alive comments during long model
# "thinking" gaps. Keeps proxies / load balancers from dropping idle connections.
_CHAT_HEARTBEAT_INTERVAL = float(os.getenv("AI_OPTION_CHAT_HEARTBEAT_INTERVAL", "12"))

# Agentic chat: when enabled (default on), the chat turn lets the LLM decide
# which tools to call with which arguments (symbol, DTE window, expiry) via an
# OpenAI tool-calling loop, instead of the regex-extract + fetch-all pipeline.
# Falls back to the legacy pipeline when the provider lacks tool support or
# errors on the tools schema. Kill-switch: set to a falsy value.
_CHAT_AGENTIC_ENABLED = (os.getenv("AI_OPTION_CHAT_AGENTIC", "true") or "").strip().lower() in {"1", "true", "yes", "on"}
# Max tool-calling hops before forcing a final answer (bounds latency/cost).
_CHAT_AGENT_MAX_HOPS = max(1, int(os.getenv("AI_OPTION_CHAT_AGENT_MAX_HOPS", "4") or 4))
# Max seconds to wait for a single agent-selected blocking tool before
# continuing with the data already collected.
_CHAT_AGENT_TOOL_DEADLINE = max(1.0, float(os.getenv("AI_OPTION_CHAT_AGENT_TOOL_DEADLINE", "30") or 30))

# How many of the nearest expirations the chat option-chain summary pulls.
_LB_CHAIN_EXPIRATIONS = max(1, int(os.getenv("AI_OPTION_LB_CHAIN_EXPIRATIONS", "5") or 5))
# Half-width (in strikes) of the at-the-money window used for live OI/IV quotes,
# keeping the option_quote volume bounded so the chat deadline is respected.
_LB_CHAIN_ATM_STRIKES = max(2, int(os.getenv("AI_OPTION_LB_CHAIN_ATM_STRIKES", "8") or 8))


def _run_tool_longbridge_quote(sym, lb_sym, lb_account_name):
    trace = {"symbol": lb_sym, "status": "fetching", "tool": "longbridge_quote"}
    ctx = ""
    spot = 0.0
    ok = False
    try:
        q = longbridge_quote(lb_sym, account_name=lb_account_name)
        if q and (q.get("last_done") or q.get("last_price")):
            price = q.get("last_done") or q.get("last_price") or "N/A"
            spot = float(price)
            change = q.get("change") or q.get("change_rate") or "N/A"
            name = q.get("name_cn") or q.get("name") or sym
            ctx = f"\n**{sym}** ({name}): 最新价={price}, 涨跌={change} [Longbridge]"
            trace["status"] = "done"
            trace["result"] = {"price": price, "change": change, "name": name, "source": "longbridge"}
            ok = True
        else:
            trace["status"] = "empty"
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = str(exc)
    return ("quote", sym, ctx, ok, spot, trace)


def _run_tool_longbridge_kline(sym, lb_sym, lb_account_name):
    trace = {"symbol": lb_sym, "status": "fetching", "tool": "longbridge_kline"}
    ctx = ""
    ok = False
    try:
        k = longbridge_kline(lb_sym, count=15, account_name=lb_account_name)
        if k and len(k) >= 3:
            closes = [float(bar.get("close", 0)) for bar in k if bar.get("close")]
            if closes:
                ma5 = sum(closes[-5:]) / min(5, len(closes[-5:]))
                high_15 = max(float(bar.get("high", 0)) for bar in k)
                low_15 = min(float(bar.get("low", float("inf"))) for bar in k)
                ctx = f"\n  近15日K线(Longbridge): 区间 {low_15:.2f} – {high_15:.2f}, 5日均价≈{ma5:.2f}"
                trace["status"] = "done"
                trace["result"] = {"high_15d": round(high_15, 2), "low_15d": round(low_15, 2), "ma5": round(ma5, 2), "bars": len(k)}
                ok = True
            else:
                trace["status"] = "empty"
        else:
            trace["status"] = "empty"
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = str(exc)
    return ("kline", sym, ctx, ok, 0.0, trace)


def _run_tool_yfinance_market(sym):
    trace = {"symbol": sym, "status": "fetching", "tool": "yfinance_market_data"}
    ctx = ""
    try:
        yf = yf_market_data(sym, daily_count=15)
        if yf.get("daily") or yf.get("quote"):
            if yf.get("quote", {}).get("last"):
                ctx += f"\n**{sym}** (YFinance): 最新价={yf['quote']['last']}"
            daily = yf.get("daily") or []
            if daily and len(daily) >= 3:
                closes = [float(bar.get("close", 0)) for bar in daily if bar.get("close")]
                if closes:
                    ma5 = sum(closes[-5:]) / min(5, len(closes[-5:]))
                    highs = [float(bar.get("high", 0)) for bar in daily if bar.get("high")]
                    lows = [float(bar.get("low", float("inf"))) for bar in daily if bar.get("low")]
                    high_15 = max(highs) if highs else 0
                    low_15 = min(lows) if lows else 0
                    ctx += f"\n  近15日K线(YFinance): 区间 {low_15:.2f} – {high_15:.2f}, 5日均价≈{ma5:.2f}"
            trace["status"] = "done"
            trace["result"] = {"bars": len(daily), "source": "yfinance"}
        else:
            trace["status"] = "empty"
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = str(exc)
    return ("yf_market", sym, ctx, False, 0.0, trace)


def _run_tool_thetadata_stock_market(sym):
    trace = {"symbol": sym, "status": "fetching", "tool": "thetadata_stock_market"}
    ctx = ""
    spot = 0.0
    try:
        from .thetadata_option_tool import market_data as theta_market_data

        data = theta_market_data(sym, daily_count=15)
        quote = data.get("quote") or {}
        daily = data.get("daily") or []
        spot = float(quote.get("last") or quote.get("price") or 0)
        if not spot and not daily:
            trace["status"] = "empty"
            return ("theta_market", sym, ctx, False, spot, trace)
        if spot:
            ctx += f"\n**{sym}** (ThetaData): 最新价={spot:.2f}"
        if len(daily) >= 3:
            closes = [float(bar.get("close", 0)) for bar in daily if bar.get("close")]
            highs = [float(bar.get("high", 0)) for bar in daily if bar.get("high")]
            lows = [float(bar.get("low", float("inf"))) for bar in daily if bar.get("low")]
            if closes and highs and lows:
                ma5 = sum(closes[-5:]) / min(5, len(closes[-5:]))
                ctx += f"\n  近15日K线(ThetaData): 区间 {min(lows):.2f} – {max(highs):.2f}, 5日均价≈{ma5:.2f}"
        trace["status"] = "done"
        trace["result"] = {"price": spot or None, "bars": len(daily), "source": "thetadata"}
        return ("theta_market", sym, ctx, True, spot, trace)
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = str(exc)
        return ("theta_market", sym, ctx, False, 0.0, trace)


def _run_tool_gex(sym, lb_sym, spot, provider_owner):
    trace = {"symbol": sym, "status": "fetching", "tool": "gex_snapshot"}
    ctx = ""
    # Prefer paid/live vendors for chat; yfinance is delayed and only a fallback.
    raw_sources = os.getenv("AI_OPTION_CHAT_GEX_SOURCE_ORDER", "thetadata,longbridge,yfinance")
    sources = [item.strip().lower() for item in raw_sources.split(",") if item.strip()]
    if not sources:
        sources = ["thetadata", "longbridge", "yfinance"]
    per_source_timeout = float(os.getenv("AI_OPTION_CHAT_GEX_SOURCE_TIMEOUT", "6"))
    max_days = max(int(os.getenv("AI_OPTION_CHAT_GEX_MAX_DAYS", "60") or 60), 1)
    try:
        gex = {"available": False}
        last_error = ""
        for source in sources:
            gs = lb_sym if source == "longbridge" else sym
            pool = ThreadPoolExecutor(max_workers=1)
            fut = pool.submit(
                fetch_gex,
                gs,
                source,
                spot if spot > 0 else None,
                owner_id=provider_owner,
                max_days=max_days,
            )
            try:
                candidate = fut.result(timeout=per_source_timeout)
            except FuturesTimeoutError:
                last_error = f"{source}: 超时(>{per_source_timeout:g}s)"
                pool.shutdown(wait=False, cancel_futures=True)
                continue
            except Exception as exc:
                last_error = f"{source}: {str(exc)[:80]}"
                pool.shutdown(wait=False, cancel_futures=True)
                continue
            pool.shutdown(wait=False, cancel_futures=True)
            if candidate.get("available"):
                gex = candidate
                break
            if candidate.get("error"):
                last_error = f"{source}: {str(candidate['error'])[:80]}"
        if gex.get("available"):
            parts = []
            if gex.get("gex_per_1pct"):
                parts.append(f"GEX/1%={gex['gex_per_1pct']:,.0f}")
            elif gex.get("net_gex") is not None:
                parts.append(f"NetGEX={gex['net_gex']:,.0f}")
            if gex.get("regime"):
                parts.append(f"regime={gex['regime']}")
            if gex.get("gamma_flip"):
                parts.append(f"GammaFlip≈{gex['gamma_flip']}")
            if gex.get("call_wall"):
                parts.append(f"CallWall={gex['call_wall']}")
            if gex.get("put_wall"):
                parts.append(f"PutWall={gex['put_wall']}")
            if parts:
                ctx = f"\n  Gamma暴露({gex.get('source','?')}): {', '.join(parts)}"
            trace["status"] = "done"
            trace["result"] = {
                k: gex.get(k)
                for k in ("gex_per_1pct", "net_gex", "regime", "call_wall", "put_wall", "gamma_flip", "source")
                if gex.get(k) is not None
            }
        else:
            trace["status"] = "empty"
            note = f"GEX 不可用 (已尝试 {'/'.join(sources)})"
            if last_error:
                note += f"；最后错误 {last_error}"
            trace["result"] = {"note": note}
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = str(exc)
    return ("gex", sym, ctx, False, 0.0, trace)


def _run_tool_lb_chain(sym, lb_sym, lb_account_name, spot=0.0, requested_expiry=None):
    trace = {"symbol": lb_sym, "status": "fetching", "tool": "lb_option_chain_summary"}
    ctx = ""
    try:
        data = _lb_option_chain_summary(lb_sym, lb_account_name, spot, requested_expiry)
        if data:
            parts = []
            exp = data.get("analyzed_expiry") or data.get("nearest_expiry")
            if exp:
                dte = data.get("days_to_expiry")
                dte_txt = f"剩余{dte}天" if isinstance(dte, int) else ""
                tag = "(指定)" if data.get("expiry_matched_request") else ""
                parts.append(f"分析到期={exp}{tag} {dte_txt}".strip())
            if data.get("total_expirations"):
                parts.append(f"可用到期日数={data['total_expirations']}")
            if data.get("atm_strike"):
                parts.append(f"ATM行权={data['atm_strike']:g}")
            if data.get("atm_window_oi"):
                parts.append(f"ATM区OI≈{data['atm_window_oi']:,}")
            if data.get("pcr") is not None:
                parts.append(f"PCR={data['pcr']:.2f}")
            if data.get("atm_iv") is not None:
                parts.append(f"ATM隐波={data['atm_iv'] * 100:.1f}%")
            greeks = data.get("atm_greeks") or {}
            call_g = greeks.get("call")
            if call_g:
                parts.append(
                    f"ATM Call δ={call_g['delta']:.2f} Γ={call_g['gamma']:.4f} Θ={call_g['theta_per_day']:.3f}/天"
                )
            put_g = greeks.get("put")
            if put_g:
                parts.append(
                    f"ATM Put δ={put_g['delta']:.2f} Θ={put_g['theta_per_day']:.3f}/天"
                )
            gex = data.get("near_term_gex") or {}
            if gex.get("available"):
                if gex.get("gex_per_1pct") is not None:
                    gex_parts = [f"近端GEX/1%={gex['gex_per_1pct']:,.0f}", f"regime={gex.get('regime', '?')}"]
                elif gex.get("net_gex") is not None:
                    gex_parts = [f"近端NetGEX={gex['net_gex']:,.0f}", f"regime={gex.get('regime', '?')}"]
                else:
                    gex_parts = [f"regime={gex.get('regime', '?')}"]
                if gex.get("call_wall"):
                    gex_parts.append(f"CallWall={gex['call_wall']:g}")
                if gex.get("put_wall"):
                    gex_parts.append(f"PutWall={gex['put_wall']:g}")
                if gex.get("gamma_flip"):
                    gex_parts.append(f"GammaFlip≈{gex['gamma_flip']:g}")
                parts.append(" ".join(gex_parts))
            ctx_lines = []
            if parts:
                src_label = "Longbridge+yfinance" if data.get("fallback_source") else "Longbridge"
                ctx_lines.append(f"\n  期权链({src_label}): {', '.join(parts)}")
            quote_error = data.get("quote_error")
            if quote_error and not data.get("fallback_source"):
                reason = _lb_quote_error_reason(quote_error)
                ctx_lines.append(
                    f"\n  ⚠️ 期权逐档行情不可用：{reason}。本次没有真实的 IV / OI / Greeks / GEX，"
                    f"不要编造具体合约的隐波或错误定价结论，应明确告知用户此限制。"
                )
            elif quote_error and data.get("fallback_source"):
                ctx_lines.append(
                    f"\n  ℹ️ Longbridge 期权行情权限未开通，已自动改用 yfinance 延迟数据"
                    f"（到期 {data.get('fallback_expiry', '?')}）计算 IV / OI / Greeks / GEX，"
                    f"数据可能有 15 分钟延迟，请据此判断而非编造。"
                )
            skew = data.get("iv_skew") or []
            if skew:
                ctx_lines.append("\n  IV Skew(行权价|CallIV%|PutIV%|CallOI|PutOI):")
                for row in skew:
                    ctx_lines.append(
                        "\n    {strike:g} | {civ} | {piv} | {coi} | {poi}".format(
                            strike=row["strike"],
                            civ=f"{row['call_iv'] * 100:.1f}" if row.get("call_iv") else "-",
                            piv=f"{row['put_iv'] * 100:.1f}" if row.get("put_iv") else "-",
                            coi=f"{row['call_oi']:,}" if row.get("call_oi") else "-",
                            poi=f"{row['put_oi']:,}" if row.get("put_oi") else "-",
                        )
                    )
            if data.get("other_expirations"):
                ctx_lines.append(f"\n  其它可用到期日: {', '.join(data['other_expirations'])}")
            ctx = "".join(ctx_lines)
            # Be honest in the trace: a chain with no live quotes is only partial,
            # so surface the entitlement/quote problem instead of a misleading ✓.
            if data.get("quote_error") and not data.get("fallback_source"):
                data["quote_error_reason"] = _lb_quote_error_reason(data["quote_error"])
                trace["status"] = "partial"
                trace["result"] = data
                trace["note"] = f"仅到期结构，缺逐档行情: {data['quote_error_reason']}"
            elif data.get("quote_error") and data.get("fallback_source"):
                # Longbridge entitlement missing but yfinance fallback supplied real data.
                trace["status"] = "done"
                trace["result"] = data
                trace["note"] = f"Longbridge 无期权行情权限，已用 yfinance 延迟数据补齐 (到期 {data.get('fallback_expiry', '?')})"
            else:
                trace["status"] = "done"
                trace["result"] = data
        else:
            trace["status"] = "empty"
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = str(exc)
    return ("lb_chain", sym, ctx, False, 0.0, trace)


def _thetadata_as_of_date(env: dict[str, Any] | None) -> date | None:
    """Decide the ThetaData chain data date.

    Returns ``None`` while the regular session is open so the chat uses the *live*
    option snapshot (盘中实时). Outside regular hours (pre-market, after-hours,
    weekends, holidays) returns the most recent *completed* regular trading session
    date so we fall back to that day's EOD chain instead of an empty realtime snapshot.
    """
    env = env if isinstance(env, dict) else market_environment()
    if env.get("is_market_open_regular"):
        return None
    # Derive "today" from the same env so the decision is internally consistent.
    date_iso = env.get("date_et")
    try:
        today = date.fromisoformat(str(date_iso)) if date_iso else et_today()
    except ValueError:
        today = et_today()
    # If today is a trading day whose regular session has already closed, today's
    # EOD is final and is the freshest completed session.
    close_iso = env.get("regular_close_at_et")
    now_iso = env.get("now_et")
    if env.get("is_trading_day") and close_iso and now_iso:
        try:
            if datetime.fromisoformat(str(now_iso)) >= datetime.fromisoformat(str(close_iso)):
                return today
        except ValueError:
            pass
    # Pre-market on a trading day, weekend, or holiday → previous completed session.
    return previous_nyse_trading_day(today - timedelta(days=1))


def _run_tool_thetadata_chain(sym, lb_sym, spot=0.0, requested_expiry=None, env=None):
    trace = {"symbol": sym, "status": "fetching", "tool": "thetadata_option_chain_summary"}
    ctx = ""
    try:
        # 盘中 (regular session open) → live snapshot; otherwise fall back to the
        # most recent completed session's EOD chain (weekend/holiday/pre/after-hours).
        as_of_date = _thetadata_as_of_date(env)
        theta_error = ""
        try:
            data = _thetadata_option_chain_summary(sym, spot, requested_expiry, as_of_date)
        except Exception as exc:
            theta_error = str(exc)[:160]
            data = None
        # Fallback (last resort): when ThetaData returns no per-strike chain (both the
        # bulk and per-strike historical paths came back empty, or it errored), enrich
        # from yfinance delayed data so the tool stays useful even without a Longbridge
        # option-quote entitlement. ThetaData EOD remains the preferred source.
        if not (isinstance(data, dict) and data.get("atm_strike")):
            seed = data if isinstance(data, dict) else {
                "source": "thetadata",
                "as_of_date": as_of_date.isoformat() if as_of_date else None,
            }
            enriched = _yf_chain_fallback(sym, spot, seed)
            if enriched.get("atm_strike"):
                # yfinance is the latest *delayed* chain, not the requested EOD date,
                # so drop as_of_date to avoid mislabeling it as "上一交易日EOD".
                enriched.pop("as_of_date", None)
                enriched["fallback_source"] = "yfinance"
                data = enriched
        if data:
            parts = []
            exp = data.get("analyzed_expiry") or data.get("nearest_expiry")
            if exp:
                dte = data.get("days_to_expiry")
                dte_txt = f"剩余{dte}天" if isinstance(dte, int) else ""
                tag = "(指定)" if data.get("expiry_matched_request") else ""
                parts.append(f"分析到期={exp}{tag} {dte_txt}".strip())
            if data.get("as_of_date"):
                parts.append(f"数据日={data['as_of_date']}(上一交易日EOD)")
            if data.get("atm_strike"):
                parts.append(f"ATM行权={data['atm_strike']:g}")
            if data.get("atm_window_oi"):
                parts.append(f"ATM区OI≈{data['atm_window_oi']:,}")
            if data.get("pcr") is not None:
                parts.append(f"PCR={data['pcr']:.2f}")
            if data.get("atm_iv") is not None:
                parts.append(f"ATM隐波={data['atm_iv'] * 100:.1f}%")
            greeks = data.get("atm_greeks") or {}
            call_g = greeks.get("call")
            if call_g:
                parts.append(f"ATM Call δ={call_g['delta']:.2f} Γ={call_g['gamma']:.4f} Θ={call_g['theta_per_day']:.3f}/天")
            put_g = greeks.get("put")
            if put_g:
                parts.append(f"ATM Put δ={put_g['delta']:.2f} Θ={put_g['theta_per_day']:.3f}/天")
            gex = data.get("near_term_gex") or {}
            if gex.get("available"):
                gex_parts = []
                if gex.get("gex_per_1pct") is not None:
                    gex_parts.append(f"近端GEX/1%={gex['gex_per_1pct']:,.0f}")
                gex_parts.append(f"regime={gex.get('regime', '?')}")
                if gex.get("call_wall"):
                    gex_parts.append(f"CallWall={gex['call_wall']:g}")
                if gex.get("put_wall"):
                    gex_parts.append(f"PutWall={gex['put_wall']:g}")
                if gex.get("gamma_flip"):
                    gex_parts.append(f"GammaFlip≈{gex['gamma_flip']:g}")
                parts.append(" ".join(gex_parts))
            ctx_lines = []
            if parts:
                if data.get("fallback_source") == "yfinance":
                    src_label = "yfinance延迟回退"
                elif data.get("as_of_date"):
                    src_label = f"ThetaData EOD {data['as_of_date']}"
                else:
                    src_label = "ThetaData盘中实时"
                ctx_lines.append(f"\n  期权链({src_label}): {', '.join(parts)}")
                if data.get("fallback_source") == "yfinance":
                    ctx_lines.append(
                        "\n  ℹ️ ThetaData 期权链暂不可用，以上为 yfinance 延迟数据(非实时)，仅供参考，请勿当作实时报价。"
                    )
                elif data.get("as_of_date"):
                    ctx_lines.append(
                        f"\n  ℹ️ 当前非盘中(休市/盘前/盘后)，以上为最近交易日 {data['as_of_date']} 的 ThetaData EOD 真实数据，"
                        f"非盘中实时，请据此判断而非编造。"
                    )
            skew = data.get("iv_skew") or []
            if skew:
                ctx_lines.append("\n  IV Skew(行权价|CallIV%|PutIV%|CallOI|PutOI):")
                for row in skew:
                    ctx_lines.append(
                        "\n    {strike:g} | {civ} | {piv} | {coi} | {poi}".format(
                            strike=row["strike"],
                            civ=f"{row['call_iv'] * 100:.1f}" if row.get("call_iv") else "-",
                            piv=f"{row['put_iv'] * 100:.1f}" if row.get("put_iv") else "-",
                            coi=f"{row['call_oi']:,}" if row.get("call_oi") else "-",
                            poi=f"{row['put_oi']:,}" if row.get("put_oi") else "-",
                        )
                    )
            ctx = "".join(ctx_lines)
            trace["status"] = "done"
            trace["result"] = data
            if data.get("fallback_source") == "yfinance":
                trace["note"] = "ThetaData 不可用，已回退 yfinance 延迟期权数据"
                if theta_error:
                    trace["note"] += f"（ThetaData: {theta_error}）"
            elif data.get("as_of_date"):
                trace["note"] = f"非盘中，已用 ThetaData 最近交易日 EOD ({data['as_of_date']})"
            else:
                trace["note"] = "盘中实时 ThetaData 期权快照"
        else:
            trace["status"] = "empty"
            if theta_error:
                trace["error"] = theta_error
                trace["result"] = {"note": f"ThetaData 与 yfinance 均无数据；ThetaData: {theta_error}"}
    except Exception as exc:
        trace["status"] = "error"
        trace["error"] = str(exc)[:200]
    return ("thetadata_chain", sym, ctx, False, 0.0, trace)


def _thetadata_option_chain_summary(
    symbol: str,
    spot: float = 0.0,
    requested_expiry: str | None = None,
    as_of_date: date | None = None,
) -> dict[str, Any] | None:
    """Option chain summary via ThetaData.

    Mirrors :func:`_lb_option_chain_summary` but sources OI / IV / Greeks / quote
    from ThetaData. When ``as_of_date`` is supplied (weekend / closed market) the
    chain is built from that day's EOD data instead of empty realtime snapshots.
    Reuses the BSM Greeks and near-term GEX helpers via synthetic per-strike keys.
    """
    try:
        from . import thetadata_option_tool as theta

        root = symbol.replace(".US", "").upper()
        exps = theta.option_expirations(root)
        if not exps:
            return None

        # Expiration selection is relative to *today* so we analyze a still-tradable
        # (future) expiry, while the quote/Greeks data itself is pulled as of the
        # previous trading day when as_of_date is set (weekend / closed market).
        today = et_today()

        def _is_future(e: str) -> bool:
            try:
                return date.fromisoformat(e) >= today
            except ValueError:
                return False

        future_exps = [e for e in exps if _is_future(e)]
        usable_exps = future_exps or exps
        nearest_expiry = usable_exps[0]

        analyzed_expiry = nearest_expiry
        expiry_matched_request = False
        if requested_expiry:
            if requested_expiry in exps:
                analyzed_expiry = requested_expiry
                expiry_matched_request = True
            else:
                try:
                    target = date.fromisoformat(requested_expiry)
                    analyzed_expiry = min(usable_exps, key=lambda e: abs((date.fromisoformat(e) - target).days))
                except ValueError:
                    analyzed_expiry = nearest_expiry

        try:
            days_to_expiry: int | None = (date.fromisoformat(analyzed_expiry) - today).days
        except ValueError:
            days_to_expiry = None

        other_expirations = [e for e in usable_exps[:_LB_CHAIN_EXPIRATIONS] if e != analyzed_expiry][:4]
        base: dict[str, Any] = {
            "nearest_expiry": nearest_expiry,
            "analyzed_expiry": analyzed_expiry,
            "requested_expiry": requested_expiry,
            "expiry_matched_request": expiry_matched_request,
            "days_to_expiry": days_to_expiry,
            "total_expirations": len(exps),
            "other_expirations": other_expirations,
            "source": "thetadata",
            "as_of_date": as_of_date.isoformat() if as_of_date else None,
        }

        rows, meta = theta.option_chain_rows(
            root,
            analyzed_expiry,
            spot=spot,
            as_of_date=as_of_date,
            strike_range=max(_LB_CHAIN_ATM_STRIKES * 2, 16),
        )
        if not rows:
            return base

        if spot <= 0:
            spot = _lb_chain_float(meta.get("underlying_price"))

        # Build synthetic by_strike + quotes so the shared GEX / Greeks helpers apply.
        by_strike: dict[float, dict[str, str]] = {}
        quotes: dict[str, dict[str, Any]] = {}
        for row in rows:
            strike = _lb_chain_float(row.get("strike"))
            side = str(row.get("side") or "").lower()
            if strike <= 0 or side not in ("call", "put"):
                continue
            key = f"{side[0].upper()}@{strike:g}"
            by_strike.setdefault(strike, {"call": "", "put": ""})[side] = key
            quotes[key] = {
                "implied_volatility": _lb_chain_float(row.get("implied_volatility")),
                "open_interest": int(_lb_chain_float(row.get("open_interest"))),
                "bid": _lb_chain_float(row.get("bid")),
                "ask": _lb_chain_float(row.get("ask")),
                "last_price": _lb_chain_float(row.get("last_price")),
            }

        strikes = sorted(by_strike)
        if not strikes:
            return base
        if spot > 0:
            atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        else:
            atm_idx = len(strikes) // 2
        lo = max(0, atm_idx - _LB_CHAIN_ATM_STRIKES)
        hi = min(len(strikes), atm_idx + _LB_CHAIN_ATM_STRIKES + 1)
        window_strikes = strikes[lo:hi]
        atm_strike = strikes[atm_idx]

        call_syms = [by_strike[s]["call"] for s in window_strikes if by_strike[s]["call"]]
        put_syms = [by_strike[s]["put"] for s in window_strikes if by_strike[s]["put"]]
        call_oi = sum(int(_lb_chain_float(quotes.get(s, {}).get("open_interest"))) for s in call_syms)
        put_oi = sum(int(_lb_chain_float(quotes.get(s, {}).get("open_interest"))) for s in put_syms)
        pcr = round(put_oi / call_oi, 3) if call_oi > 0 else None

        iv_skew: list[dict[str, Any]] = []
        for s in window_strikes:
            cq = quotes.get(by_strike[s]["call"], {})
            pq = quotes.get(by_strike[s]["put"], {})
            civ = _lb_chain_float(cq.get("implied_volatility"))
            piv = _lb_chain_float(pq.get("implied_volatility"))
            coi = int(_lb_chain_float(cq.get("open_interest")))
            poi = int(_lb_chain_float(pq.get("open_interest")))
            iv_skew.append(
                {
                    "strike": s,
                    "call_iv": round(civ, 4) if civ > 0 else None,
                    "put_iv": round(piv, 4) if piv > 0 else None,
                    "call_oi": coi or None,
                    "put_oi": poi or None,
                }
            )

        atm_call_sym = by_strike.get(atm_strike, {}).get("call", "")
        atm_iv = None
        if atm_call_sym:
            iv = _lb_chain_float(quotes.get(atm_call_sym, {}).get("implied_volatility"))
            if iv > 0:
                atm_iv = round(iv, 4)

        base.update(
            {
                "atm_strike": atm_strike,
                "atm_window_oi": call_oi + put_oi,
                "pcr": pcr,
                "atm_iv": atm_iv,
                "quoted_contracts": len(call_syms) + len(put_syms),
                "iv_skew": iv_skew,
            }
        )

        if spot > 0:
            gex = _lb_near_term_gex(by_strike, quotes, window_strikes, spot, analyzed_expiry)
            if gex.get("available"):
                gex["source"] = "thetadata_chain"
                base["near_term_gex"] = gex

        if atm_strike > 0 and spot > 0:
            greeks = _lb_atm_greeks(
                spot=spot,
                strike=atm_strike,
                expiration=analyzed_expiry,
                call_quote=quotes.get(atm_call_sym),
                put_quote=quotes.get(by_strike.get(atm_strike, {}).get("put", "")),
                fallback_iv=atm_iv,
            )
            if greeks:
                base["atm_greeks"] = greeks
        return base
    except Exception as exc:
        # Surface the real reason (network / auth / ThetaData server error) instead of
        # silently returning None, which the caller would otherwise render as a
        # contextless "empty". The caller catches this and sets trace status="error".
        raise RuntimeError(f"ThetaData 期权链获取失败: {str(exc)[:160]}") from exc


def _lb_chain_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _lb_quote_error_reason(err: Any) -> str:
    """Human-readable reason for an option-quote failure.

    The most common cause is the account lacking US option market-data
    entitlement (Longbridge error code 301604 "no quote access"); translate it
    into actionable Chinese so the chat answer can tell the user what to fix
    instead of vaguely claiming the data was "not provided".
    """
    text = str(err or "")
    if "301604" in text or "no quote access" in text.lower():
        return "期权行情权限未开通 (Longbridge 301604 no quote access)，请在长桥账户开通美股期权 Level-1 行情后重试"
    if "rate" in text.lower() or "429" in text or "limit" in text.lower():
        return "期权行情接口被限流，请稍后重试"
    return f"期权行情拉取失败: {text[:120]}"


def _lb_option_chain_summary(
    symbol: str,
    account_name: str | None,
    spot: float = 0.0,
    requested_expiry: str | None = None,
) -> dict[str, Any] | None:
    """Option chain summary via Longbridge SDK.

    ``option_chain_info_by_date`` only returns the chain structure (strikes and
    contract symbols), so open interest / IV must come from ``option_quote``.
    We pick the expiration to analyze (the user-requested date when available,
    otherwise the nearest) then pull live quotes for an at-the-money strike
    window to compute a real PCR, ATM implied volatility, BSM Greeks and a
    per-strike IV skew without flooding the option-quote rate limit.
    """
    try:
        exps = option_expirations(symbol, account_name)
        if not exps:
            return None

        # Longbridge returns already-expired dates too; only future (and today)
        # expirations are tradable, so prefer those for defaults.
        today = et_today()
        def _is_future(e: str) -> bool:
            try:
                return date.fromisoformat(e) >= today
            except ValueError:
                return False

        future_exps = [e for e in exps if _is_future(e)]
        usable_exps = future_exps or exps  # fall back to raw list if clock/data is off
        nearest_expiry = usable_exps[0]

        # Choose which expiration to analyze.
        analyzed_expiry = nearest_expiry
        expiry_matched_request = False
        if requested_expiry:
            if requested_expiry in exps:
                analyzed_expiry = requested_expiry
                expiry_matched_request = True
            else:
                try:
                    target = date.fromisoformat(requested_expiry)
                    analyzed_expiry = min(
                        usable_exps, key=lambda e: abs((date.fromisoformat(e) - target).days)
                    )
                except ValueError:
                    analyzed_expiry = nearest_expiry

        try:
            days_to_expiry: int | None = (date.fromisoformat(analyzed_expiry) - today).days
        except ValueError:
            days_to_expiry = None

        other_expirations = [e for e in usable_exps[:_LB_CHAIN_EXPIRATIONS] if e != analyzed_expiry][:4]
        base: dict[str, Any] = {
            "nearest_expiry": nearest_expiry,
            "analyzed_expiry": analyzed_expiry,
            "requested_expiry": requested_expiry,
            "expiry_matched_request": expiry_matched_request,
            "days_to_expiry": days_to_expiry,
            "total_expirations": len(exps),
            "other_expirations": other_expirations,
            "source": "longbridge",
        }

        chain = option_chain_info(symbol, analyzed_expiry, account_name)
        if not chain:
            return base

        # Map strike -> contract symbols for both legs.
        by_strike: dict[float, dict[str, str]] = {}
        for item in chain:
            strike = _lb_chain_float(item.get("strike"))
            if strike <= 0:
                continue
            entry = by_strike.setdefault(strike, {"call": "", "put": ""})
            call_sym = str(item.get("call_symbol") or "").strip()
            put_sym = str(item.get("put_symbol") or "").strip()
            if call_sym:
                entry["call"] = call_sym
            if put_sym:
                entry["put"] = put_sym

        strikes = sorted(by_strike)
        if spot > 0 and strikes:
            atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
            lo = max(0, atm_idx - _LB_CHAIN_ATM_STRIKES)
            hi = min(len(strikes), atm_idx + _LB_CHAIN_ATM_STRIKES + 1)
            window_strikes = strikes[lo:hi]
        else:
            window_strikes = strikes
        atm_strike = min(strikes, key=lambda s: abs(s - spot)) if (spot > 0 and strikes) else None

        call_syms = [by_strike[s]["call"] for s in window_strikes if by_strike[s]["call"]]
        put_syms = [by_strike[s]["put"] for s in window_strikes if by_strike[s]["put"]]
        all_syms = call_syms + put_syms

        quotes: dict[str, dict[str, Any]] = {}
        if all_syms:
            try:
                for row in option_quotes(all_syms, account_name):
                    key = str(row.get("symbol") or "").strip()
                    if key:
                        quotes[key] = row
            except Exception as exc:
                # Option-quote entitlement / rate-limit failures must not wipe out
                # the whole summary — record the error then try the free yfinance
                # source (same as the scanner) so we still get real OI/IV/Greeks/GEX.
                base["quote_error"] = str(exc)
                return _yf_chain_fallback(symbol, spot, base)

        call_oi = sum(int(_lb_chain_float(quotes.get(s, {}).get("open_interest"))) for s in call_syms)
        put_oi = sum(int(_lb_chain_float(quotes.get(s, {}).get("open_interest"))) for s in put_syms)
        pcr = round(put_oi / call_oi, 3) if call_oi > 0 else None

        # Per-strike IV skew across the ATM window.
        iv_skew: list[dict[str, Any]] = []
        for s in window_strikes:
            cq = quotes.get(by_strike[s]["call"], {})
            pq = quotes.get(by_strike[s]["put"], {})
            civ = _lb_chain_float(cq.get("implied_volatility"))
            piv = _lb_chain_float(pq.get("implied_volatility"))
            coi = int(_lb_chain_float(cq.get("open_interest")))
            poi = int(_lb_chain_float(pq.get("open_interest")))
            iv_skew.append(
                {
                    "strike": s,
                    "call_iv": round(civ, 4) if civ > 0 else None,
                    "put_iv": round(piv, 4) if piv > 0 else None,
                    "call_oi": coi or None,
                    "put_oi": poi or None,
                }
            )

        atm_call_sym = by_strike.get(atm_strike, {}).get("call", "") if atm_strike is not None else ""
        atm_put_sym = by_strike.get(atm_strike, {}).get("put", "") if atm_strike is not None else ""
        atm_iv = None
        if atm_call_sym:
            iv = _lb_chain_float(quotes.get(atm_call_sym, {}).get("implied_volatility"))
            if iv > 0:
                atm_iv = round(iv, 4)

        base.update(
            {
                "atm_strike": atm_strike,
                "atm_window_oi": call_oi + put_oi,
                "pcr": pcr,
                "atm_iv": atm_iv,
                "quoted_contracts": len(all_syms),
                "iv_skew": iv_skew,
            }
        )

        # Near-term GEX computed from the data already fetched (same formula as the
        # scanner's _gex_context), so we get a real Gamma-exposure read without the
        # separate slow full-chain GEX call that the chat deadline would kill.
        if spot > 0 and atm_strike is not None:
            gex = _lb_near_term_gex(by_strike, quotes, window_strikes, spot, analyzed_expiry)
            if gex.get("available"):
                base["near_term_gex"] = gex

        # BSM Greeks for the ATM call/put (Longbridge does not return Greeks).
        if atm_strike is not None and atm_strike > 0 and spot > 0:
            greeks = _lb_atm_greeks(
                spot=spot,
                strike=atm_strike,
                expiration=analyzed_expiry,
                call_quote=quotes.get(atm_call_sym),
                put_quote=quotes.get(atm_put_sym),
                fallback_iv=atm_iv,
            )
            if greeks:
                base["atm_greeks"] = greeks
        return base
    except Exception:
        return None


def _yf_chain_fallback(symbol: str, spot: float, base: dict[str, Any]) -> dict[str, Any]:
    """Populate IV / OI / PCR / Greeks / GEX from yfinance.

    The scanner already uses yfinance option data as a free fallback, so we reuse
    the same source here when Longbridge lacks option quote entitlement (301604).
    This keeps chat analysis usable (real OI/IV/Greeks/GEX) instead of degrading to
    metadata-only. Data is delayed (yfinance), which the caller surfaces to the user.
    """
    root = symbol.replace(".US", "").upper()
    try:
        from . import yfinance_option_tool as yf
    except Exception:
        return base
    try:
        exps = yf._ticker_options(root) or []
    except Exception:
        exps = []
    if not exps:
        return base

    # Match the Longbridge-analyzed expiry to the closest available yfinance expiry.
    target = base.get("analyzed_expiry")
    yf_exp = target if target in exps else None
    if yf_exp is None:
        try:
            tgt = date.fromisoformat(target) if target else et_today()
            yf_exp = min(exps, key=lambda e: abs((date.fromisoformat(e) - tgt).days))
        except Exception:
            yf_exp = exps[0]

    try:
        chain = yf._option_chain(root, yf_exp)
        calls, puts = chain.calls, chain.puts
    except Exception:
        return base
    if calls is None or puts is None or calls.empty or puts.empty:
        return base

    def _row_map(frame: Any) -> dict[float, dict[str, float]]:
        out: dict[float, dict[str, float]] = {}
        for _, r in frame.iterrows():
            try:
                k = float(r.get("strike"))
            except (TypeError, ValueError):
                continue
            out[k] = {
                "implied_volatility": _lb_chain_float(r.get("impliedVolatility")),
                "open_interest": int(_lb_chain_float(r.get("openInterest"))),
                "bid": _lb_chain_float(r.get("bid")),
                "ask": _lb_chain_float(r.get("ask")),
                "last_price": _lb_chain_float(r.get("lastPrice")),
            }
        return out

    call_map = _row_map(calls)
    put_map = _row_map(puts)
    strikes = sorted(set(call_map) | set(put_map))
    if not strikes:
        return base

    if spot > 0:
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    else:
        atm_idx = len(strikes) // 2
    lo = max(0, atm_idx - _LB_CHAIN_ATM_STRIKES)
    hi = min(len(strikes), atm_idx + _LB_CHAIN_ATM_STRIKES + 1)
    window = strikes[lo:hi]
    atm_strike = strikes[atm_idx]

    call_oi = sum(call_map.get(s, {}).get("open_interest", 0) for s in window)
    put_oi = sum(put_map.get(s, {}).get("open_interest", 0) for s in window)
    pcr = round(put_oi / call_oi, 3) if call_oi > 0 else None

    iv_skew: list[dict[str, Any]] = []
    for s in window:
        cq = call_map.get(s, {})
        pq = put_map.get(s, {})
        civ = cq.get("implied_volatility", 0.0)
        piv = pq.get("implied_volatility", 0.0)
        iv_skew.append(
            {
                "strike": s,
                "call_iv": round(civ, 4) if civ > 0 else None,
                "put_iv": round(piv, 4) if piv > 0 else None,
                "call_oi": cq.get("open_interest") or None,
                "put_oi": pq.get("open_interest") or None,
            }
        )

    atm_iv = None
    c_atm = call_map.get(atm_strike, {})
    if c_atm.get("implied_volatility", 0) > 0:
        atm_iv = round(c_atm["implied_volatility"], 4)

    base.update(
        {
            "atm_strike": atm_strike,
            "atm_window_oi": call_oi + put_oi,
            "pcr": pcr,
            "atm_iv": atm_iv,
            "quoted_contracts": len(window) * 2,
            "iv_skew": iv_skew,
            "fallback_source": "yfinance",
            "fallback_expiry": yf_exp,
            "source": "longbridge+yfinance",
        }
    )

    if spot > 0 and atm_strike > 0:
        greeks = _lb_atm_greeks(
            spot=spot,
            strike=atm_strike,
            expiration=yf_exp,
            call_quote=call_map.get(atm_strike),
            put_quote=put_map.get(atm_strike),
            fallback_iv=atm_iv,
        )
        if greeks:
            base["atm_greeks"] = greeks

    try:
        gex = fetch_gex(root, "yfinance", spot if spot > 0 else None)
        if gex.get("available"):
            base["near_term_gex"] = {
                "available": True,
                "regime": gex.get("regime"),
                "gex_per_1pct": gex.get("gex_per_1pct"),
                "net_gex": gex.get("net_gex"),
                "call_wall": gex.get("call_wall"),
                "put_wall": gex.get("put_wall"),
                "gamma_flip": gex.get("gamma_flip"),
                "expiration": "近端综合(yfinance)",
                "source": "yfinance",
            }
    except Exception:
        pass

    return base


def _lb_quote_mid(quote: dict[str, Any] | None) -> float:
    if not quote:
        return 0.0
    bid = _lb_chain_float(quote.get("bid"))
    ask = _lb_chain_float(quote.get("ask"))
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    last = _lb_chain_float(quote.get("last_price") or quote.get("last"))
    if last > 0:
        return last
    return ask if ask > 0 else bid


def _lb_atm_greeks(
    spot: float,
    strike: float,
    expiration: str,
    call_quote: dict[str, Any] | None,
    put_quote: dict[str, Any] | None,
    fallback_iv: float | None,
) -> dict[str, Any]:
    """Black-Scholes-Merton Greeks for the ATM call & put, reusing the scanner's estimator."""
    from .intraday_option_tools import estimate_option_greeks

    out: dict[str, Any] = {}
    for side, quote in (("call", call_quote), ("put", put_quote)):
        if not quote:
            continue
        iv = _lb_chain_float(quote.get("implied_volatility"))
        if iv <= 0:
            iv = float(fallback_iv) if fallback_iv else 0.0
        if iv <= 0:
            continue
        price = _lb_quote_mid(quote)
        try:
            g = estimate_option_greeks(
                spot=spot,
                strike=strike,
                expiration=expiration,
                side=side,
                implied_volatility=iv,
                option_price=price,
            )
        except Exception:
            continue
        out[side] = {
            "iv": round(iv, 4),
            "price": round(price, 4) if price else None,
            "delta": round(g["delta"], 4),
            "gamma": round(g["gamma"], 6),
            "theta_per_day": round(g["theta_per_day"], 4),
            "breakeven": round(g["breakeven"], 2),
            "days_to_expiration": g["days_to_expiration"],
        }
    return out


def _lb_near_term_gex(
    by_strike: dict[float, dict[str, str]],
    quotes: dict[str, dict[str, Any]],
    window_strikes: list[float],
    spot: float,
    expiration: str,
) -> dict[str, Any]:
    """Near-term Gamma Exposure from the already-fetched chain window.

    Uses the same GEX formula as the scanner's ``_gex_context``
    (gamma * OI * 100 * spot^2 * 0.01 * sign), but computed from the option
    quotes we already pulled for the analyzed expiration — no extra network and
    no risk of hitting the chat tool deadline. Covers the ATM strike window only,
    so it represents *near-term* dealer gamma rather than the full-surface GEX.
    """
    from .intraday_option_tools import estimate_option_greeks

    per_strike: dict[float, float] = {}
    total_abs = 0.0
    net = 0.0
    for strike in window_strikes:
        legs = by_strike.get(strike, {})
        for side in ("call", "put"):
            sym = legs.get(side) or ""
            quote = quotes.get(sym)
            if not quote:
                continue
            oi = int(_lb_chain_float(quote.get("open_interest")))
            iv = _lb_chain_float(quote.get("implied_volatility"))
            if oi <= 0 or iv <= 0:
                continue
            try:
                g = estimate_option_greeks(
                    spot=spot,
                    strike=strike,
                    expiration=expiration,
                    side=side,
                    implied_volatility=iv,
                    option_price=_lb_quote_mid(quote),
                )
            except Exception:
                continue
            gamma = float(g.get("gamma") or 0.0)
            if gamma <= 0:
                continue
            sign = 1.0 if side == "call" else -1.0
            gex_value = gamma * oi * 100.0 * spot * spot * 0.01 * sign
            per_strike[strike] = per_strike.get(strike, 0.0) + gex_value
            total_abs += abs(gex_value)
            net += gex_value

    if not per_strike or total_abs <= 0:
        return {"available": False}

    call_wall = max((s for s, v in per_strike.items() if v > 0), key=lambda s: per_strike[s], default=0.0)
    put_wall = min((s for s, v in per_strike.items() if v < 0), key=lambda s: per_strike[s], default=0.0)
    # Gamma flip: strike where cumulative GEX crosses zero (closest to balanced).
    flip = 0.0
    best_abs = float("inf")
    cumulative = 0.0
    for strike in sorted(per_strike):
        cumulative += per_strike[strike]
        if abs(cumulative) < best_abs:
            best_abs = abs(cumulative)
            flip = strike
    regime = (
        "positive_gamma" if net > total_abs * 0.12
        else "negative_gamma" if net < -total_abs * 0.12
        else "neutral"
    )
    return {
        "available": True,
        "scope": "near_term_window",
        "expiration": expiration,
        "regime": regime,
        "net_gex": round(net, 2),
        "gross_gex": round(total_abs, 2),
        "gex_per_1pct": round(net, 2),
        "call_wall": round(call_wall, 2) if call_wall else 0.0,
        "put_wall": round(put_wall, 2) if put_wall else 0.0,
        "gamma_flip": round(flip, 2) if flip else 0.0,
        "source": "longbridge_chain",
    }


def _build_chat_data_tables(symbol: str, tool_results: dict[str, Any], locale: str = "zh") -> list[dict[str, Any]]:
    """Build structured raw-data tables from tool results for authoritative UI display."""
    tables: list[dict[str, Any]] = []

    for result_key, src_label in (
        ("lb_option_chain_summary", "Longbridge"),
        ("thetadata_option_chain_summary", "ThetaData"),
    ):
        chain = tool_results.get(result_key)
        if isinstance(chain, dict):
            tables.extend(_chain_data_tables(symbol, chain, src_label, locale))

    return tables


# Localized labels for chat raw-data tables. zh is the source; en mirrors it.
_CHAT_TABLE_TEXT = {
    "zh": {
        "chain_overview": "期权链概览", "metric": "指标", "value": "数值",
        "analyzed_expiry": "分析到期日", "user_specified": "（用户指定）",
        "data_date_eod": "数据日期 (上一交易日 EOD)", "days_left": "剩余天数", "days_unit": " 天",
        "atm_strike": "ATM 行权价", "atm_window_oi": "ATM 区总持仓 (OI)",
        "pcr": "Put/Call OI 比 (PCR)", "atm_iv": "ATM 隐含波动率", "total_expirations": "可用到期日总数",
        "level2_quote": "⚠️ 逐档行情",
        "atm_greeks": "ATM 希腊字母 (BSM 计算", "direction": "方向", "premium": "权利金", "theta_per_day": "Theta/天", "breakeven": "盈亏平衡",
        "iv_skew_title": "IV Skew / 持仓分布 (行权价 × Call/Put", "strike": "行权价",
        "near_gex": "近端 Gamma 暴露 (GEX,", "net_gex_1pct": "净 GEX (per 1%)", "net_gex_net": "净 GEX (Net)", "net_gex": "净 GEX",
        "gamma_regime": "Gamma 状态 (regime)",
    },
    "en": {
        "chain_overview": "Option Chain Overview", "metric": "Metric", "value": "Value",
        "analyzed_expiry": "Analyzed Expiry", "user_specified": " (user-specified)",
        "data_date_eod": "Data date (prior session EOD)", "days_left": "Days to expiry", "days_unit": "d",
        "atm_strike": "ATM Strike", "atm_window_oi": "ATM-window OI",
        "pcr": "Put/Call OI Ratio (PCR)", "atm_iv": "ATM Implied Volatility", "total_expirations": "Total expirations",
        "level2_quote": "⚠️ Level-2 quote",
        "atm_greeks": "ATM Greeks (BSM", "direction": "Side", "premium": "Premium", "theta_per_day": "Theta/day", "breakeven": "Breakeven",
        "iv_skew_title": "IV Skew / OI Distribution (strike × Call/Put", "strike": "Strike",
        "near_gex": "Near-term Gamma Exposure (GEX,", "net_gex_1pct": "Net GEX (per 1%)", "net_gex_net": "Net GEX (Net)", "net_gex": "Net GEX",
        "gamma_regime": "Gamma regime",
    },
}


def _chain_data_tables(symbol: str, chain: dict[str, Any], src_label: str, locale: str = "zh") -> list[dict[str, Any]]:
    tx = _CHAT_TABLE_TEXT.get(locale) or _CHAT_TABLE_TEXT["zh"]
    tables: list[dict[str, Any]] = []
    as_of = chain.get("as_of_date")
    src_suffix = f"{src_label} · EOD {as_of}" if as_of else src_label

    # Key metrics summary table.
    metrics: list[list[Any]] = []
    exp = chain.get("analyzed_expiry") or chain.get("nearest_expiry")
    if exp:
        tag = tx["user_specified"] if chain.get("expiry_matched_request") else ""
        metrics.append([tx["analyzed_expiry"], f"{exp}{tag}"])
    if as_of:
        metrics.append([tx["data_date_eod"], as_of])
    if isinstance(chain.get("days_to_expiry"), int):
        sep = "" if locale == "en" else ""
        metrics.append([tx["days_left"], f"{chain['days_to_expiry']}{sep}{tx['days_unit']}" if locale == "zh" else f"{chain['days_to_expiry']} {tx['days_unit']}"])
    if chain.get("atm_strike"):
        metrics.append([tx["atm_strike"], f"{chain['atm_strike']:g}"])
    if chain.get("atm_window_oi"):
        metrics.append([tx["atm_window_oi"], f"{chain['atm_window_oi']:,}"])
    if chain.get("pcr") is not None:
        metrics.append([tx["pcr"], f"{chain['pcr']:.2f}"])
    if chain.get("atm_iv") is not None:
        metrics.append([tx["atm_iv"], f"{chain['atm_iv'] * 100:.1f}%"])
    if chain.get("total_expirations"):
        metrics.append([tx["total_expirations"], str(chain["total_expirations"])])
    if chain.get("quote_error"):
        metrics.append([
            tx["level2_quote"],
            chain.get("quote_error_reason") or _lb_quote_error_reason(chain["quote_error"]),
        ])
    if metrics:
        tables.append(
            {
                "title": f"{symbol} {tx['chain_overview']} ({src_suffix})",
                "columns": [tx["metric"], tx["value"]],
                "rows": metrics,
            }
        )

    # ATM Greeks (BSM) table.
    greeks = chain.get("atm_greeks") or {}
    greek_rows: list[list[Any]] = []
    for side, label in (("call", "Call"), ("put", "Put")):
        g = greeks.get(side)
        if not g:
            continue
        greek_rows.append(
            [
                label,
                f"{g.get('iv', 0) * 100:.1f}%" if g.get("iv") else "-",
                f"{g['price']:.2f}" if g.get("price") else "-",
                f"{g['delta']:.3f}",
                f"{g['gamma']:.5f}",
                f"{g['theta_per_day']:.3f}",
                f"{g['breakeven']:.2f}",
            ]
        )
    if greek_rows:
        tables.append(
            {
                "title": f"{symbol} {tx['atm_greeks']} · {src_suffix})",
                "columns": [tx["direction"], "IV", tx["premium"], "Delta", "Gamma", tx["theta_per_day"], tx["breakeven"]],
                "rows": greek_rows,
            }
        )

    # IV skew table.
    skew = chain.get("iv_skew") or []
    skew_rows: list[list[Any]] = []
    for row in skew:
        skew_rows.append(
            [
                f"{row['strike']:g}",
                f"{row['call_iv'] * 100:.1f}%" if row.get("call_iv") else "-",
                f"{row['put_iv'] * 100:.1f}%" if row.get("put_iv") else "-",
                f"{row['call_oi']:,}" if row.get("call_oi") else "-",
                f"{row['put_oi']:,}" if row.get("put_oi") else "-",
            ]
        )
    if skew_rows:
        tables.append(
            {
                "title": f"{symbol} {tx['iv_skew_title']} · {src_suffix})",
                "columns": [tx["strike"], "Call IV", "Put IV", "Call OI", "Put OI"],
                "rows": skew_rows,
            }
        )

    # Near-term GEX table.
    gex = chain.get("near_term_gex") or {}
    if gex.get("available"):
        if gex.get("gex_per_1pct") is not None:
            gex_value_row = [tx["net_gex_1pct"], f"{gex['gex_per_1pct']:,.0f}"]
        elif gex.get("net_gex") is not None:
            gex_value_row = [tx["net_gex_net"], f"{gex['net_gex']:,.0f}"]
        else:
            gex_value_row = [tx["net_gex"], "-"]
        gex_rows = [
            [tx["gamma_regime"], gex.get("regime", "-")],
            gex_value_row,
            ["Call Wall", f"{gex['call_wall']:g}" if gex.get("call_wall") else "-"],
            ["Put Wall", f"{gex['put_wall']:g}" if gex.get("put_wall") else "-"],
            ["Gamma Flip", f"{gex['gamma_flip']:g}" if gex.get("gamma_flip") else "-"],
        ]
        tables.append(
            {
                "title": f"{symbol} {tx['near_gex']} {gex.get('expiration', '')} · {src_suffix})",
                "columns": [tx["metric"], tx["value"]],
                "rows": gex_rows,
            }
        )

    return tables


def _chat_tool_specs(enabled_tools: set[str], has_lb_account: bool) -> list[dict[str, Any]]:
    """Build OpenAI function-tool specs for the chat tools the model may call.

    Account-dependent tools are omitted when no Longbridge SDK account is
    available so the model never picks a tool that can't run. DTE / expiry
    parameters let the model translate natural language like "near 5 days" or
    "a cheap LEAP" into concrete fetch windows."""
    symbol_param = {
        "symbol": {"type": "string", "description": "US stock ticker, e.g. SPY, AAPL, NVDA. Uppercase, no suffix."}
    }
    chain_params = {
        **symbol_param,
        "dte_min": {"type": "integer", "description": "Minimum days-to-expiration for the chain window (optional)."},
        "dte_max": {"type": "integer", "description": "Maximum days-to-expiration; e.g. 5 for 'near 5 days', 365+ for LEAPs (optional)."},
        "expiry": {"type": "string", "description": "Specific expiration date YYYY-MM-DD (optional; overrides dte window)."},
    }
    catalog = {
        "longbridge_quote": ("Real-time last price and change for a US ticker.", symbol_param, True),
        "longbridge_kline": ("Recent ~15-day daily K-line range and moving average.", symbol_param, True),
        "thetadata_stock_market": ("ThetaData stock quote, minute bars and recent daily K-line. No news feed.", symbol_param, False),
        "yfinance_market": ("Backup quote and daily K-line when no broker account is available.", symbol_param, False),
        "gex_snapshot": ("Gamma exposure: regime, Gamma Flip, Call/Put Wall.", symbol_param, False),
        "lb_option_chain": ("Longbridge option chain: OI, PCR, ATM IV, BSM Greeks, IV skew for a DTE window or expiry.", chain_params, True),
        "thetadata_option_chain": ("ThetaData option chain: OI/IV/Greeks/GEX; falls back to prior EOD on weekends/holidays.", chain_params, False),
    }
    specs: list[dict[str, Any]] = []
    for tool in CHAT_TOOLS:
        tid = tool["id"]
        if tid not in enabled_tools or tid not in catalog:
            continue
        desc, props, requires_account = catalog[tid]
        if requires_account and not has_lb_account:
            continue
        specs.append({
            "type": "function",
            "function": {
                "name": tid,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": ["symbol"],
                },
            },
        })
    return specs


def _dispatch_chat_tool(name: str, args: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Run one chat tool the model requested. Returns (result_text, trace).

    `ctx` carries per-turn resolution state: lb_account_name, provider_owner,
    env, and a spot-price cache keyed by symbol so the chain/GEX tools can reuse
    a freshly fetched quote."""
    sym = _clean_chat_symbol(args.get("symbol"))
    if not sym:
        return ("", {"symbol": str(args.get("symbol") or "?"), "status": "error", "tool": name, "error": "invalid or missing symbol"})
    lb_sym = sym if "." in sym else f"{sym}.US"
    lb_account_name = ctx.get("lb_account_name")
    spot = float(ctx.get("spot_by_symbol", {}).get(sym, 0.0) or 0.0)

    # Translate optional DTE window / expiry into a requested_expiry the chain
    # runners understand (they accept an explicit expiry; the DTE window is a
    # hint the model uses to pick one, surfaced to the user via the trace).
    requested_expiry = args.get("expiry") if isinstance(args.get("expiry"), str) else ctx.get("requested_expiry")

    if name == "longbridge_quote" and lb_account_name:
        _, _, text, ok, new_spot, trace = _run_tool_longbridge_quote(sym, lb_sym, lb_account_name)
        if ok and new_spot:
            ctx.setdefault("spot_by_symbol", {})[sym] = new_spot
        return (text, trace)
    if name == "longbridge_kline" and lb_account_name:
        _, _, text, _ok, _spot, trace = _run_tool_longbridge_kline(sym, lb_sym, lb_account_name)
        return (text, trace)
    if name == "yfinance_market":
        _, _, text, ok, new_spot, trace = _run_tool_yfinance_market(sym)
        if ok and new_spot:
            ctx.setdefault("spot_by_symbol", {})[sym] = new_spot
        return (text, trace)
    if name == "thetadata_stock_market":
        _, _, text, ok, new_spot, trace = _run_tool_thetadata_stock_market(sym)
        if ok and new_spot:
            ctx.setdefault("spot_by_symbol", {})[sym] = new_spot
        return (text, trace)
    if name == "gex_snapshot":
        _, _, text, _ok, _spot, trace = _run_tool_gex(sym, lb_sym, spot, ctx.get("provider_owner"))
        return (text, trace)
    if name == "lb_option_chain" and lb_account_name:
        _, _, text, _ok, _spot, trace = _run_tool_lb_chain(sym, lb_sym, lb_account_name, spot=spot, requested_expiry=requested_expiry)
        if isinstance(trace, dict):
            trace.setdefault("result", {})["dte_window"] = {"dte_min": args.get("dte_min"), "dte_max": args.get("dte_max")}
        return (text, trace)
    if name == "thetadata_option_chain":
        _, _, text, _ok, _spot, trace = _run_tool_thetadata_chain(sym, lb_sym, spot=spot, requested_expiry=requested_expiry, env=ctx.get("env"))
        if isinstance(trace, dict):
            trace.setdefault("result", {})["dte_window"] = {"dte_min": args.get("dte_min"), "dte_max": args.get("dte_max")}
        return (text, trace)
    # Account-required tool requested without an account, or unknown tool.
    return ("", {"symbol": sym, "status": "empty", "tool": name, "result": {"note": "tool unavailable for this account"}})


def _dispatch_chat_tool_streaming(
    name: str,
    args: dict[str, Any],
    ctx: dict[str, Any],
) -> "Generator[str | tuple[str, dict[str, Any]], None, None]":
    """Run a blocking chat tool while keeping the SSE response alive."""
    result_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def _produce() -> None:
        try:
            result_queue.put(("result", _dispatch_chat_tool(name, args, ctx)))
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool error trace.
            result_queue.put(("error", str(exc)[:200]))

    threading.Thread(target=_produce, name=f"chat-tool-{name}", daemon=True).start()
    deadline = time.monotonic() + _CHAT_AGENT_TOOL_DEADLINE
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield (
                "",
                {
                    "symbol": str(args.get("symbol") or "?"),
                    "status": "empty",
                    "tool": name,
                    "result": {"note": f"工具超时(>{_CHAT_AGENT_TOOL_DEADLINE:g}s)，已跳过"},
                },
            )
            return
        try:
            kind, value = result_queue.get(timeout=min(_CHAT_HEARTBEAT_INTERVAL, remaining))
        except queue.Empty:
            yield ": keep-alive\n\n"
            continue
        if kind == "result":
            yield value
            return
        yield (
            "",
            {
                "symbol": str(args.get("symbol") or "?"),
                "status": "error",
                "tool": name,
                "error": value,
            },
        )
        return


# Sentinels the agent generator yields to signal control flow to the caller.
_AGENT_FALLBACK = "__agent_fallback__"
_AGENT_DONE = "__agent_done__"


def _strip_model_tool_markup(text: str) -> str:
    """Remove provider-emitted tool-call markup from final natural-language text."""
    cleaned = re.sub(
        r"<[｜|]{0,2}DSML[｜|]{0,2}tool_calls>.*?</[｜|]{0,2}DSML[｜|]{0,2}tool_calls>",
        "",
        str(text or ""),
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(r"<[｜|]{0,2}DSML[｜|]{0,2}(?:invoke|parameter)\b.*?</[｜|]{0,2}DSML[｜|]{0,2}(?:invoke|parameter)>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def _agent_final_messages(agent_system: str, request: "ChatRequest", msg: str, tool_contexts: list[str]) -> list[dict[str, Any]]:
    """Build a plain-text final-answer prompt instead of replaying tool-call turns.

    Some OpenAI-compatible providers serialize assistant tool-call history back
    into DSML/XML-like text on the final completion. Passing only normal text
    context keeps the final response human-readable.
    """
    final_system = (
        agent_system
        + "\n\n=== 最终回答约束 ===\n"
        "你现在只能输出自然语言分析。禁止输出 tool_calls、DSML、XML、JSON 或函数调用标记。"
        "必须直接回答用户问题。"
    )
    final_messages: list[dict[str, Any]] = [{"role": "system", "content": final_system}]
    for item in (request.history or [])[-12:]:
        role = "user" if item.role == "user" else "assistant"
        text = (item.text or "").strip()
        if text:
            final_messages.append({"role": role, "content": text})
    context = "\n\n".join(part for part in tool_contexts if part.strip())
    final_messages.append({
        "role": "user",
        "content": (
            f"用户问题：{msg}\n\n"
            "以下是本轮工具返回的真实数据，请基于这些数据给出自然语言分析和多空判断：\n"
            f"{context[:12000] if context else '(本轮没有可用工具数据)'}"
        ),
    })
    return final_messages


def _run_chat_agent(
    http_request: "Request",
    request: "ChatRequest",
    owner: str,
    owner_id: str,
    msg: str,
    instance_id: str,
    *,
    provider: Any,
    enabled_tools: set[str],
    lb_account_name: str | None,
    env: dict[str, Any],
    now_ts: str,
) -> "Generator[str, None, None]":
    """Agentic chat turn: the model decides which tools to call with which args
    over a bounded loop, then streams a final answer. Yields SSE strings, or a
    sentinel (_AGENT_FALLBACK / _AGENT_DONE) to tell the caller whether to fall
    back to the legacy pipeline or stop."""
    tool_specs = _chat_tool_specs(enabled_tools, bool(lb_account_name))
    agent_locale = "en" if str(getattr(request, "locale", "") or "").lower().startswith("en") else "zh"
    if not tool_specs:
        # No tools the model can call (e.g. account-only tools, no account) →
        # let the legacy pipeline handle it (it degrades to general knowledge).
        yield _AGENT_FALLBACK
        return

    today_et = env.get("date_et", now_et_iso()[:10])
    trading_note = "交易日" if env.get("is_trading_day") else f"非交易日({env.get('trading_day_reason', '')})"
    date_context = (
        f"今天是 {today_et} (美东 ET)，{trading_note}，市场状态={env.get('session_state', '?')}。"
        f"计算期权剩余到期天数时以此日期为准，不要凭空猜测。"
    )
    agent_system = (
        CHAT_SYSTEM_PROMPT
        + _reply_language_directive(msg)
        + f"\n\n=== 当前日期与交易日 ===\n{date_context}\n"
        + "\n=== 工具调用规则 ===\n"
        "你可以调用提供的工具来获取实时行情、K线、GEX 和期权链数据。"
        "先判断用户问题涉及哪个美股标的与时间范围（例如“近5天”→ dte_max≈5，“LEAP/远月”→ dte_min≈180），"
        "再调用合适的工具并传入 symbol 和（期权链工具的）dte_min/dte_max/expiry。"
        "不要把英文动词或普通词当作 ticker。拿到数据后，必须基于真实返回值分析并给出结论，"
        "缺失的数据要明确说明不可用。无需查询行情的问题可以直接回答。"
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": agent_system}]
    for item in (request.history or [])[-12:]:
        role = "user" if item.role == "user" else "assistant"
        text = (item.text or "").strip()
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": msg})

    ctx: dict[str, Any] = {
        "lb_account_name": lb_account_name,
        "provider_owner": _provider_owner(http_request, owner_id),
        "env": env,
        "spot_by_symbol": {},
        "requested_expiry": _extract_requested_expiry(msg),
    }
    data_tables: list[dict[str, Any]] = []
    tool_contexts: list[str] = []
    prov_owner = _provider_owner(http_request, owner_id)

    made_any_tool_call = False
    for hop in range(_CHAT_AGENT_MAX_HOPS):
        result = chat_with_tools(provider, messages, tool_specs, owner_id=prov_owner, temperature=0.2)
        if not result.get("ok"):
            # Provider rejected tools schema or errored. If we haven't produced
            # anything yet, fall back to the legacy pipeline; otherwise stop.
            if not result.get("supported") and not made_any_tool_call:
                yield _AGENT_FALLBACK
                return
            if not made_any_tool_call:
                yield _AGENT_FALLBACK
                return
            break
        message = result.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            # Model is ready to answer. Stream the final response.
            messages.append({"role": "assistant", "content": message.get("content") or ""})
            break
        # Append the assistant turn (with tool_calls) then run each tool.
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls})
        made_any_tool_call = True
        for call in tool_calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if name not in enabled_tools:
                tool_result_text = ""
                trace = {"symbol": str(args.get("symbol") or "?"), "status": "empty", "tool": name or "unknown", "result": {"note": "tool not enabled"}}
            else:
                yield f"data: {_format_trace('tool_call', {'symbol': str(args.get('symbol') or '?'), 'status': 'fetching', 'tool': name})}\n\n"
                tool_result_text = ""
                trace = {}
                for item in _dispatch_chat_tool_streaming(name, args, ctx):
                    if isinstance(item, str):
                        yield item
                    else:
                        tool_result_text, trace = item
            yield f"data: {_format_trace('tool_call', trace)}\n\n"
            if tool_result_text:
                tool_contexts.append(f"[{name}]\n{tool_result_text.strip()}")
            elif isinstance(trace, dict) and trace.get("result"):
                tool_contexts.append(f"[{name}]\n{json.dumps(trace.get('result'), ensure_ascii=False)}")
            # Chain tools return a structured summary under trace["result"]; turn
            # it into authoritative raw-data tables (localized) like the legacy path.
            if isinstance(trace, dict) and trace.get("tool") in ("lb_option_chain_summary", "thetadata_option_chain_summary") and isinstance(trace.get("result"), dict):
                tbls = _build_chat_data_tables(_clean_chat_symbol(args.get("symbol")) or str(args.get("symbol") or ""), {trace["tool"]: trace["result"]}, agent_locale)
                data_tables.extend(tbls)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or name,
                "content": (tool_result_text or json.dumps(trace.get("result") or {}, ensure_ascii=False))[:4000],
            })
    else:
        # Hop budget exhausted without a final answer: ask once more, no tools.
        nudge = (
            "请基于以上已获取的数据，现在直接给出分析结论，不要再调用工具。"
            if re.search(r"[一-鿿]", msg or "")
            else "Based on the data gathered above, give your final analysis now in English. Do not call any more tools."
        )
        messages.append({"role": "user", "content": nudge})

    if data_tables:
        yield f"data: {_format_trace('data_tables', {'tables': data_tables})}\n\n"
    yield f"data: {_format_trace('ai_start', {'provider': request.ai_provider or 'deepseek'})}\n\n"

    # Stream the final answer from a plain-text prompt. Do not replay raw
    # assistant.tool_calls turns; some providers render them back as DSML.
    final_messages = _agent_final_messages(agent_system, request, msg, tool_contexts) if made_any_tool_call else messages
    token_count = 0
    full_text_parts: list[str] = []
    token_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()

    def _produce() -> None:
        try:
            for token in stream_chat_messages(provider, final_messages, owner_id=prov_owner, temperature=0.3):
                token_queue.put(("token", token))
        except Exception as exc:  # noqa: BLE001
            token_queue.put(("error", str(exc)[:200]))
        finally:
            token_queue.put(("end", None))

    threading.Thread(target=_produce, name=f"chat-agent-{instance_id}", daemon=True).start()
    stream_error: str | None = None
    while True:
        try:
            kind, value = token_queue.get(timeout=_CHAT_HEARTBEAT_INTERVAL)
        except queue.Empty:
            yield ": keep-alive\n\n"
            continue
        if kind == "token":
            token_count += 1
            full_text_parts.append(value)
        elif kind == "error":
            stream_error = value
        else:
            break

    assistant_text = _strip_model_tool_markup("".join(full_text_parts))
    if token_count == 0:
        # Final stream produced nothing — fall back so the user still gets a reply.
        if not made_any_tool_call:
            yield _AGENT_FALLBACK
            return
        err = (stream_error or get_last_ai_error() or "").strip()
        hint = f"（{err}）" if err else ""
        yield f"data: {_format_token('AI 未返回内容，请检查所选模型的 API Key 是否有效。' + hint)}\n\n"
    elif assistant_text:
        yield f"data: {_format_token(assistant_text)}\n\n"
    else:
        err = (stream_error or get_last_ai_error() or "").strip()
        hint = f"（{err}）" if err else ""
        yield f"data: {_format_token('AI 本轮只返回了工具调用标记，没有生成自然语言结论；原始工具数据已展示，请稍后重试。' + hint)}\n\n"

    if assistant_text and getattr(request, "session_id", None):
        try:
            chat_store.append_message(owner, request.session_id, "assistant", assistant_text, tables=data_tables or None, instance_id=instance_id)
        except Exception:
            pass
    yield f"data: {_format_done()}\n\n"
    yield _AGENT_DONE


def _chat_event_stream(http_request: Request, request: ChatRequest, owner_id: str) -> str:
    """SSE generator: emits trace events then token events then done."""
    msg = (request.message or "").strip()

    # Each conversation turn gets its own independent instance id so the UI and
    # logs can trace a single round of Q&A end-to-end. The client may supply the
    # id (generated and shown the moment the user hits send) so the badge is
    # visible immediately even if the connection drops before tokens flow; fall
    # back to a server-generated id when absent.
    instance_id = (request.instance_id or "").strip()[:32] or uuid.uuid4().hex[:12]
    yield f"data: {_format_trace('turn', {'instance_id': instance_id})}\n\n"

    # 0. Resolve Longbridge SDK account (if any) for quote/kline calls.
    #    Prefer the account explicitly selected in the chat panel, fall back to default.
    owner = _request_owner(http_request, owner_id)
    lb_account_name: str | None = None
    try:
        from .account_store import get_account, preferred_sdk_account
        requested = (request.longbridge_account or "").strip()
        acct = None
        if requested:
            acct = get_account(requested, create_if_missing=False, owner_id=owner)
        if acct is None or not acct.sdk_credentials_configured:
            acct = preferred_sdk_account(owner)
        if acct and acct.sdk_credentials_configured:
            lb_account_name = acct.name
    except Exception:
        pass

    # Resolve which tools are enabled (default: all). Account-dependent tools are
    # automatically skipped when no Longbridge SDK account is available.
    if request.tools is None:
        enabled_tools = set(_CHAT_TOOL_IDS)
    else:
        enabled_tools = {t for t in request.tools if t in _CHAT_TOOL_IDS}

    env = market_environment()
    now_ts = f"{env.get('now_et', now_et_iso())[:19]} ET ({env.get('session_state', '?')})"

    # --- Agentic path: let the model choose tools + args itself. -------------
    # Falls through to the legacy regex-extract pipeline when disabled, the
    # provider can't do tool-calling, or the agent loop fails before answering.
    if _CHAT_AGENTIC_ENABLED:
        provider = resolve_chat_provider(request.ai_provider or "deepseek", _provider_owner(http_request, owner_id))
        if provider is not None and provider.provider_type != "claude":
            agent_done = False
            for event in _run_chat_agent(
                http_request, request, owner, owner_id, msg, instance_id,
                provider=provider, enabled_tools=enabled_tools,
                lb_account_name=lb_account_name, env=env, now_ts=now_ts,
            ):
                if event == _AGENT_FALLBACK:
                    break
                if event == _AGENT_DONE:
                    agent_done = True
                    break
                yield event
            if agent_done:
                return
            # else: agent could not run (no tool support / early error) → legacy.

    # 1. Extract symbols
    symbols = _extract_symbols(msg)

    # 2. Fetch market data and emit trace events
    market_context = ""
    requested_expiry = _extract_requested_expiry(msg)

    if symbols:
        yield f"data: {_format_trace('symbols_detected', {'symbols': symbols})}\n\n"

    # Structured raw-data tables for the UI (debugging + authoritative display).
    data_tables: list[dict[str, Any]] = []
    quote_constraints: list[dict[str, str]] = []

    for sym in symbols:
        lb_sym = sym if "." in sym else f"{sym}.US"
        provider_owner = _provider_owner(http_request, owner_id)
        spot_val = 0.0
        tool_results: dict[str, Any] = {}

        # Fetch the quote first so spot price is available for GEX and other tools.
        if "longbridge_quote" in enabled_tools and lb_account_name:
            _, _, ctx, ok, spot, trace = _run_tool_longbridge_quote(sym, lb_sym, lb_account_name)
            if ok:
                spot_val = spot
            if ctx:
                market_context += ctx
            if isinstance(trace.get("result"), dict):
                tool_results["longbridge_quote"] = trace["result"]
            yield f"data: {_format_trace('tool_call', trace)}\n\n"

        # Submit the remaining enabled tools in parallel.
        jobs = []
        if "longbridge_kline" in enabled_tools and lb_account_name:
            jobs.append((_run_tool_longbridge_kline, (sym, lb_sym, lb_account_name)))
        if "thetadata_stock_market" in enabled_tools:
            jobs.append((_run_tool_thetadata_stock_market, (sym,)))
        if "yfinance_market" in enabled_tools:
            jobs.append((_run_tool_yfinance_market, (sym,)))
        if "gex_snapshot" in enabled_tools:
            jobs.append((_run_tool_gex, (sym, lb_sym, spot_val, provider_owner)))
        if "lb_option_chain" in enabled_tools and lb_account_name:
            jobs.append((_run_tool_lb_chain, (sym, lb_sym, lb_account_name, spot_val, requested_expiry)))
        if "thetadata_option_chain" in enabled_tools:
            jobs.append((_run_tool_thetadata_chain, (sym, lb_sym, spot_val, requested_expiry, env)))

        if jobs:
            # Gather with a hard deadline so a slow Longbridge call can't block AI start.
            pool = ThreadPoolExecutor(max_workers=max(2, len(jobs)))
            futs = {pool.submit(fn, *args): args[0] for fn, args in jobs}
            try:
                for fut in as_completed(futs, timeout=_CHAT_TOOL_DEADLINE):
                    kind, sym_name, ctx, ok, spot, trace = fut.result()
                    if ctx:
                        market_context += ctx
                    if isinstance(trace.get("result"), dict) and trace.get("tool"):
                        tool_results[str(trace["tool"])] = trace["result"]
                    yield f"data: {_format_trace('tool_call', trace)}\n\n"
            except FuturesTimeoutError:
                for fut, args in futs.items():
                    if not fut.done():
                        yield f"data: {_format_trace('tool_call', {'symbol': sym, 'status': 'empty', 'tool': 'pending', 'result': {'note': '工具超时，已跳过'}})}\n\n"
            finally:
                # Do not wait on still-running blocking IO; let it finish in the background.
                pool.shutdown(wait=False)

        data_tables.extend(_build_chat_data_tables(sym, tool_results, "en" if str(getattr(request, "locale", "") or "").lower().startswith("en") else "zh"))
        chain = tool_results.get("lb_option_chain_summary")
        if isinstance(chain, dict) and chain.get("quote_error") and not chain.get("fallback_source"):
            quote_constraints.append(
                {
                    "symbol": sym,
                    "expiry": str(chain.get("analyzed_expiry") or chain.get("nearest_expiry") or "-"),
                    "reason": str(chain.get("quote_error_reason") or _lb_quote_error_reason(chain.get("quote_error"))),
                }
            )

    # Emit the raw data tables so the UI can render them authoritatively.
    if data_tables:
        yield f"data: {_format_trace('data_tables', {'tables': data_tables})}\n\n"

    if not market_context:
        market_context = "\n(未检测到美股 ticker，以下分析基于公开知识和经验判断)"
        yield f"data: {_format_trace('note', {'text': '未检测到可查询的美股代码，将基于通用知识回答'})}\n\n"

    constraint_context = ""
    if quote_constraints:
        lines = []
        for row in quote_constraints:
            lines.append(
                f"- {row['symbol']} ({row['expiry']}): {row['reason']}。"
                "禁止给出该标的的逐档 IV/OI/GEX 结论与具体合约高低估判断。"
            )
        constraint_context = "\n".join(lines)
        # Surface a concise warning in the trace area so users immediately know
        # why contract-level mispricing cannot be computed.
        brief = "；".join(f"{row['symbol']}: {row['reason']}" for row in quote_constraints[:2])
        yield f"data: {_format_trace('note', {'text': f'逐档期权行情受限，已降级为框架分析：{brief}'})}\n\n"

    # 3. Build prompt with real data
    today_et = env.get("date_et", now_et_iso()[:10])
    trading_note = "交易日" if env.get("is_trading_day") else f"非交易日({env.get('trading_day_reason', '')})"
    date_context = (
        f"今天是 {today_et} (美东 ET)，{trading_note}，市场状态={env.get('session_state', '?')}。"
        f"计算期权剩余到期天数时以此日期为准，不要凭空猜测。"
    )
    sys_prompt = (
        CHAT_SYSTEM_PROMPT
        + _reply_language_directive(msg)
        + f"\n\n=== 当前日期与交易日 ===\n{date_context}\n"
        + f"\n=== 实时市场数据 (获取时间: {now_ts}) ===\n{market_context}\n=== 数据结束 ==="
        + (f"\n\n=== 数据可用性约束 ===\n{constraint_context}\n" if constraint_context else "")
    )

    # 4. Stream AI tokens
    yield f"data: {_format_trace('ai_start', {'provider': request.ai_provider or 'deepseek'})}\n\n"

    # Include prior conversation so the session has continuity.
    history_payload = []
    for item in (request.history or [])[-12:]:
        role = "user" if item.role == "user" else "assistant"
        text = (item.text or "").strip()
        if text:
            history_payload.append({"role": role, "text": text})

    try:
        token_count = 0
        full_text_parts: list[str] = []
        stream_error: str | None = None

        # Run the (blocking) model stream in a background thread and drain it
        # through a queue. This lets us emit SSE keep-alive comments during long
        # "thinking" gaps so proxies / load balancers do not drop the connection
        # (which previously surfaced to users as a network error mid-answer).
        token_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        def _produce_tokens() -> None:
            try:
                for token in stream_ask_ai(
                    system_prompt=sys_prompt,
                    user_payload={"query": msg, "history": history_payload},
                    provider_name=request.ai_provider or "deepseek",
                    owner_id=_provider_owner(http_request, owner_id),
                ):
                    token_queue.put(("token", token))
            except Exception as exc:  # noqa: BLE001 - surfaced to the client below
                token_queue.put(("error", str(exc)[:200]))
            finally:
                token_queue.put(("end", None))

        producer = threading.Thread(
            target=_produce_tokens, name=f"chat-ai-{instance_id}", daemon=True
        )
        producer.start()

        while True:
            try:
                kind, value = token_queue.get(timeout=_CHAT_HEARTBEAT_INTERVAL)
            except queue.Empty:
                # SSE comment line — ignored by the EventSource/stream parser but
                # keeps the underlying TCP connection warm.
                yield ": keep-alive\n\n"
                continue
            if kind == "token":
                token_count += 1
                full_text_parts.append(value)
                yield f"data: {_format_token(value)}\n\n"
            elif kind == "error":
                stream_error = value
            else:  # "end"
                break

        if stream_error and token_count == 0:
            yield f"data: {_format_token('AI 服务暂时不可用：' + stream_error)}\n\n"
        elif stream_error:
            yield f"data: {_format_token('\\n\\n（本轮回答提前结束：' + stream_error + '）')}\n\n"
        elif token_count == 0:
            err = (get_last_ai_error() or "").strip()
            hint = f"（{err}）" if err else ""
            yield f"data: {_format_token('AI 未返回内容，请检查所选模型的 API Key 是否有效。' + hint)}\n\n"
    except Exception as exc:
        yield f"data: {_format_token('\\n\\nAI 服务超时或异常：' + str(exc)[:200])}\n\n"

    # Persist the assistant reply so the conversation survives reloads / device switches.
    assistant_text = "".join(full_text_parts).strip()
    if assistant_text and getattr(request, "session_id", None):
        try:
            chat_store.append_message(
                owner,
                request.session_id,
                "assistant",
                assistant_text,
                tables=data_tables or None,
                instance_id=instance_id,
            )
        except Exception:
            pass

    yield f"data: {_format_done()}\n\n"


@app.get("/api/chat/tools")
def chat_tools() -> list[dict[str, Any]]:
    """Available investigation tool chain for the chat assistant."""
    return [dict(tool) for tool in CHAT_TOOLS]


@app.post("/api/chat")
def chat(http_request: Request, request: ChatRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")):
    owner = _request_owner(http_request, owner_id)
    try:
        _enforce_user_limit(http_request, owner, "daily_ai_chat")
    except HTTPException:
        return JSONResponse({"reply": "今日 AI 对话次数已用完，请明天再试。", "symbols": []})

    msg = (request.message or "").strip()
    if not msg:
        return JSONResponse({"reply": "请输入你想了解的问题。", "symbols": []})

    # Adopt / create the session, then persist the user message. The stored user
    # message is the authoritative source for the daily-chat quota count.
    try:
        session_id = chat_store.ensure_session(
            owner,
            request.session_id,
            provider=request.ai_provider,
            account=request.longbridge_account,
            tools=request.tools,
        )
        request = request.model_copy(update={"session_id": session_id})
        chat_store.append_message(owner, session_id, "user", msg)
    except Exception:
        pass

    return StreamingResponse(
        _chat_event_stream(http_request, request, owner_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/chat/sessions")
def chat_sessions_list(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    return {"sessions": chat_store.list_sessions(owner)}


@app.post("/api/chat/sessions")
def chat_sessions_create(http_request: Request, payload: ChatSessionCreateRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    return chat_store.create_session(
        owner,
        title=payload.title,
        provider=payload.provider,
        account=payload.account,
        tools=payload.tools,
    )


@app.patch("/api/chat/sessions/{session_id}")
def chat_sessions_update(http_request: Request, session_id: str, payload: ChatSessionUpdateRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    patch = payload.model_dump(exclude_unset=True)
    updated = chat_store.update_session(owner, session_id, **patch)
    if updated is None:
        raise HTTPException(status_code=404, detail="session not found")
    return updated


@app.delete("/api/chat/sessions/{session_id}")
def chat_sessions_delete(http_request: Request, session_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, str]:
    owner = _request_owner(http_request, owner_id)
    chat_store.delete_session(owner, session_id)
    return {"status": "ok"}


@app.get("/api/chat/sessions/{session_id}/messages")
def chat_sessions_messages(http_request: Request, session_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    return {"messages": chat_store.list_messages(owner, session_id)}


@app.delete("/api/chat/sessions/{session_id}/messages")
def chat_sessions_clear_messages(http_request: Request, session_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, str]:
    owner = _request_owner(http_request, owner_id)
    chat_store.clear_messages(owner, session_id)
    return {"status": "ok"}

def scan(http_request: Request, request: ScanRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    try:
        _enforce_user_limit(http_request, owner, "daily_scans")
        if request.use_ai:
            _enforce_user_limit(http_request, owner, "daily_ai_scans")
        account_name = _resolve_scan_account(owner, request.market_data_source, request.longbridge_account, http_request)
        if account_name and account_name not in {"yfinance", "thetadata"}:
            request = request.model_copy(update={"longbridge_account": account_name, "market_data_source": "longbridge"})
        elif account_name == "thetadata":
            request = request.model_copy(update={"longbridge_account": "thetadata", "market_data_source": "thetadata"})
        return run_scan(
            query=request.query,
            symbol=request.symbol,
            ai_provider=request.ai_provider,
            longbridge_account=account_name,
            market_data_source=request.market_data_source,
            option_data_source=request.option_data_source,
            use_ai=request.use_ai,
            council=request.council,
            analysis_modules=request.analysis_modules,
            strategy_modes=request.strategy_modes,
            ai_provider_owner=_provider_owner(http_request, owner_id),
            source_type="scan",
        )
    except ValueError as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/analysis-presets")
def analysis_presets() -> list[dict[str, Any]]:
    return analysis_presets_for_ui()


@app.post("/api/scans")
def create_scan(http_request: Request, request: ScanRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    provider_owner = _provider_owner(http_request, owner_id)
    try:
        _enforce_user_limit(http_request, owner, "daily_scans")
        if request.use_ai:
            _enforce_user_limit(http_request, owner, "daily_ai_scans")
        account_name = _resolve_scan_account(owner, request.market_data_source, request.longbridge_account, http_request)
        if account_name and account_name not in {"yfinance", "thetadata"}:
            request = request.model_copy(update={"longbridge_account": account_name, "market_data_source": "longbridge"})
        elif account_name == "thetadata":
            request = request.model_copy(update={"longbridge_account": "thetadata", "market_data_source": "thetadata"})
        elif account_name == "yfinance":
            request = request.model_copy(update={"longbridge_account": "yfinance", "market_data_source": "yfinance"})
        return submit_scan(
            query=request.query,
            symbol=request.symbol,
            ai_provider=request.ai_provider,
            longbridge_account=request.longbridge_account,
            market_data_source=request.market_data_source,
            option_data_source=request.option_data_source,
            use_ai=request.use_ai,
            council=request.council,
            analysis_modules=request.analysis_modules,
            strategy_modes=request.strategy_modes,
            owner_id=owner,
            ai_provider_owner=provider_owner,
        )
    except ValueError as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/scans")
def scan_history(
    http_request: Request,
    limit: int = 30,
    offset: int = 0,
    starred: bool = False,
    query: str | None = None,
    tag: str | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = _request_owner(http_request, owner_id)
    return list_scan_runs_with_marks(owner, limit=limit, offset=offset, starred=starred, query=query, tag=tag)


@app.get("/api/scans/{scan_id}")
def scan_detail(http_request: Request, scan_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    row = get_scan_run(scan_id, owner_id=_request_owner(http_request, owner_id))
    if row is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return row


def _scan_events_stream(scan_id: str):
    """SSE generator for a single scan's status changes. Emits an initial
    snapshot, then forwards pub/sub events as the worker advances the scan,
    with keep-alive ticks. Closes on a terminal status, on a max-duration cap,
    or when Redis is unavailable (client then falls back to polling)."""
    import time as _time

    def _frame(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    snapshot = get_scan_run(scan_id)
    if snapshot is not None:
        status = snapshot.get("status")
        yield _frame({"scan_id": scan_id, "status": status, "stage": snapshot.get("stage"), "progress": snapshot.get("progress")})
        # Already finished before we subscribed — nothing more will be published.
        if status in {"succeeded", "failed"}:
            yield "event: done\ndata: {}\n\n"
            return

    # No Redis → no events will ever arrive; end the stream so the client's
    # EventSource onerror fires and it reverts to polling.
    if not redis_available():
        yield "event: done\ndata: {}\n\n"
        return

    started = _time.monotonic()
    max_seconds = 600  # safety cap; matches the LB backend response timeout
    rechecked = False
    for event in iter_scan_events(scan_id, timeout=1.0):
        if event is None:
            # First idle tick means the subscription is now live. Re-read the DB
            # once to close the subscribe-after-publish race: if the scan reached
            # a terminal state between the initial snapshot and subscribe, that
            # event was published to no one — recover it here instead of waiting
            # for the client's fallback poll.
            if not rechecked:
                rechecked = True
                latest = get_scan_run(scan_id)
                if latest is not None and latest.get("status") in {"succeeded", "failed"}:
                    yield _frame({"scan_id": scan_id, "status": latest.get("status"), "stage": latest.get("stage"), "progress": latest.get("progress")})
                    yield "event: done\ndata: {}\n\n"
                    return
            # Idle tick: keep the connection warm and bail out past the cap.
            yield ": keep-alive\n\n"
            if _time.monotonic() - started > max_seconds:
                return
            continue
        yield _frame(event)
        if event.get("status") in {"succeeded", "failed"}:
            yield "event: done\ndata: {}\n\n"
            return


@app.get("/api/scans/{scan_id}/events")
def scan_events(http_request: Request, scan_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> StreamingResponse:
    # Ownership check up front: a user must not be able to subscribe to another
    # user's scan stream. 404 (not 403) to avoid leaking scan-id existence.
    if get_scan_run(scan_id, owner_id=_request_owner(http_request, owner_id)) is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return StreamingResponse(
        _scan_events_stream(scan_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/scan-marks")
def scan_marks(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_scan_marks(_request_owner(http_request, owner_id))


@app.patch("/api/scans/{scan_id}/mark")
def update_scan_mark(http_request: Request, scan_id: str, payload: ScanMarkRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    if get_scan_run(scan_id, owner_id=owner) is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return mark_scan(owner, scan_id, starred=payload.starred, note=payload.note, tags=payload.tags)


@app.get("/api/notification-channels")
def notification_channels(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_notification_channels(_request_owner(http_request, owner_id))


@app.post("/api/notification-channels")
def add_notification_channel(
    http_request: Request,
    payload: NotificationChannelRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    _enforce_user_limit(http_request, owner, "notification_channels")
    try:
        return create_notification_channel(owner, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/notification-channels/{channel_id}")
def patch_notification_channel(
    http_request: Request,
    channel_id: str,
    payload: NotificationChannelUpdateRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        return update_notification_channel(_request_owner(http_request, owner_id), channel_id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/notification-channels/{channel_id}")
def delete_notification_channel_route(http_request: Request, channel_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        delete_notification_channel(_request_owner(http_request, owner_id), channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True}


@app.post("/api/notification-channels/{channel_id}/test")
def test_notification_channel(http_request: Request, channel_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return send_test_notification_channel(_request_owner(http_request, owner_id), channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc


@app.get("/api/notification-channels/{channel_id}/payload-preview")
def notification_channel_payload_preview(http_request: Request, channel_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    channel = get_notification_channel(owner, channel_id, include_sensitive=True)
    if not channel:
        raise HTTPException(status_code=404, detail="notification channel not found")
    return build_notification_payload_preview(channel)


@app.get("/api/notification-channels/{channel_id}/delivery-logs")
def notification_channel_delivery_logs(
    http_request: Request,
    channel_id: str,
    limit: int = 50,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    return list_notification_delivery_logs(_request_owner(http_request, owner_id), channel_id=channel_id, limit=limit)


@app.get("/api/notification-delivery-logs")
def notification_delivery_logs(
    http_request: Request,
    channel_id: str | None = None,
    event_id: str | None = None,
    limit: int = 100,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    return list_notification_delivery_logs(_request_owner(http_request, owner_id), channel_id=channel_id, event_id=event_id, limit=limit)


@app.get("/api/notification-events")
def notification_events(http_request: Request, limit: int = 50, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_notification_events(_request_owner(http_request, owner_id), limit=limit)


@app.post("/api/notification-events/{event_id}/send")
def send_notification(http_request: Request, event_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return send_notification_event(_request_owner(http_request, owner_id), event_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/notification-events/{event_id}/delivery-logs")
def notification_event_delivery_logs(
    http_request: Request,
    event_id: str,
    limit: int = 50,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    return list_notification_delivery_logs(_request_owner(http_request, owner_id), event_id=event_id, limit=limit)


@app.post("/api/notification-events/process")
def process_notifications(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    return process_notification_events(_request_owner(http_request, owner_id), limit=50)


@app.get("/api/scan-triggers")
def scan_triggers(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_scan_triggers(_request_owner(http_request, owner_id))


@app.post("/api/scan-triggers")
def add_scan_trigger(http_request: Request, payload: ScanTriggerRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return create_scan_trigger(_request_owner(http_request, owner_id), payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/scan-triggers/{trigger_id}")
def patch_scan_trigger(
    http_request: Request,
    trigger_id: str,
    payload: ScanTriggerUpdateRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        changes = {key: value for key, value in payload.model_dump().items() if value is not None}
        return update_scan_trigger(_request_owner(http_request, owner_id), trigger_id, changes)
    except ValueError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 400, detail=str(exc)) from exc


@app.delete("/api/scan-triggers/{trigger_id}")
def remove_scan_trigger(http_request: Request, trigger_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return delete_scan_trigger(_request_owner(http_request, owner_id), trigger_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/scan-triggers/{trigger_id}/check")
def check_trigger(
    http_request: Request,
    trigger_id: str,
    payload: ScanTriggerCheckRequest | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        return check_scan_trigger(
            _request_owner(http_request, owner_id),
            trigger_id,
            current_value=(payload.current_value if payload else None),
            quote_snapshot=(payload.quote_snapshot if payload else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/scan-triggers/{trigger_id}/test")
def test_trigger(
    http_request: Request,
    trigger_id: str,
    payload: ScanTriggerCheckRequest | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        return test_scan_trigger(
            _request_owner(http_request, owner_id),
            trigger_id,
            current_value=(payload.current_value if payload else None),
            quote_snapshot=(payload.quote_snapshot if payload else None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/watchlists")
def watchlists(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_watchlists(_request_owner(http_request, owner_id))


@app.post("/api/watchlists")
def add_watchlist(http_request: Request, payload: WatchlistRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    _enforce_user_limit(http_request, owner, "watchlists")
    try:
        return create_watchlist(owner, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/watchlists/{watchlist_id}")
def patch_watchlist(http_request: Request, watchlist_id: str, payload: WatchlistRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return update_watchlist(_request_owner(http_request, owner_id), watchlist_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/watchlists/{watchlist_id}")
def remove_watchlist(http_request: Request, watchlist_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    return delete_watchlist(_request_owner(http_request, owner_id), watchlist_id)


@app.get("/api/scan-loop-instances")
def scan_loop_instances(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_scan_loop_instances(_request_owner(http_request, owner_id))


@app.post("/api/scan-loop-instances")
def add_scan_loop_instance(http_request: Request, payload: ScanLoopInstanceRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    _enforce_user_limit(http_request, owner, "scan_loop_instances")
    try:
        return create_scan_loop_instance(owner, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/scan-loop-instances/{instance_id}")
def patch_scan_loop_instance(http_request: Request, instance_id: str, payload: ScanLoopInstanceRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return update_scan_loop_instance(_request_owner(http_request, owner_id), instance_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/scan-loop-instances/{instance_id}")
def remove_scan_loop_instance(http_request: Request, instance_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    return delete_scan_loop_instance(_request_owner(http_request, owner_id), instance_id)


@app.post("/api/scan-loop-instances/{instance_id}/run-now")
def run_scan_loop_now(
    http_request: Request,
    instance_id: str,
    payload: ScanLoopRunRequest | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        request = payload or ScanLoopRunRequest()
        return run_scan_loop_instance(
            _request_owner(http_request, owner_id),
            instance_id,
            quote_snapshots=request.quote_snapshots,
            allow_non_regular=request.allow_non_regular,
            submit_scans=request.submit_scans,
            review_only=request.review_only,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/scan-loop-instances/{instance_id}/test-rules")
def test_scan_loop_rules(
    http_request: Request,
    instance_id: str,
    payload: ScanLoopRunRequest | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        request = payload or ScanLoopRunRequest(allow_non_regular=True, submit_scans=False, review_only=True)
        return test_scan_loop_instance(
            _request_owner(http_request, owner_id),
            instance_id,
            quote_snapshots=request.quote_snapshots,
            allow_non_regular=True if request.allow_non_regular is None else request.allow_non_regular,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/scan-loop-instances/{instance_id}/notification-preview")
def scan_loop_notification_preview(
    http_request: Request,
    instance_id: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    instance = get_scan_loop_instance(owner, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="scan loop instance not found")
    channel_ids = instance.get("notification_channel_ids") or []
    channels = [get_notification_channel(owner, channel_id, include_sensitive=True) for channel_id in channel_ids]
    channels = [channel for channel in channels if channel]
    event = {
        "id": "preview",
        "title": f"{instance.get('name') or '循环扫描实例'} 通知预览",
        "body": "SPY 命中预筛并满足提醒条件。此提醒仅用于研究辅助，不构成投资建议、交易建议或收益承诺。",
        "source_type": "scan_loop_run",
        "source_id": instance_id,
        "dedupe_key": f"preview:{instance_id}",
        "created_at": now_et_iso(),
        "payload": {
            "instance_id": instance_id,
            "watchlist_id": instance.get("watchlist_id"),
            "symbol": "SPY",
            "alert_mode": instance.get("alert_mode"),
            "strategy_modes": instance.get("strategy_modes") or [],
        },
    }
    return {
        "instance_id": instance_id,
        "instance_name": instance.get("name"),
        "channel_count": len(channels),
        "channels": [
            {
                "id": channel.get("id"),
                "label": channel.get("label"),
                "provider": (channel.get("config") or {}).get("provider") or channel.get("type"),
                "enabled": bool(channel.get("enabled")),
                "preview": build_notification_payload_preview(channel, event),
            }
            for channel in channels
        ],
        "missing_channel_ids": [channel_id for channel_id in channel_ids if not any(channel.get("id") == channel_id for channel in channels)],
    }


@app.get("/api/scan-loop-instances/{instance_id}/runs")
def scan_loop_runs(http_request: Request, instance_id: str, limit: int = 30, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_scan_loop_runs(_request_owner(http_request, owner_id), instance_id=instance_id, limit=limit)


@app.get("/api/scan-loop-runs/{run_id}")
def scan_loop_run_detail(http_request: Request, run_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    row = get_scan_loop_run(_request_owner(http_request, owner_id), run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="scan loop run not found")
    return row


@app.get("/api/observation-health")
def observation_health(http_request: Request, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    return {
        "status": "ok",
        "market_clock": market_clock(),
        "process": {
            "role": process_role(),
            "web_enabled": web_enabled(),
            "worker_enabled": worker_enabled(),
            "uptime_seconds": round(time.monotonic() - APP_STARTED_AT, 1),
        },
        "scheduler": observation_scheduler_runtime_snapshot(),
        "radar": observation_due_snapshot(owner),
    }


@app.post("/api/observation-health/run-due-cycle")
def run_observation_health_due_cycle(
    http_request: Request,
    payload: ObservationDueCycleRequest | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    request = payload or ObservationDueCycleRequest()
    result = run_observation_due_cycle(
        owner,
        scan_limit=max(1, min(int(request.scan_limit or 5), 20)),
        trigger_limit=max(1, min(int(request.trigger_limit or 20), 100)),
        opportunity_limit=max(1, min(int(request.opportunity_limit or 20), 100)),
    )
    return {
        "result": result,
        "health": {
            "scheduler": observation_scheduler_runtime_snapshot(),
            "radar": observation_due_snapshot(owner),
        },
    }


@app.get("/api/opportunities")
def opportunities(http_request: Request, limit: int = 30, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_opportunities(_request_owner(http_request, owner_id), limit=limit)


@app.post("/api/opportunity-followups/process")
def process_opportunity_followups_route(http_request: Request, payload: OpportunityFollowupProcessRequest | None = None, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    request = payload or OpportunityFollowupProcessRequest()
    return process_due_opportunity_followups(_request_owner(http_request, owner_id), limit=request.limit)


@app.get("/api/opportunities/{opportunity_id}")
def opportunity_detail(http_request: Request, opportunity_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = _request_owner(http_request, owner_id)
    row = get_opportunity(owner, opportunity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="opportunity not found")
    row["scan_loop_instance"] = get_scan_loop_instance(owner, row.get("scan_loop_instance_id") or "") if row.get("scan_loop_instance_id") else None
    row["source_run"] = get_scan_loop_run(owner, row.get("source_id") or "") if row.get("source_type") == "scan_loop_run" and row.get("source_id") else None
    triggers = list_scan_triggers(owner)
    direct_triggers = [trigger for trigger in triggers if trigger.get("opportunity_id") == row.get("id")]
    legacy_triggers = [
        trigger
        for trigger in triggers
        if trigger.get("opportunity_id") != row.get("id")
        and (
            trigger.get("symbol") == row.get("symbol")
            or trigger.get("scan_id") == row.get("scan_id")
            or (trigger.get("condition") or {}).get("symbol") == row.get("symbol")
        )
    ]
    row["linked_triggers"] = (direct_triggers + legacy_triggers)[:12]
    row["notification_events"] = [
        event
        for event in list_notification_events(owner, limit=100)
        if event.get("source_id") == row.get("id")
        or (event.get("payload") or {}).get("opportunity_id") == row.get("id")
        or (event.get("payload") or {}).get("scan_loop_run_id") == row.get("source_id")
    ][:20]
    return row


@app.patch("/api/opportunities/{opportunity_id}")
def patch_opportunity(http_request: Request, opportunity_id: str, payload: OpportunityUpdateRequest, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        data = payload.model_dump(exclude_none=True) if hasattr(payload, "model_dump") else payload.dict(exclude_none=True)
        return update_opportunity(_request_owner(http_request, owner_id), opportunity_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/opportunities/{opportunity_id}/pause")
def pause_opportunity_route(http_request: Request, opportunity_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return pause_opportunity(_request_owner(http_request, owner_id), opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/opportunities/{opportunity_id}/resume")
def resume_opportunity_route(http_request: Request, opportunity_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return resume_opportunity(_request_owner(http_request, owner_id), opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/opportunities/{opportunity_id}/archive")
def archive_opportunity_route(http_request: Request, opportunity_id: str, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        return archive_opportunity(_request_owner(http_request, owner_id), opportunity_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/opportunities/{opportunity_id}/check")
def check_opportunity_route(http_request: Request, opportunity_id: str, payload: OpportunityCheckRequest | None = None, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    try:
        data = payload.model_dump() if payload and hasattr(payload, "model_dump") else payload.dict() if payload else {}
        return check_opportunity_followup(_request_owner(http_request, owner_id), opportunity_id, quote_snapshot=data.get("quote_snapshot"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/opportunities/{opportunity_id}/events")
def opportunity_events(http_request: Request, opportunity_id: str, limit: int = 50, owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> list[dict[str, Any]]:
    return list_opportunity_events(_request_owner(http_request, owner_id), opportunity_id, limit=limit)


@app.get("/api/trading/config")
def trading_config(owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    return _trading_config_with_valid_account(owner, get_trading_config(owner))


@app.put("/api/trading/config")
def update_trading_config(
    request: TradingConfigRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    data = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    if data.get("single_instance_enabled") is None:
        data.pop("single_instance_enabled", None)
    broker = str(data.get("broker") or "longbridge").strip().lower()
    if broker in {"alpaca", "usmart"}:
        if not data.get("broker_account"):
            raise HTTPException(status_code=400, detail=f"{broker} broker_account is required")
        try:
            resolve_broker_account(broker, data.get("broker_account"), owner_id=owner)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not data.get("longbridge_account"):
        data["longbridge_account"] = None
    elif get_account(data["longbridge_account"], owner_id=owner) is None:
        raise HTTPException(status_code=400, detail=f"Longbridge account `{data['longbridge_account']}` does not belong to this workspace")
    return _trading_config_with_valid_account(owner, save_trading_config(owner, data))


@app.post("/api/trading/run-now")
def run_trading_now(owner_id: str = Header(default=None, alias="X-AI-Option-User")) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    config = get_trading_config(owner)
    try:
        readiness = validate_trading_readiness(owner, config, require_ai=False)
        if not readiness.get("ok"):
            raise HTTPException(status_code=400, detail="; ".join(readiness.get("issues") or ["trading readiness check failed"]))
        return start_trading_run(owner, config)
    except TradingRunBlockedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise _account_error(exc) from exc


@app.get("/api/trading/runs")
def list_trading_history(
    limit: int = 20,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    return recent_trading_runs(normalize_owner_id(owner_id), limit)


@app.get("/api/trading/runs/{run_id}")
def get_trading_history(
    run_id: str,
    light: bool = False,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    # light=true omits scan_results/council/selections (entry-time blobs ~50-150 KB) to cut polling cost.
    row = trading_run_detail(run_id, normalize_owner_id(owner_id), light=light)
    if row is None:
        raise HTTPException(status_code=404, detail="交易实例不存在")
    return row


@app.get("/api/trading/runs/{run_id}/review")
def get_trading_run_review(
    run_id: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    review = get_trade_review(run_id, normalize_owner_id(owner_id))
    if review is None:
        raise HTTPException(status_code=404, detail="复盘尚未生成")
    return review


@app.get("/api/trading/reviews")
def list_trading_reviews(
    limit: int = 50,
    status: str | None = None,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    statuses = tuple(s.strip() for s in status.split(",")) if status else None
    return list_recent_trade_reviews(
        owner_id=normalize_owner_id(owner_id),
        limit=limit,
        statuses=statuses,
    )


@app.get("/api/trading/schedule-fires")
def list_trading_schedule_fires(
    limit: int = 30,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    return list_schedule_fires(owner_id=normalize_owner_id(owner_id), limit=limit)


@app.get("/api/trading/ai-quality")
def get_trading_ai_quality(
    limit: int = 50,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    return ai_decision_quality(normalize_owner_id(owner_id), limit)


@app.post("/api/trading/monitor")
def run_trading_monitor_once() -> dict[str, Any]:
    result = monitor_pending_stops()
    return {"status": "ok", **result}


@app.post("/api/trading/flatten")
def flatten_trading_positions(
    request: FlattenPositionsRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    if str(request.confirmation or "").strip() != CONFIRMATION_TEXT:
        raise HTTPException(status_code=400, detail=f"请输入 `{CONFIRMATION_TEXT}` 确认全平")
    owner = normalize_owner_id(owner_id)
    config = get_trading_config(owner)
    try:
        account_name = account_ref_for_config(config, owner_id=owner)
        result = flatten_all_positions(account_name)
    except ValueError as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return result


@app.post("/api/trading/runs/bulk-delete")
def bulk_delete_trading_instances(
    request: InstanceBulkDeleteRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    if str(request.confirmation or "").strip() != INSTANCE_BULK_DELETE_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"请输入 `{INSTANCE_BULK_DELETE_CONFIRMATION}` 确认批量删除实例")
    run_ids = [str(run_id or "").strip() for run_id in (request.run_ids or []) if str(run_id or "").strip()]
    if not run_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个交易实例")
    owner = normalize_owner_id(owner_id)
    try:
        return bulk_delete_trade_instances(run_ids, owner, force=bool(request.force))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading/runs/{run_id}/cancel-orders")
def cancel_trading_instance_orders(
    run_id: str,
    request: InstanceActionRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    if str(request.confirmation or "").strip() != INSTANCE_CANCEL_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"请输入 `{INSTANCE_CANCEL_CONFIRMATION}` 确认撤实例订单")
    owner = normalize_owner_id(owner_id)
    run = trading_run_detail(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404, detail="交易实例不存在")
    try:
        account_name = account_ref_for_config(run.get("config") or {}, owner_id=owner)
        return cancel_trade_instance_orders(run_id, owner, account_name)
    except ValueError as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading/runs/{run_id}/flatten")
def flatten_trading_instance(
    run_id: str,
    request: InstanceActionRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    if str(request.confirmation or "").strip() != INSTANCE_FLATTEN_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"请输入 `{INSTANCE_FLATTEN_CONFIRMATION}` 确认平当前实例")
    owner = normalize_owner_id(owner_id)
    run = trading_run_detail(run_id, owner)
    if run is None:
        raise HTTPException(status_code=404, detail="交易实例不存在")
    try:
        account_name = account_ref_for_config(run.get("config") or {}, owner_id=owner)
        return flatten_trade_instance(run_id, owner, account_name)
    except ValueError as exc:
        raise _account_error(exc) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading/runs/{run_id}/reset-risk")
def reset_trading_instance_risk(
    run_id: str,
    request: InstanceActionRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    if str(request.confirmation or "").strip() != INSTANCE_RISK_RESET_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"请输入 `{INSTANCE_RISK_RESET_CONFIRMATION}` 确认初始化风控")
    owner = normalize_owner_id(owner_id)
    if trading_run_detail(run_id, owner) is None:
        raise HTTPException(status_code=404, detail="交易实例不存在")
    try:
        return reset_trade_instance_risk(run_id, owner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/trading/runs/{run_id}/delete")
def delete_trading_instance(
    run_id: str,
    request: InstanceActionRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    if str(request.confirmation or "").strip() != INSTANCE_DELETE_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"请输入 `{INSTANCE_DELETE_CONFIRMATION}` 确认删除实例")
    owner = normalize_owner_id(owner_id)
    if trading_run_detail(run_id, owner) is None:
        raise HTTPException(status_code=404, detail="交易实例不存在")
    try:
        return delete_trade_instance(run_id, owner, force=bool(request.force))
    except InstanceHasLiveBrokerStateError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "reason": "live_broker_state",
                "live_broker_state": exc.live_state,
                "force_required": True,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Auto-Trade (全自动交易): autonomous LLM trading instances. can_trade is enforced
# by the middleware path-prefix guard above; the danger of a real-broker start is
# additionally gated by a typed confirmation.
# ---------------------------------------------------------------------------
AUTO_TRADE_START_CONFIRMATION = "全自动交易"


def _auto_trade_instance_view(instance: dict[str, Any]) -> dict[str, Any]:
    out = dict(instance)
    out["caps"] = preset_caps(str(instance.get("risk_preset") or "conservative"))
    return out


def _get_owned_auto_trade_instance(instance_id: str, owner: str) -> dict[str, Any]:
    instance = get_auto_trade_instance(instance_id, owner)
    if instance is None:
        raise HTTPException(status_code=404, detail="自动交易实例不存在")
    return instance


@app.get("/api/auto-trade/instances")
def list_auto_trade(
    limit: int = 50,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    return [_auto_trade_instance_view(i) for i in list_auto_trade_instances(owner, limit=limit)]


@app.post("/api/auto-trade/instances")
def create_auto_trade(
    request: AutoTradeInstanceRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    if not payload.get("symbols"):
        raise HTTPException(status_code=400, detail="至少需要 1 个标的")
    return _auto_trade_instance_view(create_auto_trade_instance(owner, payload))


@app.get("/api/auto-trade/instances/{instance_id}")
def get_auto_trade(
    instance_id: str,
    cycles: int = 20,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    instance = _get_owned_auto_trade_instance(instance_id, owner)
    view = _auto_trade_instance_view(instance)
    view["cycles"] = list_auto_trade_cycles(instance_id, owner, limit=cycles)
    return view


@app.put("/api/auto-trade/instances/{instance_id}")
def update_auto_trade(
    instance_id: str,
    request: AutoTradeInstanceRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    _get_owned_auto_trade_instance(instance_id, owner)
    payload = request.model_dump() if hasattr(request, "model_dump") else request.dict()
    if not payload.get("symbols"):
        raise HTTPException(status_code=400, detail="至少需要 1 个标的")
    updated = update_auto_trade_instance(instance_id, owner, payload)
    return _auto_trade_instance_view(updated)


@app.delete("/api/auto-trade/instances/{instance_id}")
def remove_auto_trade(
    instance_id: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    _get_owned_auto_trade_instance(instance_id, owner)
    delete_auto_trade_instance(instance_id, owner)
    return {"status": "ok", "deleted": instance_id}


@app.post("/api/auto-trade/instances/{instance_id}/start")
def start_auto_trade(
    instance_id: str,
    request: AutoTradeStartRequest,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    instance = _get_owned_auto_trade_instance(instance_id, owner)
    # Starting a real-broker instance places live orders unattended — require a
    # typed confirmation. Dry-run instances (no broker) start without it.
    if instance.get("use_broker") and str(request.confirmation or "").strip() != AUTO_TRADE_START_CONFIRMATION:
        raise HTTPException(status_code=400, detail=f"实盘自动交易：请输入 `{AUTO_TRADE_START_CONFIRMATION}` 确认启动")
    # next_run_at=None makes it immediately due, so the scheduler picks it up on
    # the next tick (the cycle engine then gates on market session itself).
    updated = update_auto_trade_instance(instance_id, owner, {"status": "active", "next_run_at": None})
    return _auto_trade_instance_view(updated)


@app.post("/api/auto-trade/instances/{instance_id}/pause")
def pause_auto_trade(
    instance_id: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    _get_owned_auto_trade_instance(instance_id, owner)
    return _auto_trade_instance_view(update_auto_trade_instance(instance_id, owner, {"status": "paused"}))


@app.post("/api/auto-trade/instances/{instance_id}/stop")
def stop_auto_trade(
    instance_id: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    _get_owned_auto_trade_instance(instance_id, owner)
    return _auto_trade_instance_view(update_auto_trade_instance(instance_id, owner, {"status": "stopped", "next_run_at": None}))


@app.get("/api/auto-trade/instances/{instance_id}/cycles")
def list_auto_trade_cycle_history(
    instance_id: str,
    limit: int = 50,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> list[dict[str, Any]]:
    owner = normalize_owner_id(owner_id)
    _get_owned_auto_trade_instance(instance_id, owner)
    return list_auto_trade_cycles(instance_id, owner, limit=limit)


@app.get("/api/auto-trade/instances/{instance_id}/cycles/{cycle_id}")
def get_auto_trade_cycle_detail(
    instance_id: str,
    cycle_id: str,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    owner = normalize_owner_id(owner_id)
    cycle = get_auto_trade_cycle(cycle_id, owner)
    if cycle is None or str(cycle.get("instance_id")) != instance_id:
        raise HTTPException(status_code=404, detail="扫描周期不存在")
    # Attach the linked trading run (full lifecycle: orders, journal, protection).
    run_ids = cycle.get("run_ids") or []
    runs = []
    for rid in run_ids:
        detail = trading_run_detail(str(rid), owner)
        if detail is not None:
            runs.append(detail)
    cycle["runs"] = runs
    return cycle


@app.get("/api/trading/snapshots")
def get_trading_snapshots(
    days: int = 30,
    refresh: bool = False,
    owner_id: str = Header(default=None, alias="X-AI-Option-User"),
) -> dict[str, Any]:
    try:
        return trading_snapshots(normalize_owner_id(owner_id), days=days, refresh=refresh)
    except ValueError as exc:
        raise _account_error(exc) from exc


def _trading_config_with_valid_account(owner: str, config: dict[str, Any]) -> dict[str, Any]:
    broker = str(config.get("broker") or "longbridge").strip().lower()
    if broker in {"alpaca", "usmart"}:
        account_name = config.get("broker_account")
        if account_name:
            try:
                resolve_broker_account(broker, account_name, owner_id=owner)
                return config
            except ValueError:
                config["broker_account"] = None
                return config
        accounts = broker_accounts_as_rows(owner, broker=broker)
        config["broker_account"] = accounts[0]["name"] if accounts else None
        config["_requires_save"] = bool(accounts)
        return config
    account_name = config.get("longbridge_account")
    if account_name and get_account(account_name, owner_id=owner) is not None:
        return config
    accounts = accounts_as_rows(owner)
    if account_name:
        config["longbridge_account"] = None
        return config
    config["longbridge_account"] = accounts[0]["name"] if accounts else None
    config["_requires_save"] = bool(accounts)
    return config


def _scan_uses_longbridge(source: str | None, account_name: str | None) -> bool:
    return str(source or "thetadata").strip().lower() == "longbridge"


def _resolve_scan_account(owner: str, source: str | None, account_name: str | None, request: Request) -> str | None:
    normalized = str(source or "thetadata").strip().lower()
    if normalized == "yfinance":
        return "yfinance"
    if normalized == "auto":
        return "thetadata"
    if normalized == "thetadata":
        return "thetadata"
    if normalized == "longbridge":
        _ensure_longbridge_scan_permission(request)
        resolved = _preferred_longbridge_account(owner, account_name)
        if resolved is None:
            raise HTTPException(status_code=400, detail="No Longbridge API account with SDK credentials is available")
        return resolved
    return "thetadata"


def _preferred_longbridge_account(owner: str, account_name: str | None) -> str | None:
    if account_name:
        account = get_account(account_name, owner_id=owner)
        if account is not None and account.sdk_credentials_configured:
            return account.name
    account = preferred_sdk_account(owner)
    return account.name if account is not None else None


def _ensure_longbridge_scan_permission(request: Request) -> None:
    permissions = auth_user_permissions(_current_username(request))
    if not permissions["can_trade"]:
        raise HTTPException(status_code=403, detail="trade permission required for Longbridge market data")


if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


# Service worker must never be cached by the browser OR an intermediary CDN, or a
# new deploy's SW (and the navigation/chunk fixes it carries) won't reach clients
# for the duration of the stale cache — leaving them on a buggy SW that serves a
# stale index.html referencing purged asset hashes (blank/unstyled page). Cloudflare
# ignores a bare no-cache for static .js, so we also send CDN-Cache-Control: no-store.
_SW_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "CDN-Cache-Control": "no-store",
    "Cloudflare-CDN-Cache-Control": "no-store",
}


@app.middleware("http")
async def _static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def _web_dist_file(filename: str, media_type: str) -> FileResponse:
    path = WEB_DIST / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    return FileResponse(path, media_type=media_type)


def _web_dist_head(filename: str, media_type: str) -> Response:
    path = WEB_DIST / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{filename} not found")
    return Response(media_type=media_type)


@app.get("/logo.svg", include_in_schema=False)
def site_logo() -> FileResponse:
    return _web_dist_file("logo.svg", "image/svg+xml")


@app.head("/logo.svg", include_in_schema=False)
def site_logo_head() -> Response:
    return _web_dist_head("logo.svg", "image/svg+xml")


@app.get("/logo.png", include_in_schema=False)
def site_logo_png() -> FileResponse:
    return _web_dist_file("logo.png", "image/png")


@app.head("/logo.png", include_in_schema=False)
def site_logo_png_head() -> Response:
    return _web_dist_head("logo.png", "image/png")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico() -> FileResponse:
    return _web_dist_file("favicon.ico", "image/x-icon")


@app.head("/favicon.ico", include_in_schema=False)
def favicon_ico_head() -> Response:
    return _web_dist_head("favicon.ico", "image/x-icon")


@app.get("/favicon-16x16.png", include_in_schema=False)
def favicon_16() -> FileResponse:
    return _web_dist_file("favicon-16x16.png", "image/png")


@app.head("/favicon-16x16.png", include_in_schema=False)
def favicon_16_head() -> Response:
    return _web_dist_head("favicon-16x16.png", "image/png")


@app.get("/favicon-32x32.png", include_in_schema=False)
def favicon_32() -> FileResponse:
    return _web_dist_file("favicon-32x32.png", "image/png")


@app.head("/favicon-32x32.png", include_in_schema=False)
def favicon_32_head() -> Response:
    return _web_dist_head("favicon-32x32.png", "image/png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon() -> FileResponse:
    return _web_dist_file("apple-touch-icon.png", "image/png")


@app.head("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon_head() -> Response:
    return _web_dist_head("apple-touch-icon.png", "image/png")


@app.get("/icon-192.png", include_in_schema=False)
def pwa_icon_192() -> FileResponse:
    return _web_dist_file("icon-192.png", "image/png")


@app.head("/icon-192.png", include_in_schema=False)
def pwa_icon_192_head() -> Response:
    return _web_dist_head("icon-192.png", "image/png")


@app.get("/icon-512.png", include_in_schema=False)
def pwa_icon_512() -> FileResponse:
    return _web_dist_file("icon-512.png", "image/png")


@app.head("/icon-512.png", include_in_schema=False)
def pwa_icon_512_head() -> Response:
    return _web_dist_head("icon-512.png", "image/png")


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    manifest = WEB_DIST / "manifest.webmanifest"
    if not manifest.exists():
        raise HTTPException(status_code=404, detail="manifest not found")
    return FileResponse(manifest, media_type="application/manifest+json")


@app.head("/manifest.webmanifest", include_in_schema=False)
def web_manifest_head() -> Response:
    manifest = WEB_DIST / "manifest.webmanifest"
    if not manifest.exists():
        raise HTTPException(status_code=404, detail="manifest not found")
    return Response(media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    worker = WEB_DIST / "sw.js"
    if not worker.exists():
        raise HTTPException(status_code=404, detail="service worker not found")
    return FileResponse(
        worker,
        media_type="application/javascript",
        headers=_SW_NO_CACHE_HEADERS,
    )


@app.head("/sw.js", include_in_schema=False)
def service_worker_head() -> Response:
    worker = WEB_DIST / "sw.js"
    if not worker.exists():
        raise HTTPException(status_code=404, detail="service worker not found")
    return Response(
        media_type="application/javascript",
        headers=_SW_NO_CACHE_HEADERS,
    )


@app.get("/.well-known/apple-developer-domain-association.txt", include_in_schema=False)
def apple_domain_association() -> Response:
    """Serve Apple's "Sign in with Apple" domain verification file.

    Apple requires this file at exactly this path before it will activate a
    Services ID for a web domain.  The file content is provided by Apple and is
    NOT a secret, but it is deployment-specific, so it is supplied out-of-band via
    APPLE_DOMAIN_ASSOCIATION_FILE (a path) rather than committed to the repo."""
    file_path = str(os.getenv("APPLE_DOMAIN_ASSOCIATION_FILE") or "").strip()
    if not file_path or not Path(file_path).is_file():
        raise HTTPException(status_code=404, detail="apple domain association not configured")
    return FileResponse(Path(file_path), media_type="text/plain")


if web_enabled() and not redis_available():
    mark_interrupted_scan_runs("server restarted before scan finished; please run the scan again")
    mark_interrupted_trading_runs("服务重启时交易实例尚未结束；重试前请先核对券商侧订单和持仓")
if worker_enabled():
    if _env_bool("AI_OPTION_ENABLE_TRADING_SCHEDULER", False):
        start_trading_scheduler()
    if _env_bool("AI_OPTION_ENABLE_ORDER_MONITOR", False):
        start_order_monitor()
    if _env_bool("AI_OPTION_ENABLE_POST_MORTEM_WORKER", True):
        start_post_mortem_worker()


@app.get("/{path:path}")
def spa(path: str) -> FileResponse:
    index = WEB_DIST / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Frontend has not been built. Run npm run build in web/.")
    return FileResponse(index, headers={"Cache-Control": "no-store"})

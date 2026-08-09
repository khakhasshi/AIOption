"""AI-driven post-mortem of a closed trading run.

This module extracts a compact, deterministic "facts" snapshot from a
closed trading_run row (entry decision, gate flags, exit reason, PnL,
holding time), feeds it to the existing AI client, and returns a
structured review (score, what went right/wrong, lessons, suggestions).

Worker integration lives in `post_mortem_worker.py`. This file is
intentionally pure — it does not write to the database.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from .ai_client import ai_usage_context, ask_ai, get_last_ai_error
from .ai_decision_guard import JSON_RESPONSE_FORMAT, extract_json_object
from .ai_providers import DEFAULT_PROVIDER_NAME


SYSTEM_PROMPT = (
    "你是资深期权交易复盘教练。给定一笔已结束的交易实例的事实卡片，"
    "用简洁中文输出 JSON 复盘报告，帮助交易员下次做得更好。"
    "只能引用事实卡片里的信息，不要编造数据。"
)

USER_INSTRUCTIONS = (
    "请基于 facts 输出一份结构化复盘，严格使用以下 JSON schema：\n"
    "{\n"
    "  \"verdict\": \"win|loss|breakeven|inconclusive\",\n"
    "  \"score\": 0-100 的整数,\n"
    "  \"summary\": \"一句话总结这笔交易\",\n"
    "  \"what_went_right\": [\"...\"],\n"
    "  \"what_went_wrong\": [\"...\"],\n"
    "  \"lessons\": [\"...\"],\n"
    "  \"suggested_changes\": [\"...\"]\n"
    "}\n"
    "score 越高表示这笔交易的【执行+决策】质量越好（不仅看 PnL）。"
    "what_went_right/wrong/lessons/suggested_changes 每项 ≤ 30 字，最多各 4 条。"
)


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def _coerce_int(value: Any) -> int | None:
    v = _coerce_float(value)
    return int(v) if v is not None else None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _holding_minutes(orders: list[dict[str, Any]]) -> int | None:
    earliest_entry: datetime | None = None
    latest_exit: datetime | None = None
    for order in orders or []:
        entry_dt = _parse_dt(order.get("entry_time") or order.get("entry_executed_at"))
        if entry_dt and (earliest_entry is None or entry_dt < earliest_entry):
            earliest_entry = entry_dt
        for key in ("exit_filled_at", "single_leg_exit_filled_at", "software_stop_filled_at"):
            exit_dt = _parse_dt(order.get(key))
            if exit_dt and (latest_exit is None or exit_dt > latest_exit):
                latest_exit = exit_dt
    if not earliest_entry or not latest_exit or latest_exit < earliest_entry:
        return None
    delta = latest_exit - earliest_entry
    return int(delta.total_seconds() // 60)


def _exit_reason_from_orders(orders: list[dict[str, Any]]) -> str | None:
    for order in orders or []:
        for key in (
            "single_leg_smart_exit_reason",
            "smart_exit_reason",
            "exit_source",
            "residual_leg_exit_source",
        ):
            value = order.get(key)
            if value:
                return str(value)
    return None


def _realized_pnl(instance: dict[str, Any]) -> float | None:
    metrics = instance.get("review_metrics") or {}
    for key in ("realized_pnl", "strategy_realized_pnl", "estimated_total_pnl"):
        value = _coerce_float(metrics.get(key))
        if value is not None:
            return value
    return None


def _return_pct(instance: dict[str, Any]) -> float | None:
    metrics = instance.get("review_metrics") or {}
    return _coerce_float(metrics.get("return_pct"))


def _compact_advisor_reports(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for report in (reports or [])[:5]:
        if not isinstance(report, dict):
            continue
        compact.append(
            {
                "advisor": report.get("advisor") or report.get("key"),
                "conviction": _coerce_float(report.get("conviction_score"))
                or _coerce_float(report.get("conviction")),
                "selected_count": _coerce_int(report.get("selection_count")),
                "summary": (report.get("summary") or "")[:240],
            }
        )
    return compact


def _compact_event_timeline(events: list[dict[str, Any]], max_events: int = 12) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for event in (events or [])[-max_events:]:
        if not isinstance(event, dict):
            continue
        compact.append(
            {
                "time": event.get("time"),
                "type": event.get("event_type"),
                "lifecycle_state": event.get("lifecycle_state"),
                "message": (event.get("message") or "")[:200],
            }
        )
    return compact


def _compact_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for position in (positions or [])[:6]:
        if not isinstance(position, dict):
            continue
        compact.append(
            {
                "contract_symbol": position.get("contract_symbol"),
                "action": position.get("action"),
                "side": position.get("side"),
                "strike": _coerce_float(position.get("strike")),
                "expiration": position.get("expiration"),
                "units": _coerce_int(position.get("units") or position.get("quantity")),
                "entry_price": _coerce_float(position.get("entry_price")),
                "allocation_pct": _coerce_float(position.get("allocation_pct")),
                "realized_pnl": _coerce_float(position.get("realized_pnl")),
                "exit_price": _coerce_float(position.get("exit_price")),
            }
        )
    return compact


def build_facts_from_run(run: dict[str, Any]) -> dict[str, Any]:
    """Extract a compact, deterministic snapshot from a closed run.

    Output is small enough to fit in any prompt budget and contains only
    information the AI needs to write a useful review.
    """
    instance = run.get("trade_instance") or {}
    config = run.get("config") or {}
    orders = run.get("orders") or []
    ai_decision = instance.get("ai_decision") or {}
    risk_plan = instance.get("risk_plan") or {}
    review_metrics = instance.get("review_metrics") or {}

    return {
        "run_id": run.get("id"),
        "locator_id": run.get("locator_id") or config.get("locator_id"),
        "owner_id": run.get("owner_id"),
        "lifecycle_state": instance.get("lifecycle_state"),
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "config": {
            "strategy_modes": config.get("strategy_modes"),
            "entry_order_type": config.get("entry_order_type"),
            "ai_provider": config.get("ai_provider"),
            "council": config.get("council"),
            "total_capital": _coerce_float(config.get("total_capital")),
            "default_stop_loss_pct": _coerce_float(config.get("default_stop_loss_pct")),
            "default_take_profit_pct": _coerce_float(config.get("default_take_profit_pct")),
            "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled")),
            "ai_adjust_allocation": bool(config.get("ai_adjust_allocation")),
        },
        "ai_decision": {
            "council_mode": ai_decision.get("council_mode"),
            "selection_count": _coerce_int(ai_decision.get("selection_count")),
            "selected_contracts": (ai_decision.get("selected_contracts") or [])[:10],
            "rejected_count": _coerce_int(ai_decision.get("rejected_count")),
            "advisor_reports": _compact_advisor_reports(ai_decision.get("advisor_reports") or []),
        },
        "risk_plan": {
            "total_planned_capital": _coerce_float(risk_plan.get("total_planned_capital")),
            "planned_contracts": _coerce_int(risk_plan.get("planned_contracts")),
            "planned_premium_at_risk": _coerce_float(risk_plan.get("planned_premium_at_risk")),
            "max_loss_if_all_premiums_lost": _coerce_float(risk_plan.get("max_loss_if_all_premiums_lost")),
            "positions": _compact_positions(risk_plan.get("positions") or risk_plan.get("strategy_positions") or []),
        },
        "metrics": {
            "realized_pnl": _coerce_float(review_metrics.get("realized_pnl")),
            "estimated_total_pnl": _coerce_float(review_metrics.get("estimated_total_pnl")),
            "return_pct": _coerce_float(review_metrics.get("return_pct")),
            "entry_cost": _coerce_float(review_metrics.get("entry_cost")),
            "max_unrealized_profit": _coerce_float(review_metrics.get("max_unrealized_profit")),
            "max_drawdown": _coerce_float(review_metrics.get("max_drawdown")),
            "win_loss": review_metrics.get("win_loss"),
            "first_exit_trigger": review_metrics.get("first_exit_trigger"),
            "holding_minutes": _coerce_int(
                review_metrics.get("holding_minutes") or _holding_minutes(orders)
            ),
            "exit_reason": _exit_reason_from_orders(orders),
        },
        "event_timeline": _compact_event_timeline(instance.get("event_timeline") or []),
    }


def _review_pnl_threshold() -> float:
    try:
        return abs(float(os.getenv("AI_OPTION_POST_MORTEM_PNL_THRESHOLD") or 50))
    except (TypeError, ValueError):
        return 50.0


def _review_holding_min_threshold() -> int:
    try:
        return max(1, int(os.getenv("AI_OPTION_POST_MORTEM_HOLDING_MIN") or 60))
    except (TypeError, ValueError):
        return 60


def should_skip_review(facts: dict[str, Any]) -> str | None:
    """Return a reason string when the trade is too trivial to review.

    Skip rules (any one is enough): no positions submitted, realized_pnl
    missing AND nothing happened.
    """
    positions = (facts.get("risk_plan") or {}).get("positions") or []
    if not positions:
        return "no_positions_submitted"
    metrics = facts.get("metrics") or {}
    pnl = metrics.get("realized_pnl")
    holding = metrics.get("holding_minutes") or 0
    if pnl is None and not facts.get("event_timeline"):
        return "no_pnl_and_no_events"
    if (
        pnl is not None
        and abs(pnl) < _review_pnl_threshold()
        and holding < _review_holding_min_threshold()
    ):
        return "below_pnl_and_holding_thresholds"
    return None


def generate_review(
    facts: dict[str, Any],
    *,
    owner_id: str | None,
    provider_name: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call the AI provider and return (review_dict, error).

    On success, error is None. On failure, review_dict is None and error
    contains a short reason.
    """
    provider = provider_name or DEFAULT_PROVIDER_NAME
    payload = {"instructions": USER_INSTRUCTIONS, "facts": facts}
    with ai_usage_context(
        owner_id=owner_id,
        source_type="post_mortem",
        source_id=facts.get("run_id") or "",
        request_role="reviewer",
    ):
        try:
            raw = ask_ai(
                SYSTEM_PROMPT,
                payload,
                provider_name=provider,
                owner_id=owner_id,
                response_format=JSON_RESPONSE_FORMAT,
            )
        except Exception as exc:  # noqa: BLE001 - any provider error becomes a review failure
            return None, f"ai_call_exception: {exc}"
    if not raw:
        return None, get_last_ai_error() or "ai_returned_empty"
    parsed = extract_json_object(raw)
    if not isinstance(parsed, dict):
        return None, "ai_response_not_json_object"
    parsed.setdefault("_raw_preview", str(raw)[:600])
    return parsed, None

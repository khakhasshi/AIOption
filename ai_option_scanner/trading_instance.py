from __future__ import annotations

import re
from datetime import timezone
from typing import Any

from .account_store import utc_now
from .time_utils import parse_datetime


INSTANCE_VERSION = 2
TERMINAL_LIFECYCLE_STATES = {"closed", "reviewed"}
MANUAL_ATTENTION_STATES = {"manual_intervention_required", "stop_failed"}
LIFECYCLE_ORDER = {
    "created": 0,
    "scanning": 10,
    "council_review": 20,
    "approved": 30,
    "submitting": 40,
    "open": 50,
    "protected": 55,
    "monitoring": 60,
    "partial_fill": 62,
    "unprotected": 65,
    "exiting": 70,
    "blocked": 80,
    "stop_failed": 85,
    "manual_intervention_required": 90,
    "closed": 100,
    "reviewed": 110,
}


def create_trade_instance(run_id: str, owner_id: str, config: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    instance = {
        "version": INSTANCE_VERSION,
        "instance_id": run_id,
        "locator_id": config.get("locator_id"),
        "owner_id": owner_id,
        "created_at": now,
        "updated_at": now,
        "lifecycle_state": "created",
        "basic_info": {
            "locator_id": config.get("locator_id"),
            "trigger_source": config.get("trigger_source") or "manual",
            "schedule_profile": config.get("schedule_profile") or "single_run",
            "schedule_session_id": config.get("schedule_session_id"),
            "schedule_slot_id": config.get("schedule_slot_id"),
            "schedule_slot_label": config.get("schedule_slot_label"),
            "schedule_slot_action": config.get("schedule_slot_action"),
            "schedule_slot_gate_profile": config.get("schedule_slot_gate_profile"),
            "schedule_slot_allow_new_positions": bool(config.get("schedule_slot_allow_new_positions", True)),
            "schedule_slot_force_no_overnight": bool(config.get("schedule_slot_force_no_overnight", False)),
            "strategy_mode": "single_leg_option" if config.get("strategy_modes") == ["single_leg"] else "strategy_structure_analysis",
            "strategy_modes": list(config.get("strategy_modes") or ["single_leg"]),
            "broker": config.get("broker") or "longbridge",
            "account_name": config.get("broker_account") or config.get("longbridge_account"),
            "paper_or_live": config.get("trading_environment") or ("live" if config.get("live_enabled") else "paper"),
            "universe": list(config.get("universe") or []),
            "top_n": int(config.get("top_n") or 0),
            "total_capital": _num(config.get("total_capital")),
            "run_time_et": config.get("run_time_et"),
            "entry_order_type": config.get("entry_order_type"),
        },
        "strategy_intent": {
            "prompt_template": config.get("prompt_template"),
            "analysis_modules": dict(config.get("analysis_modules") or {}),
            "ai_provider": config.get("ai_provider"),
            "use_ai": bool(config.get("use_ai", True)),
            "council": bool(config.get("use_ai", True) and config.get("council", True)),
            "ai_adjust_allocation": bool(config.get("ai_adjust_allocation")),
            "ai_adjust_stop_loss": bool(config.get("ai_adjust_stop_loss")),
            "ai_adjust_take_profit": bool(config.get("ai_adjust_take_profit")),
            "software_stop_enabled": bool(config.get("software_stop_enabled", True)),
            "software_take_profit_enabled": bool(config.get("software_take_profit_enabled", True)),
            "default_stop_loss_pct": _num(config.get("default_stop_loss_pct")),
            "default_take_profit_pct": _num(config.get("default_take_profit_pct")),
            "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled", False)),
            "default_take_profit_1_pct": _num(config.get("default_take_profit_1_pct")),
            "default_take_profit_2_pct": _num(config.get("default_take_profit_2_pct")),
            "strategy_modes": list(config.get("strategy_modes") or ["single_leg"]),
        },
        "schedule_context": {
            "enabled": bool(config.get("multi_instance_enabled")),
            "profile_id": config.get("schedule_profile") or "single_run",
            "session_id": config.get("schedule_session_id"),
            "slot_id": config.get("schedule_slot_id"),
            "slot_label": config.get("schedule_slot_label"),
            "slot_time_et": config.get("schedule_slot_time_et"),
            "slot_action": config.get("schedule_slot_action"),
            "gate_profile": config.get("schedule_slot_gate_profile"),
            "allow_new_positions": bool(config.get("schedule_slot_allow_new_positions", True)),
            "force_no_overnight": bool(config.get("schedule_slot_force_no_overnight", False)),
            "trade_date_et": config.get("trade_date_et"),
        },
        "candidate_snapshot": {
            "symbols_scanned": 0,
            "symbols_with_candidates": 0,
            "contract_candidates": 0,
            "strategy_candidates": 0,
            "top_k_per_symbol": 0,
            "hard_flag_counts": {},
            "symbols": [],
        },
        "ai_decision": {
            "council_mode": None,
            "selection_count": 0,
            "selected_contracts": [],
            "rejected_count": 0,
            "post_validation": {},
        },
        "risk_plan": {
            "total_planned_capital": _num(config.get("total_capital")),
            "planned_contracts": 0,
            "planned_premium_at_risk": 0.0,
            "max_loss_if_all_premiums_lost": 0.0,
            "positions": [],
        },
        "execution_plan": {
            "entry_order_type": config.get("entry_order_type"),
            "wait_for_fill_seconds": int(config.get("wait_for_fill_seconds") or 0),
            "software_stop_enabled": bool(config.get("software_stop_enabled", True)),
            "software_take_profit_enabled": bool(config.get("software_take_profit_enabled", True)),
            "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled", False)),
            "orders": [],
        },
        "protection_status": empty_protection_status(),
        "event_timeline": [],
        "review_metrics": empty_review_metrics(),
    }
    append_instance_event(instance, "created", "交易实例已创建", lifecycle_state="created")
    instance["analysis_trace"] = build_instance_analysis_trace(instance)
    return instance


def hydrate_trade_instance(
    raw_instance: Any,
    *,
    run_id: str,
    owner_id: str,
    config: dict[str, Any] | None = None,
    orders: list[dict[str, Any]] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Upgrade legacy instance JSON into the current versioned shape."""
    config = dict(config or {})
    raw = raw_instance if isinstance(raw_instance, dict) else {}
    raw_basic = raw.get("basic_info") if isinstance(raw.get("basic_info"), dict) else {}
    if raw_basic.get("trigger_source") and not config.get("trigger_source"):
        config["trigger_source"] = raw_basic.get("trigger_source")
    if raw_basic.get("paper_or_live") and not config.get("trading_environment"):
        config["trading_environment"] = raw_basic.get("paper_or_live")

    base = create_trade_instance(run_id, owner_id, config)
    if created_at:
        base["created_at"] = created_at
        base["updated_at"] = raw.get("updated_at") or created_at
    instance = _deep_merge(base, raw)
    instance["version"] = INSTANCE_VERSION
    instance["instance_id"] = instance.get("instance_id") or run_id
    instance["owner_id"] = instance.get("owner_id") or owner_id

    instance["protection_status"] = _deep_merge(empty_protection_status(), instance.get("protection_status") or {})
    instance["review_metrics"] = _deep_merge(empty_review_metrics(), instance.get("review_metrics") or {})
    if orders:
        orders = sanitize_instance_orders(orders)
        instance["protection_status"] = build_protection_status(orders)
        update_review_metrics(instance, orders)
        if _all_orders_terminal_without_fill(orders):
            instance["lifecycle_state"] = "blocked"
    instance["analysis_trace"] = build_instance_analysis_trace(instance)
    return instance


def sanitize_instance_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for order in orders:
        item = dict(order)
        if _entry_confirmed_no_fill(item):
            invalid_flatten = item.get("instance_flatten_order") or int(_num(item.get("instance_flatten_closed_quantity"))) > 0
            if invalid_flatten:
                if item.get("instance_flatten_order"):
                    item["ignored_instance_flatten_order"] = item.pop("instance_flatten_order")
                item["instance_flatten_ignored_reason"] = "entry order was not filled; legacy instance flatten record ignored"
                item["instance_flatten_closed_quantity"] = 0
                item["instance_flatten_submitted_quantity"] = 0
                item["software_stop_active"] = False
                item["software_take_profit_active"] = False
                item["software_stop_quantity"] = 0
                item["software_take_profit_quantity"] = 0
            item["status"] = "entry_terminal_no_stop"
            item.pop("monitor_status", None)
        sanitized.append(item)
    return sanitized


def empty_protection_status() -> dict[str, Any]:
    return {
        "state": "not_started",
        "broker_stop_submitted": 0,
        "software_stop_active": False,
        "software_protected_quantity": 0,
        "software_take_profit_active": False,
        "software_take_profit_quantity": 0,
        "single_leg_smart_exit_active": False,
        "single_leg_smart_exit_quantity": 0,
        "strategy_tracked_quantity": 0,
        "strategy_exit_submitted_quantity": 0,
        "unprotected_quantity": 0,
        "protected_quantity": 0,
        "stop_failure_reason": None,
        "requires_manual_attention": False,
        "contracts": [],
    }


def empty_review_metrics() -> dict[str, Any]:
    return {
        "mfe": None,
        "mae": None,
        "max_unrealized_profit": None,
        "max_drawdown": None,
        "current_unrealized_pnl": None,
        "estimated_total_pnl": None,
        "return_pct": None,
        "entry_cost": None,
        "open_quantity": 0,
        "closed_quantity": 0,
        "holding_minutes": None,
        "first_exit_trigger": None,
        "realized_pnl": None,
        "strategy_realized_pnl": None,
        "strategy_unrealized_pnl": None,
        "strategy_trigger_mark_pnl": None,
        "win_loss": None,
        "ai_confidence_avg": None,
        "ai_confidence_vs_return": None,
    }


def set_lifecycle(instance: dict[str, Any], state: str) -> dict[str, Any]:
    instance["lifecycle_state"] = next_lifecycle_state(str(instance.get("lifecycle_state") or ""), state)
    instance["updated_at"] = utc_now()
    return instance


def next_lifecycle_state(current: str, requested: str) -> str:
    current = str(current or "")
    requested = str(requested or current or "created")
    if current in TERMINAL_LIFECYCLE_STATES and requested not in TERMINAL_LIFECYCLE_STATES:
        return current
    if current in MANUAL_ATTENTION_STATES and requested in {"open", "protected", "monitoring", "partial_fill", "unprotected"}:
        return current
    if current == "exiting" and requested in {"open", "protected", "monitoring", "partial_fill", "unprotected"}:
        return current
    return requested


def append_instance_event(
    instance: dict[str, Any],
    event_type: str,
    message: str,
    *,
    lifecycle_state: str | None = None,
    status: str = "info",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if lifecycle_state:
        instance["lifecycle_state"] = next_lifecycle_state(str(instance.get("lifecycle_state") or ""), lifecycle_state)
    event = {
        "time": utc_now(),
        "event_type": event_type,
        "status": status,
        "lifecycle_state": instance.get("lifecycle_state"),
        "message": message,
        "payload": payload or {},
    }
    timeline = instance.setdefault("event_timeline", [])
    if isinstance(timeline, list):
        timeline.append(event)
        instance["event_timeline"] = timeline[-120:]
    else:
        instance["event_timeline"] = [event]
    instance["updated_at"] = event["time"]
    instance["analysis_trace"] = build_instance_analysis_trace(instance)
    return instance


def build_instance_analysis_trace(instance: dict[str, Any]) -> dict[str, Any]:
    basic = instance.get("basic_info") or {}
    intent = instance.get("strategy_intent") or {}
    snapshot = instance.get("candidate_snapshot") or {}
    decision = instance.get("ai_decision") or {}
    risk = instance.get("risk_plan") or {}
    execution = instance.get("execution_plan") or {}
    protection = instance.get("protection_status") or {}
    events = instance.get("event_timeline") if isinstance(instance.get("event_timeline"), list) else []
    advisors = decision.get("advisor_reports") if isinstance(decision.get("advisor_reports"), list) else []
    ai_execution = decision.get("ai_execution") if isinstance(decision.get("ai_execution"), dict) else {}
    stages = [
        {
            "key": "instance_intent",
            "title": "实例意图",
            "status": "passed",
            "summary": f"{basic.get('trigger_source') or '--'} · {', '.join(basic.get('strategy_modes') or intent.get('strategy_modes') or []) or '--'}",
            "items": [
                {"label": "账号", "value": basic.get("account_name") or "--"},
                {"label": "环境", "value": basic.get("paper_or_live") or "--"},
                {"label": "股票池", "value": len(basic.get("universe") or [])},
                {"label": "Top N", "value": basic.get("top_n") or "--"},
                {"label": "资金上限", "value": basic.get("total_capital") or "--"},
                {"label": "入场方式", "value": basic.get("entry_order_type") or "--"},
            ],
            "notes": _trace_notes([intent.get("prompt_template")], 2),
        },
        {
            "key": "scan_snapshot",
            "title": "候选生成",
            "status": "passed" if _num(snapshot.get("contract_candidates")) or _num(snapshot.get("strategy_candidates")) else "warning",
            "summary": f"扫描 {snapshot.get('symbols_scanned') or 0} 个标的，单腿 {snapshot.get('contract_candidates') or 0}，结构 {snapshot.get('strategy_candidates') or 0}",
            "items": [
                {"label": "扫描标的", "value": snapshot.get("symbols_scanned") or 0},
                {"label": "有候选", "value": snapshot.get("symbols_with_candidates") or 0},
                {"label": "单腿候选", "value": snapshot.get("contract_candidates") or 0},
                {"label": "策略候选", "value": snapshot.get("strategy_candidates") or 0},
                {"label": "Top K", "value": snapshot.get("top_k_per_symbol") or 0},
                {"label": "完整性过滤", "value": snapshot.get("data_integrity_rejected_count") or 0},
            ],
            "notes": _trace_notes([
                f"剔除 {snapshot.get('data_integrity_rejected_count')} 个 root 与股票池不一致的候选。"
                if snapshot.get("data_integrity_rejected_count") else ""
            ], 2),
        },
        {
            "key": "advisor_decision",
            "title": "AI 决策",
            "status": "passed" if _num(decision.get("selection_count")) else "warning" if ai_execution.get("requested") else "skipped",
            "summary": decision.get("summary") or "等待 AI 决策或规则筛选完成。",
            "items": [
                {"label": "AI 请求", "value": "是" if ai_execution.get("requested") else "否"},
                {"label": "AI 调用", "value": "是" if ai_execution.get("attempted") else "否"},
                {"label": "顾问返回", "value": f"{ai_execution.get('advisor_success_count', len(advisors))}/{ai_execution.get('advisor_count', len(advisors))}"},
                {"label": "主持人 JSON", "value": "是" if ai_execution.get("moderator_json_valid") else "否"},
                {"label": "模式", "value": decision.get("council_mode") or "--"},
                {"label": "入选", "value": decision.get("selection_count") or 0},
                {"label": "拒绝", "value": decision.get("rejected_count") or 0},
                {"label": "顾问", "value": len(advisors)},
                {"label": "校验", "value": (decision.get("post_validation") or {}).get("status") or "--"},
            ],
            "notes": _trace_notes([ai_execution.get("fallback_reason"), *(decision.get("risk_notes") or []), (decision.get("post_validation") or {}).get("reason")], 8),
        },
        {
            "key": "risk_execution",
            "title": "风控与执行",
            "status": "passed" if _num(risk.get("planned_contracts")) or _num(risk.get("strategy_tracking_count")) else "warning",
            "summary": f"计划 {risk.get('planned_contracts') or risk.get('strategy_tracking_count') or 0} 个仓位，保护状态 {protection.get('state') or '--'}",
            "items": [
                {"label": "计划风险", "value": risk.get("planned_premium_at_risk") or 0},
                {"label": "最大亏损", "value": risk.get("max_loss_if_all_premiums_lost") or 0},
                {"label": "保护状态", "value": protection.get("state") or "--"},
                {"label": "未保护数量", "value": protection.get("unprotected_quantity") or 0},
                {"label": "软件止损", "value": "开" if execution.get("software_stop_enabled") else "关"},
                {"label": "软件止盈", "value": "开" if execution.get("software_take_profit_enabled") else "关"},
            ],
            "notes": _trace_notes([protection.get("stop_failure_reason")], 4),
        },
        {
            "key": "event_timeline",
            "title": "生命周期事件",
            "status": "passed" if events else "skipped",
            "summary": f"{len(events)} 条事件，当前 {instance.get('lifecycle_state') or '--'}",
            "items": [
                {"label": "当前状态", "value": instance.get("lifecycle_state") or "--"},
                {"label": "事件数", "value": len(events)},
                {"label": "最后事件", "value": (events[-1] or {}).get("event_type") if events else "--"},
                {"label": "更新时间", "value": instance.get("updated_at") or "--"},
            ],
            "notes": _trace_notes([event.get("message") for event in events[-6:] if isinstance(event, dict)], 6),
        },
    ]
    return {
        "version": 1,
        "kind": "trade_instance",
        "generated_at": instance.get("updated_at"),
        "summary": f"实例 {instance.get('instance_id') or '--'} · {instance.get('lifecycle_state') or '--'}",
        "stages": stages,
    }


def _trace_notes(notes: list[Any], limit: int) -> list[str]:
    rows = []
    for item in notes:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            rows.append(text)
    return rows[:limit]


def attach_candidate_snapshot(
    instance: dict[str, Any],
    scan_results: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
) -> dict[str, Any]:
    hard_flag_counts: dict[str, int] = {}
    symbols = []
    for item in scan_results:
        candidates = item.get("candidates") or ([item.get("candidate")] if item.get("candidate") else [])
        valid_candidates = [candidate for candidate in candidates if isinstance(candidate, dict)]
        strategy_candidates = [candidate for candidate in (item.get("strategy_candidates") or []) if isinstance(candidate, dict)]
        flags = []
        for candidate in valid_candidates:
            for flag in _candidate_flags(candidate):
                hard_flag_counts[flag] = hard_flag_counts.get(flag, 0) + 1
                flags.append(flag)
        candidate_count = len(valid_candidates)
        data_integrity = item.get("data_integrity") if isinstance(item.get("data_integrity"), dict) else {}
        symbols.append(
            {
                "symbol": item.get("symbol"),
                "status": item.get("status"),
                "candidate_count": candidate_count,
                "strategy_candidate_count": len(strategy_candidates),
                "top_contract": (valid_candidates[0] if valid_candidates else {}).get("contract_symbol"),
                "technical_bias": item.get("technical_bias"),
                "technical_evidence": {
                    "daily_summary": item.get("daily_summary") or {},
                    "intraday_summary": item.get("intraday_summary") or {},
                },
                "hard_flags": sorted(set(flags)),
                "data_integrity": data_integrity,
                "top_candidates": [_candidate_snapshot_card(candidate) for candidate in valid_candidates[:8]],
                "top_strategy_candidates": [_strategy_snapshot_card(candidate) for candidate in strategy_candidates[:5]],
            }
        )
    rejected_count = sum(int(((item.get("data_integrity") or {}).get("rejected_count") or 0)) for item in scan_results if isinstance(item, dict))
    rejected_examples = []
    for item in scan_results:
        integrity = item.get("data_integrity") if isinstance(item.get("data_integrity"), dict) else {}
        for row in integrity.get("rejected") or []:
            if isinstance(row, dict):
                rejected_examples.append(row)
    instance["candidate_snapshot"] = {
        "symbols_scanned": len(scan_results),
        "symbols_with_candidates": sum(1 for item in symbols if int(item.get("candidate_count") or 0) > 0),
        "contract_candidates": len(opportunities),
        "strategy_candidates": sum(int(item.get("strategy_candidate_count") or 0) for item in symbols),
        "top_k_per_symbol": max((int(item.get("candidates_per_symbol") or 0) for item in scan_results), default=0),
        "hard_flag_counts": hard_flag_counts,
        "data_integrity_rejected_count": rejected_count,
        "data_integrity_rejected_examples": rejected_examples[:12],
        "symbols": symbols,
    }
    integrity_note = f"，数据完整性过滤 {rejected_count} 个候选" if rejected_count else ""
    append_instance_event(
        instance,
        "candidate_snapshot",
        f"候选池已生成：{len(scan_results)} 个标的，{len(opportunities)} 张合约候选，{instance['candidate_snapshot']['strategy_candidates']} 个策略结构{integrity_note}。",
        lifecycle_state="council_review",
        payload={
            "contract_candidates": len(opportunities),
            "strategy_candidates": instance["candidate_snapshot"]["strategy_candidates"],
            "data_integrity_rejected_count": rejected_count,
            "data_integrity_rejected_examples": rejected_examples[:5],
        },
    )
    return instance


def attach_ai_decision(
    instance: dict[str, Any],
    council: dict[str, Any],
    selections: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_contracts = [str(item.get("contract_symbol") or "") for item in selections if item.get("contract_symbol")]
    rejected = council.get("rejected") or []
    post_validation = council.get("post_validation") or {}
    instance["ai_decision"] = {
        "council_mode": council.get("council_mode"),
        "summary": council.get("summary"),
        "selection_count": len(selections),
        "selected_contracts": selected_contracts,
        "final_top_n": [_selection_decision_card(item) for item in selections],
        "rejected_count": len(rejected) if isinstance(rejected, list) else 0,
        "rejected": rejected[:20] if isinstance(rejected, list) else [],
        "advisor_reports": [_advisor_report_card(item) for item in council.get("advisor_reports") or []],
        "ai_execution": council.get("ai_execution") or {},
        "top_up": council.get("top_up") or {},
        "post_validation": post_validation,
        "risk_notes": council.get("risk_notes") or [],
        "moderator_raw_answer_preview": _preview(council.get("raw_answer")),
    }
    append_instance_event(
        instance,
        "ai_decision",
        f"AI 决策完成：入选 {len(selections)} 张合约。",
        lifecycle_state="approved" if selections else "blocked",
        status="success" if selections else "warning",
        payload={"selected_contracts": selected_contracts},
    )
    return instance


def attach_risk_and_execution_plan(
    instance: dict[str, Any],
    selections: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    total_capital = _num(config.get("total_capital"))
    positions = []
    execution_orders = []
    planned_premium = 0.0
    for item in selections:
        entry_price = _num(item.get("entry_price"))
        allocation_pct = _num(item.get("allocation_pct"))
        allocation_amount = total_capital * allocation_pct
        estimated_quantity = int(allocation_amount // (entry_price * 100)) if entry_price > 0 else 0
        max_loss = entry_price * estimated_quantity * 100
        planned_premium += max_loss
        risk_plan = (item.get("candidate") or {}).get("risk_plan") or {}
        positions.append(
            {
                "symbol": item.get("symbol"),
                "contract_symbol": item.get("contract_symbol"),
                "side": (item.get("candidate") or {}).get("side"),
                "allocation_pct": allocation_pct,
                "allocation_amount": round(allocation_amount, 2),
                "estimated_quantity": estimated_quantity,
                "entry_price_estimate": entry_price,
                "max_loss": round(max_loss, 2),
                "stop_loss_pct": _num(item.get("stop_loss_pct")),
                "stop_trigger_price": round(entry_price * (1 - _num(item.get("stop_loss_pct")) / 100), 2) if entry_price > 0 else 0.0,
                "take_profit_1": risk_plan.get("take_profit_1"),
                "take_profit_2": risk_plan.get("take_profit_2"),
                "latest_exit": risk_plan.get("latest_exit"),
                "underlying_invalidation": risk_plan.get("invalidation"),
                "max_holding_minutes": risk_plan.get("max_holding_minutes"),
                "allow_overnight": _allow_overnight(risk_plan),
            }
        )
        execution_orders.append(
            {
                "contract_symbol": item.get("contract_symbol"),
                "order_symbol": item.get("order_symbol"),
                "entry_order_type": item.get("entry_order_type") or config.get("entry_order_type"),
                "estimated_entry_price": entry_price,
                "estimated_quantity": estimated_quantity,
                "wait_for_fill_seconds": int(config.get("wait_for_fill_seconds") or 0),
                "partial_fill_policy": "protect_filled_quantity_only",
            }
        )
    instance["risk_plan"] = {
        "total_planned_capital": total_capital,
        "planned_contracts": len(selections),
        "planned_premium_at_risk": round(planned_premium, 2),
        "max_loss_if_all_premiums_lost": round(planned_premium, 2),
        "positions": positions,
    }
    instance["execution_plan"] = {
        "entry_order_type": config.get("entry_order_type"),
        "wait_for_fill_seconds": int(config.get("wait_for_fill_seconds") or 0),
        "software_stop_enabled": bool(config.get("software_stop_enabled", True)),
        "software_take_profit_enabled": bool(config.get("software_take_profit_enabled", True)),
        "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled", False)),
        "orders": execution_orders,
    }
    append_instance_event(
        instance,
        "risk_plan",
        f"风控与执行计划已生成：计划风险权利金约 ${planned_premium:.2f}。",
        lifecycle_state="submitting" if selections else "blocked",
        payload={"planned_premium_at_risk": round(planned_premium, 2)},
    )
    return instance


def attach_order_results(instance: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    instance["protection_status"] = build_protection_status(orders)
    update_review_metrics(instance, orders)
    state = lifecycle_from_orders(orders)
    append_instance_event(
        instance,
        "orders_submitted",
        f"执行阶段完成：{len(orders)} 条记录，保护为{_protection_state_label(instance['protection_status']['state'])}。",
        lifecycle_state=state,
        status="warning" if instance["protection_status"].get("requires_manual_attention") else "success",
        payload={"order_count": len(orders), "protection_state": instance["protection_status"]["state"]},
    )
    return instance


def refresh_protection_from_orders(instance: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    previous = (instance.get("protection_status") or {}).get("state")
    instance["protection_status"] = build_protection_status(orders)
    update_review_metrics(instance, orders)
    current = instance["protection_status"].get("state")
    if current != previous:
        append_instance_event(
            instance,
            "protection_status_changed",
            f"保护从{_protection_state_label(previous)}更新为{_protection_state_label(current)}。",
            lifecycle_state=lifecycle_from_orders(orders),
            status="warning" if instance["protection_status"].get("requires_manual_attention") else "success",
        )
    else:
        set_lifecycle(instance, lifecycle_from_orders(orders))
    return instance


def update_review_metrics(instance: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    previous = instance.get("review_metrics") or {}
    metrics = build_review_metrics(orders, previous=previous, ai_decision=instance.get("ai_decision") or {})
    instance["review_metrics"] = metrics
    return instance


def build_protection_status(orders: list[dict[str, Any]]) -> dict[str, Any]:
    if not orders:
        return empty_protection_status()
    broker_stop_submitted = 0
    protected_quantity = 0
    software_protected_quantity = 0
    software_take_profit_quantity = 0
    single_leg_smart_exit_quantity = 0
    single_leg_exit_submitted_quantity = 0
    unprotected_quantity = 0
    filled_total = 0
    strategy_tracked_quantity = 0
    strategy_residual_tracking_quantity = 0
    broker_combo_close_required = 0
    strategy_exit_submitted = 0
    strategy_exit_filled = 0
    failure_reasons = []
    contracts = []
    for order in orders:
        is_strategy_order = bool(
            order.get("strategy_auto_execute")
            or order.get("strategy_execution_mode") == "auto"
            or order.get("strategy_exit_status")
            or order.get("legs")
        )
        quantity = int(_num(order.get("quantity")))
        covered = int(_num(order.get("covered_quantity")))
        software_quantity = int(_num(order.get("software_stop_quantity"))) if order.get("software_stop_active") else 0
        take_profit_quantity = int(_num(order.get("software_take_profit_quantity"))) if order.get("software_take_profit_active") else 0
        smart_exit_quantity = int(_num(order.get("single_leg_smart_exit_quantity"))) if order.get("single_leg_smart_exit_active") else 0
        software_closed = int(_num(order.get("software_stop_closed_quantity")))
        software_submitted = int(_num(order.get("software_stop_submitted_quantity")))
        take_profit_closed = int(_num(order.get("software_take_profit_closed_quantity")))
        take_profit_submitted = int(_num(order.get("software_take_profit_submitted_quantity")))
        smart_exit_closed = int(_num(order.get("single_leg_smart_exit_closed_quantity")))
        smart_exit_submitted = int(_num(order.get("single_leg_smart_exit_submitted_quantity")))
        flatten_closed = int(_num(order.get("instance_flatten_closed_quantity")))
        flatten_submitted = int(_num(order.get("instance_flatten_submitted_quantity")))
        exit_pending_or_closed = (
            max(software_closed, software_submitted)
            + max(take_profit_closed, take_profit_submitted)
            + max(smart_exit_closed, smart_exit_submitted)
            + max(flatten_closed, flatten_submitted)
        )
        exit_pending_quantity = (
            max(0, software_submitted - software_closed)
            + max(0, take_profit_submitted - take_profit_closed)
            + max(0, smart_exit_submitted - smart_exit_closed)
            + max(0, flatten_submitted - flatten_closed)
        )
        stop_count = len(order.get("stop_orders") or ([] if not order.get("stop_order") else [order.get("stop_order")]))
        if stop_count:
            broker_stop_submitted += stop_count
            protected_quantity += max(covered, 0)
        strategy_residual_legs = _strategy_residual_legs(order) if is_strategy_order else []
        strategy_residual_quantity = _strategy_residual_quantity(order, strategy_residual_legs)
        filled = int(_num(order.get("entry_filled_quantity"))) if order.get("entry_filled_quantity") is not None else quantity if _is_filled_status(order) else 0
        if is_strategy_order and filled <= 0 and strategy_residual_quantity > 0:
            filled = strategy_residual_quantity
        filled_total += max(filled, 0)
        if is_strategy_order and order.get("risk_tracking_active"):
            strategy_tracked_quantity += max(filled or quantity, 0)
        if is_strategy_order and (order.get("residual_leg_tracking_active") or strategy_residual_quantity > 0):
            strategy_residual_tracking_quantity += max(strategy_residual_quantity, filled, 0)
        if is_strategy_order and (order.get("broker_combo_close_required") or order.get("status") == "broker_combo_close_required"):
            broker_combo_close_required += 1
        if is_strategy_order and str(order.get("strategy_exit_status") or "").startswith("submitted"):
            strategy_exit_submitted += max(filled or quantity, 0)
        if is_strategy_order and order.get("strategy_exit_status") == "filled":
            strategy_exit_filled += max(filled or quantity, 0)
        software_protected_quantity += max(software_quantity, 0)
        software_take_profit_quantity += max(take_profit_quantity, 0)
        single_leg_smart_exit_quantity += max(smart_exit_quantity, 0)
        single_leg_exit_submitted_quantity += max(exit_pending_quantity, 0)
        unprotected = 0 if is_strategy_order else max(0, filled - covered - software_quantity - smart_exit_quantity - exit_pending_or_closed)
        if is_strategy_order and strategy_residual_quantity > 0 and not order.get("residual_leg_tracking_active"):
            failure_reasons.append("strategy_partial_leg_fill")
        if _stop_unsupported(order):
            if software_quantity <= 0:
                unprotected = max(unprotected, filled or quantity)
            failure_reasons.append("broker_stop_unsupported")
        elif order.get("stop_error"):
            failure_reasons.append(str(order.get("stop_error"))[:160])
        if order.get("monitor_account_error"):
            if not is_strategy_order:
                unprotected = max(unprotected, filled or quantity)
            failure_reasons.append(str(order.get("monitor_account_error"))[:160])
        if order.get("software_stop_error"):
            failure_reasons.append(str(order.get("software_stop_error"))[:160])
        if order.get("software_take_profit_error"):
            failure_reasons.append(str(order.get("software_take_profit_error"))[:160])
        if order.get("single_leg_smart_exit_error"):
            failure_reasons.append(str(order.get("single_leg_smart_exit_error"))[:160])
        if is_strategy_order and not order.get("residual_leg_tracking_active") and (order.get("strategy_exit_error") or order.get("status") == "strategy_auto_exit_failed"):
            failure_reasons.append(str(order.get("strategy_exit_error") or "strategy_auto_exit_failed")[:160])
        unprotected_quantity += unprotected
        contracts.append(
            {
                "contract_symbol": order.get("contract_symbol"),
                "order_symbol": order.get("order_symbol"),
                "status": order.get("status"),
                "quantity": quantity,
                "covered_quantity": covered,
                "unprotected_quantity": unprotected,
                "broker_stop_submitted": stop_count > 0,
                "software_stop_active": bool(order.get("software_stop_active")),
                "software_stop_quantity": software_quantity,
                "software_stop_closed_quantity": software_closed,
                "software_stop_submitted_quantity": software_submitted,
                "software_stop_status": order.get("software_stop_status"),
                "software_take_profit_active": bool(order.get("software_take_profit_active")),
                "software_take_profit_quantity": take_profit_quantity,
                "software_take_profit_closed_quantity": take_profit_closed,
                "software_take_profit_submitted_quantity": take_profit_submitted,
                "software_take_profit_status": order.get("software_take_profit_status"),
                "single_leg_smart_exit_active": bool(order.get("single_leg_smart_exit_active")),
                "single_leg_smart_exit_quantity": smart_exit_quantity,
                "single_leg_smart_exit_closed_quantity": smart_exit_closed,
                "single_leg_smart_exit_submitted_quantity": smart_exit_submitted,
                "single_leg_smart_exit_status": order.get("single_leg_smart_exit_status"),
                "monitor_account_error": order.get("monitor_account_error"),
                "instance_flatten_closed_quantity": flatten_closed,
                "instance_flatten_submitted_quantity": flatten_submitted,
                "stop_failure_reason": order.get("stop_error") or order.get("message"),
                "strategy_auto_execute": is_strategy_order,
                "strategy_exit_status": order.get("strategy_exit_status"),
                "strategy_exit_error": order.get("strategy_exit_error"),
                "residual_leg_tracking_active": bool(order.get("residual_leg_tracking_active") or strategy_residual_quantity > 0),
                "residual_leg_contract_symbol": order.get("residual_leg_contract_symbol"),
                "residual_leg_quantity": order.get("residual_leg_quantity") or strategy_residual_quantity or None,
                "residual_legs": order.get("residual_legs") or strategy_residual_legs,
                "broker_combo_close_required": bool(order.get("broker_combo_close_required") or order.get("status") == "broker_combo_close_required"),
                "broker_combo_close_reason": order.get("broker_combo_close_reason"),
            }
        )
    if filled_total <= 0 and _all_orders_terminal_without_fill(orders):
        state = "no_position"
    elif broker_combo_close_required > 0:
        state = "broker_combo_close_required"
    elif strategy_residual_tracking_quantity > 0:
        state = "strategy_residual_tracking"
    elif unprotected_quantity > 0:
        state = "unprotected"
    elif broker_stop_submitted > 0:
        state = "protected"
    elif software_protected_quantity > 0:
        state = "software_protected"
    elif single_leg_smart_exit_quantity > 0:
        state = "software_protected"
    elif single_leg_exit_submitted_quantity > 0:
        state = "exiting"
    elif _all_single_leg_positions_closed(orders):
        state = "exited"
    elif any(order.get("status") in {"strategy_auto_exit_failed", "residual_exit_failed"} for order in orders):
        state = "strategy_exit_failed"
    elif strategy_exit_submitted > 0 and strategy_tracked_quantity > 0:
        state = "strategy_partial_exiting"
    elif strategy_exit_submitted > 0:
        state = "strategy_exiting"
    elif strategy_exit_filled > 0 and strategy_tracked_quantity <= 0:
        state = "strategy_exited"
    elif strategy_tracked_quantity > 0:
        state = "strategy_protected"
    elif any(order.get("status") in {"failed", "skipped_requote_unavailable", "skipped_insufficient_allocation"} for order in orders):
        state = "blocked"
    else:
        state = "pending"
    return {
        "state": state,
        "broker_stop_submitted": broker_stop_submitted,
        "software_stop_active": any(bool(order.get("software_stop_active")) for order in orders),
        "software_protected_quantity": software_protected_quantity,
        "software_take_profit_active": any(bool(order.get("software_take_profit_active")) for order in orders),
        "software_take_profit_quantity": software_take_profit_quantity,
        "single_leg_smart_exit_active": any(bool(order.get("single_leg_smart_exit_active")) for order in orders),
        "single_leg_smart_exit_quantity": single_leg_smart_exit_quantity,
        "single_leg_exit_submitted_quantity": single_leg_exit_submitted_quantity,
        "strategy_tracked_quantity": strategy_tracked_quantity,
        "strategy_residual_tracking_quantity": strategy_residual_tracking_quantity,
        "broker_combo_close_required": broker_combo_close_required,
        "strategy_exit_submitted_quantity": strategy_exit_submitted,
        "strategy_exit_filled_quantity": strategy_exit_filled,
        "unprotected_quantity": unprotected_quantity,
        "protected_quantity": protected_quantity,
        "stop_failure_reason": "; ".join(dict.fromkeys(failure_reasons)) or None,
        "requires_manual_attention": unprotected_quantity > 0
        or (strategy_residual_tracking_quantity > 0 and software_protected_quantity <= 0 and software_take_profit_quantity <= 0 and single_leg_smart_exit_quantity <= 0)
        or any(
            bool(
                order.get("software_stop_error")
                or order.get("software_take_profit_error")
                or order.get("single_leg_smart_exit_error")
                or order.get("monitor_account_error")
                or (order.get("strategy_exit_error") and not order.get("residual_leg_tracking_active"))
                or order.get("status") == "strategy_auto_exit_failed"
                or order.get("status") == "broker_combo_close_required"
                or order.get("broker_combo_close_required")
            )
            for order in orders
        ),
        "contracts": contracts,
    }


def build_review_metrics(
    orders: list[dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
    ai_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = empty_review_metrics()
    previous = previous or {}
    pnl = _pnl_snapshot(orders)
    current_unrealized = pnl["current_unrealized_pnl"]
    realized = pnl["realized_pnl"]
    total_pnl = pnl["estimated_total_pnl"]
    prior_mfe = _optional_num(previous.get("mfe"))
    prior_mae = _optional_num(previous.get("mae"))
    mfe = current_unrealized if prior_mfe is None else max(prior_mfe, current_unrealized)
    mae = current_unrealized if prior_mae is None else min(prior_mae, current_unrealized)
    ai_confidence = _ai_confidence(ai_decision or {})
    total_cost = pnl["entry_cost"]
    return_pct = (total_pnl / total_cost * 100) if total_cost > 0 else None
    terminal_no_fill = _all_orders_terminal_without_fill(orders)
    metrics.update(
        {
            "submitted_orders": len(orders),
            "filled_entries": sum(1 for order in orders if _is_filled_status(order)),
            "failed_orders": sum(1 for order in orders if order.get("status") == "failed"),
            "skipped_orders": sum(1 for order in orders if str(order.get("status") or "").startswith("skipped")),
            "unprotected_contracts": sum(1 for order in orders if _stop_unsupported(order) or order.get("stop_error")),
            "software_stop_armed": sum(1 for order in orders if order.get("software_stop_active")),
            "software_stop_submitted": sum(1 for order in orders if order.get("status") == "software_stop_submitted"),
            "software_stop_failed": sum(1 for order in orders if order.get("status") == "software_stop_failed"),
            "software_take_profit_armed": sum(1 for order in orders if order.get("software_take_profit_active")),
            "software_take_profit_submitted": sum(
                1 for order in orders if order.get("status") in {"software_take_profit_submitted", "software_take_profit_partial_submitted"}
            ),
            "software_take_profit_failed": sum(1 for order in orders if order.get("status") == "software_take_profit_failed"),
            "single_leg_smart_exit_submitted": sum(1 for order in orders if order.get("status") == "single_leg_smart_exit_submitted"),
            "single_leg_smart_exit_failed": sum(1 for order in orders if order.get("status") == "single_leg_smart_exit_failed"),
            "entry_cost": round(total_cost, 2),
            "open_quantity": pnl["open_quantity"],
            "closed_quantity": pnl["closed_quantity"],
            "current_unrealized_pnl": round(current_unrealized, 2),
            "realized_pnl": round(realized, 2),
            "strategy_realized_pnl": round(float(pnl.get("strategy_realized_pnl") or 0), 2),
            "strategy_unrealized_pnl": round(float(pnl.get("strategy_unrealized_pnl") or 0), 2),
            "strategy_trigger_mark_pnl": (
                round(float(pnl.get("strategy_trigger_mark_pnl")), 2)
                if pnl.get("strategy_trigger_mark_pnl") is not None
                else None
            ),
            "pnl_basis": pnl.get("pnl_basis"),
            "pnl_warnings": pnl.get("pnl_warnings") or [],
            "estimated_total_pnl": round(total_pnl, 2),
            "mfe": round(mfe, 2),
            "mae": round(mae, 2),
            "max_unrealized_profit": round(max(_optional_num(previous.get("max_unrealized_profit")) or current_unrealized, current_unrealized), 2),
            "max_drawdown": round(min(_optional_num(previous.get("max_drawdown")) or current_unrealized, current_unrealized), 2),
            "holding_minutes": None if terminal_no_fill else _holding_minutes(orders),
            "first_exit_trigger": None if terminal_no_fill else previous.get("first_exit_trigger") or _first_exit_trigger(orders),
            "win_loss": "win" if total_pnl > 0 else "loss" if total_pnl < 0 else "flat",
            "return_pct": round(return_pct, 2) if return_pct is not None else None,
            "ai_confidence_avg": round(ai_confidence, 2) if ai_confidence is not None else None,
            "ai_confidence_vs_return": round((return_pct or 0) - ai_confidence, 2) if ai_confidence is not None and return_pct is not None else None,
        }
    )
    return metrics


def _pnl_snapshot(orders: list[dict[str, Any]]) -> dict[str, Any]:
    entry_cost = 0.0
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    strategy_realized_pnl = 0.0
    strategy_unrealized_pnl = 0.0
    strategy_trigger_mark_pnl = None
    open_quantity = 0
    closed_quantity = 0
    warnings: set[str] = set()

    for order in orders:
        if _is_strategy_order(order):
            strategy = annotate_strategy_order_fill_ledger(order)
            warnings.update(str(item) for item in (order.get("pnl_warnings") or []) if str(item).strip())
            entry_cost += float(strategy.get("entry_cost") or 0)
            realized_pnl += float(strategy.get("realized_pnl") or 0)
            unrealized_pnl += float(strategy.get("unrealized_pnl") or 0)
            strategy_realized_pnl += float(strategy.get("realized_pnl") or 0)
            strategy_unrealized_pnl += float(strategy.get("unrealized_pnl") or 0)
            open_quantity += int(_num(strategy.get("open_units")))
            closed_quantity += int(_num(strategy.get("closed_units")))
            trigger = _optional_num(order.get("strategy_exit_trigger_mark_pnl") or order.get("stop_trigger_pnl"))
            if trigger is not None:
                strategy_trigger_mark_pnl = trigger if strategy_trigger_mark_pnl is None else strategy_trigger_mark_pnl + trigger
            continue
        filled = _filled_quantity(order)
        if filled <= 0:
            continue
        entry_price = _entry_price(order)
        if entry_price <= 0:
            warnings.add("entry_price_unavailable")
            continue
        if _entry_price_source(order) != "broker":
            warnings.add("entry_price_estimated")
        entry_cost += entry_price * filled * 100
        remaining = filled

        for close in _close_lots(order):
            if remaining <= 0:
                break
            close_quantity = min(remaining, int(_num(close.get("quantity"))))
            close_price = _optional_num(close.get("price"))
            if close_quantity <= 0 or close_price is None or close_price <= 0:
                continue
            if close.get("price_source") != "broker":
                warnings.add("exit_price_estimated")
            realized_pnl += (close_price - entry_price) * close_quantity * 100
            closed_quantity += close_quantity
            remaining -= close_quantity

        if remaining > 0:
            current_price = _current_exit_price(order, entry_price)
            warnings.add("open_positions_use_mark")
            unrealized_pnl += (current_price - entry_price) * remaining * 100
            open_quantity += remaining

        if _has_pending_exit_order(order):
            warnings.add("exit_order_pending_broker_fill")

    return {
        "entry_cost": entry_cost,
        "realized_pnl": realized_pnl,
        "current_unrealized_pnl": unrealized_pnl,
        "estimated_total_pnl": realized_pnl + unrealized_pnl,
        "strategy_realized_pnl": strategy_realized_pnl,
        "strategy_unrealized_pnl": strategy_unrealized_pnl,
        "strategy_trigger_mark_pnl": strategy_trigger_mark_pnl,
        "open_quantity": open_quantity,
        "closed_quantity": closed_quantity,
        "pnl_basis": "broker_confirmed" if not warnings else "broker_and_estimate",
        "pnl_warnings": sorted(warnings),
    }


def annotate_strategy_order_fill_ledger(order: dict[str, Any]) -> dict[str, Any]:
    ledger = strategy_order_fill_ledger(order)
    if ledger.get("has_fills"):
        order["strategy_fill_ledger"] = ledger
        order["strategy_realized_pnl"] = ledger.get("realized_pnl")
        order["strategy_unrealized_pnl"] = ledger.get("unrealized_pnl")
        order["strategy_actual_entry_net"] = ledger.get("entry_net")
        order["strategy_actual_exit_net"] = ledger.get("exit_net")
    return ledger


def strategy_order_fill_ledger(order: dict[str, Any]) -> dict[str, Any]:
    if not _is_strategy_order(order):
        return {"has_fills": False, "legs": [], "realized_pnl": 0.0, "unrealized_pnl": 0.0}
    legs = order.get("legs") if isinstance(order.get("legs"), list) else []
    rows = []
    entry_net = 0.0
    exit_net = 0.0
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    debit_cash = 0.0
    open_units_candidates = []
    closed_units_candidates = []
    has_fills = False
    for row in legs:
        if not isinstance(row, dict):
            continue
        leg = row.get("leg") if isinstance(row.get("leg"), dict) else {}
        action = str(leg.get("action") or row.get("action") or "").lower()
        ratio = max(1, int(_num(leg.get("qty") or row.get("qty") or 1)))
        entry_qty = _strategy_leg_filled_quantity(row)
        if entry_qty <= 0:
            continue
        entry_price = _strategy_leg_entry_price(row, leg)
        if entry_price <= 0:
            continue
        exit_qty = min(entry_qty, _strategy_leg_exit_filled_quantity(row))
        exit_price = _strategy_leg_exit_price(row)
        remaining_qty = max(0, entry_qty - exit_qty)
        entry_net += (-entry_price if action == "buy" else entry_price) * entry_qty * 100
        if action == "buy":
            debit_cash += entry_price * entry_qty * 100
        has_fills = True
        leg_realized = 0.0
        if exit_qty > 0 and exit_price > 0:
            signed_entry_closed = (-entry_price if action == "buy" else entry_price) * exit_qty * 100
            signed_exit_cash = (exit_price if action == "buy" else -exit_price) * exit_qty * 100
            leg_realized = signed_entry_closed + signed_exit_cash
            realized_pnl += leg_realized
            exit_net += signed_exit_cash
        current_close = _strategy_leg_current_close_price(row, leg, action)
        leg_unrealized = 0.0
        if remaining_qty > 0 and current_close > 0:
            leg_unrealized = ((current_close - entry_price) if action == "buy" else (entry_price - current_close)) * remaining_qty * 100
            unrealized_pnl += leg_unrealized
        open_units_candidates.append(remaining_qty // ratio)
        closed_units_candidates.append(exit_qty // ratio)
        rows.append(
            {
                "contract_symbol": leg.get("contract_symbol") or row.get("contract_symbol"),
                "action": action,
                "ratio": ratio,
                "entry_quantity": entry_qty,
                "entry_price": round(entry_price, 4),
                "exit_quantity": exit_qty,
                "exit_price": round(exit_price, 4) if exit_price > 0 else None,
                "remaining_quantity": remaining_qty,
                "realized_pnl": round(leg_realized, 2),
                "unrealized_pnl": round(leg_unrealized, 2),
            }
        )
    open_units = min(open_units_candidates) if open_units_candidates else 0
    residual_units = _strategy_residual_units(order)
    if residual_units > 0:
        open_units = max(open_units, residual_units)
    closed_units = min(closed_units_candidates) if closed_units_candidates else 0
    entry_cost = abs(entry_net) if abs(entry_net) > 0.01 else debit_cash
    return {
        "has_fills": has_fills,
        "legs": rows,
        "entry_net": round(entry_net, 2),
        "exit_net": round(exit_net, 2),
        "entry_cost": round(entry_cost, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "estimated_total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "open_units": int(open_units),
        "closed_units": int(closed_units),
    }


def _strategy_residual_units(order: dict[str, Any]) -> int:
    residuals = _strategy_residual_legs(order)
    if not residuals:
        return 0
    ratios_by_symbol: dict[str, int] = {}
    for row in order.get("legs") or []:
        if not isinstance(row, dict):
            continue
        leg = row.get("leg") if isinstance(row.get("leg"), dict) else {}
        contract_symbol = str(row.get("contract_symbol") or leg.get("contract_symbol") or "").strip()
        if not contract_symbol:
            continue
        ratio = max(1, int(_num(leg.get("qty") or row.get("qty") or 1)))
        ratios_by_symbol[contract_symbol] = ratio
    candidates: list[int] = []
    for residual in residuals:
        if not isinstance(residual, dict):
            continue
        contract_symbol = str(residual.get("contract_symbol") or "").strip()
        ratio = max(1, ratios_by_symbol.get(contract_symbol, 1))
        quantity = int(_num(residual.get("filled_quantity") or residual.get("quantity") or 0))
        if quantity > 0:
            candidates.append(quantity // ratio)
    return min(candidates) if candidates else 0


def _is_strategy_order(order: dict[str, Any]) -> bool:
    return bool(order.get("strategy_auto_execute") or order.get("strategy_execution_mode") == "auto" or order.get("strategy_exit_status") or order.get("legs"))


def _strategy_residual_legs(order: dict[str, Any]) -> list[dict[str, Any]]:
    if str(order.get("strategy_exit_status") or "") == "filled" or str(order.get("status") or "") == "strategy_auto_exit_filled":
        return []
    explicit = order.get("residual_legs")
    if isinstance(explicit, list) and explicit:
        return [item for item in explicit if isinstance(item, dict)]
    status = str(order.get("status") or "")
    entry_status = str(order.get("strategy_entry_status") or "")
    if status not in {"failed", "strategy_residual_tracking"} and entry_status != "failed" and not order.get("residual_leg_tracking_active"):
        return []
    residuals: list[dict[str, Any]] = []
    for row in order.get("legs") or []:
        if not isinstance(row, dict):
            continue
        filled = _strategy_leg_filled_quantity(row)
        if filled <= 0:
            continue
        leg = row.get("leg") if isinstance(row.get("leg"), dict) else {}
        residuals.append(
            {
                "contract_symbol": row.get("contract_symbol") or leg.get("contract_symbol"),
                "order_symbol": row.get("order_symbol") or leg.get("order_symbol"),
                "action": row.get("action") or leg.get("action"),
                "filled_quantity": filled,
                "entry_price": row.get("entry_price") or leg.get("entry_price") or leg.get("price"),
                "order_id": row.get("order_id") or ((row.get("entry_order") or {}).get("order_id") if isinstance(row.get("entry_order"), dict) else None),
            }
        )
    return residuals


def _strategy_residual_quantity(order: dict[str, Any], residual_legs: list[dict[str, Any]] | None = None) -> int:
    if str(order.get("strategy_exit_status") or "") == "filled" or str(order.get("status") or "") in {"strategy_auto_exit_filled", "strategy_manual_exit_detected"}:
        return 0
    explicit = int(_num(order.get("residual_leg_quantity")))
    if explicit > 0:
        return explicit
    rows = residual_legs if residual_legs is not None else _strategy_residual_legs(order)
    return max((int(_num(item.get("filled_quantity"))) for item in rows), default=0)


def _strategy_leg_filled_quantity(row: dict[str, Any]) -> int:
    for value in (
        row.get("filled_quantity"),
        row.get("entry_filled_quantity"),
        (row.get("entry_detail") or {}).get("executed_quantity") if isinstance(row.get("entry_detail"), dict) else None,
        (row.get("entry_detail") or {}).get("filled_quantity") if isinstance(row.get("entry_detail"), dict) else None,
    ):
        quantity = int(_num(value))
        if quantity > 0:
            return quantity
    return 0


def _strategy_leg_entry_price(row: dict[str, Any], leg: dict[str, Any]) -> float:
    for value in (
        row.get("actual_entry_price"),
        row.get("entry_price"),
        row.get("executed_price"),
        _executed_price(row.get("entry_detail") or {}),
        leg.get("actual_entry_price"),
        leg.get("entry_price"),
        leg.get("price"),
    ):
        number = _optional_num(value)
        if number is not None and number > 0:
            return number
    return 0.0


def _strategy_leg_exit_price(row: dict[str, Any]) -> float:
    for value in (
        row.get("strategy_exit_executed_price"),
        row.get("strategy_exit_price"),
        _executed_price(row.get("strategy_exit_detail") or {}),
    ):
        number = _optional_num(value)
        if number is not None and number > 0:
            return number
    return 0.0


def _strategy_leg_exit_filled_quantity(row: dict[str, Any]) -> int:
    if str(row.get("strategy_exit_status") or "").lower() != "filled":
        return 0
    for value in (row.get("strategy_exit_filled_quantity"), (row.get("strategy_exit_detail") or {}).get("executed_quantity"), row.get("strategy_exit_quantity")):
        quantity = int(_num(value))
        if quantity > 0:
            return quantity
    return 0


def _strategy_leg_current_close_price(row: dict[str, Any], leg: dict[str, Any], action: str) -> float:
    quote = row.get("last_leg_quote") if isinstance(row.get("last_leg_quote"), dict) else {}
    raw = quote.get("raw") if isinstance(quote.get("raw"), dict) else quote
    preferred = ("bid", "exit_price", "last_done", "price", "last_price", "ask") if action == "buy" else ("ask", "exit_price", "last_done", "price", "last_price", "bid")
    number = _first_number(raw, preferred)
    if number is not None and number > 0:
        return number
    return _optional_num(leg.get("price")) or 0.0


def _close_lots(order: dict[str, Any]) -> list[dict[str, Any]]:
    lots: list[dict[str, Any]] = []
    for target in order.get("software_take_profit_targets") or []:
        if not isinstance(target, dict) or target.get("status") not in {"filled", "partial_filled"}:
            continue
        filled_quantity = int(_num(target.get("filled_quantity")))
        if filled_quantity <= 0 and target.get("status") == "filled":
            filled_quantity = int(_num(target.get("quantity")))
        if filled_quantity <= 0:
            continue
        executed_price = _first_number(target, ("executed_price",)) or _executed_price(target.get("detail") or {})
        lots.append(
            {
                "kind": "take_profit",
                "quantity": filled_quantity,
                "price": executed_price or _first_number(target, ("trigger_quote", "price")),
                "price_source": "broker" if executed_price else "mark",
                "time": target.get("triggered_at"),
            }
        )
    stop_quantity = int(_num(order.get("software_stop_closed_quantity")))
    if stop_quantity > 0:
        executed_price = _exit_executed_price(order, "software_stop")
        lots.append(
            {
                "kind": "software_stop",
                "quantity": stop_quantity,
                "price": executed_price or _first_number(order, ("software_stop_trigger_quote", "software_stop_trigger_price", "stop_trigger_price")),
                "price_source": "broker" if executed_price else "mark",
                "time": order.get("software_stop_triggered_at"),
            }
        )
    smart_exit_quantity = int(_num(order.get("single_leg_smart_exit_closed_quantity")))
    if smart_exit_quantity > 0:
        smart_quote = order.get("single_leg_smart_exit_last_quote") if isinstance(order.get("single_leg_smart_exit_last_quote"), dict) else {}
        executed_price = _exit_executed_price(order, "single_leg_smart_exit")
        lots.append(
            {
                "kind": "smart_exit",
                "quantity": smart_exit_quantity,
                "price": executed_price or _first_number(order, ("single_leg_smart_exit_trigger_quote",)) or _first_number(smart_quote, ("exit_price", "bid", "last_done", "price", "last_price", "ask")),
                "price_source": "broker" if executed_price else "mark",
                "time": order.get("single_leg_smart_exit_triggered_at"),
            }
        )
    flatten_quantity = int(_num(order.get("instance_flatten_closed_quantity")))
    if flatten_quantity > 0:
        executed_price = _exit_executed_price(order, "instance_flatten") or _executed_price(order.get("instance_flatten_order") or {})
        lots.append(
            {
                "kind": "manual_flatten",
                "quantity": flatten_quantity,
                "price": executed_price or _current_exit_price(order, _entry_price(order)),
                "price_source": "broker" if executed_price else "mark",
                "time": order.get("instance_flattened_at"),
            }
        )
    return sorted(lots, key=lambda item: _event_sort_key(item.get("time")))


def _filled_quantity(order: dict[str, Any]) -> int:
    explicit = order.get("entry_filled_quantity")
    if explicit is not None:
        return max(0, int(_num(explicit)))
    if _is_filled_status(order):
        return max(0, int(_num(order.get("quantity"))))
    return 0


def _entry_price(order: dict[str, Any]) -> float:
    for value in (
        order.get("actual_entry_price"),
        _executed_price(order.get("entry_detail") or {}),
        order.get("entry_price"),
        order.get("original_entry_price"),
        (order.get("entry_requote") or {}).get("limit_price"),
    ):
        number = _optional_num(value)
        if number is not None and number > 0:
            return number
    return 0.0


def _entry_price_source(order: dict[str, Any]) -> str:
    for value in (
        order.get("actual_entry_price"),
        _executed_price(order.get("entry_detail") or {}),
    ):
        number = _optional_num(value)
        if number is not None and number > 0:
            return "broker"
    return "estimate"


def _exit_executed_price(order: dict[str, Any], source: str) -> float | None:
    for value in (
        order.get(f"{source}_exit_executed_price"),
        _executed_price(order.get(f"{source}_exit_detail") or {}),
    ):
        number = _optional_num(value)
        if number is not None and number > 0:
            return number
    return None


def _has_pending_exit_order(order: dict[str, Any]) -> bool:
    if str(order.get("status") or "") in {
        "software_stop_submitted",
        "software_stop_partial_filled",
        "software_take_profit_submitted",
        "software_take_profit_partial_submitted",
        "software_take_profit_partial_filled",
        "single_leg_smart_exit_submitted",
        "single_leg_smart_exit_partial_filled",
        "instance_flatten_submitted",
        "instance_flatten_partial_filled",
    }:
        return True
    return any(isinstance(target, dict) and target.get("status") == "submitted" for target in order.get("software_take_profit_targets") or [])


def _current_exit_price(order: dict[str, Any], fallback: float) -> float:
    for quote_key in ("software_take_profit_last_quote", "software_stop_last_quote", "single_leg_smart_exit_last_quote"):
        quote_row = order.get(quote_key) or {}
        number = _first_number(quote_row, ("exit_price", "bid", "last_done", "price", "last_price", "ask"))
        if number is not None and number > 0:
            return number
    for value in (order.get("software_take_profit_trigger_quote"), order.get("software_stop_trigger_quote"), order.get("single_leg_smart_exit_trigger_quote")):
        number = _optional_num(value)
        if number is not None and number > 0:
            return number
    return max(float(fallback or 0), 0.0)


def _executed_price(payload: dict[str, Any]) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in (
        "executed_price",
        "filled_avg_price",
        "avg_price",
        "average_price",
        "filled_price",
        "price",
        "limit_price",
        "last_done",
    ):
        number = _optional_num(payload.get(key))
        if number is not None and number > 0:
            return number
    return None


def _first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        number = _optional_num(payload.get(key))
        if number is not None and number > 0:
            return number
    return None


def _ai_confidence(ai_decision: dict[str, Any]) -> float | None:
    scores = []
    for item in ai_decision.get("final_top_n") or []:
        if not isinstance(item, dict):
            continue
        score = _optional_num(item.get("confidence_score"))
        if score is None or score <= 0:
            continue
        scores.append(score * 100 if score <= 1 else score)
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    return max(0.0, min(avg, 100.0))


def _holding_minutes(orders: list[dict[str, Any]]) -> float | None:
    starts: list[Any] = []
    ends: list[Any] = []
    for order in orders:
        starts.extend(
            [
                order.get("software_stop_armed_at"),
                order.get("software_take_profit_armed_at"),
                order.get("entry_filled_at"),
                (order.get("entry_detail") or {}).get("submitted_at"),
                (order.get("entry_detail") or {}).get("created_at"),
                (order.get("entry_detail") or {}).get("updated_at"),
                (order.get("entry_order") or {}).get("submitted_at"),
                (order.get("entry_order") or {}).get("created_at"),
            ]
        )
        if order.get("software_stop_triggered_at"):
            ends.append(order.get("software_stop_triggered_at"))
        if order.get("single_leg_smart_exit_triggered_at"):
            ends.append(order.get("single_leg_smart_exit_triggered_at"))
        if order.get("instance_flattened_at"):
            ends.append(order.get("instance_flattened_at"))
        for target in order.get("software_take_profit_targets") or []:
            if isinstance(target, dict) and target.get("triggered_at"):
                ends.append(target.get("triggered_at"))
    start = min((_parse_time(value) for value in starts if _parse_time(value) is not None), default=None)
    if start is None:
        return None
    open_quantity = _pnl_snapshot(orders)["open_quantity"]
    if open_quantity > 0 or not ends:
        end = parse_datetime(utc_now(), assume_tz=timezone.utc)
    else:
        end = max((_parse_time(value) for value in ends if _parse_time(value) is not None), default=None)
    if end is None:
        return None
    return round(max((end - start).total_seconds() / 60, 0), 1)


def _first_exit_trigger(orders: list[dict[str, Any]]) -> str | None:
    candidates: list[tuple[str, str]] = []
    for order in orders:
        if int(_num(order.get("software_stop_closed_quantity"))) > 0 or order.get("status") == "software_stop_submitted":
            candidates.append((_event_sort_key(order.get("software_stop_triggered_at")), "software_stop"))
        if int(_num(order.get("single_leg_smart_exit_closed_quantity"))) > 0 or order.get("status") == "single_leg_smart_exit_submitted":
            candidates.append((_event_sort_key(order.get("single_leg_smart_exit_triggered_at")), "smart_exit"))
        if int(_num(order.get("instance_flatten_closed_quantity"))) > 0 or order.get("status") == "instance_flatten_submitted":
            candidates.append((_event_sort_key(order.get("instance_flattened_at")), "manual_flatten"))
        for target in order.get("software_take_profit_targets") or []:
            if isinstance(target, dict) and target.get("status") in {"submitted", "filled", "partial_filled"}:
                candidates.append((_event_sort_key(target.get("triggered_at")), "take_profit"))
        if order.get("status") in {"software_take_profit_submitted", "software_take_profit_partial_submitted"}:
            candidates.append((_event_sort_key(order.get("software_take_profit_last_check_at")), "take_profit"))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def _parse_time(value: Any):
    return parse_datetime(value, assume_tz=timezone.utc)


def _event_sort_key(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed else "9999-12-31T23:59:59+00:00"


def _optional_num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def lifecycle_from_orders(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return "blocked"
    if _all_orders_terminal_without_fill(orders):
        return "blocked"
    if any(_single_leg_exit_in_progress(order) for order in orders if isinstance(order, dict)):
        return "exiting"
    if _all_single_leg_positions_closed(orders):
        return "closed"
    if any(str(order.get("strategy_exit_status") or "").startswith("submitted") for order in orders):
        return "exiting"
    if any(order.get("status") == "broker_combo_close_required" or order.get("broker_combo_close_required") for order in orders):
        return "manual_intervention_required"
    if any(order.get("status") == "strategy_residual_tracking" or order.get("residual_leg_tracking_active") for order in orders):
        return "monitoring"
    if any(order.get("status") == "residual_exit_failed" for order in orders):
        return "manual_intervention_required"
    if any(order.get("status") == "strategy_auto_exit_failed" for order in orders):
        return "manual_intervention_required"
    if any(order.get("status") == "software_stop_failed" for order in orders):
        return "manual_intervention_required"
    if any(order.get("status") == "software_take_profit_failed" for order in orders):
        return "manual_intervention_required"
    if any(order.get("status") == "single_leg_smart_exit_failed" for order in orders):
        return "manual_intervention_required"
    protection = build_protection_status(orders)
    if protection["state"] == "unprotected":
        return "unprotected"
    if protection["state"] == "protected":
        return "monitoring"
    if protection["state"] == "software_protected":
        return "monitoring"
    if protection["state"] == "strategy_protected":
        return "monitoring"
    if protection["state"] == "strategy_residual_tracking":
        return "monitoring"
    if protection["state"] in {"exiting", "strategy_exiting", "strategy_partial_exiting"}:
        return "exiting"
    if protection["state"] == "strategy_exited":
        return "closed"
    if protection["state"] == "exited":
        return "closed"
    if all(str(order.get("status") or "").startswith("skipped") or order.get("status") == "failed" for order in orders):
        return "blocked"
    if any(order.get("status") in {"entry_submitted_stop_pending_unfilled", "entry_partially_filled_stop_partial"} for order in orders):
        return "partial_fill"
    return "open"


def _single_leg_exit_in_progress(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "")
    if status in {
        "instance_flatten_submitted",
        "instance_flatten_partial_filled",
        "software_stop_submitted",
        "software_stop_partial_filled",
        "software_take_profit_submitted",
        "software_take_profit_partial_submitted",
        "software_take_profit_partial_filled",
        "single_leg_smart_exit_submitted",
        "single_leg_smart_exit_partial_filled",
    }:
        return True
    return any(
        int(_num(order.get(key))) > int(_num(order.get(closed_key)))
        for key, closed_key in (
            ("software_stop_submitted_quantity", "software_stop_closed_quantity"),
            ("software_take_profit_submitted_quantity", "software_take_profit_closed_quantity"),
            ("single_leg_smart_exit_submitted_quantity", "single_leg_smart_exit_closed_quantity"),
            ("instance_flatten_submitted_quantity", "instance_flatten_closed_quantity"),
        )
    )


def _all_orders_terminal_without_fill(orders: list[dict[str, Any]]) -> bool:
    if not orders:
        return False
    terminal_statuses = {
        "entry_terminal_no_stop",
        "failed",
        "skipped_requote_unavailable",
        "skipped_untrusted_execution_quote",
        "skipped_insufficient_allocation",
        "blocked_missing_backing",
        "blocked_no_option_legs",
        "blocked_strategy_net_price_gate",
    }
    return all(_filled_quantity(order) <= 0 and _strategy_residual_quantity(order) <= 0 and str(order.get("status") or "") in terminal_statuses for order in orders)


def _all_single_leg_positions_closed(orders: list[dict[str, Any]]) -> bool:
    single_leg_orders = [order for order in orders if isinstance(order, dict) and not _is_strategy_order(order)]
    if not single_leg_orders:
        return False
    return all(_filled_quantity(order) > 0 and _single_leg_remaining_open_quantity(order) <= 0 for order in single_leg_orders)


def _single_leg_remaining_open_quantity(order: dict[str, Any]) -> int:
    closed = (
        int(_num(order.get("software_take_profit_closed_quantity")))
        + int(_num(order.get("software_stop_closed_quantity")))
        + int(_num(order.get("single_leg_smart_exit_closed_quantity")))
        + int(_num(order.get("instance_flatten_closed_quantity")))
        + int(_num(order.get("instance_flatten_submitted_quantity")))
    )
    return max(0, _filled_quantity(order) - closed)


def _entry_confirmed_no_fill(order: dict[str, Any]) -> bool:
    if _filled_quantity(order) > 0:
        return False
    status = str(order.get("status") or "").lower()
    entry_status = str(((order.get("entry_detail") or {}).get("status") or (order.get("entry_detail") or {}).get("order_status") or "")).lower()
    message = str(order.get("message") or "").lower()
    return (
        status == "entry_terminal_no_stop"
        or entry_status in {"rejected", "cancelled", "canceled", "expired"}
        or "without any fill" in message
    )


def _protection_state_label(value: Any) -> str:
    labels = {
        "not_started": "未开始",
        "pending": "待确认",
        "protected": "券商保护",
        "software_protected": "软件保护",
        "strategy_protected": "组合策略追踪中",
        "strategy_residual_tracking": "残腿追踪中",
        "broker_combo_close_required": "券商要求组合平仓",
        "strategy_partial_exiting": "部分策略退出中",
        "strategy_exiting": "策略退出中",
        "strategy_exited": "自动退出已完成",
        "exited": "已退出",
        "strategy_exit_failed": "策略退出失败",
        "no_position": "无成交仓位",
        "unprotected": "无保护",
        "blocked": "已阻塞",
    }
    text = str(value or "")
    return labels.get(text, text or "未知")


def _candidate_snapshot_card(candidate: dict[str, Any]) -> dict[str, Any]:
    risk_plan = candidate.get("risk_plan") or {}
    return {
        "contract_symbol": candidate.get("contract_symbol"),
        "side": candidate.get("side"),
        "expiration": candidate.get("expiration"),
        "strike": _num(candidate.get("strike")),
        "bid": _num(candidate.get("bid")),
        "ask": _num(candidate.get("ask")),
        "mid": _num(candidate.get("mid")),
        "spread_pct": _num(candidate.get("spread_pct")),
        "volume": int(_num(candidate.get("volume"))),
        "open_interest": int(_num(candidate.get("open_interest"))),
        "implied_volatility": _num(candidate.get("implied_volatility")),
        "iv_percentile": _num(candidate.get("iv_percentile")),
        "delta": _num(candidate.get("delta")),
        "gamma": _num(candidate.get("gamma")),
        "theta_per_day": _num(candidate.get("theta_per_day")),
        "analysis_score": _num(candidate.get("analysis_score")),
        "execution_quality_score": _num(candidate.get("execution_quality_score")),
        "execution_quality_state": candidate.get("execution_quality_state"),
        "strategy_tag": candidate.get("strategy_tag"),
        "pricing_source": candidate.get("pricing_source"),
        "risk_plan": {
            "max_loss_per_contract": risk_plan.get("max_loss_per_contract"),
            "stop_loss_option_price": risk_plan.get("stop_loss_option_price"),
            "take_profit_1": risk_plan.get("take_profit_1"),
            "take_profit_2": risk_plan.get("take_profit_2"),
            "invalidation": risk_plan.get("invalidation"),
            "latest_exit": risk_plan.get("latest_exit"),
        },
        "hard_flags": _candidate_flags(candidate),
    }


def _strategy_snapshot_card(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "family": candidate.get("family"),
        "strategy_type": candidate.get("strategy_type"),
        "label": candidate.get("label"),
        "direction": candidate.get("direction"),
        "expiration": candidate.get("expiration"),
        "legs": candidate.get("legs") or [],
        "net_debit": _num(candidate.get("net_debit")),
        "net_credit": _num(candidate.get("net_credit")),
        "max_loss": candidate.get("max_loss"),
        "max_profit": candidate.get("max_profit"),
        "breakevens": candidate.get("breakevens") or [],
        "capital_required": _num(candidate.get("capital_required")),
        "probability_hint": _num(candidate.get("probability_hint")),
        "score": _num(candidate.get("score")),
        "fit_notes": candidate.get("fit_notes") or [],
        "hard_flags": candidate.get("hard_flags") or [],
        "summary": candidate.get("summary"),
    }


def _selection_decision_card(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": selection.get("symbol"),
        "contract_symbol": selection.get("contract_symbol"),
        "selection_source": selection.get("selection_source"),
        "allocation_pct": _num(selection.get("allocation_pct")),
        "stop_loss_pct": _num(selection.get("stop_loss_pct")),
        "entry_price": _num(selection.get("entry_price")),
        "entry_order_type": selection.get("entry_order_type"),
        "confidence_score": _num(selection.get("confidence_score")),
        "reason": selection.get("reason"),
    }


def _advisor_report_card(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": report.get("key"),
        "advisor": report.get("advisor"),
        "status": report.get("status"),
        "structured_report": report.get("structured_report") or {},
        "raw_preview": _preview(report.get("report")),
    }


def _preview(value: Any, limit: int = 900) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit]}..."


def _candidate_flags(candidate: Any) -> list[str]:
    if not isinstance(candidate, dict):
        return []
    flags = []
    ask = _num(candidate.get("ask"))
    spread_pct = _num(candidate.get("spread_pct"))
    volume = _num(candidate.get("volume"))
    open_interest = _num(candidate.get("open_interest"))
    if ask <= 0:
        flags.append("invalid_ask")
    if spread_pct > 35:
        flags.append("wide_spread")
    if volume < 100 and open_interest < 1000:
        flags.append("thin_liquidity")
    if str(candidate.get("pricing_source") or "") == "unavailable":
        flags.append("quote_unavailable")
    return flags


def _deep_merge(defaults: dict[str, Any], overrides: Any) -> dict[str, Any]:
    output = dict(defaults)
    if not isinstance(overrides, dict):
        return output
    for key, value in overrides.items():
        if isinstance(output.get(key), dict) and isinstance(value, dict):
            output[key] = _deep_merge(output[key], value)
        else:
            output[key] = value
    return output


def _allow_overnight(risk_plan: dict[str, Any]) -> bool:
    latest_exit = str(risk_plan.get("latest_exit") or "")
    return "当日" not in latest_exit and "收盘前" not in latest_exit


def _status_indicates_filled(status: str) -> bool:
    """True only when a status string denotes a (fully) filled entry.

    Uses token matching, NOT a naive ``"filled" in status`` substring test —
    the substring approach wrongly matched ``"unfilled"`` and
    ``"partially_filled"`` / ``"partial_filled"``, which over-counted protected
    quantity and masked naked positions. We exclude any token that is a
    not-filled / partial-fill state and only accept genuine filled tokens.
    """
    normalized = str(status or "").strip().lower()
    if not normalized:
        return False
    # Explicit not-filled / partial states must never count as filled.
    not_filled = {
        "unfilled",
        "entry_submitted_stop_pending_unfilled",
        "not_armed_no_filled_quantity",
        "auto_exit_no_filled_legs",
        "no_filled_legs",
    }
    if normalized in not_filled:
        return False
    if "unfilled" in normalized:
        return False
    if "partial" in normalized:
        # Partial fills are tracked via entry_filled_quantity, not treated as
        # a complete fill by the status string.
        return False
    # Token-level match for genuine filled states (filled, fully_filled,
    # stop_submitted_after_fill, entry_filled_stop_unsupported_paper, ...).
    tokens = set(re.split(r"[^a-z0-9]+", normalized))
    if {"filled", "fill"} & tokens:
        return True
    return normalized in {"fully_filled", "fullfilled"}


def _is_filled_status(order: dict[str, Any]) -> bool:
    # Authoritative signal: a positive confirmed fill quantity.
    if int(_num(order.get("entry_filled_quantity"))) > 0:
        return True
    status = str(order.get("status") or "")
    entry_status = str(((order.get("entry_detail") or {}).get("status") or (order.get("entry_detail") or {}).get("order_status") or ""))
    return _status_indicates_filled(status) or _status_indicates_filled(entry_status)


def _stop_unsupported(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "")
    message = f"{order.get('stop_error') or ''} {order.get('message') or ''}".lower()
    return (
        status == "entry_filled_stop_unsupported_paper"
        or "not supported under paper account" in message
        or "604050" in message
        or "native_stop_unsupported" in message
    )


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

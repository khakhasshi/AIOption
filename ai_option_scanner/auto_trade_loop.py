"""Per-cycle engine for the fully-automatic LLM trading feature.

Design: this module is the ORCHESTRATION + MEMORY + SESSION-AWARENESS + AUDIT
layer. It does NOT reimplement scanning/AI/decision/execution — it delegates the
analyze→decide→(simulate|submit) work to the hardened trading-run pipeline
(``trading_agent``), which already runs the agentic AI, the decision gate,
schema validation, idempotency, the order journal, and protection monitoring.

Per cycle:
  1. Gate on market session + the instance's own per-session caps.
  2. Derive the fine-grained ``intraday_phase`` and build a run config from the
     risk preset, injecting session context + prior-cycle memory into the scan
     prompt so the LLM is session-aware and remembers earlier cycles.
  3. Run one trading run synchronously (dry-run = analysis-only, no broker; live
     = real orders through the hardened submit path), attributing AI usage.
  4. Persist a cycle row linking the run_id + a summary, and roll the instance's
     memory forward so cycle N+1 sees cycle N.
"""
from __future__ import annotations

import os
from datetime import timezone
from typing import Any

from . import auto_trade_store as store
from .account_store import normalize_owner_id, utc_now
from .ai_client import ai_usage_context
from .auto_trade_config import build_run_config, intraday_phase, intraday_phase_label, preset_caps
from .auto_trade_signals import (
    daily_pnl_for_instance,
    macro_regime,
    portfolio_exposure,
    track_record_summary,
)
from .market_calendar import market_environment
from .time_utils import now_et, parse_datetime, to_et_iso
from .trading_store import create_trading_run, get_trading_run

_MEMORY_KEEP = max(1, int(os.getenv("AI_OPTION_AUTO_TRADE_MEMORY_CYCLES", "6") or 6))


def auto_trade_enabled() -> bool:
    return (os.getenv("AI_OPTION_AUTO_TRADE_ENABLED", "true") or "").strip().lower() in {"1", "true", "yes", "on"}


def _session_allows_run(env: dict[str, Any], policy: str) -> bool:
    state = str(env.get("session_state") or "")
    if policy == "include_extended":
        return state in {"premarket", "regular_open", "afterhours"}
    # Default: only the regular session places/simulates trades.
    return state == "regular_open"


def _today_et(env: dict[str, Any]) -> str:
    return str(env.get("date_et") or now_et().date().isoformat())


def _advance_next_run(instance: dict[str, Any], env: dict[str, Any]) -> str:
    """Next wake time. During the session: now + interval. Otherwise: the next
    regular open (so an off-hours instance sleeps until the bell)."""
    interval = max(1, int(instance.get("interval_minutes") or 5))
    current = now_et()
    if str(env.get("session_state") or "") == "regular_open":
        return _plus_minutes(current, interval)
    next_open = parse_datetime(env.get("next_regular_open_at_et"))
    if next_open is None:
        return _plus_minutes(current, interval)
    return next_open.astimezone(timezone.utc).isoformat()


def _plus_minutes(dt, minutes: int) -> str:
    from datetime import timedelta
    return (dt + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()


def _reset_session_counters_if_new_day(instance: dict[str, Any], today: str) -> dict[str, int]:
    if str(instance.get("session_date_et") or "") != today:
        return {"cycles_today": 0, "orders_today": 0, "session_date_et_changed": True}
    return {"cycles_today": int(instance.get("cycles_today") or 0), "orders_today": int(instance.get("orders_today") or 0), "session_date_et_changed": False}


def _memory_block(instance: dict[str, Any]) -> str:
    memory = instance.get("memory") or []
    if not memory:
        return "（这是本交易日的第一次扫描，没有历史记忆。）"
    lines = []
    for entry in memory[-_MEMORY_KEEP:]:
        line = (
            f"- 第{entry.get('cycle_index')}次扫描 [{entry.get('intraday_phase')}] "
            f"{entry.get('finished_at','')[:19]}: {entry.get('thesis') or entry.get('status') or 'n/a'}"
            + (f"（下单 {entry.get('orders',0)}）" if entry.get("orders") else "（未下单）")
        )
        pnl = entry.get("pnl") if isinstance(entry.get("pnl"), dict) else None
        if pnl:
            value = pnl.get("realized_pnl")
            if value is None:
                value = pnl.get("estimated_total_pnl")
            if value is not None:
                tag = "已实现" if pnl.get("source") == "review" else "浮动"
                ret = pnl.get("return_pct")
                line += f"（{tag}盈亏 {value}" + (f"，{ret}%）" if ret is not None else "）")
        lines.append(line)
    return "\n".join(lines)


def _build_prompt_template(instance: dict[str, Any], env: dict[str, Any], phase: str) -> str:
    """Augment the scan prompt with session-phase + cross-cycle memory so the
    LLM is time-aware and has continuity across cycles. {symbol} is substituted
    per-symbol downstream by _prompt_for_symbol."""
    base = str((instance.get("config") or {}).get("prompt_template") or "").strip()
    base_clause = base or "按当前策略模式找出最值得执行的期权方案；没有高质量机会时必须放弃，不要强行交易。"
    return (
        f"【全自动交易 · 时段感知】现在是美东 {env.get('now_et','')[:16]}，交易时段：{intraday_phase_label(phase)}。"
        f"请结合该时段特征调整风格（如刚开盘波动大、午盘清淡、尾盘需控制隔夜风险）。\n"
        f"【历史记忆】最近几次扫描的结论：\n{_memory_block(instance)}\n"
        f"【纪律】只在有真实数据支撑、且通过结构化门禁的高质量机会上交易；缺数据要明说不可编造；宁可观望也不要凑单。\n"
        f"【本次任务】扫描 {{symbol}}：{base_clause}"
    )


def _build_decision_directive(
    instance: dict[str, Any],
    env: dict[str, Any],
    phase: str,
    caps: dict[str, Any],
    config_inner: dict[str, Any],
    *,
    track_record: dict[str, Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    macro: dict[str, Any] | None = None,
) -> str:
    """Session-aware budget/exit directive injected into the decision payload
    (ranking_payload.decision_directive). Tells the LLM its budget, to size via
    allocation_pct, to author smart exits, and that overnight holds are forbidden.
    Deterministic engine fallbacks still apply if the LLM omits/invalidates.

    The optional track_record / portfolio / macro signals are appended as soft
    guidance clauses (Tier 1/2). Each is fail-soft: absent or empty → its clause
    is omitted entirely, never fabricated."""
    session_budget = float(config_inner.get("total_capital") or 0.0)
    per_trade_cap = float(caps.get("max_allocation_pct_per_trade") or 0.0)
    clauses = [
        f"【全自动交易纪律 · 时段感知】当前时段：{intraday_phase_label(phase)}（美东 {str(env.get('now_et') or '')[:16]}）。\n"
        f"【资金】本时段可部署预算约 ${session_budget:,.0f}（total_capital 已是本时段预算）。"
        f"用 allocation_pct 智能分配仓位：把更多预算给确定性更高的机会，单笔不超过预算的 {per_trade_cap*100:.0f}%；"
        f"机会不足时可只用部分预算，宁可少下不要凑单。\n"
        f"【每笔风控】为每个 selection 给出 stop_loss_pct 与 take_profit_pct（按合约波动与时段自定，而非套默认值）。\n"
        f"【智能退出】尽量为每笔交易给出 exit_conditions：time_exit（按持仓时间/到点退出）、"
        f"option_greek/option_greek_change（如 IV/delta 超阈值退出）、underlying_price（标的突破/跌破失效价退出）、"
        f"pnl_giveback（回吐止盈）。\n"
        f"【隔夜纪律】禁止隔夜：必须收盘前平仓（系统已强制 no_overnight 兜底，但你应主动给出收盘前 latest_exit）。\n"
        f"【稳健】无把握的字段可不填，系统会用默认止损/止盈与收盘前平仓兜底；但不要编造数据。"
    ]
    record_clause = _track_record_clause(track_record)
    if record_clause:
        clauses.append(record_clause)
    portfolio_clause = _portfolio_clause(portfolio)
    if portfolio_clause:
        clauses.append(portfolio_clause)
    macro_clause = _macro_clause(macro)
    if macro_clause:
        clauses.append(macro_clause)
    return "\n".join(clauses)


def _track_record_clause(track_record: dict[str, Any] | None) -> str:
    """Feature A — fold the instance owner's realized track record back into the
    prompt so the LLM calibrates against its own outcomes (soft guidance)."""
    if not track_record or int(track_record.get("sample_size") or 0) <= 0:
        return ""
    parts = [f"样本 {int(track_record['sample_size'])} 笔"]
    if track_record.get("win_rate") is not None:
        parts.append(f"胜率 {track_record['win_rate']}%")
    cal = track_record.get("avg_confidence_vs_return")
    if cal is not None:
        # Positive = returns beat stated confidence; negative = overconfident.
        tone = "整体偏保守，可适度加注把握更高的机会" if cal > 5 else (
            "存在过度自信迹象，请下调把握度、收紧入场" if cal < -5 else "信心与收益大体匹配"
        )
        parts.append(f"信心-收益校准 {cal}（{tone}）")
    lessons = track_record.get("recent_lessons") or []
    line = f"【历史战绩】{'，'.join(parts)}。"
    if lessons:
        line += "近期复盘教训：" + "；".join(str(item) for item in lessons[:3]) + "。"
    line += "这是你过去决策的真实结果，请据此校准本次判断，但不要据此编造数据。"
    return line


def _portfolio_clause(portfolio: dict[str, Any] | None) -> str:
    """Feature C — surface current net delta + concentration so the LLM avoids
    piling on same-direction exposure (soft guidance; max_per_symbol is hard)."""
    if not portfolio or not portfolio.get("available"):
        return ""
    net = portfolio.get("net_delta")
    gross = portfolio.get("gross_delta")
    bias = "多头" if (net or 0) > 0 else ("空头" if (net or 0) < 0 else "中性")
    parts = [f"当前 {int(portfolio.get('open_positions') or 0)} 个未平仓，净 delta {net}（方向偏{bias}），总 delta {gross}"]
    top = portfolio.get("top_symbols") or []
    if top:
        parts.append("集中度最高：" + "、".join(f"{row['symbol']}({row['net_delta']})" for row in top[:3]))
    return (
        "【组合敞口】" + "；".join(parts) + "。"
        "若已明显偏向某一方向或某些标的，避免再叠加同向暴露，优先考虑分散或对冲。"
    )


def _macro_clause(macro: dict[str, Any] | None) -> str:
    """Feature D — VIX regime + earnings proximity as a soft market-context
    warning (never a hard block; data can be missing/stale)."""
    if not macro:
        return ""
    parts: list[str] = []
    vix = macro.get("vix") or {}
    if vix.get("available"):
        regime_label = {
            "calm": "平静(<15)", "normal": "正常(15-20)",
            "elevated": "偏高(20-28)", "stressed": "紧张(>28)",
        }.get(str(vix.get("regime")), str(vix.get("regime")))
        trend = "上行" if vix.get("rising") else "回落"
        parts.append(f"VIX {vix.get('vix')}（{regime_label}，{trend}）")
    earnings = macro.get("earnings_soon") or []
    if earnings:
        parts.append("临近财报：" + "、".join(f"{row['symbol']}({row['days']}天)" for row in earnings[:5]))
    if not parts:
        return ""
    return (
        "【市场环境】" + "；".join(parts) + "。"
        "波动率紧张时收紧仓位与止损、避免追高；临近财报的标的注意事件风险（IV 可能虚高、跳空风险大），"
        "如非有意做事件请回避或显著降仓。"
    )


def run_auto_trade_cycle(instance: dict[str, Any]) -> dict[str, Any]:
    """Run one auto-trade cycle for an instance. Returns a summary dict. Safe to
    call from the scheduler; advances next_run_at and persists a cycle row."""
    owner_id = normalize_owner_id(instance.get("owner_id"))
    instance_id = str(instance.get("id"))
    env = market_environment()
    today = _today_et(env)
    phase = intraday_phase(env)
    policy = str(instance.get("session_policy") or "regular_only")

    counters = _reset_session_counters_if_new_day(instance, today)
    cycles_today = counters["cycles_today"]
    orders_today = counters["orders_today"]

    # Always advance the wake time first so a failure can't hot-loop.
    next_run_at = _advance_next_run(instance, env)
    base_update = {
        "last_run_at": utc_now(),
        "next_run_at": next_run_at,
        "session_date_et": today,
        "cycles_today": cycles_today,
        "orders_today": orders_today,
    }
    # Persist the advanced next_run_at NOW, before the slow scan+submit. This is
    # the cross-node duplicate-order claim: list_due_auto_trade_instances only
    # returns instances whose next_run_at <= now, so a peer node that ticks while
    # this cycle is still running (the leader lock's TTL can lapse mid-cycle) sees
    # this instance as not-due and skips it — instead of running the same due
    # instance and double-submitting real orders. Without this write the claim
    # only lands after create_auto_trade_run_and_execute returns (line ~375),
    # leaving the whole scan+submit window unprotected.
    store.update_auto_trade_instance(instance_id, owner_id, base_update)

    if not auto_trade_enabled():
        return {"skipped": "auto_trade_disabled"}

    if not _session_allows_run(env, policy):
        return {"skipped": "outside_session", "session_state": env.get("session_state")}

    caps = preset_caps(str(instance.get("risk_preset") or "conservative"))
    use_broker = bool(instance.get("use_broker"))
    dry_run = not use_broker

    # Per-session order-cycle cap (only counts cycles that PLACED an order).
    cap_reached = orders_today >= int(caps.get("max_order_cycles_per_session") or 4)

    # Loss-based circuit breaker (Feature B): sum today's realized + floating P&L
    # across this instance's runs; if it breaches the preset's daily-loss limit,
    # force the rest of the session to dry-run. Fail-soft — any error → no halt.
    total_capital = float(instance.get("total_capital") or 0.0)
    loss_halt = False
    halted_reason = None
    today_pnl = 0.0
    try:
        today_cycles = _today_cycles(instance_id, owner_id, today)
        pnl = daily_pnl_for_instance(owner_id, today_cycles, today)
        today_pnl = float(pnl.get("total") or 0.0)
        max_loss_pct = float(caps.get("max_daily_loss_pct") or 0.0)
        if total_capital > 0 and max_loss_pct > 0:
            loss_limit = -(max_loss_pct * total_capital)
            if today_pnl <= loss_limit:
                loss_halt = True
                halted_reason = (
                    f"日内亏损熔断：今日 P&L ${today_pnl:,.0f} 触及上限 "
                    f"${loss_limit:,.0f}（{max_loss_pct*100:.0f}% × ${total_capital:,.0f}），本时段转为只分析不下单。"
                )
    except Exception:  # noqa: BLE001 - the breaker must never crash a cycle.
        loss_halt = False

    # Gather soft decision signals (Tier 1/2). Each is fail-soft inside its helper.
    track_record = track_record_summary(owner_id)
    portfolio = portfolio_exposure(owner_id)
    macro = macro_regime(list(instance.get("symbols") or []))

    cycle_index = cycles_today + 1
    cycle_id = store.insert_auto_trade_cycle(
        instance_id, owner_id, cycle_index,
        session_state=str(env.get("session_state") or ""), intraday_phase=phase, dry_run=dry_run,
    )

    # Build the run config: preset fields + augmented session/memory prompt.
    run_config = build_run_config(instance)
    config_inner = dict(run_config)
    config_inner["prompt_template"] = _build_prompt_template(instance, env, phase)
    config_inner["decision_directive"] = _build_decision_directive(
        instance, env, phase, caps, config_inner,
        track_record=track_record, portfolio=portfolio, macro=macro,
    )
    config_inner["dry_run"] = dry_run
    if cap_reached or loss_halt:
        # Past the order budget OR loss breaker tripped: still scan/analyze for the
        # audit log, but force analysis-only (no new orders) this cycle.
        config_inner["dry_run"] = True
        config_inner["live_enabled"] = False

    run_id = None
    run_status = None
    summary: dict[str, Any] = {
        "cycle_index": cycle_index,
        "intraday_phase": phase,
        "dry_run": bool(config_inner.get("dry_run")),
        "order_cap_reached": cap_reached,
        "loss_halt": loss_halt,
        "today_pnl": round(today_pnl, 2),
    }
    if halted_reason:
        summary["halted_reason"] = halted_reason
    error = None
    try:
        with ai_usage_context(source_type="auto_trade", source_id=instance_id, request_role="auto_trade_cycle"):
            # Import here to avoid a circular import at module load.
            from .trading_agent import create_auto_trade_run_and_execute
            run = create_auto_trade_run_and_execute(owner_id, config_inner, trigger_source=f"auto:{instance_id}:{cycle_index}")
        run_id = run.get("id") if isinstance(run, dict) else None
        if run_id:
            fresh = get_trading_run(run_id, owner_id, light=True) or {}
            run_status = str(fresh.get("status") or "")
            placed = _orders_placed(fresh)
            summary.update({"run_status": run_status, "stage": fresh.get("stage"), "orders": placed})
            if placed > 0 and not config_inner.get("dry_run"):
                orders_today += 1
    except Exception as exc:  # noqa: BLE001 - record the failure on the cycle, never crash the scheduler.
        error = str(exc)[:300]
        summary["error"] = error

    # Roll memory forward so the next cycle remembers this one — including the
    # realized/floating P&L of this cycle's run, so cycle N+1 sees what actually
    # happened, not just the thesis text (Feature A).
    cycle_pnl = _cycle_run_pnl(run_id, owner_id) if run_id else None
    memory = list(instance.get("memory") or [])
    memory.append({
        "cycle_index": cycle_index,
        "intraday_phase": phase,
        "finished_at": utc_now(),
        "status": run_status or ("error" if error else "no_run"),
        "orders": summary.get("orders", 0),
        "thesis": summary.get("thesis"),
        "run_id": run_id,
        "pnl": cycle_pnl,
    })
    memory = memory[-_MEMORY_KEEP:]

    store.finish_auto_trade_cycle(
        cycle_id,
        status="error" if error else "completed",
        run_ids=[run_id] if run_id else [],
        summary=summary,
        error=error,
    )
    store.update_auto_trade_instance(instance_id, owner_id, {
        **base_update,
        "cycles_today": cycle_index,
        "orders_today": orders_today,
        "realized_pnl_today": round(today_pnl, 2),
        "halted_reason": halted_reason,
        "memory": memory,
        "last_cycle_summary": summary,
    })
    return summary


def _today_cycles(instance_id: str, owner_id: str, today_et: str) -> list[dict[str, Any]]:
    """This instance's cycle rows whose started_at falls on today's ET date.
    Used to scope the daily-P&L breaker to the current session. Fail-soft → []."""
    try:
        rows = store.list_auto_trade_cycles(instance_id, owner_id, limit=80)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for row in rows or []:
        started = row.get("started_at")
        try:
            if started and str(to_et_iso(started))[:10] == today_et[:10]:
                out.append(row)
        except Exception:  # noqa: BLE001
            continue
    return out


def _cycle_run_pnl(run_id: str, owner_id: str) -> dict[str, Any] | None:
    """Realized (closed) or floating (open) P&L for a single cycle's run, stored
    in cross-cycle memory so the next cycle's prompt reflects real outcomes.
    Fail-soft → None."""
    try:
        from .trade_review_store import get_trade_review

        review = get_trade_review(run_id, owner_id)
        if review and review.get("realized_pnl") is not None:
            return {"realized_pnl": review.get("realized_pnl"), "return_pct": review.get("return_pct"), "source": "review"}
    except Exception:  # noqa: BLE001
        pass
    try:
        run = get_trading_run(run_id, owner_id, light=True) or {}
        metrics = (run.get("trade_instance") or {}).get("review_metrics") or {}
        value = metrics.get("estimated_total_pnl")
        if value is not None:
            return {"estimated_total_pnl": value, "return_pct": metrics.get("return_pct"), "source": "open_run"}
    except Exception:  # noqa: BLE001
        pass
    return None


def _orders_placed(run: dict[str, Any]) -> int:
    orders = run.get("orders") or []
    if isinstance(orders, list):
        return sum(1 for o in orders if isinstance(o, dict) and (o.get("order_id") or o.get("entry_order")))
    return 0

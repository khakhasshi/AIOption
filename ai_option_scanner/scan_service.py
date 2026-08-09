from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import asdict, is_dataclass, replace
from typing import Any, Callable


def _market_data_timeout() -> float:
    raw = os.getenv("AI_OPTION_MARKET_DATA_TIMEOUT")
    try:
        value = float(raw) if raw else 30.0
    except (TypeError, ValueError):
        value = 30.0
    return max(5.0, min(value, 180.0))

from .account_store import preferred_sdk_account, resolve_account
from .ai_decision_guard import DECISION_TEMPERATURE, JSON_RESPONSE_FORMAT, build_strict_analysis_payload, extract_json_object, validate_analysis_decision, validation_status
from .ai_client import ask_ai, ai_usage_context
from .council import run_council
from .intent_planner import plan_scan_intent, plan_scan_intent_rules
from .decision_gate import build_decision_environment_gate
from .intraday_option_tools import (
    apply_decision_scores,
    build_gex_context,
    build_intraday_option_tools,
    enrich_option_analysis,
    enrich_option_greeks,
    normalize_analysis_modules,
    supplement_option_greek_inputs_from_yfinance,
)
from .longbridge_client import kline, intraday, news, quote
from .market_structure import build_volatility_context, build_volume_profile
from .market_math import infer_bias, summarize_daily, summarize_intraday
from .report import STRICT_DECISION_PROMPT, STRICT_EXPLANATION_PROMPT, SYSTEM_PROMPT, build_ai_payload, fallback_report
from .time_utils import time_context, to_et_label, normalize_time_fields
from .longbridge_option_tool import collect_candidates as lb_collect_candidates
from .strategy_structures import build_strategy_candidates, normalize_strategy_modes


def run_scan(
    query: str,
    symbol: str | None = None,
    ai_provider: str = "deepseek",
    longbridge_account: str | None = None,
    use_ai: bool = True,
    council: bool = False,
    analysis_modules: dict[str, Any] | None = None,
    use_ai_planner: bool = True,
    market_data_source: str = "thetadata",
    option_data_source: str = "thetadata",
    market_data_workers: int = 2,
    strategy_modes: list[str] | None = None,
    low_gate_enabled: bool = False,
    progress_callback: Callable[[str, int], None] | None = None,
    ai_provider_owner: str | None = None,
    scan_id: str | None = None,
    scan_loop_instance_id: str | None = None,
    source_type: str = "scan",
) -> dict[str, Any]:
    _progress(progress_callback, "plan_intent", 10)
    modules = normalize_analysis_modules(analysis_modules)
    ai_planner_called = bool(use_ai and use_ai_planner)
    if use_ai and use_ai_planner:
        with ai_usage_context(
            owner_id=ai_provider_owner,
            source_type=source_type,
            source_id=scan_id or symbol or query[:80],
            scan_id=scan_id or "",
            scan_loop_instance_id=scan_loop_instance_id or "",
            symbol=symbol or "",
            council_mode=bool(council),
            radar_scan=source_type == "scan_loop",
            request_role="planner",
        ):
            intent, planner = plan_scan_intent(query, symbol, ai_provider, modules, owner_id=ai_provider_owner)
        modules = normalize_analysis_modules(planner.get("analysis_modules") or modules)
    else:
        intent, planner = plan_scan_intent_rules(query, symbol, modules)
        modules = normalize_analysis_modules(planner.get("analysis_modules") or modules)
    lb_symbol = f"{intent.symbol}.US"
    tool_plan = planner.get("tool_plan") if isinstance(planner.get("tool_plan"), dict) else {}
    parsed_strategy_modes = normalize_strategy_modes(getattr(intent, "strategy_modes", None) or planner.get("strategy_modes"))
    user_strategy_modes = normalize_strategy_modes(strategy_modes) if strategy_modes is not None else []
    effective_strategy_modes = user_strategy_modes if user_strategy_modes and user_strategy_modes != ["single_leg"] else parsed_strategy_modes
    if effective_strategy_modes != getattr(intent, "strategy_modes", []):
        intent = replace(intent, strategy_modes=effective_strategy_modes)
    planner["strategy_modes"] = effective_strategy_modes
    requested_source = _normalize_market_data_source(market_data_source)
    option_source = _normalize_option_data_source(option_data_source)
    # ThetaData Standard supplies the project's required US-stock snapshots plus
    # minute and daily history. It deliberately has no news endpoint, so scans
    # using it leave news empty instead of silently mixing providers.
    underlying_source = requested_source
    use_longbridge = underlying_source == "longbridge"
    account_name = "yfinance"
    market_data_fallback_from = ""

    if use_longbridge:
        actual_market_data_source = "longbridge"
        _progress(progress_callback, "longbridge_market_data", 20)
        account_name, quote_data, daily_candles, intraday_points, news_items = _fetch_longbridge_market_data(
            lb_symbol,
            longbridge_account,
            tool_plan,
            market_data_workers,
        )
    elif underlying_source == "thetadata":
        _progress(progress_callback, "thetadata_market_data", 20)
        try:
            from .thetadata_option_tool import market_data as theta_market_data

            data = theta_market_data(intent.symbol)
            quote_data = normalize_time_fields(data.get("quote") or {}, ("time", "timestamp", "trade_time"))
            daily_candles = [normalize_time_fields(item, ("time",)) for item in data.get("daily") or []]
            intraday_points = [normalize_time_fields(item, ("time",)) for item in data.get("intraday") or []]
            if not float(quote_data.get("last") or quote_data.get("price") or 0) and not daily_candles:
                raise RuntimeError("ThetaData returned no usable stock quote or history")
            news_items = []
            actual_market_data_source = "thetadata"
            account_name = "thetadata"
            tool_plan = {
                **tool_plan,
                "longbridge_quote": False,
                "longbridge_daily_kline": False,
                "longbridge_intraday": False,
                "longbridge_news": False,
                "longbridge_option_chain": False,
                "yfinance_option_chain": False,
                "yfinance_market_data": False,
                "thetadata_market_data": True,
            }
        except Exception as theta_exc:  # noqa: BLE001 - a selected provider must not strand a scan.
            _progress(progress_callback, "longbridge_market_data", 20)
            try:
                account_name, quote_data, daily_candles, intraday_points, news_items = _fetch_longbridge_market_data(
                    lb_symbol,
                    longbridge_account,
                    tool_plan,
                    market_data_workers,
                )
                actual_market_data_source = "longbridge"
                market_data_fallback_from = "thetadata"
                tool_plan = {
                    **tool_plan,
                    "longbridge_quote": True,
                    "longbridge_daily_kline": True,
                    "longbridge_intraday": True,
                    "longbridge_news": True,
                    "yfinance_market_data": False,
                    "thetadata_market_data": False,
                    "thetadata_market_data_error": str(theta_exc)[:240],
                }
            except Exception as longbridge_exc:  # noqa: BLE001 - YFinance is display-only last resort.
                _progress(progress_callback, "yfinance_market_data", 20)
                from .yfinance_option_tool import market_data as yf_market_data

                data = yf_market_data(intent.symbol)
                quote_data = normalize_time_fields(data.get("quote") or {}, ("time", "timestamp", "trade_time"))
                daily_candles = [normalize_time_fields(item, ("time",)) for item in data.get("daily") or []]
                intraday_points = [normalize_time_fields(item, ("time",)) for item in data.get("intraday") or []]
                news_items = [_with_source(normalize_time_fields(item, ("published_at", "time", "created_at")), "yfinance") for item in data.get("news") or []]
                actual_market_data_source = "yfinance"
                market_data_fallback_from = "thetadata,longbridge"
                tool_plan = {
                    **tool_plan,
                    "longbridge_quote": False,
                    "longbridge_daily_kline": False,
                    "longbridge_intraday": False,
                    "longbridge_news": False,
                    "longbridge_option_chain": False,
                    "yfinance_option_chain": False,
                    "yfinance_market_data": True,
                    "thetadata_market_data": False,
                    "thetadata_market_data_error": str(theta_exc)[:240],
                    "longbridge_market_data_error": str(longbridge_exc)[:240],
                }
    else:
        actual_market_data_source = "yfinance"
        _progress(progress_callback, "yfinance_market_data", 20)
        from .yfinance_option_tool import market_data as yf_market_data

        data = yf_market_data(intent.symbol)
        quote_data = normalize_time_fields(data.get("quote") or {}, ("time", "timestamp", "trade_time"))
        daily_candles = [normalize_time_fields(item, ("time",)) for item in data.get("daily") or []]
        intraday_points = [normalize_time_fields(item, ("time",)) for item in data.get("intraday") or []]
        news_items = [_with_source(normalize_time_fields(item, ("published_at", "time", "created_at")), "yfinance") for item in data.get("news") or []]
        tool_plan = {
            **tool_plan,
            "longbridge_quote": False,
            "longbridge_daily_kline": False,
            "longbridge_intraday": False,
            "longbridge_news": False,
            "longbridge_option_chain": False,
            "yfinance_option_chain": True,
            "yfinance_market_data": True,
        }

    _progress(progress_callback, "technical_summary", 45)
    daily_summary = summarize_daily(daily_candles)
    intraday_summary = summarize_intraday(intraday_points)
    intraday_tools = build_intraday_option_tools(quote_data, daily_candles, intraday_points) if modules["intraday"] else {"available": False, "tool_names": []}
    bias = infer_bias(daily_summary, intraday_summary)
    spot = float(quote_data.get("last") or daily_summary.get("close") or 0)
    option_side = _derive_option_side_for_modes(intent.preferred_side, bias, effective_strategy_modes)

    _progress(progress_callback, "option_chain", 60)
    candidates = []
    actual_option_data_source = option_source
    if option_source == "longbridge":
        if use_longbridge:
            option_account_name = account_name
        else:
            # Underlying came from a non-Longbridge source; resolve an SDK account
            # specifically for the Longbridge option chain.
            lb_account = resolve_account(longbridge_account) if longbridge_account else preferred_sdk_account()
            if lb_account is None or not lb_account.sdk_credentials_configured:
                lb_account = preferred_sdk_account()
            if lb_account is None:
                raise ValueError("Longbridge Python SDK API key account is required for Longbridge option chain data")
            option_account_name = lb_account.name
        candidates = lb_collect_candidates(
            symbol=intent.symbol,
            spot=spot,
            min_days=intent.min_days,
            max_days=intent.max_days,
            max_ask=intent.max_ask,
            lottery=intent.lottery,
            preferred_side=option_side,
            min_ask=0.20 if intent.lottery else 0.05,
            account_name=option_account_name,
        )
        tool_plan["longbridge_option_chain"] = True
        tool_plan["yfinance_option_chain"] = False
        tool_plan["thetadata_option_chain"] = False
    elif option_source == "thetadata":
        from .thetadata_option_tool import collect_candidates as theta_collect_candidates

        candidates = theta_collect_candidates(
            symbol=intent.symbol,
            spot=spot,
            min_days=intent.min_days,
            max_days=intent.max_days,
            max_ask=intent.max_ask,
            lottery=intent.lottery,
            preferred_side=option_side,
            min_ask=0.20 if intent.lottery else 0.05,
        )
        # ThetaData (paid options tier) carries per-contract IV but not native Greeks;
        # Greeks are computed via BSM from this IV in enrich_option_greeks below. The
        # yfinance supplement only fills IV for contracts ThetaData did not price.
        candidates = supplement_option_greek_inputs_from_yfinance(candidates, spot) if modules["greeks"] else candidates
        tool_plan["longbridge_option_chain"] = False
        tool_plan["yfinance_option_chain"] = False
        tool_plan["thetadata_option_chain"] = True
    else:
        from .yfinance_option_tool import collect_candidates as yf_collect_candidates

        candidates = yf_collect_candidates(
            symbol=intent.symbol,
            spot=spot,
            min_days=intent.min_days,
            max_days=intent.max_days,
            max_ask=intent.max_ask,
            lottery=intent.lottery,
            preferred_side=option_side,
            min_ask=0.20 if intent.lottery else 0.05,
        )
        tool_plan["longbridge_option_chain"] = False
        tool_plan["yfinance_option_chain"] = True
        tool_plan["thetadata_option_chain"] = False
    candidates = enrich_option_greeks(candidates, spot) if modules["greeks"] else candidates
    candidates = enrich_option_analysis(candidates, spot, intent, modules, daily_candles=daily_candles, intraday_points=intraday_points, news_items=news_items)
    volatility_context = build_volatility_context(candidates, daily_candles, news_items) if modules.get("volatility", True) else {"available": False}
    volume_profile_context = (intraday_tools.get("volume_profile") if isinstance(intraday_tools, dict) else None) or build_volume_profile(intraday_points, daily_candles, spot)
    gex_context = build_gex_context(candidates, spot) if modules.get("gex", True) and modules["greeks"] else {"available": False, "regime": "disabled"}
    decision_gate = build_decision_environment_gate(
        technical_bias=bias,
        daily_summary=daily_summary,
        intraday_summary=intraday_summary,
        intraday_tools=intraday_tools,
        gex_context=gex_context,
        candidates=candidates,
        low_gate_enabled=low_gate_enabled,
    )
    candidates = apply_decision_scores(candidates, decision_gate)
    strategy_candidates = build_strategy_candidates(candidates, spot, effective_strategy_modes, decision_gate=decision_gate) if modules.get("strategy") else []

    payload = build_ai_payload(
        query=query,
        symbol=intent.symbol,
        quote=quote_data,
        daily=daily_summary,
        intraday=intraday_summary,
        bias=bias,
        news=news_items,
        candidates=candidates,
        intraday_option_tools=intraday_tools,
        gex_context=gex_context,
        decision_gate=decision_gate,
        strategy_candidates=strategy_candidates,
        time_context=time_context(),
    )
    payload["intent"] = asdict(intent)
    payload["llm_intent_plan"] = planner
    payload["tool_plan"] = tool_plan
    payload["strategy_modes"] = effective_strategy_modes
    payload["strategy_candidates"] = [_strategy_to_row(item, index) for index, item in enumerate(strategy_candidates[:20], start=1)]
    payload["longbridge_account"] = account_name
    payload["market_data_source"] = actual_market_data_source
    if market_data_fallback_from:
        payload["market_data_fallback_from"] = market_data_fallback_from
    payload["option_data_source"] = actual_option_data_source
    payload["analysis_modules"] = modules
    payload["volatility_context"] = volatility_context
    payload["volume_profile"] = volume_profile_context
    payload["gex_context"] = gex_context
    payload["decision_gate"] = decision_gate
    payload["no_candidate_reason"] = "" if candidates else "No option candidates passed the current price, DTE, direction and liquidity filters."
    if strategy_candidates and not candidates:
        payload["no_candidate_reason"] = "No single-leg candidates passed the current filters, but strategy candidates were still constructed from the remaining chain."

    fallback = fallback_report(intent.symbol, daily_summary, intraday_summary, bias, candidates, strategy_candidates, intraday_tools)
    ai_answer = None
    strict_decision = None
    strict_answer = ""
    validation = None
    council_trace: dict[str, Any] | None = None
    ai_attempted = bool(use_ai and (candidates or strategy_candidates))
    if ai_attempted:
        _progress(progress_callback, "ai_analysis", 82)
        strict_payload = build_strict_analysis_payload(payload)
        if council:
            with ai_usage_context(
                owner_id=ai_provider_owner,
                source_type=source_type,
                source_id=scan_id or intent.symbol,
                scan_id=scan_id or "",
                scan_loop_instance_id=scan_loop_instance_id or "",
                symbol=intent.symbol,
                council_mode=True,
                radar_scan=source_type == "scan_loop",
                request_role="council",
            ):
                council_result = run_council(strict_payload, ai_provider, owner_id=ai_provider_owner)
            if council_result and isinstance(council_result, dict):
                council_trace = {
                    "mode": council_result.get("mode") or "three_advisors",
                    "advisor_reports": council_result.get("advisor_reports") or [],
                    "error": council_result.get("error") or "",
                    "error_detail": council_result.get("error_detail") or "",
                    "failed_advisor": council_result.get("failed_advisor") or "",
                }
                strict_answer = str(council_result.get("final_decision_answer") or "")
            else:
                strict_answer = ""
                council_trace = {
                    "mode": "three_advisors",
                    "advisor_reports": [],
                    "error": "three-advisor council failed to produce a decision",
                }
        else:
            with ai_usage_context(
                owner_id=ai_provider_owner,
                source_type=source_type,
                source_id=scan_id or intent.symbol,
                scan_id=scan_id or "",
                scan_loop_instance_id=scan_loop_instance_id or "",
                symbol=intent.symbol,
                council_mode=False,
                radar_scan=source_type == "scan_loop",
                request_role="decision",
            ):
                strict_answer = ask_ai(
                    STRICT_DECISION_PROMPT,
                    strict_payload,
                    ai_provider,
                    owner_id=ai_provider_owner,
                    temperature=DECISION_TEMPERATURE,
                    response_format=JSON_RESPONSE_FORMAT,
                )
        strict_decision = extract_json_object(strict_answer)
        validation = validate_analysis_decision(strict_decision, strict_payload)
        payload["structured_decision"] = strict_decision or {}
        payload["decision_validation"] = validation
        payload["decision_validation_status"] = validation_status(validation)
        strategy_fallback = None
        if not validation.get("execution_allowed") and strategy_candidates and any(mode != "single_leg" for mode in normalize_strategy_modes(effective_strategy_modes)) and decision_gate.get("should_trade", True) is not False:
            strategy_fallback = _primary_candidate_decision(candidates, None, decision_gate, strategy_candidates, effective_strategy_modes)
            fallback_consistency = strategy_fallback.get("decision_consistency") or {}
            fallback_consistency["message"] = "AI 返回的最终选择不符合当前策略模式，已改用策略评分第一作为最终结构展示。"
            fallback_consistency["ai_selected_contract"] = validation.get("selected_contract_symbol") or ""
            fallback_consistency["ai_selected_strategy"] = validation.get("selected_strategy_key") or ""
            strategy_fallback["decision_consistency"] = fallback_consistency
        if strategy_fallback:
            payload["primary_candidate"] = strategy_fallback.get("primary_candidate")
            payload["primary_strategy"] = strategy_fallback.get("primary_strategy")
            payload["primary_source"] = strategy_fallback.get("primary_source")
            payload["decision_consistency"] = strategy_fallback.get("decision_consistency")
        elif validation.get("execution_allowed"):
            selected_type = str(validation.get("selection_type") or "none")
            if selected_type == "strategy":
                payload["primary_candidate"] = None
                payload["primary_strategy"] = validation.get("selected_strategy")
                payload["primary_source"] = "validated_trade_selection"
                payload["decision_consistency"] = {
                    "version": 1,
                    "status": "consistent" if validation.get("selected_strategy_key") == (payload.get("strategy_candidates") or [{}])[0].get("strategy_key") else "ai_overrode_top_rank",
                    "severity": "ok" if validation.get("selected_strategy_key") == (payload.get("strategy_candidates") or [{}])[0].get("strategy_key") else "warning",
                    "message": "结构化策略决策通过校验，可用于分析与执行。" if validation.get("selected_strategy_key") == (payload.get("strategy_candidates") or [{}])[0].get("strategy_key") else "结构化策略决策与评分第一不同，但已通过证据校验；仅在人工复核后可执行。",
                    "should_trade": True,
                    "top_ranked_contract": "",
                    "top_ranked_strategy": (payload.get("strategy_candidates") or [{}])[0].get("strategy_key") if payload.get("strategy_candidates") else "",
                    "ai_selected_contract": "",
                    "ai_selected_strategy": validation.get("selected_strategy_key") or "",
                    "primary_contract": "",
                    "primary_strategy": validation.get("selected_strategy_key") or "",
                    "primary_source": "validated_trade_selection",
                    "top_decision_score": _num((payload.get("strategy_candidates") or [{}])[0].get("score") if payload.get("strategy_candidates") else 0.0),
                    "primary_decision_score": _num((validation.get("selected_strategy") or {}).get("score")),
                    "score_gap": 0.0,
                    "gate_preferred_execution": decision_gate.get("preferred_execution"),
                    "gate_allow_auto_trade": bool(decision_gate.get("allow_auto_trade", True)),
                }
            else:
                payload["primary_candidate"] = validation.get("selected_candidate")
                payload["primary_strategy"] = None
                payload["primary_source"] = "validated_trade_selection"
                payload["decision_consistency"] = {
                    "version": 1,
                    "status": "consistent" if validation.get("selected_contract_symbol") == payload["option_candidates"][0]["contract_symbol"] else "ai_overrode_top_rank",
                    "severity": "ok" if validation.get("selected_contract_symbol") == payload["option_candidates"][0]["contract_symbol"] else "warning",
                    "message": "结构化决策通过校验，可用于分析与执行。" if validation.get("selected_contract_symbol") == payload["option_candidates"][0]["contract_symbol"] else "结构化决策与评分第一不同，但已通过证据校验；仅在人工复核后可执行。",
                    "should_trade": True,
                    "top_ranked_contract": payload["option_candidates"][0]["contract_symbol"] if payload.get("option_candidates") else "",
                    "ai_selected_contract": validation.get("selected_contract_symbol") or "",
                    "ai_selected_strategy": "",
                    "primary_contract": validation.get("selected_contract_symbol") or "",
                    "primary_strategy": "",
                    "primary_source": "validated_trade_selection",
                    "top_decision_score": _num((payload.get("option_candidates") or [{}])[0].get("decision_score") or (payload.get("option_candidates") or [{}])[0].get("analysis_score")),
                    "primary_decision_score": _num((validation.get("selected_candidate") or {}).get("decision_score") or (validation.get("selected_candidate") or {}).get("analysis_score")),
                    "score_gap": 0.0,
                    "gate_preferred_execution": decision_gate.get("preferred_execution"),
                    "gate_allow_auto_trade": bool(decision_gate.get("allow_auto_trade", True)),
                }
        else:
            payload["primary_candidate"] = None
            payload["primary_strategy"] = None
            payload["primary_source"] = "observe_only"
            payload["decision_consistency"] = {
                "version": 1,
                "status": "observe",
                "severity": "warning" if validation and validation.get("warnings") else "danger",
                "message": "结构化决策未通过校验，系统仅保留观察结论，不自动执行。",
                "should_trade": False,
                "top_ranked_contract": (payload.get("option_candidates") or [{}])[0].get("contract_symbol") if payload.get("option_candidates") else "",
                "top_ranked_strategy": (payload.get("strategy_candidates") or [{}])[0].get("strategy_key") if payload.get("strategy_candidates") else "",
                "ai_selected_contract": validation.get("selected_contract_symbol") if validation else "",
                "ai_selected_strategy": validation.get("selected_strategy_key") if validation else "",
                "primary_contract": "",
                "primary_strategy": "",
                "primary_source": "observe_only",
                "score_gap": 0.0,
                "gate_preferred_execution": decision_gate.get("preferred_execution"),
                "gate_allow_auto_trade": bool(decision_gate.get("allow_auto_trade", True)),
            }
        explanation_payload = {
            "payload_snapshot": strict_payload,
            "validated_decision": validation or {},
            "council_trace": council_trace or {},
        }
        with ai_usage_context(
            owner_id=ai_provider_owner,
            source_type=source_type,
            source_id=scan_id or intent.symbol,
            scan_id=scan_id or "",
            scan_loop_instance_id=scan_loop_instance_id or "",
            symbol=intent.symbol,
            council_mode=bool(council),
            radar_scan=source_type == "scan_loop",
            request_role="explanation",
        ):
            ai_answer = ask_ai(
                STRICT_EXPLANATION_PROMPT,
                explanation_payload,
                ai_provider,
                owner_id=ai_provider_owner,
                temperature=0.1,
            )
    if not ai_answer:
        ai_answer = fallback

    payload["council_trace"] = council_trace or {}
    payload["ai_execution"] = _build_ai_execution(
        requested=bool(use_ai),
        attempted=ai_attempted,
        council_requested=bool(council),
        ai_provider=ai_provider,
        planner_called=ai_planner_called,
        strict_answer=strict_answer,
        strict_decision=strict_decision,
        validation=validation,
        council_trace=council_trace,
        candidate_count=len(candidates),
        strategy_count=len(strategy_candidates),
    )
    if "primary_candidate" not in payload:
        primary_decision = _primary_candidate_decision(candidates, None, decision_gate, strategy_candidates, effective_strategy_modes)
        payload["primary_candidate"] = primary_decision["primary_candidate"]
        payload["primary_strategy"] = primary_decision.get("primary_strategy")
        payload["primary_source"] = primary_decision["primary_source"]
        payload["decision_consistency"] = primary_decision["decision_consistency"]
    payload["analysis_trace"] = build_analysis_trace(payload, used_ai=bool(use_ai and strict_answer), council_trace=council_trace)

    _progress(progress_callback, "build_response", 95)
    return {
        "answer": ai_answer or fallback,
        "used_ai": bool(use_ai and strict_answer),
        "mode": "council" if council else "single",
        "ai_provider": ai_provider,
        "longbridge_account": account_name,
        "market_data_source": actual_market_data_source,
        "payload": payload,
        "charts": {
            "daily": _compact_candles(daily_candles[-60:]),
            "intraday": _compact_intraday(intraday_points[-240:]),
        },
    }


def _normalize_market_data_source(value: str | None) -> str:
    source = str(value or "thetadata").strip().lower()
    if source == "auto":
        return "thetadata"
    if source in {"longbridge", "yfinance", "thetadata"}:
        return source
    return "thetadata"


def _fetch_longbridge_market_data(
    symbol: str,
    account_name: str | None,
    tool_plan: dict[str, Any],
    worker_count: int,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    account = resolve_account(account_name) if account_name else preferred_sdk_account()
    if account is None or not account.sdk_credentials_configured:
        account = preferred_sdk_account()
    if account is None or not account.sdk_credentials_configured:
        raise ValueError("Longbridge Python SDK API key account is required for Longbridge market data")

    workers = max(1, min(int(worker_count or 2), 4))
    timeout = _market_data_timeout()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="longbridge-fetch") as executor:
        futures = {"quote": executor.submit(quote, symbol, account.name)}
        if tool_plan.get("longbridge_daily_kline", True):
            futures["daily"] = executor.submit(kline, symbol, 80, account.name)
        if tool_plan.get("longbridge_intraday", True):
            futures["intraday"] = executor.submit(intraday, symbol, account.name)
        if tool_plan.get("longbridge_news", True):
            futures["news"] = executor.submit(news, symbol, account.name)
        try:
            quote_data = normalize_time_fields(futures["quote"].result(timeout=timeout), ("time", "timestamp", "trade_time"))
            daily_candles = [normalize_time_fields(item, ("time",)) for item in futures["daily"].result(timeout=timeout)] if "daily" in futures else []
            intraday_points = [normalize_time_fields(item, ("time",)) for item in futures["intraday"].result(timeout=timeout)] if "intraday" in futures else []
            news_items = [_with_source(normalize_time_fields(item, ("published_at", "time", "created_at")), "longbridge") for item in futures["news"].result(timeout=timeout)] if "news" in futures else []
        except FuturesTimeoutError as exc:
            for pending in futures.values():
                pending.cancel()
            raise RuntimeError(f"longbridge market data fetch timed out after {timeout:.0f}s for {symbol}") from exc
    return account.name, quote_data, daily_candles, intraday_points, news_items


def _normalize_option_data_source(value: str | None) -> str:
    """Normalize the option-chain data source. ThetaData is the default/primary
    provider for option quotes, IV and open interest; longbridge / yfinance remain
    available as alternates/fallbacks."""
    source = str(value or "thetadata").strip().lower()
    if source == "auto":
        return "thetadata"
    if source in {"longbridge", "yfinance", "thetadata"}:
        return source
    return "thetadata"


def _progress(callback: Callable[[str, int], None] | None, stage: str, progress: int) -> None:
    if callback:
        callback(stage, progress)


def _derive_option_side(preferred_side: str | None, bias: str) -> str | None:
    if preferred_side is not None:
        return preferred_side
    if bias.startswith("bullish"):
        return "call"
    if bias == "bearish":
        return "put"
    return None


def _derive_option_side_for_modes(preferred_side: str | None, bias: str, strategy_modes: list[str] | None) -> str | None:
    modes = normalize_strategy_modes(strategy_modes)
    if any(mode in {"straddle", "strangle", "collar", "iron_condor", "calendar", "diagonal"} for mode in modes):
        return None
    if "cash_secured_put" in modes and "single_leg" not in modes:
        return "put"
    if "covered_call" in modes and "single_leg" not in modes:
        return "call"
    if "poor_mans_covered_call" in modes and "single_leg" not in modes:
        return "call"
    if "butterfly" in modes and "single_leg" not in modes:
        return "call"
    return _derive_option_side(preferred_side, bias)


def _build_ai_execution(
    *,
    requested: bool,
    attempted: bool,
    council_requested: bool,
    ai_provider: str,
    planner_called: bool,
    strict_answer: str | None,
    strict_decision: dict[str, Any] | None,
    validation: dict[str, Any] | None,
    council_trace: dict[str, Any] | None,
    candidate_count: int,
    strategy_count: int,
) -> dict[str, Any]:
    advisor_reports = (council_trace or {}).get("advisor_reports") or []
    errors = (validation or {}).get("errors") or []
    if not requested:
        fallback_reason = "本次扫描未启用 AI。"
    elif not attempted:
        fallback_reason = "已请求 AI，但候选池为空，无法进入严格结构化决策。"
    elif not strict_answer:
        detail = (council_trace or {}).get("error_detail") or (council_trace or {}).get("error")
        fallback_reason = f"已请求 AI，但严格决策调用没有返回可用答案：{detail}" if detail else "已请求 AI，但严格决策调用没有返回可用答案。"
    elif not strict_decision:
        fallback_reason = "AI 返回了答案，但无法解析为 JSON 对象。"
    elif errors:
        fallback_reason = "AI 返回了 JSON，但 schema 校验失败：" + "；".join(str(item) for item in errors[:4])
    else:
        fallback_reason = ""
    return {
        "version": 1,
        "requested": requested,
        "attempted": attempted,
        "provider": ai_provider,
        "planner_called": planner_called,
        "council_requested": council_requested,
        "council_mode": (council_trace or {}).get("mode") or ("three_advisors" if council_requested else "single"),
        "council_returned": bool(advisor_reports),
        "advisor_count": len(advisor_reports),
        "council_error": (council_trace or {}).get("error") or "",
        "council_error_detail": (council_trace or {}).get("error_detail") or "",
        "failed_advisor": (council_trace or {}).get("failed_advisor") or "",
        "strict_answer_present": bool(strict_answer),
        "structured_json_extracted": bool(strict_decision),
        "structured_decision_valid": bool((validation or {}).get("valid")),
        "execution_allowed": bool((validation or {}).get("execution_allowed")),
        "validation_status": validation_status(validation) if validation else "not_run",
        "candidate_count": candidate_count,
        "strategy_count": strategy_count,
        "fallback_reason": fallback_reason,
    }


def build_analysis_trace(payload: dict[str, Any], *, used_ai: bool = False, council_trace: dict[str, Any] | None = None) -> dict[str, Any]:
    intent = payload.get("intent") or {}
    planner = payload.get("llm_intent_plan") or {}
    tool_plan = payload.get("tool_plan") or {}
    ai_execution = payload.get("ai_execution") or {}
    gate = payload.get("decision_gate") or {}
    validation = payload.get("decision_validation") or {}
    consistency = payload.get("decision_consistency") or {}
    structured = payload.get("structured_decision") or {}
    option_candidates = payload.get("option_candidates") or []
    strategy_candidates = payload.get("strategy_candidates") or []
    top_candidate = option_candidates[0] if option_candidates else {}
    top_strategy = strategy_candidates[0] if strategy_candidates else {}
    primary_candidate = payload.get("primary_candidate") or {}
    primary_strategy = payload.get("primary_strategy") or {}
    volatility = payload.get("volatility_context") or {}
    volume_profile = payload.get("volume_profile") or {}
    stages = [
        {
            "key": "intent",
            "title": "意图识别",
            "status": "passed",
            "summary": f"{payload.get('symbol') or '--'} · {', '.join(intent.get('strategy_modes') or payload.get('strategy_modes') or []) or 'single_leg'} · DTE {intent.get('min_days', '--')}-{intent.get('max_days', '--')}",
            "items": [
                {"label": "标的", "value": payload.get("symbol") or "--"},
                {"label": "方向", "value": intent.get("preferred_side") or "自动"},
                {"label": "DTE", "value": f"{intent.get('min_days', '--')}-{intent.get('max_days', '--')} 天"},
                {"label": "策略模式", "value": ", ".join(intent.get("strategy_modes") or payload.get("strategy_modes") or []) or "--"},
                {"label": "规划来源", "value": planner.get("source") or "--"},
                {"label": "规划置信度", "value": _percent(_num(planner.get("confidence")) * 100) if planner.get("confidence") is not None else "--"},
            ],
            "notes": _compact_notes([planner.get("reasoning"), *(intent.get("semantic_notes") or [])], 6),
        },
        {
            "key": "data",
            "title": "数据与候选",
            "status": "passed" if option_candidates or strategy_candidates else "blocked",
            "summary": f"{payload.get('market_data_source') or '--'} · 单腿 {len(option_candidates)} · 结构 {len(strategy_candidates)}",
            "items": [
                {"label": "行情源", "value": payload.get("market_data_source") or "--"},
                {"label": "账号", "value": payload.get("longbridge_account") or "--"},
                {"label": "工具链", "value": _enabled_tools(tool_plan)},
                {"label": "单腿候选", "value": len(option_candidates)},
                {"label": "策略候选", "value": len(strategy_candidates)},
                {"label": "新闻源", "value": payload.get("latest_news_source") or "none"},
                {"label": "RV20 / RV60", "value": f"{_percent(_num(volatility.get('rv20')) * 100)} / {_percent(_num(volatility.get('rv60')) * 100)}"},
                {"label": "筹码峰 POC", "value": _round(volume_profile.get("poc")) if volume_profile.get("available") else "--"},
            ],
            "notes": _compact_notes([payload.get("no_candidate_reason")], 3),
        },
        {
            "key": "gate",
            "title": "环境门控",
            "status": "blocked" if gate.get("should_trade") is False else "warning" if gate.get("allow_auto_trade") is False or gate.get("warnings") else "passed",
            "summary": gate.get("summary") or gate.get("regime") or "门控完成",
            "items": [
                {"label": "是否交易", "value": "否" if gate.get("should_trade") is False else "可观察"},
                {"label": "自动执行", "value": "否" if gate.get("allow_auto_trade") is False else "允许"},
                {"label": "执行偏好", "value": gate.get("preferred_execution") or "--"},
                {"label": "环境", "value": gate.get("regime") or "--"},
                {"label": "置信度", "value": _percent(_num(gate.get("confidence")) * 100) if gate.get("confidence") is not None else "--"},
            ],
            "notes": _compact_notes([*(gate.get("blockers") or []), *(gate.get("warnings") or [])], 8),
        },
        {
            "key": "scoring",
            "title": "候选评分路径",
            "status": "passed" if top_candidate or top_strategy else "blocked",
            "summary": _score_summary(top_candidate, top_strategy),
            "items": [
                {"label": "单腿第一", "value": top_candidate.get("contract_symbol") or "--"},
                {"label": "单腿决策分", "value": _round(top_candidate.get("decision_score") or top_candidate.get("analysis_score"))},
                {"label": "Alpha / 执行", "value": f"{_round(top_candidate.get('alpha_score'))} / {_round(top_candidate.get('execution_score') or top_candidate.get('execution_quality_score'))}"},
                {"label": "触发", "value": f"{_round(top_candidate.get('trigger_score'))} · {top_candidate.get('trigger_state') or '--'}"},
                {"label": "结构第一", "value": top_strategy.get("label") or top_strategy.get("strategy_key") or "--"},
                {"label": "结构评分", "value": _round(top_strategy.get("score"))},
            ],
        },
        {
            "key": "ai_decision",
            "title": "AI 结构化选择",
            "status": (
                "passed"
                if validation.get("execution_allowed")
                else "warning"
                if ai_execution.get("requested") or structured
                else "skipped"
            ),
            "summary": structured.get("summary") or structured.get("rationale") or ai_execution.get("fallback_reason") or ("使用非 AI 规则回退" if not used_ai else "AI 未返回可执行结构化选择"),
            "items": [
                {"label": "AI 请求", "value": "是" if ai_execution.get("requested", used_ai) else "否"},
                {"label": "AI 规划", "value": "是" if ai_execution.get("planner_called") else "否"},
                {"label": "决策调用", "value": "是" if ai_execution.get("attempted", used_ai) else "否"},
                {"label": "结构化有效", "value": "是" if ai_execution.get("structured_decision_valid") else "否"},
                {"label": "三顾问请求", "value": "是" if ai_execution.get("council_requested") else "否"},
                {"label": "三顾问返回", "value": f"{ai_execution.get('advisor_count', len((council_trace or {}).get('advisor_reports') or []))} 份"},
                {"label": "失败顾问", "value": ai_execution.get("failed_advisor") or "--"},
                {"label": "动作", "value": structured.get("action") or "--"},
                {"label": "选择类型", "value": structured.get("selection_type") or "--"},
                {"label": "合约", "value": structured.get("selected_contract_symbol") or "--"},
                {"label": "策略", "value": structured.get("selected_strategy_key") or "--"},
            ],
            "evidence": _compact_evidence(structured.get("evidence") or []),
            "notes": _compact_notes(structured.get("warnings") or [], 6),
        },
        {
            "key": "validation",
            "title": "Schema 校验",
            "status": "passed" if validation.get("execution_allowed") else "warning" if validation else "skipped",
            "summary": validation.get("summary") or consistency.get("message") or "结构化校验记录",
            "items": [
                {"label": "校验状态", "value": payload.get("decision_validation_status") or "--"},
                {"label": "允许执行", "value": "是" if validation.get("execution_allowed") else "否"},
                {"label": "最终来源", "value": payload.get("primary_source") or "--"},
                {"label": "最终合约", "value": (primary_candidate or {}).get("contract_symbol") or consistency.get("primary_contract") or "--"},
                {"label": "最终策略", "value": (primary_strategy or {}).get("strategy_key") or consistency.get("primary_strategy") or "--"},
                {"label": "一致性", "value": consistency.get("status") or "--"},
            ],
            "notes": _compact_notes([*(validation.get("errors") or []), *(validation.get("warnings") or []), consistency.get("message")], 8),
        },
    ]
    return {
        "version": 1,
        "kind": "analysis_scan",
        "generated_at": (payload.get("time_context") or {}).get("now"),
        "summary": consistency.get("message") or structured.get("summary") or "分析轨迹已生成。",
        "stages": stages,
    }


def _enabled_tools(tool_plan: dict[str, Any]) -> str:
    labels = [key for key, value in tool_plan.items() if value]
    return ", ".join(labels[:8]) if labels else "--"


def _score_summary(top_candidate: dict[str, Any], top_strategy: dict[str, Any]) -> str:
    if top_strategy:
        return f"结构第一：{top_strategy.get('label') or top_strategy.get('strategy_key')}，评分 {_round(top_strategy.get('score'))}"
    if top_candidate:
        return f"单腿第一：{top_candidate.get('contract_symbol')}，决策分 {_round(top_candidate.get('decision_score') or top_candidate.get('analysis_score'))}"
    return "没有可评分候选。"


def _compact_evidence(evidence: list[Any], limit: int = 8) -> list[dict[str, Any]]:
    rows = []
    for item in evidence[:limit] if isinstance(evidence, list) else []:
        if isinstance(item, dict):
            rows.append({"field": item.get("field") or "--", "value": item.get("value"), "supports": item.get("supports") or ""})
    return rows


def _compact_notes(notes: list[Any], limit: int) -> list[str]:
    rows = []
    for item in notes:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            rows.append(text)
    return rows[:limit]


def _round(value: Any) -> str:
    if value is None:
        return "--"
    return f"{_num(value):.2f}"


def _percent(value: float) -> str:
    return f"{value:.2f}%"


def _with_source(item: dict[str, Any], source: str) -> dict[str, Any]:
    item.setdefault("source", source)
    return item


def _derive_ai_selection(ai_answer: str | None, candidates: list[Any]) -> dict[str, Any] | None:
    if not ai_answer or not candidates:
        return None
    candidate_rows = {
        str(_candidate_value(candidate, "contract_symbol") or ""): _candidate_to_row(candidate)
        for candidate in candidates
    }
    candidate_symbols = set(candidate_rows)
    if not candidate_symbols:
        return None

    markers = ("最终单腿", "最终选择", "合约代码", "最终方案", "最终合约")
    for marker in markers:
        index = ai_answer.find(marker)
        if index < 0:
            continue
        segment = ai_answer[index:index + 1600]
        for symbol in _contract_symbols_in_text(segment):
            if symbol in candidate_symbols:
                return {
                    "contract_symbol": symbol,
                    "candidate": candidate_rows[symbol],
                    "source": f"ai_answer_section:{marker}",
                }

    mentioned = [symbol for symbol in _contract_symbols_in_text(ai_answer) if symbol in candidate_symbols]
    unique = []
    for symbol in mentioned:
        if symbol not in unique:
            unique.append(symbol)
    if len(unique) == 1:
        symbol = unique[0]
        return {
            "contract_symbol": symbol,
            "candidate": candidate_rows[symbol],
            "source": "ai_answer_unique_mention",
        }
    return None


def _primary_candidate_decision(
    candidates: list[Any],
    ai_selection: dict[str, Any] | None,
    decision_gate: dict[str, Any] | None,
    strategy_candidates: list[Any] | None = None,
    strategy_modes: list[str] | None = None,
) -> dict[str, Any]:
    gate = decision_gate or {}
    top_candidate = _candidate_to_row(candidates[0]) if candidates else None
    top_strategy = _candidate_to_row(strategy_candidates[0]) if strategy_candidates else None
    if top_strategy is not None and not top_strategy.get("strategy_key"):
        top_strategy["strategy_key"] = _strategy_key(top_strategy, 1)
    top_contract = str((top_candidate or {}).get("contract_symbol") or "")
    top_strategy_key = str((top_strategy or {}).get("strategy_key") or "")
    ai_candidate = (ai_selection or {}).get("candidate") if ai_selection else None
    ai_contract = str((ai_selection or {}).get("contract_symbol") or "")
    should_trade = bool(gate.get("should_trade", True))
    modes = normalize_strategy_modes(strategy_modes)
    strategy_enabled = any(mode != "single_leg" for mode in modes)
    single_leg_enabled = "single_leg" in modes

    if strategy_enabled and top_strategy is not None and should_trade:
        return {
            "primary_candidate": None,
            "primary_strategy": top_strategy,
            "primary_source": "strategy_score_top",
            "decision_consistency": {
                "version": 1,
                "status": "strategy_top_fallback",
                "severity": "warning",
                "message": "当前选择了多腿策略模式，使用策略评分第一作为最终结构展示。",
                "should_trade": True,
                "top_ranked_contract": "",
                "top_ranked_strategy": top_strategy_key,
                "ai_selected_contract": ai_contract,
                "ai_selected_strategy": "",
                "primary_contract": "",
                "primary_strategy": top_strategy_key,
                "primary_source": "strategy_score_top",
                "score_gap": 0.0,
                "gate_preferred_execution": gate.get("preferred_execution"),
                "gate_allow_auto_trade": bool(gate.get("allow_auto_trade", True)),
            },
        }

    if strategy_enabled and not top_strategy and not single_leg_enabled:
        return {
            "primary_candidate": None,
            "primary_source": "no_strategy_candidates",
            "decision_consistency": {
                "version": 1,
                "status": "blocked",
                "severity": "danger",
                "message": "当前策略模式需要多腿结构，但没有生成可用策略候选。",
                "should_trade": False,
                "top_ranked_contract": top_contract,
                "top_ranked_strategy": "",
                "ai_selected_contract": ai_contract,
                "ai_selected_strategy": "",
                "primary_contract": "",
                "primary_strategy": "",
                "score_gap": 0.0,
                "gate_preferred_execution": gate.get("preferred_execution"),
                "gate_allow_auto_trade": bool(gate.get("allow_auto_trade", True)),
            },
        }

    if not candidates:
        return {
            "primary_candidate": None,
            "primary_source": "no_candidates",
            "decision_consistency": {
                "version": 1,
                "status": "blocked",
                "severity": "danger",
                "message": "候选池为空，无法形成最终合约。",
                "should_trade": False,
                "top_ranked_contract": "",
                "top_ranked_strategy": top_strategy_key,
                "ai_selected_contract": "",
                "primary_contract": "",
                "score_gap": 0.0,
            },
        }

    if not should_trade:
        return {
            "primary_candidate": None,
            "primary_source": "decision_gate_blocked",
            "decision_consistency": {
                "version": 1,
                "status": "gate_blocked",
                "severity": "danger",
                "message": "决策环境门控判定当前不应开仓；候选只作为观察池。",
                "should_trade": False,
                "top_ranked_contract": top_contract,
                "top_ranked_strategy": top_strategy_key,
                "ai_selected_contract": ai_contract,
                "primary_contract": "",
                "score_gap": 0.0,
                "blockers": gate.get("blockers") or [],
                "warnings": gate.get("warnings") or [],
            },
        }

    if ai_candidate and ai_contract:
        primary_candidate = ai_candidate
        primary_source = "ai_selected_candidate"
        status = "consistent" if ai_contract == top_contract else "ai_overrode_top_rank"
        severity = "ok" if status == "consistent" else "warning"
        message = "AI 最终合约与决策评分第一一致。" if status == "consistent" else "AI 最终合约偏离决策评分第一，需在理由中解释 alpha / execution 取舍。"
    else:
        primary_candidate = top_candidate
        primary_source = "decision_score_top"
        status = "score_top_fallback"
        severity = "warning"
        message = "未能从 AI 文本中可靠抽取最终合约，使用决策评分第一作为最终展示和后续执行来源。"

    primary_contract = str((primary_candidate or {}).get("contract_symbol") or "")
    top_score = _num((top_candidate or {}).get("decision_score") or (top_candidate or {}).get("analysis_score"))
    primary_score = _num((primary_candidate or {}).get("decision_score") or (primary_candidate or {}).get("analysis_score"))
    return {
        "primary_candidate": primary_candidate,
        "primary_source": primary_source,
        "decision_consistency": {
            "version": 1,
            "status": status,
            "severity": severity,
            "message": message,
            "should_trade": True,
            "top_ranked_contract": top_contract,
            "ai_selected_contract": ai_contract,
            "primary_contract": primary_contract,
            "primary_source": primary_source,
            "top_decision_score": round(top_score, 2),
            "primary_decision_score": round(primary_score, 2),
            "score_gap": round(top_score - primary_score, 2),
            "ai_selection_source": (ai_selection or {}).get("source"),
            "gate_preferred_execution": gate.get("preferred_execution"),
            "gate_allow_auto_trade": bool(gate.get("allow_auto_trade", True)),
        },
    }


def _contract_symbols_in_text(text: str) -> list[str]:
    return [f"{root}{tail}" for root, tail in re.findall(r"\b([A-Z]{1,6})\s*(\d{6}[CP]\d{8})\b", text or "")]


def _candidate_to_row(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, dict):
        return dict(candidate)
    to_dict = getattr(candidate, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if is_dataclass(candidate):
        return asdict(candidate)
    return dict(getattr(candidate, "__dict__", {}) or {})


def _strategy_to_row(candidate: Any, index: int) -> dict[str, Any]:
    row = _candidate_to_row(candidate)
    row["strategy_key"] = _strategy_key(row, index)
    return row


def _strategy_key(row: dict[str, Any], index: int) -> str:
    explicit = str(row.get("strategy_key") or "").strip()
    if explicit:
        return explicit
    return "::".join(
        str(part).replace(" ", "_")
        for part in (
            row.get("family") or "strategy",
            row.get("strategy_type") or "type",
            row.get("expiration") or "exp",
            row.get("label") or index,
        )
    )


def _candidate_value(candidate: Any, key: str) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(key)
    return getattr(candidate, key, None)


def _compact_candles(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "time": str(item.get("time", "")),
            "time_label": to_et_label(item.get("time")),
            "open": _num(item.get("open")),
            "high": _num(item.get("high")),
            "low": _num(item.get("low")),
            "close": _num(item.get("close")),
            "volume": _num(item.get("volume")),
        }
        for item in candles
    ]


def _compact_intraday(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "time": str(item.get("time", "")),
            "time_label": to_et_label(item.get("time")),
            "price": _num(item.get("price")),
            "vwap": _num(item.get("avg_price")),
            "volume": _num(item.get("volume")),
        }
        for item in points
    ]


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

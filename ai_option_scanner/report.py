from __future__ import annotations

from dataclasses import asdict
from typing import Any


SYSTEM_PROMPT = """你是一个美股期权扫描智能体。你必须基于给定 JSON 数据做判断，不能编造价格。
所有时间、日期、K线、分时、新闻发布时间、退出时间，都必须按 America/New_York 美东交易所时间理解和输出；不要使用服务器本地时区或北京时间。
输出中文，简洁但可交易。任务是挑一个最适合当前场景的方案；如果 strategy_candidates 存在，你需要在单腿与多腿结构之间做比较，说明最终选择、方向、触发位、止损、目标和为什么不是其它候选。
如果 user_query 或 intent.day_trade 涉及日内交易，且 intraday_option_tools.available 为 true，必须优先参考 intraday_option_tools：VWAP 结构、ORB、RVOL、EMA9/20、多周期趋势、关键位、MACD 动能，以及候选期权的 delta/gamma/theta/breakeven。
如果 payload.gex_context.available=true，必须参考 GEX：positive_gamma 更偏钉住/均值回归，negative_gamma 更偏顺势加速；不要在明显正 gamma 墙附近把方向性单腿说成高确定性突破。
如果 payload.decision_gate.should_trade=false，最终方案必须明确建议不交易或等待，不要硬选合约；如果 allow_auto_trade=false，只能给观察/限价等待方案，不能建议立即实盘市价进入。
必须参考候选字段里的 volatility structure、strategy_tag、scenario_prices、probability_breakeven、reward_risk_score、risk_plan，避免选择 IV 已明显过热且赔率不足的合约。
候选字段里的 alpha_score 与 execution_score 含义不同：alpha_score 代表方向/赔率潜力，execution_score 代表真实可成交性和保护质量；最终选择必须同时解释两者。
如果 payload.primary_candidate 存在，它是展示和执行的单一来源；如果你选择了与决策评分第一不同的合约，必须明确说明为什么。
如果 payload 里存在 strategy_candidates，你必须把它们作为“结构备选”进行比较，说明哪个结构最适合当前场景；单腿与多腿结构不要混为一谈。
最终输出必须包含：最大亏损、失效条件、止损触发、止盈区间、最晚退出时间。
必须提示：不是投资建议；期权可能归零；不要市价单，优先限价单。
"""


STRICT_DECISION_PROMPT = """你是期权扫描的结构化决策器，只能输出一个 JSON object，不能输出 markdown 或自然语言段落。
你不能编造任何合约、策略、价格、新闻或指标。你只能从 user payload 的 option_candidates.contract_symbol 或 strategy_candidates.strategy_key 中选择。

硬性规则：
- 如果 decision_gate.should_trade=false，action 必须是 "no_trade" 或 "observe"。
- 如果 decision_gate.allow_auto_trade=false，action 不能是 "trade"，只能是 "observe"。
- 单腿只能选择 option_candidates 中存在的 selected_contract_symbol。
- 策略只能选择 strategy_candidates 中存在的 selected_strategy_key。
- 每个 trade/observe 结论必须提供 evidence，evidence 每一项必须包含 field、value、supports。
- 单腿 evidence 必须覆盖 decision_score、alpha_score、execution_score、gex_context 或候选 gex、risk_plan。
- 缺失必要证据时，不要硬选，action 用 "observe"。
- 自由文本要短，rationale 不超过 80 个中文字。

JSON schema:
{
  "action": "trade|observe|no_trade",
  "selection_type": "single_leg|strategy|none",
  "selected_contract_symbol": "候选池里的合约或空字符串",
  "selected_strategy_key": "策略候选里的 strategy_key 或空字符串",
  "summary": "一句话结论",
  "rationale": "短理由",
  "evidence": [
    {"field": "option_candidates[0].decision_score", "value": 12.3, "supports": "为什么支持该结论"}
  ],
  "risk": {
    "max_loss_source": "risk_plan.max_loss_per_contract 或 strategy.max_loss",
    "stop_loss_source": "risk_plan.stop_loss_option_price 或 strategy stop_loss_pnl",
    "take_profit_source": "risk_plan.take_profit_1/take_profit_2",
    "latest_exit_source": "risk_plan.latest_exit"
  },
  "warnings": ["简短风险"]
}
"""


STRICT_EXPLANATION_PROMPT = """你是期权扫描的解释器。你只能根据 validated_decision 和 payload_snapshot 写短中文说明，不能重新选择合约，不能编造新价格。
输出必须简洁，控制在 10 行以内。
如果 validated_decision.action 不是 trade，必须明确写“当前不自动执行/仅观察”。
如果 council_trace.advisor_reports 存在，必须用一行概括“三个诸葛亮”的分歧或共识，但不能改变 validated_decision 的最终选择。
必须包含：结论、证据、风险、触发/退出、免责声明。
"""


def build_ai_payload(
    query: str,
    symbol: str,
    quote: dict[str, Any],
    daily: dict[str, Any],
    intraday: dict[str, Any],
    bias: str,
    news: list[dict[str, Any]],
    candidates: list[Any],
    strategy_candidates: list[Any] | None = None,
    intraday_option_tools: dict[str, Any] | None = None,
    gex_context: dict[str, Any] | None = None,
    decision_gate: dict[str, Any] | None = None,
    time_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "time_context": time_context or {},
        "user_query": query,
        "symbol": symbol,
        "quote": quote,
        "daily_summary": daily,
        "intraday_summary": intraday,
        "technical_bias": bias,
        "latest_news_titles": [
            {
                "title": item.get("title"),
                "published_at": item.get("published_at"),
                "url": item.get("url"),
                "source": item.get("source"),
            }
            for item in news[:12]
        ],
        "latest_news_source": _news_source(news),
        "option_candidates": [asdict(item) for item in candidates[:20]],
        "strategy_candidates": [
            {**(asdict(item) if hasattr(item, "__dataclass_fields__") else dict(item)), "strategy_key": _strategy_key(item, index)}
            for index, item in enumerate((strategy_candidates or [])[:20], start=1)
        ],
        "intraday_option_tools": intraday_option_tools or {},
        "volatility_context": {},
        "volume_profile": (intraday_option_tools or {}).get("volume_profile") if isinstance(intraday_option_tools, dict) else {},
        "gex_context": gex_context or {"available": False},
        "decision_gate": decision_gate or {"should_trade": True, "allow_auto_trade": True},
    }


def _news_source(news: list[dict[str, Any]]) -> str:
    sources = {str(item.get("source") or "").strip() for item in news if item.get("source")}
    sources.discard("")
    if not sources:
        return "none"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def _strategy_key(item: Any, index: int) -> str:
    row = asdict(item) if hasattr(item, "__dataclass_fields__") else dict(item)
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


def fallback_report(
    symbol: str,
    daily: dict[str, Any],
    intraday: dict[str, Any],
    bias: str,
    candidates: list[Any],
    strategy_candidates: list[Any] | None = None,
    intraday_option_tools: dict[str, Any] | None = None,
) -> str:
    if not candidates and not strategy_candidates:
        return f"{symbol}: 没筛到满足价格/流动性条件的单腿期权。"

    strategy_pick = (strategy_candidates or [None])[0]
    if strategy_pick is not None:
        strategy_risk = getattr(strategy_pick, "fit_notes", None) or []
        lines = [
            f"**{symbol} 扫描结论**",
            f"- 现价参考：`{daily.get('close', 0):.2f}`；日内相对 VWAP：`{float(intraday.get('vs_vwap_pct', 0)):.2f}%`；技术偏向：`{bias}`。",
            f"- 结构候选：`{getattr(strategy_pick, 'label', '--')}` / `{getattr(strategy_pick, 'strategy_type', '--')}`；到期 `{getattr(strategy_pick, 'expiration', '--')}`；评分 `{getattr(strategy_pick, 'score', 0):.1f}`。",
            f"- 风格：`{getattr(strategy_pick, 'family', '--')}`；方向：`{getattr(strategy_pick, 'direction', '--')}`；平衡点：`{', '.join(str(v) for v in getattr(strategy_pick, 'breakevens', [])[:2]) or '--'}`。",
            f"- 备注：{', '.join(strategy_risk) if strategy_risk else '结构候选已生成。'}",
        ]
        if candidates:
            pick = candidates[0]
            lines.append(f"- 单腿备选：`{pick.contract_symbol}`，但当前优先展示策略结构，不把单腿当作最终结构方案。")
        lines.append("- 提醒：不是投资建议；期权可能归零；不要市价单，优先限价单。")
        return "\n".join(lines)

    pick = candidates[0]
    risk = pick.risk_plan or {}
    scenarios = pick.scenario_prices or {}
    lines = [
        f"**{symbol} 扫描结论**",
        f"- 现价参考：`{daily.get('close', 0):.2f}`；日内相对 VWAP：`{float(intraday.get('vs_vwap_pct', 0)):.2f}%`；技术偏向：`{bias}`。",
        f"- 单腿候选：`{pick.contract_symbol}`，`{pick.expiration}` 到期 `{pick.strike:g}` `{pick.side.upper()}`。",
        f"- 盘口：bid/ask `{pick.bid:.2f}/{pick.ask:.2f}`；volume `{pick.volume:,}`；OI `{pick.open_interest:,}`；IV `{pick.implied_volatility:.2%}`；delta `{pick.delta:.2f}`；theta/日 `{pick.theta_per_day:.3f}`。",
        f"- 波动率结构：IV `{pick.implied_volatility:.2%}` / RV20 `{getattr(pick, 'rv20', 0):.2%}` / RV60 `{getattr(pick, 'rv60', 0):.2%}`；IV/RV `{getattr(pick, 'iv_rv_ratio', 0):.2f}`；IV Rank `{pick.iv_rank:.0f}` / IV Percentile `{pick.iv_percentile:.0f}`；事件风险 `{getattr(pick, 'event_risk_state', 'low')}`。",
        f"- 策略标签：`{pick.strategy_tag}`；到期 ITM 概率 `{pick.probability_itm:.0f}%`；breakeven 概率 `{pick.probability_breakeven:.0f}%`；触及概率 `{pick.probability_touch:.0f}%`；赔率评分 `{pick.reward_risk_score:.1f}`。",
        f"- 情景推演：正股 +2% 理论价 `{float(scenarios.get('underlying_+2pct_now', 0)):.2f}`；正股 -2% 理论价 `{float(scenarios.get('underlying_-2pct_now', 0)):.2f}`；1 日 theta 后 `{float(scenarios.get('one_day_decay', 0)):.2f}`；3 日 theta 后 `{float(scenarios.get('three_day_decay', 0)):.2f}`。",
    ]
    if intraday_option_tools and intraday_option_tools.get("available"):
        day_bias = intraday_option_tools.get("day_trade_bias", {})
        rvol = intraday_option_tools.get("relative_volume", {})
        orb15 = intraday_option_tools.get("opening_ranges", {}).get("15m", {})
        ema = intraday_option_tools.get("ema_trend", {})
        lines.append(
            f"- 日内工具：bias `{day_bias.get('bias')}`；15m ORB `{orb15.get('state')}`；EMA `{ema.get('state')}`；RVOL `{float(rvol.get('rvol_time_adjusted', 0)):.2f}`。"
        )
        lines.append(
            f"- 关键位：15m ORH `{float(orb15.get('high', 0)):.2f}` / ORL `{float(orb15.get('low', 0)):.2f}`；breakeven `{pick.breakeven:.2f}`。"
        )
        profile = intraday_option_tools.get("volume_profile") or {}
        if profile.get("available"):
            lines.append(
                f"- 筹码峰：POC `{float(profile.get('poc', 0)):.2f}`；VA `{float(profile.get('value_area_low', 0)):.2f}-{float(profile.get('value_area_high', 0)):.2f}`；位置 `{profile.get('position')}`；低量真空上/下 `{float(profile.get('low_volume_room_up_pct', 0)):.1f}%/{float(profile.get('low_volume_room_down_pct', 0)):.1f}%`。"
            )
    lines.extend(
        [
            f"- 触发：按方向等待正股突破/跌破关键位确认；不要提前在横盘里烧 theta。",
            f"- 风控：最大亏损 `${float(risk.get('max_loss_per_contract', pick.ask * 100)):.0f}/张`；止损触发：期权价 `{float(risk.get('stop_loss_option_price', pick.ask * 0.55)):.2f}` 或 `{risk.get('invalidation', '正股触发位失效')}`。",
            f"- 止盈/退出：第一止盈 `{float(risk.get('take_profit_1', pick.ask * 1.5)):.2f}`；第二止盈 `{float(risk.get('take_profit_2', pick.ask * 2.2)):.2f}`；最晚退出 `{risk.get('latest_exit', '到期前退出')}`。",
            "- 提醒：不是投资建议；期权可能归零；不要市价单，优先限价单。",
        ]
    )
    return "\n".join(lines)

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from .account_store import resolve_account, utc_now
from . import adaptive_pricing
from .ai_decision_guard import DECISION_TEMPERATURE, JSON_RESPONSE_FORMAT, extract_json_object
from .ai_client import ask_ai
from .ai_providers import load_providers
from .ai_provider_store import get_user_provider
from .concurrency import env_int
from .longbridge_auth import auth_manager
from .longbridge_client import LongbridgeError
from .broker_client import (
    account_ref_for_config,
    cancel_order,
    display_account_name,
    check as broker_check,
    option_order_symbol,
    order_detail,
    quote_option_contract,
    submit_buy_order,
    submit_sell_order,
    submit_stop_sell_order,
    wait_for_order_fill,
    assets as lb_assets,
    positions as lb_positions,
)
from .broker_store import normalize_broker
from .redis_runtime import redis_available, redis_del, redis_setnx
from .scan_service import run_scan
from .trading_store import (
    DEFAULT_SCHEDULE_SLOTS,
    get_or_create_schedule_session,
    create_trading_run,
    claim_schedule_slot,
    get_trading_config,
    get_trading_run,
    find_recent_order_journal,
    list_trading_runs,
    mark_schedule_slot_fired,
    recover_stale_schedule_slots,
    skip_schedule_slot,
    mark_trading_run,
    normalize_trading_config,
    record_order_journal,
    schedule_config_hash,
    trading_readiness_risk_snapshot,
)
from .trading_idempotency import client_order_key, idempotency_enabled as _idempotency_enabled
from .time_utils import EASTERN, parse_datetime, to_et_iso
from .strategy_structures import normalize_strategy_modes
from .smart_exit_rules import infer_invalidation_rule, normalize_exit_rules, normalize_latest_exit
from .trading_instance_actions import flatten_trade_instance
from .trading_instance import (
    annotate_strategy_order_fill_ledger,
    append_instance_event,
    attach_ai_decision,
    attach_candidate_snapshot,
    attach_order_results,
    attach_risk_and_execution_plan,
    build_protection_status,
    set_lifecycle,
)


class TradingRunBlockedError(ValueError):
    """Raised when a trading entry point is disabled by the saved config."""


_LOG = logging.getLogger(__name__)


LIVE_ADVISORS = [
    {
        "key": "attack",
        "name": "进攻型实盘诸葛亮",
        "prompt": """你是进攻型实盘期权顾问。你会收到多个标的扫描出的单腿期权候选。
你的目标是寻找最有凸性和大额度盈利潜力的实盘买入机会，但只能从 opportunities 中选择，不能编造合约或价格。
重点审查每个 opportunity.evidence_card：趋势/日内动量、催化、赔率、目标涨幅、同标的不同合约的相对吸引力、仓位是否值得。任何结论都必须引用 evidence_card 里的字段名，不能只写感觉。
只能返回 JSON，不要 markdown，不要解释 JSON 外内容。
JSON 格式：
{
  "advisor_key": "attack",
  "summary": "中文摘要",
  "ranked": [
    {
      "contract_symbol": "...",
      "conviction_score": 0-100,
      "liquidity_score": 0-100,
      "risk_score": 0-100,
      "setup_quality": "excellent|good|mixed|poor",
      "allocation_pct": 0.2,
      "stop_loss_pct": 25,
      "reason": "进攻理由、目标区间、最晚退出时间",
      "invalid_if": "放弃或失效条件"
    }
  ],
  "rejected": [{"contract_symbol": "...", "reason": "剔除原因"}],
  "risk_notes": ["组合层面风险"]
}""",
    },
    {
        "key": "risk",
        "name": "风控型实盘诸葛亮",
        "prompt": """你是风控型实盘期权顾问。你会收到多个标的扫描出的单腿期权候选。
你的目标是保护策略资金，优先审查是否值得提交真实订单；只能从 opportunities 中选择，不能编造合约或价格。
重点审查每个 opportunity.evidence_card：bid/ask 价差、成交量/持仓量、IV 是否过热、theta 损耗、到期时间、最大亏损、止损可执行性、总仓位暴露。理由必须引用字段来源。
只能返回 JSON，不要 markdown，不要解释 JSON 外内容。
JSON 格式：
{
  "advisor_key": "risk",
  "summary": "中文摘要",
  "ranked": [
    {
      "contract_symbol": "...",
      "conviction_score": 0-100,
      "liquidity_score": 0-100,
      "risk_score": 0-100,
      "setup_quality": "excellent|good|mixed|poor",
      "allocation_pct": 0.2,
      "stop_loss_pct": 25,
      "reason": "为什么风险可控或需要降仓",
      "invalid_if": "失效条件"
    }
  ],
  "rejected": [{"contract_symbol": "...", "reason": "剔除原因"}],
  "risk_notes": ["组合层面风险"]
}""",
    },
    {
        "key": "skeptic",
        "name": "反方实盘诸葛亮",
        "prompt": """你是反方实盘期权顾问。你会收到多个标的扫描出的单腿期权候选。
你的任务是专门找交易陷阱，不是为了交易而交易；只能从 opportunities 中选择，不能编造合约或价格。
重点审查每个 opportunity.evidence_card：彩票票过贵、IV 结构不划算、新闻噪音、假突破、远 OTM 归零风险、报价陈旧、流动性差、当日不适合交易的理由。反对意见也必须引用证据字段。
只能返回 JSON，不要 markdown，不要解释 JSON 外内容。
JSON 格式：
{
  "advisor_key": "skeptic",
  "summary": "中文摘要",
  "ranked": [
    {
      "contract_symbol": "...",
      "conviction_score": 0-100,
      "liquidity_score": 0-100,
      "risk_score": 0-100,
      "setup_quality": "excellent|good|mixed|poor",
      "allocation_pct": 0.2,
      "stop_loss_pct": 25,
      "reason": "若必须交易，为什么这是最不坏候选",
      "invalid_if": "必须放弃交易的条件"
    }
  ],
  "rejected": [{"contract_symbol": "...", "reason": "剔除原因"}],
  "risk_notes": ["主要反对意见"]
}""",
    },
]


LIVE_MODERATOR_PROMPT = """你是实盘期权交易小组主持人。你会收到合约级 opportunities，以及三个独立 DeepSeek 顾问会话的 JSON 报告。
请模拟三位顾问讨论后的最终决策，只能从 opportunities 里选择 top_n 个最值得实盘买入的单腿期权合约，并决定 allocation_pct 和 stop_loss_pct。
硬性要求：
- 只能返回 JSON，不要 markdown，不要解释 JSON 外的内容。
- selections 只能引用 opportunities 里存在的 contract_symbol，不能编造候选外合约或价格。
- 每个 opportunity 代表一张具体合约；同一标的可能有多张不同到期日/行权价候选，必须比较它们，不要默认选该标的第一张。
- opportunities 可能包含 observation_only=true 的观察候选；你仍要给出相对排序和观察理由，但系统不会对这些候选自动下单。
- 优先参考 evidence_card，candidate 原始字段作为核对；如果 evidence_card.candidate.hard_flags 不为空，通常应剔除或大幅降权。
- selections 必须返回 min(top_n, opportunities数量) 个候选；如果顾问认为大部分都不理想，也要在候选池中按相对吸引力排满 Top N，并在 reason 中写清风险。
- 每个 selection.reason 必须引用至少 2 个字段来源，例如 decision_score、alpha_score、execution_score、gex_context、risk_plan、hard_flags、spread_pct、theta_to_ask_pct。
- allocation_pct 是总策略资金比例，所有入选之和不得超过 1；如果 ai_adjust_allocation=false，系统会改为等权，但你仍需给出完整 Top N 决策和理由。
- stop_loss_pct 是该期权权利金亏损百分比，必须在 1-95 之间，通常 15-45。
- default_take_profit_pct 是系统默认软件止盈百分比；tiered_take_profit_enabled=false 时只做一次性止盈。
- tiered_take_profit_enabled=true 时可使用 take_profit_1_pct / take_profit_2_pct 做分级止盈；ai_adjust_take_profit=false 时系统会覆盖为用户配置。
- 如果 ai_adjust_stop_loss=false，仍可写你的建议，但系统会用默认止损覆盖。
- 若 ranking_payload.decision_directive 非空，必须严格遵守其中的资金/时段/退出纪律（如总预算、单笔上限、禁止隔夜、智能退出要求）。
- 每个 selection 可给出 exit_conditions（智能退出规则数组）：支持 time_exit(exit_at)、no_overnight、underlying_price(突破/跌破失效价)、pnl_giveback(回吐止盈)、option_greek/option_greek_change(指标阈值，如 delta/iv 超阈值退出)；并可给 latest_exit(最晚退出时间，如"收盘前")。无把握时可省略，系统会用默认止损止盈兜底。
- 避免 IV 明显过热、流动性差、theta 压力过高、breakeven 概率过低且赔率不足的合约。
- 下单方式由 execution_context.entry_order_type 决定：market 表示市价单且下单前不重新询价；limit 表示下单前重新询价并用限价单。你的理由必须与该执行方式一致。
- 输出必须体现三个顾问的分歧、共识、剔除理由和最终风险控制。
JSON 格式：
{
  "summary": "中文最终总结",
  "council_mode": "three_advisors",
  "discussion": {
    "attack": "进攻型观点摘要",
    "risk": "风控型观点摘要",
    "skeptic": "反方观点摘要",
    "agreement": "共识",
    "disagreement": "主要分歧"
  },
  "selections": [
    {
      "contract_symbol": "...",
      "symbol": "NVDA",
      "allocation_pct": 0.2,
      "stop_loss_pct": 25,
      "take_profit_pct": 30,
      "take_profit_1_pct": 20,
      "take_profit_2_pct": 35,
      "latest_exit": "收盘前",
      "allow_overnight": false,
      "exit_conditions": [
        {"type": "time_exit", "exit_at": "15:50"},
        {"type": "underlying_price", "direction": "below", "price": 0},
        {"type": "option_greek", "greek": "iv", "op": "above", "value": 0}
      ],
      "reason": "为什么最终入选，包含最大亏损、失效条件、止损触发、止盈区间、最晚退出时间"
    }
  ],
  "rejected": [
    {"contract_symbol": "...", "reason": "剔除理由"}
  ],
  "risk_notes": ["组合层面的风险提示"]
}
"""


LIVE_SINGLE_DECISION_PROMPT = """你是实盘期权交易决策员。你会收到合约级 opportunities。
请只从 opportunities 里选择 top_n 个最值得实盘买入的单腿期权合约，并决定 allocation_pct 和 stop_loss_pct。
硬性要求：
- 只能返回 JSON，不要 markdown，不要解释 JSON 外的内容。
- selections 只能引用 opportunities 里存在的 contract_symbol，不能编造候选外合约或价格。
- opportunities 可能包含 observation_only=true 的观察候选；你仍要给出相对排序和观察理由，但系统不会对这些候选自动下单。
- 每个 selection.reason 必须引用至少 2 个字段来源，例如 decision_score、alpha_score、execution_score、risk_plan、hard_flags、spread_pct、theta_to_ask_pct。
- selections 必须返回 min(top_n, opportunities数量) 个候选；如果候选质量一般，也要按相对吸引力排满 Top N，并在 reason 中写清风险。
- allocation_pct 是总策略资金比例，所有入选之和不得超过 1。
- stop_loss_pct 是该期权权利金亏损百分比，必须在 1-95 之间，通常 15-45。
- default_take_profit_pct 是系统默认软件止盈百分比；tiered_take_profit_enabled=false 时只做一次性止盈。
- tiered_take_profit_enabled=true 时可使用 take_profit_1_pct / take_profit_2_pct 做分级止盈；ai_adjust_take_profit=false 时系统会覆盖为用户配置。
- 若 ranking_payload.decision_directive 非空，必须严格遵守其中的资金/时段/退出纪律（如总预算、单笔上限、禁止隔夜、智能退出要求）。
- 每个 selection 可给出 exit_conditions（智能退出规则数组）：支持 time_exit(exit_at)、no_overnight、underlying_price(突破/跌破失效价)、pnl_giveback(回吐止盈)、option_greek/option_greek_change(指标阈值，如 delta/iv 超阈值退出)；并可给 latest_exit（最晚退出时间）。无把握时可省略，系统会用默认止损止盈兜底。
- 下单方式由 execution_context.entry_order_type 决定，理由必须与该执行方式一致。
JSON 格式：
{
  "summary": "中文最终总结",
  "council_mode": "single_ai",
  "selections": [
    {
      "contract_symbol": "...",
      "symbol": "NVDA",
      "allocation_pct": 0.2,
      "stop_loss_pct": 25,
      "take_profit_pct": 30,
      "take_profit_1_pct": 20,
      "take_profit_2_pct": 35,
      "latest_exit": "收盘前",
      "allow_overnight": false,
      "exit_conditions": [
        {"type": "time_exit", "exit_at": "15:50"},
        {"type": "option_greek", "greek": "iv", "op": "above", "value": 0}
      ],
      "reason": "为什么最终入选，包含主要风险、止损触发、止盈区间、最晚退出时间"
    }
  ],
  "rejected": [
    {"contract_symbol": "...", "reason": "剔除理由"}
  ],
  "risk_notes": ["组合层面的风险提示"]
}
"""


LIVE_TOP_UP_PROMPT = """你是实盘期权交易小组主持人。第一次主持决策返回的 selections 少于配置的 Top N。
现在你必须只从 remaining_opportunities 里补足 missing_count 个额外单腿期权，使最终选择数量达到 target_count。
硬性要求：
- 只能返回 JSON，不要 markdown，不要解释 JSON 外的内容。
- additional_selections 只能引用 remaining_opportunities 里存在的 contract_symbol，不能重复 already_selected_contracts，不能编造候选外合约或价格。
- 必须返回 missing_count 个 additional_selections；如果剩余候选质量一般，也要按相对优先级补足，并在 reason 中明确风险和为什么仍排在补选名单中。
- reason 必须引用 remaining_opportunities 的字段来源，不得只写泛泛理由。
- allocation_pct 是总策略资金比例；如果 ai_adjust_allocation=false，系统会改为等权，但你仍需给出建议值。
- stop_loss_pct 必须在 1-95 之间。
JSON 格式：
{
  "summary": "中文补足说明",
  "additional_selections": [
    {
      "contract_symbol": "...",
      "symbol": "TSLA",
      "allocation_pct": 0.2,
      "stop_loss_pct": 25,
      "reason": "为什么补选入 Top N，包含主要风险、止损触发、止盈区间、最晚退出时间"
    }
  ],
  "rejected": [
    {"contract_symbol": "...", "reason": "仍未入选的理由"}
  ],
  "risk_notes": ["组合层面的新增风险提示"]
}
"""


STRATEGY_ADVISORS = [
    {
        "key": "attack",
        "name": "进攻型策略诸葛亮",
        "prompt": """你是进攻型实盘多腿期权策略顾问。你会收到 strategy_opportunities，每个机会是一组可执行期权结构。
你的目标是寻找最有结构性赔率、凸性或收租效率的策略，但只能从 strategy_opportunities 中选择 strategy_key，不能编造策略、合约或价格。
重点审查 strategy_candidate.score、direction、legs、net_debit/net_credit、max_loss、max_profit、breakevens、fit_notes、hard_flags 和市场上下文。理由必须引用字段来源。
只能返回 JSON，不要 markdown，不要解释 JSON 外内容。
JSON 格式：
{
  "advisor_key": "attack",
  "summary": "中文摘要",
  "ranked": [
    {
      "strategy_key": "...",
      "conviction_score": 0-100,
      "risk_reward_score": 0-100,
      "execution_score": 0-100,
      "setup_quality": "excellent|good|mixed|poor",
      "allocation_pct": 0.2,
      "reason": "进攻理由，必须引用字段来源",
      "invalid_if": "策略失效条件"
    }
  ],
  "rejected": [{"strategy_key": "...", "reason": "剔除原因"}],
  "risk_notes": ["组合层面风险"]
}""",
    },
    {
        "key": "risk",
        "name": "风控型策略诸葛亮",
        "prompt": """你是风控型实盘多腿期权策略顾问。你会收到 strategy_opportunities，每个机会是一组可执行期权结构。
你的目标是保护策略资金，优先审查定义风险、最大亏损、保证金/资本占用、腿级执行难度、备兑/领式是否有持股支撑风险；只能从 strategy_opportunities 中选择 strategy_key。
重点审查 strategy_candidate.max_loss、capital_required、legs、hard_flags、breakevens、net_debit/net_credit、fit_notes 和市场上下文。理由必须引用字段来源。
只能返回 JSON，不要 markdown，不要解释 JSON 外内容。
JSON 格式：
{
  "advisor_key": "risk",
  "summary": "中文摘要",
  "ranked": [
    {
      "strategy_key": "...",
      "conviction_score": 0-100,
      "risk_reward_score": 0-100,
      "execution_score": 0-100,
      "setup_quality": "excellent|good|mixed|poor",
      "allocation_pct": 0.2,
      "reason": "为什么风险可控或需要降仓，必须引用字段来源",
      "invalid_if": "策略失效条件"
    }
  ],
  "rejected": [{"strategy_key": "...", "reason": "剔除原因"}],
  "risk_notes": ["组合层面风险"]
}""",
    },
    {
        "key": "skeptic",
        "name": "反方策略诸葛亮",
        "prompt": """你是反方实盘多腿期权策略顾问。你会收到 strategy_opportunities，每个机会是一组可执行期权结构。
你的任务是找结构陷阱：卖方尾部风险、腿级滑点、价差过窄但风险不划算、备兑/领式被错误理解、跨式/宽跨 IV 过贵、铁鹰/蝶式区间假设不成立。只能从 strategy_opportunities 中选择 strategy_key。
反对意见必须引用 strategy_candidate 或 market_context 字段；若必须交易，给出最不坏结构。
只能返回 JSON，不要 markdown，不要解释 JSON 外内容。
JSON 格式：
{
  "advisor_key": "skeptic",
  "summary": "中文摘要",
  "ranked": [
    {
      "strategy_key": "...",
      "conviction_score": 0-100,
      "risk_reward_score": 0-100,
      "execution_score": 0-100,
      "setup_quality": "excellent|good|mixed|poor",
      "allocation_pct": 0.2,
      "reason": "若必须交易，为什么这是最不坏结构，必须引用字段来源",
      "invalid_if": "必须放弃该结构的条件"
    }
  ],
  "rejected": [{"strategy_key": "...", "reason": "剔除原因"}],
  "risk_notes": ["主要反对意见"]
}""",
    },
]


STRATEGY_MODERATOR_PROMPT = """你是实盘多腿期权策略小组主持人。你会收到 strategy_opportunities，以及三个独立 DeepSeek 策略顾问会话的 JSON 报告。
请模拟三位顾问讨论后的最终决策，只能从 strategy_opportunities 里选择 top_n 个最值得进入实盘工作流的策略结构，并决定 allocation_pct。
硬性要求：
- 只能返回 JSON，不要 markdown，不要解释 JSON 外的内容。
- strategy_selections 只能引用 strategy_opportunities 里存在的 strategy_key，不能编造候选外策略、合约或价格。
- 每个 selection.reason 必须引用至少 2 个字段来源，例如 strategy_candidate.score、max_loss、capital_required、legs、breakevens、fit_notes、hard_flags、market_context。
- selections 必须尽量返回 min(top_n, strategy_opportunities数量) 个候选；如果结构质量一般，也要在 reason 中写清风险。
- **分散化硬要求**：每个标的(symbol)最多入选 max_per_symbol 个策略结构；如果某个标的已有入选，优先从其他标的中选择最佳候选，避免组合集中于单一标的。系统会在入选后再次强制过滤。
- allocation_pct 是总策略资金比例，所有入选之和不得超过 1；如果 ai_adjust_allocation=false，系统会改为等权，但你仍需给出完整 Top N 决策和理由。
- 输出必须体现三个顾问的分歧、共识、剔除理由和最终风险控制。
JSON 格式：
{
  "summary": "中文最终总结",
  "council_mode": "strategy_three_advisors",
  "discussion": {
    "attack": "进攻型观点摘要",
    "risk": "风控型观点摘要",
    "skeptic": "反方观点摘要",
    "agreement": "共识",
    "disagreement": "主要分歧"
  },
  "strategy_selections": [
    {
      "strategy_key": "...",
      "symbol": "NVDA",
      "allocation_pct": 0.2,
      "reason": "为什么最终入选，包含最大亏损、失效条件、止盈止损计划应关注的字段"
    }
  ],
  "rejected": [
    {"strategy_key": "...", "reason": "剔除理由"}
  ],
  "risk_notes": ["组合层面的风险提示"]
}
"""


STRATEGY_SINGLE_DECISION_PROMPT = """你是实盘多腿期权策略决策员。你会收到 strategy_opportunities，每个机会是一组可执行期权结构。
请只从 strategy_opportunities 里选择 top_n 个最值得进入实盘工作流的策略结构，并决定 allocation_pct。
硬性要求：
- 只能返回 JSON，不要 markdown，不要解释 JSON 外的内容。
- strategy_selections 只能引用 strategy_opportunities 里存在的 strategy_key，不能编造候选外策略、合约或价格。
- 每个 selection.reason 必须引用至少 2 个字段来源，例如 strategy_candidate.score、max_loss、capital_required、legs、breakevens、fit_notes、hard_flags、market_context。
- selections 必须尽量返回 min(top_n, strategy_opportunities数量) 个候选；如果结构质量一般，也要在 reason 中写清风险。
- allocation_pct 是总策略资金比例，所有入选之和不得超过 1。
JSON 格式：
{
  "summary": "中文最终总结",
  "council_mode": "strategy_single_ai",
  "strategy_selections": [
    {
      "strategy_key": "...",
      "symbol": "NVDA",
      "allocation_pct": 0.2,
      "reason": "为什么最终入选，包含最大亏损、失效条件、止盈止损计划应关注的字段"
    }
  ],
  "rejected": [
    {"strategy_key": "...", "reason": "剔除理由"}
  ],
  "risk_notes": ["组合层面的风险提示"]
}
"""


STRATEGY_RISK_PLANNER_PROMPT = """你是实盘期权策略结构的风控计划师。你会收到三顾问已入选的策略结构。系统可能处于仅人工复核模式，也可能处于自动多腿执行模式；无论哪种模式，都必须按你的计划做组合PnL追踪和退出。
你只能为 payload.strategy_positions 中存在的 tracking_id 制定风控，不能编造策略、合约或行情。
每个 plan.reason 必须引用对应 position 的 max_loss、capital_required、profit_basis、fit_notes 或 hard_flags 等字段来源。

输出必须是 JSON，不要 markdown，不要解释 JSON 外内容。

约束：
- stop_loss_pnl 必须是负数，表示组合PnL低于该值时触发止损提醒。
- take_profit_1_pnl 必须是正数；只有 tiered_take_profit_enabled=true 时才需要 take_profit_2_pnl，且 take_profit_2_pnl >= take_profit_1_pnl。
- default_take_profit_pct 是系统默认组合止盈百分比；tiered_take_profit_enabled=false 时只做一次性止盈，可只设置 take_profit_pct 或 take_profit_1_pnl。
- tiered_take_profit_enabled=true 时才使用分级 TP1/TP2；可返回 take_profit_1_pct / take_profit_2_pct，系统会用真实成交价回写实际 PnL 阈值。
- ai_adjust_take_profit=false 时，系统会保留用户配置的止盈百分比，不采用你返回的止盈目标。
- 不要给出超过结构理论最大盈利太多的止盈；定义风险结构的止损不要超过最大亏损。
- latest_exit 用简短中文或 ET 时间描述。
- invalidation 写清楚正股、波动率、时间衰减或结构失效条件。
- exit_conditions 是可选结构化智能退出条件；当前系统可执行：
  - {"type":"time_exit","exit_at":"2026-05-11T15:45:00-04:00","reason":"..."}
  - {"type":"no_overnight","time_et":"15:50","reason":"..."}
  - {"type":"underlying_price","operator":"<=","price":500,"reason":"..."}
  - {"type":"pnl_giveback","min_profit_pnl":120,"giveback_pct":35,"reason":"..."}
  - {"type":"option_greek","field":"delta","operator":"<=","value":0.25,"reason":"delta 衰减，方向暴露不足"}
  - {"type":"option_greek_change","field":"theta","operator":"<=","change_pct":-40,"reason":"theta 恶化过快"}
- reason 说明为什么这样设置，必须可交易、可复盘。

JSON 格式：
{
  "summary": "中文总评",
  "plans": [
    {
      "tracking_id": "strategy-1",
      "stop_loss_pnl": -80,
      "take_profit_pct": 30,
      "take_profit_1_pct": 20,
      "take_profit_2_pct": 35,
      "take_profit_1_pnl": 120,
      "take_profit_2_pnl": 220,
      "latest_exit": "到期日前一交易日 15:30 ET 前",
      "invalidation": "正股跌破关键位或组合价格跌破止损",
      "allow_overnight": false,
      "exit_conditions": [
        {"type": "no_overnight", "time_et": "15:50", "reason": "不隔夜"},
        {"type": "underlying_price", "operator": "<=", "price": 500, "reason": "跌破关键支撑"}
      ],
      "confidence": 0.72,
      "reason": "中文理由"
    }
  ],
  "risk_notes": ["组合层面风险提示"]
}
"""


def start_trading_run(owner_id: str, config: dict[str, Any] | None = None, trigger_source: str = "manual") -> dict[str, Any]:
    active_config = normalize_trading_config(config or get_trading_config(owner_id))
    active_config["trigger_source"] = trigger_source
    entry_blockers = trading_run_entry_blockers(active_config, trigger_source=trigger_source)
    if entry_blockers:
        raise TradingRunBlockedError("; ".join(entry_blockers))
    readiness = validate_trading_readiness(owner_id, active_config, require_ai=False)
    if not readiness.get("ok"):
        raise TradingRunBlockedError("; ".join(readiness.get("issues") or ["trading readiness check failed"]))
    run = create_trading_run(owner_id, active_config)
    _executor.submit(execute_trading_run, run["id"], owner_id, active_config)
    return run


def create_auto_trade_run_and_execute(owner_id: str, config: dict[str, Any], trigger_source: str = "auto") -> dict[str, Any]:
    """Create and SYNCHRONOUSLY run one auto-trade cycle's trading run.

    Used by the auto-trade loop, which needs the run outcome to record the
    cycle. Live runs (real broker) still pass the full readiness gate; dry-run
    runs (no broker) skip the broker-requiring readiness because they finish in
    analysis-only mode and never touch a broker. Execution still goes through
    the hardened ``execute_trading_run`` (lock, journal, decision gate, etc.).
    """
    active_config = normalize_trading_config(config)
    active_config["trigger_source"] = trigger_source
    dry_run = bool(active_config.get("dry_run")) and not active_config.get("live_enabled")
    if not dry_run:
        readiness = validate_trading_readiness(owner_id, active_config, require_ai=False)
        if not readiness.get("ok"):
            raise TradingRunBlockedError("; ".join(readiness.get("issues") or ["trading readiness check failed"]))
    run = create_trading_run(owner_id, active_config)
    execute_trading_run(run["id"], owner_id, active_config)
    return run


def trading_run_entry_blockers(config: dict[str, Any], *, trigger_source: str = "manual") -> list[str]:
    normalized = normalize_trading_config(config)
    source = str(trigger_source or normalized.get("trigger_source") or "manual")
    is_slot_run = bool(str(normalized.get("schedule_slot_id") or "").strip()) or source.startswith("scheduler:")
    if is_slot_run:
        return []
    if not normalized.get("single_instance_enabled", True):
        return ["单实例创建开关已关闭；请开启单实例，或等待多时段时段触发。"]
    return []


def start_scheduled_trading_slot(
    owner_id: str,
    config: dict[str, Any],
    *,
    trade_date_et: str,
    profile_id: str,
    slot: dict[str, Any],
) -> dict[str, Any] | None:
    session = get_or_create_schedule_session(owner_id, trade_date_et, profile_id, config)
    replay_after_minutes = int(config.get("schedule_replay_after_minutes") or 30)
    recovered = recover_stale_schedule_slots(
        owner_id,
        trade_date_et,
        profile_id,
        stale_after_minutes=replay_after_minutes,
    )
    if recovered:
        session = get_or_create_schedule_session(owner_id, trade_date_et, profile_id, config)
    slot_id = str(slot.get("slot_id") or "").strip()
    if not slot_id:
        return None
    scheduled_time_et = str(slot.get("time_et") or config.get("run_time_et") or "10:30")
    gate_result = evaluate_schedule_slot_gate(config, slot, session)
    if gate_result.get("skip"):
        skip_schedule_slot(
            owner_id,
            trade_date_et,
            profile_id,
            slot_id,
            scheduled_time_et,
            session_id=str(session.get("session_id") or ""),
            action=str(slot.get("action") or ""),
            gate_profile=str(slot.get("gate_profile") or ""),
            gate_result=gate_result,
            reason=str(gate_result.get("reason") or "slot skipped"),
        )
        return None
    if not gate_result.get("allowed"):
        skip_schedule_slot(
            owner_id,
            trade_date_et,
            profile_id,
            slot_id,
            scheduled_time_et,
            session_id=str(session.get("session_id") or ""),
            action=str(slot.get("action") or ""),
            gate_profile=str(slot.get("gate_profile") or ""),
            gate_result=gate_result,
            reason="; ".join(gate_result.get("blockers") or ["slot gate blocked"]),
        )
        return None
    if not claim_schedule_slot(
        owner_id,
        trade_date_et,
        profile_id,
        slot_id,
        scheduled_time_et,
        session_id=str(session.get("session_id") or ""),
        action=str(slot.get("action") or ""),
        gate_profile=str(slot.get("gate_profile") or ""),
        allocated_capital=float(gate_result.get("allocated_capital") or 0),
        gate_result=gate_result,
    ):
        return None
    if _schedule_slot_is_exit_only(slot):
        try:
            result = execute_scheduled_exit_slot(owner_id, config, trade_date_et=trade_date_et, profile_id=profile_id, slot=slot)
            status = "failed" if result.get("status") in {"failed", "partial_failed"} else "fired"
            mark_schedule_slot_fired(
                owner_id,
                trade_date_et,
                profile_id,
                slot_id,
                run_id=str(result.get("run_id") or ""),
                status=status,
                error=result.get("error"),
            )
            return result
        except Exception as exc:  # noqa: BLE001
            mark_schedule_slot_fired(owner_id, trade_date_et, profile_id, slot_id, status="failed", error=str(exc))
            raise
    slot_config = _schedule_slot_config(owner_id, config, profile_id, trade_date_et, slot)
    slot_config["schedule_session_id"] = str(session.get("session_id") or "")
    try:
        run = start_trading_run(owner_id, slot_config, trigger_source=f"scheduler:{profile_id}:{slot_id}")
        mark_schedule_slot_fired(owner_id, trade_date_et, profile_id, slot_id, run_id=str(run.get("id") or ""), status="fired")
        return run
    except Exception as exc:  # noqa: BLE001
        mark_schedule_slot_fired(owner_id, trade_date_et, profile_id, slot_id, status="failed", error=str(exc))
        raise


def get_schedule_slots(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    normalized = normalize_trading_config(config or {})
    slots = list(normalized.get("schedule_slots") or [])
    if normalized.get("multi_instance_enabled") and not slots:
        slots = list(DEFAULT_SCHEDULE_SLOTS)
    return [dict(slot) for slot in slots if isinstance(slot, dict)]


def evaluate_schedule_slot_gate(config: dict[str, Any], slot: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    slot_action = str(slot.get("action") or "open_or_adjust")
    allow_new_positions = bool(slot.get("allow_new_positions", True))
    force_no_overnight = bool(slot.get("force_no_overnight", False))
    slot_gate_profile = str(slot.get("gate_profile") or "default")
    strategy_modes = normalize_strategy_modes(slot.get("strategy_modes") or config.get("strategy_modes") or ["single_leg"])
    current_config_hash = schedule_config_hash(config)
    session_config_hash = str(session.get("config_hash") or "").strip()
    total_capital = max(float(config.get("total_capital") or 0), 0.0)
    requested_capital = round(total_capital * float(slot.get("capital_pct") or 0), 2)
    remaining_capital = float(session.get("remaining_capital") or total_capital)
    if session_config_hash and session_config_hash != current_config_hash:
        blockers.append("session config changed since session creation")
    is_exit_only = _schedule_slot_is_exit_only(slot)
    if is_exit_only:
        requested_capital = 0.0
    if requested_capital <= 0 and not is_exit_only:
        blockers.append("slot capital allocation is zero")
    elif requested_capital > remaining_capital + 0.01:
        blockers.append(f"slot capital {requested_capital:.2f} exceeds remaining capital {remaining_capital:.2f}")
    if is_exit_only:
        if not config.get("live_enabled"):
            blockers.append("live trading is disabled")
        return {
            "allowed": not blockers,
            "skip": False,
            "reason": "; ".join(blockers) if blockers else "slot configured as exit-only risk pass",
            "reasons": ["slot configured as exit-only risk pass"],
            "warnings": [],
            "blockers": blockers,
            "allocated_capital": 0.0,
            "slot_action": slot_action,
            "gate_profile": slot_gate_profile,
            "strategy_modes": strategy_modes,
        }
    template = _schedule_slot_gate_template(slot_gate_profile, strategy_modes, slot, config)
    blockers.extend(template["blockers"])
    reasons.extend(template["reasons"])
    warnings.extend(template["warnings"])
    if not strategy_modes:
        blockers.append("slot has no strategy modes")
    if not blockers:
        reasons.append(f"slot `{slot.get('label') or slot.get('slot_id')}` gate passed")
    else:
        warnings.append("slot gate blockers present")
    return {
        "allowed": not blockers,
        "skip": False,
        "reason": "; ".join(reasons or blockers),
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "blockers": list(dict.fromkeys(blockers)),
        "allocated_capital": requested_capital if not blockers else 0.0,
        "slot_action": slot_action,
        "gate_profile": slot_gate_profile,
        "strategy_modes": strategy_modes,
    }


def _schedule_slot_is_exit_only(slot: dict[str, Any]) -> bool:
    slot_action = str(slot.get("action") or "open_or_adjust")
    allow_new_positions = bool(slot.get("allow_new_positions", True))
    return slot_action in {"reduce_or_exit", "risk_review"} or not allow_new_positions


def execute_scheduled_exit_slot(
    owner_id: str,
    config: dict[str, Any],
    *,
    trade_date_et: str,
    profile_id: str,
    slot: dict[str, Any],
) -> dict[str, Any]:
    account_ref = account_ref_for_config(config, owner_id=owner_id)
    slot_action = str(slot.get("action") or "")
    force_no_overnight = bool(slot.get("force_no_overnight", False))
    candidates = _scheduled_exit_candidate_runs(owner_id)
    flattened: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for run in candidates:
        run_id = str(run.get("id") or "")
        state = str(((run.get("trade_instance") or {}).get("lifecycle_state") or "")).strip()
        if run.get("status") in {"queued", "running"}:
            skipped.append({"run_id": run_id, "reason": f"run status is {run.get('status')}"})
            continue
        if state in {"closed", "reviewed"}:
            skipped.append({"run_id": run_id, "reason": f"lifecycle is {state}"})
            continue
        if slot_action == "risk_review" and not force_no_overnight:
            skipped.append({"run_id": run_id, "reason": "risk review slot does not force flatten"})
            continue
        try:
            result = flatten_trade_instance(run_id, owner_id, account_ref)
            flattened.append(result)
        except Exception as exc:  # noqa: BLE001
            failed.append({"run_id": run_id, "error": str(exc)})
    submitted_count = sum(int(item.get("submitted_count") or 0) + int(item.get("strategy_submitted_count") or 0) for item in flattened)
    failed_count = len(failed) + sum(int(item.get("failed_count") or 0) + int(item.get("strategy_failed_count") or 0) for item in flattened)
    return {
        "id": f"scheduled-exit:{profile_id}:{slot.get('slot_id')}",
        "run_id": f"scheduled-exit:{profile_id}:{slot.get('slot_id')}",
        "owner_id": owner_id,
        "trade_date_et": trade_date_et,
        "profile_id": profile_id,
        "slot_id": slot.get("slot_id"),
        "slot_action": slot_action,
        "status": "partial_failed" if failed_count else "succeeded",
        "flattened_count": len(flattened),
        "submitted_count": submitted_count,
        "failed_count": failed_count,
        "skipped_count": len(skipped),
        "flattened": flattened,
        "failed": failed,
        "skipped": skipped,
        "error": "; ".join(item.get("error") or "" for item in failed)[:500] or None,
    }


def _scheduled_exit_candidate_runs(owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for run in list_trading_runs(owner_id, limit=limit, summary=False):
        if run.get("status") not in {"queued", "running", "succeeded"}:
            continue
        orders = run.get("orders") or []
        instance = run.get("trade_instance") or {}
        protection = instance.get("protection_status") if isinstance(instance.get("protection_status"), dict) else {}
        state = str(instance.get("lifecycle_state") or "")
        has_open_risk = bool(
            orders
            and state not in {"closed", "reviewed"}
            and (
                protection.get("state") not in {None, "", "not_started", "completed", "closed"}
                or any(_order_has_flattenable_quantity(order) for order in orders if isinstance(order, dict))
            )
        )
        if has_open_risk:
            candidates.append(run)
    return candidates


def _order_has_flattenable_quantity(order: dict[str, Any]) -> bool:
    filled = int(_coerce_float(order.get("entry_filled_quantity"), 0))
    closed = (
        max(
            int(_coerce_float(order.get("software_take_profit_closed_quantity"), 0)),
            int(_coerce_float(order.get("software_take_profit_submitted_quantity"), 0)),
        )
        + max(
            int(_coerce_float(order.get("software_stop_closed_quantity"), 0)),
            int(_coerce_float(order.get("software_stop_submitted_quantity"), 0)),
        )
        + max(
            int(_coerce_float(order.get("single_leg_smart_exit_closed_quantity"), 0)),
            int(_coerce_float(order.get("single_leg_smart_exit_submitted_quantity"), 0)),
        )
        + max(
            int(_coerce_float(order.get("instance_flatten_closed_quantity"), 0)),
            int(_coerce_float(order.get("instance_flatten_submitted_quantity"), 0)),
        )
    )
    if filled > closed:
        return True
    for leg in order.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        leg_filled = int(_coerce_float(leg.get("filled_quantity") or leg.get("quantity"), 0))
        leg_exiting = int(_coerce_float(leg.get("strategy_exit_quantity") or leg.get("strategy_exit_filled_quantity"), 0))
        if leg_filled > leg_exiting:
            return True
    return False


def _schedule_slot_gate_template(slot_gate_profile: str, strategy_modes: list[str], slot: dict[str, Any], config: dict[str, Any]) -> dict[str, list[str]]:
    blockers: list[str] = []
    reasons: list[str] = []
    warnings: list[str] = []
    mode_set = set(strategy_modes)
    if "single_leg" in mode_set:
        reasons.append("单腿模板偏方向性和触发交易。")
        if slot_gate_profile == "strict_momentum":
            reasons.append("严格动量模板已匹配。")
        max_debit = _slot_gate_number(slot, config, "max_debit")
        expected_debit = _slot_gate_number(slot, config, "expected_debit", "net_debit")
        if max_debit is not None and expected_debit is not None and expected_debit > max_debit:
            blockers.append(f"single_leg debit {expected_debit:.2f} exceeds max debit {max_debit:.2f}")
        return {"blockers": blockers, "reasons": reasons, "warnings": warnings}
    if "calendar" in mode_set:
        reasons.append("calendar 模板偏期限结构和波动率回归。")
        if slot_gate_profile == "structure_specific":
            warnings.append("calendar 依赖期限结构输入字段。")
        max_debit = _slot_gate_number(slot, config, "calendar_max_debit", "max_debit")
        expected_debit = _slot_gate_number(slot, config, "calendar_expected_debit", "expected_debit", "net_debit")
        if expected_debit is not None and expected_debit <= 0:
            blockers.append("calendar requires positive net debit")
        if max_debit is not None and expected_debit is not None and expected_debit > max_debit:
            blockers.append(f"calendar debit {expected_debit:.2f} exceeds max debit {max_debit:.2f}")
        term_structure_ok = _slot_gate_bool(slot, config, "calendar_term_structure_ok", "term_structure_ok")
        if term_structure_ok is False:
            blockers.append("calendar term structure gate failed")
    if "iron_condor" in mode_set:
        reasons.append("iron_condor 模板偏宽度与净信用控制。")
        min_credit = _slot_gate_number(slot, config, "iron_condor_min_credit", "min_credit")
        expected_credit = _slot_gate_number(slot, config, "iron_condor_expected_credit", "expected_credit", "net_credit")
        width = _slot_gate_number(slot, config, "iron_condor_width", "spread_width", "width")
        max_width = _slot_gate_number(slot, config, "iron_condor_max_width", "max_width")
        min_credit_to_width = _slot_gate_number(slot, config, "iron_condor_min_credit_to_width", "min_credit_to_width")
        if expected_credit is not None and expected_credit <= 0:
            blockers.append("iron_condor requires net credit")
        if min_credit is not None and expected_credit is not None and expected_credit < min_credit:
            blockers.append(f"iron_condor credit {expected_credit:.2f} below minimum {min_credit:.2f}")
        if max_width is not None and width is not None and width > max_width:
            blockers.append(f"iron_condor width {width:.2f} exceeds max width {max_width:.2f}")
        if min_credit_to_width is not None and expected_credit is not None and width and expected_credit / width < min_credit_to_width:
            blockers.append("iron_condor credit/width ratio below minimum")
    if "straddle" in mode_set:
        reasons.append("straddle 模板偏事件波动和净借方控制。")
        max_debit = _slot_gate_number(slot, config, "straddle_max_debit", "max_debit")
        expected_debit = _slot_gate_number(slot, config, "straddle_expected_debit", "expected_debit", "net_debit")
        if expected_debit is not None and expected_debit <= 0:
            blockers.append("straddle requires net debit")
        if max_debit is not None and expected_debit is not None and expected_debit > max_debit:
            blockers.append(f"straddle debit {expected_debit:.2f} exceeds max debit {max_debit:.2f}")
    if "strangle" in mode_set:
        reasons.append("strangle 模板偏 IV 与方向失真控制。")
        max_debit = _slot_gate_number(slot, config, "strangle_max_debit", "max_debit")
        expected_debit = _slot_gate_number(slot, config, "strangle_expected_debit", "expected_debit", "net_debit")
        width = _slot_gate_number(slot, config, "strangle_width", "strike_width", "width")
        min_width = _slot_gate_number(slot, config, "strangle_min_width", "min_width")
        if expected_debit is not None and expected_debit <= 0:
            blockers.append("strangle requires net debit")
        if max_debit is not None and expected_debit is not None and expected_debit > max_debit:
            blockers.append(f"strangle debit {expected_debit:.2f} exceeds max debit {max_debit:.2f}")
        if min_width is not None and width is not None and width < min_width:
            blockers.append(f"strangle width {width:.2f} below min width {min_width:.2f}")
    if "butterfly" in mode_set:
        reasons.append("butterfly 模板偏最大收益与翼宽约束。")
    if "spread" in mode_set:
        reasons.append("spread 模板偏净价与腿间价差检查。")
    if not mode_set:
        blockers.append("slot has no strategy modes")
    if slot_gate_profile == "structure_specific" and not (mode_set & {"calendar", "iron_condor", "straddle", "strangle", "butterfly", "credit_spread", "debit_spread"}):
        blockers.append("structure template requires multi-leg structure modes")
    if slot_gate_profile == "strict_momentum" and not (mode_set & {"single_leg", "spread"}):
        blockers.append("strict momentum template requires directional modes")
    if slot_gate_profile == "no_overnight":
        reasons.append("no_overnight 模板要求尾盘仓位清理。")
    iv_rank = _slot_gate_number(slot, config, "iv_rank", "iv_percentile")
    min_iv_rank = _slot_gate_number(slot, config, "min_iv_rank", "iv_rank_min")
    max_iv_rank = _slot_gate_number(slot, config, "max_iv_rank", "iv_rank_max")
    if min_iv_rank is not None and iv_rank is not None and iv_rank < min_iv_rank:
        blockers.append(f"iv rank {iv_rank:.2f} below minimum {min_iv_rank:.2f}")
    if max_iv_rank is not None and iv_rank is not None and iv_rank > max_iv_rank:
        blockers.append(f"iv rank {iv_rank:.2f} above maximum {max_iv_rank:.2f}")
    return {"blockers": blockers, "reasons": reasons, "warnings": warnings}


def _slot_gate_number(slot: dict[str, Any], config: dict[str, Any], *keys: str) -> float | None:
    for source in (slot, config):
        for key in keys:
            if key in source and source.get(key) not in (None, ""):
                try:
                    return float(source.get(key))
                except (TypeError, ValueError):
                    return None
    return None


def _slot_gate_bool(slot: dict[str, Any], config: dict[str, Any], *keys: str) -> bool | None:
    for source in (slot, config):
        for key in keys:
            if key in source:
                value = source.get(key)
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "ok", "pass", "passed"}
                return bool(value)
    return None


def _schedule_slot_config(
    owner_id: str,
    config: dict[str, Any],
    profile_id: str,
    trade_date_et: str,
    slot: dict[str, Any],
) -> dict[str, Any]:
    slot_config = dict(config)
    slot_config["owner_id"] = owner_id
    slot_config["trigger_source"] = f"scheduler:{profile_id}:{slot.get('slot_id')}"
    slot_config["schedule_profile"] = profile_id
    slot_config["schedule_slot_id"] = slot.get("slot_id")
    slot_config["schedule_slot_label"] = slot.get("label")
    slot_config["schedule_slot_time_et"] = slot.get("time_et")
    slot_config["schedule_slot_action"] = slot.get("action")
    slot_config["schedule_slot_gate_profile"] = slot.get("gate_profile")
    slot_config["schedule_slot_allow_new_positions"] = bool(slot.get("allow_new_positions", True))
    slot_config["schedule_slot_force_no_overnight"] = bool(slot.get("force_no_overnight", False))
    slot_config["trade_date_et"] = trade_date_et
    slot_config["strategy_modes"] = slot.get("strategy_modes") or config.get("strategy_modes") or ["single_leg"]
    if slot.get("capital_pct"):
        slot_config["total_capital"] = max(1.0, float(config.get("total_capital") or 0) * float(slot.get("capital_pct") or 0))
    if slot.get("force_no_overnight"):
        slot_config["software_take_profit_enabled"] = bool(slot_config.get("software_take_profit_enabled", True))
    slot_config["risk_max_daily_runs"] = max(
        int(slot_config.get("risk_max_daily_runs") or 1),
        len([item for item in get_schedule_slots(config) if item.get("enabled", True)]),
    )
    return normalize_trading_config(slot_config)


def _finish_trading_run(run_id: str, owner_id: str, config: dict[str, Any], **fields: Any) -> None:
    mark_trading_run(run_id, **fields)
    status = str(fields.get("status") or "")
    if status not in {"succeeded", "failed"}:
        return
    slot_id = str(config.get("schedule_slot_id") or "").strip()
    profile_id = str(config.get("schedule_profile") or "").strip()
    trade_date_et = str(config.get("trade_date_et") or "").strip()
    if not slot_id or not profile_id or not trade_date_et:
        return
    schedule_status = "fired" if status == "succeeded" else "failed"
    mark_schedule_slot_fired(
        owner_id,
        trade_date_et,
        profile_id,
        slot_id,
        run_id=run_id,
        status=schedule_status,
        error=fields.get("error"),
    )


def _trading_run_lock_key(run_id: str) -> str:
    return f"ai-option:trading-run:{run_id}"


def _require_distributed_lock() -> bool:
    return (os.getenv("AI_OPTION_TRADING_REQUIRE_LOCK", "true") or "").strip().lower() in {"1", "true", "yes", "on"}


def _claim_trading_run_lock(run_id: str, owner_id: str) -> tuple[str | None, bool]:
    """Attempt to claim a redis-backed lock for this run.

    Returns (lock_key, acquired). When redis is unavailable, returns (None, False)
    and the caller should proceed without distributed locking. When redis is
    available but the lock is already held, returns (key, False) so the caller
    can abort to avoid concurrent execution of the same run.
    """
    if not redis_available():
        return None, False
    key = _trading_run_lock_key(run_id)
    acquired = redis_setnx(key, owner_id or "1", 600)
    return key, acquired


def _force_finish_stuck_running(run_id: str, owner_id: str, config: dict[str, Any]) -> None:
    try:
        current = get_trading_run(run_id, owner_id)
    except Exception:
        return
    if not current or str(current.get("status") or "").lower() != "running":
        return
    try:
        tail_instance = dict(current.get("trade_instance") or {})
        append_instance_event(
            tail_instance,
            "execute_run_aborted",
            "execute_trading_run 未正常结束，已强制收尾。",
            lifecycle_state="blocked",
            status="error",
        )
        _finish_trading_run(
            run_id,
            owner_id,
            config,
            status="failed",
            stage="internal_error",
            finished_at=utc_now(),
            error="execute_trading_run terminated without finishing",
            instance_json=tail_instance,
        )
    except Exception:
        # Last-resort safety net: if even force-finishing a stuck run fails, the run
        # can be stranded in "running". Log loudly so it is visible for manual repair.
        _LOG.exception("force-finish of stuck trading run %s failed", run_id)


def execute_trading_run(run_id: str, owner_id: str, config: dict[str, Any]) -> None:
    config = normalize_trading_config(config)
    # Fail-closed: without a distributed lock we cannot guarantee that two
    # workers/nodes won't execute the same run and double-submit real orders.
    # Refuse rather than proceed unlocked (override with
    # AI_OPTION_TRADING_REQUIRE_LOCK=false only for single-process/dev use).
    if not redis_available() and _require_distributed_lock():
        try:
            current = get_trading_run(run_id, owner_id)
            instance = dict((current or {}).get("trade_instance") or {})
            append_instance_event(
                instance, "execute_run_blocked",
                "Redis 分布式锁不可用，已拒绝执行以避免重复下单。",
                lifecycle_state="blocked", status="error",
            )
            _finish_trading_run(
                run_id, owner_id, config, status="failed", stage="lock_unavailable",
                finished_at=utc_now(),
                error="distributed lock unavailable (redis down); refusing to execute to avoid duplicate broker orders",
                instance_json=instance,
            )
        except Exception:
            _LOG.exception("failed to record lock-unavailable abort for trading run %s", run_id)
        return
    lock_key, lock_acquired = _claim_trading_run_lock(run_id, owner_id)
    if lock_key is not None and not lock_acquired:
        return
    try:
        _execute_trading_run_body(run_id, owner_id, config)
    finally:
        _force_finish_stuck_running(run_id, owner_id, config)
        if lock_acquired and lock_key is not None:
            try:
                redis_del(lock_key)
            except Exception:
                # A leaked lock blocks every future run of this run_id; surface it.
                _LOG.exception("failed to release trading run lock %s", lock_key)


def _execute_trading_run_body(run_id: str, owner_id: str, config: dict[str, Any]) -> None:
    instance = _load_trade_instance(run_id, owner_id)
    try:
        # Dry-run (auto-trade with no broker): run the full scan -> AI -> gate
        # pipeline but finish in analysis-only mode without submitting real
        # orders. Real submission still requires live_enabled.
        dry_run = bool(config.get("dry_run")) and not config.get("live_enabled")
        try:
            account_ref = account_ref_for_config(config, owner_id=owner_id)
        except ValueError:
            if not dry_run:
                raise
            account_ref = ""
        account_name = display_account_name(account_ref)
        if not config.get("live_enabled") and not dry_run:
            raise ValueError("live trading is disabled; enable live_enabled before submitting real orders")
        instance.setdefault("basic_info", {})["broker"] = normalize_broker(config.get("broker"))
        instance.setdefault("basic_info", {})["account_name"] = account_name
        if dry_run:
            instance.setdefault("basic_info", {})["dry_run"] = True
        append_instance_event(instance, "scanning_started", "开始扫描股票池。", lifecycle_state="scanning")
        mark_trading_run(run_id, status="running", stage="scan_universe", progress=5, started_at=utc_now(), instance_json=instance)
        scan_results = _scan_universe(config, _market_data_account_name(config), owner_id)
        opportunities = _contract_opportunities(scan_results, config)
        attach_candidate_snapshot(instance, scan_results, opportunities)
        mark_trading_run(run_id, scan_results_json=scan_results, stage="council_ranking", progress=55, instance_json=instance)

        if _strategy_has_multi_leg_modes(config):
            strategy_opportunities = _strategy_opportunities(scan_results, config, account_ref)
            strategy_auto_execute = _strategy_auto_execute_enabled(config)
            strategy_council = {
                "summary": (
                    "当前实盘配置选择了非单腿策略模式。"
                    if not strategy_auto_execute
                    else "当前实盘配置已开启策略自动执行，系统将按腿级顺序提交多腿策略订单。"
                ),
                "council_mode": "strategy_auto_execute" if strategy_auto_execute else "strategy_analysis_only",
                "strategy_modes": list(config.get("strategy_modes") or []),
                "strategy_candidates": strategy_opportunities,
                "selection_count": 0,
                "selected_contracts": [],
                "rejected_count": 0,
            }
            strategy_council = _rank_strategy_opportunities(strategy_opportunities, config, owner_id)
            strategy_selections = _unique_valid_strategy_selections(
                strategy_council.get("strategy_selections") or [],
                strategy_opportunities,
                max_per_symbol=int(config.get("max_per_symbol") or 1),
            )
            strategy_council["strategy_selections"] = strategy_selections
            strategy_council["selection_count"] = len(strategy_selections)
            strategy_council["selected_contracts"] = [item.get("strategy_key") for item in strategy_selections if item.get("strategy_key")]
            if not strategy_selections:
                strategy_mode_label = "三诸葛亮" if config.get("council", True) else "单 AI" if config.get("use_ai", True) else "本地评分"
                strategy_council["post_validation"] = {
                    "status": "blocked",
                    "strategy_candidate_count": len(strategy_opportunities),
                    "strategy_selection_count": 0,
                    "reason": f"{strategy_mode_label} strategy ranking produced no valid selections",
                }
                strategy_council["strategy_selections"] = []
                strategy_council["selection_count"] = 0
                strategy_council["selected_contracts"] = []
                strategy_positions = []
                strategy_risk_plan = {"source": "none", "summary": "没有策略结构候选。", "plans": [], "risk_notes": []}
            else:
                strategy_positions = _build_strategy_risk_positions(strategy_opportunities, config, strategy_selections)
                strategy_positions, strategy_risk_plan = _apply_ai_strategy_risk_plan(strategy_positions, scan_results, config, owner_id)
            if not strategy_positions:
                strategy_council["summary"] = strategy_council.get("summary") or "未生成任何可用策略结构候选；请放宽股票池、DTE、价格或策略模式后重试。"
                strategy_council.setdefault("post_validation", {
                    "status": "blocked",
                    "strategy_candidate_count": len(strategy_opportunities),
                    "strategy_position_count": 0,
                    "reason": "no strategy positions were generated from strategy selections",
                })
                if not strategy_opportunities:
                    blocked_stage = "strategy_no_candidates"
                    blocked_event = "strategy_no_candidates"
                    blocked_message = "未生成任何可用策略结构候选，实例已阻断。"
                elif not strategy_selections:
                    blocked_stage = "strategy_selection_blocked"
                    blocked_event = "strategy_selection_blocked"
                    blocked_message = "策略候选已生成，但 AI 决策未形成有效入选，实例已阻断。"
                else:
                    blocked_stage = "strategy_position_build_blocked"
                    blocked_event = "strategy_position_build_blocked"
                    blocked_message = "策略已入选，但未能生成可追踪的策略持仓，实例已阻断。"
                _attach_strategy_decision_and_plan(
                    instance,
                    strategy_council,
                    strategy_positions,
                    strategy_opportunities,
                    strategy_risk_plan,
                    config,
                    strategy_auto_execute=strategy_auto_execute,
                    manual_review_required=True,
                )
                append_instance_event(
                    instance,
                    blocked_event,
                    blocked_message,
                    lifecycle_state="blocked",
                    status="error",
                    payload={"strategy_candidates": len(strategy_opportunities), "strategy_modes": list(config.get("strategy_modes") or [])},
                )
                _finish_trading_run(
                    run_id,
                    owner_id,
                    config,
                    status="failed",
                    stage=blocked_stage,
                    progress=70,
                    finished_at=utc_now(),
                    council_json=strategy_council,
                    selections_json=[],
                    orders_json=[],
                    instance_json=instance,
                    error=strategy_council["summary"],
                )
                return
            if not strategy_auto_execute:
                _attach_strategy_decision_and_plan(
                    instance,
                    strategy_council,
                    strategy_positions,
                    strategy_opportunities,
                    strategy_risk_plan,
                    config,
                    strategy_auto_execute=False,
                    manual_review_required=True,
                )
                append_instance_event(
                    instance,
                    "strategy_analysis_only",
                    "已生成策略结构候选，但当前配置未开启自动执行。",
                    lifecycle_state="manual_intervention_required",
                    status="warning",
                    payload={"strategy_candidates": len(strategy_opportunities), "strategy_modes": list(config.get("strategy_modes") or [])},
                )
                _finish_trading_run(
                    run_id,
                    owner_id,
                    config,
                    status="succeeded",
                    stage="strategy_analysis_only",
                    progress=100,
                    finished_at=utc_now(),
                    council_json=strategy_council,
                    selections_json=[],
                    orders_json=[],
                    instance_json=instance,
                    error=None,
                )
                return

            _attach_strategy_decision_and_plan(
                instance,
                strategy_council,
                strategy_positions,
                strategy_opportunities,
                strategy_risk_plan,
                config,
                strategy_auto_execute=True,
                manual_review_required=False,
            )
            append_instance_event(
                instance,
                "strategy_auto_execute",
                "已开启策略自动执行，系统将按腿级顺序提交多腿策略订单。",
                lifecycle_state="submitting",
                status="warning",
                payload={"strategy_candidates": len(strategy_opportunities), "strategy_modes": list(config.get("strategy_modes") or [])},
            )
            if dry_run:
                append_instance_event(
                    instance,
                    "dry_run_analysis_only",
                    f"Dry-run（无券商）：已完成策略结构候选与风控计划，模拟 {len(strategy_positions)} 个策略，不提交真实订单。",
                    lifecycle_state="closed",
                    status="info",
                    payload={"strategy_positions": len(strategy_positions), "mode": "strategy"},
                )
                _finish_trading_run(
                    run_id, owner_id, config,
                    status="succeeded", stage="dry_run_analysis_only", progress=100,
                    finished_at=utc_now(), council_json=strategy_council,
                    selections_json=[], orders_json=[], instance_json=instance, error=None,
                )
                return
            strategy_orders = _submit_strategy_orders(
                strategy_positions,
                config,
                account_ref,
                run_id=run_id,
                owner_id=owner_id,
            )
            _refresh_strategy_plan_after_orders(instance, strategy_positions, strategy_orders)
            attach_order_results(instance, strategy_orders)
            outcome = _strategy_auto_execute_outcome(strategy_orders)
            _finish_trading_run(
                run_id,
                owner_id,
                config,
                status=outcome["status"],
                stage=outcome["stage"],
                progress=100,
                finished_at=utc_now(),
                council_json=strategy_council,
                selections_json=[],
                orders_json=strategy_orders,
                instance_json=instance,
                error=outcome.get("error"),
            )
            return

        council = _rank_opportunities(opportunities, config, owner_id)
        data_integrity_summary = _scan_data_integrity_summary(scan_results)
        raw_selections = council.get("selections") or []
        valid_ai_selection_count = len(_unique_valid_selections(raw_selections, opportunities))
        mode_label = "三诸葛亮" if config.get("council", True) else "单 AI" if config.get("use_ai", True) else "本地评分"
        if valid_ai_selection_count <= 0:
            if data_integrity_summary.get("blocked"):
                council["summary"] = data_integrity_summary["message"]
                council["council_mode"] = "data_integrity_blocked"
                council["data_integrity"] = data_integrity_summary
            council["post_validation"] = {
                "status": "data_integrity_blocked" if data_integrity_summary.get("blocked") else "observe_only" if opportunities else "blocked",
                "target_count": min(int(config.get("top_n") or 5), len(opportunities)),
                "input_selection_count": len(raw_selections),
                "accepted_count": 0,
                "rejected_count": 0,
                "repaired_count": 0,
                "observation_only": bool(opportunities),
                "reason": (
                    data_integrity_summary["message"]
                    if data_integrity_summary.get("blocked")
                    else
                    "AI 已返回观察结论，但当前门控不允许自动下单。"
                    if opportunities
                    else f"strict live trading requires at least one valid {mode_label} selected contract before submitting orders"
                ),
            }
            council = _attach_execution_policy(council, [], config)
            council["execution_policy"]["blocked_by_decision_gate"] = bool(opportunities)
            attach_ai_decision(instance, council, [])
            attach_risk_and_execution_plan(instance, [], config)
            append_instance_event(
                instance,
                "data_integrity_blocked" if data_integrity_summary.get("blocked") else "decision_gate_blocked_execution" if opportunities else "council_blocked_execution",
                (
                    data_integrity_summary["message"]
                    if data_integrity_summary.get("blocked")
                    else
                    f"{mode_label}已给出观察候选，但决策门控未允许自动下单。"
                    if opportunities
                    else f"{mode_label}未返回任何可校验入选合约；严格实盘模式阻止提交订单。"
                ),
                lifecycle_state="blocked",
                status="error" if data_integrity_summary.get("blocked") else "warning" if opportunities else "error",
                payload={
                    "council_mode": council.get("council_mode"),
                    "summary": council.get("summary"),
                    "opportunity_count": len(opportunities),
                    "observation_only": bool(opportunities),
                    "data_integrity": data_integrity_summary if data_integrity_summary.get("blocked") else {},
                },
            )
            _finish_trading_run(
                run_id,
                owner_id,
                config,
                status="failed",
                stage="data_integrity_blocked" if data_integrity_summary.get("blocked") else "decision_gate_blocked" if opportunities else "council_blocked",
                progress=70,
                finished_at=utc_now(),
                council_json=council,
                selections_json=[],
                orders_json=[],
                instance_json=instance,
                error=(
                    data_integrity_summary["message"]
                    if data_integrity_summary.get("blocked")
                    else
                    "AI returned observation-only candidates; auto trade suppressed"
                    if opportunities
                    else council.get("summary") or f"{mode_label} produced no valid selections"
                ),
            )
            return
        selections = _normalize_selections(raw_selections, opportunities, config)
        selection_gate_issues = _selection_gate_issues(selections, opportunities)
        if selection_gate_issues:
            council["post_validation"] = {
                "status": "blocked",
                "target_count": min(int(config.get("top_n") or 5), len(opportunities)),
                "input_selection_count": len(raw_selections),
                "accepted_count": len(selections),
                "rejected_count": len(selection_gate_issues),
                "repaired_count": 0,
                "observation_only": True,
                "reason": "AI 已生成候选，但其中包含观察池或门控受限合约，系统不会自动下单。",
                "gate_issues": selection_gate_issues[:20],
            }
            council = _attach_execution_policy(council, selections, config)
            council["execution_policy"]["blocked_by_decision_gate"] = True
            council["execution_policy"]["observation_only_count"] = len(selection_gate_issues)
            attach_ai_decision(instance, council, selections)
            attach_risk_and_execution_plan(instance, [], config)
            append_instance_event(
                instance,
                "decision_gate_blocked_execution",
                f"{mode_label}已生成观察候选，但决策门控未允许自动下单。",
                lifecycle_state="blocked",
                status="warning",
                payload={
                    "council_mode": council.get("council_mode"),
                    "summary": council.get("summary"),
                    "opportunity_count": len(opportunities),
                    "gate_issues": selection_gate_issues[:5],
                },
            )
            _finish_trading_run(
                run_id,
                owner_id,
                config,
                status="failed",
                stage="decision_gate_blocked",
                progress=70,
                finished_at=utc_now(),
                council_json=council,
                selections_json=[],
                orders_json=[],
                instance_json=instance,
                error="AI selections are observation-only or gate-blocked; auto trade suppressed",
            )
            return
        selections, post_validation = _post_validate_and_repair_selections(selections, opportunities, config)
        council["post_validation"] = post_validation
        council = _attach_execution_policy(council, selections, config)
        attach_ai_decision(instance, council, selections)
        attach_risk_and_execution_plan(instance, selections, config)
        breaker_issues = _pre_submit_risk_breaker_issues(config, selections)
        if breaker_issues:
            append_instance_event(
                instance,
                "risk_circuit_breaker_blocked",
                "风控熔断阻止提交订单。",
                lifecycle_state="blocked",
                status="error",
                payload={"issues": breaker_issues},
            )
            _finish_trading_run(
                run_id,
                owner_id,
                config,
                status="failed",
                stage="risk_circuit_breaker",
                progress=75,
                finished_at=utc_now(),
                council_json=council,
                selections_json=selections,
                instance_json=instance,
                error="; ".join(breaker_issues),
            )
            return
        mark_trading_run(run_id, council_json=council, selections_json=selections, stage="submit_orders", progress=75, instance_json=instance)
        if dry_run:
            append_instance_event(
                instance,
                "dry_run_analysis_only",
                f"Dry-run（无券商）：已完成扫描与 AI 决策，模拟 {len(selections)} 个单腿选择，不提交真实订单。",
                lifecycle_state="closed",
                status="info",
                payload={"selections": len(selections), "mode": "single_leg"},
            )
            _finish_trading_run(
                run_id, owner_id, config,
                status="succeeded", stage="dry_run_analysis_only", progress=100,
                finished_at=utc_now(), council_json=council, selections_json=selections,
                orders_json=[], instance_json=instance, error=None,
            )
            return
        orders = _submit_orders(selections, config, account_ref, run_id=run_id, owner_id=owner_id)
        attach_order_results(instance, orders)
        outcome = _single_leg_order_outcome(orders)
        _finish_trading_run(
            run_id,
            owner_id,
            config,
            status=outcome["status"],
            stage=outcome["stage"],
            progress=100,
            finished_at=utc_now(),
            orders_json=orders,
            instance_json=instance,
            error=outcome.get("error"),
        )
    except Exception as exc:
        append_instance_event(instance, "failed", str(exc), lifecycle_state="blocked", status="error")
        _finish_trading_run(run_id, owner_id, config, status="failed", stage="failed", finished_at=utc_now(), error=str(exc), instance_json=instance)


def recent_trading_runs(owner_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return list_trading_runs(owner_id, limit, summary=True)


def trading_run_detail(run_id: str, owner_id: str, *, light: bool = False) -> dict[str, Any] | None:
    return get_trading_run(run_id, owner_id, light=light)


def _load_trade_instance(run_id: str, owner_id: str) -> dict[str, Any]:
    run = get_trading_run(run_id, owner_id)
    instance = dict((run or {}).get("trade_instance") or {})
    if instance:
        return instance
    return set_lifecycle({"instance_id": run_id, "owner_id": owner_id, "event_timeline": []}, "created")


def _scan_universe(config: dict[str, Any], account_name: str | None, owner_id: str) -> list[dict[str, Any]]:
    prompt_template = str(config.get("prompt_template") or "")
    universe = list(config.get("universe") or [])
    results: list[dict[str, Any]] = []
    candidates_per_symbol = _live_candidates_per_symbol(config)
    single_leg_allowed = _single_leg_mode_allowed(config)
    with ThreadPoolExecutor(max_workers=_live_scan_workers(len(universe)), thread_name_prefix="trade-scan") as executor:
        futures = {
            executor.submit(
                run_scan,
                query=_prompt_for_symbol(prompt_template, symbol),
                symbol=symbol,
                ai_provider=config.get("ai_provider") or "deepseek",
                longbridge_account=account_name,
                use_ai=False,
                council=False,
                analysis_modules=config.get("analysis_modules"),
                strategy_modes=config.get("strategy_modes"),
                low_gate_enabled=bool(config.get("low_gate_enabled")),
                market_data_source=config.get("market_data_source") or "thetadata",
                market_data_workers=1,
                ai_provider_owner=owner_id,
            ): symbol
            for symbol in universe
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result()
                payload = result.get("payload", {}) or {}
                raw_candidates = payload.get("option_candidates") or []
                raw_strategy_candidates = payload.get("strategy_candidates") or []
                candidate_rejections = _contract_candidate_rejections_for_symbol(symbol, raw_candidates)
                strategy_rejections = _strategy_candidate_rejections_for_symbol(symbol, raw_strategy_candidates)
                candidates = _filter_contract_candidates_for_symbol(symbol, raw_candidates)
                strategy_candidates = _filter_strategy_candidates_for_symbol(symbol, raw_strategy_candidates)
                decision_gate = payload.get("decision_gate") or {}
                top_candidates = list(candidates[:candidates_per_symbol]) if single_leg_allowed else []
                top_candidate = top_candidates[0] if top_candidates else None
                if single_leg_allowed:
                    gate_allows = _decision_gate_allows_single_leg_auto(decision_gate)
                    status = "succeeded" if top_candidate and gate_allows else "blocked_by_decision_gate" if top_candidate and not gate_allows else "no_candidate"
                else:
                    gate_allows = _decision_gate_allows_strategy(
                        decision_gate,
                        require_auto=_strategy_auto_execute_enabled(config),
                        structure_only=_strategy_structure_only_modes(config),
                        strategy_candidates=strategy_candidates,
                    )
                    status = "succeeded" if strategy_candidates and gate_allows else "blocked_by_decision_gate" if strategy_candidates and not gate_allows else "no_candidate"
                integrity_rejections = candidate_rejections + strategy_rejections
                if integrity_rejections and not top_candidates and not strategy_candidates:
                    status = "data_integrity_blocked"
                data_integrity = {
                    "status": "blocked" if status == "data_integrity_blocked" else "filtered" if integrity_rejections else "ok",
                    "rejected_count": len(integrity_rejections),
                    "contract_rejected_count": len(candidate_rejections),
                    "strategy_rejected_count": len(strategy_rejections),
                    "rejected": integrity_rejections[:12],
                }
                results.append(
                    {
                        "symbol": symbol,
                        "status": status,
                        "answer": result.get("answer"),
                        "candidate": top_candidate,
                        "candidates": top_candidates,
                        "strategy_candidates": strategy_candidates,
                        "decision_gate": decision_gate,
                        "decision_consistency": result.get("payload", {}).get("decision_consistency") or {},
                        "candidates_per_symbol": candidates_per_symbol,
                        "evidence_card": _symbol_evidence_card(
                            symbol=symbol,
                            technical_bias=result.get("payload", {}).get("technical_bias"),
                            daily_summary=result.get("payload", {}).get("daily_summary") or {},
                            intraday_summary=result.get("payload", {}).get("intraday_summary") or {},
                            candidates=top_candidates,
                            decision_gate=decision_gate,
                        ),
                        "technical_bias": result.get("payload", {}).get("technical_bias"),
                        "daily_summary": result.get("payload", {}).get("daily_summary"),
                        "intraday_summary": result.get("payload", {}).get("intraday_summary"),
                        "gate_blocked": status == "blocked_by_decision_gate",
                        "data_integrity": data_integrity,
                    }
                )
            except Exception as exc:
                results.append({"symbol": symbol, "status": "failed", "error": str(exc), "candidate": None})
    return sorted(results, key=_scan_result_sort_score, reverse=True)


def _market_data_account_name(config: dict[str, Any]) -> str | None:
    return str(config.get("longbridge_account") or "").strip() or None


def _strategy_analysis_only(config: dict[str, Any]) -> bool:
    modes = normalize_strategy_modes(config.get("strategy_modes"))
    return any(mode != "single_leg" for mode in modes) and not bool(config.get("strategy_auto_execute_enabled"))


def _single_leg_mode_allowed(config: dict[str, Any]) -> bool:
    return "single_leg" in normalize_strategy_modes(config.get("strategy_modes"))


def _normalized_underlying_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper().replace(" ", "")
    return re.sub(r"\.(US|HK|SH|SZ|SG)$", "", text)


def _option_contract_root(contract_symbol: Any) -> str:
    text = str(contract_symbol or "").strip().upper().replace(" ", "")
    text = re.sub(r"\.(US|HK|SH|SZ|SG)$", "", text)
    marker = re.search(r"\d{6}[CP]", text)
    if not marker:
        return ""
    return text[: marker.start()]


def _option_contract_matches_underlying(contract_symbol: Any, symbol: Any) -> bool:
    root = _option_contract_root(contract_symbol)
    underlying = _normalized_underlying_symbol(symbol)
    if not root or not underlying:
        return True
    if root == underlying:
        return True
    index_aliases = {
        "SPX": {"SPX", "SPXW"},
        "NDX": {"NDX", "NDXP"},
        "RUT": {"RUT", "RUTW"},
    }
    return root in index_aliases.get(underlying, set())


def _candidate_contract_mismatch(candidate: dict[str, Any], symbol: Any) -> str:
    contract_symbol = str(candidate.get("contract_symbol") or candidate.get("option_symbol") or "").strip()
    if contract_symbol and not _option_contract_matches_underlying(contract_symbol, symbol):
        return f"contract_root_mismatch:{_option_contract_root(contract_symbol)}!={_normalized_underlying_symbol(symbol)}"
    return ""


def _contract_candidate_rejections_for_symbol(symbol: Any, candidates: Any) -> list[dict[str, Any]]:
    rejected: list[dict[str, Any]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        reason = _candidate_contract_mismatch(candidate, symbol)
        if not reason:
            continue
        contract_symbol = str(candidate.get("contract_symbol") or candidate.get("option_symbol") or "").strip()
        rejected.append({
            "kind": "option_candidate",
            "symbol": _normalized_underlying_symbol(symbol),
            "contract_symbol": contract_symbol,
            "contract_root": _option_contract_root(contract_symbol),
            "reason": reason,
        })
    return rejected


def _filter_contract_candidates_for_symbol(symbol: Any, candidates: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        if _candidate_contract_mismatch(candidate, symbol):
            continue
        output.append(candidate)
    return output


def _strategy_contract_mismatch(candidate: dict[str, Any], symbol: Any) -> str:
    for leg in candidate.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        contract_symbol = leg.get("contract_symbol") or leg.get("option_symbol") or leg.get("symbol")
        if contract_symbol and not _option_contract_matches_underlying(contract_symbol, symbol):
            return f"strategy_leg_root_mismatch:{_option_contract_root(contract_symbol)}!={_normalized_underlying_symbol(symbol)}"
    return ""


def _strategy_candidate_rejections_for_symbol(symbol: Any, candidates: Any) -> list[dict[str, Any]]:
    rejected: list[dict[str, Any]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        for leg in candidate.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            contract_symbol = leg.get("contract_symbol") or leg.get("option_symbol") or leg.get("symbol")
            if not contract_symbol or _option_contract_matches_underlying(contract_symbol, symbol):
                continue
            text = str(contract_symbol).strip()
            rejected.append({
                "kind": "strategy_leg",
                "symbol": _normalized_underlying_symbol(symbol),
                "strategy_type": candidate.get("strategy_type"),
                "contract_symbol": text,
                "contract_root": _option_contract_root(text),
                "reason": f"strategy_leg_root_mismatch:{_option_contract_root(text)}!={_normalized_underlying_symbol(symbol)}",
            })
    return rejected


def _filter_strategy_candidates_for_symbol(symbol: Any, candidates: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        if _strategy_contract_mismatch(candidate, symbol):
            continue
        output.append(candidate)
    return output


def _scan_data_integrity_summary(scan_results: list[dict[str, Any]]) -> dict[str, Any]:
    rejected: list[dict[str, Any]] = []
    blocked_symbols: list[str] = []
    for item in scan_results or []:
        integrity = item.get("data_integrity") or {}
        rows = integrity.get("rejected") if isinstance(integrity, dict) else []
        if isinstance(rows, list):
            rejected.extend(row for row in rows if isinstance(row, dict))
        if item.get("status") == "data_integrity_blocked":
            blocked_symbols.append(str(item.get("symbol") or ""))
    count = len(rejected)
    blocked = bool(blocked_symbols and count)
    examples = ", ".join(
        f"{row.get('contract_symbol')}({row.get('reason')})"
        for row in rejected[:4]
        if row.get("contract_symbol")
    )
    message = (
        f"数据完整性阻断：扫描结果中有 {count} 个候选合约 root 与股票池标的不一致"
        + (f"：{examples}" if examples else "")
    ) if blocked else ""
    return {
        "blocked": blocked,
        "rejected_count": count,
        "blocked_symbols": [symbol for symbol in blocked_symbols if symbol],
        "rejected": rejected[:20],
        "message": message,
    }


def _strategy_auto_execute_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("strategy_auto_execute_enabled")) and _strategy_has_multi_leg_modes(config)


def _strategy_has_multi_leg_modes(config: dict[str, Any]) -> bool:
    modes = normalize_strategy_modes(config.get("strategy_modes"))
    return any(mode != "single_leg" for mode in modes)


def _strategy_structure_only_modes(config: dict[str, Any]) -> bool:
    modes = normalize_strategy_modes(config.get("strategy_modes"))
    if not modes:
        return False
    return all(mode != "single_leg" for mode in modes)


def _scan_result_sort_score(item: dict[str, Any]) -> float:
    candidate_score = _candidate_sort_score(item.get("candidate") or {})
    if candidate_score:
        return candidate_score
    strategy_scores = [
        _coerce_float(candidate.get("score"), _coerce_float(candidate.get("analysis_score"), 0.0))
        for candidate in (item.get("strategy_candidates") or [])
        if isinstance(candidate, dict)
    ]
    return max(strategy_scores, default=0.0)


def _attach_strategy_decision_and_plan(
    instance: dict[str, Any],
    strategy_council: dict[str, Any],
    strategy_positions: list[dict[str, Any]],
    strategy_opportunities: list[dict[str, Any]],
    strategy_risk_plan: dict[str, Any],
    config: dict[str, Any],
    *,
    strategy_auto_execute: bool,
    manual_review_required: bool,
) -> dict[str, Any]:
    planned_loss = _strategy_planned_loss(strategy_positions)
    risk_notes = list(strategy_risk_plan.get("risk_notes") or [])
    if manual_review_required and strategy_positions and not strategy_auto_execute:
        risk_notes = ["非单腿策略当前仅生成结构候选，不自动提交券商订单。", *risk_notes]
    post_validation = strategy_council.get("post_validation") or {
        "strategy_analysis_only": not strategy_auto_execute,
        "strategy_auto_execute": bool(strategy_auto_execute),
        "status": "passed" if strategy_positions else "blocked",
    }
    instance["ai_decision"] = {
        "council_mode": strategy_council.get("council_mode"),
        "summary": strategy_council.get("summary"),
        "selection_count": len(strategy_positions),
        "selected_contracts": [item.get("tracking_id") for item in strategy_positions],
        "final_top_n": [_strategy_selection_card(item) for item in strategy_positions],
        "rejected_count": len(strategy_council.get("rejected") or []) if isinstance(strategy_council.get("rejected"), list) else 0,
        "rejected": (strategy_council.get("rejected") or [])[:20] if isinstance(strategy_council.get("rejected"), list) else [],
        "advisor_reports": strategy_council.get("advisor_reports") or [],
        "ai_execution": strategy_council.get("ai_execution") or {},
        "top_up": strategy_council.get("top_up") or {},
        "post_validation": post_validation,
        "risk_notes": risk_notes,
        "strategy_candidates": strategy_opportunities,
        "strategy_risk_plan": strategy_risk_plan,
    }
    append_instance_event(
        instance,
        "strategy_decision",
        f"策略决策完成：入选 {len(strategy_positions)} 个结构。",
        lifecycle_state="approved" if strategy_positions else "blocked",
        status="success" if strategy_positions else "warning",
        payload={"selected_strategies": [item.get("tracking_id") for item in strategy_positions]},
    )
    instance["risk_plan"] = {
        "total_planned_capital": float(config.get("total_capital") or 0),
        "planned_contracts": len(strategy_positions),
        "planned_premium_at_risk": planned_loss,
        "max_loss_if_all_premiums_lost": planned_loss,
        "positions": [],
        "strategy_positions": strategy_positions,
        "strategy_tracking_count": len(strategy_positions),
        "ai_strategy_risk_plan": strategy_risk_plan,
        "strategy_analysis_only": not strategy_auto_execute,
        "strategy_auto_execute_enabled": bool(strategy_auto_execute),
    }
    instance["execution_plan"] = {
        "entry_order_type": config.get("entry_order_type"),
        "wait_for_fill_seconds": int(config.get("wait_for_fill_seconds") or 0),
        "software_stop_enabled": bool(config.get("software_stop_enabled", True)),
        "software_take_profit_enabled": bool(config.get("software_take_profit_enabled", True)),
        "orders": [],
        "strategy_analysis_only": not strategy_auto_execute,
        "strategy_auto_execute_enabled": bool(strategy_auto_execute),
        "manual_review_required": bool(manual_review_required),
        "strategy_orders": _strategy_planned_order_cards(strategy_positions, "pending_submit" if strategy_auto_execute else "manual_review_required"),
    }
    append_instance_event(
        instance,
        "strategy_risk_plan",
        f"策略风控与执行计划已生成：{len(strategy_positions)} 个结构，计划最大亏损约 ${planned_loss:.2f}。",
        lifecycle_state="submitting" if strategy_positions and strategy_auto_execute else "manual_intervention_required" if strategy_positions else "blocked",
        status="info" if strategy_positions else "warning",
        payload={"planned_premium_at_risk": planned_loss, "strategy_tracking_count": len(strategy_positions)},
    )
    return instance


def _refresh_strategy_plan_after_orders(
    instance: dict[str, Any],
    strategy_positions: list[dict[str, Any]],
    strategy_orders: list[dict[str, Any]],
) -> dict[str, Any]:
    risk_plan = instance.setdefault("risk_plan", {})
    risk_plan["strategy_positions"] = strategy_positions
    risk_plan["strategy_tracking_count"] = len(strategy_positions)
    positions_by_id = {str(item.get("tracking_id") or ""): item for item in strategy_positions}
    executed_units = 0
    executed_contracts = 0
    executed_risk = 0.0
    temporary_exposure = 0.0
    for order in strategy_orders:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "")
        units = max(0, int(float(order.get("units") or 0)))
        has_fill = any(int(float(leg.get("filled_quantity") or 0)) > 0 for leg in (order.get("legs") or []) if isinstance(leg, dict))
        effective_units = 0 if status.startswith(("blocked_", "skipped_")) or (status == "failed" and not has_fill) else units
        position = positions_by_id.get(str(order.get("tracking_id") or ""), {})
        executed_units += effective_units
        leg_ratio = sum(
            max(1, int(float((leg.get("leg") or leg).get("qty") or 1)))
            for leg in (order.get("legs") or position.get("legs") or [])
            if isinstance(leg, dict) and str((leg.get("leg") or leg).get("side") or "").lower() != "stock"
        )
        executed_contracts += effective_units * leg_ratio
        unit_risk = float(position.get("max_loss") or order.get("capital_required") or 0)
        if has_fill and status != "submitted":
            unit_risk = max(unit_risk, float(order.get("risk_capital_per_unit") or 0))
        executed_risk += unit_risk * effective_units
        temporary_exposure += float(order.get("temporary_exposure_per_unit") or 0) * effective_units
    risk_plan["planned_strategy_count"] = len(strategy_positions)
    risk_plan["planned_units"] = executed_units
    risk_plan["planned_contracts"] = executed_contracts
    risk_plan["planned_premium_at_risk"] = round(executed_risk, 2)
    risk_plan["max_loss_if_all_premiums_lost"] = round(executed_risk, 2)
    risk_plan["temporary_leg_exposure"] = round(temporary_exposure, 2)
    execution_plan = instance.setdefault("execution_plan", {})
    execution_plan["strategy_orders"] = strategy_orders
    ai_decision = instance.setdefault("ai_decision", {})
    ai_decision["final_top_n"] = [_strategy_selection_card(item) for item in strategy_positions]
    return instance


def _strategy_planned_loss(strategy_positions: list[dict[str, Any]]) -> float:
    return round(sum(float(item.get("max_loss") or 0) for item in strategy_positions), 2)


def _strategy_planned_order_cards(strategy_positions: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    return [
        {
            "tracking_id": item.get("tracking_id"),
            "symbol": item.get("symbol"),
            "strategy_type": item.get("strategy_type"),
            "status": status,
            "risk_tracking_active": item.get("risk_tracking_active"),
        }
        for item in strategy_positions
    ]


def _strategy_opportunities(scan_results: list[dict[str, Any]], config: dict[str, Any], account_name: str | None = None) -> list[dict[str, Any]]:
    limit = _live_candidates_per_symbol(config)
    opportunities: list[dict[str, Any]] = []
    seen: set[str] = set()
    require_auto = _strategy_auto_execute_enabled(config)
    structure_only = _strategy_structure_only_modes(config)
    for item in scan_results:
        decision_gate = item.get("decision_gate") or {}
        raw = _filter_strategy_candidates_for_symbol(item.get("symbol"), item.get("strategy_candidates") or [])
        if not _decision_gate_allows_strategy(
            decision_gate,
            require_auto=require_auto,
            structure_only=structure_only,
            strategy_candidates=raw,
        ):
            continue
        strategies = [candidate for candidate in raw if isinstance(candidate, dict) and candidate.get("strategy_type")]
        symbol_rows: list[dict[str, Any]] = []
        for index, candidate in enumerate(strategies, start=1):
            candidate_row = dict(candidate)
            family_gate = _strategy_family_gate(decision_gate, str(candidate_row.get("family") or candidate_row.get("strategy_type") or ""))
            if family_gate and not family_gate.get("allowed"):
                continue
            if family_gate:
                candidate_row["strategy_family_gate"] = family_gate
            candidate_row = _annotate_strategy_live_precheck(candidate_row, item.get("symbol"), account_name)
            strategy_key = _strategy_opportunity_key(item, candidate_row, index)
            dedupe_key = f"{candidate_row.get('strategy_type')}::{candidate_row.get('expiration')}::{candidate_row.get('summary') or candidate_row.get('label')}::{strategy_key}"
            if dedupe_key in seen:
                continue
            candidate_row["strategy_key"] = strategy_key
            symbol_rows.append(
                {
                    "_dedupe_key": dedupe_key,
                    "strategy_key": strategy_key,
                    "symbol": item.get("symbol"),
                    "strategy_type": candidate_row.get("strategy_type"),
                    "family": candidate_row.get("family"),
                    "label": candidate_row.get("label"),
                    "direction": candidate_row.get("direction"),
                    "expiration": candidate_row.get("expiration"),
                    "candidate_rank_for_symbol": index,
                    "candidate": candidate_row,
                    "evidence_card": {
                        "symbol": item.get("symbol"),
                        "technical_bias": item.get("technical_bias"),
                        "daily_summary": item.get("daily_summary"),
                        "intraday_summary": item.get("intraday_summary"),
                        "strategy_candidate": candidate_row,
                    },
                }
            )
        for row in _select_strategy_rows_for_symbol(symbol_rows, config, limit):
            seen.add(str(row.pop("_dedupe_key", "")))
            opportunities.append(row)
    return sorted(opportunities, key=lambda item: float((item.get("candidate") or {}).get("score") or 0), reverse=True)


def _select_strategy_rows_for_symbol(rows: list[dict[str, Any]], config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    modes = [mode for mode in normalize_strategy_modes(config.get("strategy_modes")) if mode != "single_leg"]
    target_count = max(limit, len(modes), 1)
    selected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    rows_by_score = sorted(rows, key=lambda item: float((item.get("candidate") or {}).get("score") or 0), reverse=True)
    for mode in modes:
        family_rows = [
            item
            for item in rows_by_score
            if str((item.get("candidate") or {}).get("family") or item.get("family") or "") == mode
            or str((item.get("candidate") or {}).get("strategy_type") or item.get("strategy_type") or "") == mode
        ]
        if not family_rows:
            continue
        key = str(family_rows[0].get("strategy_key") or "")
        if key and key not in seen_keys:
            selected.append(family_rows[0])
            seen_keys.add(key)
    for item in rows_by_score:
        if len(selected) >= target_count:
            break
        key = str(item.get("strategy_key") or "")
        if key in seen_keys:
            continue
        selected.append(item)
        seen_keys.add(key)
    return selected


def _strategy_opportunity_key(scan_result: dict[str, Any], candidate: dict[str, Any], index: int) -> str:
    explicit = str(candidate.get("strategy_key") or "").strip()
    symbol = str(scan_result.get("symbol") or candidate.get("symbol") or "symbol").strip() or "symbol"
    legs_signature = _strategy_legs_key(candidate)
    if explicit:
        if explicit.startswith(f"{symbol}::"):
            return explicit
        parts = [symbol, explicit]
        if legs_signature:
            parts.append(legs_signature)
        else:
            parts.append(str(index))
        return "::".join(str(part).replace(" ", "_") for part in parts)
    parts = [
        symbol,
        candidate.get("family") or "strategy",
        candidate.get("strategy_type") or "type",
        candidate.get("expiration") or "exp",
        candidate.get("label") or index,
        legs_signature or index,
        index,
    ]
    return "::".join(str(part).replace(" ", "_") for part in parts)


def _strategy_legs_key(candidate: dict[str, Any]) -> str:
    legs = candidate.get("legs") or []
    if not isinstance(legs, list):
        return ""
    parts: list[str] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        contract = str(leg.get("contract_symbol") or leg.get("option_symbol") or leg.get("symbol") or "").strip()
        action = str(leg.get("action") or leg.get("side") or "").strip()
        qty = str(leg.get("qty") or leg.get("quantity") or 1).strip()
        if contract:
            parts.append(f"{action}:{qty}:{contract}")
    return "|".join(parts)


def _annotate_strategy_live_precheck(candidate: dict[str, Any], symbol: Any, account_name: str | None) -> dict[str, Any]:
    row = dict(candidate)
    flags = list(row.get("hard_flags") or [])
    blocking_flags = _strategy_blocking_flags(row)
    live_executable = not blocking_flags
    family = str(row.get("family") or "")
    if family in {"covered_call", "collar"}:
        stock_qty = _stock_quantity_for_symbol(str(symbol or row.get("symbol") or ""), account_name) if account_name else 0
        row["stock_quantity"] = stock_qty
        if stock_qty < 100:
            flags.append("needs_stock_backing")
            live_executable = False
    if family == "cash_secured_put":
        buy_power = _buy_power_for_account(account_name) if account_name else 0.0
        row["buy_power"] = round(buy_power, 2)
        if buy_power < float(row.get("capital_required") or 0):
            flags.append("needs_cash_secured")
            live_executable = False
    row["hard_flags"] = list(dict.fromkeys(flags))
    row["live_executable"] = live_executable
    if not live_executable:
        notes = list(row.get("fit_notes") or [])
        if "实盘执行前置检查未通过" not in notes:
            notes.append("实盘执行前置检查未通过")
        row["fit_notes"] = notes
    return row


def _strategy_blocking_flags(candidate: dict[str, Any]) -> list[str]:
    flags = set(candidate.get("hard_flags") or [])
    return sorted(flags & {"bad_long_ask", "short_leg_bid_unavailable", "net_price_inconsistent"})


def _strategy_family_gate(gate: dict[str, Any], family: str) -> dict[str, Any]:
    normalized = str(family or "").strip()
    family_gates = gate.get("strategy_family_gates") or {}
    row = family_gates.get(normalized) if isinstance(family_gates, dict) else None
    if isinstance(row, dict):
        return row
    allowed = gate.get("allowed_strategy_families") or {}
    if isinstance(allowed, dict) and normalized in allowed:
        return {"allowed": bool(allowed.get(normalized)), "reasons": [], "warnings": [], "blockers": []}
    return {}


def _stock_quantity_for_symbol(symbol: str, account_name: str | None) -> int:
    symbol = symbol.strip().upper()
    if not symbol or not account_name:
        return 0
    rows = lb_positions(account_name)
    target = f"{symbol}.US" if not symbol.endswith(".US") else symbol
    qty = 0
    for row in rows:
        row_symbol = str(row.get("symbol") or row.get("order_symbol") or "").strip().upper()
        if row_symbol == target or row_symbol == symbol:
            try:
                qty += int(float(row.get("available_quantity") or row.get("quantity") or 0))
            except (TypeError, ValueError):
                continue
    return qty


def _buy_power_for_account(account_name: str | None) -> float:
    if not account_name:
        return 0.0
    try:
        rows = lb_assets(account_name, "USD")
    except Exception:  # noqa: BLE001
        return 0.0
    for row in rows:
        for key in ("buy_power", "available_cash", "total_cash", "cash"):
            try:
                value = float(row.get(key) or 0)
            except (TypeError, ValueError, AttributeError):
                value = 0.0
            if value > 0:
                return value
    return 0.0


def _build_strategy_risk_positions(
    strategy_opportunities: list[dict[str, Any]],
    config: dict[str, Any],
    strategy_selections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    top_n = max(1, min(int(config.get("top_n") or 1), 20))
    max_per_symbol = max(0, int(config.get("max_per_symbol") or 1))
    total_capital = float(config.get("total_capital") or 0)
    stop_loss_pct = max(1.0, min(float(config.get("default_stop_loss_pct") or 25), 95.0))
    selected_keys = [str(item.get("strategy_key") or "") for item in strategy_selections or [] if item.get("strategy_key")]
    by_key = {_strategy_selection_key(item): item for item in strategy_opportunities if _strategy_selection_key(item)}
    selection_by_key = {str(item.get("strategy_key") or ""): item for item in strategy_selections or [] if item.get("strategy_key")}
    if selected_keys:
        raw_opportunities = [by_key[key] for key in selected_keys if key in by_key]
    else:
        raw_opportunities = strategy_opportunities
    # Enforce per-symbol diversity: keep at most max_per_symbol per symbol
    if max_per_symbol > 0:
        seen_symbols: dict[str, int] = {}
        diversified: list[dict[str, Any]] = []
        for opp in raw_opportunities:
            sym = str(opp.get("symbol") or "").strip()
            if sym and seen_symbols.get(sym, 0) >= max_per_symbol:
                continue
            diversified.append(opp)
            if sym:
                seen_symbols[sym] = seen_symbols.get(sym, 0) + 1
        raw_opportunities = diversified
    selected_opportunities = raw_opportunities[:top_n]
    allocation_map = _strategy_allocation_map(selected_opportunities, selection_by_key, config)
    positions: list[dict[str, Any]] = []
    for index, opportunity in enumerate(selected_opportunities, start=1):
        candidate = opportunity.get("candidate") or {}
        if not isinstance(candidate, dict):
            continue
        if candidate.get("live_executable") is False and _strategy_auto_execute_enabled(config):
            continue
        strategy_key = _strategy_selection_key(opportunity)
        raw_selection = selection_by_key.get(strategy_key) or {}
        entry_mark = _strategy_entry_mark(candidate)
        max_loss = _coerce_float(candidate.get("max_loss"), 0.0)
        max_profit = _coerce_float(candidate.get("max_profit"), 0.0)
        capital_required = _coerce_float(candidate.get("capital_required"), 0.0)
        # For credit spreads (entry_mark < 0), abs(entry_mark) = credit received = max PROFIT,
        # not max RISK. Use capital_required (margin = max_loss) as risk basis instead, so
        # stop_loss_pct is applied to capital at risk, not to the premium collected.
        if entry_mark < 0:
            risk_basis = max(max_loss, capital_required, 1.0)
        else:
            risk_basis = max(max_loss, capital_required * 0.25, abs(entry_mark), 1.0)
        # For profit_basis: credit spreads (entry_mark < 0) profit up to the full
        # credit received (abs(entry_mark)); debit spreads measure take-profit %
        # against the premium paid (risk_basis = capital deployed), consistent
        # with stop-loss and single-leg, so "20%" means +20% on cost.
        if entry_mark < 0:
            profit_basis = max(max_profit, abs(entry_mark), risk_basis * 0.6)
        else:
            profit_basis = risk_basis
        take_profit_plan = _strategy_take_profit_pnls(profit_basis, config)
        allocation_pct = allocation_map.get(strategy_key, round(1 / max(len(selected_opportunities), 1), 4))
        selection_source = str(raw_selection.get("selection_source") or "strategy_ai_initial").strip() if raw_selection else "strategy_score_fallback"
        is_score_selection = selection_source.startswith("strategy_score")
        positions.append(
            {
                "tracking_id": f"strategy-{index}",
                "strategy_key": strategy_key,
                "symbol": opportunity.get("symbol"),
                "family": candidate.get("family"),
                "strategy_type": candidate.get("strategy_type"),
                "label": candidate.get("label"),
                "direction": candidate.get("direction"),
                "expiration": candidate.get("expiration"),
                "legs": candidate.get("legs") or [],
                "entry_mark": round(entry_mark, 2),
                "net_debit": _coerce_float(candidate.get("net_debit"), 0.0),
                "net_credit": _coerce_float(candidate.get("net_credit"), 0.0),
                "max_loss": max_loss,
                "max_profit": candidate.get("max_profit"),
                "capital_required": capital_required,
                "allocation_pct": allocation_pct,
                "allocation_amount": round(total_capital * allocation_pct, 2),
                "allocation_source": "ai" if config.get("ai_adjust_allocation") and raw_selection and not is_score_selection else "equal_default",
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_plan["take_profit_pct"],
                "take_profit_1_pct": take_profit_plan["take_profit_1_pct"],
                "take_profit_2_pct": take_profit_plan["take_profit_2_pct"],
                "tiered_take_profit_enabled": take_profit_plan["tiered_take_profit_enabled"],
                "market_data_source": config.get("market_data_source") or "yfinance",
                "stop_loss_pnl": round(-risk_basis * stop_loss_pct / 100, 2),
                "take_profit_1_pnl": take_profit_plan["take_profit_1_pnl"],
                "take_profit_2_pnl": take_profit_plan["take_profit_2_pnl"],
                "risk_plan_source": "system_default",
                "risk_tracking_active": True,
                "tracking_status": "armed",
                "take_profit_1_status": "pending",
                "take_profit_2_status": "pending",
                "manual_review_required": bool(candidate.get("live_executable") is False),
                "execution_blocked": bool(candidate.get("live_executable") is False),
                "source": "strategy_score_selection" if is_score_selection else "strategy_ai_selection" if raw_selection else "strategy_score_fallback",
                "selection_source": selection_source,
                "reason": raw_selection.get("reason") or "",
                "score": _coerce_float(candidate.get("score"), 0.0),
                "fit_notes": candidate.get("fit_notes") or [],
                "hard_flags": candidate.get("hard_flags") or [],
                "live_executable": candidate.get("live_executable", True),
                "stock_quantity": candidate.get("stock_quantity", 0),
                "natural_exit": candidate.get("natural_exit") or {},
                "structure_fit_score": _coerce_float(candidate.get("structure_fit_score"), 0.0),
                "payoff_quality_score": _coerce_float(candidate.get("payoff_quality_score"), 0.0),
                "execution_complexity_score": _coerce_float(candidate.get("execution_complexity_score"), 0.0),
                "capital_efficiency_score": _coerce_float(candidate.get("capital_efficiency_score"), 0.0),
                "risk_defined_score": _coerce_float(candidate.get("risk_defined_score"), 0.0),
                "quote_consistency_score": _coerce_float(candidate.get("quote_consistency_score"), 0.0),
                "quote_consistency_state": candidate.get("quote_consistency_state") or "unknown",
                "summary": candidate.get("summary"),
            }
        )
    return positions


def _strategy_allocation_map(
    opportunities: list[dict[str, Any]],
    selections_by_key: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, float]:
    keys = [_strategy_selection_key(item) for item in opportunities if _strategy_selection_key(item)]
    if not keys:
        return {}
    cap = max(0.0, min(float(config.get("max_allocation_pct_per_trade") or 0.0), 1.0))

    def _clamp_map(alloc: dict[str, float]) -> dict[str, float]:
        # Cap any single position's share of the budget (0 = uncapped). Auto-trade
        # sets this per preset so the LLM can't over-concentrate the whole budget.
        if cap <= 0:
            return alloc
        return {key: min(value, cap) for key, value in alloc.items()}

    if not config.get("ai_adjust_allocation"):
        equal = round(1 / len(keys), 4)
        return _clamp_map({key: equal for key in keys})
    raw = {key: max(0.0, _coerce_allocation((selections_by_key.get(key) or {}).get("allocation_pct"))) for key in keys}
    total = sum(raw.values())
    if total <= 0:
        equal = round(1 / len(keys), 4)
        return _clamp_map({key: equal for key in keys})
    if total > 1:
        return _clamp_map({key: value / total for key, value in raw.items()})
    zero_keys = [key for key, value in raw.items() if value <= 0]
    remaining = max(0.0, 1 - total)
    if zero_keys and remaining > 0:
        fill = remaining / len(zero_keys)
        for key in zero_keys:
            raw[key] = fill
    return _clamp_map(raw)


def _apply_ai_strategy_risk_plan(
    strategy_positions: list[dict[str, Any]],
    scan_results: list[dict[str, Any]],
    config: dict[str, Any],
    owner_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not strategy_positions:
        return strategy_positions, {"source": "none", "summary": "没有策略结构候选。", "plans": [], "risk_notes": []}
    if not config.get("use_ai", True):
        return strategy_positions, {
            "source": "system_default",
            "summary": "AI 已关闭，策略结构使用系统默认 PnL 风控线。",
            "plans": [],
            "risk_notes": [],
        }
    if not config.get("ai_adjust_stop_loss", True) and not config.get("ai_adjust_take_profit", False):
        return strategy_positions, {
            "source": "system_default",
            "summary": "AI 调止损/止盈已关闭，策略结构使用系统默认 PnL 风控线。",
            "plans": [],
            "risk_notes": [],
        }

    payload = {
        "total_capital": float(config.get("total_capital") or 0),
        "default_stop_loss_pct": float(config.get("default_stop_loss_pct") or 25),
        "default_take_profit_pct": float(config.get("default_take_profit_pct") or 30),
        "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled")),
        "default_take_profit_1_pct": float(config.get("default_take_profit_1_pct") or 20),
        "default_take_profit_2_pct": float(config.get("default_take_profit_2_pct") or 35),
        "ai_adjust_stop_loss": bool(config.get("ai_adjust_stop_loss", True)),
        "ai_adjust_take_profit": bool(config.get("ai_adjust_take_profit", False)),
        "strategy_modes": list(config.get("strategy_modes") or []),
        "strategy_positions": [_compact_strategy_position_for_ai(item) for item in strategy_positions],
        "market_context": [_compact_scan_context(item) for item in scan_results],
    }
    provider_name = config.get("ai_provider") or "deepseek"
    try:
        answer = ask_ai(
            STRATEGY_RISK_PLANNER_PROMPT,
            payload,
            provider_name,
            owner_id=owner_id,
            temperature=DECISION_TEMPERATURE,
            response_format=JSON_RESPONSE_FORMAT,
        )
        parsed = extract_json_object(answer)
    except Exception as exc:  # noqa: BLE001 - keep strategy instances usable if AI risk planning fails.
        return strategy_positions, {
            "source": "system_default",
            "summary": "AI 策略风控规划失败，已保留系统默认 PnL 风控线。",
            "plans": [],
            "risk_notes": [str(exc)[:240]],
            "error": str(exc),
        }
    if not parsed or not isinstance(parsed.get("plans"), list):
        return strategy_positions, {
            "source": "system_default",
            "summary": "AI 策略风控规划返回不可解析，已保留系统默认 PnL 风控线。",
            "plans": [],
            "risk_notes": [],
            "raw_answer": answer,
        }

    by_id = {str(item.get("tracking_id")): item for item in strategy_positions}
    applied_plans = []
    rejected_plans = []
    for raw_plan in parsed.get("plans") or []:
        if not isinstance(raw_plan, dict):
            continue
        tracking_id = str(raw_plan.get("tracking_id") or "")
        position = by_id.get(tracking_id)
        if not position:
            rejected_plans.append({"tracking_id": tracking_id, "reason": "tracking_id not found"})
            continue
        applied = _apply_one_strategy_risk_plan(position, raw_plan, config)
        if applied.get("applied"):
            applied_plans.append(applied)
        else:
            rejected_plans.append(applied)

    return strategy_positions, {
        "source": "ai" if applied_plans else "system_default",
        "summary": parsed.get("summary") or ("AI 已生成策略结构风控计划。" if applied_plans else "AI 未生成可用策略风控计划。"),
        "plans": applied_plans,
        "rejected_plans": rejected_plans,
        "risk_notes": parsed.get("risk_notes") if isinstance(parsed.get("risk_notes"), list) else [],
        "raw_answer": answer,
    }


def _apply_one_strategy_risk_plan(position: dict[str, Any], raw_plan: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    entry_mark = _coerce_float(position.get("entry_mark"), 0.0)
    max_loss = max(_coerce_float(position.get("max_loss"), 0.0), _coerce_float(position.get("capital_required"), 0.0) * 0.25, abs(entry_mark), 1.0)
    max_profit = _coerce_float(position.get("max_profit"), 0.0)
    # Debit spreads (entry_mark >= 0) measure take-profit % against premium paid
    # (max_loss ≈ capital deployed); credit spreads against max profit. Keeps
    # "20% take-profit" meaning +20% on cost for debit spreads. The post-fill
    # reprice is authoritative; this aligns dry-run/display values.
    fallback_profit = max(max_loss, 1.0) if entry_mark >= 0 else max(max_profit, max_loss * 0.6, 1.0)
    stop_loss_pnl = _coerce_float(raw_plan.get("stop_loss_pnl"), _coerce_float(position.get("stop_loss_pnl"), -max_loss * 0.25))
    take_profit_source = raw_plan if config.get("ai_adjust_take_profit") else position
    take_profit_plan = _strategy_take_profit_pnls(fallback_profit, config, take_profit_source)
    tp1 = _coerce_float(raw_plan.get("take_profit_1_pnl"), take_profit_plan["take_profit_1_pnl"]) if config.get("ai_adjust_take_profit") else _coerce_float(position.get("take_profit_1_pnl"), take_profit_plan["take_profit_1_pnl"])
    tp2 = _coerce_float(raw_plan.get("take_profit_2_pnl"), take_profit_plan["take_profit_2_pnl"]) if config.get("ai_adjust_take_profit") and take_profit_plan["tiered_take_profit_enabled"] else take_profit_plan["take_profit_2_pnl"]
    exit_conditions = _normalize_strategy_exit_conditions(raw_plan.get("exit_conditions"), position)
    if config.get("ai_adjust_stop_loss", True) and stop_loss_pnl >= 0:
        return {"tracking_id": position.get("tracking_id"), "applied": False, "reason": "stop_loss_pnl must be negative"}
    lower_bound = -max_loss if max_loss > 0 else stop_loss_pnl
    stop_loss_pnl = max(stop_loss_pnl, lower_bound) if config.get("ai_adjust_stop_loss", True) else _coerce_float(position.get("stop_loss_pnl"), -max_loss * 0.25)
    tp1 = max(tp1, 1.0)
    tp2 = max(tp2, tp1) if take_profit_plan["tiered_take_profit_enabled"] else 0.0
    if max_profit > 0:
        tp1 = min(tp1, max_profit)
        tp2 = min(tp2, max_profit) if tp2 > 0 else 0.0
        if tp2 < tp1:
            tp2 = tp1 if take_profit_plan["tiered_take_profit_enabled"] else 0.0
    position["stop_loss_pnl"] = round(stop_loss_pnl, 2)
    position["tiered_take_profit_enabled"] = bool(take_profit_plan["tiered_take_profit_enabled"])
    position["take_profit_pct"] = take_profit_plan["take_profit_pct"]
    position["take_profit_1_pct"] = take_profit_plan["take_profit_1_pct"]
    position["take_profit_2_pct"] = take_profit_plan["take_profit_2_pct"]
    position["take_profit_1_pnl"] = round(tp1, 2)
    position["take_profit_2_pnl"] = round(tp2, 2)
    position["latest_exit"] = str(raw_plan.get("latest_exit") or position.get("latest_exit") or "").strip()
    position["invalidation"] = str(raw_plan.get("invalidation") or position.get("invalidation") or "").strip()
    position["allow_overnight"] = bool(raw_plan.get("allow_overnight")) if raw_plan.get("allow_overnight") is not None else bool(position.get("allow_overnight", False))
    if config.get("force_no_overnight"):
        position["allow_overnight"] = False
    position["exit_conditions"] = exit_conditions
    position["ai_risk_plan"] = {
        "stop_loss_pnl": position["stop_loss_pnl"],
        "tiered_take_profit_enabled": position["tiered_take_profit_enabled"],
        "take_profit_pct": position["take_profit_pct"],
        "take_profit_1_pct": position["take_profit_1_pct"],
        "take_profit_2_pct": position["take_profit_2_pct"],
        "take_profit_1_pnl": position["take_profit_1_pnl"],
        "take_profit_2_pnl": position["take_profit_2_pnl"],
        "latest_exit": position["latest_exit"],
        "invalidation": position["invalidation"],
        "allow_overnight": position["allow_overnight"],
        "exit_conditions": exit_conditions,
        "confidence": _coerce_float(raw_plan.get("confidence"), 0.0),
        "reason": str(raw_plan.get("reason") or "").strip(),
    }
    position["risk_plan_source"] = "ai"
    return {"tracking_id": position.get("tracking_id"), "applied": True, **position["ai_risk_plan"]}


def _submit_strategy_orders(
    strategy_positions: list[dict[str, Any]],
    config: dict[str, Any],
    account_name: str,
    *,
    run_id: str | None = None,
    owner_id: str = "local",
) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    # Track capital/stock committed by earlier positions so the shared buy-power
    # pool and per-symbol stock backing aren't double-counted across positions
    # in the same run.
    committed: dict[str, Any] = {"buy_power": 0.0, "stock": {}}
    for position in strategy_positions:
        order_record = _submit_one_strategy_order(
            position,
            config,
            account_name,
            committed,
            run_id=run_id,
            owner_id=owner_id,
        )
        units = int(order_record.get("units") or 0)
        family = str(position.get("family") or "")
        if units > 0:
            if family == "cash_secured_put":
                per_unit = max(_coerce_float(position.get("capital_required"), 0.0), _strategy_unit_capital_required(position), 0.0)
                committed["buy_power"] = float(committed.get("buy_power") or 0.0) + per_unit * units
            elif family in {"covered_call", "collar"}:
                sym = str(position.get("symbol") or "").strip().upper()
                committed["stock"][sym] = int(committed["stock"].get(sym, 0)) + units * 100
        position["execution_status"] = order_record.get("status")
        position["strategy_units"] = order_record.get("units") or 0
        position["strategy_entry_orders"] = order_record.get("legs") or []
        position["risk_tracking_active"] = bool(order_record.get("risk_tracking_active"))
        if order_record.get("error"):
            position["execution_error"] = order_record.get("error")
            position["tracking_status"] = "execution_failed"
        elif order_record.get("status") == "submitted":
            position["tracking_status"] = "armed"
            position["actual_entry_at"] = order_record.get("actual_entry_at") or utc_now()
        else:
            position["tracking_status"] = order_record.get("status") or "pending"
        orders.append(order_record)
    return orders


def _strategy_auto_execute_outcome(strategy_orders: list[dict[str, Any]]) -> dict[str, str | None]:
    if not strategy_orders:
        return {"status": "failed", "stage": "strategy_no_orders", "error": "strategy auto execute produced no orders"}
    protection = build_protection_status(strategy_orders)
    protection_state = str(protection.get("state") or "")
    statuses = {str(order.get("status") or "") for order in strategy_orders}
    manual_states = {"unprotected", "strategy_residual_tracking", "broker_combo_close_required", "strategy_exit_failed"}
    if protection.get("requires_manual_attention") or protection_state in manual_states:
        return {
            "status": "failed",
            "stage": "strategy_manual_attention",
            "error": _strategy_execution_error_summary(strategy_orders, protection_state),
        }
    terminal_no_position = {
        "blocked_strategy_net_price_gate",
        "blocked_missing_backing",
        "blocked_no_option_legs",
        "skipped_insufficient_allocation",
        "skipped_untrusted_execution_quote",
    }
    if statuses and all(status in terminal_no_position for status in statuses):
        return {
            "status": "failed",
            "stage": "strategy_no_execution",
            "error": "strategy execution was blocked before any confirmed position was opened",
        }
    completed_or_open = any(
        status in {"submitted", "strategy_auto_exit_filled", "strategy_manual_exit_detected"}
        or status.endswith("_filled")
        for status in statuses
    )
    mixed_problem = any(status in {"failed", "strategy_auto_exit_failed", "residual_exit_failed"} or status.startswith(("blocked_", "skipped_")) for status in statuses)
    if completed_or_open and mixed_problem:
        return {
            "status": "succeeded",
            "stage": "strategy_partial_completed",
            "error": None,
        }
    if mixed_problem:
        return {
            "status": "failed",
            "stage": "strategy_partial_execution",
            "error": _strategy_execution_error_summary(strategy_orders, protection_state),
        }
    return {"status": "succeeded", "stage": "strategy_auto_execute", "error": None}


def _strategy_execution_error_summary(strategy_orders: list[dict[str, Any]], protection_state: str) -> str:
    parts: list[str] = []
    if protection_state:
        parts.append(f"protection_state={protection_state}")
    for order in strategy_orders:
        status = str(order.get("status") or "unknown")
        symbol = str(order.get("symbol") or order.get("tracking_id") or "strategy")
        error = str(order.get("error") or order.get("message") or "").strip()
        if status == "submitted" and not error:
            continue
        parts.append(f"{symbol}:{status}{f'({error})' if error else ''}")
    return "; ".join(parts)[:500] or "strategy execution requires review"


def _submit_one_strategy_order(
    position: dict[str, Any],
    config: dict[str, Any],
    account_name: str,
    committed: dict[str, Any] | None = None,
    *,
    run_id: str | None = None,
    owner_id: str = "local",
) -> dict[str, Any]:
    entry_order_type = _normalize_entry_order_type(config.get("entry_order_type"))
    exit_order_type = adaptive_pricing.normalize_order_type(config.get("exit_order_type"))
    strategy_unwind_on_failure = bool(config.get("strategy_unwind_on_failure", True))
    capital_required = _strategy_unit_capital_required(position)
    temporary_exposure_per_unit = _strategy_temporary_exposure_per_unit(position.get("legs") or [])
    risk_capital_per_unit = max(capital_required, temporary_exposure_per_unit)
    allocation_amount = float(config.get("total_capital") or 0) * float(position.get("allocation_pct") or 0)
    units = int(allocation_amount // risk_capital_per_unit) if allocation_amount > 0 and risk_capital_per_unit > 0 else 0
    max_units = _strategy_max_executable_units(position, account_name, committed)
    if max_units is not None:
        units = min(units, max_units)

    order_record = {
        "tracking_id": position.get("tracking_id"),
        "symbol": position.get("symbol"),
        "strategy_type": position.get("strategy_type"),
        "label": position.get("label"),
        "entry_order_type": entry_order_type,
        "exit_order_type": exit_order_type,
        "allocation_amount": round(allocation_amount, 2),
        "capital_required": round(capital_required, 2),
        "risk_capital_per_unit": round(risk_capital_per_unit, 2),
        "temporary_exposure_per_unit": round(temporary_exposure_per_unit, 2),
        "units": units,
        "quantity": units,
        "status": "pending",
        "risk_tracking_active": False,
        "legs": [],
        "strategy_entry_order_ids": [],
        "strategy_entry_status": "pending",
        "strategy_execution_mode": "auto",
        "risk_plan_source": position.get("risk_plan_source") or "system_default",
        "strategy_auto_execute": True,
        "market_data_source": config.get("market_data_source") or position.get("market_data_source") or "yfinance",
    }
    if units < 1:
        order_record["status"] = "skipped_insufficient_allocation"
        order_record["message"] = _strategy_units_block_message(position, max_units)
        return order_record

    if not _strategy_position_is_executable(position, account_name):
        order_record["status"] = "blocked_missing_backing"
        order_record["message"] = "strategy backing or buying power is not available"
        return order_record

    executable_legs = [
        _normalize_strategy_leg(leg)
        for leg in (position.get("legs") or [])
        if isinstance(leg, dict) and str(leg.get("side") or "").lower() != "stock"
    ]
    executable_legs = [leg for leg in executable_legs if leg.get("contract_symbol")]
    if not executable_legs:
        order_record["status"] = "blocked_no_option_legs"
        order_record["message"] = "strategy has no executable option legs"
        return order_record
    net_price_gate = _strategy_net_price_gate(position, executable_legs, account_name)
    order_record["strategy_net_price_gate"] = net_price_gate
    if not net_price_gate.get("passed"):
        order_record["status"] = "blocked_strategy_net_price_gate"
        order_record["strategy_entry_status"] = "blocked"
        order_record["message"] = net_price_gate.get("message") or "strategy net price gate blocked execution"
        return order_record
    executable_legs = list(net_price_gate.get("legs") or executable_legs)
    fresh_temporary_exposure = _strategy_temporary_exposure_per_unit(executable_legs)
    fresh_risk_capital = max(capital_required, fresh_temporary_exposure)
    fresh_units = int(allocation_amount // fresh_risk_capital) if allocation_amount > 0 and fresh_risk_capital > 0 else 0
    if fresh_units < units:
        units = fresh_units
        order_record["units"] = units
        order_record["quantity"] = units
    order_record["risk_capital_per_unit"] = round(fresh_risk_capital, 2)
    order_record["temporary_exposure_per_unit"] = round(fresh_temporary_exposure, 2)
    if units < 1:
        order_record["status"] = "skipped_insufficient_allocation"
        order_record["message"] = "allocation_amount cannot cover temporary sequential-leg exposure for one strategy unit"
        return order_record

    opened_orders: list[dict[str, Any]] = []
    try:
        execution_sequence = _strategy_execution_sequence(executable_legs)
        recheck_enabled = _strategy_recheck_between_legs_enabled() and len(execution_sequence) > 1
        for leg_index, leg in enumerate(execution_sequence):
            if recheck_enabled and leg_index > 0 and opened_orders:
                remaining = execution_sequence[leg_index:]
                recheck = _strategy_inter_leg_net_recheck(position, opened_orders, remaining, account_name)
                order_record.setdefault("strategy_inter_leg_rechecks", []).append(recheck)
                if recheck.get("issues"):
                    raise LongbridgeError(
                        "strategy net price breached tolerance after partial leg fills: "
                        + "; ".join(recheck["issues"])
                    )
            leg_record = _submit_strategy_leg_order(
                leg,
                units,
                entry_order_type,
                position,
                account_name,
                int(config.get("wait_for_fill_seconds") or 0),
                run_id=run_id,
                owner_id=owner_id,
            )
            order_record["legs"].append(leg_record)
            if leg_record.get("status") != "filled":
                if int(leg_record.get("filled_quantity") or 0) > 0:
                    opened_orders.append(leg_record)
                raise LongbridgeError(leg_record.get("error") or "strategy leg execution failed")
            opened_orders.append(leg_record)
        order_record["status"] = "submitted"
        order_record["strategy_entry_status"] = "submitted"
        order_record["risk_tracking_active"] = True
        order_record["strategy_entry_order_ids"] = [item.get("order_id") for item in opened_orders if item.get("order_id")]
        order_record["entry_filled_quantity"] = units
        order_record["actual_entry_at"] = utc_now()
        _apply_strategy_actual_entry_basis(position, order_record, opened_orders, config)
        order_record["message"] = "strategy legs submitted successfully"
        return order_record
    except Exception as exc:  # noqa: BLE001
        order_record["strategy_entry_status"] = "failed"
        order_record["error"] = str(exc)
        # Unwind BEFORE deciding residual state: a confirmed close annotates the
        # leg's exit fields, so a fully-unwound order tracks no residual instead
        # of being left stuck in strategy_residual_tracking / manual_attention.
        #
        # Which legs to unwind matters. `_strategy_execution_sequence` fills longs
        # FIRST, so a partial fill is (almost) always a defined-risk LONG option —
        # market-dumping it just pays the bid/ask spread a second time for a certain
        # loss and leaves no position (the reported bug: instant buy→sell close on a
        # single leg). A naked SHORT leg, by contrast, is unbounded risk and MUST be
        # closed. So by default we unwind only short residuals and HOLD longs as a
        # protected single-leg track. The old dump-everything behavior stays
        # available behind AI_OPTION_STRATEGY_UNWIND_LONG_LEGS=true.
        unwind: list[dict[str, Any]] = []
        if strategy_unwind_on_failure:
            if _strategy_unwind_long_legs_enabled():
                unwind_targets = opened_orders
            else:
                unwind_targets = [o for o in opened_orders if _strategy_leg_action(o.get("leg") or {}) == "sell"]
            if unwind_targets:
                unwind = _unwind_strategy_orders(unwind_targets, account_name, int(config.get("wait_for_fill_seconds") or 0))
                if unwind:
                    order_record["unwind"] = unwind
        residual_legs = _strategy_residual_legs(opened_orders)
        if residual_legs:
            _apply_entry_residual_tracking(order_record, position, config, residual_legs)
        elif any(item.get("confirmed") for item in unwind):
            # All filled legs were closed by unwind — flat at broker, no residual.
            order_record["status"] = "failed"
            order_record["residual_leg_tracking_active"] = False
            order_record["residual_legs"] = []
            order_record["residual_leg_quantity"] = 0
            order_record["strategy_exit_status"] = "filled"
            order_record["strategy_exit_reason"] = "unwind"
            order_record["entry_filled_quantity"] = max(
                (int(item.get("filled_quantity") or item.get("quantity") or 0) for item in opened_orders),
                default=0,
            )
            order_record["message"] = "strategy leg execution failed; filled legs unwound to flat"
            annotate_strategy_order_fill_ledger(order_record)
        else:
            order_record["status"] = "failed"
        return order_record


def _apply_entry_residual_tracking(
    order_record: dict[str, Any],
    position: dict[str, Any],
    config: dict[str, Any],
    residual_legs: list[dict[str, Any]],
) -> None:
    """Enrich an entry-path residual so the monitor can protect and self-heal it.

    A residual created here previously carried ONLY the ``residual_legs`` list,
    missing the top-level ``contract_symbol`` / ``order_symbol`` the monitor's
    ``_try_residual_position_reconcile`` needs to identify the contract — so it
    could never auto-clear when the position went flat, and no software stop was
    armed (the leg sat unprotected). Mirror the monitor's exit-failure routing:
    a single long residual becomes a protected single-leg track; anything with a
    short leg or multiple legs needs a broker combo close (manual).
    """
    order_record["residual_legs"] = residual_legs
    order_record["residual_leg_quantity"] = sum(int(item.get("filled_quantity") or 0) for item in residual_legs)
    order_record["entry_filled_quantity"] = max((int(item.get("filled_quantity") or 0) for item in residual_legs), default=0)
    order_record["residual_leg_tracking_active"] = True

    single_long = len(residual_legs) == 1 and str(residual_legs[0].get("action") or "").lower() == "buy"
    if not single_long:
        # Short or multi-leg residual: cannot be safely closed leg-by-leg.
        order_record["status"] = "broker_combo_close_required"
        order_record["broker_combo_close_required"] = True
        order_record["broker_combo_close_reason"] = "residual contains a short or multiple legs; close as a combo at the broker"
        order_record["message"] = "strategy leg execution failed; residual requires broker combo close"
        return

    leg = residual_legs[0]
    contract_symbol = str(leg.get("contract_symbol") or "").strip()
    order_symbol = str(leg.get("order_symbol") or (option_order_symbol(contract_symbol) if contract_symbol else "")).strip()
    quantity = int(leg.get("filled_quantity") or 0)
    entry_price = _coerce_float(leg.get("entry_price"), 0.0)
    stop_pct = max(1.0, min(_coerce_float(position.get("stop_loss_pct"), _coerce_float(config.get("default_stop_loss_pct"), 25.0)), 95.0))

    order_record["status"] = "strategy_residual_tracking"
    order_record["residual_leg_contract_symbol"] = contract_symbol
    order_record["residual_leg_order_symbol"] = order_symbol
    order_record["contract_symbol"] = contract_symbol
    order_record["order_symbol"] = order_symbol
    order_record["quantity"] = quantity
    order_record["entry_price"] = round(entry_price, 4) if entry_price > 0 else order_record.get("entry_price")
    order_record["actual_entry_price"] = round(entry_price, 4) if entry_price > 0 else order_record.get("actual_entry_price")
    order_record["stop_loss_pct"] = stop_pct
    order_record["stop_trigger_price"] = round(entry_price * (1 - stop_pct / 100), 2) if entry_price > 0 else order_record.get("stop_trigger_price")
    order_record["take_profit_pct"] = position.get("take_profit_pct", config.get("default_take_profit_pct"))
    order_record["message"] = "strategy leg execution failed; residual long leg switched to protected single-leg tracking"
    _arm_software_stop(order_record, quantity, config, "strategy_residual_long_leg_after_entry_failure")
    _arm_software_take_profit(order_record, quantity, config)
    annotate_strategy_order_fill_ledger(order_record)


def _strategy_residual_legs(opened_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    residuals: list[dict[str, Any]] = []
    for item in opened_orders:
        leg = _normalize_strategy_leg(item.get("leg") or {})
        quantity = int(item.get("filled_quantity") or item.get("quantity") or 0)
        # A leg that has been confirmed closed (e.g. by unwind) is no longer
        # residual — net it out so a fully-unwound order tracks nothing.
        if str(item.get("strategy_exit_status") or "").lower() == "filled":
            quantity -= int(item.get("strategy_exit_filled_quantity") or item.get("strategy_exit_quantity") or quantity)
        if quantity <= 0:
            continue
        residuals.append(
            {
                "contract_symbol": leg.get("contract_symbol"),
                "order_symbol": option_order_symbol(str(leg.get("contract_symbol") or "")) if leg.get("contract_symbol") else "",
                "action": leg.get("action"),
                "filled_quantity": quantity,
                "entry_price": item.get("entry_price") or leg.get("entry_price") or leg.get("price"),
                "order_id": item.get("order_id"),
            }
        )
    return residuals


def _strategy_position_is_executable(position: dict[str, Any], account_name: str) -> bool:
    if position.get("live_executable") is False:
        return False
    if position.get("family") in {"covered_call", "collar"}:
        return (_strategy_max_executable_units(position, account_name) or 0) >= 1
    if position.get("family") == "cash_secured_put":
        return (_strategy_max_executable_units(position, account_name) or 0) >= 1
    return True


def _apply_strategy_actual_entry_basis(
    position: dict[str, Any],
    order_record: dict[str, Any],
    opened_orders: list[dict[str, Any]],
    config: dict[str, Any],
) -> None:
    actual_legs: list[dict[str, Any]] = []
    actual_price_by_symbol: dict[str, float] = {}
    actual_source_by_symbol: dict[str, str] = {}
    any_executed_price = False
    for leg_record in opened_orders:
        leg = _normalize_strategy_leg(leg_record.get("leg") or {})
        planned_price = _coerce_float(leg_record.get("planned_entry_price"), _coerce_float(leg.get("planned_entry_price"), _coerce_float(leg.get("price"), 0.0)))
        actual_price = _coerce_float(leg_record.get("entry_price"), planned_price)
        if actual_price > 0:
            leg["price"] = round(actual_price, 4)
            leg["entry_price"] = round(actual_price, 4)
            leg["actual_entry_price"] = round(actual_price, 4)
            leg["planned_entry_price"] = round(planned_price, 4) if planned_price > 0 else planned_price
            leg["entry_price_source"] = leg_record.get("entry_price_source") or ("executed_price" if _coerce_float(leg_record.get("actual_entry_price"), 0.0) > 0 else "planned_price")
            actual_price_by_symbol[str(leg.get("contract_symbol") or "")] = round(actual_price, 4)
            actual_source_by_symbol[str(leg.get("contract_symbol") or "")] = str(leg["entry_price_source"])
            if leg["entry_price_source"] == "executed_price":
                any_executed_price = True
        actual_legs.append(leg)
        leg_record["leg"] = leg

    if not actual_legs:
        return
    if not any_executed_price:
        return

    merged_legs: list[dict[str, Any]] = []
    for raw_leg in position.get("legs") or []:
        if not isinstance(raw_leg, dict):
            continue
        if str(raw_leg.get("side") or "").lower() == "stock":
            merged_legs.append(raw_leg)
            continue
        leg = _normalize_strategy_leg(raw_leg)
        symbol = str(leg.get("contract_symbol") or "")
        actual_price = actual_price_by_symbol.get(symbol)
        if actual_price and actual_price > 0:
            planned_price = _coerce_float(leg.get("planned_entry_price"), _coerce_float(leg.get("price"), 0.0))
            leg["planned_entry_price"] = round(planned_price, 4) if planned_price > 0 else planned_price
            leg["price"] = actual_price
            leg["entry_price"] = actual_price
            leg["actual_entry_price"] = actual_price
            leg["entry_price_source"] = actual_source_by_symbol.get(symbol) or "planned_price"
        merged_legs.append(leg)
    position["legs"] = merged_legs or actual_legs

    actual_net = _strategy_net_from_legs(actual_legs)
    actual_net_debit = max(actual_net, 0.0)
    actual_net_credit = max(-actual_net, 0.0)
    actual_entry_mark = round(actual_net * 100, 2)
    position.setdefault("planned_entry_mark", position.get("entry_mark"))
    position.setdefault("planned_net_debit", position.get("net_debit"))
    position.setdefault("planned_net_credit", position.get("net_credit"))
    position["entry_mark"] = actual_entry_mark
    position["actual_entry_mark"] = actual_entry_mark
    position["net_debit"] = round(actual_net_debit, 4)
    position["net_credit"] = round(actual_net_credit, 4)
    position["actual_net_debit"] = round(actual_net_debit, 4)
    position["actual_net_credit"] = round(actual_net_credit, 4)
    position["entry_price_source"] = "executed_price" if any_executed_price else "planned_price"
    position["actual_entry_basis_adjusted"] = any_executed_price

    _reprice_strategy_risk_thresholds_from_actual_basis(position, config)
    order_record["actual_entry_mark"] = position.get("actual_entry_mark")
    order_record["actual_net_debit"] = position.get("actual_net_debit")
    order_record["actual_net_credit"] = position.get("actual_net_credit")
    order_record["entry_price_source"] = position.get("entry_price_source")
    order_record["stop_loss_pnl"] = position.get("stop_loss_pnl")
    order_record["take_profit_1_pnl"] = position.get("take_profit_1_pnl")
    order_record["take_profit_2_pnl"] = position.get("take_profit_2_pnl")


def _reprice_strategy_risk_thresholds_from_actual_basis(position: dict[str, Any], config: dict[str, Any]) -> None:
    actual_debit = _coerce_float(position.get("actual_net_debit"), _coerce_float(position.get("net_debit"), 0.0))
    actual_credit = _coerce_float(position.get("actual_net_credit"), _coerce_float(position.get("net_credit"), 0.0))
    width = _coerce_float(position.get("width"), 0.0)
    family = str(position.get("family") or "")
    strategy_type = str(position.get("strategy_type") or "")
    planned_max_loss = _coerce_float(position.get("max_loss"), 0.0)
    planned_max_profit = _coerce_float(position.get("max_profit"), 0.0)
    actual_max_loss = planned_max_loss
    actual_max_profit = planned_max_profit
    # bull_put/bear_call spreads built in the generic "spread" mode carry
    # family="spread" even though they are economically credit spreads, so the
    # family set alone misses them. Detect by strategy_type too — otherwise the
    # max-loss recompute below is skipped and risk_basis falls through to the
    # credit-received entry mark, doubling the effective stop (a credit spread's
    # risk is width-credit, never the credit it collected).
    is_credit_spread = (
        family in {"credit_spread", "iron_condor"}
        or strategy_type in {"bull_put_spread", "bear_call_spread", "iron_condor"}
    )

    if actual_debit > 0:
        actual_max_loss = actual_debit * 100
        if width > 0 and (family in {"spread", "butterfly"} or strategy_type in {"bull_call_spread", "bear_put_spread"}):
            actual_max_profit = max((width - actual_debit) * 100, 0.0)
    elif actual_credit > 0:
        actual_max_profit = actual_credit * 100
        if width > 0 and is_credit_spread:
            actual_max_loss = max((width - actual_credit) * 100, 0.0)

    if actual_debit > 0:
        risk_basis = max(actual_debit * 100, 1.0)
    elif actual_credit > 0 and is_credit_spread:
        # A credit spread's capital at risk is its max loss (width - credit), NOT
        # the credit received. Lock risk_basis to max loss and keep the credit
        # entry mark out of the max() so the stop stays anchored to real risk.
        risk_basis = max(actual_max_loss, 1.0)
    else:
        risk_basis = max(actual_max_loss, planned_max_loss, abs(_coerce_float(position.get("actual_entry_mark"), 0.0)), 1.0)
    if actual_credit > 0:
        profit_basis = max(actual_credit * 100, 1.0)
    elif actual_debit > 0:
        # Debit spreads measure take-profit % against the premium paid (capital
        # deployed), consistent with stop-loss and single-leg legs, so "20%
        # take-profit" means +20% on cost — not 20% of the theoretical max
        # profit (which would let a 20% setting ride to a far larger return).
        profit_basis = risk_basis
    else:
        profit_basis = max(actual_max_profit, planned_max_profit, risk_basis * 0.6, 1.0)

    stop_loss_pct = max(1.0, min(_coerce_float(position.get("stop_loss_pct"), _coerce_float(config.get("default_stop_loss_pct"), 25.0)), 95.0))
    take_profit_plan = _strategy_take_profit_pnls(profit_basis, config, position)
    position.setdefault("planned_stop_loss_pnl", position.get("stop_loss_pnl"))
    position.setdefault("planned_take_profit_1_pnl", position.get("take_profit_1_pnl"))
    position.setdefault("planned_take_profit_2_pnl", position.get("take_profit_2_pnl"))
    position.setdefault("planned_max_loss", position.get("max_loss"))
    position.setdefault("planned_max_profit", position.get("max_profit"))
    position["max_loss"] = round(actual_max_loss, 2) if actual_max_loss > 0 else position.get("max_loss")
    position["max_profit"] = round(actual_max_profit, 2) if actual_max_profit > 0 else position.get("max_profit")
    position["actual_risk_basis"] = round(risk_basis, 2)
    position["actual_profit_basis"] = round(profit_basis, 2)
    position["stop_loss_pnl"] = round(-risk_basis * stop_loss_pct / 100, 2)
    position["tiered_take_profit_enabled"] = take_profit_plan["tiered_take_profit_enabled"]
    position["take_profit_pct"] = take_profit_plan["take_profit_pct"]
    position["take_profit_1_pct"] = take_profit_plan["take_profit_1_pct"]
    position["take_profit_2_pct"] = take_profit_plan["take_profit_2_pct"]
    position["take_profit_1_pnl"] = take_profit_plan["take_profit_1_pnl"]
    position["take_profit_2_pnl"] = take_profit_plan["take_profit_2_pnl"]
    if isinstance(position.get("ai_risk_plan"), dict):
        position["ai_risk_plan"] = {
            **position["ai_risk_plan"],
            "planned_stop_loss_pnl": position.get("planned_stop_loss_pnl"),
            "planned_take_profit_1_pnl": position.get("planned_take_profit_1_pnl"),
            "planned_take_profit_2_pnl": position.get("planned_take_profit_2_pnl"),
            "tiered_take_profit_enabled": position["tiered_take_profit_enabled"],
            "take_profit_pct": position["take_profit_pct"],
            "take_profit_1_pct": position["take_profit_1_pct"],
            "take_profit_2_pct": position["take_profit_2_pct"],
            "stop_loss_pnl": position["stop_loss_pnl"],
            "take_profit_1_pnl": position["take_profit_1_pnl"],
            "take_profit_2_pnl": position["take_profit_2_pnl"],
            "actual_entry_basis_adjusted": position.get("actual_entry_basis_adjusted", False),
            "actual_entry_mark": position.get("actual_entry_mark"),
            "actual_net_debit": position.get("actual_net_debit"),
            "actual_net_credit": position.get("actual_net_credit"),
        }


def _strategy_unit_capital_required(position: dict[str, Any]) -> float:
    family = str(position.get("family") or "")
    net_debit_required = max(_coerce_float(position.get("net_debit"), 0.0) * 100, _coerce_float(position.get("entry_mark"), 0.0), 0.0)
    if family in {"covered_call", "collar"}:
        return max(net_debit_required, 1.0)
    return max(
        _coerce_float(position.get("capital_required"), 0.0),
        _coerce_float(position.get("max_loss"), 0.0),
        net_debit_required,
        1.0,
    )


def _strategy_temporary_exposure_per_unit(legs: list[dict[str, Any]]) -> float:
    """Maximum premium at risk while buy legs are filled before hedge legs.

    Multi-leg orders are submitted buy legs first to avoid naked shorts. Until
    the sell legs fill, every bought option can lose its full premium, so sizing
    only from the final net debit materially understates the live path risk.
    """
    exposure = 0.0
    for raw in legs or []:
        if not isinstance(raw, dict):
            continue
        leg = _normalize_strategy_leg(raw)
        if str(leg.get("action") or "").lower() != "buy" or str(leg.get("side") or "").lower() == "stock":
            continue
        exposure += _coerce_float(leg.get("price"), 0.0) * max(1, int(_coerce_float(leg.get("qty"), 1))) * 100
    return round(exposure, 2)


def _strategy_max_executable_units(position: dict[str, Any], account_name: str, committed: dict[str, Any] | None = None) -> int | None:
    # `committed` accumulates capital/stock already reserved by earlier positions
    # in the SAME run so the shared buy-power pool (cash-secured puts) and stock
    # backing (covered calls / collars) are not double-counted across positions.
    committed = committed if isinstance(committed, dict) else {}
    family = str(position.get("family") or "")
    if family in {"covered_call", "collar"}:
        symbol = str(position.get("symbol") or "").strip().upper()
        used = int((committed.get("stock") or {}).get(symbol, 0))
        return max(0, (_strategy_stock_quantity(position, account_name) - used) // 100)
    if family == "cash_secured_put":
        required = max(_coerce_float(position.get("capital_required"), 0.0), _strategy_unit_capital_required(position), 1.0)
        available = _buy_power_for_account(account_name) - float(committed.get("buy_power") or 0.0)
        return int(max(0.0, available) // required)
    return None


def _strategy_units_block_message(position: dict[str, Any], max_units: int | None) -> str:
    family = str(position.get("family") or "")
    if max_units is not None and max_units < 1:
        if family in {"covered_call", "collar"}:
            return "stock backing is not enough for one strategy unit"
        if family == "cash_secured_put":
            return "buying power is not enough for one cash-secured put unit"
    return "allocation_amount is not enough for one strategy unit"


def _strategy_stock_quantity(position: dict[str, Any], account_name: str) -> int:
    symbol = str(position.get("symbol") or "").strip().upper()
    if not symbol:
        return 0
    rows = lb_positions(account_name)
    target = f"{symbol}.US"
    qty = 0
    for row in rows:
        row_symbol = str(row.get("symbol") or row.get("order_symbol") or "").strip().upper()
        if row_symbol == target or row_symbol == symbol:
            try:
                qty += int(float(row.get("available_quantity") or row.get("quantity") or 0))
            except (TypeError, ValueError):
                continue
    return qty


def _strategy_execution_sequence(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buys = [leg for leg in legs if str(leg.get("action") or "").lower() == "buy"]
    sells = [leg for leg in legs if str(leg.get("action") or "").lower() == "sell"]
    return buys + sells


def _strategy_net_price_gate(position: dict[str, Any], legs: list[dict[str, Any]], account_name: str) -> dict[str, Any]:
    tolerance_pct = _strategy_net_price_tolerance_pct()
    expected_net = _strategy_expected_net_price(position, legs)
    refreshed_legs: list[dict[str, Any]] = []
    quote_errors: list[str] = []
    for leg in legs:
        row = dict(leg)
        contract_symbol = str(row.get("contract_symbol") or "").strip()
        if not contract_symbol:
            quote_errors.append("missing contract symbol")
            refreshed_legs.append(row)
            continue
        try:
            quote_row = quote_option_contract(contract_symbol, account_name)
        except Exception as exc:  # noqa: BLE001
            quote_errors.append(f"{contract_symbol}: {exc}")
            refreshed_legs.append(row)
            continue
        if not _execution_quote_is_trusted(quote_row):
            quote_errors.append(
                f"{contract_symbol}: untrusted execution quote source {quote_row.get('source') or 'unknown'}"
            )
            row["fresh_quote"] = quote_row
            refreshed_legs.append(row)
            continue
        action = str(row.get("action") or "").lower()
        price = _strategy_quote_leg_price(quote_row, action)
        row["fresh_quote"] = quote_row
        row["candidate_price"] = _coerce_float(row.get("price"), 0.0)
        if price <= 0:
            quote_errors.append(f"{contract_symbol}: refreshed quote missing usable {'ask' if action == 'buy' else 'bid'}")
        else:
            row["price"] = round(price, 2)
            row["price_source"] = "fresh_ask" if action == "buy" else "fresh_bid"
        refreshed_legs.append(row)
    if quote_errors:
        return {
            "passed": False,
            "message": "fresh strategy leg quote unavailable",
            "issues": quote_errors[:10],
            "expected_net": round(expected_net, 4),
            "legs": refreshed_legs,
        }

    actual_net = _strategy_net_from_legs(refreshed_legs)
    issues = _strategy_net_price_issues(position, expected_net, actual_net, tolerance_pct)
    return {
        "passed": not issues,
        "message": "strategy net price accepted" if not issues else "; ".join(issues),
        "issues": issues,
        "expected_net": round(expected_net, 4),
        "actual_net": round(actual_net, 4),
        "expected_debit": round(max(expected_net, 0.0), 4),
        "actual_debit": round(max(actual_net, 0.0), 4),
        "expected_credit": round(max(-expected_net, 0.0), 4),
        "actual_credit": round(max(-actual_net, 0.0), 4),
        "tolerance_pct": tolerance_pct,
        "width": round(_coerce_float(position.get("width"), 0.0), 4),
        "legs": refreshed_legs,
    }


def _strategy_quote_leg_price(quote_row: dict[str, Any], action: str) -> float:
    if not quote_row.get("available"):
        return 0.0
    keys = ("ask", "limit_price", "last_price", "last") if action == "buy" else ("bid", "last_price", "last", "limit_price")
    for key in keys:
        price = _coerce_float(quote_row.get(key), 0.0)
        if price > 0:
            return price
    return 0.0


def _execution_quote_is_trusted(quote_row: dict[str, Any]) -> bool:
    source = str(quote_row.get("source") or "").strip().lower()
    return bool(quote_row.get("available")) and source != "yfinance" and quote_row.get("execution_trusted") is not False


def _max_quote_spread_pct() -> float:
    # A remaining leg's fresh quote is treated as untrustworthy (a bad/one-sided
    # tick, not a real move) when its bid-ask spread exceeds this fraction of its
    # own mid. Deliberately generous so genuinely wide-but-real illiquid quotes
    # still count; only absurd/broken quotes are rejected.
    return max(10.0, min(_coerce_float(os.getenv("AI_OPTION_STRATEGY_MAX_QUOTE_SPREAD_PCT"), 100.0), 500.0))


def _strategy_quote_is_trustworthy(quote_row: dict[str, Any]) -> bool:
    """True when both sides of the fresh quote look internally consistent.

    Rejects the bad ticks that drove the instant-loss unwinds (prod META/TSLA:
    a bid that collapsed to a fraction of mid while the ask stayed put): a
    non-positive bid or ask, a crossed book (bid > ask), or a spread wider than
    ``_max_quote_spread_pct`` of mid. An untrustworthy quote must NOT be used to
    declare a net-price breach — the caller records it as a quote error instead,
    which suppresses the breach (a transient bad tick shouldn't strand a combo)."""
    if not quote_row.get("available"):
        return False
    bid = _coerce_float(quote_row.get("bid"), 0.0)
    ask = _coerce_float(quote_row.get("ask"), 0.0)
    if bid <= 0 or ask <= 0:
        return False
    if bid > ask:
        return False
    mid = (bid + ask) / 2
    if mid <= 0:
        return False
    return (ask - bid) <= mid * (_max_quote_spread_pct() / 100)


def _strategy_quote_leg_mid_price(quote_row: dict[str, Any], action: str) -> float:
    """Mid price (bid+ask)/2 for a still-unfilled recheck leg.

    The front gate is intentionally conservative (buy@ask / sell@bid) because it
    decides whether to open at all. The inter-leg recheck answers a different
    question — "is the combo still economically sound?" — so it should value the
    remaining leg at mid, not bake in the full half-spread we would only pay if we
    crossed the market right now. Falls back to the conservative single-sided
    price when mid is unavailable."""
    if not quote_row.get("available"):
        return 0.0
    bid = _coerce_float(quote_row.get("bid"), 0.0)
    ask = _coerce_float(quote_row.get("ask"), 0.0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return _strategy_quote_leg_price(quote_row, action)


# --- Adaptive ("smart") limit orders -------------------------------------
# An adaptive order is a limit order whose price sits BETWEEN the mid and the
# opposite touch: buy at mid + aggr*half_spread, sell at mid - aggr*half_spread
# (aggr in [0, 1]; 0 = passive mid, 1 = marketable at ask/bid). It saves the
# half-spread the plain "limit" path always pays, while the reprice loop walks
# aggr toward 1.0 so a resting order still crosses before we give up.
#
# The pricing math lives in the standalone ``adaptive_pricing`` module so the
# entry path (here) and the exit path (trading_monitor) share ONE source of
# truth without a cross-module dependency. These thin wrappers keep the private
# ``_``-prefixed names the entry call sites and tests already use.
_adaptive_order_enabled = adaptive_pricing.adaptive_order_enabled
_adaptive_aggr_start = adaptive_pricing.adaptive_aggr_start
_adaptive_aggr_for_attempt = adaptive_pricing.adaptive_aggr_for_attempt
_round_to_tick = adaptive_pricing.round_to_tick
_adaptive_limit_price = adaptive_pricing.adaptive_limit_price


def _strategy_expected_net_price(position: dict[str, Any], legs: list[dict[str, Any]]) -> float:
    net_debit = _coerce_float(position.get("net_debit"), 0.0)
    net_credit = _coerce_float(position.get("net_credit"), 0.0)
    if net_debit > 0:
        return net_debit
    if net_credit > 0:
        return -net_credit
    entry_mark = _coerce_float(position.get("entry_mark"), 0.0)
    if entry_mark:
        return entry_mark / 100
    return _strategy_net_from_legs(legs)


def _strategy_net_from_legs(legs: list[dict[str, Any]]) -> float:
    net = 0.0
    for leg in legs:
        action = str(leg.get("action") or "").lower()
        qty = max(1, int(_coerce_float(leg.get("qty"), 1)))
        price = _coerce_float(leg.get("price"), 0.0)
        signed = price * qty
        net += signed if action == "buy" else -signed
    return net


def _strategy_net_price_issues(position: dict[str, Any], expected_net: float, actual_net: float, tolerance_pct: float) -> list[str]:
    issues: list[str] = []
    family = str(position.get("family") or "")
    strategy_type = str(position.get("strategy_type") or "")
    width = _coerce_float(position.get("width"), 0.0)
    expected_debit = max(expected_net, 0.0)
    actual_debit = max(actual_net, 0.0)
    expected_credit = max(-expected_net, 0.0)
    actual_credit = max(-actual_net, 0.0)
    tolerance = max(tolerance_pct, 0.0) / 100

    if expected_debit > 0:
        max_debit = expected_debit * (1 + tolerance)
        if actual_debit <= 0:
            issues.append("net_price_flipped_from_debit_to_credit_or_zero")
        elif actual_debit > max(max_debit, expected_debit + 0.05):
            issues.append(f"net_debit_worse_than_tolerance actual={actual_debit:.2f} expected={expected_debit:.2f}")
    if expected_credit > 0:
        min_credit = expected_credit * (1 - tolerance)
        if actual_credit <= 0:
            issues.append("net_price_flipped_from_credit_to_debit_or_zero")
        elif actual_credit < min(max(min_credit, 0.01), expected_credit - 0.05):
            issues.append(f"net_credit_worse_than_tolerance actual={actual_credit:.2f} expected={expected_credit:.2f}")

    is_credit = expected_credit > 0 or family in {"credit_spread", "cash_secured_put", "covered_call", "iron_condor"} or strategy_type in {"bull_put_spread", "bear_call_spread"}
    is_debit = expected_debit > 0 or family in {"straddle", "strangle", "calendar", "diagonal", "poor_mans_covered_call", "butterfly"} or strategy_type in {"bull_call_spread", "bear_put_spread"}
    if is_credit:
        min_credit = _strategy_min_credit(position)
        if actual_credit < min_credit:
            issues.append(f"net_credit_below_minimum actual={actual_credit:.2f} minimum={min_credit:.2f}")
    if is_debit:
        max_debit = _strategy_max_debit(position, expected_debit)
        if max_debit and actual_debit > max_debit:
            issues.append(f"net_debit_above_maximum actual={actual_debit:.2f} maximum={max_debit:.2f}")

    if family == "iron_condor" and width > 0:
        if actual_credit <= 0 or actual_credit >= width:
            issues.append(f"iron_condor_credit_out_of_width actual={actual_credit:.2f} width={width:.2f}")
        elif actual_credit < max(0.05, width * 0.08):
            issues.append(f"iron_condor_credit_too_small actual={actual_credit:.2f} width={width:.2f}")
    if family == "butterfly" and width > 0:
        if actual_debit <= 0 or actual_debit >= width:
            issues.append(f"butterfly_debit_out_of_width actual={actual_debit:.2f} width={width:.2f}")
        elif actual_debit > width * 0.65:
            issues.append(f"butterfly_debit_too_expensive actual={actual_debit:.2f} width={width:.2f}")
    return list(dict.fromkeys(issues))


def _strategy_min_credit(position: dict[str, Any]) -> float:
    width = _coerce_float(position.get("width"), 0.0)
    family = str(position.get("family") or "")
    if family in {"credit_spread", "iron_condor"} or str(position.get("strategy_type") or "") in {"bull_put_spread", "bear_call_spread"}:
        return round(max(0.05, width * 0.08), 4) if width > 0 else 0.05
    return 0.05


def _strategy_max_debit(position: dict[str, Any], expected_debit: float) -> float:
    width = _coerce_float(position.get("width"), 0.0)
    family = str(position.get("family") or "")
    if family == "butterfly" and width > 0:
        return round(width * 0.65, 4)
    if width > 0 and (family == "spread" or str(position.get("strategy_type") or "") in {"bull_call_spread", "bear_put_spread"}):
        return round(width * 0.95, 4)
    if expected_debit > 0:
        return round(expected_debit * (1 + _strategy_net_price_tolerance_pct() / 100), 4)
    return 0.0


def _strategy_net_price_tolerance_pct() -> float:
    return max(1.0, min(_coerce_float(os.getenv("AI_OPTION_STRATEGY_NET_PRICE_TOLERANCE_PCT"), 15.0), 50.0))


def _strategy_recheck_between_legs_enabled() -> bool:
    return (os.getenv("AI_OPTION_STRATEGY_RECHECK_NET_BETWEEN_LEGS", "true") or "").strip().lower() in {"1", "true", "yes", "on"}


def _strategy_unwind_long_legs_enabled() -> bool:
    # Legacy behavior: market-close a filled defined-risk LONG leg on entry
    # failure. Off by default — a long residual is now held as a protected
    # single-leg track instead of eating the bid/ask spread for a certain loss.
    # Naked SHORT residuals are always unwound regardless of this flag.
    return (os.getenv("AI_OPTION_STRATEGY_UNWIND_LONG_LEGS", "false") or "").strip().lower() in {"1", "true", "yes", "on"}


def _strategy_inter_leg_net_recheck(
    position: dict[str, Any],
    filled_leg_records: list[dict[str, Any]],
    remaining_legs: list[dict[str, Any]],
    account_name: str,
) -> dict[str, Any]:
    """Re-check the combined net price mid-execution, after one or more legs have
    filled but before the next leg is submitted.

    The up-front gate only sees pre-trade quotes. Once a leg fills, the market can
    move against the still-open legs and turn a vetted combo into a bad one (legging
    risk). This recombines the ACTUAL fill prices of already-filled legs with FRESH
    quotes for the legs not yet submitted, and runs the same tolerance issues check.

    Quote-unavailable on a remaining leg is recorded but does NOT itself flag a
    breach: a transient quote gap should not strand an in-progress combo into
    residual tracking. Only a computable net that breaches tolerance flags `issues`.
    """
    expected_net = _strategy_expected_net_price(position, remaining_legs)
    combined: list[dict[str, Any]] = []
    quote_errors: list[str] = []
    for record in filled_leg_records:
        leg = _normalize_strategy_leg(record.get("leg") or {})
        # leg["price"] was rewritten to the executed fill price on a filled leg.
        combined.append(leg)
    for leg in remaining_legs:
        row = _normalize_strategy_leg(leg)
        contract_symbol = str(row.get("contract_symbol") or "").strip()
        action = str(row.get("action") or "").lower()
        try:
            quote_row = quote_option_contract(contract_symbol, account_name) if contract_symbol else {}
        except Exception as exc:  # noqa: BLE001
            quote_errors.append(f"{contract_symbol}: {exc}")
            quote_row = {}
        # A bad / one-sided tick (crossed or absurdly wide book) must not be
        # allowed to declare a breach — record it as a quote error, which
        # suppresses the breach and holds the in-progress combo instead of
        # unwinding on garbage data. Real quotes are valued at MID here, not the
        # spread-crossing side, so a normal half-spread never trips the gate.
        if quote_row and not _strategy_quote_is_trustworthy(quote_row):
            quote_errors.append(f"{contract_symbol}: untrusted quote (bid/ask crossed, one-sided, or spread too wide)")
            price = 0.0
        else:
            price = _strategy_quote_leg_mid_price(quote_row, action)
        if price <= 0:
            quote_errors.append(f"{contract_symbol}: refreshed quote missing usable mid")
        else:
            row["price"] = round(price, 2)
        combined.append(row)
    actual_net = _strategy_net_from_legs(combined)
    issues = _strategy_net_price_issues(position, expected_net, actual_net, _strategy_net_price_tolerance_pct()) if not quote_errors else []
    return {
        "expected_net": round(expected_net, 4),
        "actual_net": round(actual_net, 4),
        "issues": issues,
        "quote_errors": quote_errors[:10],
    }


def _normalize_strategy_leg(leg: dict[str, Any]) -> dict[str, Any]:
    row = dict(leg)
    row["contract_symbol"] = _strategy_leg_contract_symbol(row)
    row["action"] = _strategy_leg_action(row)
    row["qty"] = _strategy_leg_quantity(row)
    row["price"] = _strategy_leg_price(row, row["action"])
    return row


def _strategy_leg_contract_symbol(leg: dict[str, Any]) -> str:
    for key in ("contract_symbol", "option_symbol", "symbol", "order_symbol"):
        value = str(leg.get(key) or "").strip()
        if value:
            return value
    return ""


def _strategy_leg_action(leg: dict[str, Any]) -> str:
    action = str(leg.get("action") or "").strip().lower()
    if action in {"buy", "sell"}:
        return action
    side = str(leg.get("side") or leg.get("position_side") or "").strip().lower()
    if side in {"long", "buy", "bto", "buy_to_open"}:
        return "buy"
    if side in {"short", "sell", "sto", "sell_to_open"}:
        return "sell"
    role = str(leg.get("role") or "").strip().lower()
    if role.startswith("long"):
        return "buy"
    if role.startswith("short"):
        return "sell"
    return action


def _strategy_leg_quantity(leg: dict[str, Any]) -> int:
    for key in ("qty", "quantity", "ratio", "contracts"):
        value = _coerce_float(leg.get(key), 0.0)
        if value > 0:
            return max(1, int(value))
    return 1


def _strategy_leg_price(leg: dict[str, Any], action: str) -> float:
    for key in ("price", "limit_price", "entry_price"):
        value = _coerce_float(leg.get(key), 0.0)
        if value > 0:
            return value
    fallback_key = "ask" if action == "buy" else "bid"
    return _coerce_float(leg.get(fallback_key), 0.0)


def _entry_reprice_attempts() -> int:
    return env_int("AI_OPTION_ENTRY_REPRICE_ATTEMPTS", 1, 0, 5)


def _cancel_order_safely(order_id: str | None, account_name: str | None) -> dict[str, Any] | None:
    if not order_id:
        return None
    try:
        return cancel_order(order_id, account_name)
    except Exception as exc:  # noqa: BLE001
        return {"order_id": order_id, "cancel_failed": True, "error": str(exc)}


def _reconcile_recent_strategy_submission(client_key: str, account_name: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    rows = find_recent_order_journal(client_key, within_seconds=900)
    if not rows:
        return None
    latest_after = next((row for row in rows if row.get("phase") == "after" and row.get("order_id")), None)
    latest_before = next((row for row in rows if row.get("phase") == "before"), None)
    if latest_before and (not latest_after or str(latest_before.get("created_at") or "") > str(latest_after.get("created_at") or "")):
        raise LongbridgeError("idempotency journal contains an unresolved pre-submit record; refusing a duplicate strategy order")
    if not latest_after:
        return None
    order_id = str(latest_after.get("order_id") or "")
    try:
        detail = order_detail(order_id, account_name)
    except Exception as exc:  # noqa: BLE001 - unknown broker state must fail closed.
        raise LongbridgeError(f"cannot reconcile prior strategy order {order_id}: {exc}") from exc
    status = str(_order_status(detail) or "").lower()
    if _is_terminal_unfilled_status(status) and _filled_quantity(detail) <= 0:
        return None
    return {"order_id": order_id, "status": status, "reused_from_journal": True}, detail


def _reprice_leg_from_quote(contract_symbol: str, action: str, account_name: str | None) -> tuple[float, dict[str, Any]]:
    if not contract_symbol:
        return 0.0, {"error": "missing contract symbol"}
    try:
        quote_row = quote_option_contract(contract_symbol, account_name)
    except Exception as exc:  # noqa: BLE001
        return 0.0, {"error": str(exc)}
    price = _strategy_quote_leg_price(quote_row, action)
    return price, quote_row


def _submit_strategy_leg_order(
    leg: dict[str, Any],
    units: int,
    entry_order_type: str,
    position: dict[str, Any],
    account_name: str,
    wait_for_fill_seconds: int,
    *,
    run_id: str | None = None,
    owner_id: str = "local",
) -> dict[str, Any]:
    leg = _normalize_strategy_leg(leg)
    contract_symbol = str(leg.get("contract_symbol") or "").strip()
    action = str(leg.get("action") or "").strip().lower()
    leg_qty = max(1, int(_coerce_float(leg.get("qty"), 1)))
    quantity = int(leg_qty * max(1, units))
    price = _coerce_float(leg.get("price"), 0.0)
    planned_price = price
    order_symbol = option_order_symbol(contract_symbol) if contract_symbol else ""
    if not order_symbol:
        return {"status": "failed", "error": "missing contract symbol", "leg": leg}
    if action not in {"buy", "sell"}:
        return {"status": "failed", "error": f"unsupported leg action `{action}`", "leg": leg}
    remark_base = f"AI_STRATEGY {position.get('strategy_type')} {position.get('tracking_id')} {action}".strip()
    is_limit = entry_order_type in {"limit", "adaptive"}
    is_adaptive = entry_order_type == "adaptive" and _adaptive_order_enabled()
    broker_order_type = "limit" if is_limit else "market"
    max_attempts = 1 + (_entry_reprice_attempts() if is_limit else 0)
    fill_wait = max(1, min(int(wait_for_fill_seconds or 8), 60))
    # Adaptive opens between mid and the touch. The net-price gate already
    # stamped a fresh quote onto the leg, so we can price off it without an
    # extra requote; fall back to the gate's conservative price if the flag is
    # off or the quote is unusable (adaptive is never worse than plain limit).
    if is_adaptive:
        gate_quote = leg.get("fresh_quote") if isinstance(leg.get("fresh_quote"), dict) else {}
        adaptive_open = _adaptive_limit_price(gate_quote, action, _adaptive_aggr_for_attempt(0, max_attempts)) if gate_quote else 0.0
        if adaptive_open > 0:
            price = adaptive_open

    attempts: list[dict[str, Any]] = []
    last_entry: dict[str, Any] = {}
    last_detail: dict[str, Any] = {}
    last_order_id: str | None = None
    last_status = ""
    filled_quantity = 0
    actual_entry_price = 0.0
    reprice_error: str | None = None

    try:
        journal_run_id = str(run_id or position.get("instance_id") or position.get("tracking_id") or "")
        cok = client_order_key(journal_run_id, f"{contract_symbol}:{action}", "strategy_entry") if _idempotency_enabled() and run_id else ""
        cok_tag = f" [cok:{cok}]" if cok else ""
        for attempt_idx in range(max_attempts):
            attempt_remark = (remark_base if attempt_idx == 0 else f"{remark_base} reprice{attempt_idx}@{price:.2f}") + cok_tag
            submit_fn = submit_buy_order if action == "buy" else submit_sell_order
            reconciled = _reconcile_recent_strategy_submission(cok, account_name) if cok else None
            if reconciled:
                entry, detail = reconciled
            else:
                record_order_journal(
                    owner_id=owner_id, run_id=run_id, client_order_key=cok or None,
                    action=f"strategy_{action}", phase="before",
                    account_ref=account_name, symbol=order_symbol, side=action, quantity=quantity,
                    price=price if is_limit else None,
                    detail={"tracking_id": position.get("tracking_id"), "strategy_type": position.get("strategy_type"), "attempt": attempt_idx + 1},
                )
                entry = submit_fn(
                    order_symbol,
                    quantity,
                    price if is_limit else None,
                    account_name,
                    attempt_remark,
                    order_type=broker_order_type,
                )
                detail = {}
            last_entry = entry
            order_id = _order_id(entry)
            record_order_journal(
                owner_id=owner_id, run_id=run_id, client_order_key=cok or None,
                action=f"strategy_{action}", phase="after",
                account_ref=account_name, symbol=order_symbol, side=action, quantity=quantity,
                order_id=order_id, status=str(_order_status(entry) or ""),
            )
            last_order_id = order_id
            detail = detail or (wait_for_order_fill(order_id, account_name, fill_wait) if order_id else {})
            last_detail = detail
            filled_quantity = _filled_quantity(detail)
            last_status = str(_order_status(detail or entry) or "").lower()
            actual_entry_price = _executed_price(detail or entry)
            record_order_journal(
                owner_id=owner_id, run_id=run_id, client_order_key=cok or None,
                action=f"strategy_{action}", phase="fill",
                account_ref=account_name, symbol=order_symbol, side=action, quantity=quantity,
                order_id=order_id, status=last_status,
                detail={"filled_quantity": filled_quantity, "executed_price": actual_entry_price},
            )
            attempt_record = {
                "attempt": attempt_idx + 1,
                "order_id": order_id,
                "submitted_price": round(price, 4) if price > 0 else 0,
                "order_type": entry_order_type,
                "status": last_status,
                "filled_quantity": filled_quantity,
            }
            attempts.append(attempt_record)
            if filled_quantity > 0 or _status_is_filled(last_status):
                break
            cancel_result = _cancel_order_safely(order_id, account_name)
            if cancel_result is not None:
                attempt_record["cancel"] = cancel_result
                record_order_journal(
                    owner_id=owner_id, run_id=run_id, client_order_key=cok or None,
                    action=f"strategy_{action}", phase="cancel",
                    account_ref=account_name, symbol=order_symbol, side=action, quantity=quantity,
                    order_id=order_id, status="cancel_requested", detail=cancel_result,
                )
            if not is_limit or attempt_idx + 1 >= max_attempts:
                break
            new_price, fresh_quote = _reprice_leg_from_quote(contract_symbol, action, account_name)
            attempt_record["reprice_quote"] = fresh_quote
            if new_price <= 0:
                reprice_error = "reprice quote unavailable"
                break
            if is_adaptive:
                walked = _adaptive_limit_price(fresh_quote, action, _adaptive_aggr_for_attempt(attempt_idx + 1, max_attempts))
                price = walked if walked > 0 else round(new_price, 2)
            else:
                price = round(new_price, 2)
        if actual_entry_price > 0:
            leg["planned_entry_price"] = round(planned_price, 4) if planned_price > 0 else planned_price
            leg["price"] = round(actual_entry_price, 4)
            leg["entry_price"] = round(actual_entry_price, 4)
            leg["actual_entry_price"] = round(actual_entry_price, 4)
            leg["entry_price_source"] = "executed_price"
        entry_price = actual_entry_price if actual_entry_price > 0 else planned_price
        if filled_quantity <= 0 and not _status_is_filled(last_status):
            return {
                "status": "unfilled",
                "order_id": last_order_id,
                "entry_order": last_entry,
                "entry_detail": last_detail,
                "leg": leg,
                "quantity": quantity,
                "filled_quantity": filled_quantity,
                "planned_entry_price": planned_price,
                "entry_price": entry_price,
                "entry_price_source": "executed_price" if actual_entry_price > 0 else "planned_price",
                "entry_attempts": attempts,
                "error": reprice_error or "leg not confirmed filled",
            }
        if filled_quantity and filled_quantity < quantity:
            return {
                "status": "partial_fill",
                "order_id": last_order_id,
                "entry_order": last_entry,
                "entry_detail": last_detail,
                "leg": leg,
                "quantity": quantity,
                "filled_quantity": filled_quantity,
                "planned_entry_price": planned_price,
                "entry_price": entry_price,
                "actual_entry_price": actual_entry_price,
                "entry_price_source": "executed_price" if actual_entry_price > 0 else "planned_price",
                "entry_attempts": attempts,
                "error": f"leg partially filled {filled_quantity}/{quantity}",
            }
        return {
            "status": "filled",
            "order_id": last_order_id,
            "entry_order": last_entry,
            "entry_detail": last_detail,
            "leg": leg,
            "quantity": quantity,
            "filled_quantity": filled_quantity,
            "planned_entry_price": planned_price,
            "entry_price": entry_price,
            "actual_entry_price": actual_entry_price,
            "entry_price_source": "executed_price" if actual_entry_price > 0 else "planned_price",
            "entry_attempts": attempts,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "error": str(exc),
            "leg": leg,
            "quantity": quantity,
            "entry_attempts": attempts,
        }



def _unwind_strategy_orders(opened_orders: list[dict[str, Any]], account_name: str, fill_wait_seconds: int = 0) -> list[dict[str, Any]]:
    unwind_results: list[dict[str, Any]] = []
    for order in reversed(opened_orders):
        leg = _normalize_strategy_leg(order.get("leg") or {})
        contract_symbol = str(leg.get("contract_symbol") or "").strip()
        action = str(leg.get("action") or "").strip().lower()
        qty = int(order.get("filled_quantity") or order.get("quantity") or 0)
        if not contract_symbol or qty < 1:
            continue
        exit_side = "sell" if action == "buy" else "buy"
        try:
            submit_fn = submit_sell_order if exit_side == "sell" else submit_buy_order
            close = submit_fn(option_order_symbol(contract_symbol), qty, None, account_name, f"AI_STRATEGY_UNWIND {contract_symbol}", order_type="market")
            close_order_id = _order_id(close)
            detail = wait_for_order_fill(close_order_id, account_name, max(1, min(int(fill_wait_seconds or 8), 60))) if close_order_id else {}
            exit_filled = _filled_quantity(detail)
            exit_price = _executed_price(detail or close)
            exit_status = str(_order_status(detail or close) or "").lower()
            confirmed = exit_filled >= qty or _status_is_filled(exit_status)
            result = {
                "contract_symbol": contract_symbol,
                "side": exit_side,
                "quantity": qty,
                "order": close,
                "order_id": close_order_id,
                "exit_detail": detail,
                "exit_filled_quantity": exit_filled,
                "exit_price": round(exit_price, 4) if exit_price > 0 else 0,
                "exit_status": exit_status,
                "confirmed": confirmed,
            }
            # Annotate the leg record in place so the fill ledger / residual
            # derivation see this close. opened_orders entries are the SAME dicts
            # held in order_record["legs"], so this propagates to the order.
            if confirmed:
                filled_q = exit_filled if exit_filled > 0 else qty
                order["strategy_exit_status"] = "filled"
                order["strategy_exit_filled_quantity"] = filled_q
                order["strategy_exit_quantity"] = filled_q
                order["strategy_exit_detail"] = detail
                order["strategy_exit_reason"] = "unwind"
                if exit_price > 0:
                    order["strategy_exit_executed_price"] = round(exit_price, 4)
                    order["strategy_exit_price"] = round(exit_price, 4)
            unwind_results.append(result)
        except Exception as exc:  # noqa: BLE001
            unwind_results.append({"contract_symbol": contract_symbol, "side": exit_side, "quantity": qty, "error": str(exc)})
    return unwind_results


def _compact_strategy_position_for_ai(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "tracking_id": position.get("tracking_id"),
        "strategy_key": position.get("strategy_key"),
        "symbol": position.get("symbol"),
        "family": position.get("family"),
        "strategy_type": position.get("strategy_type"),
        "label": position.get("label"),
        "direction": position.get("direction"),
        "expiration": position.get("expiration"),
        "entry_mark": position.get("entry_mark"),
        "max_loss": position.get("max_loss"),
        "max_profit": position.get("max_profit"),
        "capital_required": position.get("capital_required"),
        "take_profit_pct": position.get("take_profit_pct"),
        "system_stop_loss_pnl": position.get("stop_loss_pnl"),
        "system_take_profit_1_pnl": position.get("take_profit_1_pnl"),
        "system_take_profit_2_pnl": position.get("take_profit_2_pnl"),
        "score": position.get("score"),
        "structure_fit_score": position.get("structure_fit_score"),
        "payoff_quality_score": position.get("payoff_quality_score"),
        "execution_complexity_score": position.get("execution_complexity_score"),
        "capital_efficiency_score": position.get("capital_efficiency_score"),
        "risk_defined_score": position.get("risk_defined_score"),
        "legs": position.get("legs"),
        "fit_notes": position.get("fit_notes"),
        "hard_flags": position.get("hard_flags"),
        "natural_exit": position.get("natural_exit"),
        "live_executable": position.get("live_executable", True),
    }


def _strategy_selection_card(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "tracking_id": position.get("tracking_id"),
        "strategy_key": position.get("strategy_key"),
        "symbol": position.get("symbol"),
        "family": position.get("family"),
        "strategy_type": position.get("strategy_type"),
        "label": position.get("label"),
        "direction": position.get("direction"),
        "allocation_pct": position.get("allocation_pct"),
        "stop_loss_pct": position.get("stop_loss_pct"),
        "take_profit_pct": position.get("take_profit_pct"),
        "entry_mark": position.get("entry_mark"),
        "max_loss": position.get("max_loss"),
        "capital_required": position.get("capital_required"),
        "score": position.get("score"),
        "structure_fit_score": position.get("structure_fit_score"),
        "payoff_quality_score": position.get("payoff_quality_score"),
        "execution_complexity_score": position.get("execution_complexity_score"),
        "capital_efficiency_score": position.get("capital_efficiency_score"),
        "risk_defined_score": position.get("risk_defined_score"),
        "reason": position.get("reason"),
        "selection_source": position.get("selection_source"),
        "risk_plan_source": position.get("risk_plan_source"),
        "live_executable": position.get("live_executable", True),
        "natural_exit": position.get("natural_exit"),
    }


def _compact_scan_context(scan_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": scan_result.get("symbol"),
        "technical_bias": scan_result.get("technical_bias"),
        "daily_summary": scan_result.get("daily_summary"),
        "intraday_summary": scan_result.get("intraday_summary"),
        "status": scan_result.get("status"),
    }


def _strategy_entry_mark(candidate: dict[str, Any]) -> float:
    legs = candidate.get("legs") or []
    mark = 0.0
    for leg in legs if isinstance(legs, list) else []:
        if not isinstance(leg, dict):
            continue
        qty = max(1, int(_coerce_float(leg.get("qty"), 1)))
        price = _coerce_float(leg.get("price"), 0.0)
        multiplier = 1 if str(leg.get("side") or "").lower() == "stock" else 100
        signed = price * qty * multiplier
        mark += signed if str(leg.get("action") or "").lower() == "buy" else -signed
    if abs(mark) > 0:
        return mark
    net_debit = _coerce_float(candidate.get("net_debit"), 0.0)
    net_credit = _coerce_float(candidate.get("net_credit"), 0.0)
    if net_debit > 0:
        return net_debit * 100
    if net_credit > 0:
        return -net_credit * 100
    return 0.0


def _live_scan_workers(universe_size: int) -> int:
    default_workers = min(3, max(universe_size, 1))
    workers = env_int("AI_OPTION_LIVE_SCAN_WORKERS", default_workers, 1, 16)
    return max(1, min(workers, max(universe_size, 1)))


def _live_candidates_per_symbol(config: dict[str, Any]) -> int:
    configured = os.getenv("AI_OPTION_LIVE_CANDIDATES_PER_SYMBOL")
    if configured is None:
        configured = str(config.get("candidates_per_symbol") or 3)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 8))


def _contract_opportunities(scan_results: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    if not _single_leg_mode_allowed(config):
        return []
    limit = _live_candidates_per_symbol(config)
    opportunities: list[dict[str, Any]] = []
    seen_contracts: set[str] = set()
    for item in scan_results:
        gate = item.get("decision_gate") or {}
        gate_issues = _decision_gate_auto_blockers(gate)
        raw_candidates = item.get("candidates") or ([item.get("candidate")] if item.get("candidate") else [])
        candidates = [
            candidate for candidate in _filter_contract_candidates_for_symbol(item.get("symbol"), raw_candidates)
            if candidate.get("contract_symbol")
        ]
        for index, candidate in enumerate(candidates[:limit], start=1):
            contract_symbol = str(candidate.get("contract_symbol") or "")
            if not contract_symbol or contract_symbol in seen_contracts:
                continue
            if _candidate_observation_blockers(candidate):
                continue
            opportunity = {
                "symbol": item.get("symbol"),
                "contract_symbol": contract_symbol,
                "candidate_rank_for_symbol": index,
                "candidate": candidate,
                "decision_gate": gate,
                "auto_trade_allowed": not gate_issues,
                "observation_only": bool(gate_issues),
                "observation_reasons": gate_issues,
                "technical_bias": item.get("technical_bias"),
                "daily_summary": item.get("daily_summary"),
                "intraday_summary": item.get("intraday_summary"),
                "evidence_card": _contract_evidence_card(item, candidate, index),
            }
            opportunities.append(opportunity)
            seen_contracts.add(contract_symbol)
    return sorted(opportunities, key=lambda item: _candidate_sort_score(item.get("candidate") or {}), reverse=True)


def _decision_gate_allows_single_leg_auto(gate: dict[str, Any]) -> bool:
    if gate.get("should_trade") is False:
        return False
    if gate.get("allow_single_leg") is False:
        return False
    if gate.get("allow_auto_trade") is False:
        return False
    trigger = gate.get("single_leg_trigger") or {}
    if trigger and not trigger.get("triggered"):
        return False
    return True


def _decision_gate_auto_blockers(gate: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if gate.get("should_trade") is False:
        issues.append("decision_gate_should_trade_false")
    if gate.get("allow_single_leg") is False:
        issues.append("decision_gate_single_leg_disabled")
    if gate.get("allow_auto_trade") is False:
        issues.append("decision_gate_auto_trade_disabled")
    trigger = gate.get("single_leg_trigger") or {}
    if trigger and not trigger.get("triggered"):
        issues.append("single_leg_trigger_not_met")
    return issues


def _decision_gate_allows_strategy(
    gate: dict[str, Any],
    *,
    require_auto: bool,
    structure_only: bool = False,
    strategy_candidates: list[dict[str, Any]] | None = None,
) -> bool:
    if gate.get("should_trade") is False:
        return False
    if gate.get("allow_strategy") is False:
        return False
    if require_auto and gate.get("allow_auto_trade") is False:
        # gate.allow_auto_trade is derived from the single-leg candidate pool; for
        # structure-only instances we re-check against structure candidate quality.
        if structure_only and _structure_candidates_pass_auto_threshold(
            strategy_candidates,
            low_gate_enabled=bool(gate.get("low_gate_enabled")),
        ):
            return True
        return False
    return True


def _structure_candidates_pass_auto_threshold(
    strategy_candidates: list[dict[str, Any]] | None,
    *,
    low_gate_enabled: bool,
) -> bool:
    if not strategy_candidates:
        return False
    score_threshold = 40.0 if low_gate_enabled else 50.0
    rr_threshold = 0.25 if low_gate_enabled else 0.35
    for candidate in strategy_candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("hard_flags"):
            continue
        if _coerce_float(candidate.get("score"), 0.0) < score_threshold:
            continue
        max_loss = _coerce_float(candidate.get("max_loss"), 0.0)
        max_profit = _coerce_float(candidate.get("max_profit"), 0.0)
        if max_loss <= 0 or max_profit <= 0:
            continue
        if max_profit / max_loss < rr_threshold:
            continue
        return True
    return False


def _symbol_evidence_card(
    symbol: str,
    technical_bias: Any,
    daily_summary: dict[str, Any],
    intraday_summary: dict[str, Any],
    candidates: list[dict[str, Any]],
    decision_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "technical_bias": technical_bias,
        "decision_gate": _decision_gate_card(decision_gate or {}),
        "market_context": _market_context_card(daily_summary, intraday_summary),
        "candidate_count_sent_to_council": len(candidates),
        "best_analysis_score": max((_coerce_float(item.get("analysis_score"), 0.0) for item in candidates), default=0.0),
        "best_decision_score": max((_coerce_float(item.get("decision_score"), _coerce_float(item.get("analysis_score"), 0.0)) for item in candidates), default=0.0),
        "candidate_cards": [_candidate_evidence_card(candidate, index + 1) for index, candidate in enumerate(candidates)],
    }


def _contract_evidence_card(scan_result: dict[str, Any], candidate: dict[str, Any], rank_for_symbol: int) -> dict[str, Any]:
    return {
        "symbol": scan_result.get("symbol"),
        "contract_symbol": candidate.get("contract_symbol"),
        "rank_for_symbol": rank_for_symbol,
        "technical_bias": scan_result.get("technical_bias"),
        "decision_gate": _decision_gate_card(scan_result.get("decision_gate") or {}),
        "market_context": _market_context_card(scan_result.get("daily_summary") or {}, scan_result.get("intraday_summary") or {}),
        "candidate": _candidate_evidence_card(candidate, rank_for_symbol),
    }


def _market_context_card(daily_summary: dict[str, Any], intraday_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "daily_change_pct": _round(_coerce_float(daily_summary.get("change_pct"), 0.0), 2),
        "daily_close": _round(_coerce_float(daily_summary.get("close"), 0.0), 2),
        "intraday_change_pct": _round(_coerce_float(intraday_summary.get("change_pct"), 0.0), 2),
        "vs_vwap_pct": _round(_coerce_float(intraday_summary.get("vs_vwap_pct"), 0.0), 2),
        "intraday_trend": intraday_summary.get("trend") or intraday_summary.get("state") or "unknown",
    }


def _candidate_evidence_card(candidate: dict[str, Any], rank_for_symbol: int) -> dict[str, Any]:
    hard_flags = _candidate_hard_flags(candidate)
    return {
        "rank_for_symbol": rank_for_symbol,
        "contract_symbol": candidate.get("contract_symbol"),
        "side": candidate.get("side"),
        "expiration": candidate.get("expiration"),
        "strike": _round(_coerce_float(candidate.get("strike"), 0.0), 2),
        "ask": _round(_coerce_float(candidate.get("ask"), 0.0), 2),
        "bid": _round(_coerce_float(candidate.get("bid"), 0.0), 2),
        "spread_pct": _round(_coerce_float(candidate.get("spread_pct"), 100.0), 1),
        "volume": int(_coerce_float(candidate.get("volume"), 0.0)),
        "open_interest": int(_coerce_float(candidate.get("open_interest"), 0.0)),
        "days_to_expiration": _round(_coerce_float(candidate.get("days_to_expiration"), 0.0), 1),
        "delta": _round(_coerce_float(candidate.get("delta"), 0.0), 2),
        "gamma": _round(_coerce_float(candidate.get("gamma"), 0.0), 4),
        "theta_to_ask_pct": _round(_coerce_float(candidate.get("theta_to_ask_pct"), 0.0), 1),
        "iv_percentile": _round(_coerce_float(candidate.get("iv_percentile"), 0.0), 0),
        "execution_quality_score": _round(_coerce_float(candidate.get("execution_quality_score"), 0.0), 1),
        "execution_quality_state": candidate.get("execution_quality_state"),
        "strategy_tag": candidate.get("strategy_tag"),
        "probability_breakeven": _round(_coerce_float(candidate.get("probability_breakeven"), 0.0), 1),
        "reward_risk_score": _round(_coerce_float(candidate.get("reward_risk_score"), 0.0), 1),
        "analysis_score": _round(_coerce_float(candidate.get("analysis_score"), 0.0), 2),
        "alpha_score": _round(_coerce_float(candidate.get("alpha_score"), 0.0), 2),
        "execution_score": _round(_coerce_float(candidate.get("execution_score"), candidate.get("execution_quality_score") or 0.0), 2),
        "decision_score": _round(_coerce_float(candidate.get("decision_score"), candidate.get("analysis_score") or 0.0), 2),
        "decision_bucket": candidate.get("decision_bucket"),
        "gex_regime": candidate.get("gex_regime"),
        "gex_alignment": candidate.get("gex_alignment"),
        "hard_flags": hard_flags,
        "risk_plan": {
            "max_loss_per_contract": _round(_coerce_float((candidate.get("risk_plan") or {}).get("max_loss_per_contract"), _coerce_float(candidate.get("ask"), 0.0) * 100), 2),
            "stop_loss_option_price": _round(_coerce_float((candidate.get("risk_plan") or {}).get("stop_loss_option_price"), _coerce_float(candidate.get("ask"), 0.0) * 0.55), 2),
            "take_profit_1": _round(_coerce_float((candidate.get("risk_plan") or {}).get("take_profit_1"), _coerce_float(candidate.get("ask"), 0.0) * 1.45), 2),
            "take_profit_2": _round(_coerce_float((candidate.get("risk_plan") or {}).get("take_profit_2"), _coerce_float(candidate.get("ask"), 0.0) * 2.1), 2),
            "latest_exit": (candidate.get("risk_plan") or {}).get("latest_exit"),
        },
    }


def _decision_gate_card(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "should_trade": bool(gate.get("should_trade", True)),
        "allow_single_leg": bool(gate.get("allow_single_leg", True)),
        "allow_auto_trade": bool(gate.get("allow_auto_trade", True)),
        "preferred_execution": gate.get("preferred_execution") or "normal",
        "regime": gate.get("regime") or "unknown",
        "confidence": _round(_coerce_float(gate.get("confidence"), 0.0), 2),
        "vote_summary": gate.get("vote_summary") or {},
        "single_leg_trigger": gate.get("single_leg_trigger") or {},
        "preferred_strategy_families": gate.get("preferred_strategy_families") or [],
        "blockers": gate.get("blockers") or [],
        "warnings": gate.get("warnings") or [],
    }


def _candidate_hard_flags(candidate: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    ask = _coerce_float(candidate.get("ask"), 0.0)
    bid = _coerce_float(candidate.get("bid"), 0.0)
    spread_pct = _coerce_float(candidate.get("spread_pct"), 100.0)
    volume = _coerce_float(candidate.get("volume"), 0.0)
    open_interest = _coerce_float(candidate.get("open_interest"), 0.0)
    execution_quality = _coerce_float(candidate.get("execution_quality_score"), 0.0)
    if ask <= 0:
        flags.append("invalid_ask")
    if bid <= 0:
        flags.append("no_bid")
    if spread_pct > 30:
        flags.append("wide_spread")
    if volume < 100 and open_interest < 1000:
        flags.append("thin_liquidity")
    if execution_quality and execution_quality < 25:
        flags.append("poor_execution_quality")
    if str(candidate.get("pricing_source") or "") == "unavailable":
        flags.append("quote_unavailable")
    if str(candidate.get("decision_bucket") or "") in {"observe_trigger_not_met", "blocked_execution"}:
        flags.append(str(candidate.get("decision_bucket")))
    if _coerce_float(candidate.get("trigger_score"), 0.0) < 60:
        flags.append("trigger_not_met")
    if _coerce_float(candidate.get("time_value_risk_penalty"), 0.0) >= 12:
        flags.append("time_value_risk_high")
    flags.extend(str(flag) for flag in (candidate.get("execution_hard_flags") or []) if flag)
    return flags


def _candidate_live_blockers(candidate: dict[str, Any]) -> list[str]:
    flags = set(_candidate_hard_flags(candidate))
    return sorted(flags & {
        "invalid_ask",
        "wide_spread",
        "thin_liquidity",
        "quote_unavailable",
        "observe_trigger_not_met",
        "blocked_execution",
        "trigger_not_met",
    })


def _candidate_observation_blockers(candidate: dict[str, Any]) -> list[str]:
    flags = set(_candidate_hard_flags(candidate))
    return sorted(flags & {
        "invalid_ask",
        "wide_spread",
        "thin_liquidity",
        "quote_unavailable",
        "blocked_execution",
    })


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def _ai_ranking_payload_limit() -> int:
    # Cap how many ranked opportunities we serialize into AI ranking prompts.
    # Full list is still scored locally; AI only needs the top slice.
    return env_int("AI_OPTION_RANKING_PAYLOAD_LIMIT", 80, 5, 500)


def _live_ranking_payload(opportunities: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    entry_order_type = _normalize_entry_order_type(config.get("entry_order_type"))
    total = len(opportunities)
    limit = _ai_ranking_payload_limit()
    truncated = opportunities[:limit] if total > limit else opportunities
    return {
        "top_n": int(config.get("top_n") or 5),
        "total_capital": float(config.get("total_capital") or 0),
        "default_stop_loss_pct": float(config.get("default_stop_loss_pct") or 25),
        "default_take_profit_pct": float(config.get("default_take_profit_pct") or 30),
        "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled")),
        "default_take_profit_1_pct": float(config.get("default_take_profit_1_pct") or 20),
        "default_take_profit_2_pct": float(config.get("default_take_profit_2_pct") or 35),
        "ai_adjust_allocation": bool(config.get("ai_adjust_allocation")),
        "ai_adjust_stop_loss": bool(config.get("ai_adjust_stop_loss")),
        "ai_adjust_take_profit": bool(config.get("ai_adjust_take_profit")),
        "auto_trade_candidate_count": sum(1 for item in opportunities if not item.get("observation_only")),
        "observation_candidate_count": sum(1 for item in opportunities if item.get("observation_only")),
        "opportunities_total": total,
        "opportunities_truncated_to": len(truncated),
        "execution_context": {
            "entry_order_type": entry_order_type,
            "market_order_skips_requote": entry_order_type == "market",
            "limit_order_requotes_before_submit": entry_order_type == "limit",
        },
        "opportunities": truncated,
        "decision_directive": str(config.get("decision_directive") or "").strip() or None,
    }


def _strategy_ranking_payload(strategy_opportunities: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    entry_order_type = _normalize_entry_order_type(config.get("entry_order_type"))
    total = len(strategy_opportunities)
    limit = _ai_ranking_payload_limit()
    truncated = strategy_opportunities[:limit] if total > limit else strategy_opportunities
    return {
        "top_n": int(config.get("top_n") or 5),
        "total_capital": float(config.get("total_capital") or 0),
        "default_stop_loss_pct": float(config.get("default_stop_loss_pct") or 25),
        "default_take_profit_pct": float(config.get("default_take_profit_pct") or 30),
        "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled")),
        "default_take_profit_1_pct": float(config.get("default_take_profit_1_pct") or 20),
        "default_take_profit_2_pct": float(config.get("default_take_profit_2_pct") or 35),
        "ai_adjust_allocation": bool(config.get("ai_adjust_allocation")),
        "ai_adjust_stop_loss": bool(config.get("ai_adjust_stop_loss")),
        "ai_adjust_take_profit": bool(config.get("ai_adjust_take_profit")),
        "strategy_opportunities_total": total,
        "strategy_opportunities_truncated_to": len(truncated),
        "execution_context": {
            "entry_order_type": entry_order_type,
            "market_order_skips_requote": entry_order_type == "market",
            "limit_order_requotes_before_submit": entry_order_type == "limit",
        },
        "max_per_symbol": int(config.get("max_per_symbol") or 1),
        "strategy_opportunities": truncated,
        "decision_directive": str(config.get("decision_directive") or "").strip() or None,
    }


def _rank_opportunities_by_score(opportunities: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    target_count = min(int(config.get("top_n") or 5), len(opportunities))
    stop_loss_pct = float(config.get("default_stop_loss_pct") or 25)
    take_profit_settings = _take_profit_settings(config)
    selections = []
    for opportunity in opportunities[:target_count]:
        candidate = opportunity.get("candidate") or {}
        contract_symbol = _opportunity_contract_symbol(opportunity)
        if not contract_symbol:
            continue
        selections.append(
            {
                "contract_symbol": contract_symbol,
                "symbol": opportunity.get("symbol"),
                "allocation_pct": 0,
                "allocation_source": "score_rank",
                "selection_source": "score_rank",
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_settings["single_pct"],
                "take_profit_1_pct": take_profit_settings["tp1_pct"],
                "take_profit_2_pct": take_profit_settings["tp2_pct"],
                "tiered_take_profit_enabled": take_profit_settings["tiered"],
                "reason": (
                    "未启用 AI，按本地决策分排序入选；"
                    f"decision_score={_round(_candidate_sort_score(candidate), 2)}，"
                    f"spread_pct={_round(_coerce_float(candidate.get('spread_pct'), 0.0), 1)}，"
                    f"execution_score={_round(_coerce_float(candidate.get('execution_score'), candidate.get('execution_quality_score') or 0.0), 1)}。"
                ),
            }
        )
    return {
        "summary": "未启用 AI，系统按本地候选决策分选择实盘 Top N。",
        "council_mode": "disabled",
        "advisor_reports": [],
        "ai_execution": {
            "version": 1,
            "phase": "single_leg_score_rank",
            "requested": False,
            "attempted": False,
            "provider": config.get("ai_provider") or "",
            "council_requested": False,
            "advisor_count": 0,
            "advisor_success_count": 0,
            "moderator_answer_present": False,
            "moderator_json_valid": False,
            "selection_count": len(selections),
        },
        "selections": selections,
        "rejected": [],
        "risk_notes": ["AI 已关闭，入选理由来自本地评分与执行硬规则。"],
    }


def _rank_strategy_opportunities_by_score(strategy_opportunities: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    target_count = min(int(config.get("top_n") or 5), len(strategy_opportunities))
    selections = []
    for opportunity in strategy_opportunities[:target_count]:
        candidate = opportunity.get("candidate") or {}
        strategy_key = _strategy_selection_key(opportunity)
        if not strategy_key:
            continue
        selections.append(
            {
                "strategy_key": strategy_key,
                "symbol": opportunity.get("symbol"),
                "allocation_pct": 0,
                "selection_source": "strategy_score_rank",
                "reason": (
                    "未启用 AI，按本地策略结构评分排序入选；"
                    f"score={_round(_coerce_float(candidate.get('score'), 0.0), 2)}，"
                    f"max_loss={_round(_coerce_float(candidate.get('max_loss'), 0.0), 2)}，"
                    f"capital_required={_round(_coerce_float(candidate.get('capital_required'), 0.0), 2)}。"
                ),
            }
        )
    return {
        "summary": "未启用 AI，系统按本地策略结构评分选择 Top N。",
        "council_mode": "strategy_disabled",
        "advisor_reports": [],
        "ai_execution": {
            "version": 1,
            "phase": "strategy_score_rank",
            "requested": False,
            "attempted": False,
            "provider": config.get("ai_provider") or "",
            "council_requested": False,
            "advisor_count": 0,
            "advisor_success_count": 0,
            "moderator_answer_present": False,
            "moderator_json_valid": False,
            "selection_count": len(selections),
        },
        "strategy_selections": selections,
        "rejected": [],
        "risk_notes": ["AI 已关闭，策略入选来自本地评分与结构硬规则。"],
    }


def _rank_opportunities(opportunities: list[dict[str, Any]], config: dict[str, Any], owner_id: str) -> dict[str, Any]:
    if not opportunities:
        return {"summary": "没有可交易候选", "council_mode": "no_candidates", "selections": []}
    if not config.get("use_ai", True):
        return _rank_opportunities_by_score(opportunities, config)
    if not config.get("council", True):
        return _run_single_live_decision(opportunities, config, owner_id)
    return _run_live_council(opportunities, config, owner_id)


def _rank_strategy_opportunities(strategy_opportunities: list[dict[str, Any]], config: dict[str, Any], owner_id: str) -> dict[str, Any]:
    if not strategy_opportunities:
        return {"summary": "没有策略结构候选", "council_mode": "strategy_no_candidates", "strategy_selections": []}
    if not config.get("use_ai", True):
        return _rank_strategy_opportunities_by_score(strategy_opportunities, config)
    if not config.get("council", True):
        return _run_single_strategy_decision(strategy_opportunities, config, owner_id)
    return _run_strategy_council(strategy_opportunities, config, owner_id)


def _run_strategy_council(strategy_opportunities: list[dict[str, Any]], config: dict[str, Any], owner_id: str) -> dict[str, Any]:
    payload = _strategy_ranking_payload(strategy_opportunities, config)
    provider_name = config.get("ai_provider") or "deepseek"
    advisor_reports = _ask_strategy_advisors(payload, provider_name, owner_id)
    if not advisor_reports or any(report.get("status") != "succeeded" for report in advisor_reports):
        return _failed_strategy_council(
            advisor_reports=advisor_reports,
            council_mode="fallback_advisor_unavailable",
            summary="三诸葛亮会话未全部返回有效意见；策略实盘模式下不使用本地排序替代。",
            ai_execution=_trading_ai_execution(
                phase="strategy_council",
                provider_name=provider_name,
                advisor_reports=advisor_reports,
                moderator_answer=None,
                parsed=None,
                selection_count=0,
                fallback_reason="策略三顾问没有全部返回有效 JSON。",
            ),
        )

    compact_advisor_reports = _compact_strategy_advisor_reports(advisor_reports)
    moderator_payload = {
        "ranking_payload": payload,
        "advisor_reports": compact_advisor_reports,
    }
    answer = ask_ai(
        STRATEGY_MODERATOR_PROMPT,
        moderator_payload,
        provider_name,
        owner_id=owner_id,
        temperature=DECISION_TEMPERATURE,
        response_format=JSON_RESPONSE_FORMAT,
    )
    parsed = extract_json_object(answer)
    if parsed and isinstance(parsed.get("strategy_selections"), list):
        parsed.setdefault("summary", "三诸葛亮已完成策略结构讨论并给出终选。")
        parsed["council_mode"] = "strategy_three_advisors"
        parsed["advisor_reports"] = advisor_reports
        parsed["raw_answer"] = answer
        parsed["ai_execution"] = _trading_ai_execution(
            phase="strategy_council",
            provider_name=provider_name,
            advisor_reports=advisor_reports,
            moderator_answer=answer,
            parsed=parsed,
            selection_count=len(parsed.get("strategy_selections") or []),
        )
        return parsed
    return _failed_strategy_council(
        advisor_reports=advisor_reports,
        raw_answer=answer,
        council_mode="fallback_moderator_invalid_json",
        summary="策略主持人未返回有效 JSON；策略实盘模式下不使用本地排序替代。",
        ai_execution=_trading_ai_execution(
            phase="strategy_council",
            provider_name=provider_name,
            advisor_reports=advisor_reports,
            moderator_answer=answer,
            parsed=parsed,
            selection_count=0,
            fallback_reason="策略主持人没有返回可解析 JSON。",
        ),
    )


def _run_single_strategy_decision(strategy_opportunities: list[dict[str, Any]], config: dict[str, Any], owner_id: str) -> dict[str, Any]:
    payload = _strategy_ranking_payload(strategy_opportunities, config)
    provider_name = config.get("ai_provider") or "deepseek"
    answer = ask_ai(
        STRATEGY_SINGLE_DECISION_PROMPT,
        payload,
        provider_name,
        owner_id=owner_id,
        temperature=DECISION_TEMPERATURE,
        response_format=JSON_RESPONSE_FORMAT,
    )
    parsed = extract_json_object(answer)
    if parsed and isinstance(parsed.get("strategy_selections"), list):
        parsed.setdefault("summary", "单 AI 已完成策略结构裁决并给出终选。")
        parsed["council_mode"] = "strategy_single_ai"
        parsed["advisor_reports"] = []
        parsed["raw_answer"] = answer
        for selection in parsed.get("strategy_selections") or []:
            if isinstance(selection, dict):
                selection.setdefault("selection_source", "strategy_ai_initial")
        parsed["ai_execution"] = _trading_ai_execution(
            phase="strategy_single_ai",
            provider_name=provider_name,
            advisor_reports=[],
            moderator_answer=answer,
            parsed=parsed,
            selection_count=len(parsed.get("strategy_selections") or []),
            council_requested=False,
        )
        return parsed
    return _failed_strategy_council(
        advisor_reports=[],
        raw_answer=answer,
        council_mode="strategy_single_ai_invalid_json",
        summary="单 AI 策略裁决未返回有效 JSON；未形成可执行结构。",
        ai_execution=_trading_ai_execution(
            phase="strategy_single_ai",
            provider_name=provider_name,
            advisor_reports=[],
            moderator_answer=answer,
            parsed=parsed,
            selection_count=0,
            fallback_reason="单 AI 策略裁决没有返回可解析 JSON。",
            council_requested=False,
        ),
    )


def _ask_strategy_advisors(payload: dict[str, Any], provider_name: str, owner_id: str) -> list[dict[str, Any]]:
    advisor_reports: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(STRATEGY_ADVISORS), thread_name_prefix="strategy-council") as executor:
        futures = {
            executor.submit(
                ask_ai,
                advisor["prompt"],
                payload,
                provider_name,
                owner_id,
                DECISION_TEMPERATURE,
                JSON_RESPONSE_FORMAT,
            ): advisor
            for advisor in STRATEGY_ADVISORS
        }
        for future in as_completed(futures):
            advisor = futures[future]
            report = None
            try:
                report = future.result()
            except Exception as exc:
                report = f"ERROR: {exc}"
            structured_report = extract_json_object(report)
            advisor_reports.append(
                {
                    "key": advisor["key"],
                    "advisor": advisor["name"],
                    "report": report or "",
                    "structured_report": structured_report or {},
                    "status": "succeeded" if structured_report and not str(report).startswith("ERROR:") else "failed",
                }
            )
    order = {advisor["key"]: index for index, advisor in enumerate(STRATEGY_ADVISORS)}
    return sorted(advisor_reports, key=lambda item: order.get(str(item.get("key")), 99))


def _compact_strategy_advisor_reports(advisor_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "key": item.get("key"),
            "advisor": item.get("advisor"),
            "status": item.get("status"),
            "structured_report": item.get("structured_report") or {},
        }
        for item in advisor_reports
    ]


def _trading_ai_execution(
    *,
    phase: str,
    provider_name: str,
    advisor_reports: list[dict[str, Any]] | None,
    moderator_answer: str | None,
    parsed: dict[str, Any] | None,
    selection_count: int,
    fallback_reason: str = "",
    council_requested: bool = True,
) -> dict[str, Any]:
    reports = advisor_reports or []
    success_count = sum(1 for item in reports if item.get("status") == "succeeded")
    return {
        "version": 1,
        "phase": phase,
        "requested": True,
        "attempted": True,
        "provider": provider_name,
        "council_requested": bool(council_requested),
        "advisor_count": len(reports),
        "advisor_success_count": success_count,
        "moderator_answer_present": bool(moderator_answer),
        "moderator_json_valid": bool(parsed),
        "selection_count": int(selection_count or 0),
        "fallback_reason": fallback_reason,
    }


def _failed_strategy_council(
    advisor_reports: list[dict[str, Any]] | None = None,
    raw_answer: str | None = None,
    council_mode: str = "strategy_council_failed",
    summary: str = "三诸葛亮策略决策失败；未形成可执行结构。",
    ai_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "summary": summary,
        "council_mode": council_mode,
        "advisor_reports": advisor_reports or [],
        "ai_execution": ai_execution or {},
        "raw_answer": raw_answer,
        "strategy_selections": [],
    }


def _unique_valid_strategy_selections(
    selections: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    *,
    max_per_symbol: int = 1,
) -> list[dict[str, Any]]:
    valid_keys = {
        _strategy_selection_key(item)
        for item in opportunities
        if _strategy_selection_key(item)
    }
    output = []
    seen: set[str] = set()
    symbol_count: dict[str, int] = {}
    for selection in selections:
        key = str(selection.get("strategy_key") or "").strip()
        if not key or key not in valid_keys or key in seen:
            continue
        symbol = str(selection.get("symbol") or "").strip()
        if max_per_symbol > 0 and symbol and symbol_count.get(symbol, 0) >= max_per_symbol:
            continue
        output.append(dict(selection))
        seen.add(key)
        if symbol:
            symbol_count[symbol] = symbol_count.get(symbol, 0) + 1
    return output


def _strategy_selection_key(item: dict[str, Any]) -> str:
    key = str(item.get("strategy_key") or "").strip()
    if key:
        return key
    candidate = item.get("candidate") or {}
    return str(candidate.get("strategy_key") or "").strip()


def _run_live_council(opportunities: list[dict[str, Any]], config: dict[str, Any], owner_id: str) -> dict[str, Any]:
    payload = _live_ranking_payload(opportunities, config)
    provider_name = config.get("ai_provider") or "deepseek"
    advisor_reports = _ask_live_advisors(payload, provider_name, owner_id)
    if not advisor_reports or any(report.get("status") != "succeeded" for report in advisor_reports):
        return _failed_council(
            advisor_reports=advisor_reports,
            council_mode="fallback_advisor_unavailable",
            summary="三诸葛亮会话未全部返回有效意见；严格实盘模式下不使用本地排序替代，因此不提交订单。",
            ai_execution=_trading_ai_execution(
                phase="single_leg_council",
                provider_name=provider_name,
                advisor_reports=advisor_reports,
                moderator_answer=None,
                parsed=None,
                selection_count=0,
                fallback_reason="单腿三顾问没有全部返回有效 JSON。",
            ),
        )

    compact_advisor_reports = _compact_advisor_reports(advisor_reports)
    moderator_payload = {
        "ranking_payload": payload,
        "advisor_reports": compact_advisor_reports,
    }
    answer = ask_ai(
        LIVE_MODERATOR_PROMPT,
        moderator_payload,
        provider_name,
        owner_id=owner_id,
        temperature=DECISION_TEMPERATURE,
        response_format=JSON_RESPONSE_FORMAT,
    )
    parsed = extract_json_object(answer)
    if parsed and isinstance(parsed.get("selections"), list):
        parsed.setdefault("summary", "三诸葛亮已完成实盘讨论并给出终选。")
        parsed["council_mode"] = "three_advisors"
        parsed["advisor_reports"] = advisor_reports
        parsed["raw_answer"] = answer
        parsed["ai_execution"] = _trading_ai_execution(
            phase="single_leg_council",
            provider_name=provider_name,
            advisor_reports=advisor_reports,
            moderator_answer=answer,
            parsed=parsed,
            selection_count=len(parsed.get("selections") or []),
        )
        return _top_up_council_selections(parsed, opportunities, config, provider_name, advisor_reports, owner_id)
    return _failed_council(
        advisor_reports=advisor_reports,
        raw_answer=answer,
        council_mode="fallback_moderator_invalid_json",
        summary="主持人未返回有效 JSON；严格实盘模式下不使用本地排序替代，因此不提交订单。",
        ai_execution=_trading_ai_execution(
            phase="single_leg_council",
            provider_name=provider_name,
            advisor_reports=advisor_reports,
            moderator_answer=answer,
            parsed=parsed,
            selection_count=0,
            fallback_reason="单腿主持人没有返回可解析 JSON。",
        ),
    )


def _run_single_live_decision(opportunities: list[dict[str, Any]], config: dict[str, Any], owner_id: str) -> dict[str, Any]:
    payload = _live_ranking_payload(opportunities, config)
    provider_name = config.get("ai_provider") or "deepseek"
    answer = ask_ai(
        LIVE_SINGLE_DECISION_PROMPT,
        payload,
        provider_name,
        owner_id=owner_id,
        temperature=DECISION_TEMPERATURE,
        response_format=JSON_RESPONSE_FORMAT,
    )
    parsed = extract_json_object(answer)
    if parsed and isinstance(parsed.get("selections"), list):
        parsed.setdefault("summary", "单 AI 已完成实盘裁决并给出终选。")
        parsed["council_mode"] = "single_ai"
        parsed["advisor_reports"] = []
        parsed["raw_answer"] = answer
        parsed["ai_execution"] = _trading_ai_execution(
            phase="single_leg_single_ai",
            provider_name=provider_name,
            advisor_reports=[],
            moderator_answer=answer,
            parsed=parsed,
            selection_count=len(parsed.get("selections") or []),
            council_requested=False,
        )
        return _top_up_council_selections(parsed, opportunities, config, provider_name, [], owner_id)
    return _failed_council(
        advisor_reports=[],
        raw_answer=answer,
        council_mode="single_ai_invalid_json",
        summary="单 AI 实盘裁决未返回有效 JSON；严格实盘模式下不使用本地排序替代，因此不提交订单。",
        ai_execution=_trading_ai_execution(
            phase="single_leg_single_ai",
            provider_name=provider_name,
            advisor_reports=[],
            moderator_answer=answer,
            parsed=parsed,
            selection_count=0,
            fallback_reason="单 AI 实盘裁决没有返回可解析 JSON。",
            council_requested=False,
        ),
    )


def _top_up_council_selections(
    council: dict[str, Any],
    opportunities: list[dict[str, Any]],
    config: dict[str, Any],
    provider_name: str,
    advisor_reports: list[dict[str, Any]],
    owner_id: str,
) -> dict[str, Any]:
    target_count = min(int(config.get("top_n") or 5), len(opportunities))
    current_selections = _unique_valid_selections(council.get("selections") or [], opportunities)
    for selection in current_selections:
        selection.setdefault("selection_source", "ai_initial")
    council["selections"] = current_selections
    missing_count = target_count - len(current_selections)
    if missing_count <= 0:
        council["top_up"] = {"needed": False, "target_count": target_count, "added_count": 0}
        return council

    selected_contracts = {str(item.get("contract_symbol") or "") for item in current_selections}
    remaining_opportunities = [
        item
        for item in opportunities
        if _opportunity_contract_symbol(item) not in selected_contracts
    ]
    tradable_remaining = [
        item
        for item in remaining_opportunities
        if not _decision_gate_auto_blockers(item.get("decision_gate") or {})
    ]
    if not tradable_remaining:
        council["top_up"] = {
            "needed": True,
            "status": "observation_only_remaining",
            "target_count": target_count,
            "missing_count": missing_count,
            "added_count": 0,
            "observation_only_remaining_count": len(remaining_opportunities),
        }
        return council

    payload = {
        "target_count": target_count,
        "missing_count": min(missing_count, len(tradable_remaining)),
        "total_capital": float(config.get("total_capital") or 0),
        "default_stop_loss_pct": float(config.get("default_stop_loss_pct") or 25),
        "default_take_profit_pct": float(config.get("default_take_profit_pct") or 30),
        "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled")),
        "default_take_profit_1_pct": float(config.get("default_take_profit_1_pct") or 20),
        "default_take_profit_2_pct": float(config.get("default_take_profit_2_pct") or 35),
        "ai_adjust_allocation": bool(config.get("ai_adjust_allocation")),
        "ai_adjust_stop_loss": bool(config.get("ai_adjust_stop_loss")),
        "ai_adjust_take_profit": bool(config.get("ai_adjust_take_profit")),
        "execution_context": {
            "entry_order_type": _normalize_entry_order_type(config.get("entry_order_type")),
            "market_order_skips_requote": _normalize_entry_order_type(config.get("entry_order_type")) == "market",
            "limit_order_requotes_before_submit": _normalize_entry_order_type(config.get("entry_order_type")) == "limit",
        },
        "already_selected_contracts": sorted(selected_contracts),
        "initial_summary": council.get("summary"),
        "initial_selections": current_selections,
        "remaining_opportunities": tradable_remaining,
        "advisor_reports": _compact_advisor_reports(advisor_reports),
    }
    answer = ask_ai(
        LIVE_TOP_UP_PROMPT,
        payload,
        provider_name,
        owner_id=owner_id,
        temperature=DECISION_TEMPERATURE,
        response_format=JSON_RESPONSE_FORMAT,
    )
    parsed = extract_json_object(answer)
    additional = []
    if parsed and isinstance(parsed.get("additional_selections"), list):
        additional = _unique_valid_selections(parsed.get("additional_selections") or [], tradable_remaining)
        for selection in additional:
            selection["selection_source"] = "ai_top_up"
    additional = additional[:missing_count]
    council["selections"] = current_selections + additional
    ai_execution = dict(council.get("ai_execution") or {})
    if ai_execution:
        ai_execution["selection_count"] = len(council["selections"])
        ai_execution["top_up_attempted"] = True
        ai_execution["top_up_answer_present"] = bool(answer)
        ai_execution["top_up_json_valid"] = bool(parsed)
        ai_execution["top_up_added_count"] = len(additional)
        council["ai_execution"] = ai_execution
    council["top_up"] = {
        "needed": True,
        "status": "succeeded" if len(additional) >= missing_count else "partial_or_failed",
        "target_count": target_count,
        "missing_count": missing_count,
        "added_count": len(additional),
        "raw_answer": answer,
        "summary": parsed.get("summary") if parsed else None,
        "rejected": parsed.get("rejected") if parsed else [],
        "risk_notes": parsed.get("risk_notes") if parsed else [],
    }
    if additional:
        council["summary"] = (
            f"{council.get('summary') or ''} AI 补足决策：主持人首次返回 {len(current_selections)} 个，"
            f"二次裁决从剩余候选补选 {len(additional)} 个。{parsed.get('summary') if parsed else ''}"
        ).strip()
    return council


def _unique_valid_selections(selections: list[dict[str, Any]], opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_contracts = {
        _opportunity_contract_symbol(item)
        for item in opportunities
        if _opportunity_contract_symbol(item)
    }
    output = []
    seen: set[str] = set()
    for selection in selections:
        contract_symbol = str(selection.get("contract_symbol") or "")
        if not contract_symbol or contract_symbol not in valid_contracts or contract_symbol in seen:
            continue
        output.append(dict(selection))
        seen.add(contract_symbol)
    return output


def _selection_gate_issues(
    selections: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_contract = {
        _opportunity_contract_symbol(item): item
        for item in opportunities
        if _opportunity_contract_symbol(item)
    }
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for selection in selections:
        contract_symbol = str(selection.get("contract_symbol") or "").strip()
        if not contract_symbol or contract_symbol in seen:
            continue
        source = by_contract.get(contract_symbol)
        if not source:
            continue
        gate = source.get("decision_gate") or {}
        candidate = source.get("candidate") or {}
        gate_issues = _decision_gate_auto_blockers(gate)
        candidate_issues = _candidate_observation_blockers(candidate)
        blocked = []
        if gate_issues:
            blocked.extend(gate_issues)
        if candidate_issues:
            blocked.extend(f"candidate_{item}" for item in candidate_issues)
        if blocked:
            issues.append(
                {
                    "contract_symbol": contract_symbol,
                    "symbol": source.get("symbol"),
                    "issues": blocked,
                    "decision_gate": _decision_gate_card(gate),
                    "candidate": _candidate_evidence_card(candidate, int(source.get("candidate_rank_for_symbol") or 1)),
                }
            )
        seen.add(contract_symbol)
    return issues


def _ask_live_advisors(payload: dict[str, Any], provider_name: str, owner_id: str) -> list[dict[str, Any]]:
    advisor_reports: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(LIVE_ADVISORS), thread_name_prefix="live-council") as executor:
        futures = {
            executor.submit(
                ask_ai,
                advisor["prompt"],
                payload,
                provider_name,
                owner_id,
                DECISION_TEMPERATURE,
                JSON_RESPONSE_FORMAT,
            ): advisor
            for advisor in LIVE_ADVISORS
        }
        for future in as_completed(futures):
            advisor = futures[future]
            report = None
            try:
                report = future.result()
            except Exception as exc:
                report = f"ERROR: {exc}"
            structured_report = extract_json_object(report)
            advisor_reports.append(
                {
                    "key": advisor["key"],
                    "advisor": advisor["name"],
                    "report": report or "",
                    "structured_report": structured_report or {},
                    "status": "succeeded" if structured_report and not str(report).startswith("ERROR:") else "failed",
                }
            )
    order = {advisor["key"]: index for index, advisor in enumerate(LIVE_ADVISORS)}
    return sorted(advisor_reports, key=lambda item: order.get(str(item.get("key")), 99))


def _compact_advisor_reports(advisor_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "key": item.get("key"),
            "advisor": item.get("advisor"),
            "status": item.get("status"),
            "structured_report": item.get("structured_report") or {},
        }
        for item in advisor_reports
    ]


def _failed_council(
    advisor_reports: list[dict[str, Any]] | None = None,
    raw_answer: str | None = None,
    council_mode: str = "council_failed",
    summary: str = "三诸葛亮实盘决策失败；未提交订单。",
    ai_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "summary": summary,
        "council_mode": council_mode,
        "advisor_reports": advisor_reports or [],
        "ai_execution": ai_execution or {},
        "raw_answer": raw_answer,
        "selections": [],
    }


def _finalize_allocation(
    items: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    stage: str,
) -> None:
    # Single source of truth for allocation normalization. Stage 'primary' is the
    # first pass; 'post_validation' is called only when the post-validator
    # actually rejected/repaired entries, and uses suffixed labels so downstream
    # consumers can tell which pass last touched each row.
    if not items:
        return
    suffix = "_post_validation" if stage == "post_validation" else ""

    def _apply_cap() -> None:
        # Cap any single position's share of the budget (0 = uncapped). Auto-trade
        # sets this per preset so the LLM can't over-concentrate the whole budget.
        cap = max(0.0, min(_coerce_float(config.get("max_allocation_pct_per_trade"), 0.0), 1.0))
        if cap <= 0:
            return
        for item in items:
            if _coerce_float(item.get("allocation_pct"), 0.0) > cap:
                item["allocation_pct"] = cap

    if not config.get("ai_adjust_allocation"):
        equal = 1 / len(items)
        for item in items:
            item["allocation_pct"] = equal
            item["allocation_source"] = f"equal_default{suffix}"
        _apply_cap()
        return
    total = sum(_coerce_float(item.get("allocation_pct"), 0.0) for item in items)
    if total <= 0:
        equal = 1 / len(items)
        for item in items:
            item["allocation_pct"] = equal
            item["allocation_source"] = f"equal_fallback{suffix}"
        _apply_cap()
        return
    if total > 1:
        for item in items:
            item["allocation_pct"] = _coerce_float(item.get("allocation_pct"), 0.0) / total
            item["allocation_source"] = f"ai_normalized{suffix}"
        _apply_cap()
        return
    zero_items = [item for item in items if _coerce_float(item.get("allocation_pct"), 0.0) <= 0]
    remaining = max(0.0, 1 - total)
    if zero_items and remaining > 0:
        fill = remaining / len(zero_items)
        for item in zero_items:
            item["allocation_pct"] = fill
            item["allocation_source"] = f"remaining_equal_fill{suffix}"
    _apply_cap()


def _normalize_selections(
    selections: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    top_n = int(config.get("top_n") or 5)
    by_contract = {
        _opportunity_contract_symbol(item): item
        for item in opportunities
        if _opportunity_contract_symbol(item)
    }
    normalized = []
    seen_contracts: set[str] = set()
    for selection in selections:
        contract_symbol = str(selection.get("contract_symbol") or "")
        source = by_contract.get(contract_symbol)
        if not source or contract_symbol in seen_contracts:
            continue
        normalized.append(_selection_record(source, selection, config, auto_filled=False))
        seen_contracts.add(contract_symbol)
        if len(normalized) >= top_n:
            break

    for source in opportunities:
        if len(normalized) >= top_n:
            break
        candidate = source.get("candidate") or {}
        contract_symbol = str(candidate.get("contract_symbol") or "")
        if not contract_symbol or contract_symbol in seen_contracts:
            continue
        normalized.append(
            _selection_record(
                source,
                {
                    "contract_symbol": contract_symbol,
                    "allocation_pct": 0,
                    "stop_loss_pct": config.get("default_stop_loss_pct"),
                    "take_profit_pct": config.get("default_take_profit_pct"),
                    "reason": "AI 补足未返回足够候选，系统按扫描分数兜底补齐到配置数量。",
                },
                config,
                auto_filled=True,
            )
        )
        seen_contracts.add(contract_symbol)

    if not normalized:
        return []

    _finalize_allocation(normalized, config, stage="primary")
    return normalized[:top_n]


def _post_validate_and_repair_selections(
    selections: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    top_n = min(int(config.get("top_n") or 5), len(opportunities))
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_contracts: set[str] = set()
    valid_contracts = {_opportunity_contract_symbol(item) for item in opportunities if _opportunity_contract_symbol(item)}

    for selection in selections:
        contract_symbol = str(selection.get("contract_symbol") or "")
        issues = _selection_hard_issues(selection, valid_contracts)
        if contract_symbol in seen_contracts:
            issues.append("duplicate_contract")
        if issues:
            rejected.append({"contract_symbol": contract_symbol, "issues": issues, "source": selection.get("selection_source")})
            continue
        accepted.append(selection)
        seen_contracts.add(contract_symbol)

    repaired_count = 0
    if len(accepted) < top_n:
        for opportunity in opportunities:
            if len(accepted) >= top_n:
                break
            contract_symbol = _opportunity_contract_symbol(opportunity)
            if not contract_symbol or contract_symbol in seen_contracts:
                continue
            # Don't auto-fund an opportunity the decision gate blocked. The repair
            # path runs _selection_hard_issues but NOT the decision-gate check, so
            # a gate-blocked / observe-only candidate could otherwise receive real
            # capital here. Skip those buckets.
            if str(opportunity.get("decision_bucket") or "") in {"observe_trigger_not_met", "blocked_execution"}:
                rejected.append({"contract_symbol": contract_symbol, "issues": ["decision_gate_blocked"], "source": "post_validator_repair"})
                continue
            repair = _selection_record(
                opportunity,
                {
                    "contract_symbol": contract_symbol,
                    "allocation_pct": 0,
                    "stop_loss_pct": config.get("default_stop_loss_pct"),
                    "take_profit_pct": config.get("default_take_profit_pct"),
                    "reason": "AI 选择未通过执行前硬校验，系统按合约级扫描分数从剩余候选补齐。",
                },
                config,
                auto_filled=True,
            )
            issues = _selection_hard_issues(repair, valid_contracts)
            if issues:
                rejected.append({"contract_symbol": contract_symbol, "issues": issues, "source": "post_validator_repair"})
                continue
            repair["selection_source"] = "post_validator_fill"
            repair["allocation_source"] = "post_validator_fill"
            accepted.append(repair)
            seen_contracts.add(contract_symbol)
            repaired_count += 1

    accepted = accepted[:top_n]
    # Only re-finalize when the post-validator actually mutated the selection set
    # (rejected something OR injected a repaired row). Otherwise the primary
    # normalization from _normalize_selections is already authoritative and
    # re-running it just flips labels nondeterministically.
    if accepted and (rejected or repaired_count):
        _finalize_allocation(accepted, config, stage="post_validation")
    return accepted, {
        "status": "passed" if not rejected and len(accepted) >= top_n else "repaired" if accepted else "blocked",
        "target_count": top_n,
        "input_selection_count": len(selections),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "repaired_count": repaired_count,
        "rejected": rejected[:20],
        "hard_rules": [
            "contract must exist in opportunities",
            "entry price must be positive",
            "spread_pct must be <= 35",
            "volume >= 100 or open_interest >= 1000",
            "execution_quality_score must be >= 25 when available",
            "stop_loss_pct must be within 1-95",
        ],
    }


def _selection_hard_issues(selection: dict[str, Any], valid_contracts: set[str]) -> list[str]:
    issues: list[str] = []
    contract_symbol = str(selection.get("contract_symbol") or "")
    candidate = selection.get("candidate") or {}
    if not contract_symbol or contract_symbol not in valid_contracts:
        issues.append("contract_not_in_opportunities")
    if _coerce_float(selection.get("entry_price"), 0.0) <= 0:
        issues.append("invalid_entry_price")
    if _coerce_float(selection.get("stop_loss_pct"), 0.0) < 1 or _coerce_float(selection.get("stop_loss_pct"), 0.0) > 95:
        issues.append("invalid_stop_loss_pct")
    spread_pct = _coerce_float(candidate.get("spread_pct"), 0.0)
    if spread_pct > 30:
        issues.append("wide_spread")
    volume = _coerce_float(candidate.get("volume"), 0.0)
    open_interest = _coerce_float(candidate.get("open_interest"), 0.0)
    if volume < 100 and open_interest < 1000:
        issues.append("thin_liquidity")
    execution_quality = _coerce_float(candidate.get("execution_quality_score"), 0.0)
    if execution_quality and execution_quality < 25:
        issues.append("poor_execution_quality")
    if str(candidate.get("pricing_source") or "") == "unavailable":
        issues.append("quote_unavailable")
    if str(candidate.get("decision_bucket") or "") in {"observe_trigger_not_met", "blocked_execution"}:
        issues.append(str(candidate.get("decision_bucket")))
    if _coerce_float(candidate.get("trigger_score"), 0.0) < 60:
        issues.append("trigger_not_met")
    if _coerce_float(candidate.get("time_value_risk_penalty"), 0.0) >= 12:
        issues.append("time_value_risk_high")
    return issues


def _opportunity_contract_symbol(opportunity: dict[str, Any]) -> str:
    return str(opportunity.get("contract_symbol") or (opportunity.get("candidate") or {}).get("contract_symbol") or "")


def _selection_record(
    source: dict[str, Any],
    selection: dict[str, Any],
    config: dict[str, Any],
    auto_filled: bool,
) -> dict[str, Any]:
    candidate = source["candidate"]
    contract_symbol = str(candidate["contract_symbol"])
    risk_plan = dict(candidate.get("risk_plan") or {})
    # Prefer LLM-authored smart exits from the selection (when the AI controls
    # stops); fall back to the candidate's pre-computed plan. Lets the auto-trade
    # LLM specify time/indicator/underlying exit_conditions per trade.
    if config.get("ai_adjust_stop_loss"):
        if selection.get("exit_conditions") is not None:
            risk_plan["exit_conditions"] = selection.get("exit_conditions")
        if str(selection.get("latest_exit") or "").strip():
            risk_plan["latest_exit"] = selection.get("latest_exit")
        if str(selection.get("invalidation") or "").strip():
            risk_plan["invalidation"] = selection.get("invalidation")
        if selection.get("allow_overnight") is not None:
            risk_plan["allow_overnight"] = selection.get("allow_overnight")
    stop_loss_pct = _coerce_float(selection.get("stop_loss_pct"), float(config.get("default_stop_loss_pct") or 25))
    take_profit_source = selection if config.get("ai_adjust_take_profit") else {}
    take_profit_settings = _take_profit_settings(config, take_profit_source)
    latest_exit = str(risk_plan.get("latest_exit") or "").strip()
    underlying_invalidation = str(risk_plan.get("invalidation") or "").strip()
    allow_overnight = bool(risk_plan.get("allow_overnight")) if risk_plan.get("allow_overnight") is not None else ("当日" not in latest_exit and "收盘前" not in latest_exit)
    if config.get("force_no_overnight"):
        allow_overnight = False
    if not config.get("ai_adjust_stop_loss"):
        stop_loss_pct = float(config.get("default_stop_loss_pct") or 25)
    entry_order_type = _normalize_entry_order_type(config.get("entry_order_type"))
    return {
        "symbol": source["symbol"],
        "contract_symbol": contract_symbol,
        "order_symbol": option_order_symbol(contract_symbol),
        "allocation_pct": max(0.0, _coerce_allocation(selection.get("allocation_pct"))),
        "allocation_source": "auto_fill" if auto_filled else selection.get("allocation_source") or "ai",
        "selection_source": "system_fallback" if auto_filled else selection.get("selection_source") or "ai_initial",
        "stop_loss_pct": max(1.0, min(stop_loss_pct, 95.0)),
        "take_profit_pct": take_profit_settings["single_pct"],
        "take_profit_1_pct": take_profit_settings["tp1_pct"],
        "take_profit_2_pct": take_profit_settings["tp2_pct"],
        "tiered_take_profit_enabled": take_profit_settings["tiered"],
        "take_profit_source": "ai" if config.get("ai_adjust_take_profit") and any(key in selection for key in ("take_profit_pct", "take_profit_1_pct", "take_profit_2_pct")) else "config_default",
        "entry_price": float(candidate.get("ask") or candidate.get("mid_price") or 0),
        "entry_order_type": entry_order_type,
        "entry_price_source": "candidate_estimate",
        "candidate": candidate,
        "latest_exit": latest_exit,
        "underlying_invalidation": underlying_invalidation,
        "allow_overnight": allow_overnight,
        "single_leg_exit_conditions": _single_leg_exit_conditions_from_risk_plan(risk_plan, latest_exit, underlying_invalidation, allow_overnight),
        "reason": selection.get("reason") or "",
    }


def _attach_execution_policy(
    council: dict[str, Any],
    selections: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(council)
    top_n = int(config.get("top_n") or 5)
    allocation_policy = "ai" if config.get("ai_adjust_allocation") else "equal_default"
    entry_order_type = _normalize_entry_order_type(config.get("entry_order_type"))
    updated["execution_policy"] = {
        "configured_top_n": top_n,
        "final_selection_count": len(selections),
        "allocation_policy": allocation_policy,
        "stop_loss_policy": "ai" if config.get("ai_adjust_stop_loss") else "default",
        "take_profit_policy": "ai" if config.get("ai_adjust_take_profit") else "default",
        "tiered_take_profit_enabled": bool(config.get("tiered_take_profit_enabled")),
        "default_take_profit_pct": _default_take_profit_pct(config),
        "default_take_profit_1_pct": _take_profit_settings(config)["tp1_pct"],
        "default_take_profit_2_pct": _take_profit_settings(config)["tp2_pct"],
        "entry_order_type": entry_order_type,
    }
    updated["execution_selections"] = [
        {
            "symbol": item.get("symbol"),
            "contract_symbol": item.get("contract_symbol"),
            "allocation_pct": item.get("allocation_pct"),
            "allocation_source": item.get("allocation_source"),
            "selection_source": item.get("selection_source"),
            "stop_loss_pct": item.get("stop_loss_pct"),
            "take_profit_pct": item.get("take_profit_pct"),
            "take_profit_1_pct": item.get("take_profit_1_pct"),
            "take_profit_2_pct": item.get("take_profit_2_pct"),
            "tiered_take_profit_enabled": item.get("tiered_take_profit_enabled"),
            "take_profit_source": item.get("take_profit_source"),
            "entry_order_type": item.get("entry_order_type"),
            "entry_price_source": item.get("entry_price_source"),
        }
        for item in selections
    ]
    fallback_count = sum(1 for item in selections if item.get("selection_source") == "system_fallback")
    ai_top_up_count = sum(1 for item in selections if item.get("selection_source") == "ai_top_up")
    post_validator_count = sum(1 for item in selections if item.get("selection_source") == "post_validator_fill")
    updated["execution_policy"].update(
        {
            "ai_initial_count": sum(1 for item in selections if item.get("selection_source") == "ai_initial"),
            "ai_top_up_count": ai_top_up_count,
            "system_fallback_count": fallback_count,
            "post_validator_fill_count": post_validator_count,
        }
    )
    if selections and fallback_count:
        updated["summary"] = (
            f"{updated.get('summary') or ''} 系统执行策略：主持人返回 "
            f"{len(council.get('selections') or [])} 个，已按 Top N={top_n} 从扫描池补齐到 "
            f"{len(selections)} 个。"
        ).strip()
    elif selections and ai_top_up_count:
        updated["summary"] = (
            f"{updated.get('summary') or ''} 系统执行策略：最终 Top N 中有 {ai_top_up_count} 个来自 AI 二次补足裁决。"
        ).strip()
    if selections and post_validator_count:
        updated["summary"] = (
            f"{updated.get('summary') or ''} 执行前硬校验剔除了不合格选择，并从合约级候选池补齐 {post_validator_count} 个。"
        ).strip()
    if selections and not config.get("ai_adjust_allocation"):
        updated["summary"] = (
            f"{updated.get('summary') or ''} 当前未开启 AI 调仓位，最终订单按入选合约等权分配。"
        ).strip()
    if selections:
        updated["summary"] = (
            f"{updated.get('summary') or ''} 入场方式：{ '市价单' if entry_order_type == 'market' else '限价单' }，"
            f"{'已跳过下单前询价' if entry_order_type == 'market' else '下单前会重新询价' }。"
        ).strip()
    return updated


def _submit_orders(selections: list[dict[str, Any]], config: dict[str, Any], account_name: str, run_id: str = "", owner_id: str = "") -> list[dict[str, Any]]:
    if not _single_leg_mode_allowed(config):
        return [
            {
                **item,
                "status": "skipped_single_leg_not_allowed",
                "message": "strategy_modes does not include single_leg; single-leg order submission is blocked",
            }
            for item in selections
        ]
    total_capital = float(config.get("total_capital") or 0)
    entry_order_type = _normalize_entry_order_type(config.get("entry_order_type"))
    exit_order_type = adaptive_pricing.normalize_order_type(config.get("exit_order_type"))
    orders = []
    for item in selections:
        mismatch = _candidate_contract_mismatch(item, item.get("symbol"))
        if mismatch:
            orders.append({
                **item,
                "status": "skipped_contract_symbol_mismatch",
                "message": f"contract symbol does not match selected underlying: {mismatch}",
            })
            continue
        original_entry_price = float(item.get("entry_price") or 0)
        if entry_order_type == "market":
            # Size market orders off a FRESH quote, not the stale scan-time ask —
            # a gapped estimate otherwise over/under-deploys capital. Fall back to
            # the scan price only if the requote is unavailable (a market order
            # can still execute, but log that sizing used a stale basis).
            market_requote = quote_option_contract(str(item.get("contract_symbol") or ""), account_name)
            if not _execution_quote_is_trusted(market_requote):
                orders.append({
                    **item,
                    "status": "skipped_untrusted_execution_quote",
                    "message": f"live entry requires ThetaData/Longbridge quote; got {market_requote.get('source') or 'unknown'}",
                    "entry_requote": market_requote,
                })
                continue
            fresh_ask = float(market_requote.get("ask") or market_requote.get("limit_price") or 0)
            sizing_price = fresh_ask if (market_requote.get("available") and fresh_ask > 0) else original_entry_price
            refreshed_quote = {
                "available": True,
                "mode": "market_order",
                "limit_price": round(sizing_price, 2) if sizing_price > 0 else 0,
                "sizing_basis": "fresh_quote" if (market_requote.get("available") and fresh_ask > 0) else "scan_estimate",
                "requote": market_requote,
                "message": "market order selected; sized off fresh quote" if fresh_ask > 0 else "market order selected; fresh quote unavailable, sized off scan estimate",
            }
            entry_price = sizing_price
        else:
            refreshed_quote = quote_option_contract(str(item.get("contract_symbol") or ""), account_name)
            if not _execution_quote_is_trusted(refreshed_quote):
                orders.append({
                    **item,
                    "status": "skipped_untrusted_execution_quote",
                    "message": f"live entry requires ThetaData/Longbridge quote; got {refreshed_quote.get('source') or 'unknown'}",
                    "entry_requote": refreshed_quote,
                })
                continue
            entry_price = float(refreshed_quote.get("limit_price") or original_entry_price or 0)
        allocation_amount = total_capital * float(item.get("allocation_pct") or 0)
        quantity = int(allocation_amount // (entry_price * 100)) if entry_price > 0 else 0
        order_record = {
            **item,
            "original_entry_price": original_entry_price,
            "planned_entry_price": entry_price,
            "entry_price": entry_price,
            "entry_requote": refreshed_quote,
            "entry_order_type": entry_order_type,
            "exit_order_type": exit_order_type,
            "market_data_source": config.get("market_data_source") or "yfinance",
            "allocation_amount": round(allocation_amount, 2),
            "quantity": quantity,
        }
        if entry_order_type != "market" and not refreshed_quote.get("available"):
            order_record["status"] = "skipped_requote_unavailable"
            order_record["message"] = refreshed_quote.get("error") or "fresh option quote unavailable before order submission"
            orders.append(order_record)
            continue
        if quantity < 1:
            order_record["status"] = "skipped_insufficient_allocation"
            orders.append(order_record)
            continue
        try:
            # "adaptive" requotes and submits a price like "limit", but prices
            # BETWEEN mid and the ask (walking toward the ask on reprice) instead
            # of always paying the full ask. Sizing still uses the conservative
            # ask (entry_price) so we never over-deploy. When the global flag is
            # off, adaptive silently degrades to a plain limit order.
            is_limit = entry_order_type in {"limit", "adaptive"}
            is_adaptive = entry_order_type == "adaptive" and _adaptive_order_enabled()
            broker_order_type = "limit" if is_limit else "market"
            max_attempts = 1 + (_entry_reprice_attempts() if is_limit else 0)
            configured_wait = int(config.get("wait_for_fill_seconds") or 0)
            fill_wait = max(5 if is_limit else 1, min(configured_wait or (5 if is_limit else 0), 60))
            stop_basis_price = entry_price
            entry_attempts: list[dict[str, Any]] = []
            entry_order: dict[str, Any] = {}
            entry_detail: dict[str, Any] = {}
            order_id: str | None = None
            filled_quantity = 0
            entry_status = ""
            actual_entry_price = 0.0
            reprice_error: str | None = None
            # Adaptive opens at the walked mid-ward price; limit opens at the ask.
            if is_adaptive:
                adaptive_open = _adaptive_limit_price(refreshed_quote, "buy", _adaptive_aggr_for_attempt(0, max_attempts))
                current_entry_price = adaptive_open if adaptive_open > 0 else entry_price
            else:
                current_entry_price = entry_price
            cok = client_order_key(run_id, str(item.get("contract_symbol") or item["symbol"]), "entry") if _idempotency_enabled() and run_id else ""
            cok_tag = f" [cok:{cok}]" if cok else ""
            for attempt_idx in range(max_attempts):
                suffix_tag = f"rq={current_entry_price:.2f}" if is_limit else f"mo est={current_entry_price:.2f}"
                attempt_tag = "" if attempt_idx == 0 else f" reprice{attempt_idx}"
                remark = f"AI_OPTION_ENTRY {item['symbol']} {item['stop_loss_pct']:.1f}% {suffix_tag}{attempt_tag}{cok_tag}".strip()
                reconciled = _reconcile_recent_strategy_submission(cok, account_name) if cok else None
                if reconciled:
                    entry_order, entry_detail = reconciled
                else:
                    record_order_journal(
                        owner_id=owner_id, run_id=run_id, client_order_key=cok or None,
                        action="entry_buy", phase="before", account_ref=account_name,
                        symbol=item.get("order_symbol"), side="buy", quantity=quantity,
                        price=current_entry_price if is_limit else None,
                        detail={"attempt": attempt_idx + 1, "order_type": entry_order_type},
                    )
                    entry_order = submit_buy_order(
                        item["order_symbol"],
                        quantity,
                        current_entry_price if is_limit else None,
                        account_name,
                        remark,
                        order_type=broker_order_type,
                    )
                    entry_detail = {}
                order_id = _order_id(entry_order)
                record_order_journal(
                    owner_id=owner_id, run_id=run_id, client_order_key=cok or None,
                    action="entry_buy", phase="after", account_ref=account_name,
                    symbol=item.get("order_symbol"), side="buy", quantity=quantity,
                    order_id=order_id, status=str(_order_status(entry_order) or ""),
                )
                attempt_record: dict[str, Any] = {
                    "attempt": attempt_idx + 1,
                    "order_id": order_id,
                    "submitted_price": round(current_entry_price, 4) if current_entry_price > 0 else 0,
                    "order_type": entry_order_type,
                }
                if order_id and fill_wait > 0:
                    entry_detail = entry_detail or wait_for_order_fill(order_id, account_name, fill_wait)
                    filled_quantity = _filled_quantity(entry_detail)
                    entry_status = _order_status(entry_detail) or ""
                    actual_entry_price = _executed_price(entry_detail)
                else:
                    entry_detail = {}
                    filled_quantity = 0
                    entry_status = ""
                    actual_entry_price = 0.0
                attempt_record["status"] = str(entry_status or "").lower()
                attempt_record["filled_quantity"] = filled_quantity
                entry_attempts.append(attempt_record)
                record_order_journal(
                    owner_id=owner_id, run_id=run_id, client_order_key=cok or None,
                    action="entry_buy", phase="fill", account_ref=account_name,
                    symbol=item.get("order_symbol"), side="buy", quantity=quantity,
                    order_id=order_id, status=str(entry_status or ""),
                    detail={"filled_quantity": filled_quantity, "executed_price": actual_entry_price},
                )
                if filled_quantity > 0 or _status_is_filled(entry_status):
                    break
                cancel_result = _cancel_order_safely(order_id, account_name)
                if cancel_result is not None:
                    attempt_record["cancel"] = cancel_result
                if not is_limit or attempt_idx + 1 >= max_attempts:
                    break
                new_price, fresh_quote = _reprice_leg_from_quote(str(item.get("contract_symbol") or ""), "buy", account_name)
                attempt_record["reprice_quote"] = fresh_quote
                if new_price <= 0:
                    reprice_error = "reprice quote unavailable"
                    break
                if is_adaptive:
                    # Walk toward the ask: next attempt is more aggressive, the
                    # final attempt is marketable (aggr=1.0 -> ask).
                    walked = _adaptive_limit_price(fresh_quote, "buy", _adaptive_aggr_for_attempt(attempt_idx + 1, max_attempts))
                    current_entry_price = walked if walked > 0 else round(new_price, 2)
                else:
                    current_entry_price = round(new_price, 2)
            order_record["entry_order"] = entry_order
            order_record["entry_attempts"] = entry_attempts
            if entry_detail:
                order_record["entry_detail"] = entry_detail
            if actual_entry_price > 0:
                stop_basis_price = actual_entry_price
                order_record["planned_entry_price"] = entry_price
                order_record["entry_price"] = actual_entry_price
                order_record["actual_entry_price"] = actual_entry_price
                order_record["entry_price_source"] = "executed_price"
            elif current_entry_price != entry_price:
                stop_basis_price = current_entry_price
                order_record["planned_entry_price"] = entry_price
                order_record["entry_price"] = current_entry_price
            stop_price = round(stop_basis_price * (1 - float(item["stop_loss_pct"]) / 100), 2)
            order_record["stop_trigger_price"] = stop_price
            if filled_quantity < 1:
                if _is_terminal_unfilled_status(entry_status):
                    order_record["status"] = "entry_terminal_no_stop"
                    order_record["message"] = f"entry order ended as `{entry_status}` without any fill; no stop order was submitted"
                    if reprice_error:
                        order_record["message"] += f"; reprice aborted: {reprice_error}"
                    orders.append(order_record)
                    continue
                order_record["status"] = "entry_submitted_stop_pending_unfilled"
                order_record["message"] = "entry order was not confirmed filled; stop order was not submitted to avoid an uncovered sell"
                if reprice_error:
                    order_record["message"] += f"; reprice aborted: {reprice_error}"
                orders.append(order_record)
                continue
            covered_quantity = min(quantity, filled_quantity)
            order_record["entry_filled_quantity"] = filled_quantity
            _arm_software_take_profit(order_record, filled_quantity, config)
            try:
                stop_order = submit_stop_sell_order(
                    item["order_symbol"],
                    covered_quantity,
                    stop_price,
                    account_name,
                    f"AI_OPTION_STOP {item['symbol']} entry={order_id or 'unknown'}",
                )
            except Exception as exc:  # noqa: BLE001
                if _is_stop_unsupported(exc):
                    order_record["status"] = "entry_filled_stop_unsupported_paper"
                    order_record["covered_quantity"] = 0
                    order_record["monitor_status"] = "completed"
                    order_record["stop_error"] = str(exc)
                    order_record["message"] = "买单已成交，但该券商不支持券商触发止损；实例将使用软件保护。"
                    _arm_software_stop(order_record, filled_quantity, config, "broker_stop_unsupported")
                    orders.append(order_record)
                    continue
                raise
            order_record["stop_order"] = stop_order
            order_record["stop_orders"] = [stop_order]
            order_record["covered_quantity"] = covered_quantity
            if covered_quantity < quantity:
                order_record["status"] = "entry_partially_filled_stop_partial"
                order_record["message"] = "买单部分成交，系统只保护已成交数量，并继续等待剩余成交。"
            else:
                order_record["status"] = "submitted"
        except Exception as exc:  # noqa: BLE001
            order_record["status"] = "failed"
            order_record["error"] = str(exc)
            filled = int(locals().get("filled_quantity") or 0)
            if filled > 0:
                order_record["entry_filled_quantity"] = filled
                order_record["covered_quantity"] = int(order_record.get("covered_quantity") or 0)
                order_record["stop_error"] = str(exc)
                _arm_software_stop(order_record, filled, config, "broker_stop_submit_failed")
        orders.append(order_record)
    return orders


def _pre_submit_risk_breaker_issues(config: dict[str, Any], selections: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    max_stop = float(config.get("risk_max_single_stop_loss_pct") or 45)
    if any(float(item.get("stop_loss_pct") or 0) > max_stop for item in selections):
        issues.append(f"风控熔断：入选合约止损超过 {max_stop:g}%")
    if str(config.get("entry_order_type") or "market") == "market" and config.get("risk_require_protection_for_market_order"):
        if not config.get("software_stop_enabled") and not config.get("software_take_profit_enabled"):
            issues.append("风控熔断：市价单需要开启软件止损或软件止盈保护")
    total_allocation = sum(float(item.get("allocation_pct") or 0) for item in selections)
    if total_allocation > 1.05:
        issues.append("风控熔断：总仓位分配超过 105%")
    return issues


def _single_leg_order_outcome(orders: list[dict[str, Any]]) -> dict[str, str | None]:
    if not orders:
        return {"status": "failed", "stage": "order_submission_failed", "error": "no single-leg orders were produced"}
    active_statuses = {
        "submitted",
        "stop_submitted_after_fill",
        "entry_partially_filled_stop_partial",
        "entry_filled_stop_unsupported_paper",
        "entry_submitted_stop_pending_unfilled",
    }
    has_live_order = False
    failures: list[str] = []
    for order in orders:
        status = str(order.get("status") or "").strip()
        filled_quantity = int(_coerce_float(order.get("entry_filled_quantity"), 0))
        if status in active_statuses or filled_quantity > 0:
            has_live_order = True
        elif status:
            failures.append(str(order.get("error") or order.get("message") or status))
    if has_live_order:
        return {"status": "succeeded", "stage": "completed", "error": None}
    return {
        "status": "failed",
        "stage": "order_submission_failed",
        "error": "; ".join(failures[:5]) or "all single-leg orders failed or were skipped",
    }


def _default_take_profit_pct(config: dict[str, Any]) -> float:
    return max(1.0, min(_coerce_float(config.get("default_take_profit_pct"), 30.0), 500.0))


def _take_profit_settings(config: dict[str, Any], source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = source or {}
    tiered = bool(config.get("tiered_take_profit_enabled", False))
    single_pct = max(1.0, min(_coerce_float(source.get("take_profit_pct"), _default_take_profit_pct(config)), 500.0))
    tp1_pct = max(1.0, min(_coerce_float(source.get("take_profit_1_pct"), _coerce_float(config.get("default_take_profit_1_pct"), 20.0)), 500.0))
    tp2_pct = max(tp1_pct, min(_coerce_float(source.get("take_profit_2_pct"), _coerce_float(config.get("default_take_profit_2_pct"), 35.0)), 500.0))
    return {
        "tiered": tiered,
        "single_pct": single_pct,
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
    }


def _strategy_take_profit_pnls(profit_basis: float, config: dict[str, Any], source: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = _take_profit_settings(config, source)
    basis = max(float(profit_basis or 0), 1.0)
    if settings["tiered"]:
        return {
            "tiered_take_profit_enabled": True,
            "take_profit_pct": settings["single_pct"],
            "take_profit_1_pct": settings["tp1_pct"],
            "take_profit_2_pct": settings["tp2_pct"],
            "take_profit_1_pnl": round(basis * settings["tp1_pct"] / 100, 2),
            "take_profit_2_pnl": round(basis * settings["tp2_pct"] / 100, 2),
        }
    return {
        "tiered_take_profit_enabled": False,
        "take_profit_pct": settings["single_pct"],
        "take_profit_1_pct": settings["single_pct"],
        "take_profit_2_pct": 0.0,
        "take_profit_1_pnl": round(basis * settings["single_pct"] / 100, 2),
        "take_profit_2_pnl": 0.0,
    }


def _single_leg_exit_conditions_from_risk_plan(
    risk_plan: dict[str, Any],
    latest_exit: str,
    underlying_invalidation: str,
    allow_overnight: bool,
) -> list[dict[str, Any]]:
    return normalize_exit_rules(
        raw_conditions=risk_plan.get("exit_conditions"),
        latest_exit=latest_exit,
        invalidation=underlying_invalidation,
        allow_overnight=allow_overnight,
        position={"latest_exit": latest_exit},
    )


def _normalize_strategy_exit_conditions(raw_conditions: Any, position: dict[str, Any]) -> list[dict[str, Any]]:
    return normalize_exit_rules(
        raw_conditions=raw_conditions,
        latest_exit=str(position.get("latest_exit") or ""),
        invalidation=str(position.get("invalidation") or ""),
        allow_overnight=position.get("allow_overnight"),
        position=position,
    )


def _normalize_strategy_latest_exit(position: dict[str, Any]) -> str:
    latest_exit = str(position.get("latest_exit") or "").strip()
    if not latest_exit:
        return ""
    return normalize_latest_exit(latest_exit, position)


def _normalize_strategy_exit_time_et(time_text: str, position: dict[str, Any]) -> str:
    return normalize_latest_exit(time_text, position)


def _parse_strategy_exit_datetime(text: str, position: dict[str, Any]):
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    parsed = parse_datetime(cleaned)
    if parsed is not None:
        return parsed
    if re.search(r"\d{1,2}:\d{2}", cleaned):
        expiration = str(position.get("expiration") or "")
        base = parse_datetime(expiration)
        if base is None:
            base = datetime.now(timezone.utc)
        time_match = re.search(r"(\d{1,2}):(\d{2})", cleaned)
        hour = int(time_match.group(1)) if time_match else 15
        minute = int(time_match.group(2)) if time_match else 50
        from .time_utils import EASTERN

        base_et = base.astimezone(EASTERN)
        return base_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if "收盘前" in cleaned or "当日" in cleaned:
        from .time_utils import EASTERN

        now = datetime.now(EASTERN)
        return now.replace(hour=15, minute=50, second=0, microsecond=0)
    if "到期前" in cleaned and "1 个交易日" in cleaned:
        from .time_utils import EASTERN

        expiration = str(position.get("expiration") or "")
        base = parse_datetime(expiration)
        if base is None:
            return None
        return base.astimezone(EASTERN).replace(hour=15, minute=50, second=0, microsecond=0) - timedelta(days=1)
    return None


def _parse_strategy_invalidation(text: str, position: dict[str, Any]) -> dict[str, Any] | None:
    return infer_invalidation_rule(text, position)


def _extract_first_number(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _arm_software_stop(order_record: dict[str, Any], quantity: int, config: dict[str, Any], reason: str) -> None:
    if not config.get("software_stop_enabled", True):
        order_record["software_stop_active"] = False
        order_record["software_stop_status"] = "disabled"
        return
    stop_price = float(order_record.get("stop_trigger_price") or 0)
    if quantity < 1 or stop_price <= 0:
        order_record["software_stop_active"] = False
        order_record["software_stop_status"] = "not_armed_invalid_quantity_or_stop"
        return
    order_record["software_stop_active"] = True
    order_record["software_stop_status"] = "armed"
    order_record["software_stop_quantity"] = int(quantity)
    order_record["software_stop_reason"] = reason
    order_record["software_stop_armed_at"] = utc_now()


def _arm_software_take_profit(order_record: dict[str, Any], quantity: int, config: dict[str, Any]) -> None:
    if not config.get("software_take_profit_enabled", True):
        order_record["software_take_profit_active"] = False
        order_record["software_take_profit_status"] = "disabled"
        return
    if quantity < 1:
        order_record["software_take_profit_active"] = False
        order_record["software_take_profit_status"] = "not_armed_no_filled_quantity"
        return
    entry_price = float(order_record.get("entry_price") or 0)
    settings = _take_profit_settings(config, order_record)
    tp1_pct = settings["tp1_pct"] if settings["tiered"] else settings["single_pct"]
    tp2_pct = settings["tp2_pct"]
    tp1 = entry_price * (1 + tp1_pct / 100) if entry_price > 0 else 0
    tp2 = entry_price * (1 + tp2_pct / 100) if entry_price > 0 and settings["tiered"] else 0
    targets = []
    if not settings["tiered"] and tp1 > 0:
        targets.append({"name": "take_profit", "price": round(tp1, 2), "quantity": int(quantity), "status": "pending"})
    elif quantity == 1 and tp1 > 0:
        targets.append({"name": "tp1", "price": round(tp1, 2), "quantity": 1, "status": "pending"})
    elif quantity >= 2 and tp1 > 0:
        targets.append({"name": "tp1", "price": round(tp1, 2), "quantity": max(1, quantity // 2), "status": "pending"})
    remaining_quantity = quantity - sum(int(item["quantity"]) for item in targets)
    if remaining_quantity > 0 and tp2 > 0:
        targets.append({"name": "tp2", "price": round(tp2, 2), "quantity": remaining_quantity, "status": "pending"})
    if not targets:
        order_record["software_take_profit_active"] = False
        order_record["software_take_profit_status"] = "not_armed_invalid_targets"
        return
    order_record["software_take_profit_active"] = True
    order_record["software_take_profit_status"] = "armed"
    order_record["software_take_profit_quantity"] = sum(int(item["quantity"]) for item in targets)
    order_record["software_take_profit_targets"] = targets
    order_record["software_take_profit_pct"] = settings["single_pct"]
    order_record["tiered_take_profit_enabled"] = settings["tiered"]
    order_record["take_profit_1_pct"] = settings["tp1_pct"]
    order_record["take_profit_2_pct"] = settings["tp2_pct"]
    order_record["software_take_profit_source"] = order_record.get("take_profit_source") or "config_default"
    order_record["software_take_profit_armed_at"] = utc_now()


def _prompt_for_symbol(template: str, symbol: str) -> str:
    if "{symbol}" in template:
        return template.format(symbol=symbol)
    return f"扫描{symbol}，{template}"


def _extract_json(answer: str | None) -> dict[str, Any] | None:
    if not answer:
        return None
    text = answer.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _order_id(payload: dict[str, Any]) -> str | None:
    for key in ("order_id", "id", "orderId"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _coerce_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip().rstrip("%")
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _candidate_sort_score(candidate: dict[str, Any]) -> float:
    return _coerce_float(candidate.get("decision_score"), _coerce_float(candidate.get("analysis_score"), 0.0))


def _coerce_allocation(value: Any) -> float:
    if isinstance(value, str) and value.strip().endswith("%"):
        return _coerce_float(value, 0.0) / 100
    return _coerce_float(value, 0.0)


def _status_is_filled(status: Any) -> bool:
    """Token-aware fill check. ``"filled" in status`` is WRONG: uSMART reports
    ``"unfilled"`` / ``"partially_filled"`` which both contain that substring, so
    a bare substring test treats an unfilled leg as filled (→ submits the next
    leg into a broken structure, or breaks a reprice loop before cancelling a
    live limit order → naked position). A genuine fill is a "filled" token that
    is not "unfilled"/"partial*"."""
    text = str(status or "").strip().lower()
    return "filled" in text and "unfilled" not in text and "partial" not in text


def _filled_quantity(payload: dict[str, Any]) -> int:
    for key in ("executed_quantity", "filled_quantity", "filled_qty", "quantity_filled"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    status = str(payload.get("status") or payload.get("order_status") or "").lower()
    # Only infer full quantity from the status when it is a genuine filled
    # state — never for "unfilled" / "partial*" (which contain the substring
    # "filled" but are not complete fills).
    if _status_is_filled(status):
        for key in ("quantity", "submitted_quantity", "qty"):
            value = payload.get(key)
            if value is None:
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
    return 0


def _executed_price(payload: dict[str, Any]) -> float:
    for key in ("executed_price", "filled_avg_price", "filled_price", "average_price", "avg_price", "price"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _order_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or payload.get("order_status") or "").strip()


def _is_terminal_unfilled_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {"rejected", "canceled", "cancelled", "expired"}


def _is_stop_unsupported(error: Exception | str) -> bool:
    # Two brokers can reject a native stop: Alpaca paper (code 604050) and uSMART
    # (no broker-side stops at all -> "native_stop_unsupported"). Both must route
    # to the software-stop fallback, not the generic failed path.
    message = str(error).lower()
    return ("604050" in message and "paper account" in message) or "native_stop_unsupported" in message


def _normalize_entry_order_type(value: Any) -> str:
    return adaptive_pricing.normalize_order_type(value)


def validate_trading_readiness(
    owner_id: str,
    config: dict[str, Any],
    require_ai: bool = False,
    force_session_check: bool = True,
) -> dict[str, Any]:
    normalized = normalize_trading_config(config)
    issues: list[str] = []
    warnings: list[str] = []

    if not normalized.get("live_enabled"):
        issues.append("live trading is disabled")
    if float(normalized.get("total_capital") or 0) <= 0:
        issues.append("total_capital must be greater than 0")
    if not normalized.get("universe"):
        issues.append("universe is empty")
    broker = normalize_broker(normalized.get("broker"))
    account_name = None
    session = {"token": "missing"}
    if broker == "alpaca":
        if not normalized.get("broker_account"):
            issues.append("broker_account is required for Alpaca scheduled live trading")
        else:
            try:
                account_ref = account_ref_for_config(normalized, owner_id=owner_id)
                account_name = display_account_name(account_ref)
                status = broker_check(account_ref) if force_session_check else {"session": {"token": "valid"}}
                session = status.get("session") or {}
                if str(session.get("token") or "").lower() != "valid":
                    issues.append(f"Alpaca credentials are not valid for account `{account_name}`")
            except Exception as exc:  # noqa: BLE001
                issues.append(str(exc))
    elif broker == "usmart":
        if not normalized.get("broker_account"):
            issues.append("broker_account is required for uSMART scheduled live trading")
        else:
            try:
                account_ref = account_ref_for_config(normalized, owner_id=owner_id)
                account_name = display_account_name(account_ref)
                status = broker_check(account_ref) if force_session_check else {"session": {"token": "valid"}}
                session = status.get("session") or {}
                if str(session.get("token") or "").lower() != "valid":
                    issues.append(f"uSMART credentials are not valid for account `{account_name}`")
            except Exception as exc:  # noqa: BLE001
                issues.append(str(exc))
    else:
        if not normalized.get("longbridge_account"):
            issues.append("longbridge_account is required for scheduled live trading")
        else:
            account = resolve_account(str(normalized.get("longbridge_account")), owner_id=owner_id)
            account_name = account.name
            status = auth_manager.status(account.name, owner_id=owner_id, force=force_session_check)
            session = status.get("session") or {}
            if str(session.get("token") or "").lower() != "valid":
                issues.append(f"Longbridge session is not valid for account `{account.name}`")

    provider_name = str(normalized.get("ai_provider") or "")
    ai_required = bool(require_ai or normalized.get("use_ai", True))
    provider = get_user_provider(owner_id, provider_name) or load_providers().get(provider_name)
    if ai_required and provider is None:
        issues.append(f"AI provider `{provider_name}` is not configured")
    elif ai_required and not (provider.api_key or os.getenv(provider.api_key_env)):
        issues.append(f"missing env var `{provider.api_key_env}` for AI provider `{provider_name}`")

    breaker = _readiness_risk_breakers(owner_id, normalized)
    issues.extend(breaker["issues"])
    warnings.extend(breaker["warnings"])

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "account_name": account_name,
        "session": session,
        "config": normalized,
        "risk_breakers": breaker,
    }


def _readiness_risk_breakers(owner_id: str, config: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    snapshot = trading_readiness_risk_snapshot(owner_id, recent_limit=50)
    max_daily_runs = int(config.get("risk_max_daily_runs") or 3)
    today_run_count = int(snapshot.get("today_run_count") or 0)
    if today_run_count >= max_daily_runs:
        issues.append(f"风控熔断：今日交易实例数量已达上限（{today_run_count}/{max_daily_runs}）")

    max_failures = int(config.get("risk_max_consecutive_failures") or 2)
    consecutive_failures = int(snapshot.get("consecutive_failures") or 0)
    if consecutive_failures >= max_failures:
        run_ids = _risk_snapshot_ids(snapshot.get("consecutive_failure_runs"))
        suffix = f"（{run_ids}）" if run_ids else ""
        issues.append(f"风控熔断：今日连续失败实例已达 {consecutive_failures} 次{suffix}")

    manual_attention = int(snapshot.get("manual_attention_count") or 0)
    if manual_attention > 0:
        run_ids = _risk_snapshot_ids(snapshot.get("manual_attention_runs"))
        suffix = f"：{run_ids}" if run_ids else ""
        issues.append(f"风控熔断：仍有 {manual_attention} 个交易实例需要人工处理{suffix}")

    max_unprotected = int(config.get("risk_max_unprotected_quantity") or 0)
    active_unprotected = int(snapshot.get("active_unprotected_quantity") or 0)
    if active_unprotected > max_unprotected:
        run_ids = _risk_snapshot_ids(snapshot.get("active_unprotected_runs"))
        suffix = f"（{run_ids}）" if run_ids else ""
        issues.append(f"风控熔断：当前未保护数量 {active_unprotected} 超过上限 {max_unprotected}{suffix}")

    max_stop = float(config.get("risk_max_single_stop_loss_pct") or 45)
    if float(config.get("default_stop_loss_pct") or 0) > max_stop:
        issues.append(f"风控熔断：默认止损超过 {max_stop:g}%")
    if str(config.get("entry_order_type") or "market") == "market" and config.get("risk_require_protection_for_market_order"):
        if not config.get("software_stop_enabled") and not config.get("software_take_profit_enabled"):
            issues.append("风控熔断：市价单需要开启软件止损或软件止盈保护")
    if not config.get("software_stop_enabled"):
        warnings.append("提醒：软件止损已关闭")
    return {
        "issues": issues,
        "warnings": warnings,
        "today_run_count": today_run_count,
        "max_daily_runs": max_daily_runs,
        "consecutive_failures": consecutive_failures,
        "max_consecutive_failures": max_failures,
        "active_unprotected_quantity": active_unprotected,
        "max_unprotected_quantity": max_unprotected,
        "active_run_count": int(snapshot.get("active_run_count") or 0),
        "manual_attention_count": int(snapshot.get("manual_attention_count") or 0),
        "sampled_recent_count": int(snapshot.get("sampled_recent_count") or 0),
    }


def _risk_snapshot_ids(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    ids = [str(row.get("id") or "")[:12] for row in rows if isinstance(row, dict) and row.get("id")]
    return ", ".join(ids[:5])


_executor = ThreadPoolExecutor(
    max_workers=env_int("AI_OPTION_LIVE_TRADING_WORKERS", 1, 1, 4),
    thread_name_prefix="live-trading",
)


@atexit.register
def _shutdown_live_trading_executor() -> None:
    # Best-effort: cancel pending futures so SIGTERM/SIGINT does not leave
    # background trading jobs running past process teardown.
    try:
        _executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

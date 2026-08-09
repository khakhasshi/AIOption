from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from typing import Any

from .ai_client import ask_ai
from .intraday_option_tools import normalize_analysis_modules
from .query_parser import ScanIntent, parse_query
from .strategy_structures import normalize_strategy_modes
from .time_utils import time_context


RULE_ANALYSIS_PRESETS: list[dict[str, Any]] = [
    {
        "key": "intraday_momentum",
        "label": "日内动量",
        "label_en": "Intraday Momentum",
        "description": "适合开盘后按 ORB / VWAP / EMA / RVOL / MACD 触发的短线单腿或价差。",
        "description_en": "For short-term single-leg or spread plays triggered after the open by ORB / VWAP / EMA / RVOL / MACD.",
        "aliases": ["日内动量", "日内交易", "开盘突破", "开盘区间", "orb", "vwap", "rvol", "分时"],
        "query_template": "扫描{symbol}日内动量期权，参考15m ORB、VWAP、EMA9/20、RVOL、MACD和执行质量，未触发只进观察池",
        "intent": {"day_trade": True, "min_days": 0, "max_days": 7, "max_ask": 6.0, "horizon_label": "weekly"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "cheap_lottery",
        "label": "便宜彩票",
        "label_en": "Cheap Lottery",
        "description": "寻找低权利金、高凸性的单腿候选，但会惩罚 theta 过高和达平衡概率过低。",
        "description_en": "Hunts for low-premium, high-convexity single legs, but penalizes excessive theta and low breakeven probability.",
        "aliases": ["便宜彩票", "彩票", "低价", "低成本", "一美元", "$1", "高赔率", "暴击"],
        "query_template": "扫描{symbol}便宜一点、稍微彩票一点的单腿期权，优先低权利金和高赔率，但必须过滤价差、流动性和theta风险",
        "intent": {"cheap": True, "lottery": True, "min_days": 0, "max_days": 24, "max_ask": 1.25, "horizon_label": "slightly_far"},
        "strategy_modes": ["single_leg"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "scenario": True, "risk": True},
    },
    {
        "key": "slightly_far_swing",
        "label": "稍远波段",
        "label_en": "Slightly-Far Swing",
        "description": "适合 7-24 DTE 的方向性波段，优先兼顾 theta、概率和执行质量。",
        "description_en": "For 7-24 DTE directional swings, balancing theta, probability, and execution quality.",
        "aliases": ["稍微远", "远一点", "稍远", "下周", "两周", "一两周", "7-24"],
        "query_template": "扫描{symbol}稍微远一点的期权，DTE控制在7-24天，兼顾方向、theta、概率、GEX和执行质量",
        "intent": {"min_days": 7, "max_days": 24, "horizon_label": "slightly_far"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "scenario": True, "risk": True},
    },
    {
        "key": "explosive_single_leg",
        "label": "爆发单腿",
        "label_en": "Explosive Single-Leg",
        "description": "寻找高 gamma、高弹性、可快速放大的单腿；只在突破/VWAP/动量触发后进入可交易池。",
        "description_en": "Seeks high-gamma, high-elasticity single legs that scale fast; only enters the tradable pool after a breakout/VWAP/momentum trigger.",
        "aliases": ["爆发单腿", "爆发力", "高爆发", "强爆发", "暴力单腿", "高gamma", "gamma爆发", "convexity", "凸性"],
        "query_template": "扫描{symbol}爆发力强的单腿期权，优先高gamma、高赔率、方向触发明确的合约，必须检查ORB、VWAP、RVOL、MACD、价差和theta风险",
        "intent": {"lottery": True, "min_days": 0, "max_days": 14, "max_ask": 4.0, "horizon_label": "near_term"},
        "strategy_modes": ["single_leg"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "low_theta_single_leg",
        "label": "低衰减单腿",
        "label_en": "Low-Decay Single-Leg",
        "description": "寻找 theta/ask 压力较小、DTE 稍长、方向容错更高的单腿。",
        "description_en": "Seeks single legs with lower theta/ask pressure, slightly longer DTE, and more directional tolerance.",
        "aliases": ["低衰减", "衰减弱", "theta低", "theta小", "低theta", "慢衰减", "不烧theta", "时间价值压力小"],
        "query_template": "扫描{symbol}衰减弱一点的单腿期权，DTE放到14-45天，优先theta/ask低、delta适中、流动性强、执行质量好的合约",
        "intent": {"cheap": False, "lottery": False, "min_days": 14, "max_days": 45, "max_ask": 10.0, "horizon_label": "monthly"},
        "strategy_modes": ["single_leg"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "gamma_squeeze_breakout",
        "label": "Gamma挤压",
        "label_en": "Gamma Squeeze",
        "description": "寻找负 GEX、关键位突破、成交放大的顺势加速机会，优先单腿和 debit spread。",
        "description_en": "Finds trend-acceleration setups with negative GEX, key-level breakouts, and volume expansion; prefers single legs and debit spreads.",
        "aliases": ["gamma挤压", "gamma squeeze", "逼空", "挤压", "负gex", "顺势加速", "加速突破"],
        "query_template": "扫描{symbol}Gamma挤压型机会，重点检查负GEX、gamma flip、call/put wall、ORB突破、VWAP站稳和RVOL放大，优先单腿或debit spread",
        "intent": {"day_trade": True, "min_days": 0, "max_days": 21, "max_ask": 6.0, "horizon_label": "near_term"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "vwap_reclaim_reversal",
        "label": "VWAP反转",
        "label_en": "VWAP Reversal",
        "description": "适合早盘下探后收复 VWAP 或冲高后跌破 VWAP 的日内反转流派。",
        "description_en": "For intraday reversals that reclaim VWAP after an early dip or reject VWAP after a spike.",
        "aliases": ["vwap反转", "收复vwap", "跌破vwap", "均价线反转", "日内反转", "reclaim", "reject vwap"],
        "query_template": "扫描{symbol}VWAP反转型单腿机会，要求价格相对VWAP出现收复或拒绝信号，并结合EMA9/20、MACD、RVOL和止损失效位",
        "intent": {"day_trade": True, "min_days": 0, "max_days": 10, "max_ask": 5.0, "horizon_label": "near_term_intraday_context"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "gap_go_intraday",
        "label": "跳空延续",
        "label_en": "Gap-and-Go",
        "description": "适合开盘跳空后不回补、VWAP 同向、量能放大的 gap-and-go。",
        "description_en": "For gap-and-go setups where the open gap doesn't fill, VWAP agrees with direction, and volume expands.",
        "aliases": ["跳空延续", "gap go", "gap-and-go", "不回补", "缺口延续", "开盘强势", "开盘弱势"],
        "query_template": "扫描{symbol}跳空延续型期权，检查开盘缺口后是否站稳/跌破VWAP、15m ORB方向、RVOL放大和执行质量，优先日内单腿",
        "intent": {"day_trade": True, "min_days": 0, "max_days": 7, "max_ask": 5.0, "horizon_label": "weekly"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "pullback_continuation",
        "label": "回踩延续",
        "label_en": "Pullback Continuation",
        "description": "适合趋势中回踩 EMA/VWAP 后再启动，追求比直接追突破更好的入场位置。",
        "description_en": "For trends that pull back to EMA/VWAP before resuming, seeking a better entry than chasing the breakout.",
        "aliases": ["回踩延续", "回踩买入", "回踩做多", "反弹延续", "趋势回踩", "buy the dip", "pullback"],
        "query_template": "扫描{symbol}趋势回踩延续型期权，重点检查日线趋势、VWAP/EMA回踩、MACD再扩张、低价差和theta风险，优先单腿或debit spread",
        "intent": {"min_days": 7, "max_days": 30, "max_ask": 7.0, "horizon_label": "slightly_far"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "breakdown_followthrough_put",
        "label": "破位追空",
        "label_en": "Breakdown Follow-Through",
        "description": "适合跌破 ORL、VWAP 下方、EMA 空头排列后的 put 或 bear put spread。",
        "description_en": "For puts or bear put spreads after a break below ORL, price under VWAP, and bearish EMA alignment.",
        "aliases": ["破位追空", "下破", "跌破支撑", "breakdown", "bearish followthrough", "空头延续", "追put"],
        "query_template": "扫描{symbol}破位追空期权，优先PUT或bear put spread，检查跌破ORL、VWAP下方、EMA空头排列、RVOL放大、GEX顺风和止损失效价",
        "intent": {"preferred_side": "put", "day_trade": True, "min_days": 0, "max_days": 21, "max_ask": 6.0, "horizon_label": "near_term"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "iv_expansion_prelude",
        "label": "IV预扩张",
        "label_en": "IV Pre-Expansion",
        "description": "寻找事件前隐波未充分抬升、方向未明但波动可能扩大的结构。",
        "description_en": "Finds structures before an event where IV hasn't fully risen, direction is unclear, but volatility may expand.",
        "aliases": ["iv预扩张", "隐波扩张", "波动预埋", "事件前", "pre earnings", "iv expansion", "vol expansion"],
        "query_template": "扫描{symbol}IV预扩张型机会，比较单腿、跨式和宽跨，重点检查IV Percentile、期限结构、breakeven、组合成本和事件前退出计划",
        "intent": {"preferred_side": None, "min_days": 7, "max_days": 35, "max_ask": 12.0, "horizon_label": "monthly"},
        "strategy_modes": ["single_leg", "straddle", "strangle"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "post_event_premium_crush",
        "label": "事件后收租",
        "label_en": "Post-Event Premium Crush",
        "description": "适合财报/事件后 IV 回落、方向趋稳时，用定义风险结构吃剩余权利金。",
        "description_en": "For when IV falls and direction settles after earnings/events — harvest residual premium with defined-risk structures.",
        "aliases": ["事件后收租", "财报后", "iv crush", "隐波回落", "波动塌陷", "post earnings", "premium crush"],
        "query_template": "扫描{symbol}事件后收租结构，优先credit spread、iron condor或butterfly，检查IV回落、正GEX、区间、最大亏损和腿级报价一致性",
        "intent": {"preferred_side": None, "min_days": 7, "max_days": 35, "max_ask": 12.0, "horizon_label": "monthly"},
        "strategy_modes": ["credit_spread", "iron_condor", "butterfly"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "pin_wall_butterfly",
        "label": "Pin价蝶式",
        "label_en": "Pin-Wall Butterfly",
        "description": "利用正 GEX、gamma wall 或整数关口附近的钉住预期，寻找 butterfly。",
        "description_en": "Uses pinning expectations near positive GEX, a gamma wall, or a round number to find butterflies.",
        "aliases": ["pin价", "pin risk", "钉住", "gamma wall", "整数关口", "蝶式", "butterfly", "蝶式钉住", "pinning"],
        "query_template": "扫描{symbol}Pin价蝶式机会，重点检查正GEX、最近gamma wall、整数关口、到期日、组合净支出和接近中间行权价的止盈规则",
        "intent": {"preferred_side": None, "min_days": 0, "max_days": 21, "max_ask": 8.0, "horizon_label": "near_term"},
        "strategy_modes": ["butterfly"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "relative_strength_leader",
        "label": "强者恒强",
        "label_en": "Relative-Strength Leader",
        "description": "寻找强于大盘、VWAP 上方、趋势票一致的 call 或 bull call spread。",
        "description_en": "Finds calls or bull call spreads on names stronger than the market, above VWAP, with aligned trend.",
        "aliases": ["强者恒强", "相对强势", "领涨", "强趋势", "趋势龙头", "relative strength", "rs leader"],
        "query_template": "扫描{symbol}强者恒强型期权，优先CALL或bull call spread，检查日线趋势、VWAP上方、ORB突破、RVOL、MACD和低衰减风险",
        "intent": {"preferred_side": "call", "min_days": 7, "max_days": 30, "max_ask": 8.0, "horizon_label": "slightly_far"},
        "strategy_modes": ["single_leg", "spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "defined_risk_trend",
        "label": "趋势价差",
        "label_en": "Defined-Risk Trend",
        "description": "趋势明确但单腿成本或 theta 偏高时，优先寻找 debit/credit spread。",
        "description_en": "When the trend is clear but single-leg cost or theta is high, prefer debit/credit spreads.",
        "aliases": ["定义风险", "价差", "垂直价差", "spread", "debit spread", "credit spread"],
        "query_template": "扫描{symbol}适合趋势方向的定义风险价差策略，比较debit spread和credit spread的结构适配、盈亏质量和执行复杂度",
        "intent": {"min_days": 0, "max_days": 30, "horizon_label": "near_term"},
        "strategy_modes": ["spread", "credit_spread"],
        "analysis_modules": {"intraday": True, "greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "volatility_event",
        "label": "波动事件",
        "label_en": "Volatility Event",
        "description": "适合方向不清但预期波动放大的场景，比较跨式/宽跨的成本、IV 和 breakeven。",
        "description_en": "For when direction is unclear but volatility is expected to expand — compare straddle/strangle cost, IV, and breakeven.",
        "aliases": ["大波动", "波动放大", "财报波动", "事件波动", "跨式", "宽跨", "straddle", "strangle", "iv expansion"],
        "query_template": "扫描{symbol}事件波动型期权结构，方向不明时比较跨式和宽跨，重点检查IV、组合成本、breakeven和自然退出规则",
        "intent": {"preferred_side": None, "min_days": 0, "max_days": 24, "max_ask": 12.0, "horizon_label": "near_term"},
        "strategy_modes": ["straddle", "strangle"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "income_range",
        "label": "震荡收租",
        "label_en": "Range Income",
        "description": "适合震荡、正 GEX 或区间假设，优先铁鹰、蝶式和信用价差。",
        "description_en": "For chop, positive GEX, or a range thesis — prefer iron condors, butterflies, and credit spreads.",
        "aliases": ["震荡", "区间收租", "震荡区间", "收租", "铁鹰", "iron condor", "range income"],
        "query_template": "扫描{symbol}震荡收租型期权结构，优先铁鹰、蝶式和信用价差，重点检查正GEX、区间、最大亏损和腿级报价",
        "intent": {"preferred_side": None, "min_days": 7, "max_days": 45, "horizon_label": "monthly"},
        "strategy_modes": ["credit_spread", "iron_condor", "butterfly"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "covered_call",
        "label": "备兑收租",
        "label_en": "Covered Call Income",
        "description": "适合已有 100 股底仓时卖出虚值 call；实盘会检查正股支撑。",
        "description_en": "For selling OTM calls when you already hold 100 shares; live trading checks the underlying backing.",
        "aliases": ["备兑", "备兑期权", "covered call", "covered"],
        "query_template": "扫描{symbol}适合下周或近月的备兑期权合约对，检查权利金、行权价距离、底仓要求和自然退出规则",
        "intent": {"preferred_side": "call", "min_days": 7, "max_days": 45, "horizon_label": "monthly"},
        "strategy_modes": ["covered_call"],
        "analysis_modules": {"greeks": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "cash_secured_put",
        "label": "现金担保Put",
        "label_en": "Cash-Secured Put",
        "description": "适合愿意以目标价接股的用户，卖出虚值 put 收权利金，但必须检查现金占用和接股风险。",
        "description_en": "For those willing to take shares at a target price — sell OTM puts for premium, but check cash usage and assignment risk.",
        "aliases": ["现金担保", "现金担保put", "cash secured put", "secured put", "卖put接股", "低价接股"],
        "query_template": "扫描{symbol}现金担保Put机会，优先虚值put，检查权利金、接股价、现金占用、IV和自然退出规则",
        "intent": {"preferred_side": "put", "min_days": 7, "max_days": 45, "horizon_label": "monthly"},
        "strategy_modes": ["cash_secured_put"],
        "analysis_modules": {"greeks": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "calendar_spread",
        "label": "日历价差",
        "label_en": "Calendar Spread",
        "description": "适合横盘、近月衰减更快或预期价格钉住的场景，买远月卖近月。",
        "description_en": "For sideways markets, faster near-term decay, or a pinning thesis — buy the far month, sell the near month.",
        "aliases": ["日历价差", "calendar", "calendar spread", "近月衰减", "时间价差"],
        "query_template": "扫描{symbol}日历价差机会，寻找同执行价买远月卖近月结构，检查近月theta、远月流动性、IV期限结构和整组退出规则",
        "intent": {"preferred_side": None, "min_days": 7, "max_days": 60, "horizon_label": "monthly"},
        "strategy_modes": ["calendar"],
        "analysis_modules": {"greeks": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "diagonal_spread",
        "label": "对角价差",
        "label_en": "Diagonal Spread",
        "description": "适合带方向倾斜的时间价差，买远月、卖近月不同执行价。",
        "description_en": "For a directionally-tilted time spread — buy the far month and sell the near month at a different strike.",
        "aliases": ["对角价差", "diagonal", "diagonal spread", "对角", "方向日历"],
        "query_template": "扫描{symbol}对角价差机会，比较call/put diagonal，检查远月长腿、近月短腿、方向偏置、最大亏损和滚动风险",
        "intent": {"preferred_side": None, "min_days": 7, "max_days": 60, "horizon_label": "monthly"},
        "strategy_modes": ["diagonal"],
        "analysis_modules": {"greeks": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "poor_mans_covered_call",
        "label": "穷人备兑",
        "label_en": "Poor Man's Covered Call",
        "description": "用远月深度实值 call 替代正股，再卖近月 call 降低成本；适合资金效率优先的类备兑。",
        "description_en": "Replaces stock with a deep-ITM far-month call, then sells near-month calls to cut cost; a capital-efficient covered-call analog.",
        "aliases": ["穷人备兑", "poor man's covered call", "poor mans covered call", "pmcc", "小资金备兑", "类备兑"],
        "query_template": "扫描{symbol}穷人备兑机会，买远月ITM call并卖近月OTM call，检查净支出、短腿风险、最大亏损和滚动规则",
        "intent": {"preferred_side": "call", "min_days": 14, "max_days": 90, "max_ask": 25.0, "horizon_label": "monthly"},
        "strategy_modes": ["poor_mans_covered_call"],
        "analysis_modules": {"greeks": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "protective_collar",
        "label": "领式保护",
        "label_en": "Protective Collar",
        "description": "适合已有正股、想限制下行风险并用卖 call 抵扣保护 put 成本。",
        "description_en": "For holders who want to cap downside and offset the protective put's cost by selling a call.",
        "aliases": ["领式", "collar", "保护底仓", "底仓保护", "保护性领式"],
        "query_template": "扫描{symbol}适合底仓保护的领式策略，比较保护put和卖出call的成本、保护区间和正股支撑要求",
        "intent": {"preferred_side": None, "min_days": 7, "max_days": 45, "horizon_label": "monthly"},
        "strategy_modes": ["collar"],
        "analysis_modules": {"greeks": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
    {
        "key": "bearish_hedge",
        "label": "下跌对冲",
        "label_en": "Bearish Hedge",
        "description": "偏看跌或需要保护时，优先 put、bear put spread 或 collar。",
        "description_en": "When bearish or needing protection, prefer puts, bear put spreads, or collars.",
        "aliases": ["对冲", "保护性put", "买保险", "看跌保护", "下跌保护", "hedge"],
        "query_template": "扫描{symbol}下跌对冲型期权，优先put、bear put spread或collar，检查成本、保护区间、delta和最大亏损",
        "intent": {"preferred_side": "put", "min_days": 7, "max_days": 45, "horizon_label": "monthly"},
        "strategy_modes": ["single_leg", "spread", "collar"],
        "analysis_modules": {"greeks": True, "gex": True, "execution": True, "volatility": True, "strategy": True, "scenario": True, "risk": True},
    },
]


PLANNER_PROMPT = """你是美股期权扫描器的“工具链调度员”。
你会先读用户自然语言，再决定：标的、方向、期限、价格偏好、是否彩票、是否日内、需要哪些工具链。
所有时间都必须按 America/New_York 美东交易所时间理解；用户说“今天/今晚/5月1日”只是分析日期语境，不能自动误判为月度远期期限。

只返回 JSON，不要 Markdown。字段必须包含：
{
  "symbol": "NVDA",
  "preferred_side": "call|put|null",
  "min_days": 0,
  "max_days": 12,
  "max_ask": 8.0,
  "lottery": false,
  "cheap": false,
  "day_trade": false,
  "requested_date_et": "YYYY-MM-DD|null",
  "horizon_label": "0DTE|weekly|near_term|slightly_far|monthly|custom",
  "strategy_modes": ["single_leg","spread","credit_spread","straddle","strangle","collar","covered_call","cash_secured_put","calendar","diagonal","poor_mans_covered_call","iron_condor","butterfly"],
  "analysis_modules": {"intraday": true, "greeks": true, "execution": true, "volatility": true, "strategy": true, "scenario": true, "risk": true},
  "tool_plan": {
    "longbridge_quote": true,
    "longbridge_daily_kline": true,
    "longbridge_intraday": true,
    "longbridge_news": true,
    "longbridge_option_chain": true,
    "yfinance_option_chain": true,
    "iv_structure": true,
    "scenario_pricing": true,
    "risk_plan": true
  },
  "reasoning": "一句话解释为什么这样调度",
  "confidence": 0.0
}

约束：
- 如果用户明确提到 ticker，必须优先用户文本里的 ticker，而不是 UI 残留字段。
- 如果用户明确提到“单腿”，只能返回 single_leg；如果提到“价差/信用价差/跨式/宽跨/领式/备兑/现金担保Put/日历价差/对角价差/穷人备兑/铁鹰/蝶式”，要把对应策略模式加入 strategy_modes。
- 如果用户说“策略/组合/结构”，可以同时返回多个策略模式；但单腿仍可以保留为对照基准。
- 如果用户说“稍微远/远一点/一两周”，用 7-24 DTE；“月底/月度/一个月”才用 14-45 DTE。
- 如果用户说 0DTE/当天到期，才用 0-0 DTE。
- 如果用户说“便宜/不贵”，max_ask 通常 2.5；“彩票+便宜/一美元内”通常 1.25。
- analysis_modules 只能在已启用模块里保持 true，不能把用户关闭的模块重新打开。
- 单腿期权请求通常必须启用期权链；Longbridge API 账号可用时优先 longbridge_option_chain，未选择账号时才使用 yfinance_option_chain。
"""


def plan_scan_intent(
    query: str,
    symbol: str | None,
    provider_name: str,
    enabled_modules: dict[str, Any] | None = None,
    owner_id: str | None = None,
) -> tuple[ScanIntent, dict[str, Any]]:
    fallback_intent = parse_query(query, symbol)
    modules = normalize_analysis_modules(enabled_modules)
    fallback_plan = _fallback_plan(fallback_intent, modules)
    answer = ask_ai(
        PLANNER_PROMPT,
        {
            "user_query": query,
            "ui_symbol_override": symbol,
            "rule_based_fallback_intent": asdict(fallback_intent),
            "enabled_analysis_modules": modules,
            "time_context": time_context(),
        },
        provider_name,
        owner_id=owner_id,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    parsed = _parse_json_object(answer)
    if not parsed:
        fallback_plan["source"] = "rules_fallback"
        fallback_plan["planner_error"] = "LLM planner unavailable or returned non-JSON."
        return fallback_intent, fallback_plan

    plan = _sanitize_plan(parsed, fallback_intent, modules)
    # An explicit symbol (UI pick / auto-trade universe) is authoritative — the
    # LLM must never relabel it. Without this, a planner returning "T" for a
    # "TSLA" scan fetches AT&T's chain, which the data-integrity guard then
    # blocks every cycle (contract_root_mismatch).
    override = _clean_symbol(symbol)
    if override:
        plan["symbol"] = override
    intent = _intent_from_plan(fallback_intent, plan)
    plan["source"] = "llm_planner"
    return intent, plan


def plan_scan_intent_rules(
    query: str,
    symbol: str | None,
    enabled_modules: dict[str, Any] | None = None,
) -> tuple[ScanIntent, dict[str, Any]]:
    base_intent = parse_query(query, symbol)
    # An explicit symbol (UI pick / auto-trade universe) is authoritative — same
    # rule the LLM path enforces in plan_scan_intent. parse_query lets a ticker
    # *found in the query text* win over the passed symbol, but auto-trade prompts
    # are templated prose laced with timestamps and jargon ("...26T11:07 morning
    # trend...") whose first uppercase run ("T") is mis-read as AT&T — fetching the
    # wrong chain that the data-integrity guard then blocks every cycle. When the
    # caller passed a symbol, trust it over anything scraped from the query.
    override = _clean_symbol(symbol)
    if override and base_intent.symbol != override:
        base_intent = replace(base_intent, symbol=override)
    modules = normalize_analysis_modules(enabled_modules)
    intent, matched = _apply_rule_presets(query, base_intent, modules)
    plan = _fallback_plan(intent, modules)
    plan["source"] = "rules_preset" if matched else "rules_only"
    plan["matched_presets"] = [
        {
            "key": item["key"],
            "label": item["label"],
            "description": item["description"],
            "query_template": item["query_template"],
            "strategy_modes": item.get("strategy_modes") or [],
        }
        for item in matched
    ]
    plan["preset_instructions"] = [item["description"] for item in matched]
    plan["reasoning"] = (
        "非 AI 规则解析；命中预设：" + " / ".join(item["label"] for item in matched)
        if matched
        else "非 AI 规则解析；未命中特定预设，使用通用期权扫描规则。"
    )
    plan["confidence"] = 0.72 if matched else 0.45
    return intent, plan


def analysis_presets_for_ui() -> list[dict[str, Any]]:
    return [
        {
            "key": item["key"],
            "label": item["label"],
            "label_en": item.get("label_en") or item["label"],
            "description": item["description"],
            "description_en": item.get("description_en") or item["description"],
            "aliases": item.get("aliases") or [],
            "query_template": item["query_template"],
            "strategy_modes": item.get("strategy_modes") or ["single_leg"],
            "analysis_modules": item.get("analysis_modules") or {},
            "default_use_ai": False,
        }
        for item in RULE_ANALYSIS_PRESETS
    ]


def _fallback_plan(intent: ScanIntent, modules: dict[str, bool]) -> dict[str, Any]:
    return {
        **asdict(intent),
        "analysis_modules": modules,
        "tool_plan": _default_tool_plan(intent, modules),
        "reasoning": "规则兜底解析。",
        "confidence": 0.35,
    }


def _default_tool_plan(intent: ScanIntent, modules: dict[str, bool]) -> dict[str, bool]:
    return {
        "longbridge_quote": True,
        "longbridge_daily_kline": True,
        "longbridge_intraday": True,
        "longbridge_news": True,
        "longbridge_option_chain": True,
        "yfinance_option_chain": True,
        "iv_structure": bool(modules.get("volatility")),
        "scenario_pricing": bool(modules.get("scenario")),
        "risk_plan": bool(modules.get("risk")),
    }


def _apply_rule_presets(query: str, intent: ScanIntent, modules: dict[str, bool]) -> tuple[ScanIntent, list[dict[str, Any]]]:
    matched = [preset for preset in RULE_ANALYSIS_PRESETS if _preset_matches(query, preset)]
    if not matched:
        return intent, []

    values = asdict(intent)
    strategy_modes = list(values.get("strategy_modes") or [])
    strategy_modes_from_specific_presets: list[str] = []
    semantic_notes = list(values.get("semantic_notes") or [])
    explicit_single_leg = _contains_any(query, ["单腿", "single_leg", "single leg", "one leg"])

    for preset in matched:
        patch = preset.get("intent") or {}
        preset_modes = list(preset.get("strategy_modes") or [])
        preset_requires_both_sides = any(mode in {"collar", "straddle", "strangle", "iron_condor", "calendar", "diagonal"} for mode in normalize_strategy_modes(preset_modes))
        for key, value in patch.items():
            if key == "preferred_side" and values.get("preferred_side") is not None and value is None and not preset_requires_both_sides:
                continue
            values[key] = value
        if preset["key"] not in {"slightly_far_swing"}:
            strategy_modes_from_specific_presets.extend(preset_modes)
        elif not explicit_single_leg:
            strategy_modes.extend(preset.get("strategy_modes") or [])
        semantic_notes.append(f"命中非AI分析预设：{preset['label']}。{preset['description']}")
        for module_key, enabled in (preset.get("analysis_modules") or {}).items():
            if module_key in modules:
                modules[module_key] = bool(modules.get(module_key)) and bool(enabled)

    if explicit_single_leg:
        strategy_modes = ["single_leg"]
    elif strategy_modes_from_specific_presets:
        strategy_modes = strategy_modes_from_specific_presets
    values["strategy_modes"] = normalize_strategy_modes(strategy_modes)
    values["semantic_notes"] = semantic_notes
    return ScanIntent(**values), matched


def _preset_matches(query: str, preset: dict[str, Any]) -> bool:
    return _contains_any(query, list(preset.get("aliases") or []))


def _contains_any(query: str, words: list[str]) -> bool:
    normalized = query.lower()
    return any(str(word).lower() in normalized for word in words)


def _sanitize_plan(raw: dict[str, Any], fallback: ScanIntent, enabled_modules: dict[str, bool]) -> dict[str, Any]:
    min_days = _bounded_int(raw.get("min_days"), fallback.min_days, 0, 365)
    max_days = _bounded_int(raw.get("max_days"), fallback.max_days, min_days, 730)
    modules = normalize_analysis_modules(raw.get("analysis_modules"))
    modules = {key: bool(enabled_modules.get(key)) and bool(modules.get(key)) for key in enabled_modules}
    tool_plan = _default_tool_plan(fallback, modules)
    raw_tool_plan = raw.get("tool_plan") if isinstance(raw.get("tool_plan"), dict) else {}
    for key in tool_plan:
        if key in raw_tool_plan:
            tool_plan[key] = bool(raw_tool_plan[key])
    if fallback.day_trade:
        tool_plan["longbridge_intraday"] = True
    if not tool_plan.get("longbridge_option_chain") and not tool_plan.get("yfinance_option_chain"):
        tool_plan["longbridge_option_chain"] = True
        tool_plan["yfinance_option_chain"] = True

    preferred_side = raw.get("preferred_side")
    if preferred_side not in ("call", "put", None):
        preferred_side = fallback.preferred_side
    requested_date = raw.get("requested_date_et")
    if not isinstance(requested_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", requested_date):
        requested_date = fallback.requested_date_et
    raw_strategy_modes = raw.get("strategy_modes") if "strategy_modes" in raw and raw.get("strategy_modes") is not None else fallback.strategy_modes
    strategy_modes = normalize_strategy_modes(raw_strategy_modes)

    return {
        "symbol": _clean_symbol(raw.get("symbol")) or fallback.symbol,
        "preferred_side": preferred_side,
        "min_days": min_days,
        "max_days": max_days,
        "max_ask": _bounded_float(raw.get("max_ask"), fallback.max_ask, 0.05, 100.0),
        "lottery": bool(raw.get("lottery", fallback.lottery)),
        "cheap": bool(raw.get("cheap", fallback.cheap)),
        "day_trade": bool(raw.get("day_trade", fallback.day_trade)),
        "requested_date_et": requested_date,
        "time_basis": fallback.time_basis,
        "horizon_label": str(raw.get("horizon_label") or fallback.horizon_label),
        "strategy_modes": strategy_modes,
        "semantic_notes": list(fallback.semantic_notes),
        "analysis_modules": modules,
        "tool_plan": tool_plan,
        "reasoning": str(raw.get("reasoning") or "LLM 调度完成。")[:500],
        "confidence": _bounded_float(raw.get("confidence"), 0.65, 0.0, 1.0),
    }


def _intent_from_plan(fallback: ScanIntent, plan: dict[str, Any]) -> ScanIntent:
    return replace(
        fallback,
        symbol=str(plan["symbol"]),
        preferred_side=plan["preferred_side"],
        min_days=int(plan["min_days"]),
        max_days=int(plan["max_days"]),
        max_ask=float(plan["max_ask"]),
        lottery=bool(plan["lottery"]),
        cheap=bool(plan["cheap"]),
        day_trade=bool(plan["day_trade"]),
        requested_date_et=plan.get("requested_date_et"),
        horizon_label=str(plan.get("horizon_label") or fallback.horizon_label),
        strategy_modes=list(plan.get("strategy_modes") or fallback.strategy_modes),
        semantic_notes=list(plan.get("semantic_notes") or fallback.semantic_notes),
    )


def _parse_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _clean_symbol(value: Any) -> str | None:
    text = str(value or "").upper().strip()
    if re.fullmatch(r"[A-Z]{1,5}", text):
        return text
    return None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))

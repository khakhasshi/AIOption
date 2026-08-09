from __future__ import annotations

from typing import Any

from .ai_client import ask_ai, get_last_ai_error


ADVISORS = [
    {
        "name": "进攻型诸葛亮",
        "prompt": """你是进攻型期权策略顾问。只基于 JSON 数据，寻找最有凸性和大额盈利潜力的候选。
如果 strategy_candidates 存在，可以选择策略结构；否则只能从 option_candidates 选择单腿。
偏好高赔率、催化、趋势突破和合理流动性，但不能忽略 bid/ask、成交量与最大亏损。
只输出 JSON object：
{
  "advisor": "进攻型诸葛亮",
  "stance": "bullish|bearish|neutral|volatile",
  "selection_type": "single_leg|strategy|none",
  "selected_contract_symbol": "候选池里的合约或空字符串",
  "selected_strategy_key": "策略候选里的 strategy_key 或空字符串",
  "rationale": "短理由",
  "evidence": [{"field": "字段路径", "value": "字段值", "supports": "为什么支持"}],
  "warnings": ["风险"]
}""",
    },
    {
        "name": "风控型诸葛亮",
        "prompt": """你是风控型期权策略顾问。只基于 JSON 数据，优先审查胜率、流动性、价差、theta、IV、最大亏损和止损清晰度。
如果 strategy_candidates 存在，可以选择策略结构；否则只能从 option_candidates 选择单腿。
可以否决过于彩票的合约，但必须从候选里给出一个你认为最可控的方案。
只输出 JSON object：
{
  "advisor": "风控型诸葛亮",
  "stance": "bullish|bearish|neutral|volatile",
  "selection_type": "single_leg|strategy|none",
  "selected_contract_symbol": "候选池里的合约或空字符串",
  "selected_strategy_key": "策略候选里的 strategy_key 或空字符串",
  "rationale": "短理由",
  "evidence": [{"field": "字段路径", "value": "字段值", "supports": "为什么支持"}],
  "warnings": ["风险"]
}""",
    },
    {
        "name": "反方诸葛亮",
        "prompt": """你是反方期权策略顾问。只基于 JSON 数据，专门寻找交易陷阱、假突破、新闻噪音、流动性问题和赔率错觉。
如果 strategy_candidates 存在，可以选择策略结构；否则只能从 option_candidates 选择单腿。
你的任务不是唱空，而是逼迫方案经得起反驳；若证据不足可以明确 no_trade/observe。
只输出 JSON object：
{
  "advisor": "反方诸葛亮",
  "stance": "bullish|bearish|neutral|volatile",
  "selection_type": "single_leg|strategy|none",
  "selected_contract_symbol": "候选池里的合约或空字符串",
  "selected_strategy_key": "策略候选里的 strategy_key 或空字符串",
  "rationale": "短理由",
  "evidence": [{"field": "字段路径", "value": "字段值", "supports": "为什么支持"}],
  "warnings": ["风险"]
}""",
    },
]


MODERATOR_PROMPT = """你是期权小组讨论主持人。你会收到原始扫描 JSON 和三个 DeepSeek 顾问的独立意见。
你必须只输出一个 JSON object，不能输出 markdown 或自然语言段落。
只能从 option_candidates.contract_symbol 或 strategy_candidates.strategy_key 里选择，不能编造候选外合约、策略、价格或指标。

硬性规则：
- 如果 decision_gate.should_trade=false，action 必须是 "no_trade" 或 "observe"。
- 如果 decision_gate.allow_auto_trade=false，action 不能是 "trade"，只能是 "observe"。
- 单腿只能选择 option_candidates 中存在的 selected_contract_symbol。
- 策略只能选择 strategy_candidates 中存在的 selected_strategy_key。
- 每个 trade/observe 结论必须提供 evidence，evidence 每一项必须包含 field、value、supports。
- 缺失必要证据时，不要硬选，action 用 "observe"。

JSON schema:
{
  "action": "trade|observe|no_trade",
  "selection_type": "single_leg|strategy|none",
  "selected_contract_symbol": "候选池里的合约或空字符串",
  "selected_strategy_key": "策略候选里的 strategy_key 或空字符串",
  "summary": "一句话结论",
  "rationale": "短理由，说明三顾问分歧后的裁定",
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
}"""


def run_council(payload: dict[str, Any], provider_name: str, owner_id: str | None = None) -> dict[str, Any] | None:
    advisor_reports: list[dict[str, Any]] = []
    for advisor in ADVISORS:
        report = ask_ai(advisor["prompt"], payload, provider_name, owner_id=owner_id, temperature=0.1, response_format={"type": "json_object"})
        if not report:
            error = get_last_ai_error()
            advisor_reports.append({"advisor": advisor["name"], "report": "", "status": "failed", "error": error})
            return {
                "mode": "three_advisors",
                "advisor_reports": advisor_reports,
                "final_decision_answer": "",
                "error": "advisor_failed",
                "error_detail": error,
                "failed_advisor": advisor["name"],
            }
        advisor_reports.append({"advisor": advisor["name"], "report": report, "status": "succeeded"})

    synthesis_payload = {
        "scan_payload": payload,
        "advisor_reports": advisor_reports,
    }
    final_decision_answer = ask_ai(MODERATOR_PROMPT, synthesis_payload, provider_name, owner_id=owner_id, temperature=0.1, response_format={"type": "json_object"})
    if not final_decision_answer:
        return {
            "mode": "three_advisors",
            "advisor_reports": advisor_reports,
            "final_decision_answer": "",
            "error": "moderator_failed",
            "error_detail": get_last_ai_error(),
        }
    return {
        "mode": "three_advisors",
        "advisor_reports": advisor_reports,
        "final_decision_answer": final_decision_answer,
    }

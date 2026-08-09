from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .strategy_gate_templates import evaluate_strategy_template


STRATEGY_MODE_ORDER = [
    "single_leg",
    "spread",
    "straddle",
    "strangle",
    "collar",
    "covered_call",
    "cash_secured_put",
    "credit_spread",
    "calendar",
    "diagonal",
    "poor_mans_covered_call",
    "iron_condor",
    "butterfly",
]

STRATEGY_MODE_LABELS = {
    "single_leg": "单腿",
    "spread": "价差",
    "straddle": "跨式",
    "strangle": "宽跨",
    "collar": "领式",
    "covered_call": "备兑",
    "cash_secured_put": "现金担保Put",
    "credit_spread": "信用价差",
    "calendar": "日历价差",
    "diagonal": "对角价差",
    "poor_mans_covered_call": "穷人备兑",
    "iron_condor": "铁鹰",
    "butterfly": "蝶式",
}


@dataclass(frozen=True)
class StrategyCandidate:
    family: str
    strategy_type: str
    label: str
    direction: str
    expiration: str
    legs: list[dict[str, Any]]
    net_debit: float = 0.0
    net_credit: float = 0.0
    max_loss: float = 0.0
    max_profit: float | None = None
    breakevens: list[float] = field(default_factory=list)
    width: float = 0.0
    capital_required: float = 0.0
    probability_hint: float = 0.0
    score: float = 0.0
    structure_fit_score: float = 0.0
    payoff_quality_score: float = 0.0
    execution_complexity_score: float = 0.0
    capital_efficiency_score: float = 0.0
    risk_defined_score: float = 0.0
    quote_consistency_score: float = 0.0
    quote_consistency_state: str = "unknown"
    natural_exit: dict[str, Any] = field(default_factory=dict)
    strategy_template_gate: dict[str, Any] = field(default_factory=dict)
    fit_notes: list[str] = field(default_factory=list)
    hard_flags: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["mode_label"] = STRATEGY_MODE_LABELS.get(self.family, self.family)
        data["max_profit_unlimited"] = self.max_profit is None
        return data


def normalize_strategy_modes(raw_modes: Any) -> list[str]:
    if raw_modes is None:
        return ["single_leg"]
    if isinstance(raw_modes, str):
        raw_modes = [raw_modes]
    modes: list[str] = []
    for raw in raw_modes if isinstance(raw_modes, (list, tuple, set)) else []:
        raw_text = str(raw or "").strip().lower()
        if raw_text in {"all", "全部", "所有", "all_strategies"}:
            for item in STRATEGY_MODE_ORDER:
                if item not in modes:
                    modes.append(item)
            continue
        mode = _normalize_mode(raw)
        if mode and mode not in modes:
            modes.append(mode)
    if not modes:
        modes = ["single_leg"]
    return sorted(modes, key=lambda item: STRATEGY_MODE_ORDER.index(item) if item in STRATEGY_MODE_ORDER else 99)


def strategy_mode_label(mode: str) -> str:
    return STRATEGY_MODE_LABELS.get(mode, mode or "--")


def build_strategy_candidates(
    candidates: list[Any],
    spot: float,
    strategy_modes: list[str] | None = None,
    decision_gate: dict[str, Any] | None = None,
) -> list[StrategyCandidate]:
    modes = normalize_strategy_modes(strategy_modes)
    if "single_leg" in modes:
        modes = [mode for mode in modes if mode != "single_leg"]

    if not candidates or not modes:
        return []

    by_expiration: dict[str, dict[str, list[Any]]] = defaultdict(lambda: {"call": [], "put": []})
    for candidate in candidates:
        expiration = str(_candidate_value(candidate, "expiration") or "")
        side = str(_candidate_value(candidate, "side") or "").lower()
        if not expiration or side not in {"call", "put"}:
            continue
        by_expiration[expiration][side].append(candidate)

    strategy_candidates: list[StrategyCandidate] = []
    if "spread" in modes:
        strategy_candidates.extend(_build_spreads(by_expiration, spot))
    if "credit_spread" in modes:
        strategy_candidates.extend(_build_credit_spreads(by_expiration, spot))
    if "straddle" in modes:
        strategy_candidates.extend(_build_straddle(by_expiration, spot))
    if "strangle" in modes:
        strategy_candidates.extend(_build_strangle(by_expiration, spot))
    if "collar" in modes:
        strategy_candidates.extend(_build_collar(by_expiration, spot))
    if "covered_call" in modes:
        strategy_candidates.extend(_build_covered_call(candidates, spot))
    if "cash_secured_put" in modes:
        strategy_candidates.extend(_build_cash_secured_put(candidates, spot))
    if "calendar" in modes:
        strategy_candidates.extend(_build_calendar_spreads(by_expiration, spot))
    if "diagonal" in modes:
        strategy_candidates.extend(_build_diagonal_spreads(by_expiration, spot))
    if "poor_mans_covered_call" in modes:
        strategy_candidates.extend(_build_poor_mans_covered_call(by_expiration, spot))
    if "iron_condor" in modes:
        strategy_candidates.extend(_build_iron_condor(by_expiration, spot))
    if "butterfly" in modes:
        strategy_candidates.extend(_build_butterfly(by_expiration, spot))

    enriched = [_enrich_strategy_candidate(item, decision_gate) for item in strategy_candidates]
    return sorted(enriched, key=lambda item: item.score, reverse=True)


def _build_spreads(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for expiration, sides in by_expiration.items():
        calls = _rank_candidates(sides["call"])
        puts = _rank_candidates(sides["put"])
        rows.extend(_bull_call_spreads(expiration, calls, spot))
        rows.extend(_bear_put_spreads(expiration, puts, spot))
        rows.extend(_bull_put_spreads(expiration, puts, spot))
        rows.extend(_bear_call_spreads(expiration, calls, spot))
    return rows


def _build_credit_spreads(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for expiration, sides in by_expiration.items():
        puts = _rank_candidates(sides["put"])
        calls = _rank_candidates(sides["call"])
        rows.extend(_bull_put_spreads(expiration, puts, spot, family="credit_spread"))
        rows.extend(_bear_call_spreads(expiration, calls, spot, family="credit_spread"))
    return rows


def _bull_call_spreads(expiration: str, calls: list[Any], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for long_idx, long_leg in enumerate(calls):
        for short_leg in calls[long_idx + 1 : long_idx + 5]:
            if _num(_candidate_value(long_leg, "strike")) >= _num(_candidate_value(short_leg, "strike")):
                continue
            debit = max(0.05, _num(_candidate_value(long_leg, "ask")) - _num(_candidate_value(short_leg, "bid")))
            width = _num(_candidate_value(short_leg, "strike")) - _num(_candidate_value(long_leg, "strike"))
            if width <= 0 or debit >= width:
                continue
            rows.append(
                _build_vertical_spread(
                    family="spread",
                    strategy_type="bull_call_spread",
                    label="看涨价差",
                    direction="bullish",
                    expiration=expiration,
                    long_leg=long_leg,
                    short_leg=short_leg,
                    debit=debit,
                    width=width,
                    spot=spot,
                )
            )
    return rows


def _bear_put_spreads(expiration: str, puts: list[Any], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for long_idx, long_leg in enumerate(sorted(puts, key=lambda item: _num(_candidate_value(item, "strike")), reverse=True)):
        for short_leg in sorted(puts, key=lambda item: _num(_candidate_value(item, "strike")), reverse=True)[long_idx + 1 : long_idx + 5]:
            if _num(_candidate_value(long_leg, "strike")) <= _num(_candidate_value(short_leg, "strike")):
                continue
            debit = max(0.05, _num(_candidate_value(long_leg, "ask")) - _num(_candidate_value(short_leg, "bid")))
            width = _num(_candidate_value(long_leg, "strike")) - _num(_candidate_value(short_leg, "strike"))
            if width <= 0 or debit >= width:
                continue
            rows.append(
                _build_vertical_spread(
                    family="spread",
                    strategy_type="bear_put_spread",
                    label="看跌价差",
                    direction="bearish",
                    expiration=expiration,
                    long_leg=long_leg,
                    short_leg=short_leg,
                    debit=debit,
                    width=width,
                    spot=spot,
                )
            )
    return rows


def _bull_put_spreads(expiration: str, puts: list[Any], spot: float, family: str = "spread") -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    ordered = sorted(puts, key=lambda item: _num(_candidate_value(item, "strike")))
    for short_idx, short_leg in enumerate(ordered):
        for long_leg in ordered[max(0, short_idx - 4) : short_idx]:
            if _num(_candidate_value(long_leg, "strike")) >= _num(_candidate_value(short_leg, "strike")):
                continue
            credit = max(0.05, _num(_candidate_value(short_leg, "bid")) - _num(_candidate_value(long_leg, "ask")))
            width = _num(_candidate_value(short_leg, "strike")) - _num(_candidate_value(long_leg, "strike"))
            if width <= 0 or credit <= 0:
                continue
            rows.append(
                _build_credit_spread(
                    family=family,
                    strategy_type="bull_put_spread",
                    label="牛市信用价差",
                    direction="bullish",
                    expiration=expiration,
                    sell_leg=short_leg,
                    buy_leg=long_leg,
                    credit=credit,
                    width=width,
                    spot=spot,
                )
            )
    return rows


def _bear_call_spreads(expiration: str, calls: list[Any], spot: float, family: str = "spread") -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    ordered = sorted(calls, key=lambda item: _num(_candidate_value(item, "strike")))
    for short_idx, short_leg in enumerate(ordered):
        for buy_leg in ordered[short_idx + 1 : short_idx + 5]:
            if _num(_candidate_value(buy_leg, "strike")) <= _num(_candidate_value(short_leg, "strike")):
                continue
            credit = max(0.05, _num(_candidate_value(short_leg, "bid")) - _num(_candidate_value(buy_leg, "ask")))
            width = _num(_candidate_value(buy_leg, "strike")) - _num(_candidate_value(short_leg, "strike"))
            if width <= 0 or credit <= 0:
                continue
            rows.append(
                _build_credit_spread(
                    family=family,
                    strategy_type="bear_call_spread",
                    label="熊市信用价差",
                    direction="bearish",
                    expiration=expiration,
                    sell_leg=short_leg,
                    buy_leg=buy_leg,
                    credit=credit,
                    width=width,
                    spot=spot,
                )
            )
    return rows


def _build_vertical_spread(
    family: str,
    strategy_type: str,
    label: str,
    direction: str,
    expiration: str,
    long_leg: Any,
    short_leg: Any,
    debit: float,
    width: float,
    spot: float,
) -> StrategyCandidate:
    max_loss = round(max(debit, 0.0) * 100, 2)
    max_profit = round(max(width - debit, 0.0) * 100, 2)
    breakeven = round(_num(_candidate_value(long_leg, "strike")) + debit if _candidate_value(long_leg, "side") == "call" else _num(_candidate_value(long_leg, "strike")) - debit, 2)
    legs = [
        _leg_dict(long_leg, "buy", 1, _num(_candidate_value(long_leg, "ask"))),
        _leg_dict(short_leg, "sell", 1, _num(_candidate_value(short_leg, "bid"))),
    ]
    score = _pair_score(long_leg, short_leg, debit, width, max_profit, max_loss, spot)
    fit_notes = [
        "定义风险",
        f"宽度 {width:.2f}",
        f"净支出 {debit:.2f}",
    ]
    return StrategyCandidate(
        family=family,
        strategy_type=strategy_type,
        label=label,
        direction=direction,
        expiration=expiration,
        legs=legs,
        net_debit=round(debit, 2),
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=[breakeven],
        width=round(width, 2),
        capital_required=max_loss,
        probability_hint=_probability_hint(long_leg, short_leg, direction=direction),
        score=score,
        natural_exit=_natural_exit(strategy_type, max_profit=max_profit, credit=0.0, width=width),
        fit_notes=fit_notes,
        hard_flags=[],
        summary=f"{label} · {expiration} · {breakeven:.2f}",
    )


def _build_credit_spread(
    family: str,
    strategy_type: str,
    label: str,
    direction: str,
    expiration: str,
    sell_leg: Any,
    buy_leg: Any,
    credit: float,
    width: float,
    spot: float,
) -> StrategyCandidate:
    max_profit = round(max(credit, 0.0) * 100, 2)
    max_loss = round(max(width - credit, 0.0) * 100, 2)
    short_strike = _num(_candidate_value(sell_leg, "strike"))
    if _candidate_value(sell_leg, "side") == "call":
        breakeven = round(short_strike + credit, 2)
    else:
        breakeven = round(short_strike - credit, 2)
    legs = [
        _leg_dict(sell_leg, "sell", 1, _num(_candidate_value(sell_leg, "bid"))),
        _leg_dict(buy_leg, "buy", 1, _num(_candidate_value(buy_leg, "ask"))),
    ]
    score = _pair_score(sell_leg, buy_leg, credit, width, max_profit, max_loss, spot, credit_spread=True)
    return StrategyCandidate(
        family=family,
        strategy_type=strategy_type,
        label=label,
        direction=direction,
        expiration=expiration,
        legs=legs,
        net_credit=round(credit, 2),
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=[breakeven],
        width=round(width, 2),
        capital_required=max_loss,
        probability_hint=_probability_hint(sell_leg, buy_leg, direction=direction),
        score=score,
        natural_exit=_natural_exit(strategy_type, max_profit=max_profit, credit=credit, width=width),
        fit_notes=["定义风险", f"宽度 {width:.2f}", f"净收 {credit:.2f}"],
        hard_flags=[],
        summary=f"{label} · {expiration} · {breakeven:.2f}",
    )


def _build_straddle(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for expiration, sides in by_expiration.items():
        calls = _rank_candidates(sides["call"])
        puts = _rank_candidates(sides["put"])
        pair = _closest_strike_pair(calls, puts, spot)
        if not pair:
            continue
        call_leg, put_leg = pair
        debit = max(0.05, _num(_candidate_value(call_leg, "ask")) + _num(_candidate_value(put_leg, "ask")))
        strike = _num(_candidate_value(call_leg, "strike"))
        rows.append(
            StrategyCandidate(
                family="straddle",
                strategy_type="long_straddle",
                label="跨式",
                direction="neutral",
                expiration=expiration,
                legs=[
                    _leg_dict(call_leg, "buy", 1, _num(_candidate_value(call_leg, "ask"))),
                    _leg_dict(put_leg, "buy", 1, _num(_candidate_value(put_leg, "ask"))),
                ],
                net_debit=round(debit, 2),
                max_loss=round(debit * 100, 2),
                max_profit=None,
                breakevens=[round(strike - debit, 2), round(strike + debit, 2)],
                width=0.0,
                capital_required=round(debit * 100, 2),
                probability_hint=50.0,
                score=_neutral_score(calls, puts, debit, spot),
                natural_exit=_natural_exit("long_straddle", max_profit=None, credit=0.0, width=0.0),
                fit_notes=["方向不确定", "波动扩大受益"],
                hard_flags=["unlimited_upside"],
                summary=f"跨式 · {expiration} · {strike:.2f}",
            )
        )
    return rows


def _build_strangle(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for expiration, sides in by_expiration.items():
        calls = _rank_candidates(sides["call"])
        puts = _rank_candidates(sides["put"])
        call_leg = _select_otm_leg(calls, spot, "call", prefer_higher=True)
        put_leg = _select_otm_leg(puts, spot, "put", prefer_higher=False)
        if not call_leg or not put_leg:
            continue
        if _num(_candidate_value(call_leg, "strike")) <= spot or _num(_candidate_value(put_leg, "strike")) >= spot:
            continue
        debit = max(0.05, _num(_candidate_value(call_leg, "ask")) + _num(_candidate_value(put_leg, "ask")))
        rows.append(
            StrategyCandidate(
                family="strangle",
                strategy_type="long_strangle",
                label="宽跨",
                direction="neutral",
                expiration=expiration,
                legs=[
                    _leg_dict(call_leg, "buy", 1, _num(_candidate_value(call_leg, "ask"))),
                    _leg_dict(put_leg, "buy", 1, _num(_candidate_value(put_leg, "ask"))),
                ],
                net_debit=round(debit, 2),
                max_loss=round(debit * 100, 2),
                max_profit=None,
                breakevens=[
                    round(_num(_candidate_value(put_leg, "strike")) - debit, 2),
                    round(_num(_candidate_value(call_leg, "strike")) + debit, 2),
                ],
                width=0.0,
                capital_required=round(debit * 100, 2),
                probability_hint=50.0,
                score=_neutral_score(calls, puts, debit, spot, wingy=True),
                natural_exit=_natural_exit("long_strangle", max_profit=None, credit=0.0, width=0.0),
                fit_notes=["方向不确定", "比跨式更便宜"],
                hard_flags=["unlimited_upside"],
                summary=f"宽跨 · {expiration} · {_candidate_value(call_leg, 'contract_symbol') or ''}",
            )
        )
    return rows


def _build_collar(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for expiration, sides in by_expiration.items():
        calls = _rank_candidates(sides["call"])
        puts = _rank_candidates(sides["put"])
        call_leg = _select_otm_leg(calls, spot, "call", prefer_higher=True)
        put_leg = _select_otm_leg(puts, spot, "put", prefer_higher=False)
        if not call_leg or not put_leg:
            continue
        credit = max(0.05, _num(_candidate_value(call_leg, "bid")) - _num(_candidate_value(put_leg, "ask")))
        capital = round(spot * 100, 2)
        rows.append(
            StrategyCandidate(
                family="collar",
                strategy_type="protective_collar",
                label="领式",
                direction="neutral",
                expiration=expiration,
                legs=[
                    _leg_dict(put_leg, "buy", 1, _num(_candidate_value(put_leg, "ask"))),
                    _leg_dict(call_leg, "sell", 1, _num(_candidate_value(call_leg, "bid"))),
                ],
                net_debit=round(max(_num(_candidate_value(put_leg, "ask")) - _num(_candidate_value(call_leg, "bid")), 0.0), 2),
                net_credit=round(max(credit, 0.0), 2),
                max_loss=round(max((spot - _num(_candidate_value(put_leg, "strike"))) + max(_num(_candidate_value(put_leg, "ask")) - _num(_candidate_value(call_leg, "bid")), 0.0), 0.0) * 100, 2),
                max_profit=round(max((_num(_candidate_value(call_leg, "strike")) - spot) + max(_num(_candidate_value(call_leg, "bid")) - _num(_candidate_value(put_leg, "ask")), 0.0), 0.0) * 100, 2),
                breakevens=[round(spot + max(_num(_candidate_value(put_leg, "ask")) - _num(_candidate_value(call_leg, "bid")), 0.0), 2)],
                width=round(abs(_num(_candidate_value(call_leg, "strike")) - _num(_candidate_value(put_leg, "strike"))), 2),
                capital_required=capital,
                probability_hint=58.0,
                score=_collar_score(call_leg, put_leg, spot),
                natural_exit=_natural_exit("collar", max_profit=round(max((_num(_candidate_value(call_leg, "strike")) - spot) + max(_num(_candidate_value(call_leg, "bid")) - _num(_candidate_value(put_leg, "ask")), 0.0), 0.0) * 100, 2), credit=credit, width=round(abs(_num(_candidate_value(call_leg, "strike")) - _num(_candidate_value(put_leg, "strike"))), 2)),
                fit_notes=["需要底仓100股", "保护下行并保留上行"],
                hard_flags=["requires_stock_position"],
                summary=f"领式 · {expiration}",
            )
        )
    return rows


def _build_covered_call(candidates: list[Any], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    calls = [item for item in candidates if str(_candidate_value(item, "side")).lower() == "call"]
    call_leg = _select_otm_leg(calls, spot, "call", prefer_higher=True)
    if not call_leg:
        return rows
    credit = max(0.05, _num(_candidate_value(call_leg, "bid")))
    strike = _num(_candidate_value(call_leg, "strike"))
    rows.append(
        StrategyCandidate(
            family="covered_call",
            strategy_type="covered_call",
            label="备兑",
            direction="neutral_to_bullish",
            expiration=str(_candidate_value(call_leg, "expiration") or ""),
            legs=[
                {"contract_symbol": "", "role": "stock", "action": "buy", "side": "stock", "qty": 100, "strike": spot, "price": spot},
                _leg_dict(call_leg, "sell", 1, _num(_candidate_value(call_leg, "bid"))),
            ],
            net_debit=0.0,
            net_credit=round(credit, 2),
            max_loss=round(max(spot - credit, 0.0) * 100, 2),
            max_profit=round(max((strike - spot) + credit, 0.0) * 100, 2),
            breakevens=[round(spot - credit, 2)],
            width=round(max(strike - spot, 0.0), 2),
            capital_required=round(spot * 100, 2),
            probability_hint=62.0,
            score=_covered_call_score(call_leg, spot),
            natural_exit=_natural_exit("covered_call", max_profit=round(max((strike - spot) + credit, 0.0) * 100, 2), credit=credit, width=round(max(strike - spot, 0.0), 2)),
            fit_notes=["需要底仓100股", "收租型策略"],
            hard_flags=["requires_stock_position"],
            summary=f"备兑 · {_candidate_value(call_leg, 'expiration') or ''}",
        )
    )
    return rows


def _build_cash_secured_put(candidates: list[Any], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    puts = [item for item in candidates if str(_candidate_value(item, "side")).lower() == "put"]
    put_leg = _select_otm_leg(puts, spot, "put", prefer_higher=False)
    if not put_leg:
        return rows
    credit = max(0.05, _num(_candidate_value(put_leg, "bid")))
    strike = _num(_candidate_value(put_leg, "strike"))
    max_profit = round(credit * 100, 2)
    max_loss = round(max(strike - credit, 0.0) * 100, 2)
    rows.append(
        StrategyCandidate(
            family="cash_secured_put",
            strategy_type="cash_secured_put",
            label="现金担保Put",
            direction="neutral_to_bullish",
            expiration=str(_candidate_value(put_leg, "expiration") or ""),
            legs=[_leg_dict(put_leg, "sell", 1, _num(_candidate_value(put_leg, "bid")))],
            net_debit=0.0,
            net_credit=round(credit, 2),
            max_loss=max_loss,
            max_profit=max_profit,
            breakevens=[round(strike - credit, 2)],
            width=0.0,
            capital_required=round(strike * 100, 2),
            probability_hint=64.0,
            score=_covered_call_score(put_leg, spot),
            natural_exit=_natural_exit("cash_secured_put", max_profit=max_profit, credit=credit, width=0.0),
            fit_notes=["需要现金担保", "适合愿意低价接股"],
            hard_flags=["requires_cash_secured"],
            summary=f"现金担保Put · {_candidate_value(put_leg, 'expiration') or ''} · K {strike:.2f}",
        )
    )
    return rows


def _build_calendar_spreads(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    expirations = _ordered_expirations(by_expiration)
    if len(expirations) < 2:
        return rows
    for near_exp, far_exp in _expiration_pairs(expirations):
        for side in ("call", "put"):
            near_legs = _rank_candidates(by_expiration[near_exp][side])
            far_legs = _rank_candidates(by_expiration[far_exp][side])
            pair = _same_strike_calendar_pair(near_legs, far_legs, spot)
            if not pair:
                continue
            near_leg, far_leg = pair
            strike = _num(_candidate_value(far_leg, "strike"))
            debit = max(0.05, _num(_candidate_value(far_leg, "ask")) - _num(_candidate_value(near_leg, "bid")))
            if debit <= 0:
                continue
            rows.append(
                StrategyCandidate(
                    family="calendar",
                    strategy_type=f"{side}_calendar_spread",
                    label=f"{'Call' if side == 'call' else 'Put'}日历价差",
                    direction="neutral",
                    expiration=f"{near_exp}/{far_exp}",
                    legs=[
                        _leg_dict(far_leg, "buy", 1, _num(_candidate_value(far_leg, "ask"))),
                        _leg_dict(near_leg, "sell", 1, _num(_candidate_value(near_leg, "bid"))),
                    ],
                    net_debit=round(debit, 2),
                    max_loss=round(debit * 100, 2),
                    max_profit=None,
                    breakevens=[strike],
                    width=0.0,
                    capital_required=round(debit * 100, 2),
                    probability_hint=56.0,
                    score=_calendar_score(near_leg, far_leg, debit, spot),
                    natural_exit=_natural_exit("calendar_spread", max_profit=None, credit=0.0, width=0.0),
                    fit_notes=["买远月卖近月", "受益于近月衰减和价格钉住"],
                    hard_flags=["term_structure_risk"],
                    summary=f"{'Call' if side == 'call' else 'Put'}日历价差 · K {strike:.2f} · {near_exp}/{far_exp}",
                )
            )
    return rows


def _build_diagonal_spreads(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    expirations = _ordered_expirations(by_expiration)
    if len(expirations) < 2:
        return rows
    for near_exp, far_exp in _expiration_pairs(expirations):
        rows.extend(_call_diagonal(near_exp, far_exp, by_expiration, spot))
        rows.extend(_put_diagonal(near_exp, far_exp, by_expiration, spot))
    return rows


def _build_poor_mans_covered_call(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    expirations = _ordered_expirations(by_expiration)
    if len(expirations) < 2:
        return rows
    for near_exp, far_exp in _expiration_pairs(expirations):
        far_calls = by_expiration[far_exp]["call"]
        near_calls = by_expiration[near_exp]["call"]
        long_call = _select_itm_leg(far_calls, spot, "call")
        short_call = _select_otm_leg(near_calls, spot, "call", prefer_higher=True)
        if not long_call or not short_call:
            continue
        long_strike = _num(_candidate_value(long_call, "strike"))
        short_strike = _num(_candidate_value(short_call, "strike"))
        if short_strike <= long_strike:
            continue
        debit = max(0.05, _num(_candidate_value(long_call, "ask")) - _num(_candidate_value(short_call, "bid")))
        width = max(short_strike - long_strike, 0.0)
        max_profit = round(max(width - debit, 0.0) * 100, 2) if width > 0 else None
        rows.append(
            StrategyCandidate(
                family="poor_mans_covered_call",
                strategy_type="poor_mans_covered_call",
                label="穷人备兑",
                direction="neutral_to_bullish",
                expiration=f"{near_exp}/{far_exp}",
                legs=[
                    _leg_dict(long_call, "buy", 1, _num(_candidate_value(long_call, "ask"))),
                    _leg_dict(short_call, "sell", 1, _num(_candidate_value(short_call, "bid"))),
                ],
                net_debit=round(debit, 2),
                max_loss=round(debit * 100, 2),
                max_profit=max_profit,
                breakevens=[round(long_strike + debit, 2)],
                width=round(width, 2),
                capital_required=round(debit * 100, 2),
                probability_hint=58.0,
                score=_calendar_score(short_call, long_call, debit, spot) + 4,
                natural_exit=_natural_exit("poor_mans_covered_call", max_profit=max_profit, credit=0.0, width=width),
                fit_notes=["用远月ITM Call替代正股", "卖近月Call降低持仓成本"],
                hard_flags=["diagonal_assignment_risk"],
                summary=f"穷人备兑 · {near_exp}/{far_exp} · {long_strike:.2f}/{short_strike:.2f}",
            )
        )
    return rows


def _build_iron_condor(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for expiration, sides in by_expiration.items():
        puts = sorted(_rank_candidates(sides["put"]), key=lambda item: _num(_candidate_value(item, "strike")))
        calls = sorted(_rank_candidates(sides["call"]), key=lambda item: _num(_candidate_value(item, "strike")))
        put_credit = _best_credit_pair(puts, reverse=True, side="put")
        call_credit = _best_credit_pair(calls, reverse=False, side="call")
        if not put_credit or not call_credit:
            continue
        sell_put, buy_put, put_credit_value, put_width = put_credit
        sell_call, buy_call, call_credit_value, call_width = call_credit
        credit = put_credit_value + call_credit_value
        width = min(put_width, call_width)
        max_loss = round(max(width - credit, 0.0) * 100, 2)
        max_profit = round(max(credit, 0.0) * 100, 2)
        rows.append(
            StrategyCandidate(
                family="iron_condor",
                strategy_type="iron_condor",
                label="铁鹰",
                direction="neutral",
                expiration=expiration,
                legs=[
                    _leg_dict(sell_put, "sell", 1, _num(_candidate_value(sell_put, "bid"))),
                    _leg_dict(buy_put, "buy", 1, _num(_candidate_value(buy_put, "ask"))),
                    _leg_dict(sell_call, "sell", 1, _num(_candidate_value(sell_call, "bid"))),
                    _leg_dict(buy_call, "buy", 1, _num(_candidate_value(buy_call, "ask"))),
                ],
                net_debit=0.0,
                net_credit=round(credit, 2),
                max_loss=max_loss,
                max_profit=max_profit,
                breakevens=[
                    round(_num(_candidate_value(sell_put, "strike")) - credit, 2),
                    round(_num(_candidate_value(sell_call, "strike")) + credit, 2),
                ],
                width=round(width, 2),
                capital_required=max_loss,
                probability_hint=67.0,
                score=_iron_condor_score(sell_put, buy_put, sell_call, buy_call, credit, width, spot),
                natural_exit=_natural_exit("iron_condor", max_profit=max_profit, credit=credit, width=width),
                fit_notes=["中性区间", "双边收权利金"],
                hard_flags=[],
                summary=f"铁鹰 · {expiration}",
            )
        )
    return rows


def _build_butterfly(by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    for expiration, sides in by_expiration.items():
        calls = sorted(_rank_candidates(sides["call"]), key=lambda item: _num(_candidate_value(item, "strike")))
        butterfly = _call_butterfly(expiration, calls, spot)
        if butterfly:
            rows.append(butterfly)
    return rows


def _call_butterfly(expiration: str, calls: list[Any], spot: float) -> StrategyCandidate | None:
    if len(calls) < 3:
        return None
    ordered = sorted(calls, key=lambda item: abs(_num(_candidate_value(item, "strike")) - spot))
    center = ordered[0]
    strike_values = sorted({_num(_candidate_value(item, "strike")) for item in calls})
    if len(strike_values) < 3:
        return None
    center_strike = _num(_candidate_value(center, "strike"))
    lower_strikes = [value for value in strike_values if value < center_strike]
    upper_strikes = [value for value in strike_values if value > center_strike]
    if not lower_strikes or not upper_strikes:
        return None
    low_strike = lower_strikes[-1]
    high_strike = upper_strikes[0]
    width = min(center_strike - low_strike, high_strike - center_strike)
    if width <= 0:
        return None
    low_leg = _pick_by_strike(calls, low_strike)
    high_leg = _pick_by_strike(calls, high_strike)
    if not low_leg or not high_leg:
        return None
    debit = max(0.05, _num(_candidate_value(low_leg, "ask")) + _num(_candidate_value(high_leg, "ask")) - 2 * _num(_candidate_value(center, "bid")))
    max_loss = round(debit * 100, 2)
    max_profit = round(max(width - debit, 0.0) * 100, 2)
    return StrategyCandidate(
        family="butterfly",
        strategy_type="call_butterfly",
        label="蝶式",
        direction="neutral",
        expiration=expiration,
        legs=[
            _leg_dict(low_leg, "buy", 1, _num(_candidate_value(low_leg, "ask"))),
            _leg_dict(center, "sell", 2, _num(_candidate_value(center, "bid"))),
            _leg_dict(high_leg, "buy", 1, _num(_candidate_value(high_leg, "ask"))),
        ],
        net_debit=round(debit, 2),
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=[round(low_strike + debit, 2), round(high_strike - debit, 2)],
        width=round(width, 2),
        capital_required=max_loss,
        probability_hint=54.0,
        score=_butterfly_score(center, low_leg, high_leg, debit, width, spot),
        natural_exit=_natural_exit("butterfly", max_profit=max_profit, credit=0.0, width=width),
        fit_notes=["中性收敛", "适合区间收窄"],
        hard_flags=[],
        summary=f"蝶式 · {expiration} · {center_strike:.2f}",
    )


def _call_diagonal(near_exp: str, far_exp: str, by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    far_calls = _rank_candidates(by_expiration[far_exp]["call"])
    near_calls = _rank_candidates(by_expiration[near_exp]["call"])
    long_leg = _select_otm_or_atm_leg(far_calls, spot, "call")
    if not long_leg:
        return rows
    long_strike = _num(_candidate_value(long_leg, "strike"))
    short_candidates = [leg for leg in near_calls if _num(_candidate_value(leg, "strike")) > long_strike]
    short_leg = _select_otm_leg(short_candidates, spot, "call", prefer_higher=True)
    if not short_leg:
        return rows
    rows.append(_build_diagonal_candidate("call_diagonal_spread", "Call对角价差", "bullish", near_exp, far_exp, long_leg, short_leg, spot))
    return rows


def _put_diagonal(near_exp: str, far_exp: str, by_expiration: dict[str, dict[str, list[Any]]], spot: float) -> list[StrategyCandidate]:
    rows: list[StrategyCandidate] = []
    far_puts = _rank_candidates(by_expiration[far_exp]["put"])
    near_puts = _rank_candidates(by_expiration[near_exp]["put"])
    long_leg = _select_otm_or_atm_leg(far_puts, spot, "put")
    if not long_leg:
        return rows
    long_strike = _num(_candidate_value(long_leg, "strike"))
    short_candidates = [leg for leg in near_puts if _num(_candidate_value(leg, "strike")) < long_strike]
    short_leg = _select_otm_leg(short_candidates, spot, "put", prefer_higher=False)
    if not short_leg:
        return rows
    rows.append(_build_diagonal_candidate("put_diagonal_spread", "Put对角价差", "bearish", near_exp, far_exp, long_leg, short_leg, spot))
    return rows


def _build_diagonal_candidate(
    strategy_type: str,
    label: str,
    direction: str,
    near_exp: str,
    far_exp: str,
    long_leg: Any,
    short_leg: Any,
    spot: float,
) -> StrategyCandidate:
    long_strike = _num(_candidate_value(long_leg, "strike"))
    short_strike = _num(_candidate_value(short_leg, "strike"))
    debit = max(0.05, _num(_candidate_value(long_leg, "ask")) - _num(_candidate_value(short_leg, "bid")))
    width = abs(short_strike - long_strike)
    max_profit = round(max(width - debit, 0.0) * 100, 2) if width > 0 else None
    return StrategyCandidate(
        family="diagonal",
        strategy_type=strategy_type,
        label=label,
        direction=direction,
        expiration=f"{near_exp}/{far_exp}",
        legs=[
            _leg_dict(long_leg, "buy", 1, _num(_candidate_value(long_leg, "ask"))),
            _leg_dict(short_leg, "sell", 1, _num(_candidate_value(short_leg, "bid"))),
        ],
        net_debit=round(debit, 2),
        max_loss=round(debit * 100, 2),
        max_profit=max_profit,
        breakevens=[round(long_strike + debit if direction == "bullish" else long_strike - debit, 2)],
        width=round(width, 2),
        capital_required=round(debit * 100, 2),
        probability_hint=_probability_hint(long_leg, short_leg, direction=direction),
        score=_calendar_score(short_leg, long_leg, debit, spot),
        natural_exit=_natural_exit("diagonal_spread", max_profit=max_profit, credit=0.0, width=width),
        fit_notes=["买远月卖近月", "带方向倾斜的日历结构"],
        hard_flags=["diagonal_assignment_risk"],
        summary=f"{label} · {near_exp}/{far_exp} · {long_strike:.2f}/{short_strike:.2f}",
    )


def _best_credit_pair(legs: list[Any], reverse: bool, side: str) -> tuple[Any, Any, float, float] | None:
    if len(legs) < 2:
        return None
    ordered = sorted(legs, key=lambda item: _num(_candidate_value(item, "strike")), reverse=reverse)
    for idx, sell_leg in enumerate(ordered):
        for buy_leg in ordered[idx + 1 : idx + 5]:
            if side == "put":
                if _num(_candidate_value(sell_leg, "strike")) <= _num(_candidate_value(buy_leg, "strike")):
                    continue
                width = _num(_candidate_value(sell_leg, "strike")) - _num(_candidate_value(buy_leg, "strike"))
            else:
                if _num(_candidate_value(buy_leg, "strike")) <= _num(_candidate_value(sell_leg, "strike")):
                    continue
                width = _num(_candidate_value(buy_leg, "strike")) - _num(_candidate_value(sell_leg, "strike"))
            credit = max(0.05, _num(_candidate_value(sell_leg, "bid")) - _num(_candidate_value(buy_leg, "ask")))
            if width > 0 and credit > 0:
                return sell_leg, buy_leg, credit, width
    return None


def _rank_candidates(candidates: list[Any]) -> list[Any]:
    return sorted(candidates, key=lambda item: _candidate_value(item, "analysis_score") or _candidate_value(item, "score") or 0, reverse=True)[:6]


def _ordered_expirations(by_expiration: dict[str, dict[str, list[Any]]]) -> list[str]:
    return sorted(by_expiration.keys(), key=lambda item: (_expiration_sort_key(item), item))


def _expiration_pairs(expirations: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for near_index, near_exp in enumerate(expirations[:-1]):
        for far_exp in expirations[near_index + 1 : near_index + 4]:
            pairs.append((near_exp, far_exp))
    return pairs[:8]


def _expiration_sort_key(value: str) -> tuple[int, int, int]:
    try:
        year, month, day = [int(part) for part in str(value).split("-")[:3]]
        return year, month, day
    except (TypeError, ValueError):
        return 9999, 12, 31


def _same_strike_calendar_pair(near_legs: list[Any], far_legs: list[Any], spot: float) -> tuple[Any, Any] | None:
    best: tuple[Any, Any] | None = None
    best_distance = float("inf")
    for near_leg in near_legs:
        near_strike = _num(_candidate_value(near_leg, "strike"))
        for far_leg in far_legs:
            far_strike = _num(_candidate_value(far_leg, "strike"))
            if abs(near_strike - far_strike) > 1e-6:
                continue
            distance = abs(far_strike - spot)
            if distance < best_distance:
                best = (near_leg, far_leg)
                best_distance = distance
    return best


def _select_otm_leg(candidates: list[Any], spot: float, side: str, prefer_higher: bool) -> Any | None:
    eligible = []
    for candidate in candidates:
        strike = _num(_candidate_value(candidate, "strike"))
        if side == "call" and strike <= spot:
            continue
        if side == "put" and strike >= spot:
            continue
        eligible.append(candidate)
    if not eligible:
        eligible = list(candidates)
    if not eligible:
        return None
    eligible = sorted(eligible, key=lambda item: (abs(_num(_candidate_value(item, "strike")) - spot), -(_num(_candidate_value(item, "score")) or 0)))
    if prefer_higher and side == "call":
        eligible = sorted(eligible, key=lambda item: (_num(_candidate_value(item, "strike")) - spot, -(_num(_candidate_value(item, "score")) or 0)))
    elif not prefer_higher and side == "put":
        eligible = sorted(eligible, key=lambda item: (spot - _num(_candidate_value(item, "strike")), -(_num(_candidate_value(item, "score")) or 0)))
    return eligible[0] if eligible else None


def _select_otm_or_atm_leg(candidates: list[Any], spot: float, side: str) -> Any | None:
    if not candidates:
        return None
    if side == "call":
        eligible = [item for item in candidates if _num(_candidate_value(item, "strike")) >= spot]
    else:
        eligible = [item for item in candidates if _num(_candidate_value(item, "strike")) <= spot]
    if not eligible:
        eligible = list(candidates)
    return min(eligible, key=lambda item: abs(_num(_candidate_value(item, "strike")) - spot))


def _select_itm_leg(candidates: list[Any], spot: float, side: str) -> Any | None:
    if side == "call":
        eligible = [item for item in candidates if _num(_candidate_value(item, "strike")) < spot]
    else:
        eligible = [item for item in candidates if _num(_candidate_value(item, "strike")) > spot]
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda item: (
            -abs(_num(_candidate_value(item, "delta"))),
            abs(_num(_candidate_value(item, "strike")) - spot),
            -_num(_candidate_value(item, "score") or _candidate_value(item, "analysis_score")),
        ),
    )[0]


def _closest_strike_pair(calls: list[Any], puts: list[Any], spot: float) -> tuple[Any, Any] | None:
    if not calls or not puts:
        return None
    call = min(calls, key=lambda item: abs(_num(_candidate_value(item, "strike")) - spot))
    strike = _num(_candidate_value(call, "strike"))
    put = min(puts, key=lambda item: abs(_num(_candidate_value(item, "strike")) - strike))
    return call, put


def _pick_by_strike(candidates: list[Any], strike: float) -> Any | None:
    for candidate in candidates:
        if abs(_num(_candidate_value(candidate, "strike")) - strike) < 1e-6:
            return candidate
    return None


def _leg_dict(candidate: Any, action: str, qty: int, price: float) -> dict[str, Any]:
    ask = _num(_candidate_value(candidate, "ask"))
    bid = _num(_candidate_value(candidate, "bid"))
    return {
        "contract_symbol": str(_candidate_value(candidate, "contract_symbol") or ""),
        "expiration": str(_candidate_value(candidate, "expiration") or ""),
        "side": str(_candidate_value(candidate, "side") or ""),
        "strike": _num(_candidate_value(candidate, "strike")),
        "action": action,
        "qty": qty,
        "price": round(price, 2),
        "bid": bid,
        "ask": ask,
        "spread_pct": _spread_pct(bid, ask),
        "price_source": "ask" if action == "buy" else "bid",
        "delta": _num(_candidate_value(candidate, "delta")),
        "theta_per_day": _num(_candidate_value(candidate, "theta_per_day")),
        "analysis_score": _num(_candidate_value(candidate, "analysis_score")),
    }


def _enrich_strategy_candidate(candidate: StrategyCandidate, gate: dict[str, Any] | None = None) -> StrategyCandidate:
    quote_score, quote_state, quote_flags = _quote_consistency(candidate)
    template_gate = evaluate_strategy_template(candidate, gate)
    structure_fit = _structure_fit_score(candidate, gate)
    payoff_quality = _payoff_quality_score(candidate)
    execution_complexity = _execution_complexity_score(candidate, quote_score)
    capital_efficiency = _capital_efficiency_score(candidate)
    risk_defined = _risk_defined_score(candidate)
    hard_flags = list(dict.fromkeys([*candidate.hard_flags, *quote_flags]))
    if template_gate.get("allowed") is False:
        hard_flags.append("strategy_template_blocked")
    score = (
        structure_fit * 0.30
        + payoff_quality * 0.24
        + execution_complexity * 0.18
        + capital_efficiency * 0.16
        + risk_defined * 0.12
    )
    score += float(template_gate.get("score_adjustment") or 0)
    if any(flag in hard_flags for flag in {"bad_long_ask", "short_leg_bid_unavailable", "net_price_inconsistent"}):
        score -= 35
    if "strategy_template_blocked" in hard_flags:
        score -= 45
    if "requires_stock_position" in hard_flags:
        score -= 6
    fit_notes = list(candidate.fit_notes)
    route_note = _strategy_route_note(candidate, gate)
    if route_note and route_note not in fit_notes:
        fit_notes.append(route_note)
    if "requires_stock_position" in hard_flags and "需要正股支撑" not in fit_notes:
        fit_notes.append("需要正股支撑")
    for note in (template_gate.get("blockers") or [])[:2]:
        fit_notes.append(f"模板阻止：{note}")
    for note in (template_gate.get("warnings") or [])[:2]:
        fit_notes.append(f"模板提示：{note}")
    if not template_gate.get("blockers"):
        for note in (template_gate.get("reasons") or [])[:2]:
            fit_notes.append(f"模板通过：{note}")
    fit_notes = list(dict.fromkeys(fit_notes))
    return replace(
        candidate,
        score=round(max(score, 0.0), 2),
        structure_fit_score=round(structure_fit, 2),
        payoff_quality_score=round(payoff_quality, 2),
        execution_complexity_score=round(execution_complexity, 2),
        capital_efficiency_score=round(capital_efficiency, 2),
        risk_defined_score=round(risk_defined, 2),
        quote_consistency_score=round(quote_score, 2),
        quote_consistency_state=quote_state,
        strategy_template_gate=template_gate,
        fit_notes=fit_notes,
        hard_flags=hard_flags,
        natural_exit=candidate.natural_exit or _natural_exit(candidate.strategy_type, max_profit=candidate.max_profit, credit=candidate.net_credit, width=candidate.width),
    )


def _quote_consistency(candidate: StrategyCandidate) -> tuple[float, str, list[str]]:
    flags: list[str] = []
    net = 0.0
    option_legs = 0
    total_spread = 0.0
    for leg in candidate.legs:
        if str(leg.get("side") or "").lower() == "stock":
            continue
        option_legs += 1
        action = str(leg.get("action") or "").lower()
        qty = max(1, int(_num(leg.get("qty")) or 1))
        price = _num(leg.get("price"))
        bid = _num(leg.get("bid"))
        ask = _num(leg.get("ask"))
        spread_pct = _num(leg.get("spread_pct"))
        total_spread += spread_pct
        if action == "buy":
            if ask <= 0 or price <= 0:
                flags.append("bad_long_ask")
            net += ask * qty
        elif action == "sell":
            if bid <= 0 or price <= 0:
                flags.append("short_leg_bid_unavailable")
            net -= bid * qty
        if spread_pct > 30:
            flags.append("leg_wide_spread")
    if option_legs <= 0:
        return 60.0, "stock_only_or_unknown", flags
    expected_debit = max(net, 0.0)
    expected_credit = max(-net, 0.0)
    if candidate.net_debit > 0 and abs(expected_debit - candidate.net_debit) > max(0.05, candidate.net_debit * 0.25):
        flags.append("net_price_inconsistent")
    if candidate.net_credit > 0 and abs(expected_credit - candidate.net_credit) > max(0.05, candidate.net_credit * 0.25):
        flags.append("net_price_inconsistent")
    avg_spread = total_spread / option_legs
    score = max(0.0, 100.0 - avg_spread * 1.6 - len(flags) * 18)
    state = "ok" if not flags and avg_spread <= 20 else "caution" if not any(flag in flags for flag in {"bad_long_ask", "short_leg_bid_unavailable", "net_price_inconsistent"}) else "blocked"
    return score, state, list(dict.fromkeys(flags))


def _structure_fit_score(candidate: StrategyCandidate, gate: dict[str, Any] | None = None) -> float:
    gate = gate or {}
    preferred = set(gate.get("preferred_strategy_families") or [])
    vote_summary = gate.get("vote_summary") or {}
    majority = str(vote_summary.get("majority_direction") or gate.get("direction_bias") or "neutral")
    regime = str(gate.get("regime") or "")
    score = 45.0
    if candidate.family in preferred:
        score += 28.0
    if candidate.family == "spread" and "spread" in preferred:
        score += 10.0
    if candidate.family == "credit_spread" and {"spread", "credit_spread"} & preferred:
        score += 10.0
    if candidate.family in {"calendar", "diagonal"} and "calendar" in preferred:
        score += 8.0
    if candidate.direction in {majority, f"neutral_to_{majority}"}:
        score += 16.0
    if candidate.direction == "neutral" and regime in {"range", "choppy"}:
        score += 18.0
    if candidate.family in {"straddle", "strangle"} and majority == "neutral" and _num(gate.get("avg_iv_percentile")) >= 70:
        score += 14.0
    if not preferred and majority == "neutral":
        score -= 24.0
    return max(0.0, min(score, 100.0))


def _strategy_route_note(candidate: StrategyCandidate, gate: dict[str, Any] | None = None) -> str:
    gate = gate or {}
    preferred = set(gate.get("preferred_strategy_families") or [])
    if candidate.family in preferred:
        return "匹配当前市场环境路由"
    if preferred:
        return "非首选结构，需人工确认"
    return "方向不清，低优先级"


def _payoff_quality_score(candidate: StrategyCandidate) -> float:
    max_loss = max(_num(candidate.max_loss), 1.0)
    if candidate.max_profit is None:
        return 70.0 if candidate.max_loss > 0 else 35.0
    ratio = _num(candidate.max_profit) / max_loss
    score = 35.0 + min(ratio * 35, 45.0) + min(candidate.probability_hint / 5, 18.0)
    return max(0.0, min(score, 100.0))


def _execution_complexity_score(candidate: StrategyCandidate, quote_score: float) -> float:
    option_legs = sum(1 for leg in candidate.legs if str(leg.get("side") or "").lower() != "stock")
    complexity_penalty = max(option_legs - 1, 0) * 8
    return max(0.0, min(quote_score - complexity_penalty, 100.0))


def _capital_efficiency_score(candidate: StrategyCandidate) -> float:
    capital = max(_num(candidate.capital_required), _num(candidate.max_loss), 1.0)
    if candidate.max_profit is None:
        return 62.0
    return max(0.0, min((_num(candidate.max_profit) / capital) * 65 + min(candidate.probability_hint / 3, 25), 100.0))


def _risk_defined_score(candidate: StrategyCandidate) -> float:
    if candidate.family == "cash_secured_put":
        return 58.0
    if candidate.family in {"covered_call", "collar"}:
        return 70.0
    if candidate.max_loss > 0 and candidate.max_profit is not None:
        return 92.0
    if candidate.max_loss > 0 and candidate.family in {"straddle", "strangle"}:
        return 78.0
    if candidate.max_loss > 0 and candidate.family in {"calendar", "diagonal", "poor_mans_covered_call"}:
        return 76.0
    return 35.0


def _natural_exit(strategy_type: str, *, max_profit: float | None, credit: float, width: float) -> dict[str, Any]:
    if strategy_type in {"bull_call_spread", "bear_put_spread"}:
        return {
            "take_profit": "最大收益的 40%-60%",
            "stop_loss": "组合亏损达到初始净支出的 40%-50%",
            "exit_logic": "触发后按相反方向平掉所有已成交腿",
        }
    if strategy_type in {"bull_put_spread", "bear_call_spread"}:
        return {
            "take_profit": "收回权利金的 50%-70%",
            "stop_loss": "亏损达到收取权利金的 1.5-2 倍",
            "exit_logic": "触发后按相反方向平掉所有已成交腿",
        }
    if strategy_type == "cash_secured_put":
        return {
            "take_profit": "权利金回吐 50%-70% 时买回短 put",
            "stop_loss": "正股跌破接股失效位或亏损达到权利金的 1.5-2 倍",
            "exit_logic": "默认买回短 put；接股风险需要用户确认",
        }
    if strategy_type == "calendar_spread":
        return {
            "take_profit": "组合盈利达到初始净支出的 30%-60% 或近月衰减完成",
            "stop_loss": "价格远离共同执行价或组合亏损 30%-45%",
            "exit_logic": "整组平仓，避免只保留远月裸方向",
        }
    if strategy_type in {"diagonal_spread", "poor_mans_covered_call"}:
        return {
            "take_profit": "短腿回吐 50%-70% 或组合盈利达到初始净支出的 35%-60%",
            "stop_loss": "正股突破短腿风险区或组合亏损 30%-45%",
            "exit_logic": "优先整组平仓；如滚动短腿需人工确认",
        }
    if strategy_type == "iron_condor":
        return {
            "take_profit": "收回权利金的 50%-70%",
            "stop_loss": "单侧突破或亏损达到权利金的 1.5-2 倍",
            "exit_logic": "优先整组平仓，避免单边裸露",
        }
    if strategy_type == "butterfly":
        return {
            "take_profit": "正股接近中间行权价且组合盈利达最大收益 35%-55%",
            "stop_loss": "正股脱离蝶式主体区间或组合亏损 35%-50%",
            "exit_logic": "整组平仓",
        }
    if strategy_type in {"long_straddle", "long_strangle"}:
        return {
            "take_profit": "组合盈利 40%-80% 或隐波继续扩张后分批止盈",
            "stop_loss": "IV 回落或组合亏损 30%-45%",
            "exit_logic": "整组平仓，避免只留单边方向暴露",
        }
    if strategy_type == "covered_call":
        return {
            "take_profit": "权利金回吐 50%-70% 时买回短 call",
            "stop_loss": "正股跌破失效位或短 call 风险不可接受时调整",
            "exit_logic": "默认只处理短 call，底仓由用户确认",
        }
    if strategy_type == "collar":
        return {
            "take_profit": "保护目标完成或上方 call 接近被行权时调整",
            "stop_loss": "正股跌破保护区间后按底仓计划处理",
            "exit_logic": "整组期权腿调整，底仓由用户确认",
        }
    return {
        "take_profit": "按组合收益/风险比达到 1:1 后复核",
        "stop_loss": "组合亏损达到计划风险 35%-50%",
        "exit_logic": "按相反方向平掉所有已成交腿",
    }


def _spread_pct(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100 if mid > 0 else 100.0


def _pair_score(long_leg: Any, short_leg: Any, debit: float, width: float, max_profit: float | None, max_loss: float, spot: float, credit_spread: bool = False) -> float:
    long_score = _num(_candidate_value(long_leg, "analysis_score") or _candidate_value(long_leg, "score"))
    short_score = _num(_candidate_value(short_leg, "analysis_score") or _candidate_value(short_leg, "score"))
    execution = max(0.0, 6.0 - (_num(_candidate_value(long_leg, "spread_pct")) + _num(_candidate_value(short_leg, "spread_pct"))) / 12.0)
    efficiency = 0.0
    if max_loss > 0:
        efficiency = (float(max_profit or 0.0) / max_loss) * 18 if not credit_spread else (float(max_profit or 0.0) / max_loss) * 14
    distance = max(0.0, 4.0 - abs(((spot - _num(_candidate_value(long_leg, "strike"))) / spot) * 100))
    return round(long_score * 0.48 + short_score * 0.34 + execution + efficiency + distance, 2)


def _neutral_score(calls: list[Any], puts: list[Any], debit: float, spot: float, wingy: bool = False) -> float:
    best_call = max((_num(_candidate_value(item, "score") or _candidate_value(item, "analysis_score")) for item in calls), default=0.0)
    best_put = max((_num(_candidate_value(item, "score") or _candidate_value(item, "analysis_score")) for item in puts), default=0.0)
    width_penalty = 2.0 if wingy else 0.0
    premium_penalty = min(debit * 5, 6)
    spot_bonus = max(0.0, 4.0 - abs(spot / max(spot, 1.0) - 1) * 100)
    return round((best_call + best_put) * 0.22 + spot_bonus - premium_penalty - width_penalty, 2)


def _collar_score(call_leg: Any, put_leg: Any, spot: float) -> float:
    return round((_num(_candidate_value(call_leg, "score")) + _num(_candidate_value(put_leg, "score"))) * 0.3 + 18.0, 2)


def _covered_call_score(call_leg: Any, spot: float) -> float:
    distance = abs(_num(_candidate_value(call_leg, "strike")) - spot) / max(spot, 1.0) * 100
    return round(_num(_candidate_value(call_leg, "score")) * 0.25 + max(0.0, 20 - distance), 2)


def _iron_condor_score(sell_put: Any, buy_put: Any, sell_call: Any, buy_call: Any, credit: float, width: float, spot: float) -> float:
    leg_score = sum(_num(_candidate_value(item, "score")) for item in (sell_put, buy_put, sell_call, buy_call)) / 4
    efficiency = (credit / max(width, 0.01)) * 28
    midpoint = (_num(_candidate_value(sell_put, "strike")) + _num(_candidate_value(sell_call, "strike"))) / 2
    neutral = max(0.0, 8.0 - abs(spot - midpoint) / max(spot, 1.0) * 100)
    return round(leg_score * 0.2 + efficiency + neutral, 2)


def _butterfly_score(center: Any, low_leg: Any, high_leg: Any, debit: float, width: float, spot: float) -> float:
    leg_score = (_num(_candidate_value(center, "score")) + _num(_candidate_value(low_leg, "score")) + _num(_candidate_value(high_leg, "score"))) / 3
    pin_bonus = max(0.0, 10 - abs(_num(_candidate_value(center, "strike")) - spot) / max(spot, 1.0) * 100)
    efficiency = max(0.0, (width / max(debit, 0.01)) - 1) * 4
    return round(leg_score * 0.22 + pin_bonus + efficiency, 2)


def _calendar_score(short_leg: Any, long_leg: Any, debit: float, spot: float) -> float:
    short_score = _num(_candidate_value(short_leg, "score") or _candidate_value(short_leg, "analysis_score"))
    long_score = _num(_candidate_value(long_leg, "score") or _candidate_value(long_leg, "analysis_score"))
    strike = _num(_candidate_value(long_leg, "strike"))
    pin_bonus = max(0.0, 12 - abs(strike - spot) / max(spot, 1.0) * 100)
    debit_penalty = min(debit * 2.5, 8)
    theta_edge = max(0.0, abs(_num(_candidate_value(short_leg, "theta_per_day"))) - abs(_num(_candidate_value(long_leg, "theta_per_day"))) * 0.4) * 20
    return round(short_score * 0.22 + long_score * 0.26 + pin_bonus + min(theta_edge, 8) - debit_penalty, 2)


def _probability_hint(*legs: Any, direction: str) -> float:
    deltas = [abs(_num(_candidate_value(leg, "delta"))) for leg in legs if leg is not None]
    if not deltas:
        return 50.0
    avg = sum(deltas) / len(deltas)
    if direction == "neutral":
        return round(max(45.0, 100 - avg * 55), 2)
    if direction == "bullish":
        return round(max(40.0, 50 + avg * 25), 2)
    if direction == "bearish":
        return round(max(40.0, 50 + avg * 25), 2)
    return round(max(40.0, 100 - avg * 50), 2)


def _candidate_value(candidate: Any, key: str) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(key)
    return getattr(candidate, key, None)


def _normalize_mode(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    aliases = {
        "single": "single_leg",
        "single_leg": "single_leg",
        "单腿": "single_leg",
        "spread": "spread",
        "价差": "spread",
        "vertical": "spread",
        "straddle": "straddle",
        "跨式": "straddle",
        "strangle": "strangle",
        "宽跨": "strangle",
        "collar": "collar",
        "领式": "collar",
        "covered_call": "covered_call",
        "covered call": "covered_call",
        "备兑": "covered_call",
        "cash_secured_put": "cash_secured_put",
        "cash secured put": "cash_secured_put",
        "secured put": "cash_secured_put",
        "现金担保put": "cash_secured_put",
        "现金担保": "cash_secured_put",
        "卖put": "cash_secured_put",
        "credit_spread": "credit_spread",
        "credit spread": "credit_spread",
        "信用价差": "credit_spread",
        "calendar": "calendar",
        "calendar spread": "calendar",
        "日历价差": "calendar",
        "diagonal": "diagonal",
        "diagonal spread": "diagonal",
        "对角价差": "diagonal",
        "poor_mans_covered_call": "poor_mans_covered_call",
        "poor man's covered call": "poor_mans_covered_call",
        "pmcc": "poor_mans_covered_call",
        "穷人备兑": "poor_mans_covered_call",
        "iron_condor": "iron_condor",
        "iron condor": "iron_condor",
        "铁鹰": "iron_condor",
        "butterfly": "butterfly",
        "蝶式": "butterfly",
    }
    return aliases.get(text)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

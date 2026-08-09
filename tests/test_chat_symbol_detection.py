"""Tests for chat symbol detection and agentic tool-spec gating."""
from __future__ import annotations

import ai_option_scanner.web_api as web_api
from ai_option_scanner.web_api import (
    _clean_chat_symbol,
    _extract_symbols,
    _chat_tool_specs,
    _CHAT_TOOL_IDS,
    _run_tool_gex,
    _agent_final_messages,
    _strip_model_tool_markup,
)


def test_clean_chat_symbol_rejects_common_words():
    assert _clean_chat_symbol("study") is None
    assert _clean_chat_symbol("scan") is None
    assert _clean_chat_symbol("check") is None
    assert _clean_chat_symbol("leap") is None
    assert _clean_chat_symbol("") is None
    assert _clean_chat_symbol("TOOLONG") is None  # >5 letters


def test_clean_chat_symbol_accepts_tickers():
    assert _clean_chat_symbol("SPY") == "SPY"
    assert _clean_chat_symbol("aapl") == "AAPL"
    assert _clean_chat_symbol("NVDA.US") == "NVDA"


def test_extract_symbols_ignores_request_verbs():
    # The reported bug: "study on SPY" must not treat STUDY as a ticker.
    assert _extract_symbols("study on SPY") == ["SPY"]
    syms = _extract_symbols("check the option chain in the near 5 days for AAPL")
    assert syms == ["AAPL"]
    syms2 = _extract_symbols("study a cheap LEAP call on AAPL")
    assert "AAPL" in syms2 and "STUDY" not in syms2 and "LEAP" not in syms2


def test_chat_tool_specs_gate_account_tools():
    no_acct = {s["function"]["name"] for s in _chat_tool_specs(set(_CHAT_TOOL_IDS), has_lb_account=False)}
    # Account-only tools dropped; non-account tools kept.
    assert "longbridge_quote" not in no_acct
    assert "lb_option_chain" not in no_acct
    assert "thetadata_stock_market" in no_acct
    assert "thetadata_option_chain" in no_acct
    assert "yfinance_market" in no_acct

    with_acct = {s["function"]["name"] for s in _chat_tool_specs(set(_CHAT_TOOL_IDS), has_lb_account=True)}
    assert "longbridge_quote" in with_acct
    assert "lb_option_chain" in with_acct


def test_chat_tool_specs_have_valid_openai_shape():
    specs = _chat_tool_specs(set(_CHAT_TOOL_IDS), has_lb_account=True)
    assert specs, "expected at least one tool spec"
    for spec in specs:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert "symbol" in fn["parameters"]["properties"]
        assert fn["parameters"]["required"] == ["symbol"]


def test_gex_prefers_thetadata_before_delayed_yfinance(monkeypatch):
    calls = []

    def fake_fetch_gex(symbol, source, spot, owner_id=None, max_days=120):
        calls.append((symbol, source, spot, owner_id, max_days))
        return {
            "available": True,
            "source": source,
            "regime": "positive_gamma",
            "net_gex": 123.0,
        }

    monkeypatch.delenv("AI_OPTION_CHAT_GEX_SOURCE_ORDER", raising=False)
    monkeypatch.setattr(web_api, "fetch_gex", fake_fetch_gex)

    _, _, ctx, _, _, trace = _run_tool_gex("NVDA", "NVDA.US", 200.0, "owner-1")

    assert calls == [("NVDA", "thetadata", 200.0, "owner-1", 60)]
    assert "Gamma暴露(thetadata)" in ctx
    assert trace["status"] == "done"
    assert trace["result"]["source"] == "thetadata"


def test_gex_uses_yfinance_only_after_preferred_sources_fail(monkeypatch):
    calls = []

    def fake_fetch_gex(symbol, source, spot, owner_id=None, max_days=120):
        calls.append((symbol, source))
        if source == "yfinance":
            return {
                "available": True,
                "source": source,
                "regime": "negative_gamma",
                "net_gex": -456.0,
            }
        return {"available": False, "error": f"{source} unavailable"}

    monkeypatch.delenv("AI_OPTION_CHAT_GEX_SOURCE_ORDER", raising=False)
    monkeypatch.setattr(web_api, "fetch_gex", fake_fetch_gex)

    _, _, _, _, _, trace = _run_tool_gex("NVDA", "NVDA.US", 200.0, "owner-1")

    assert calls == [
        ("NVDA", "thetadata"),
        ("NVDA.US", "longbridge"),
        ("NVDA", "yfinance"),
    ]
    assert trace["status"] == "done"
    assert trace["result"]["source"] == "yfinance"


def test_agent_final_messages_use_plain_text_tool_context():
    class RequestStub:
        history = []

    messages = _agent_final_messages(
        "system",
        RequestStub(),
        "分析 NVDA",
        ["[gex_snapshot]\nGamma暴露(thetadata): regime=positive_gamma"],
    )

    assert all("tool_calls" not in message for message in messages)
    assert messages[-1]["role"] == "user"
    assert "Gamma暴露(thetadata)" in messages[-1]["content"]
    assert "禁止输出 tool_calls" in messages[0]["content"]


def test_strip_model_tool_markup_removes_dsml_blocks():
    raw = (
        "先看结论。\n"
        "<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"gex_snapshot\"></｜｜DSML｜｜invoke></｜｜DSML｜｜tool_calls>"
    )

    assert _strip_model_tool_markup(raw) == "先看结论。"
    assert _strip_model_tool_markup("<｜｜DSML｜｜tool_calls>x</｜｜DSML｜｜tool_calls>") == ""

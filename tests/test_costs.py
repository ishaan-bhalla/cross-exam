"""Per-answer usage and cost (crossexam.costs) and LLM token tracking."""
from types import SimpleNamespace
import pytest
from crossexam import costs
from crossexam.clients import llm_gateway

RATES = dict(cost_llm_input_per_million_tokens="", cost_llm_output_per_million_tokens="",
             cost_tts_per_million_chars="", cost_stt_per_hour="")


def test_unknown_rates_give_usage_but_no_cost(monkeypatch):
    monkeypatch.setattr(costs, "get_settings", lambda: SimpleNamespace(**RATES))
    u = costs.usage([{"input_tokens": 900, "output_tokens": 40}, {"input_tokens": 300, "output_tokens": 10}],
                    tts_chars=180, tts_audio_seconds=9.5, stt_audio_seconds=4.0)
    assert (u["llm_calls"], u["llm_input_tokens"], u["llm_output_tokens"], u["tts_chars"]) == (2, 1200, 50, 180)
    assert u["cost_usd"] == {"llm": None, "tts": None, "stt": None, "total": None}
    assert costs.describe(u).endswith("cost not measured")


def test_known_rates_are_applied(monkeypatch):
    monkeypatch.setattr(costs, "get_settings", lambda: SimpleNamespace(**{
        **RATES, "cost_llm_input_per_million_tokens": "0.5", "cost_llm_output_per_million_tokens": "2",
        "cost_tts_per_million_chars": "10", "cost_stt_per_hour": "0.36"}))
    u = costs.usage([{"input_tokens": 1_000_000, "output_tokens": 500_000}], tts_chars=100_000, stt_audio_seconds=100)
    assert u["cost_usd"] == pytest.approx({"llm": 1.5, "tts": 1.0, "stt": 0.01, "total": 2.51})
    # A typed question streams no audio: STT costs nothing rather than being unknown.
    assert costs.usage([], stt_audio_seconds=None)["cost_usd"]["stt"] == 0.0


def test_gateway_records_token_usage_inside_track_usage(monkeypatch):
    reply = {"choices": [{"message": {"content": "OK"}}], "usage": {"input_tokens": 15, "output_tokens": 2}}
    monkeypatch.setattr(llm_gateway, "get_settings", lambda: SimpleNamespace(
        llm_max_retries=0, llm_gateway_url="http://x", assemblyai_api_key="k", llm_model="m", llm_max_tokens=5,
        llm_temperature=0, llm_timeout_seconds=1, llm_retry_backoff_seconds=0))
    monkeypatch.setattr(llm_gateway.requests, "post", lambda *a, **k: SimpleNamespace(
        status_code=200, headers={}, raise_for_status=lambda: None, json=lambda: reply))
    assert llm_gateway.chat([]) == "OK"                       # outside: nothing recorded, no error
    with llm_gateway.track_usage() as calls:
        llm_gateway.chat([]); llm_gateway.chat([])
    assert calls == [{"input_tokens": 15, "output_tokens": 2}] * 2

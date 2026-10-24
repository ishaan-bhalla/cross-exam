"""The Render deployment: deploy/render.env, and the demo's usage limits (crossexam.budget)."""
import re
from types import SimpleNamespace
import pytest
from dotenv import dotenv_values
from crossexam import budget
from crossexam.config import PROJECT_ROOT, Settings

RENDER_ENV = PROJECT_ROOT / "deploy" / "render.env"


@pytest.fixture
def clean_env(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_render_env_plus_the_key_satisfies_settings(clean_env, monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test-key")
    s = Settings(_env_file=RENDER_ENV)
    assert s.assemblyai_api_key == "test-key"
    assert (s.persist_uploads, s.api_host, s.embedding_batch_size) == (False, "0.0.0.0", 1)
    assert s.budget_llm_calls_per_day > 0 and s.limit_questions_per_day > 0


def test_render_env_without_the_key_is_incomplete(clean_env):
    with pytest.raises(Exception, match="assemblyai_api_key"):
        Settings(_env_file=RENDER_ENV)


def test_render_env_contains_no_key():
    values = dotenv_values(RENDER_ENV)
    assert "ASSEMBLYAI_API_KEY" not in values
    text = RENDER_ENV.read_text()
    assert not re.search(r"^\s*ASSEMBLYAI_API_KEY\s*=", text, re.M)
    assert not re.search(r"\b[0-9a-f]{32}\b", text)  # an AssemblyAI key is 32 hex characters


def test_render_env_sets_every_setting_except_the_key():
    documented = {k for k in dotenv_values(PROJECT_ROOT / ".env.example")}
    assert documented - {"ASSEMBLYAI_API_KEY"} == set(dotenv_values(RENDER_ENV))


# ------------------------------------------------------------------ limits

LIMITS = dict(limit_questions_per_minute=2, limit_questions_per_day=3, limit_spoken_seconds_per_day=60,
              limit_stt_sessions_per_day=2, limit_uploads_per_day=1, limit_ip_factor=2,
              budget_llm_calls_per_day=5, budget_tts_chars_per_day=100, budget_stt_seconds_per_day=0,
              budget_stt_sessions_per_day=3, budget_cost_usd_per_day=0, budget_state_file="", demo_paused=False,
              cost_llm_input_per_million_tokens="", cost_llm_output_per_million_tokens="",
              cost_tts_per_million_chars="", cost_stt_per_hour="")


@pytest.fixture
def limits(monkeypatch):
    s = SimpleNamespace(**LIMITS, resolve=lambda p: p)
    monkeypatch.setattr(budget, "get_settings", lambda: s)
    from crossexam import costs
    monkeypatch.setattr(costs, "get_settings", lambda: s)
    budget.reset()
    yield s
    budget.reset()


def test_questions_per_minute_and_per_day(limits):
    keys = budget.client_keys("cookie-a", "10.0.0.1")
    for _ in range(2):
        budget.check_question(keys); budget.charge_question(keys)
    with pytest.raises(budget.LimitReached, match="in the last minute"):
        budget.check_question(keys)
    budget._minute.clear()  # a minute later
    budget.check_question(keys); budget.charge_question(keys)
    with pytest.raises(budget.LimitReached, match="3 questions today"):
        budget.check_question(keys)


def test_clearing_cookies_does_not_reset_the_ip_quota(limits):
    # The IP may ask limit_ip_factor (2) x 3 questions a day, whichever cookies it uses.
    for i in range(6):
        keys = budget.client_keys(f"cookie-{i}", "10.0.0.9")
        budget._minute.clear()
        budget.check_question(keys); budget.charge_question(keys)
    with pytest.raises(budget.LimitReached, match="Your network has asked 6 questions today"):
        budget.check_question(budget.client_keys("fresh-cookie", "10.0.0.9"))
    budget.check_question(budget.client_keys("fresh-cookie", "10.0.0.10"))  # another network is fine


def test_spoken_seconds_sessions_and_uploads(limits):
    keys = budget.client_keys("c", "10.0.0.2")
    budget.charge_spoken(keys, 61)
    with pytest.raises(budget.LimitReached, match="used this demo's 1 minutes of spoken questions"):
        budget.check_spoken(keys)
    keys = budget.client_keys("d", "10.0.0.3")
    budget.charge_stt_session(keys); budget.charge_stt_session(keys)
    with pytest.raises(budget.LimitReached, match="started the microphone"):
        budget.check_spoken(keys)
    budget.charge_upload(keys)
    with pytest.raises(budget.LimitReached, match="uploaded 1 statements today"):
        budget.check_upload(keys)


def test_global_budget_switches_to_demo_limit_mode(limits):
    assert budget.demo_limited() is None
    budget.record(llm_calls=5)
    assert budget.demo_limited() == budget.DEMO_LIMIT
    with pytest.raises(budget.LimitReached) as e:
        budget.check_demo()
    assert e.value.demo


def test_stt_budget_stops_only_spoken_questions(limits):
    for i in range(3):
        budget.charge_stt_session(budget.client_keys(f"v{i}", f"10.1.0.{i}"))
    assert budget.demo_limited() is None                       # typed questions carry on
    with pytest.raises(budget.LimitReached, match="microphone time for today"):
        budget.check_spoken(budget.client_keys("new", "10.2.0.1"))


def test_cost_budget_applies_once_rates_are_known(limits):
    budget.record(tts_chars=50)
    assert budget.demo_limited() is None
    limits.budget_cost_usd_per_day = 0.001
    limits.cost_llm_input_per_million_tokens = limits.cost_llm_output_per_million_tokens = "1"
    limits.cost_tts_per_million_chars, limits.cost_stt_per_hour = "30", "0.5"
    assert budget.demo_limited() == budget.DEMO_LIMIT          # 50 chars x $30/M = $0.0015


def test_demo_paused(limits):
    limits.demo_paused = True
    assert budget.demo_limited() == budget.DEMO_LIMIT


def test_state_file_survives_a_restart(limits, tmp_path):
    limits.budget_state_file = str(tmp_path / "budget.json")
    budget.record(tts_chars=40)
    budget.charge_question(budget.client_keys("c", None))
    budget._state = None                                       # a new process
    assert budget.status()["used"]["tts_chars"] == 40
    assert budget.client_status(budget.client_keys("c", None))["used"]["v:c"]["questions"] == 1


# ------------------------------------------------------------------ busy gateway

def test_rate_limited_gateway_waits_then_gives_up(monkeypatch):
    from crossexam.clients import llm_gateway
    s = SimpleNamespace(llm_max_retries=10, llm_gateway_url="http://x", assemblyai_api_key="k", llm_model="m",
                        llm_max_tokens=5, llm_temperature=0, llm_timeout_seconds=1, llm_retry_backoff_seconds=2,
                        llm_max_wait_seconds=10)
    monkeypatch.setattr(llm_gateway, "get_settings", lambda: s)
    monkeypatch.setattr(llm_gateway.requests, "post", lambda *a, **k: SimpleNamespace(status_code=429, headers={}))
    slept = []
    monkeypatch.setattr(llm_gateway.time, "sleep", slept.append)
    told = []
    with llm_gateway.on_rate_limit(told.append), pytest.raises(llm_gateway.LLMBusy, match="busy after 10 seconds"):
        llm_gateway.chat([])
    assert slept == [2, 4, 4] and told == slept               # backoff, capped by LLM_MAX_WAIT_SECONDS

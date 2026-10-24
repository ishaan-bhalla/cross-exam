import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
import requests
from crossexam import budget
from crossexam.config import get_settings

_rate_limit_wait_seconds = 0.0
# Token usage of each chat() call made inside track_usage(), for cost logging.
_usage: ContextVar[list | None] = ContextVar("llm_usage", default=None)
# Called with the wait in seconds before each rate-limit wait, so the caller can tell the user.
_on_wait: ContextVar[Callable[[float], None] | None] = ContextVar("llm_on_wait", default=None)


class LLMBusy(Exception):
    """The gateway stayed rate-limited for llm_max_wait_seconds."""


@contextmanager
def on_rate_limit(callback: Callable[[float], None]):
    token = _on_wait.set(callback)
    try:
        yield
    finally:
        _on_wait.reset(token)


@contextmanager
def track_usage():
    """Collects {"input_tokens", "output_tokens"} for every chat() call in this context
    (asyncio.to_thread copies the context, so calls made in a worker thread count too)."""
    calls = []
    token = _usage.set(calls)
    try:
        yield calls
    finally:
        _usage.reset(token)


def rate_limit_wait_seconds() -> float:
    """Total time this process has slept waiting on rate limits."""
    return _rate_limit_wait_seconds


def chat(messages: list[dict], model: str | None = None, max_tokens: int | None = None) -> str:
    """Retries on 429 (rate limit), waiting Retry-After if given, else exponential backoff, up to
    llm_max_retries times and llm_max_wait_seconds in all; then raises LLMBusy."""
    global _rate_limit_wait_seconds
    s = get_settings()
    waited = 0.0
    for attempt in range(s.llm_max_retries + 1):
        r = requests.post(
            s.llm_gateway_url,
            headers={"authorization": s.assemblyai_api_key},
            json={
                "model": model or s.llm_model,
                "messages": messages,
                "max_tokens": max_tokens or s.llm_max_tokens,
                "temperature": s.llm_temperature,
            },
            timeout=s.llm_timeout_seconds,
        )
        if r.status_code != 429:
            break
        remaining = s.llm_max_wait_seconds - waited
        if attempt == s.llm_max_retries or remaining <= 0:
            raise LLMBusy(f"The service is still busy after {waited:.0f} seconds of waiting. "
                          "Please ask again in a minute.")
        retry_after = r.headers.get("Retry-After", "")
        wait = min(float(retry_after) if retry_after.isdigit() else s.llm_retry_backoff_seconds * 2 ** attempt, remaining)
        if (notify := _on_wait.get()) is not None:
            notify(wait)
        time.sleep(wait)
        waited += wait
        _rate_limit_wait_seconds += wait
    r.raise_for_status()
    body = r.json()
    u = body.get("usage") or {}
    budget.record(llm_calls=1, llm_input_tokens=u.get("input_tokens", u.get("prompt_tokens")) or 0,
                  llm_output_tokens=u.get("output_tokens", u.get("completion_tokens")) or 0)
    calls = _usage.get()
    if calls is not None:
        u = body.get("usage") or {}
        calls.append({"input_tokens": u.get("input_tokens", u.get("prompt_tokens")),
                      "output_tokens": u.get("output_tokens", u.get("completion_tokens"))})
    return body["choices"][0]["message"]["content"]

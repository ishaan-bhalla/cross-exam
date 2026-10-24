"""Usage limits for a public demo: a global daily budget across all visitors, and per-visitor
quotas applied to both the visitor's cookie and their IP address.

Global usage is recorded where it is spent: every LLM call (clients.llm_gateway), every
character sent to TTS (clients.tts), and STT sessions and seconds (api.app, when a token is
issued and when a spoken question reports its audio length). The day is the UTC date. A limit of
0 means no limit. When the COST_* rates are all set, BUDGET_COST_USD_PER_DAY also applies to the
estimated cost; without rates the unit budgets (calls, characters, seconds, sessions) are the
limit (docs/deploy.md has the assumed rates they were chosen from).

Counters are kept in memory and, if budget_state_file is set, written there after each change and
read at startup, so a process restart keeps them. A new container (a redeploy, or Render's free
plan waking from sleep) starts from zero unless that file is on persistent storage.
"""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from crossexam.config import get_settings

GLOBAL_KEYS = ("llm_calls", "llm_input_tokens", "llm_output_tokens", "tts_chars", "stt_seconds", "stt_sessions")
DEMO_LIMIT = "The demo has reached its limit for today. Please try again tomorrow."

_lock = threading.Lock()
_state: dict | None = None
_minute: dict[str, list[float]] = {}  # client key -> question times in the last minute (not persisted)


class LimitReached(Exception):
    """A quota was reached; str() is the message for the visitor. demo is True for the global budget."""

    def __init__(self, message: str, demo: bool = False):
        super().__init__(message)
        self.demo = demo


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    """The state for today, reading the state file the first time."""
    global _state
    day = _today()
    if _state is None:
        _state = {"day": day, "global": {}, "clients": {}}
        path = get_settings().budget_state_file.strip()
        if path:
            try:
                saved = json.loads(get_settings().resolve(Path(path)).read_text())
                if saved.get("day") == day:
                    _state = saved
            except (OSError, ValueError):
                pass
    if _state["day"] != day:
        _state = {"day": day, "global": {}, "clients": {}}
    return _state


def _save():
    path = get_settings().budget_state_file.strip()
    if path:
        target = get_settings().resolve(Path(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(_state))
        tmp.replace(target)


def record(**usage):
    """Add to today's global usage, e.g. record(llm_calls=1, llm_input_tokens=800)."""
    with _lock:
        g = _load()["global"]
        for k, v in usage.items():
            if v:
                g[k] = g.get(k, 0) + v
        _save()


def _limits() -> dict:
    """The budgets that stop the whole demo. STT budgets only stop spoken questions (check_spoken)."""
    s = get_settings()
    return {"llm_calls": s.budget_llm_calls_per_day, "tts_chars": s.budget_tts_chars_per_day}


def _stt_limits() -> dict:
    s = get_settings()
    return {"stt_seconds": s.budget_stt_seconds_per_day, "stt_sessions": s.budget_stt_sessions_per_day}


def estimated_cost_usd(g: dict) -> float | None:
    """Today's cost from the COST_* rates, or None if any rate is unknown."""
    from crossexam import costs
    r = costs.rates()
    if None in r.values():
        return None
    return (g.get("llm_input_tokens", 0) * r["llm_input"] + g.get("llm_output_tokens", 0) * r["llm_output"]) / 1e6 \
        + g.get("tts_chars", 0) * r["tts"] / 1e6 + g.get("stt_seconds", 0) / 3600 * r["stt"]


def demo_limited() -> str | None:
    """The visitor-facing message if the demo is paused or today's global budget is spent, else None."""
    s = get_settings()
    if s.demo_paused:
        return DEMO_LIMIT
    with _lock:
        g = dict(_load()["global"])
    if any(limit and g.get(k, 0) >= limit for k, limit in _limits().items()):
        return DEMO_LIMIT
    cost = estimated_cost_usd(g)
    if s.budget_cost_usd_per_day and cost is not None and cost >= s.budget_cost_usd_per_day:
        return DEMO_LIMIT
    return None


def check_demo():
    if msg := demo_limited():
        raise LimitReached(msg, demo=True)


def status() -> dict:
    with _lock:
        g = dict(_load()["global"])
    return {"day": _state["day"], "used": {k: g.get(k, 0) for k in GLOBAL_KEYS}, "limits": {**_limits(), **_stt_limits()},
            "estimated_cost_usd": estimated_cost_usd(g), "cost_limit_usd": get_settings().budget_cost_usd_per_day or None,
            "demo_limited": demo_limited() is not None}


# ------------------------------------------------------------------ per visitor and per IP

def client_keys(visitor: str | None, ip: str | None) -> list[tuple[str, int]]:
    """(key, limit multiplier): the cookie at 1x, the IP at limit_ip_factor (people may share one)."""
    keys = []
    if visitor:
        keys.append((f"v:{visitor}", 1))
    if ip:
        keys.append((f"ip:{ip}", get_settings().limit_ip_factor))
    return keys


def _who(key: str) -> str:
    return "You have" if key.startswith("v:") else "Your network has"


def _client(key: str) -> dict:
    return _load()["clients"].setdefault(key, {})


def check_question(keys: list[tuple[str, int]]):
    """Raise LimitReached if a question now would pass a per-minute or per-day quota."""
    s = get_settings()
    now = time.time()
    with _lock:
        for key, factor in keys:
            recent = [t for t in _minute.get(key, []) if now - t < 60]
            _minute[key] = recent
            if s.limit_questions_per_minute and len(recent) >= s.limit_questions_per_minute * factor:
                raise LimitReached(f"{_who(key)} asked {len(recent)} questions in the last minute, the most this demo "
                                   f"allows. Please wait a moment, then ask again.")
            if s.limit_questions_per_day and _client(key).get("questions", 0) >= s.limit_questions_per_day * factor:
                raise LimitReached(f"{_who(key)} asked {s.limit_questions_per_day * factor} questions today, the most this "
                                   f"demo allows. Please come back tomorrow.")


def charge_question(keys: list[tuple[str, int]]):
    now = time.time()
    with _lock:
        for key, _ in keys:
            _minute.setdefault(key, []).append(now)
            c = _client(key)
            c["questions"] = c.get("questions", 0) + 1
        _save()


def check_spoken(keys: list[tuple[str, int]]):
    """Raise LimitReached if spoken questions are used up for today (seconds or sessions)."""
    s = get_settings()
    with _lock:
        g = _load()["global"]
        if any(limit and g.get(k, 0) >= limit for k, limit in _stt_limits().items()):
            raise LimitReached("The demo's microphone time for today is used up. You can still type your questions.")
        for key, factor in keys:
            c = _client(key)
            if s.limit_spoken_seconds_per_day and c.get("spoken_seconds", 0) >= s.limit_spoken_seconds_per_day * factor:
                limit = s.limit_spoken_seconds_per_day * factor
                amount = f"{limit / 60:g} minutes" if limit >= 60 else f"{limit} seconds"
                raise LimitReached(f"{_who(key)} used this demo's {amount} of spoken questions for today. "
                                   f"You can still type your questions.")
            if s.limit_stt_sessions_per_day and c.get("stt_sessions", 0) >= s.limit_stt_sessions_per_day * factor:
                raise LimitReached(f"{_who(key)} started the microphone as often as this demo allows today. "
                                   f"You can still type your questions.")


def charge_stt_session(keys: list[tuple[str, int]]):
    with _lock:
        for key, _ in keys:
            c = _client(key)
            c["stt_sessions"] = c.get("stt_sessions", 0) + 1
        _save()
    record(stt_sessions=1)


def charge_spoken(keys: list[tuple[str, int]], seconds: float):
    if not seconds or seconds < 0:
        return
    with _lock:
        for key, _ in keys:
            c = _client(key)
            c["spoken_seconds"] = c.get("spoken_seconds", 0) + seconds
        _save()
    record(stt_seconds=seconds)


def check_upload(keys: list[tuple[str, int]]):
    s = get_settings()
    with _lock:
        for key, factor in keys:
            if s.limit_uploads_per_day and _client(key).get("uploads", 0) >= s.limit_uploads_per_day * factor:
                raise LimitReached(f"{_who(key)} uploaded {s.limit_uploads_per_day * factor} statements today, the most "
                                   f"this demo allows. Please come back tomorrow, or try the example witness.")


def charge_upload(keys: list[tuple[str, int]]):
    with _lock:
        for key, _ in keys:
            c = _client(key)
            c["uploads"] = c.get("uploads", 0) + 1
        _save()


def client_status(keys: list[tuple[str, int]]) -> dict:
    s = get_settings()
    with _lock:
        used = {k: dict(_client(k)) for k, _ in keys}
    return {"used": used, "limits": {"questions_per_minute": s.limit_questions_per_minute,
                                     "questions_per_day": s.limit_questions_per_day,
                                     "spoken_seconds_per_day": s.limit_spoken_seconds_per_day,
                                     "stt_sessions_per_day": s.limit_stt_sessions_per_day,
                                     "uploads_per_day": s.limit_uploads_per_day, "ip_factor": s.limit_ip_factor}}


def reset():
    """Forget all usage (tests)."""
    global _state
    with _lock:
        _state = None
        _minute.clear()

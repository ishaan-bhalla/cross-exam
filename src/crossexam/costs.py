"""Per-answer usage and estimated cost.

Usage is measured: LLM tokens in and out (the gateway's usage field, every call the answer
made), TTS characters sent and audio seconds synthesised live (cached lead-ins and refusals
cost nothing at answer time), and STT audio seconds streamed for a spoken question. Cost is
estimated from the cost_* rate settings; a rate left empty is unknown, and that part of the
cost is reported as None ("not measured").
"""
from crossexam.config import get_settings

RATES = {"llm_input": "cost_llm_input_per_million_tokens", "llm_output": "cost_llm_output_per_million_tokens",
         "tts": "cost_tts_per_million_chars", "stt": "cost_stt_per_hour"}


def rates() -> dict[str, float | None]:
    s = get_settings()
    return {k: (float(getattr(s, name)) if str(getattr(s, name)).strip() else None) for k, name in RATES.items()}


def usage(llm_calls: list[dict] | None = None, tts_chars: int = 0, tts_audio_seconds: float | None = None,
          stt_audio_seconds: float | None = None) -> dict:
    """One answer's usage and cost. tts_audio_seconds / stt_audio_seconds are None when not
    measured (the text eval synthesises nothing; a typed question streams no audio)."""
    calls = llm_calls or []
    u = {"llm_calls": len(calls),
         "llm_input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
         "llm_output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
         "tts_chars": tts_chars, "tts_audio_seconds": tts_audio_seconds, "stt_audio_seconds": stt_audio_seconds}
    r = rates()
    parts = {
        "llm": (None if r["llm_input"] is None or r["llm_output"] is None else
                (u["llm_input_tokens"] * r["llm_input"] + u["llm_output_tokens"] * r["llm_output"]) / 1e6),
        "tts": None if r["tts"] is None else tts_chars * r["tts"] / 1e6,
        "stt": (0.0 if stt_audio_seconds is None else None if r["stt"] is None
                else stt_audio_seconds / 3600 * r["stt"]),
    }
    u["cost_usd"] = {**parts, "total": None if None in parts.values() else sum(parts.values())}
    return u


def live_tts_chars(result: dict) -> int:
    """Characters an answer sends to TTS live, as the examination does: fixed phrases (refusals,
    lead-ins without a paragraph number) are pre-synthesised; the quote is sent in chunks."""
    from crossexam.witness.answer import fixed_phrases, spoken_quote
    from crossexam.witness.chunking import chunk_quote
    fixed = set(fixed_phrases())
    if result["para_no"] is None or result.get("referral"):
        return 0 if result["spoken"] in fixed else len(result["spoken"])
    lead = result["lead_in"] if result["lead_in"] and result["lead_in"] not in fixed else ""
    return len(lead) + sum(len(c) for c in chunk_quote(spoken_quote(result["quote"])))


def describe(u: dict | None) -> str:
    """One line for the debug timing: tokens, characters, seconds and cost."""
    if not u:
        return "cost not measured"
    tts = f"TTS {u['tts_chars']} chars" + (f", {u['tts_audio_seconds']:.1f} s" if u.get("tts_audio_seconds") is not None else "")
    stt = f" · STT {u['stt_audio_seconds']:.1f} s" if u.get("stt_audio_seconds") is not None else ""
    total = u["cost_usd"]["total"]
    return (f"LLM {u['llm_calls']} call(s), {u['llm_input_tokens']} in / {u['llm_output_tokens']} out tokens · {tts}{stt}"
            f" · cost {'$%.5f' % total if total is not None else 'not measured'}")

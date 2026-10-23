"""Latency and usage of spoken answers: each question asked once, typed, over the examination
WebSocket (in-process, real LLM and TTS), as the browser does.

Per answer it records the done message: answer latency (question received to answer known),
question to first audio and to quote audio, the audio length, and usage (LLM tokens, TTS
characters and audio seconds synthesised live), and the time spent waiting on LLM gateway rate
limits, which is reported separately and also subtracted ("_excl_rate_limit"), as the text evals
do. At the end the TTS session's own Termination
totals are recorded to cross-check the per-answer sums. Writes outputs/eval/live/<timestamp>/
and, with --save-as, eval/results/run_<date>_<name>.json.

usage: run_live_eval.py --witness ismay --questions ismay_questions.jsonl [--save-as NAME]
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import eval_results  # noqa: E402
from crossexam.api import app as appmod  # noqa: E402
from crossexam.clients import tts  # noqa: E402
from crossexam.clients.llm_gateway import rate_limit_wait_seconds  # noqa: E402
from crossexam.config import get_settings  # noqa: E402

TIMING = ("answer_latency_ms", "question_to_first_audio_ms", "question_to_quote_audio_ms", "duration_ms")


def pct(values, q):
    v = sorted(x for x in values if x is not None)
    return v[min(len(v) - 1, round(q * (len(v) - 1)))] if v else None


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--witness", default="ismay")
    p.add_argument("--questions", default="ismay_questions.jsonl")
    p.add_argument("--save-as", metavar="NAME")
    a = p.parse_args()
    s = get_settings()
    qpath = Path(a.questions) if Path(a.questions).parent != Path(".") else s.resolve(s.eval_dir) / a.questions
    questions = [json.loads(l) for l in qpath.read_text().splitlines() if l.strip()]

    terminations = []
    close = tts.PersistentSession.close

    async def recording_close(self):
        t = await close(self)
        terminations.append(t)
        return t
    tts.PersistentSession.close = recording_close

    started = datetime.now()
    rows = []
    with TestClient(appmod.app) as client:
      with client.websocket_connect(f"/ws/examine?witness={a.witness}") as ws:
        for q in questions:
            waited_before = rate_limit_wait_seconds()
            ws.send_json({"type": "question", "text": q["question"]})
            answer, done = None, None
            while True:
                m = ws.receive_json()
                if m["type"] == "answer":
                    answer = m
                elif m["type"] == "done":
                    done = m
                elif m["type"] == "turn":
                    break
                elif m["type"] == "error":
                    raise RuntimeError(m["message"])
            waited_ms = round((rate_limit_wait_seconds() - waited_before) * 1000)
            rows.append({"id": q["id"], "category": q["category"], "refused": answer["refused"],
                         "referral": answer["referral"], "ocr_corrected": answer["ocr_corrected"],
                         "cached": done["cached"], **{k: done.get(k) for k in TIMING}, "rate_limit_wait_ms": waited_ms,
                         **{f"{k}_excl_rate_limit": (done[k] - waited_ms if done.get(k) is not None and k != "duration_ms" else done.get(k))
                            for k in TIMING}, "usage": done["usage"]})
            print(f"{q['id']:>4}  first audio {done['question_to_first_audio_ms']:>5} ms  audio {done['duration_ms'] / 1000:5.1f} s  "
                  f"tts {done['usage']['tts_chars']:>4} chars  rate-limit wait {waited_ms} ms", flush=True)
      for _ in range(50):  # the server closes the TTS session after the socket closes
          if terminations:
              break
          time.sleep(0.2)

    def stats(key, sel=rows):
        v = [r[key] for r in sel if r[key] is not None]
        return {"n": len(v), "mean": statistics.mean(v) if v else None, "p50": pct(v, .5), "p95": pct(v, .95)}
    answered = [r for r in rows if not r["refused"]]
    usage = lambda k: [r["usage"][k] for r in rows]
    summary = {
        "questions": len(rows), "answered": len(answered), "refused": len(rows) - len(answered),
        "latency_ms": {k: stats(k) for k in TIMING},
        "latency_ms_answered": {k: stats(k, answered) for k in TIMING},
        "latency_ms_excl_rate_limit": {k: stats(f"{k}_excl_rate_limit") for k in TIMING},
        "latency_ms_answered_excl_rate_limit": {k: stats(f"{k}_excl_rate_limit", answered) for k in TIMING},
        "rate_limit_wait_ms_total": sum(r["rate_limit_wait_ms"] for r in rows),
        "questions_with_rate_limit_wait": sum(r["rate_limit_wait_ms"] > 0 for r in rows),
        "usage_totals": {k: sum(usage(k)) for k in ("llm_calls", "llm_input_tokens", "llm_output_tokens", "tts_chars")}
                        | {"tts_audio_seconds": round(sum(usage("tts_audio_seconds")), 2)},
        "usage_mean_per_answer": {k: statistics.mean(usage(k)) for k in
                                  ("llm_calls", "llm_input_tokens", "llm_output_tokens", "tts_chars", "tts_audio_seconds")},
        "cost_usd_mean_per_answer": (None if None in [r["usage"]["cost_usd"]["total"] for r in rows]
                                     else statistics.mean(r["usage"]["cost_usd"]["total"] for r in rows)),
        "tts_termination": terminations[-1] if terminations else None,
    }
    out = s.resolve(s.outputs_dir) / "eval" / "live" / started.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ("latency_ms", "usage_totals", "tts_termination")}, indent=1))
    print(f"wrote {out}")
    if a.save_as:
        record = {"name": a.save_as, "date": started.isoformat(), "source": str(out.relative_to(s.resolve(s.outputs_dir).parent)),
                  "layout": "live", "git": eval_results.git_info(), "model": s.llm_model, "temperature": s.llm_temperature,
                  "settings": eval_results.settings_snapshot(), "environment": eval_results.environment(),
                  "notes": f"{a.witness}, {qpath.name}, each question asked once, typed, over the WebSocket with live TTS.",
                  "summary": summary, "per_question": rows}
        path = eval_results.RESULTS_DIR / f"run_{started:%Y%m%d}_{a.save_as}.json"
        eval_results.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1, default=str) + "\n")
        print(f"saved {path}")


if __name__ == "__main__":
    main()

"""Spot-check answering on uploaded statements, through the upload path a user would take.

Each statement in the question file is uploaded with POST /api/witnesses/upload and polled
until ready; its questions then go to the session witness's retriever and the normal answer
pipeline (what the examination WebSocket calls), using run_eval's question runner and metrics.
Writes results.jsonl and summary.json to <outputs_dir>/eval/upload_spotcheck/<timestamp>/.

usage: run_upload_spotcheck.py [--questions upload_spotcheck.jsonl] [--repeats 3]
"""
import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
import run_eval  # noqa: E402
from crossexam.api import witnesses  # noqa: E402
from crossexam.api.app import app  # noqa: E402
from crossexam.config import get_settings  # noqa: E402

OCR_JUNK_RE = re.compile(r"[~°`^¢€¬•►✓|{}\\]|[a-z][A-Z]|[A-Za-z]\d|\d[a-z]")
DOC_ID_TOKEN_RE = re.compile(r"^\W*[A-Z]{2,5}[0-9O]{5,}[_\-0-9A-Z]*\W*$")


def ocr_marks(text: str) -> list[str]:
    """Tokens in text that look like OCR damage (document IDs with digits excepted)."""
    return [t for t in (text or "").split() if OCR_JUNK_RE.search(t) and not DOC_ID_TOKEN_RE.match(t)]


def upload(client: TestClient, path: Path) -> dict:
    r = client.post("/api/witnesses/upload", files={"file": (path.name, path.read_bytes(), "application/pdf")})
    r.raise_for_status()
    url = r.json()["status_url"]
    while (status := client.get(url).json())["stage"] not in ("ready", "error"):
        time.sleep(0.2)
    if status["stage"] != "ready":
        raise RuntimeError(f"{path.name}: {status['error']}")
    return status


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--questions", default="upload_spotcheck.jsonl")
    p.add_argument("--repeats", type=int, default=3)
    a = p.parse_args()
    s = get_settings()
    qpath = Path(a.questions)
    qpath = qpath if qpath.parent != Path(".") else s.resolve(s.eval_dir) / qpath
    questions = [json.loads(line) for line in qpath.read_text().splitlines() if line.strip()]
    by_statement = defaultdict(list)
    for q in questions:
        by_statement[q["statement"]].append(q)

    client = TestClient(app)
    sessions = {}
    for f in by_statement:
        status = upload(client, s.resolve(s.statements_dir) / f)
        sessions[f] = status
        print(f"uploaded {f}: {status['witness_id']} ready in {status['elapsed_ms'] / 1000:.1f}s, {status['units']} units, "
              f"OCR {len(status['ocr_pages'])} pages")

    rows = []
    for run in range(1, a.repeats + 1):
        for f, qs in by_statement.items():
            wid = sessions[f]["witness_id"]
            witness = witnesses.get_witness(wid)
            retriever = witnesses.retriever(wid)
            for q in qs:
                r = run_eval.run_question(q, retriever, witness["name"], use_llm=True, relevance_check=None)
                expected = {str(x) for x in q["expected_paras"]}
                r["para_correct"] = (str(r["para_no"]) in expected) if expected else None
                r["hit_at_1"] = (str(r["retrieved"][0]) in expected) if expected else None
                r["hit_at_k"] = any(str(x) in expected for x in r["retrieved"]) if expected else None
                r.update(run=run, statement=f, witness_id=wid, why=q["why"],
                         quote_ocr_marks=ocr_marks(r.get("quote")),
                         gate_refused_answerable=bool(expected) and r["skipped_llm"] and not r.get("doc_id_unknown"))
                rows.append(r)
        print(f"run {run} done")

    summary = {"questions": str(qpath), "repeats": a.repeats, "dense_min_score": s.dense_min_score,
               "uploads": {f: {k: st[k] for k in ("witness_id", "elapsed_ms", "units", "ocr_pages")} for f, st in sessions.items()},
               "by_statement": {f: run_eval.summarise([r for r in rows if r["statement"] == f], True) for f in by_statement},
               "overall": run_eval.summarise(rows, True)}
    for key, group in list(summary["by_statement"].items()) + [("overall", summary["overall"])]:
        sel = rows if key == "overall" else [r for r in rows if r["statement"] == key]
        group["quote_shortening_calls"] = sum(bool(r.get("shortening")) for r in sel)
        group["quotes_with_ocr_marks"] = sum(bool(r["quote_ocr_marks"]) for r in sel)
        group["gate_refused_answerable"] = sum(r["gate_refused_answerable"] for r in sel)

    out = s.resolve(s.outputs_dir) / "eval" / "upload_spotcheck" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    cols = [("n", "n", "d"), ("hit@1", "hit_at_1", "%"), ("hit@k", "hit_at_k", "%"), ("unit acc", "para_accuracy", "%"),
            ("grounded", "grounding_rate", "%"), ("ref prec", "refusal_precision", "%"), ("ref rec", "refusal_recall", "%"),
            ("referrals", "referrals", "d"), ("ocr corr", "ocr_corrected", "d"), ("shorten", "quote_shortening_calls", "d"), ("gate ref", "gate_refused_answerable", "d"),
            ("ocr q", "quotes_with_ocr_marks", "d"), ("mean ms", "latency_mean_ms", "f"), ("p95 ms", "latency_p95_ms", "f")]
    print(f"\n{'':<18}" + "".join(f"{h:>10}" for h, _, _ in cols))
    for key, g in list(summary["by_statement"].items()) + [("overall", summary["overall"])]:
        print(f"{key[:18]:<18}" + "".join(f"{run_eval.fmt(g.get(k), kind):>10}" for _, k, kind in cols))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

"""Evaluate retrieval and witness answers against a question set.

A bare --questions filename is looked up in eval_dir. Results go to
<outputs_dir>/eval/<timestamp>/ as results.jsonl and summary.json.
"""
import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from crossexam.config import get_settings
from crossexam.statements.parser import load_statement
from crossexam.retrieval.bm25 import BM25Index
from crossexam.witness.answer import answer

CATEGORY_ORDER = ["direct", "paraphrased", "multi", "trap"]
WORST_MISSES = 5


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def run_question(q: dict, index: BM25Index, witness: str, top_k: int, min_score: float, use_llm: bool) -> dict:
    expected = set(q["expected_paras"])
    answerable = bool(expected)
    start = time.perf_counter()
    ranking = index.search(q["question"], top_k=len(index.paragraphs))
    hits = ranking[:top_k]
    rank_of = {h["para_no"]: i + 1 for i, h in enumerate(ranking)}

    r = {"id": q["id"], "category": q["category"], "difficulty": q["difficulty"],
         "question": q["question"], "expected_paras": q["expected_paras"],
         "retrieved": [h["para_no"] for h in hits], "scores": [round(h["score"], 3) for h in hits],
         "best_expected_rank": min((rank_of[n] for n in expected if n in rank_of), default=None),
         "hit_at_1": hits[0]["para_no"] in expected if answerable else None,
         "hit_at_k": any(h["para_no"] in expected for h in hits) if answerable else None}

    if use_llm:
        a = answer(q["question"], hits, witness, min_score=min_score)
        refused = a["para_no"] is None
        r.update({"para_no": a["para_no"],
                  "para_correct": (a["para_no"] in expected) if answerable else None,
                  "verified": a["verified"], "fallback": a["fallback"],
                  "verified_no_fallback": a["verified"] and not a["fallback"],
                  "para_corrected": a["para_corrected"], "skipped_llm": a["skipped_llm"],
                  "refused": refused, "spoken": a["spoken"], "raw": a["raw"]})
    else:
        # Retrieval only: the only refusal possible is the score threshold.
        refused = hits[0]["score"] < min_score
        r.update({"para_no": None, "para_correct": None, "verified": None, "fallback": None,
                  "verified_no_fallback": None, "para_corrected": None,
                  "skipped_llm": refused, "refused": refused})
    r["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return r


def summarise(rows: list[dict], use_llm: bool) -> dict:
    answerable = [r for r in rows if r["expected_paras"]]
    traps = [r for r in rows if not r["expected_paras"]]
    refused = [r for r in rows if r["refused"]]
    non_refused = [r for r in rows if not r["refused"]]
    latencies = [r["latency_ms"] for r in rows]
    s = {"n": len(rows),
         "hit_at_1": _ratio(sum(r["hit_at_1"] for r in answerable), len(answerable)),
         "hit_at_k": _ratio(sum(r["hit_at_k"] for r in answerable), len(answerable)),
         "refused": len(refused),
         "refusal_precision": _ratio(sum(not r["expected_paras"] for r in refused), len(refused)),
         "refusal_recall": _ratio(sum(r["refused"] for r in traps), len(traps)),
         "latency_mean_ms": statistics.mean(latencies) if latencies else None,
         "latency_p95_ms": _p95(latencies)}
    if use_llm:
        s["para_accuracy"] = _ratio(sum(r["para_correct"] for r in answerable), len(answerable))
        s["grounding_rate"] = _ratio(sum(r["verified_no_fallback"] for r in non_refused), len(non_refused))
        s["para_corrected"] = sum(r["para_corrected"] for r in rows)
        s["skipped_llm"] = sum(r["skipped_llm"] for r in rows)
    return s


def print_table(summary: dict, use_llm: bool):
    cols = [("n", "n", "d"), ("hit@1", "hit_at_1", "%"), ("hit@k", "hit_at_k", "%")]
    if use_llm:
        cols += [("para acc", "para_accuracy", "%"), ("grounded", "grounding_rate", "%"),
                 ("corrected", "para_corrected", "d"), ("skip llm", "skipped_llm", "d")]
    cols += [("refused", "refused", "d"), ("ref prec", "refusal_precision", "%"),
             ("ref rec", "refusal_recall", "%"), ("mean ms", "latency_mean_ms", "f"),
             ("p95 ms", "latency_p95_ms", "f")]

    def fmt(v, kind):
        if v is None:
            return "–"
        return {"%": f"{v:.0%}", "d": f"{v}", "f": f"{v:.0f}"}[kind]

    groups = [(c, summary["by_category"][c]) for c in CATEGORY_ORDER if c in summary["by_category"]]
    groups += [(c, v) for c, v in summary["by_category"].items() if c not in CATEGORY_ORDER]
    groups.append(("overall", summary["overall"]))
    print(f"{'':<12}" + "".join(f"{h:>10}" for h, _, _ in cols))
    for name, g in groups:
        print(f"{name:<12}" + "".join(f"{fmt(g.get(k), kind):>10}" for _, k, kind in cols))


def main():
    p = argparse.ArgumentParser(description="Evaluate retrieval and witness answers on a question set.")
    p.add_argument("--questions", required=True, type=Path)
    p.add_argument("--pdf", required=True, type=Path)
    p.add_argument("--witness", required=True)
    p.add_argument("--top-k", type=int)
    p.add_argument("--min-score", type=float)
    p.add_argument("--no-llm", action="store_true", help="retrieval only, no LLM calls")
    a = p.parse_args()
    s = get_settings()
    top_k = a.top_k or s.retrieval_top_k
    min_score = s.retrieval_min_score if a.min_score is None else a.min_score
    use_llm = not a.no_llm

    qpath = a.questions if a.questions.parent != Path(".") else s.resolve(s.eval_dir) / a.questions
    questions = [json.loads(line) for line in qpath.read_text().splitlines() if line.strip()]
    index = BM25Index(load_statement(a.pdf))

    rows = []
    for q in questions:
        rows.append(run_question(q, index, a.witness, top_k, min_score, use_llm))
        print(f"  {q['id']}: done", end="\r", flush=True)

    categories = sorted({r["category"] for r in rows})
    summary = {"questions": str(qpath), "pdf": str(a.pdf), "witness": a.witness,
               "top_k": top_k, "min_score": min_score, "llm": use_llm,
               "by_category": {c: summarise([r for r in rows if r["category"] == c], use_llm) for c in categories},
               "overall": summarise(rows, use_llm)}

    out_dir = s.resolve(s.outputs_dir) / "eval" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print(f"\ntop_k={top_k} min_score={min_score} llm={use_llm}\n")
    print_table(summary, use_llm)

    misses = sorted((r for r in rows if r["expected_paras"] and not r["hit_at_1"]),
                    key=lambda r: (r["best_expected_rank"] or 10**6), reverse=True)[:WORST_MISSES]
    print(f"\nWorst retrieval misses (expected para's best rank in full ranking):")
    for r in misses:
        print(f"  {r['id']} rank {r['best_expected_rank']}: {r['question']}\n"
              f"      expected {r['expected_paras']}  got {r['retrieved']}")
    print(f"\nwrote {out_dir}")


if __name__ == "__main__":
    main()

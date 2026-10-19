"""Evaluate retrieval and witness answers against a question set.

A bare --questions filename is looked up in eval_dir. Results go to
<outputs_dir>/eval/<timestamp>/ as results.jsonl and summary.json (one
subfolder per variant with --ablation). Time spent waiting on LLM rate limits
is recorded per question and excluded from latency.

--no-llm skips answer generation. HyDE also calls the LLM, so with --no-llm it
only runs if --hyde is passed explicitly, and --ablation leaves it out.
"""
import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from crossexam.clients.llm_gateway import rate_limit_wait_seconds
from crossexam.config import get_settings
from crossexam.statements.parser import load_statement
from crossexam.retrieval.retriever import Retriever
from crossexam.witness.answer import answer

CATEGORY_ORDER = ["direct", "paraphrased", "multi", "trap"]
ABLATION = [("bm25", False), ("dense", False), ("hybrid", False), ("hybrid", True)]
WORST_MISSES = 5
DENSE_SCORE_LISTING = 6


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def variant_name(kind: str, hyde: bool) -> str:
    return f"{kind}+hyde" if hyde else kind


def run_question(q: dict, retriever: Retriever, witness: str, use_llm: bool) -> dict:
    expected = set(q["expected_paras"])
    answerable = bool(expected)
    waited_before = rate_limit_wait_seconds()
    start = time.perf_counter()
    retrieval = retriever.retrieve(q["question"])
    hits = retrieval.hits
    rank_of = {h["para_no"]: i + 1 for i, h in enumerate(retrieval.ranking)}

    r = {"id": q["id"], "category": q["category"], "difficulty": q["difficulty"],
         "question": q["question"], "expected_paras": q["expected_paras"],
         "retriever": retriever.kind, "hyde": retriever.hyde,
         "query": retrieval.query if retriever.hyde else None, "hyde_error": retrieval.hyde_error,
         "retrieved": [h["para_no"] for h in hits], "scores": [round(h["score"], 4) for h in hits],
         "hit_details": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in h.items() if k != "text"}
                         for h in hits],
         "gate_score": retrieval.gate_score, "top_dense_score": retrieval.top_dense_score,
         "best_expected_rank": min((rank_of[n] for n in expected if n in rank_of), default=None),
         "hit_at_1": hits[0]["para_no"] in expected if answerable else None,
         "hit_at_k": any(h["para_no"] in expected for h in hits) if answerable else None}

    if use_llm:
        a = answer(q["question"], retriever, witness, retrieval=retrieval)
        r.update({"para_no": a["para_no"],
                  "para_correct": (a["para_no"] in expected) if answerable else None,
                  "verified": a["verified"], "fallback": a["fallback"],
                  "verified_no_fallback": a["verified"] and not a["fallback"],
                  "para_corrected": a["para_corrected"], "skipped_llm": a["skipped_llm"],
                  "refused": a["para_no"] is None, "spoken": a["spoken"], "raw": a["raw"]})
    else:
        # No answer step: the only refusal possible is the retrieval gate.
        refused = not retriever.passes_gate(retrieval)
        r.update({"para_no": None, "para_correct": None, "verified": None, "fallback": None,
                  "verified_no_fallback": None, "para_corrected": None,
                  "skipped_llm": refused, "refused": refused})
    waited = rate_limit_wait_seconds() - waited_before
    r["rate_limit_wait_ms"] = round(waited * 1000, 1)
    r["latency_ms"] = round((time.perf_counter() - start - waited) * 1000, 1)
    return r


def summarise(rows: list[dict], use_llm: bool) -> dict:
    answerable = [r for r in rows if r["expected_paras"]]
    traps = [r for r in rows if not r["expected_paras"]]
    refused = [r for r in rows if r["refused"]]
    non_refused = [r for r in rows if not r["refused"]]
    latencies = [r["latency_ms"] for r in rows]
    dense = lambda rs: [r["top_dense_score"] for r in rs if r["top_dense_score"] is not None]
    s = {"n": len(rows),
         "hit_at_1": _ratio(sum(r["hit_at_1"] for r in answerable), len(answerable)),
         "hit_at_k": _ratio(sum(r["hit_at_k"] for r in answerable), len(answerable)),
         "refused": len(refused),
         "refusal_precision": _ratio(sum(not r["expected_paras"] for r in refused), len(refused)),
         "refusal_recall": _ratio(sum(r["refused"] for r in traps), len(traps)),
         "mean_top_dense_answerable": _mean(dense(answerable)),
         "mean_top_dense_trap": _mean(dense(traps)),
         "latency_mean_ms": _mean(latencies),
         "latency_p95_ms": _p95(latencies),
         "rate_limit_wait_ms": sum(r["rate_limit_wait_ms"] for r in rows)}
    if use_llm:
        s["para_accuracy"] = _ratio(sum(r["para_correct"] for r in answerable), len(answerable))
        s["grounding_rate"] = _ratio(sum(r["verified_no_fallback"] for r in non_refused), len(non_refused))
        s["para_corrected"] = sum(r["para_corrected"] for r in rows)
        s["skipped_llm"] = sum(r["skipped_llm"] for r in rows)
    return s


def build_summary(rows: list[dict], use_llm: bool, **meta) -> dict:
    categories = sorted({r["category"] for r in rows})
    return {**meta, "llm": use_llm,
            "by_category": {c: summarise([r for r in rows if r["category"] == c], use_llm) for c in categories},
            "overall": summarise(rows, use_llm)}


def fmt(v, kind: str) -> str:
    if v is None:
        return "–"
    return {"%": f"{v:.0%}", "d": f"{v}", "f": f"{v:.0f}", "3": f"{v:.3f}"}[kind]


def print_table(summary: dict, use_llm: bool):
    cols = [("n", "n", "d"), ("hit@1", "hit_at_1", "%"), ("hit@k", "hit_at_k", "%")]
    if use_llm:
        cols += [("para acc", "para_accuracy", "%"), ("grounded", "grounding_rate", "%"),
                 ("corrected", "para_corrected", "d"), ("skip llm", "skipped_llm", "d")]
    cols += [("refused", "refused", "d"), ("ref prec", "refusal_precision", "%"),
             ("ref rec", "refusal_recall", "%"), ("mean ms", "latency_mean_ms", "f"),
             ("p95 ms", "latency_p95_ms", "f"), ("rl wait ms", "rate_limit_wait_ms", "f")]
    groups = [(c, summary["by_category"][c]) for c in CATEGORY_ORDER if c in summary["by_category"]]
    groups += [(c, v) for c, v in summary["by_category"].items() if c not in CATEGORY_ORDER]
    groups.append(("overall", summary["overall"]))
    print(f"{'':<12}" + "".join(f"{h:>11}" for h, _, _ in cols))
    for name, g in groups:
        print(f"{name:<12}" + "".join(f"{fmt(g.get(k), kind):>11}" for _, k, kind in cols))


def print_ablation(summaries: dict[str, dict]):
    cats = ["direct", "paraphrased", "multi"]
    head = "".join(f"{c[:5] + ' @1':>10}{c[:5] + ' @k':>10}" for c in cats) + f"{'all @1':>10}{'all @k':>10}"
    print(f"{'variant':<14}{head}{'dense ans':>11}{'dense trap':>11}")
    for name, sm in summaries.items():
        by = sm["by_category"]
        cells = "".join(f"{fmt(by.get(c, {}).get('hit_at_1'), '%'):>10}{fmt(by.get(c, {}).get('hit_at_k'), '%'):>10}"
                        for c in cats)
        o = sm["overall"]
        print(f"{name:<14}{cells}{fmt(o['hit_at_1'], '%'):>10}{fmt(o['hit_at_k'], '%'):>10}"
              f"{fmt(o['mean_top_dense_answerable'], '3'):>11}{fmt(o['mean_top_dense_trap'], '3'):>11}")


def print_misses(rows: list[dict]):
    misses = sorted((r for r in rows if r["expected_paras"] and not r["hit_at_1"]),
                    key=lambda r: (r["best_expected_rank"] or 10**6), reverse=True)[:WORST_MISSES]
    print("\nWorst retrieval misses (expected para's best rank in full ranking):")
    for r in misses:
        print(f"  {r['id']} rank {r['best_expected_rank']}: {r['question']}\n"
              f"      expected {r['expected_paras']}  got {r['retrieved']}")


def print_dense_scores(rows: list[dict]):
    """Top dense score of every trap vs the lowest-scoring answerable questions, to pick dense_min_score."""
    scored = [r for r in rows if r["top_dense_score"] is not None]
    if not scored:
        return
    traps = sorted((r for r in scored if not r["expected_paras"]), key=lambda r: -r["top_dense_score"])
    low = sorted((r for r in scored if r["expected_paras"]), key=lambda r: r["top_dense_score"])[:DENSE_SCORE_LISTING]
    print("\nTop dense score: traps vs lowest-scoring answerable")
    for label, group in (("trap", traps), ("answerable", low)):
        for r in group:
            print(f"  {label:<11}{r['id']}  {r['top_dense_score']:.3f}  {r['question']}")


def write(out_dir: Path, rows: list[dict], summary: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))


def run_variant(questions, paragraphs, a, kind, hyde, use_llm) -> tuple[list[dict], dict]:
    retriever = Retriever(paragraphs, a.pdf.stem, kind=kind, hyde=hyde, top_k=a.top_k, min_score=a.min_score)
    rows = []
    for q in questions:
        rows.append(run_question(q, retriever, a.witness, use_llm))
        print(f"  {variant_name(kind, hyde)} {q['id']}", end="\r", flush=True)
    print(" " * 40, end="\r")
    summary = build_summary(rows, use_llm, questions=str(a.questions), pdf=str(a.pdf), witness=a.witness,
                            retriever=kind, hyde=hyde, top_k=retriever.top_k, min_score=retriever.min_score)
    return rows, summary


def main():
    p = argparse.ArgumentParser(description="Evaluate retrieval and witness answers on a question set.")
    p.add_argument("--questions", required=True, type=Path)
    p.add_argument("--pdf", required=True, type=Path)
    p.add_argument("--witness", required=True)
    p.add_argument("--top-k", type=int)
    p.add_argument("--min-score", type=float, help="override the refusal gate threshold")
    p.add_argument("--retriever", help="bm25 | dense | hybrid (default: RETRIEVER setting)")
    p.add_argument("--hyde", action=argparse.BooleanOptionalAction, help="override HYDE_ENABLED")
    p.add_argument("--ablation", action="store_true", help="run bm25, dense, hybrid and hybrid+hyde")
    p.add_argument("--no-llm", action="store_true", help="retrieval only, no answer generation")
    a = p.parse_args()
    s = get_settings()
    use_llm = not a.no_llm

    qpath = a.questions if a.questions.parent != Path(".") else s.resolve(s.eval_dir) / a.questions
    a.questions = qpath
    questions = [json.loads(line) for line in qpath.read_text().splitlines() if line.strip()]
    paragraphs = load_statement(a.pdf)
    out_dir = s.resolve(s.outputs_dir) / "eval" / datetime.now().strftime("%Y%m%d-%H%M%S")

    if a.ablation:
        variants = [(k, h) for k, h in ABLATION if use_llm or not h]
        summaries, rows_by_name = {}, {}
        for kind, hyde in variants:
            rows, summary = run_variant(questions, paragraphs, a, kind, hyde, use_llm)
            name = variant_name(kind, hyde)
            write(out_dir / name, rows, summary)
            summaries[name] = summary
            rows_by_name[name] = rows
        (out_dir / "ablation.json").write_text(json.dumps(summaries, indent=2))
        print(f"top_k={summaries[name]['top_k']} llm={use_llm}\n")
        print_ablation(summaries)
        print_dense_scores(rows_by_name["dense"])  # question embeddings, the scale the gate uses without HyDE
    else:
        # --no-llm keeps HyDE off unless --hyde is passed explicitly.
        hyde = a.hyde if a.hyde is not None else (False if a.no_llm else s.hyde_enabled)
        rows, summary = run_variant(questions, paragraphs, a, a.retriever or s.retriever, hyde, use_llm)
        write(out_dir, rows, summary)
        print(f"retriever={summary['retriever']} hyde={hyde} top_k={summary['top_k']} "
              f"min_score={summary['min_score']} llm={use_llm}\n")
        print_table(summary, use_llm)
        print_misses(rows)
        print_dense_scores(rows)
    print(f"\nwrote {out_dir}")


if __name__ == "__main__":
    main()

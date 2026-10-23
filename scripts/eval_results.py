"""Summaries of eval runs that are kept in the repo: eval/results/run_<date>_<name>.json.

outputs/ (gitignored) holds every run's full rows; the README cites these summaries instead.
Each one is rebuilt from the run's per-question rows with run_eval's metrics, and records the
date, the git commit (and whether the working tree had uncommitted changes), the model, the
question file, per-run and aggregated metrics with exact counts, and each question's outcome
in every run. Runs saved when they are made (run_eval.py / run_upload_spotcheck.py --save-as)
also record a settings snapshot (no secrets) and the hardware; older runs saved later from
outputs/ say so in "notes" and have settings null.

usage: eval_results.py <outputs run dir> <name> [--note TEXT]   (save an existing run)
"""
import argparse
import json
import platform
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_eval  # noqa: E402
from crossexam.config import PROJECT_ROOT, get_settings  # noqa: E402

RESULTS_DIR = PROJECT_ROOT / "eval" / "results"
OUTCOME_FIELDS = ("para_no", "para_correct", "hit_at_1", "hit_at_k", "best_expected_rank", "refused", "grounded",
                  "verified", "ocr_corrected", "referral", "latency_ms")


def git_info() -> dict:
    def run(*args):
        return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.strip()
    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def git_at(when: datetime) -> dict:
    """For a run saved after the fact: the last commit before it and the first after it (the run
    used the working tree at the time, which may have had changes committed later)."""
    def run(*args):
        return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.strip()
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    after = run("log", "--reverse", f"--after={stamp}", "--format=%H").splitlines()
    return {"commit_before_run": run("log", "-1", f"--before={stamp}", "--format=%H"),
            "first_commit_after_run": after[0] if after else None, "dirty": None}


def environment() -> dict:
    cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    mem = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
    return {"platform": platform.platform(), "cpu": cpu or platform.processor(),
            "memory_gb": round(int(mem) / 2 ** 30) if mem.isdigit() else None, "python": platform.python_version()}


def settings_snapshot() -> dict:
    """Every setting except secrets, as strings."""
    s = get_settings()
    return {k: str(v) for k, v in s.model_dump().items() if "key" not in k}


def _normalise(row: dict) -> dict:
    """Rows from before referrals and OCR-tolerant verification existed."""
    row = dict(row)
    if "grounded" not in row:
        row["grounded"] = row.get("verified_no_fallback")
        row["fallback_first_sentence"] = row.get("fallback")
    row.setdefault("referral", False)
    row.setdefault("ocr_corrected", False)
    return row


def _aggregate(values: list) -> dict | None:
    values = [v for v in values if v is not None]
    return {"mean": statistics.mean(values), "min": min(values), "max": max(values), "values": values} if values else None


def _summaries(rows: list[dict], group_key: str | None, use_llm: bool) -> dict:
    out = {"overall": run_eval.summarise(rows, use_llm)}
    if group_key:
        groups = defaultdict(list)
        for r in rows:
            groups[r[group_key]].append(r)
        out[f"by_{group_key}"] = {g: run_eval.summarise(rs, use_llm) for g, rs in groups.items()}
    return out


def _aggregate_runs(per_run: list[dict]) -> dict:
    """mean/min/max over runs of every scalar metric, overall and per group."""
    def agg(summaries):
        keys = [k for k, v in summaries[0].items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        out = {k: _aggregate([s.get(k) for s in summaries]) for k in keys}
        out["counts"] = {k: _aggregate([s["counts"][k] for s in summaries if "counts" in s])
                         for k in summaries[0].get("counts", {})}
        return out
    result = {"overall": agg([r["overall"] for r in per_run])}
    for key in per_run[0]:
        if key.startswith("by_"):
            result[key] = {g: agg([r[key][g] for r in per_run]) for g in per_run[0][key]}
    return result


def _per_question(runs: list[list[dict]]) -> list[dict]:
    by_id = {}
    for i, rows in enumerate(runs, 1):
        for r in rows:
            q = by_id.setdefault(r["id"], {k: r.get(k) for k in ("id", "category", "question", "expected_paras", "statement")
                                         if r.get(k) is not None} | {"runs": []})
            q["runs"].append({"run": i, **{k: r.get(k) for k in OUTCOME_FIELDS if k in r}})
    return list(by_id.values())


def _load_runs(src: Path) -> tuple[str, list[list[dict]] | dict[str, list[dict]]]:
    """("repeats" | "single" | "ablation" | "spotcheck", rows per run or per variant)."""
    read = lambda p: [_normalise(json.loads(l)) for l in p.read_text().splitlines() if l.strip()]
    if (src / "ablation.json").exists():
        return "ablation", {d.name: read(d / "results.jsonl") for d in sorted(src.iterdir()) if (d / "results.jsonl").exists()}
    if (src / "repeats.json").exists():
        return "repeats", [read(d / "results.jsonl") for d in sorted(src.glob("run_*"), key=lambda d: int(d.name[4:]))]
    rows = read(src / "results.jsonl")
    if rows and "run" in rows[0] and "statement" in rows[0]:
        by_run = defaultdict(list)
        for r in rows:
            by_run[r["run"]].append(r)
        return "spotcheck", [by_run[k] for k in sorted(by_run)]
    return "single", [rows]


def save(src: Path, name: str, notes: str | None = None, fresh: bool = False, overrides: dict | None = None) -> Path:
    """Write eval/results/run_<date>_<name>.json from the run in src. fresh: the run was made just
    now, so the current settings, git state and hardware are the run's own."""
    s = get_settings()
    layout, runs = _load_runs(src)
    date = datetime.strptime(src.name[:15], "%Y%m%d-%H%M%S")
    first = next(iter(runs.values()))[0] if isinstance(runs, dict) else runs[0][0]
    use_llm = first.get("verified") is not None  # retrieval-only rows have verified None
    record = {"name": name, "date": date.isoformat(), "source": str(src.relative_to(s.resolve(s.outputs_dir).parent)),
              "layout": layout, "git": git_info() if fresh else git_at(date), "model": s.llm_model if fresh else None,
              "temperature": s.llm_temperature if fresh else None,
              "settings": ({**settings_snapshot(), **(overrides or {})} if fresh else None),
              "environment": environment() if fresh else None,
              "notes": notes, "llm": use_llm}
    if not fresh:
        record["notes"] = ((notes + " ") if notes else "") + (
            "Saved after the run from its outputs; settings were not snapshotted at run time (see the source "
            "run's summary.json for the retriever, top_k and gate it used). Metrics are recomputed from the rows.")
    if layout == "ablation":
        record["variants"] = {v: _summaries(rows, "category", use_llm) for v, rows in runs.items()}
    else:
        group = "statement" if layout == "spotcheck" else "category"
        per_run = []
        for rows in runs:
            summ = _summaries(rows, group, use_llm)
            if layout == "spotcheck":
                summ.update({k: v for k, v in _summaries(rows, "category", use_llm).items() if k != "overall"})
            per_run.append(summ)
        record.update(repeats=len(runs), runs=per_run, aggregate=_aggregate_runs(per_run), per_question=_per_question(runs))
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"run_{date:%Y%m%d}_{name}.json"
    path.write_text(json.dumps(record, indent=1, default=str) + "\n")
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("source", type=Path)
    p.add_argument("name")
    p.add_argument("--note")
    p.add_argument("--fresh", action="store_true", help="the run was just made with the current settings")
    p.add_argument("--set", action="append", default=[], metavar="SETTING=VALUE",
                   help="with --fresh, a setting the run overrode (as an environment variable)")
    a = p.parse_args()
    overrides = dict(kv.split("=", 1) for kv in a.set)
    print(save(a.source.resolve(), a.name, a.note, fresh=a.fresh, overrides={k.lower(): v for k, v in overrides.items()}))


if __name__ == "__main__":
    main()

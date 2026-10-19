"""The configured retriever (bm25 | dense | hybrid), optional HyDE rewrite, and refusal gate.

Document-ID routing: if the question cites IDs matching doc_id_pattern, the
paragraphs mentioning any of them (case-insensitive) come first, ordered by
dense score for the question (or the configured ranking if there is no dense
index), and the configured ranking fills the rest; HyDE is skipped and the gate
passes. If none of the cited IDs is in the statement, doc_id_unknown is set.

The gate compares a retriever-specific score with a threshold: the top BM25
score against retrieval_min_score for bm25, and the top dense cosine
similarity against dense_min_score for dense and hybrid.
"""
import re
from dataclasses import dataclass
from crossexam.clients.llm_gateway import chat
from crossexam.config import get_settings
from crossexam.retrieval.bm25 import BM25Index
from crossexam.retrieval.dense import DenseIndex
from crossexam.retrieval.hybrid import fuse

KINDS = ("bm25", "dense", "hybrid")
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class Retrieval:
    query: str                    # text actually searched: the HyDE rewrite or the question
    ranking: list[dict]           # every paragraph, best first
    hits: list[dict]              # the top_k of ranking
    gate_score: float             # compared with Retriever.min_score
    top_dense_score: float | None
    hyde_error: str | None = None
    routed: str | None = None     # "doc_id" when the question cited document IDs
    doc_ids: list[str] | None = None
    doc_id_unknown: bool = False  # cited IDs, none of which appear in the statement


class Retriever:
    def __init__(self, paragraphs: list[dict], statement_id: str, kind: str | None = None,
                 hyde: bool | None = None, top_k: int | None = None, min_score: float | None = None,
                 dense_weight: float | None = None):
        s = get_settings()
        self.kind = kind or s.retriever
        if self.kind not in KINDS:
            raise ValueError(f"unknown retriever {self.kind!r}; expected one of {KINDS}")
        self.hyde = s.hyde_enabled if hyde is None else hyde
        self.dense_weight = s.dense_weight if dense_weight is None else dense_weight
        self.top_k = top_k or s.retrieval_top_k
        default_min = s.retrieval_min_score if self.kind == "bm25" else s.dense_min_score
        self.min_score = default_min if min_score is None else min_score
        self.paragraphs = paragraphs
        self.bm25 = BM25Index(paragraphs) if self.kind in ("bm25", "hybrid") else None
        self.dense = DenseIndex(paragraphs, statement_id) if self.kind in ("dense", "hybrid") else None
        self.doc_id_re = re.compile(s.doc_id_pattern, re.IGNORECASE) if s.doc_id_pattern else None

    def _doc_ids(self, question: str) -> list[str]:
        if not self.doc_id_re:
            return []
        return list(dict.fromkeys(m.upper() for m in self.doc_id_re.findall(question)))

    def _rewrite(self, question: str) -> tuple[str, str | None]:
        """HyDE: a hypothetical statement passage answering the question; the question on failure."""
        s = get_settings()
        template = (s.resolve(s.prompts_dir) / s.hyde_prompt).read_text()
        try:
            text = THINK_RE.sub("", chat([{"role": "user", "content": template.format(question=question)}])).strip()
        except Exception as e:
            return question, f"{type(e).__name__}: {e}"
        return (text, None) if text else (question, "empty HyDE reply")

    def retrieve(self, question: str) -> Retrieval:
        doc_ids = self._doc_ids(question)
        query, hyde_error = self._rewrite(question) if self.hyde and not doc_ids else (question, None)
        rewritten = query != question
        bm25_ranking = self.bm25.rank(query) if self.bm25 else None
        dense_ranking = None
        if self.dense:
            dense_ranking = self.dense.rank_passage(query) if rewritten else self.dense.rank(query)

        if self.kind == "bm25":
            ranking = bm25_ranking
        elif self.kind == "dense":
            ranking = dense_ranking
        else:
            ranking = fuse(bm25_ranking, dense_ranking, self.dense_weight)
        top_dense = dense_ranking[0]["score"] if dense_ranking else None
        gate = bm25_ranking[0]["score"] if self.kind == "bm25" else top_dense

        if not doc_ids:
            return Retrieval(query, ranking, ranking[:self.top_k], gate, top_dense, hyde_error)
        lowered = [d.lower() for d in doc_ids]
        matched = {h["para_no"] for h in ranking if any(d in h["text"].lower() for d in lowered)}
        by_no = {h["para_no"]: h for h in ranking}
        routed = [by_no[h["para_no"]] for h in (dense_ranking or ranking) if h["para_no"] in matched]
        ranking = routed + [h for h in ranking if h["para_no"] not in matched]
        return Retrieval(query, ranking, ranking[:self.top_k], gate, top_dense, hyde_error,
                         routed="doc_id", doc_ids=doc_ids, doc_id_unknown=not matched)

    def passes_gate(self, retrieval: Retrieval) -> bool:
        if retrieval.routed == "doc_id" and not retrieval.doc_id_unknown:
            return True
        return bool(retrieval.hits) and retrieval.gate_score >= self.min_score

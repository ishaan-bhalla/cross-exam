"""Reciprocal rank fusion of BM25 and dense rankings: score = sum of 1 / (rrf_k + rank)."""
from crossexam.config import get_settings
from crossexam.retrieval.bm25 import BM25Index
from crossexam.retrieval.dense import DenseIndex


def fuse(bm25_ranking: list[dict], dense_ranking: list[dict]) -> list[dict]:
    """Every paragraph, best fused score first, with each component's rank and raw score."""
    k = get_settings().rrf_k
    bm25 = {h["para_no"]: (rank, h) for rank, h in enumerate(bm25_ranking, start=1)}
    dense = {h["para_no"]: (rank, h) for rank, h in enumerate(dense_ranking, start=1)}
    fused = []
    for para_no, (b_rank, b_hit) in bm25.items():
        d_rank, d_hit = dense[para_no]
        fused.append({**b_hit, "score": 1 / (k + b_rank) + 1 / (k + d_rank),
                      "bm25_rank": b_rank, "bm25_score": b_hit["score"],
                      "dense_rank": d_rank, "dense_score": d_hit["score"]})
    return sorted(fused, key=lambda h: h["score"], reverse=True)


class HybridIndex:
    def __init__(self, paragraphs: list[dict], statement_id: str):
        self.bm25 = BM25Index(paragraphs)
        self.dense = DenseIndex(paragraphs, statement_id)

    def rank(self, question: str) -> list[dict]:
        return fuse(self.bm25.rank(question), self.dense.rank(question))

    def search(self, question: str, top_k: int | None = None) -> list[dict]:
        return self.rank(question)[:top_k or get_settings().retrieval_top_k]

"""Dense retrieval over a statement's paragraphs with fastembed.

Paragraph vectors are cached under processed_dir, keyed by statement and
model, and rebuilt if the paragraph texts change. Queries get the model's
query prefix (embedding_query_prefix); passages, including HyDE rewrites,
are embedded without it.
"""
import hashlib
import numpy as np
from fastembed import TextEmbedding
from crossexam.config import get_settings

_models: dict[str, TextEmbedding] = {}


def _model() -> TextEmbedding:
    s = get_settings()
    if s.embedding_model not in _models:
        _models[s.embedding_model] = TextEmbedding(model_name=s.embedding_model,
                                                   cache_dir=str(s.resolve(s.embedding_cache_dir)))
    return _models[s.embedding_model]


def _embed(texts: list[str]) -> np.ndarray:
    v = np.array(list(_model().embed(texts)), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


class DenseIndex:
    def __init__(self, paragraphs: list[dict], statement_id: str):
        s = get_settings()
        self.paragraphs = paragraphs
        texts = [p["text"] for p in paragraphs]
        digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()
        cache = s.resolve(s.processed_dir) / f"{statement_id}.{s.embedding_model.replace('/', '__')}.npz"
        if cache.exists():
            data = np.load(cache)
            if str(data["digest"]) == digest:
                self.vectors = data["vectors"]
                return
        self.vectors = _embed(texts)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, vectors=self.vectors, digest=digest)

    def _rank(self, vector: np.ndarray) -> list[dict]:
        scores = self.vectors @ vector
        order = np.argsort(-scores)
        return [{**self.paragraphs[i], "score": float(scores[i])} for i in order]

    def rank(self, question: str) -> list[dict]:
        """Every paragraph by cosine similarity to the question, best first."""
        return self._rank(_embed([get_settings().embedding_query_prefix + question])[0])

    def rank_passage(self, text: str) -> list[dict]:
        """As rank(), but embeds text as a passage (no query prefix), e.g. a HyDE rewrite."""
        return self._rank(_embed([text])[0])

    def search(self, question: str, top_k: int | None = None) -> list[dict]:
        return self.rank(question)[:top_k or get_settings().retrieval_top_k]

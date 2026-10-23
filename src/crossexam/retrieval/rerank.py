"""Optional cross-encoder reranking with fastembed.

The retriever's ranking (dense by default) supplies the top rerank_candidates units; a
cross-encoder scores each against the question and they are reordered by that score. The
text scored is the same as the dense index embeds, so a sub-unit carries its section heading
and parent's lead-in. The refusal gate is not affected: it still uses the top dense score.
"""
from fastembed.rerank.cross_encoder import TextCrossEncoder
from crossexam.config import get_settings
from crossexam.retrieval.dense import embedding_texts

_models: dict[str, TextCrossEncoder] = {}


def _model(name: str) -> TextCrossEncoder:
    if name not in _models:
        s = get_settings()
        _models[name] = TextCrossEncoder(model_name=name, cache_dir=str(s.resolve(s.embedding_cache_dir)))
    return _models[name]


def rerank(question: str, ranking: list[dict], paragraphs: list[dict], model: str, candidates: int) -> list[dict]:
    """ranking with its first `candidates` units reordered by cross-encoder score. Each
    reranked unit gets "rerank_score" and "pre_rerank_rank"; the rest keep their order."""
    head, tail = ranking[:candidates], ranking[candidates:]
    if not head:
        return ranking
    texts = dict(zip((p["para_no"] for p in paragraphs), embedding_texts(paragraphs)))
    scores = list(_model(model).rerank(question, [texts.get(h["para_no"], h["text"]) for h in head]))
    scored = [{**h, "rerank_score": float(sc), "pre_rerank_rank": i + 1} for i, (h, sc) in enumerate(zip(head, scores))]
    return sorted(scored, key=lambda h: h["rerank_score"], reverse=True) + tail

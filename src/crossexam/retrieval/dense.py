"""Dense retrieval over a statement's paragraphs with fastembed.

Paragraph vectors are cached under processed_dir, keyed by statement and
model, and rebuilt if the texts embedded change (persist=False keeps them in
memory only, for uploads). Queries get the model's query prefix
(embedding_query_prefix); passages, including HyDE rewrites, are embedded without it.

A sub-unit ("7(a)", "12.1") is often a fragment that only makes sense under its
parent ("the following teams:" ... "(a) The Agents Application Process Team"), so it
is embedded with its section heading and the start of its parent paragraph in front.
Top-level paragraphs are embedded as they are. Results and citations stay per unit.
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
                                                   cache_dir=str(s.resolve(s.embedding_cache_dir)),
                                                   threads=int(s.embedding_threads) if s.embedding_threads.strip() else None)
    return _models[s.embedding_model]


def _embed(texts: list[str]) -> np.ndarray:
    # Small batches: each batch is padded to its longest text (up to 512 tokens), and ONNX keeps
    # the peak allocation, so a large batch of long statement units costs gigabytes.
    v = np.array(list(_model().embed(texts, batch_size=get_settings().embedding_batch_size)), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _parent_id(unit_id) -> int | str | None:
    """'2.6.1' -> '2.6', '2.6' -> 2, '7(a)' -> 7, '12.1(b)' -> '12.1'; None for a top-level paragraph."""
    if isinstance(unit_id, int):
        return None
    text = str(unit_id)
    parent = text[:text.index("(")] if "(" in text else text.rsplit(".", 1)[0] if "." in text else None
    if parent is None:
        return None
    return int(parent) if parent.isdigit() else parent


def embedding_texts(paragraphs: list[dict]) -> list[str]:
    """What is embedded for each unit: the text itself for a paragraph; for a sub-unit, its
    section heading and its parent's lead-in (first subunit_context_max_words words of the
    nearest ancestor with text) before its own text."""
    s = get_settings()
    by_id = {p["para_no"]: p for p in paragraphs}
    out = []
    for p in paragraphs:
        parent = _parent_id(p["para_no"])
        if not s.subunit_context_enabled or parent is None:
            out.append(p["text"])
            continue
        while parent is not None and parent not in by_id:  # e.g. an implicit parent with no text
            parent = _parent_id(parent)
        lead = " ".join(by_id[parent]["text"].split()[:s.subunit_context_max_words]) if parent is not None else ""
        prefix = " ".join(x for x in (p.get("section") and f"{p['section']}.", lead) if x)
        out.append(f"{prefix} {p['text']}" if prefix else p["text"])
    return out


class DenseIndex:
    def __init__(self, paragraphs: list[dict], statement_id: str, persist: bool = True):
        s = get_settings()
        self.paragraphs = paragraphs
        texts = embedding_texts(paragraphs)
        digest = hashlib.sha256("\n".join(texts).encode()).hexdigest()
        if not persist:
            self.vectors = _embed(texts)
            return
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

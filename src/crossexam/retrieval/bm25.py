import re
from rank_bm25 import BM25Okapi
from crossexam.config import get_settings

TOKEN_RE = re.compile(r"\w+")
# Question words and pronouns would otherwise dominate short questions.
STOPWORDS = set("""a an the and or of to in on at for by with from as is was were be been are am
i you your my me we our it its this that these those what which who whom when where why how
did do does done have has had not no but if so than then there their they them he she his her
about into over any all""".split())
SUFFIXES = ("ings", "ing", "edly", "ed", "es", "s")


def _stem(word: str) -> str:
    for suf in SUFFIXES:
        if len(word) > len(suf) + 2 and word.endswith(suf):
            return word[:-len(suf)]
    return word


def tokenize(text: str) -> list[str]:
    return [_stem(w) for w in TOKEN_RE.findall(text.lower()) if w not in STOPWORDS]


class BM25Index:
    def __init__(self, paragraphs: list[dict]):
        self.paragraphs = paragraphs
        self.bm25 = BM25Okapi([tokenize(p["text"]) for p in paragraphs])

    def search(self, question: str, top_k: int | None = None) -> list[dict]:
        """Top-k paragraphs for the question, best first, each with a "score" key."""
        k = top_k or get_settings().retrieval_top_k
        scores = self.bm25.get_scores(tokenize(question))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        return [{**self.paragraphs[i], "score": float(scores[i])} for i in ranked]

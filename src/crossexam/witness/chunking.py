"""Split a spoken quote into chunks for separate TTS flushes.

The service sends a flush's word timings only once the whole flush is generated, and for
long flushes sometimes not at all, so a long quote is sent as several short flushes.

Splits are made only at whitespace, so a document ID or a number ("£2.8", "1,000") is
never broken. First at sentence ends (".", "!", "?" or the elision pause "…" followed by
a space and a capital, digit or quote mark; an abbreviation such as "Ltd." or "Mr.", or a
full stop inside parentheses, is not a sentence end), then any sentence longer than max_words at clause punctuation
(",", ";", ":", "…"): at the last clause break within max_words, or, if there is none,
at the first one after it. A sentence with no clause break stays whole.
"""
import re
from crossexam.config import get_settings

ABBREVIATIONS = {"ltd", "mr", "mrs", "ms", "dr", "no", "co", "plc", "st", "vs", "etc", "e.g", "i.e", "inc"}
SENTENCE_END_RE = re.compile(r"[.!?…][\"'’”)]*$")
CLAUSE_END_RE = re.compile(r"[,;:…][\"'’”)]*$")
SENTENCE_START_RE = re.compile(r"^[\"'‘“(]*[A-Z0-9]")


def _is_sentence_end(token: str, next_token: str | None) -> bool:
    if next_token is None or not SENTENCE_END_RE.search(token) or not SENTENCE_START_RE.match(next_token):
        return False
    return token.rstrip(".\"'’”)").lower() not in ABBREVIATIONS


def sentences(text: str) -> list[list[str]]:
    """Sentences as token lists. A full stop inside open parentheses, as in a document title
    "(Royal Mail Internal Information. Back Office ...)", does not end a sentence."""
    tokens = text.split()
    out, cur, depth = [], [], 0
    for i, tok in enumerate(tokens):
        cur.append(tok)
        depth = max(0, depth + tok.count("(") - tok.count(")"))
        if depth == 0 and _is_sentence_end(tok, tokens[i + 1] if i + 1 < len(tokens) else None):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _split_long(tokens: list[str], max_words: int) -> list[list[str]]:
    chunks = []
    while len(tokens) > max_words:
        breaks = [i + 1 for i, t in enumerate(tokens[:-1]) if CLAUSE_END_RE.search(t)]
        if not breaks:
            break
        within = [b for b in breaks if b <= max_words]
        cut = within[-1] if within else breaks[0]
        chunks.append(tokens[:cut])
        tokens = tokens[cut:]
    chunks.append(tokens)
    return chunks


def chunk_quote(text: str, max_words: int | None = None) -> list[str]:
    """Chunks whose concatenation (joined by single spaces) is the whitespace-normalised text."""
    max_words = max_words or get_settings().tts_chunk_max_words
    return [" ".join(c) for sent in sentences(text) for c in _split_long(sent, max_words) if c]


def clauses(text: str) -> list[str]:
    """Every sentence and clause of text, in order: split at sentence ends and after
    clause punctuation, only at whitespace (so IDs, numbers and abbreviations stay whole)."""
    out = []
    for sent in sentences(text):
        cur = []
        for i, tok in enumerate(sent):
            cur.append(tok)
            if i < len(sent) - 1 and CLAUSE_END_RE.search(tok):
                out.append(" ".join(cur))
                cur = []
        if cur:
            out.append(" ".join(cur))
    return out

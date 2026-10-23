"""Speech-to-text key terms for a statement: its acronyms and the document IDs it cites.

Acronyms are capitalised tokens of 2-8 characters ("POL", "NBSC", "P&BA", "SPMs") that occur
at least keyterms_min_acronym_count times; roman numerals and single letters are left out.
Document IDs are whatever doc_id_pattern matches. Nothing here is specific to one witness.
"""
import re
from collections import Counter
from crossexam.config import get_settings

ACRONYM_RE = re.compile(r"(?<![\w&])([A-Z][A-Z0-9&]{1,7})(s?)(?![\w&])")
ROMAN_RE = re.compile(r"^[IVXLCDM]+$")


def cited_ids(paragraphs: list[dict]) -> list[str]:
    """Document IDs in order of first mention."""
    pattern = get_settings().doc_id_pattern
    if not pattern:
        return []
    found = re.compile(pattern)
    return list(dict.fromkeys(i for p in paragraphs for i in found.findall(p["text"])))


def acronyms(paragraphs: list[dict]) -> list[str]:
    """Acronyms used at least keyterms_min_acronym_count times, most frequent first."""
    s = get_settings()
    counts = Counter()
    for p in paragraphs:
        # The pattern's boundaries already keep it from matching inside a document ID.
        for word, _plural in ACRONYM_RE.findall(p["text"]):
            if sum(c.isalpha() for c in word) >= 2 and not ROMAN_RE.match(word):
                counts[word] += 1
    return [w for w, n in counts.most_common() if n >= s.keyterms_min_acronym_count]


def statement_keyterms(paragraphs: list[dict]) -> list[str]:
    """Acronyms, then cited document IDs, de-duplicated and capped at stt_max_keyterms."""
    return list(dict.fromkeys(acronyms(paragraphs) + cited_ids(paragraphs)))[:get_settings().stt_max_keyterms]

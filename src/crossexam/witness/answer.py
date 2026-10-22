"""Answer a question as the witness, quoting only from their statement.

Paragraphs come from the configured retriever. If retrieval fails its refusal
gate (see retrieval.retriever) the witness says the point is not covered,
without calling the LLM. If the relevance check is on, the LLM is first
asked whether any retrieved paragraph directly answers the question; if not,
the witness says it is not covered. Otherwise the LLM picks a
paragraph, copies a quote from it (optionally elided with "...") and picks a
lead-in id from a fixed menu. The quote is checked against the chosen
paragraph, then against the other retrieved paragraphs; if it matches none,
the answer falls back to the first sentence of the top-ranked paragraph and
is marked unverified.
"""
import functools
import json
import re
from crossexam.clients.llm_gateway import chat
from crossexam.config import get_settings
from crossexam.retrieval.retriever import Retrieval, Retriever
import time
from crossexam.witness.chunking import CLAUSE_END_RE, clauses, sentences

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘(A-Z0-9])")
ELLIPSIS_RE = re.compile(r"\.\.\.|…")
# Straight and curly single and double quotes all compare as the same character; so do dashes.
QUOTE_CHARS = str.maketrans({"‘": '"', "’": '"', "“": '"', "”": '"', "'": '"',
                             "–": "-", "—": "-"})
QUOTE_MARKS = "\"'‘’“”"
# Trimming the ends of a segment cannot make it match text that it would not match
# untrimmed, so wrapping quotation marks and closing punctuation are dropped before matching.
SEGMENT_EDGES_RE = re.compile(rf"^[\s{QUOTE_MARKS}]+|[\s{QUOTE_MARKS}.,;:!?]+$")
# Joins the parts of an elided quote in the spoken text; TTS reads it as a short pause.
SPOKEN_PAUSE = "… "
DEFAULT_DEMEANOURS = ("confident", "neutral")


def _load_prompt() -> tuple[str, dict[str, str]]:
    """(system prompt template, fixed phrases) from the prompt file. The header above
    the "---" line holds one "KEY: phrase" per line (NOT_COVERED, NOT_RECOGNISED)."""
    s = get_settings()
    header, template = (s.resolve(s.prompts_dir) / s.witness_answer_prompt).read_text().split("\n---\n", 1)
    phrases = dict((k.strip(), v.strip()) for k, v in
                   (line.split(":", 1) for line in header.splitlines() if ":" in line))
    return template, phrases


def fixed_phrases() -> list[str]:
    """Every phrase the witness can say verbatim: lead-ins without placeholders, and the
    fixed refusal phrases. These can be synthesised ahead of time."""
    s = get_settings()
    lead_ins = json.loads((s.resolve(s.prompts_dir) / s.lead_ins_file).read_text())
    texts = [li["text"] for li in lead_ins if "{" not in li["text"]]
    return list(dict.fromkeys(texts + list(_load_prompt()[1].values())))


def _load_lead_ins(demeanour: str | None) -> dict[str, str]:
    """{id: text} for the requested demeanour (default: confident + neutral)."""
    s = get_settings()
    lead_ins = json.loads((s.resolve(s.prompts_dir) / s.lead_ins_file).read_text())
    wanted = {demeanour} if demeanour else set(DEFAULT_DEMEANOURS)
    known = {li["demeanour"] for li in lead_ins}
    if not wanted <= known:
        raise ValueError(f"unknown demeanour {demeanour!r}; expected one of {sorted(known)}")
    return {li["id"]: li["text"] for li in lead_ins if li["demeanour"] in wanted}


def _normalise(text: str) -> str:
    return " ".join(text.translate(QUOTE_CHARS).split()).lower()


def _strip_quote_marks(quote: str) -> str:
    """Remove quotation marks wrapping the whole quote (the LLM sometimes adds them)."""
    return quote.strip().strip(QUOTE_MARKS).strip()


def _segments(quote: str) -> list[str]:
    return [seg.strip() for seg in ELLIPSIS_RE.split(_strip_quote_marks(quote)) if seg.strip()]


def _matches(segments: list[str], text: str) -> bool:
    """Every segment, minus its wrapping quote marks and closing punctuation, is a
    normalised substring of text, in order."""
    needles = [n for n in (_normalise(SEGMENT_EDGES_RE.sub("", seg)) for seg in segments) if n]
    haystack, pos = _normalise(text), 0
    for needle in needles:
        i = haystack.find(needle, pos)
        if i < 0:
            return False
        pos = i + len(needle)
    return bool(needles)


def quote_segments(quote: str) -> list[str]:
    """The quote's parts between ellipses, wrapping quotation marks removed."""
    return _segments(quote)


def spoken_quote(quote: str) -> str:
    """The quote as spoken: its segments joined with a short pause."""
    return SPOKEN_PAUSE.join(_segments(quote))


def quote_matches(quote: str, text: str) -> bool:
    """True if quote (optionally elided with "...") is a verbatim extract of text."""
    return _matches(_segments(quote), text)


def limit_quote(quote: str, max_words: int) -> str:
    """The quote cut to at most max_words, by whole sentences where possible. If even the
    first sentence is too long, it is cut at its last clause break (",", ";", ":") within the
    limit, or at the limit. Every cut keeps a verbatim prefix of the quote, so a quote that
    verifies still verifies."""
    if len(quote.split()) <= max_words:
        return quote
    kept = []
    for sent in sentences(quote):
        if len(kept) + len(sent) > max_words:
            break
        kept.extend(sent)
    if not kept:
        first = sentences(quote)[0][:max_words]
        breaks = [i + 1 for i, t in enumerate(first) if CLAUSE_END_RE.search(t)]
        kept = first[:breaks[-1]] if breaks else first
        kept[-1] = kept[-1].rstrip(",;:")
    return " ".join(kept)


def join_clauses(parts: list[str], chosen: list[int]) -> str:
    """The chosen clauses (0-based) in original order; skipped clauses become "…", so the
    result passes quote verification segment by segment. Trailing clause punctuation is
    dropped before a gap and at the end."""
    out, prev = [], None
    for i in sorted(set(chosen)):
        if prev is not None and i != prev + 1:
            out[-1] = out[-1].rstrip(",;:") + "…"
        out.append(parts[i])
        prev = i
    out[-1] = out[-1].rstrip(",;:")
    return " ".join(out)


def select_clauses(quote: str, question: str, max_words: int) -> tuple[str | None, dict]:
    """Ask the LLM which clauses of an over-long quote answer the question.
    (shortened quote or None if the reply is unusable, {"ms", "raw", "chosen", "kept"}). Chosen
    clauses are kept in original order while they fit max_words; None if even the first does not."""
    s = get_settings()
    parts = clauses(quote)
    listing = "\n".join(f"[{i}] ({len(c.split())} words) {c}" for i, c in enumerate(parts, 1))
    template = (s.resolve(s.prompts_dir) / s.quote_select_prompt).read_text()
    started = time.perf_counter()
    info = {"ms": None, "raw": "", "chosen": None, "kept": None}
    try:
        info["raw"] = chat([{"role": "user", "content": template.format(
            question=question, clauses=listing, max_words=max_words)}])
    except Exception as e:
        info["raw"] = f"{type(e).__name__}: {e}"
    info["ms"] = (time.perf_counter() - started) * 1000
    reply = _parse_reply(info["raw"]) or {}
    chosen = reply.get("clauses")
    if not (isinstance(chosen, list) and chosen and all(isinstance(n, int) and 1 <= n <= len(parts) for n in chosen)):
        return None, info
    info["chosen"] = sorted(set(chosen))
    # Keep the chosen clauses, in original order, while they fit the limit.
    kept, words = [], 0
    for n in info["chosen"]:
        size = len(parts[n - 1].split())
        if words + size > max_words:
            break
        kept.append(n - 1)
        words += size
    info["kept"] = [n + 1 for n in kept]
    return (join_clauses(parts, kept) if kept else None), info


def _first_sentence(text: str) -> str:
    return SENTENCE_RE.split(text.strip(), 1)[0]


def _parse_reply(raw: str) -> dict | None:
    m = JSON_RE.search(THINK_RE.sub("", raw))
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _relevance_check(question: str, paragraphs: list[dict]) -> dict:
    """{"answers": bool | None, "para_no": int | None, "raw": str, "error": str | None}.
    answers is None when the check could not be run or parsed; the caller then proceeds."""
    s = get_settings()
    template = (s.resolve(s.prompts_dir) / s.relevance_check_prompt).read_text()
    context = "\n\n".join(f"[Paragraph {p['para_no']}] {p['text']}" for p in paragraphs)
    try:
        raw = chat([{"role": "user", "content": template.format(paragraphs=context, question=question)}])
    except Exception as e:
        return {"answers": None, "para_no": None, "raw": "", "error": f"{type(e).__name__}: {e}"}
    reply = _parse_reply(raw)
    if reply is None or not isinstance(reply.get("answers"), bool):
        return {"answers": None, "para_no": None, "raw": raw, "error": "unparseable reply"}
    para_no = reply.get("para_no")
    return {"answers": reply["answers"], "para_no": para_no if isinstance(para_no, int) else None,
            "raw": raw, "error": None}


def _base_result(**fields) -> dict:
    base = {"quote": "", "para_no": None, "lead_in_id": None, "lead_in": "", "verified": False,
            "para_corrected": False, "fallback": False, "skipped_llm": False,
            "relevance_rejected": False, "relevance": None, "routed": None, "doc_id_unknown": False,
            "shortening": None,
            "spoken": "", "raw": ""}
    return {**base, **fields}


def answer(question: str, retriever: Retriever, witness_name: str,
           demeanour: str | None = None, retrieval: Retrieval | None = None,
           relevance_check: bool | None = None) -> dict:
    """Retrieves with retriever unless a retrieval for this question is passed in.
    relevance_check overrides the relevance_check_enabled setting.

    Returns {quote, para_no, lead_in_id, lead_in, verified, para_corrected,
    fallback, skipped_llm, relevance_rejected, relevance, spoken, raw}; raw is
    the answer LLM's reply text and relevance the check's result (or None).
    """
    template, phrases = _load_prompt()
    not_covered = phrases["NOT_COVERED"]
    lead_ins = _load_lead_ins(demeanour)
    retrieval = retrieval or retriever.retrieve(question)
    paragraphs = retrieval.hits
    _result = functools.partial(_base_result, routed=retrieval.routed)

    if retrieval.doc_id_unknown:
        return _result(verified=True, skipped_llm=True, doc_id_unknown=True, spoken=phrases["NOT_RECOGNISED"])
    if not retriever.passes_gate(retrieval):
        return _result(verified=True, skipped_llm=True, spoken=not_covered)

    check = get_settings().relevance_check_enabled if relevance_check is None else relevance_check
    relevance = _relevance_check(question, paragraphs) if check else None
    if relevance and relevance["answers"] is False:
        return _result(verified=True, relevance_rejected=True, relevance=relevance, spoken=not_covered)

    context = "\n\n".join(f"[Paragraph {p['para_no']}] {p['text']}" for p in paragraphs)
    menu = "\n".join(f"- {i}: {text}" for i, text in lead_ins.items())
    raw = chat([
        {"role": "system", "content": template.format(witness_name=witness_name, lead_ins=menu,
                                                      quote_max_words=get_settings().quote_max_words)},
        {"role": "user", "content": f"Statement paragraphs:\n\n{context}\n\nQuestion: {question}"},
    ])
    reply = _parse_reply(raw)

    if reply is not None and reply.get("para_no") is None:
        return _result(verified=True, relevance=relevance, spoken=not_covered, raw=raw)

    by_no = {p["para_no"]: p for p in paragraphs}
    quote, lead_in_id, para = "", None, None
    shortening = None  # set when an over-long quote was shortened
    if reply is not None:
        quote = _strip_quote_marks(str(reply.get("quote") or ""))
        max_words = get_settings().quote_max_words
        if len(quote.split()) > max_words:
            # Too long to read out: pick the clauses that answer the question, else trim.
            quote_words = len(quote.split())
            selected, select_info = select_clauses(quote, question, max_words)
            quote = selected or limit_quote(quote, max_words)
            shortening = {"from_words": quote_words, "method": "selected" if selected else "trimmed",
                          "select_ms": round(select_info["ms"]), "chosen": select_info["chosen"],
                          "kept": select_info["kept"],
                          "select_raw": select_info["raw"]}
        lead_in_id = reply.get("lead_in_id")
        try:
            para = by_no.get(int(reply["para_no"]))
        except (TypeError, ValueError):
            para = None
    segments = _segments(quote)

    verified = bool(para and _matches(segments, para["text"]))
    para_corrected = False
    if not verified and segments:
        others = [p for p in paragraphs if p is not para and _matches(segments, p["text"])]
        if len(others) == 1:
            para, verified, para_corrected = others[0], True, True

    fallback = not verified
    if fallback:
        # The lead-in was chosen for a quote we are discarding, so drop it too.
        para = paragraphs[0]
        quote = limit_quote(_first_sentence(para["text"]), get_settings().quote_max_words)
        segments, lead_in_id = [quote], None

    lead_in = lead_ins.get(lead_in_id, "") if isinstance(lead_in_id, str) else ""
    lead_in = lead_in.format(para_no=para["para_no"])
    spoken = f"{lead_in} {SPOKEN_PAUSE.join(segments)}".strip()
    return _result(quote=quote, para_no=para["para_no"], lead_in_id=lead_in_id if lead_in else None,
                   lead_in=lead_in, verified=verified, para_corrected=para_corrected,
                   fallback=fallback, relevance=relevance, spoken=spoken, raw=raw,
                   shortening=shortening)

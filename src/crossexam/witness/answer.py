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


def quote_matches(quote: str, text: str) -> bool:
    """True if quote (optionally elided with "...") is a verbatim extract of text."""
    return _matches(_segments(quote), text)


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
        {"role": "system", "content": template.format(witness_name=witness_name, lead_ins=menu)},
        {"role": "user", "content": f"Statement paragraphs:\n\n{context}\n\nQuestion: {question}"},
    ])
    reply = _parse_reply(raw)

    if reply is not None and reply.get("para_no") is None:
        return _result(verified=True, relevance=relevance, spoken=not_covered, raw=raw)

    by_no = {p["para_no"]: p for p in paragraphs}
    quote, lead_in_id, para = "", None, None
    if reply is not None:
        quote = _strip_quote_marks(str(reply.get("quote") or ""))
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
        quote = _first_sentence(para["text"])
        segments, lead_in_id = [quote], None

    lead_in = lead_ins.get(lead_in_id, "") if isinstance(lead_in_id, str) else ""
    lead_in = lead_in.format(para_no=para["para_no"])
    spoken = f"{lead_in} {SPOKEN_PAUSE.join(segments)}".strip()
    return _result(quote=quote, para_no=para["para_no"], lead_in_id=lead_in_id if lead_in else None,
                   lead_in=lead_in, verified=verified, para_corrected=para_corrected,
                   fallback=fallback, relevance=relevance, spoken=spoken, raw=raw)

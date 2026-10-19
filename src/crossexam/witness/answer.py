"""Answer a question as the witness, quoting only from their statement.

If the best retrieval score is below retrieval_min_score the witness says the
point is not covered, without calling the LLM. Otherwise the LLM picks a
paragraph, copies a quote from it (optionally elided with "...") and picks a
lead-in id from a fixed menu. The quote is checked against the chosen
paragraph, then against the other retrieved paragraphs; if it matches none,
the answer falls back to the first sentence of the top-ranked paragraph and
is marked unverified.
"""
import json
import re
from crossexam.clients.llm_gateway import chat
from crossexam.config import get_settings

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘(A-Z0-9])")
ELLIPSIS_RE = re.compile(r"\.\.\.|…")
QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                             "–": "-", "—": "-"})
# Joins the parts of an elided quote in the spoken text; TTS reads it as a short pause.
SPOKEN_PAUSE = "… "
DEFAULT_DEMEANOURS = ("confident", "neutral")


def _load_prompt() -> tuple[str, str]:
    """(system prompt template, not-covered phrase) from the prompt file."""
    s = get_settings()
    header, template = (s.resolve(s.prompts_dir) / s.witness_answer_prompt).read_text().split("\n---\n", 1)
    not_covered = header.split(":", 1)[1].strip()
    return template, not_covered


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


def _segments(quote: str) -> list[str]:
    return [seg.strip() for seg in ELLIPSIS_RE.split(quote) if seg.strip()]


def _matches(segments: list[str], text: str) -> bool:
    """Every segment is a normalised substring of text, in order."""
    haystack, pos = _normalise(text), 0
    for seg in segments:
        i = haystack.find(_normalise(seg), pos)
        if i < 0:
            return False
        pos = i + len(_normalise(seg))
    return bool(segments)


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


def _result(**fields) -> dict:
    base = {"quote": "", "para_no": None, "lead_in_id": None, "lead_in": "", "verified": False,
            "para_corrected": False, "skipped_llm": False, "spoken": "", "raw": ""}
    return {**base, **fields}


def answer(question: str, paragraphs: list[dict], witness_name: str,
           demeanour: str | None = None) -> dict:
    """paragraphs: retrieved paragraphs, best first, each with a BM25 "score".

    Returns {quote, para_no, lead_in_id, lead_in, verified, para_corrected,
    skipped_llm, spoken, raw}; raw is the LLM's reply text.
    """
    s = get_settings()
    template, not_covered = _load_prompt()
    lead_ins = _load_lead_ins(demeanour)

    if not paragraphs or paragraphs[0]["score"] < s.retrieval_min_score:
        return _result(verified=True, skipped_llm=True, spoken=not_covered)

    context = "\n\n".join(f"[Paragraph {p['para_no']}] {p['text']}" for p in paragraphs)
    menu = "\n".join(f"- {i}: {text}" for i, text in lead_ins.items())
    raw = chat([
        {"role": "system", "content": template.format(witness_name=witness_name, lead_ins=menu)},
        {"role": "user", "content": f"Statement paragraphs:\n\n{context}\n\nQuestion: {question}"},
    ])
    reply = _parse_reply(raw)

    if reply is not None and reply.get("para_no") is None:
        return _result(verified=True, spoken=not_covered, raw=raw)

    by_no = {p["para_no"]: p for p in paragraphs}
    quote, lead_in_id, para = "", None, None
    if reply is not None:
        quote = str(reply.get("quote") or "").strip()
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

    if not verified:
        # The lead-in was chosen for a quote we are discarding, so drop it too.
        para = paragraphs[0]
        quote = _first_sentence(para["text"])
        segments, lead_in_id = [quote], None

    lead_in = lead_ins.get(lead_in_id, "") if isinstance(lead_in_id, str) else ""
    lead_in = lead_in.format(para_no=para["para_no"])
    spoken = f"{lead_in} {SPOKEN_PAUSE.join(segments)}".strip()
    return _result(quote=quote, para_no=para["para_no"], lead_in_id=lead_in_id if lead_in else None,
                   lead_in=lead_in, verified=verified, para_corrected=para_corrected,
                   spoken=spoken, raw=raw)

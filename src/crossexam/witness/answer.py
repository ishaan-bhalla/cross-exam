"""Answer a question as the witness, quoting only from their statement.

The LLM picks a paragraph, copies a quote from it and writes a short lead-in.
The quote is then checked against the paragraph text; if it is not there, the
answer falls back to the first sentence of the top-ranked paragraph and is
marked unverified.
"""
import json
import re
from crossexam.clients.llm_gateway import chat
from crossexam.config import get_settings

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘(A-Z0-9])")
QUOTE_CHARS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                             "–": "-", "—": "-"})


def _load_prompt() -> tuple[str, str]:
    """(system prompt template, not-covered phrase) from the prompt file."""
    s = get_settings()
    header, template = (s.resolve(s.prompts_dir) / s.witness_answer_prompt).read_text().split("\n---\n", 1)
    not_covered = header.split(":", 1)[1].strip()
    return template, not_covered


def _normalise(text: str) -> str:
    return " ".join(text.translate(QUOTE_CHARS).split()).lower()


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


def answer(question: str, paragraphs: list[dict], witness_name: str) -> dict:
    """paragraphs: retrieved paragraphs, best first.

    Returns {quote, para_no, framing, verified, spoken, raw}; raw is the LLM's reply text.
    """
    template, not_covered = _load_prompt()
    context = "\n\n".join(f"[Paragraph {p['para_no']}] {p['text']}" for p in paragraphs)
    raw = chat([
        {"role": "system", "content": template.format(witness_name=witness_name)},
        {"role": "user", "content": f"Statement paragraphs:\n\n{context}\n\nQuestion: {question}"},
    ])
    reply = _parse_reply(raw)

    if reply is not None and reply.get("para_no") is None:
        return {"quote": "", "para_no": None, "framing": not_covered, "verified": True,
                "spoken": not_covered, "raw": raw}

    by_no = {p["para_no"]: p for p in paragraphs}
    quote = framing = ""
    para = None
    if reply is not None:
        quote = str(reply.get("quote") or "").strip()
        framing = str(reply.get("framing") or "").strip()
        try:
            para = by_no.get(int(reply["para_no"]))
        except (TypeError, ValueError):
            para = None
    verified = bool(para and quote and _normalise(quote) in _normalise(para["text"]))

    if not verified:
        # The lead-in was written for a quote we are discarding, so drop it too.
        para = paragraphs[0]
        quote, framing = _first_sentence(para["text"]), ""

    spoken = f"{framing} {quote}".strip()
    return {"quote": quote, "para_no": para["para_no"], "framing": framing,
            "verified": verified, "spoken": spoken, "raw": raw}

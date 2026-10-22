"""Read a JSON object out of an LLM reply.

Replies are not always just the object: they can be wrapped in ```json fences, have text
before or after, contain <think> blocks, or hold several objects (a small model asked a
two-part question sometimes returns one answer per part). A greedy {.*} regex fails on
several objects, so instead each "{" is tried in turn with JSONDecoder.raw_decode, and the
first object that parses and has the required fields is used. Anything else is ignored.
"""
import json
import re

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_decoder = json.JSONDecoder()


def parse_json_object(text: str, required: tuple[str, ...] = ()) -> dict | None:
    """The first JSON object in text that has every key in required, else None."""
    text = THINK_RE.sub("", text or "")
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = _decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and all(k in obj for k in required):
            return obj
        i = text.find("{", i + 1)
    return None

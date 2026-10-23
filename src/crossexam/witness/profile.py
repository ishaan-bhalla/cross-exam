"""Draft witness profile from the opening of a statement, checked against the statement.

The LLM reads the preamble (witness name, statement title) and the first
profile_opening_units units, and returns a name, an organisation and roles with dates.
Every field is then looked up in the statement text (case, spacing, quote marks and dashes
ignored); a field that does not appear there is dropped, and a role whose title does not
appear is dropped whole. The name may come from the preamble (the title page); role titles
and organisations must appear in the paragraphs themselves, since a model with no role to
report tends to build one from the title ("POST OFFICE HORIZON IT INQUIRY"). A title that
ends in a dangling word ("Head of") is incomplete and dropped. The result is a draft for the user to confirm, with the dropped
values listed so the confirm screen can show what was left out and why.
"""
import time
from crossexam.clients.llm_gateway import chat
from crossexam.config import get_settings
from crossexam.llm_json import parse_json_object

QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})
ROLE_FIELDS = ("title", "organisation", "from", "to")
DANGLING = {"of", "and", "the", "for", "to", "in", "at", "a", "an", "&", "with", "on"}


def _norm(text: str) -> str:
    return " ".join(str(text).translate(QUOTES).lower().split())


def appears_in(value, haystack_norm: str) -> bool:
    """value (trimmed of surrounding punctuation) occurs in the normalised statement text."""
    v = _norm(value).strip(" .,;:()\"'")
    return bool(v) and v in haystack_norm


def statement_text(preamble: str, paragraphs: list[dict]) -> str:
    return " ".join([preamble] + [p["text"] for p in paragraphs])


def verify_profile(draft: dict, text: str, body: str | None = None) -> dict:
    """Keep only fields that appear in the statement. text is everything (preamble included);
    body, if given, is the paragraphs alone, which role titles and organisations must appear in.
    Returns {"name", "organisation", "roles", "dropped"}."""
    hay, body_hay = _norm(text), _norm(body if body is not None else text)
    dropped = []

    def keep(field, value, where=None):
        if value in (None, ""):
            return None
        where = body_hay if where is None else where
        ok = isinstance(value, str) and appears_in(value, where)
        if ok and field.endswith(".title") and value.split()[-1].lower() in DANGLING:
            ok = False  # "Head of": an incomplete title
        if ok:
            return value.strip()
        dropped.append({"field": field, "value": value})
        return None

    roles = []
    for i, role in enumerate(draft.get("roles") or []):
        if not isinstance(role, dict):
            continue
        title = keep(f"roles[{i}].title", role.get("title"))
        if title is None:
            if role.get("title") not in (None, ""):
                dropped.extend({"field": f"roles[{i}].{f}", "value": role[f]} for f in ROLE_FIELDS[1:] if role.get(f))
            continue
        roles.append({"title": title, **{f: keep(f"roles[{i}].{f}", role.get(f)) for f in ROLE_FIELDS[1:]}})
    return {"name": keep("name", draft.get("name"), hay), "organisation": keep("organisation", draft.get("organisation")),
            "roles": roles, "dropped": dropped}


def role_label(profile: dict) -> str | None:
    """'Head of Product & Branch Accounting, Post Office Ltd (2006–2016)' from verified fields."""
    if not profile.get("roles"):
        return profile.get("organisation")
    role = profile["roles"][0]
    org = role.get("organisation") or profile.get("organisation")
    dates = "–".join(d for d in (role.get("from"), role.get("to")) if d)
    return f"{role['title']}{', ' + org if org else ''}{f' ({dates})' if dates else ''}"


def draft_profile(preamble: str, paragraphs: list[dict]) -> dict:
    """{"name", "organisation", "roles", "dropped", "role_label", "ms", "raw", "error"}."""
    s = get_settings()
    opening = "\n\n".join([preamble] + [f"{p['para_no']}. {p['text']}" for p in paragraphs[:s.profile_opening_units]])
    template = (s.resolve(s.prompts_dir) / s.profile_prompt).read_text()
    started = time.perf_counter()
    try:
        raw = chat([{"role": "user", "content": template.format(text=opening, max_roles=s.profile_max_roles)}],
                   max_tokens=s.profile_max_tokens)
    except Exception as e:
        return {"name": None, "organisation": None, "roles": [], "dropped": [], "role_label": None,
                "ms": round((time.perf_counter() - started) * 1000), "raw": "", "error": f"{type(e).__name__}: {e}"}
    draft = parse_json_object(raw, required=("name",)) or {}
    profile = verify_profile(draft, statement_text(preamble, paragraphs), statement_text("", paragraphs))
    return {**profile, "role_label": role_label(profile), "ms": round((time.perf_counter() - started) * 1000),
            "raw": raw, "error": None if draft else "unparseable reply"}

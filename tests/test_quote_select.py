import json
from types import SimpleNamespace
import pytest
from crossexam.witness import answer as A
from crossexam.witness.answer import join_clauses, quote_matches, select_clauses
from crossexam.witness.chunking import clauses

PARA_64 = ("Regarding POL00055418 (Emails: The Horizon Trial), given that the vast majority of post offices "
           "were understood not to have significant concerns about Horizon, and because of reassurance from "
           "audit risk modelling, feedback from audit, training, support and the perspective of internal IT "
           "specialists, I did not have serious concerns about the system myself, but I would have been "
           "equally keen to be aware of new concerns arising in case this would enable me and my team to "
           "positively support the resolution of something if it became a confirmed issue.")


def test_clauses_split_at_sentence_and_clause_breaks_only_at_whitespace():
    parts = clauses(PARA_64)
    assert " ".join(parts) == " ".join(PARA_64.split())
    assert parts[0] == "Regarding POL00055418 (Emails:"
    assert "I did not have serious concerns about the system myself," in parts
    assert clauses("The total was £2.8 million, about 1,000 audits. I spoke to Mr. Smith at Post Office Ltd.") == [
        "The total was £2.8 million,", "about 1,000 audits.", "I spoke to Mr. Smith at Post Office Ltd."]


def test_join_clauses_keeps_order_and_marks_gaps_so_it_verifies():
    parts = clauses(PARA_64)
    i = parts.index("I did not have serious concerns about the system myself,")
    quote = join_clauses(parts, [i, 0])
    assert quote.startswith("Regarding POL00055418 (Emails…")
    assert quote.endswith("I did not have serious concerns about the system myself")
    assert quote_matches(quote, PARA_64)
    assert join_clauses(parts, [i, i + 1]).count("…") == 0  # adjacent clauses are not elided


@pytest.fixture
def settings(monkeypatch, tmp_path):
    (tmp_path / "quote_select.txt").write_text("{question}\n{clauses}\n{max_words}")
    s = SimpleNamespace(prompts_dir=tmp_path, quote_select_prompt="quote_select.txt", resolve=lambda p: p)
    monkeypatch.setattr(A, "get_settings", lambda: s)


def _reply(monkeypatch, text):
    monkeypatch.setattr(A, "chat", lambda messages: text)


def test_valid_selection_builds_the_quote(settings, monkeypatch):
    parts = clauses(PARA_64)
    n = parts.index("I did not have serious concerns about the system myself,") + 1
    _reply(monkeypatch, json.dumps({"clauses": [n]}))
    quote, info = select_clauses(PARA_64, "What did you make of POL00055418?", 40)
    assert quote == "I did not have serious concerns about the system myself"
    assert info["chosen"] == [n] and info["ms"] is not None


@pytest.mark.parametrize("reply", [
    "not json", json.dumps({"clauses": []}), json.dumps({"clauses": [0]}), json.dumps({"clauses": [99]}),
    json.dumps({"clauses": ["2"]}),
])
def test_unusable_selection_returns_none_for_the_trim_fallback(settings, monkeypatch, reply):
    _reply(monkeypatch, reply)
    assert select_clauses(PARA_64, "q", 40)[0] is None


def test_selection_over_the_limit_keeps_the_chosen_clauses_that_fit(settings, monkeypatch):
    parts = clauses(PARA_64)
    n = parts.index("I did not have serious concerns about the system myself,") + 1
    _reply(monkeypatch, json.dumps({"clauses": [n + 1, n]}))   # 10 + 36 words: over 40
    quote, info = select_clauses(PARA_64, "q", 40)
    assert quote == "I did not have serious concerns about the system myself"
    assert info["chosen"] == [n, n + 1] and info["kept"] == [n]


def test_selection_whose_first_clause_is_too_long_falls_back(settings, monkeypatch):
    _reply(monkeypatch, json.dumps({"clauses": [len(clauses(PARA_64))]}))  # the 36-word clause
    assert select_clauses(PARA_64, "q", 20)[0] is None

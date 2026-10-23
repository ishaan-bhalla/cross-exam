"""When the model's quote fails verification, the witness quotes nothing and refers
counsel to a paragraph: the model's chosen unit if it was retrieved, else the top one."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from crossexam.config import PROJECT_ROOT
from crossexam.retrieval.retriever import Retrieval
from crossexam.witness import answer as A

FIXTURES = Path(__file__).parent / "fixtures"
HITS = [{"para_no": 17, "page": 5, "score": 0.8, "text": "I left Post Office Ltd in March 2016. Then I retired."},
        {"para_no": "12.1", "page": 4, "score": 0.7, "text": "The Contract Advisor decided on suspension."},
        {"para_no": 30, "page": 9, "score": 0.6, "text": "I was not involved in prosecutions."}]


@pytest.fixture
def settings(monkeypatch):
    s = SimpleNamespace(prompts_dir=PROJECT_ROOT / "prompts", witness_answer_prompt="witness_answer.txt",
                        lead_ins_file="lead_ins.json", quote_select_prompt="quote_select.txt", quote_max_words=60,
                        relevance_check_enabled=False, resolve=lambda p: p)
    monkeypatch.setattr(A, "get_settings", lambda: s)
    return s


def ask(monkeypatch, replies, hits=HITS, question="q", routed=None):
    replies = iter(replies)
    monkeypatch.setattr(A, "chat", lambda messages: next(replies))
    retrieval = Retrieval(query=question, ranking=hits, hits=hits, gate_score=0.8, top_dense_score=0.8, routed=routed)
    return A.answer(question, SimpleNamespace(passes_gate=lambda r: True), "Witness", retrieval=retrieval)


def assert_referral(result, para_no, label):
    assert result["referral"] is True and result["verified"] is False and result["quote"] is None
    assert result["para_no"] == para_no and result["lead_in"] == ""
    assert result["spoken"] == f"I'd refer you to paragraph {label} of my statement."


def test_unverified_quote_refers_to_the_models_chosen_unit(settings, monkeypatch):
    reply = '{"para_no": 30, "quote": "I never took part in any prosecution.", "lead_in_id": "c1"}'
    assert_referral(ask(monkeypatch, [reply]), 30, "30")


def test_unretrieved_unit_refers_to_the_top_retrieved_unit(settings, monkeypatch):
    reply = '{"para_no": 99, "quote": "Something not in any retrieved unit.", "lead_in_id": "c1"}'
    assert_referral(ask(monkeypatch, [reply]), 17, "17")


def test_unparseable_reply_refers_to_the_top_retrieved_unit(settings, monkeypatch):
    assert_referral(ask(monkeypatch, ["{para_no: 30, quote: 'not valid JSON'}"]), 17, "17")


def test_sub_unit_referral_reads_as_paragraph_12_1(settings, monkeypatch):
    reply = '{"para_no": "12.1", "quote": "The Contract Advisor made the final decision.", "lead_in_id": "n1"}'
    assert_referral(ask(monkeypatch, [reply]), "12.1", "12.1")


def test_verified_quote_is_not_a_referral(settings, monkeypatch):
    result = ask(monkeypatch, ['{"para_no": 30, "quote": "I was not involved in prosecutions.", "lead_in_id": "c1"}'])
    assert result["referral"] is False and result["verified"] is True
    assert result["quote"] == "I was not involved in prosecutions."


def test_referral_phrase_is_not_pre_synthesised(settings):
    phrases = A.fixed_phrases()
    assert not any("{" in p for p in phrases)
    assert "That is not something I addressed in my witness statement." in phrases


def test_replay_u4_docref_refers_to_paragraph_51(settings, monkeypatch):
    # The held-out run's recorded reply: the model reworded the start of a 115-word quote from ¶51,
    # clause selection kept clauses 7-10, and verification failed (the old fallback then quoted ¶51's
    # first sentence, which does not give the reason asked for).
    fx = json.loads((FIXTURES / "u4_docref_replay.json").read_text())
    select_reply = json.dumps({"clauses": fx["shortening_chosen"]})
    result = ask(monkeypatch, [fx["raw"], select_reply], hits=fx["hits"], question=fx["question"], routed=fx["routed"])
    assert result["shortening"]["kept"] == [7, 8, 9, 10]
    assert_referral(result, 51, "51")

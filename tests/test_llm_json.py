from types import SimpleNamespace
from crossexam.config import PROJECT_ROOT
from crossexam.llm_json import parse_json_object

REQ = ("para_no",)


def test_two_objects_in_a_row_takes_the_first():
    # The m03 case: one answer per half of a two-part question.
    raw = ('{"para_no": 35, "quote": "branches would sometimes select other postage", "lead_in_id": "n1"} '
           '{"para_no": 36, "quote": "the other issue in this area", "lead_in_id": "n1"}')
    assert parse_json_object(raw, REQ) == {"para_no": 35, "quote": "branches would sometimes select other postage",
                                           "lead_in_id": "n1"}


def test_object_in_json_fences():
    raw = '```json\n{"para_no": 17, "quote": "I left Post Office Ltd in March 2016.", "lead_in_id": "c1"}\n```'
    assert parse_json_object(raw, REQ)["para_no"] == 17


def test_text_before_and_after():
    raw = 'Here is my answer: {"answers": true, "para_no": 64} Hope that helps.'
    assert parse_json_object(raw, ("answers",)) == {"answers": True, "para_no": 64}


def test_brace_inside_a_string_value():
    raw = '{"para_no": 5, "quote": "a } and a { inside the quote", "lead_in_id": "n2"} trailing }'
    assert parse_json_object(raw, REQ)["quote"] == "a } and a { inside the quote"


def test_think_block_and_objects_without_the_required_fields_are_skipped():
    raw = '<think>{"para_no": 1}</think> {"note": "no para_no here"} {"para_no": null, "quote": ""}'
    assert parse_json_object(raw, REQ) == {"para_no": None, "quote": ""}


def test_invalid_json_returns_none():
    for raw in ("not json at all", "{para_no: 5, quote: 'single quotes'}", '{"para_no": 5, "quote": "unterminated',
                "", "[1, 2, 3]"):
        assert parse_json_object(raw, REQ) is None


def test_invalid_reply_still_uses_the_answer_fallback(monkeypatch):
    from crossexam.retrieval.retriever import Retrieval
    from crossexam.witness import answer as A
    settings = SimpleNamespace(prompts_dir=PROJECT_ROOT / "prompts", witness_answer_prompt="witness_answer.txt",
                               lead_ins_file="lead_ins.json", quote_max_words=60, relevance_check_enabled=False,
                               resolve=lambda p: p)
    monkeypatch.setattr(A, "get_settings", lambda: settings)
    monkeypatch.setattr(A, "chat", lambda messages: "{para_no: 17, quote: 'not valid JSON'}")
    hits = [{"para_no": 17, "page": 5, "score": 0.8, "text": "I left Post Office Ltd in March 2016. Then I retired."}]
    retrieval = Retrieval(query="q", ranking=hits, hits=hits, gate_score=0.8, top_dense_score=0.8)
    retriever = SimpleNamespace(passes_gate=lambda r: True)
    result = A.answer("When did you leave?", retriever, "Rod Ismay", retrieval=retrieval)
    assert result["fallback"] is True and result["verified"] is False
    assert result["para_no"] == 17 and result["quote"] == "I left Post Office Ltd in March 2016."

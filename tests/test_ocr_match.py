"""OCR-tolerant verification: a second pass after the exact check, for quotes the model gave
with the statement's OCR errors corrected. The Roper cases are the recorded model replies
(witn09180100, ¶1 and ¶3), which were referrals before this pass existed."""
from types import SimpleNamespace
import pytest
from crossexam.config import PROJECT_ROOT
from crossexam.retrieval.retriever import Retrieval
from crossexam.witness import answer as A, ocr_match
from crossexam.witness.align import align_words

ROPER_1 = "lama former employee of Post Office Ltd and held the position of Field Support Advisor."
ROPER_3 = ('I started working for POL on 2" April 1979, after 6-week counter training course I commenced working on '
           'the counter at Wigan Post Office. I performed this duty for 12 months and then became a TV license evasion '
           'officer. This job entailed visiting private households that records showed did not hold a TV license, '
           'interviewing people with a view to prosecution if no licence could be produced. The job also involved '
           'attendance at court when people pleaded not guilty.')
REPLY_1 = ('{"para_no": 1, "quote": "I am a former employee of Post Office Ltd and held the position of Field Support '
           'Advisor.", "lead_in_id": "c1"}')
REPLY_3 = ('{"para_no": 3, "quote": "I started working for POL on 2nd April 1979, after 6-week counter training course I '
           'commenced working on the counter at Wigan Post Office. I performed this duty for 12 months and then became a '
           'TV license evasion officer.", "lead_in_id": "n1"}')
HITS = [{"para_no": 3, "page": 1, "score": 0.8, "text": ROPER_3}, {"para_no": 1, "page": 1, "score": 0.7, "text": ROPER_1}]


@pytest.fixture
def settings(monkeypatch):
    s = SimpleNamespace(prompts_dir=PROJECT_ROOT / "prompts", witness_answer_prompt="witness_answer.txt",
                        lead_ins_file="lead_ins.json", quote_select_prompt="quote_select.txt", quote_max_words=60,
                        relevance_check_enabled=False, ocr_verify_enabled=True, ocr_verify_max_edit_ratio=0.25,
                        ocr_verify_max_edits=2, ocr_verify_max_merge=3, ocr_verify_min_exact_fraction=0.7,
                        ocr_verify_confusable="il1,o0", resolve=lambda p: p)
    monkeypatch.setattr(A, "get_settings", lambda: s)
    monkeypatch.setattr(ocr_match, "get_settings", lambda: s)
    return s


def ask(monkeypatch, reply, hits=HITS):
    monkeypatch.setattr(A, "chat", lambda messages: reply)
    retrieval = Retrieval(query="q", ranking=hits, hits=hits, gate_score=0.8, top_dense_score=0.8)
    return A.answer("q", SimpleNamespace(passes_gate=lambda r: True), "Jeffrey Roper", retrieval=retrieval)


def test_roper_1_lama_is_read_as_i_am_a(settings, monkeypatch):
    r = ask(monkeypatch, REPLY_1)
    assert (r["ocr_corrected"], r["verified"], r["referral"], r["para_no"]) == (True, False, False, 1)
    assert r["spoken"].endswith("I am a former employee of Post Office Ltd and held the position of Field Support Advisor.")
    # "I", "am" and "a" are all highlighted on the original "lama".
    assert [ROPER_1[s:e] for _, s, e in r["ocr_spans"][:4]] == ["lama", "lama", "lama", "former"]


def test_roper_3_2nd_for_ocr_2_quote(settings, monkeypatch):
    r = ask(monkeypatch, REPLY_3)
    assert (r["ocr_corrected"], r["verified"], r["para_no"]) == (True, False, 3)
    assert "2nd April 1979" in r["spoken"]
    assert ROPER_3[slice(*r["ocr_spans"][6][1:])] == '2"'


def test_spoken_words_highlight_the_original_text(settings, monkeypatch):
    r = ask(monkeypatch, REPLY_1)
    words = [{"word": w, "start": i * 100, "end": i * 100 + 90} for i, w in enumerate("I am a former employee".split())]
    spans = [w["span"] for w in align_words(words, r["quote"], ROPER_1, 0, r["ocr_spans"])]
    assert [ROPER_1[s:e] for s, e in spans] == ["lama", "lama", "lama", "former", "employee"]


@pytest.mark.parametrize("quote", [
    "I began working for POL on 2nd April 1979, after a 6-week counter training course.",   # reworded
    "I started working happily for POL on 2nd April 1979",                                  # one invented word
    "I started working for POL on 2nd April 1978",                                          # a changed number
    "I am not a former employee of Post Office Ltd",                                        # inserted "not"
])
def test_reworded_or_invented_quotes_still_fail(settings, monkeypatch, quote):
    r = ask(monkeypatch, f'{{"para_no": 3, "quote": "{quote}", "lead_in_id": "n1"}}')
    assert r["referral"] is True and r["ocr_corrected"] is False and r["quote"] is None


def test_exact_quotes_are_verified_not_ocr_corrected(settings, monkeypatch):
    r = ask(monkeypatch, '{"para_no": 3, "quote": "I performed this duty for 12 months", "lead_in_id": "n1"}')
    assert (r["verified"], r["ocr_corrected"], r["ocr_spans"]) == (True, False, None)


def test_clean_words_take_only_confusable_substitutions(settings):
    # "awake" is a clean word: a model's "aware" is not an OCR correction of it.
    assert ocr_match.ocr_align("I was aware of it", "I was awake of it") is None
    assert ocr_match.ocr_align("she contacted me on LinkedIn about the role", "she contacted me on Linkedln about the role")  # l for I
    assert ocr_match.ocr_align("he told me that it was in hand", "he to►d me that it was in hand")  # damaged word
    # Short quotes need mostly exact words: 2 of 3 is below ocr_verify_min_exact_fraction.
    assert ocr_match.ocr_align("I contacted LinkedIn", "I contacted Linkedln") is None


def test_disabled_falls_back_to_referral(settings, monkeypatch):
    settings.ocr_verify_enabled = False
    assert ask(monkeypatch, REPLY_1)["referral"] is True

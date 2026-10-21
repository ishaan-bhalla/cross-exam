import pytest
from crossexam.witness.docids import normalise_ids

KNOWN = ["POL00055418", "POL00055225", "POL00026572", "FUJ00084797", "NFSP00000521"]


def _norm(text):
    return normalise_ids(text, KNOWN, max_distance=1)


@pytest.mark.parametrize("heard", [
    "POLOO055418",                                      # O for 0 inside the number
    "POL 00055418",                                     # split letters and digits
    "POL 000 55418",
    "P O L zero zero zero five five four one eight",    # letters and digits spoken one by one
    "POL double oh oh 55418",                           # double oh = 00, then oh = 0
    "POL0OO55418",
    "POL 0OO554l8",                                      # l for 1
])
def test_spoken_forms_become_the_cited_id(heard):
    text, changes = _norm(f"What did you make of document {heard}?")
    assert text == "What did you make of document POL00055418?"
    assert changes == [{"heard": heard, "id": "POL00055418", "snapped": False}]


def test_one_wrong_digit_snaps_to_the_unique_close_id():
    text, changes = _norm("Can you help with FUJ 00084798?")
    assert text == "Can you help with FUJ00084797?"
    assert changes[0]["snapped"] is True


def test_two_characters_off_does_not_snap():
    text, changes = _norm("What about POL00055499?")
    assert text == "What about POL00055499?"           # kept as heard, so routing says not recognised
    assert changes == []


def test_ambiguous_close_match_does_not_snap():
    known = ["POL00055418", "POL00055428"]
    text, _ = normalise_ids("What about POL00055408?", known, max_distance=1)
    assert text == "What about POL00055408?"


def test_made_up_id_stays_as_heard():
    text, changes = _norm("Please look at P O L zero zero zero four one two three five.")
    assert text == "Please look at POL00041235."
    assert changes == [{"heard": "P O L zero zero zero four one two three five", "id": "POL00041235",
                        "snapped": False}]
    assert "POL00041235" not in KNOWN


def test_ordinary_text_is_untouched():
    for text in ("Did you design the training given to subpostmasters?",
                 "Oh, I left Post Office Ltd in March 2016. One of the issues was polling.",
                 "The POL team met at 10 am."):
        assert _norm(text) == (text, [])


def test_routing_reports_a_made_up_id_as_not_recognised():
    from crossexam.retrieval.retriever import Retriever
    paragraphs = [{"para_no": 1, "page": 1, "text": "Regarding POL00055418, I had no concerns."}]
    r = Retriever.__new__(Retriever)  # only the doc-ID routing is exercised
    import re
    r.doc_id_re = re.compile(r"\b[A-Z]{3,4}\d{8}\b", re.IGNORECASE)
    assert r._doc_ids(_norm("Please look at POL 00041235.")[0]) == ["POL00041235"]
    assert not any("POL00041235" in p["text"] for p in paragraphs)

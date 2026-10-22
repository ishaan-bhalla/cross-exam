"""Statement parser on real witness statements (eval/parser_statements.json, plus Ismay).

The PDFs live under data/, which is not in the repo, so each test skips if its PDF is
missing. Expected values were checked by hand against each statement's text: the unit
count (paragraphs and sub-units), the first and last unit IDs, and two spot-checked units.
"""
import json
import pytest
from crossexam.config import PROJECT_ROOT

CASES = [
    # file, units, first, last, [(unit id, text it must contain), ...]
    ("witn09180100.pdf", 81, 1, 73,
     [(13, "Occasionally if the Investigation Team"), (41, "any information the auditors had access to")]),
    ("WITN05290200 - David Smith - Second Witness Statement.pdf", 45, 1, 45,
     [(1, "This statement is in response to the Rule 9 request"), (43, "would have done differently")]),
    ("witn06700100.pdf", 282, 1, 162,
     [("2.6.1", "The Agents Application Process Team"), (4, "Contract Advisors were responsible for appointing")]),
    ("witn10660100_0.pdf", 111, 1, 73,
     [("6.1", "graduated from Nottingham University"), (7, "In the autumn of 2002")]),
    ("witn09380100_0.pdf", 139, 1, 139,
     [(4, "Between 1980 and 1990 I served in the Army"), (10, "I was the second officer")]),
    ("witn08420100.pdf", 270, 1, 255,
     [("7(a)", "Part 1: Background"), (255, "I have always thought of my professional role")]),
    ("witn09830100.pdf", 294, 1, 294,
     [(1, "I am currently employed by Post Office Limited"), (294, "I have been asked to reflect on my time at POL")]),
    ("witn09910100_1.pdf", 109, 1, 109,
     [(11, "I also provided a support function"), (109, "Other than the matters addressed within this statement")]),
    ("WITN03510100 Sir Geoffrey Mulgan - Witness staement.pdf", 11, 1, 11,
     [(4, "The position was a mess"), (11, "I had no one to one meetings with Fujitsu")]),
    ("witn08540100.pdf", 32, 1, 32,
     [(11, "I would have discussed the audit report"), (32, "There are no other matters")]),
]


@pytest.fixture(scope="module")
def settings():
    from crossexam.config import load_settings
    try:
        return load_settings()
    except SystemExit as e:
        pytest.skip(f"settings not configured: {e}")


def _load(path):
    from crossexam.statements.parser import load_statement
    if not path.exists():
        pytest.skip(f"{path.name} not present (data/ is not in the repo)")
    return load_statement(path)


def test_test_set_matches_the_cases():
    listed = [s["file"] for s in json.loads((PROJECT_ROOT / "eval/parser_statements.json").read_text())["statements"]]
    assert listed == [c[0] for c in CASES]
    assert not any(f.lower().startswith("witn0463") for f in listed)  # Ismay is tested separately


@pytest.mark.parametrize("file, count, first, last, spots", CASES, ids=[c[0][:20] for c in CASES])
def test_statement(settings, file, count, first, last, spots):
    units = _load(settings.resolve(settings.statements_dir) / file)
    ids = [u["para_no"] for u in units]
    assert len(units) == count
    assert ids[0] == first and ids[-1] == last
    assert len(ids) == len(set(ids)), "duplicate unit IDs"
    by_id = {u["para_no"]: u for u in units}
    for unit_id, text in spots:
        assert unit_id in by_id, f"unit {unit_id} missing"
        assert text in by_id[unit_id]["text"], f"unit {unit_id}: {by_id[unit_id]['text'][:120]!r}"
    tops = [i for i in ids if isinstance(i, int)]
    assert tops == list(range(1, last + 1)), "top-level paragraphs are not 1..last without gaps"


def test_ismay_still_78_paragraphs_with_69_recovered(settings):
    witness = json.loads(settings.resolve(settings.witnesses_file).read_text())[0]
    units = _load(settings.resolve(settings.data_dir) / witness["statement_pdf"])
    assert [u["para_no"] for u in units] == list(range(1, 79))
    assert "been asked to comment on the Second Sight report" in units[68]["text"]
    assert units[16]["text"] == "I left Post Office Ltd in March 2016."


def test_unit_key_normalises_ids_from_llm_replies():
    from crossexam.statements.parser import unit_key
    assert unit_key(12) == 12 and unit_key("12") == 12 and unit_key(12.0) == 12
    assert unit_key(12.1) == "12.1" and unit_key("12.1") == "12.1" and unit_key(" 7(a) ") == "7(a)"
    assert unit_key("¶ 5") == 5 and unit_key(None) is None and unit_key(True) is None

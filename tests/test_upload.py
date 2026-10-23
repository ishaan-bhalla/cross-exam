"""Statement upload: POST /api/witnesses/upload, progress polling, profile verification.

The end-to-end tests need the statement PDFs (data/ is not in the repo; they skip without
them) and call the LLM gateway for the draft profile.
"""
import time
from types import SimpleNamespace
import pytest
from crossexam.config import PROJECT_ROOT


@pytest.fixture(scope="module")
def settings():
    from crossexam.config import load_settings
    try:
        return load_settings()
    except SystemExit as e:
        pytest.skip(f"settings not configured: {e}")


@pytest.fixture(scope="module")
def client(settings):
    from fastapi.testclient import TestClient
    from crossexam.api.app import app
    return TestClient(app)  # no lifespan: no TTS warm-up needed for uploads


# ------------------------------------------------------------------ offline

def test_verify_profile_keeps_only_fields_found_in_the_statement():
    from crossexam.witness.profile import role_label, verify_profile
    text = ("I, Roderick Mark Ismay will say as follows: I joined Post Office Ltd in September 2003 as Head of "
            "Risk and Control. I was asked to take over the role of Head of Product & Branch Accounting in June 2006.")
    draft = {"name": "Roderick Mark Ismay", "organisation": "Post Office Ltd",
             "roles": [{"title": "Head of Product & Branch Accounting", "organisation": "Post Office Ltd",
                        "from": "June 2006", "to": "March 2016"},            # "March 2016" is not in the text
                       {"title": "Chief Executive", "organisation": "Post Office Ltd", "from": "2010"}]}  # invented
    p = verify_profile(draft, text)
    assert p["name"] == "Roderick Mark Ismay" and p["organisation"] == "Post Office Ltd"
    assert p["roles"] == [{"title": "Head of Product & Branch Accounting", "organisation": "Post Office Ltd",
                           "from": "June 2006", "to": None}]
    assert {"field": "roles[0].to", "value": "March 2016"} in p["dropped"]
    assert {"field": "roles[1].title", "value": "Chief Executive"} in p["dropped"]
    assert role_label(p) == "Head of Product & Branch Accounting, Post Office Ltd (June 2006)"


def test_roles_must_appear_in_the_paragraphs_and_be_complete():
    from crossexam.witness.profile import verify_profile
    preamble = "Witness Name: David Smith POST OFFICE HORIZON IT INQUIRY SECOND WITNESS STATEMENT OF DAVID SMITH"
    body = "This statement is in response to the Rule 9 request. I was Head of the IT team at the Post Office."
    draft = {"name": "David Smith", "organisation": "POST OFFICE HORIZON",
             "roles": [{"title": "Head of", "organisation": "POST OFFICE HORIZON"}]}
    p = verify_profile(draft, preamble + " " + body, body)
    assert p["name"] == "David Smith"                 # from the title page
    assert p["organisation"] is None and p["roles"] == []
    assert {"field": "roles[0].title", "value": "Head of"} in p["dropped"]


def test_unverified_name_is_dropped():
    from crossexam.witness.profile import verify_profile
    assert verify_profile({"name": "Jane Doe", "roles": []}, "I, John Smith, will say")["name"] is None


def test_sub_units_are_embedded_with_section_and_parent_lead_in(monkeypatch):
    from crossexam.retrieval import dense
    monkeypatch.setattr(dense, "get_settings", lambda: SimpleNamespace(subunit_context_enabled=True,
                                                                         subunit_context_max_words=6))
    units = [{"para_no": 7, "text": "Given the issues raised, this statement is in the following parts:", "section": "Introduction"},
             {"para_no": "7(a)", "text": "Part 1: Background", "section": "Introduction"},
             {"para_no": "2.6.1", "text": "The Agents Application Process Team", "section": None},
             {"para_no": 2, "text": "I was employed in a number of roles", "section": None}]
    texts = dense.embedding_texts(units)
    assert texts[0] == units[0]["text"]                                   # paragraphs unchanged
    # 6-word lead-in from the parent, after the section heading
    assert texts[1] == "Introduction. Given the issues raised, this statement Part 1: Background"
    assert texts[2] == "I was employed in a number The Agents Application Process Team"  # 2.6 missing: uses 2


def test_acronyms_and_cited_ids_become_key_terms(monkeypatch):
    from crossexam.statements import keyterms as K
    monkeypatch.setattr(K, "get_settings", lambda: SimpleNamespace(
        doc_id_pattern=r"\b[A-Z]{3,4}\d{8}\b", keyterms_min_acronym_count=2, stt_max_keyterms=100))
    units = [{"text": "POL and the NFSP met. See POL00055418. The NBSC helped SPMs."},
             {"text": "POL asked the NFSP about SPMs. P&BA and P&BA again. Part II of III."}]
    assert K.statement_keyterms(units) == ["POL", "NFSP", "SPM", "P&BA", "POL00055418"]


def test_non_pdf_and_oversized_uploads_are_rejected(client, settings, monkeypatch):
    r = client.post("/api/witnesses/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 415
    monkeypatch.setattr(settings.__class__, "upload_max_mb", 0, raising=False)
    from crossexam.config import get_settings
    monkeypatch.setattr(get_settings(), "upload_max_mb", 0)
    r = client.post("/api/witnesses/upload", files={"file": ("s.pdf", b"%PDF-1.7 tiny", "application/pdf")})
    assert r.status_code == 413


# ------------------------------------------------------------------ end to end

def _data_snapshot(settings):
    root = settings.resolve(settings.data_dir)
    return {str(p): p.stat().st_mtime for p in root.rglob("*") if p.is_file() and "models" not in p.parts}


def _upload_and_wait(client, path, timeout=300):
    r = client.post("/api/witnesses/upload", files={"file": (path.name, path.read_bytes(), "application/pdf")})
    assert r.status_code == 202, r.text
    record, stages = r.json(), []
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(record["status_url"]).json()
        if not stages or stages[-1] != status["stage"]:
            stages.append(status["stage"])
        if status["stage"] in ("ready", "error"):
            return status, stages
        time.sleep(0.2)
    pytest.fail(f"upload not ready after {timeout}s: {status}")


def _check_profile_against_statement(client, status):
    from crossexam.witness.profile import _norm, appears_in
    units = client.get(f"/api/witnesses/{status['witness_id']}/statement").json()
    hay = _norm(" ".join(u["text"] for u in units))
    p = status["profile"]
    fields = [p["name"], p["organisation"]] + [r.get(f) for r in p["roles"] for f in ("title", "organisation", "from", "to")]
    # The name may come from the preamble (title page), which is not a unit; check the rest here.
    for value in [f for f in fields[1:] if f]:
        assert appears_in(value, hay), f"profile field {value!r} not in the statement"
    return units


@pytest.mark.parametrize("file, units, needs_ocr", [
    ("WITN03510100 Sir Geoffrey Mulgan - Witness staement.pdf", 11, False),
    ("witn09180100.pdf", 81, True),
])
def test_upload_end_to_end(client, settings, file, units, needs_ocr):
    path = settings.resolve(settings.statements_dir) / file
    if not path.exists():
        pytest.skip(f"{file} not present (data/ is not in the repo)")
    before = _data_snapshot(settings)
    status, stages = _upload_and_wait(client, path)
    assert status["stage"] == "ready", status
    assert status["units"] == units
    assert stages[-1] == "ready" and "indexing" in stages and "profiling" in stages
    if needs_ocr:
        assert status["ocr_pages"], "expected OCR on some pages"
    got = _check_profile_against_statement(client, status)
    assert len(got) == units
    assert status["witness_id"] in [w["id"] for w in client.get("/api/witnesses").json()]
    assert _data_snapshot(settings) == before, "an upload wrote to data/"

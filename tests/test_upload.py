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


# ------------------------------------------------------------------ limits, expiry, confirm (no LLM or TTS)

@pytest.fixture
def fake_processing(monkeypatch, settings):
    """Uploads that are ready at once with a one-unit statement, and a voice that warms instantly."""
    from crossexam.api import phrases, uploads, witnesses

    def process(upload_id, filename, data, visitor=None):
        units = [{"para_no": 1, "page": 1, "text": "I was Head of Audit at Post Office Ltd.", "section": None}]
        witnesses.register_upload({"id": upload_id, "name": "JANE DOE", "role_label": "Head of Audit",
                                   "voice": "charles", "uploaded": True, "profile_confirmed": False,
                                   "visitor": visitor}, units, retriever=None, terms=[])
        uploads._update(upload_id, stage="ready", preamble="Witness Name: JANE DOE",
                        profile={"name": "JANE DOE", "roles": [], "dropped": [], "role_label": "Head of Audit"})

    async def warm(voice, texts):
        return {}
    monkeypatch.setattr(uploads, "_uploads", {})  # no uploads left open by earlier tests
    monkeypatch.setattr(uploads, "_process", process)
    monkeypatch.setattr(phrases, "warm", warm)
    monkeypatch.setattr(uploads, "_executor", lambda: SimpleNamespace(submit=lambda fn, *a: fn(*a)))
    return uploads


def _new_client():
    from fastapi.testclient import TestClient
    from crossexam.api.app import app
    return TestClient(app)


def _pdf_upload(c):
    return c.post("/api/witnesses/upload", files={"file": ("s.pdf", b"%PDF-1.7 fake", "application/pdf")})


def test_each_visitor_may_have_uploads_per_client_open(fake_processing, settings, monkeypatch):
    from crossexam.config import get_settings
    monkeypatch.setattr(get_settings(), "uploads_per_client", 2)
    a, b = _new_client(), _new_client()
    assert [_pdf_upload(a).status_code for _ in range(3)] == [202, 202, 429]
    assert _pdf_upload(b).status_code == 202                       # another visitor has their own limit
    first = a.get("/api/uploads").json()[-1]["id"]
    assert b.get(f"/api/uploads/{first}").status_code == 404       # and cannot see a's uploads
    assert b.get(f"/api/witnesses/{first}/statement").status_code == 404
    assert first not in [w["id"] for w in b.get("/api/witnesses").json()]
    assert a.delete(f"/api/uploads/{first}").status_code == 204    # removing one frees a place
    assert _pdf_upload(a).status_code == 202


def test_unused_upload_expires(fake_processing, settings, monkeypatch):
    import time as _time
    c = _new_client()
    upload_id = _pdf_upload(c).json()["id"]
    assert c.get(f"/api/witnesses/{upload_id}/statement").status_code == 200
    fake_processing._uploads[upload_id]["last_active"] = _time.time() - settings.upload_ttl_minutes * 60 - 1
    r = c.get(f"/api/witnesses/{upload_id}/statement")
    assert r.status_code == 410 and r.json()["detail"] == "This upload has expired, please upload again."
    status = c.get(f"/api/uploads/{upload_id}").json()
    assert status["stage"] == "expired" and status["profile"] is None and status["preamble"] is None
    with c.websocket_connect(f"/ws/examine?witness={upload_id}") as ws:
        assert ws.receive_json() == {"type": "error", "message": "This upload has expired, please upload again.", "status": 410}


def test_examination_waits_for_confirmation(fake_processing, settings):
    c = _new_client()
    upload_id = _pdf_upload(c).json()["id"]
    with c.websocket_connect(f"/ws/examine?witness={upload_id}") as ws:
        assert ws.receive_json()["status"] == 409
    body = {"name": "Jane Doe", "role_label": "Head of Audit, Post Office Ltd", "voice": "anna",
            "roles": [{"title": "Head of Audit", "organisation": "Post Office Ltd", "from": None, "to": None}]}
    assert c.post(f"/api/witnesses/{upload_id}/confirm", json={**body, "voice": "nobody"}).status_code == 422
    assert c.post(f"/api/witnesses/{upload_id}/confirm", json=body).status_code == 202
    assert c.get(f"/api/uploads/{upload_id}").json()["stage"] == "confirmed"
    w = next(w for w in c.get("/api/witnesses").json() if w["id"] == upload_id)
    assert (w["name"], w["role_label"], w["voice"], w["profile_confirmed"]) == ("Jane Doe", body["role_label"], "anna", True)


def test_global_cap_on_open_uploads_across_visitors(fake_processing, settings, monkeypatch):
    from crossexam.config import get_settings
    monkeypatch.setattr(get_settings(), "max_open_uploads", 2)
    a, b = _new_client(), _new_client()
    assert _pdf_upload(a).status_code == 202
    assert _pdf_upload(b).status_code == 202
    r = _pdf_upload(b)                                   # a third, from either visitor
    assert r.status_code == 503 and r.json()["detail"] == "The demo is busy, please try again in a few minutes."
    a.delete(f"/api/uploads/{a.get('/api/uploads').json()[0]['id']}")
    assert _pdf_upload(b).status_code == 202             # a place freed up


def test_healthz_sets_no_cookie_and_counts_towards_nothing(settings):
    from crossexam import budget
    c = _new_client()
    before = budget.status()["used"]
    for _ in range(20):
        r = c.get("/healthz")
        assert r.status_code == 200 and "set-cookie" not in r.headers
    assert budget.status()["used"] == before

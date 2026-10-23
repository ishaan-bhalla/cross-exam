"""Each witness's conversation is kept on the server per visitor: through the examination
WebSocket, with the LLM and TTS stubbed out."""
from types import SimpleNamespace
import pytest


@pytest.fixture
def app_module(monkeypatch):
    from crossexam.config import load_settings
    try:
        load_settings()
    except SystemExit as e:
        pytest.skip(f"settings not configured: {e}")
    from crossexam.api import app as appmod, phrases, uploads, witnesses

    def fake_answer(question, retriever, name):
        return {"para_no": 3, "quote": f"Quote for {question}", "lead_in": "", "spoken": f"Quote for {question}",
                "verified": True, "referral": False, "ocr_corrected": False, "ocr_spans": None, "routed": None}

    spoken = []

    async def fake_speak(ws, witness, result, tts_conn, received, answer_latency_ms):
        spoken.append((witness["id"], result["quote"]))
        done = {"type": "done", "answer_latency_ms": round(answer_latency_ms), "duration_ms": 1200}
        await ws.send_json(done)
        return done

    class FakeTTS:
        opens = 0
        def __init__(self, voice): pass
        async def ensure(self): return self
        async def close(self): pass

    def process(upload_id, filename, data, visitor=None):
        units = [{"para_no": 3, "page": 1, "text": "I was Head of Audit.", "section": None}]
        witnesses.register_upload({"id": upload_id, "name": "Jane Doe", "role_label": "Head of Audit", "voice": "charles",
                                   "uploaded": True, "profile_confirmed": False, "visitor": visitor}, units, None, [])
        uploads._update(upload_id, stage="ready", preamble="", profile={"name": "Jane Doe", "roles": [], "dropped": []})

    async def warm(voice, texts):
        return {}
    monkeypatch.setattr(appmod, "answer", fake_answer)
    monkeypatch.setattr(appmod, "_speak", fake_speak)
    monkeypatch.setattr(appmod.tts, "PersistentSession", FakeTTS)
    monkeypatch.setattr(uploads, "_process", process)
    monkeypatch.setattr(phrases, "warm", warm)
    monkeypatch.setattr(uploads, "_executor", lambda: SimpleNamespace(submit=lambda fn, *a: fn(*a)))
    appmod.spoken = spoken
    return appmod


def _client(appmod):
    from fastapi.testclient import TestClient
    c = TestClient(appmod.app)
    c.get("/api/config")  # sets the visitor cookie, as loading the page does
    return c


def _confirmed_upload(c):
    upload_id = c.post("/api/witnesses/upload", files={"file": ("s.pdf", b"%PDF-1.7 x", "application/pdf")}).json()["id"]
    c.post(f"/api/witnesses/{upload_id}/confirm", json={"name": "Jane Doe", "role_label": "Head of Audit", "voice": "charles"})
    return upload_id


def _ask(c, witness, questions, spoken=False):
    with c.websocket_connect(f"/ws/examine?witness={witness}") as ws:
        ids = []
        for q in questions:
            ws.send_json({"type": "question", "text": q, "spoken": spoken,
                          "voice_timing": {"end_of_speech_to_submit_ms": 640} if spoken else None})
            types = []
            while (m := ws.receive_json())["type"] != "turn":
                types.append(m["type"])
            assert types[-2:] == ["answer", "done"] or types[-1] == "done"
            ids.append(m["turn"])
        return ids


def test_two_questions_survive_switching_witness_and_back(app_module):
    c = _client(app_module)
    other = _confirmed_upload(c)
    _ask(c, "ismay", ["When did you join?", "What was P&BA?"])
    _ask(c, other, ["Where did you work?"], spoken=True)           # go to another witness...
    turns = c.get("/api/witnesses/ismay/conversation").json()       # ...and back
    assert [t["question"] for t in turns] == ["When did you join?", "What was P&BA?"]
    assert [t["answer"]["quote"] for t in turns] == ["Quote for When did you join?", "Quote for What was P&BA?"]
    assert turns[0]["asked"] == "typed" and turns[0]["answer"]["para_no"] == 3 and turns[0]["timing"]["duration_ms"] == 1200
    voice = c.get(f"/api/witnesses/{other}/conversation").json()
    assert voice[0]["asked"] == "voice" and voice[0]["timing"]["voice"] == {"end_of_speech_to_submit_ms": 640}
    counts = {w["id"]: w["questions"] for w in c.get("/api/witnesses").json()}
    assert counts["ismay"] == 2 and counts[other] == 1


def test_conversations_are_per_visitor(app_module):
    a, b = _client(app_module), _client(app_module)
    _ask(a, "ismay", ["Only mine?"])
    assert [t["question"] for t in a.get("/api/witnesses/ismay/conversation").json()] == ["Only mine?"]
    assert b.get("/api/witnesses/ismay/conversation").json() == []


def test_replay_speaks_the_stored_answer_without_asking_again(app_module):
    c = _client(app_module)
    [turn_id] = _ask(c, "ismay", ["Why did you write the report?"])
    app_module.spoken.clear()
    with c.websocket_connect("/ws/examine?witness=ismay") as ws:
        ws.send_json({"type": "replay", "turn": turn_id})
        first = ws.receive_json()
        assert first["type"] == "replay" and first["turn"] == turn_id and first["quote"] == "Quote for Why did you write the report?"
        assert ws.receive_json()["type"] == "done"
    assert app_module.spoken == [("ismay", "Quote for Why did you write the report?")]
    assert len(c.get("/api/witnesses/ismay/conversation").json()) == 1    # a replay is not a new turn


def test_turns_past_the_cap_drop_the_oldest_and_clear_empties(app_module, monkeypatch):
    from crossexam.config import get_settings
    monkeypatch.setattr(get_settings(), "conversation_max_turns", 2)
    c = _client(app_module)
    _ask(c, "ismay", ["one", "two", "three"])
    assert [t["question"] for t in c.get("/api/witnesses/ismay/conversation").json()] == ["two", "three"]
    assert c.delete("/api/witnesses/ismay/conversation").status_code == 204
    assert c.get("/api/witnesses/ismay/conversation").json() == []


def test_removing_an_upload_drops_its_conversation(app_module):
    c = _client(app_module)
    other = _confirmed_upload(c)
    _ask(c, other, ["Anything?"])
    c.delete(f"/api/uploads/{other}")
    from crossexam.api import conversations
    assert conversations.count(c.cookies.get("cx_visitor"), other) == 0

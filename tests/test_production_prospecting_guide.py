from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app import main, portal_guide
from app.auth import Identity, require_identity
from app.onboarding import OnboardingSource
from app.prospecting_guide import TargetingGuide


OWNER = "owner@example.test"
MEMBER = "member@example.test"
OTHER = "other@example.test"
RECORD = "ONB-GUIDE-TEST"


class TestSheets:
    __test__ = False

    def __init__(self):
        self.properties = []
        self.rows = []
        self.updates = []
        self.saved = []
        self.source = OnboardingSource.from_sheet_record({
            "ID registro": RECORD, "Empresa": "Estudio de prueba", "Web": "https://studio.example.test",
            "Email responsable": OWNER, "Servicio prioritario": "Podcast", "Sectores": "Tecnología",
            "Países objetivo": "España", "Autorización": "Sí", "Público": "Empresas privadas",
        })

    def _sheet_properties(self):
        return self.properties

    def _get(self, name):
        assert name == "'Prospeccion Guiada'!A1:A2000"
        return self.rows

    def _batch_update(self, requests):
        self.updates.extend(requests)
        for request in requests:
            if "addSheet" in request:
                self.properties = [{"title": request["addSheet"]["properties"]["title"], "sheetId": 7}]
            else:
                update = request["updateCells"]
                assert update["range"]["sheetId"] == 7
                self.rows = [[row["values"][0]["userEnteredValue"]["stringValue"]] for row in update["rows"]]

    def get_access(self, email):
        return SimpleNamespace(email=email, account_email=OWNER if email == MEMBER else email, state="Activo", role="Cliente")

    def get_onboarding_source(self, tenant, owner):
        return self.source if (tenant, owner) == (RECORD, OWNER) else None

    def recent_prospects(self, owner, limit):
        assert owner == OWNER
        return []

    def upsert_saved_search(self, record, owner, **payload):
        assert (record, owner) == (RECORD, OWNER)
        self.saved.append(payload)
        return {"saved_search_id": "guide-test"}


def fake_reasoner(context):
    return {
        "turn": {"summary": "Propuesta de prueba.", "question": "¿Qué servicio quieres ofrecer?",
                 "options": ["Podcast", "Vídeo"] if context["question_number"] else [],
                 "criteria": context["criteria"], "assumptions": []},
        "sources": [], "web_status": "not_requested",
    }


def test_guide_preserves_original_sheets_and_isolates_authenticated_answers(monkeypatch):
    sheets = TestSheets()
    service = TargetingGuide(portal_guide.GuideStore(sheets), fake_reasoner)
    actor = OWNER

    monkeypatch.setattr(portal_guide, "get_settings", lambda: SimpleNamespace(google_sheets_enabled=True))
    monkeypatch.setattr(portal_guide, "SheetStore", lambda _: sheets)
    monkeypatch.setattr(portal_guide, "_load_service", lambda: service)
    monkeypatch.setitem(main.app.dependency_overrides, require_identity, lambda: Identity(actor, "Cliente", "test"))
    client = TestClient(main.app)
    client.cookies.set("focus_csrf", "test-csrf")
    headers = {"X-CSRF-Token": "test-csrf"}
    url = f"/api/intelligence/{RECORD}/guide"
    state = client.get(url).raise_for_status().json()
    assert state["session"] is None and state["ai_ready"]
    assert sheets.updates == []

    assert client.post(url + "/sessions", json={"request_id": str(uuid4())}).status_code == 403
    session = client.post(url + "/sessions", headers=headers,
                          json={"request_id": str(uuid4())}).raise_for_status().json()
    sid = f"{url}/sessions/{session['id']}"
    for _ in range(10):
        session = client.post(sid + "/answers", headers=headers, json={
            "version": session["version"], "request_id": str(uuid4()),
            "answer": "Podcast", "mode": "option",
        }).raise_for_status().json()
    assert session["state"] == "checkpoint"
    assert len(session["history"]) == 10
    assert not sheets.saved

    actor = OTHER
    assert client.get(url).status_code == 404
    actor = MEMBER
    assert client.get(url).raise_for_status().json()["session"] is None
    assert client.get(sid).status_code == 409
    actor = OWNER
    service = TargetingGuide(portal_guide.GuideStore(sheets), fake_reasoner)
    assert client.get(sid).raise_for_status().json()["history"] == session["history"]
    session = client.post(sid + "/checkpoint", headers=headers,
                          json={"version": session["version"], "choice": "review"}).raise_for_status().json()
    client.post(sid + "/apply", headers=headers,
                json={"version": session["version"], "confirmed": True}).raise_for_status()
    assert len(sheets.saved) == 1
    assert sheets.saved[0]["created_by_email"] == OWNER
    assert sheets.properties == [{"title": "Prospeccion Guiada", "sheetId": 7}]

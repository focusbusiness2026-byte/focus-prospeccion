from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE, Identity, create_session
from app.main import _csv_cell, _demo_payload, app


def test_demo_dashboard_exercises_complete_portal_shape():
    payload = _demo_payload(Identity("demo@focus.local", "Administrador", "demo"), 500)

    assert payload["demo"] is True
    assert payload["metrics"]["total"] == 2
    assert len(payload["prospects"]) == 2
    assert len(payload["executions"]) == 2
    assert payload["prospects"][0]["evidence"]
    assert payload["global"]["gemini_requests_remaining"] == 498


def test_demo_csv_export_is_downloadable_and_formula_safe():
    identity = Identity("demo@focus.local", "Administrador", "demo")
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, create_session(identity))

    response = client.get("/api/prospects/export.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment; filename=focus-prospeccion-leads.csv"
    assert "Estudio Horizonte" in response.text
    assert _csv_cell("=HYPERLINK(\"https://bad.example\")").startswith("'=")

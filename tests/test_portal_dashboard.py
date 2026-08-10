from fastapi.testclient import TestClient
from pathlib import Path

from app.auth import SESSION_COOKIE, Identity, create_session
import pytest
from pydantic import ValidationError

from app.main import ResearchAdjustments, _csv_cell, _demo_payload, app


def test_demo_dashboard_exercises_complete_portal_shape():
    payload = _demo_payload(Identity("demo@focus.local", "Administrador", "demo"), 500)

    assert payload["demo"] is True
    assert payload["metrics"]["total"] == 2
    assert len(payload["prospects"]) == 2
    assert len(payload["executions"]) == 2
    assert payload["source_metrics"] == {"total": 1, "ready": 1, "blocked": 0}
    assert payload["sources"][0]["onboarding_id"] == "ONB-DEMO0001"
    assert payload["prospects"][0]["evidence"]
    assert payload["global"]["openai_requests_remaining"] == 498
    assert payload["prospects"][0]["web_search_call_limit"] == 5
    assert payload["prospects"][0]["public_contacts"]


def test_demo_csv_export_is_downloadable_and_formula_safe():
    identity = Identity("demo@focus.local", "Administrador", "demo")
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, create_session(identity))

    response = client.get("/api/prospects/export.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment; filename=focus-prospeccion-leads.csv"
    assert "Estudio Horizonte" in response.text
    assert _csv_cell("=HYPERLINK(\"https://bad.example\")").startswith("'=")


def test_portal_uses_onboarding_sources_instead_of_manual_company_fields():
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, create_session(Identity("demo@focus.local", "Administrador", "demo")))

    response = client.get("/portal")

    assert response.status_code == 200
    assert 'id="source-list"' in response.text
    assert "La configuración recomendada se construye automáticamente con el Onboarding" in response.text
    assert "Máximo 5 búsquedas web públicas" in response.text
    assert 'name="openai_api_key"' not in response.text
    assert 'id="scrape-form"' not in response.text
    assert 'id="recommended-config"' in response.text
    assert 'data-number="lead_count"' in response.text
    assert "Firmografía avanzada" in response.text
    assert "Madurez comercial y digital" in response.text
    assert "Decisores y empresas similares" in response.text
    assert 'id="warmup"' in response.text
    assert "11-4-7" in response.text

    source_response = client.get("/api/onboarding-sources/ONB-DEMO0001")
    assert source_response.status_code == 200
    assert source_response.json()["source"]["productora"]["email"] == "demo@focus.local"


def test_internal_onboarding_trigger_is_closed_without_server_secret():
    client = TestClient(app)

    response = client.post("/api/internal/onboarding-trigger", json={"onboarding_id": "ONB-DEMO0001"})

    assert response.status_code == 503
    assert "no está configurado" in response.json()["detail"]


def test_professional_research_configuration_limits_requested_leads():
    assert ResearchAdjustments(lead_count=25).lead_count == 25
    with pytest.raises(ValidationError):
        ResearchAdjustments(lead_count=51)


def test_hidden_demo_badge_cannot_be_overridden_by_badge_display_rule():
    css = (Path(__file__).parents[1] / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert "[hidden] { display: none !important; }" in css

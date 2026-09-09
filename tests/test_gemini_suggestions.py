from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.gemini_suggestions as suggestions_module
from app.auth import CSRF_COOKIE, Identity, require_identity
from app.config import get_settings
from app.config import Settings
from app.sheet_store import AccessRecord


def _source(email="client@example.com"):
    return SimpleNamespace(
        record_id="ONB-CLIENT",
        email=email,
        company="Productora Cliente",
        activity="Audiovisual",
        location="Madrid",
        main_service="Producción",
        services=("Vídeo",),
        audience=("B2B",),
        sectors=("Tecnología",),
        markets=("España",),
        target_city="Madrid",
        target_region="Comunidad de Madrid",
        target_countries=("España",),
        target_client_types=("Empresa",),
        ideal_company_size="11-50",
        minimum_budget="",
        prospect_exclusions="",
        prospect_preferences="",
    )


class SuggestionStore:
    def __init__(self, settings=None):
        pass

    def get_access(self, email):
        return AccessRecord(2, email, "Cliente", "Activo", 50, 2, "2026-09-07")

    def get_onboarding_source(self, record_id, email=None):
        if record_id != "ONB-CLIENT" or email != "client@example.com":
            return None
        return _source()

    def recent_prospects(self, email, limit=1000):
        return [
            {
                "execution_id": "RUN-OK-1", "email": "client@example.com", "onboarding_id": "ONB-CLIENT",
                "company": "Empresa Uno", "sector": "Tecnología", "classification": "green",
                "website": "https://private.example", "contact_email": "secret@example.com",
            },
            {
                "execution_id": "RUN-SECOND-1", "email": "client@example.com", "onboarding_id": "ONB-CLIENT",
                "company": "Empresa Dos", "sector": "Salud", "classification": "yellow",
            },
            {
                "execution_id": "lead-other", "email": "client@example.com", "onboarding_id": "ONB-OTHER",
                "company": "Otra Productora", "sector": "Retail", "classification": "red",
            },
        ]

    def recent_executions(self, email, limit=1000):
        return [
            {
                "execution_id": "RUN-OK", "created_at": "2026-09-08T10:00:00Z",
                "email": "client@example.com", "onboarding_id": "ONB-CLIENT", "status": "Completado",
                "adjustments": {"target_city": "Madrid", "lead_count": 5},
                "error": "internal detail that must not be sent",
            },
            {
                "execution_id": "RUN-PENDING", "created_at": "2026-09-08T11:00:00Z",
                "email": "client@example.com", "onboarding_id": "ONB-CLIENT", "status": "Pendiente",
                "adjustments": {},
            },
            {
                "execution_id": "RUN-OTHER", "created_at": "2026-09-08T12:00:00Z",
                "email": "other@example.com", "onboarding_id": "ONB-OTHER", "status": "Completado",
                "adjustments": {},
            },
        ]


def _client(monkeypatch, *, gemini_key="fixture-only"):
    monkeypatch.setenv("GOOGLE_SHEETS_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)
    get_settings.cache_clear()
    monkeypatch.setattr(main_module, "SheetStore", SuggestionStore)
    main_module.app.dependency_overrides[require_identity] = lambda: Identity("client@example.com", "Cliente", "client")
    client = TestClient(main_module.app)
    client.cookies.set(CSRF_COOKIE, "csrf-test")
    return client


def test_gemini_suggestions_are_isolated_and_return_exactly_three(monkeypatch):
    captured = {}

    def fake_suggest(self, *, source_profile, leads):
        captured["source_profile"] = source_profile
        captured["leads"] = leads
        return [
            {"id": f"suggestion-{index}", "title": f"Mejora {index}", "reason": "Motivo", "adjustments": {"target_city": "Madrid"}}
            for index in range(1, 4)
        ]

    monkeypatch.setattr(main_module.GeminiCriteriaSuggestions, "suggest", fake_suggest)
    client = _client(monkeypatch)
    try:
        response = client.post(
            "/api/onboarding-sources/ONB-CLIENT/prospecting-improvements",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"adjustments": {"lead_count": 5}},
        )
        assert response.status_code == 200
        assert len(response.json()["suggestions"]) == 3
        assert response.json()["lead_count_analyzed"] == 2
        assert captured["leads"][0]["company"] == "Empresa Uno"
        assert captured["leads"][0]["sector"] == "Tecnología"
        assert captured["leads"][0]["classification"] == "green"
        assert "website" in captured["leads"][0]
        assert "linkedin" in captured["leads"][0]
        assert "email" not in captured["source_profile"]
        assert captured["source_profile"]["website"] == ""
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_suggestion_prompt_targets_recurrence_without_claiming_web_visits():
    source = Path("app/gemini_suggestions.py").read_text(encoding="utf-8")
    assert "relaciones comerciales" in source
    assert "B2B o B2C" in source
    assert "no afirmes haberlos visitado" in source


def test_gemini_suggestions_can_be_limited_to_one_completed_execution(monkeypatch):
    captured = {}

    def fake_suggest(self, *, source_profile, leads):
        captured["source_profile"] = source_profile
        captured["leads"] = leads
        return [
            {"id": f"suggestion-{index}", "title": f"Mejora {index}", "reason": "Motivo", "adjustments": {"target_city": "Madrid"}}
            for index in range(1, 4)
        ]

    monkeypatch.setattr(main_module.GeminiCriteriaSuggestions, "suggest", fake_suggest)
    client = _client(monkeypatch)
    try:
        response = client.post(
            "/api/onboarding-sources/ONB-CLIENT/prospecting-improvements",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"execution_id": "RUN-OK", "adjustments": {"lead_count": 5}},
        )
        assert response.status_code == 200
        assert response.json()["lead_count_analyzed"] == 1
        assert [lead["company"] for lead in captured["leads"]] == ["Empresa Uno"]
        assert captured["source_profile"]["selected_execution"] == {
            "execution_id": "RUN-OK",
            "created_at": "2026-09-08T10:00:00Z",
            "status": "Completado",
            "adjustments": {"target_city": "Madrid"},
        }
        assert "error" not in captured["source_profile"]["selected_execution"]
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_gemini_rejects_execution_outside_source_or_not_completed(monkeypatch):
    client = _client(monkeypatch)
    try:
        outside = client.post(
            "/api/onboarding-sources/ONB-CLIENT/prospecting-improvements",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"execution_id": "RUN-OTHER", "adjustments": {"lead_count": 5}},
        )
        pending = client.post(
            "/api/onboarding-sources/ONB-CLIENT/prospecting-improvements",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"execution_id": "RUN-PENDING", "adjustments": {"lead_count": 5}},
        )
        assert outside.status_code == 404
        assert pending.status_code == 422
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_gemini_is_not_called_when_server_key_is_missing(monkeypatch):
    called = False

    def fake_suggest(self, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main_module.GeminiCriteriaSuggestions, "suggest", fake_suggest)
    client = _client(monkeypatch, gemini_key="")
    try:
        response = client.post(
            "/api/onboarding-sources/ONB-CLIENT/prospecting-improvements",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"adjustments": {"lead_count": 5}},
        )
        assert response.status_code == 503
        assert called is False
        assert "servidor" in response.json()["detail"]
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_suggestion_provider_uses_production_timeout_and_stable_model_defaults():
    settings = Settings(_env_file=None)

    assert settings.gemini_timeout_seconds == 30.0
    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.gemini_fallback_model == "gemini-3.5-flash-lite"


def _provider_success_response(url: str) -> httpx.Response:
    payload = {
        "suggestions": [
            {"title": f"Mejora {index}", "reason": "Motivo", "adjustments": {"target_city": "Madrid"}}
            for index in range(1, 4)
        ]
    }
    return httpx.Response(
        200,
        request=httpx.Request("POST", url),
        json={"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(payload)}]}}]},
    )


def test_suggestion_provider_retries_transient_statuses_with_short_backoff(monkeypatch):
    calls = []
    sleeps = []
    request_bodies = []

    def fake_post(self, url, **kwargs):
        calls.append(url)
        request_bodies.append(kwargs["json"])
        if len(calls) == 1:
            return httpx.Response(429, request=httpx.Request("POST", url), json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        if len(calls) == 2:
            return httpx.Response(500, request=httpx.Request("POST", url), json={"error": {"status": "INTERNAL"}})
        return _provider_success_response(url)

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.setattr(suggestions_module.time, "sleep", sleeps.append)
    service = suggestions_module.GeminiCriteriaSuggestions(Settings(_env_file=None, gemini_api_key="fixture"))

    result = service.suggest(source_profile={"productora": "Demo"}, leads=[{"company": "Uno"}])

    assert len(result) == 3
    assert sleeps == [1.0, 2.0]
    assert len(calls) == 3
    assert all("gemini-3.5-flash:generateContent" in url for url in calls)
    adjustment_properties = request_bodies[0]["generationConfig"]["responseSchema"]["properties"]["suggestions"]["items"]["properties"]["adjustments"]["properties"]
    assert adjustment_properties["sectors"] == {"type": "ARRAY", "items": {"type": "STRING"}}
    assert adjustment_properties["require_updated_website"] == {"type": "BOOLEAN"}
    assert adjustment_properties["target_city"] == {"type": "STRING"}


def test_suggestion_provider_uses_stable_fallback_on_primary_503(monkeypatch):
    calls = []

    def fake_post(self, url, **kwargs):
        calls.append(url)
        if "gemini-3.5-flash:generateContent" in url:
            return httpx.Response(503, request=httpx.Request("POST", url), json={"error": {"status": "UNAVAILABLE"}})
        return _provider_success_response(url)

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    service = suggestions_module.GeminiCriteriaSuggestions(Settings(_env_file=None, gemini_api_key="fixture"))

    result = service.suggest(source_profile={"productora": "Demo"}, leads=[{"company": "Uno"}])

    assert len(result) == 3
    assert len(calls) == 2
    assert "gemini-3.5-flash:generateContent" in calls[0]
    assert "gemini-3.5-flash-lite:generateContent" in calls[1]


def test_suggestion_provider_does_not_retry_non_transient_403(monkeypatch):
    calls = []

    def fake_post(self, url, **kwargs):
        calls.append(url)
        return httpx.Response(403, request=httpx.Request("POST", url), json={"error": {"status": "PERMISSION_DENIED"}})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    service = suggestions_module.GeminiCriteriaSuggestions(Settings(_env_file=None, gemini_api_key="fixture"))

    with pytest.raises(suggestions_module.GeminiSuggestionsError) as exc_info:
        service.suggest(source_profile={"productora": "Demo"}, leads=[{"company": "Uno"}])

    assert len(calls) == 1
    assert exc_info.value.status_code == 403


def test_suggestion_errors_return_provider_neutral_payload(monkeypatch):
    def timeout(self, **kwargs):
        raise main_module.GeminiSuggestionsTimeout("SUGGESTIONS_TIMEOUT")

    monkeypatch.setattr(main_module.GeminiCriteriaSuggestions, "suggest", timeout)
    client = _client(monkeypatch)
    try:
        response = client.post(
            "/api/onboarding-sources/ONB-CLIENT/prospecting-improvements",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"adjustments": {"lead_count": 5}},
        )
        assert response.status_code == 504
        assert response.json()["detail"] == {
            "code": "SUGGESTIONS_TIMEOUT",
            "message": "No se pudieron generar sugerencias en este momento. Inténtalo de nuevo más tarde.",
        }
        assert "Gemini" not in response.text
        assert "OpenAI" not in response.text
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()

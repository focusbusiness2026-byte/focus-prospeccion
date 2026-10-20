import json

import pytest

from app.config import Settings
from app.onboarding import OnboardingSource
from app.short_profile import prepare_short_profile
from app.enrichment import _public_url


def short_source(website=""):
    return OnboardingSource.from_sheet_record({
        "ID registro": "ONB-12345678", "Empresa": "Productora A", "Web": website,
        "Email responsable": "ana@example.test", "Descripción": "Vídeo corporativo para empresas",
        "Servicio prioritario": "Vídeo corporativo para empresas", "Sectores": "Multisectorial",
        "Países objetivo": "España", "Tipos de cliente objetivo": "Empresa privada B2B",
        "Perfil ideal detallado": "Empresas B2B en España", "Autorización": "TRUE",
        "Modalidad alta": "Breve", "Estado perfil": "Pendiente",
    })


class FakeStore:
    def __init__(self, source):
        self.source = source
        self.updates = []

    def get_onboarding_source(self, record_id):
        assert record_id == "ONB-12345678"
        return self.source

    def update_short_onboarding_profile(self, record_id, profile):
        assert record_id == "ONB-12345678"
        self.updates.append(profile)
        return True


def test_short_source_without_website_is_ready_but_full_source_still_requires_it():
    source = short_source()
    assert source.ready
    assert not source.website
    full = OnboardingSource.from_sheet_record({key: value for key, value in source.raw_form.items() if key != "Modalidad alta"})
    assert "Falta una web pública válida" in full.blockers


def test_no_website_uses_declared_description_and_no_lead_search():
    store = FakeStore(short_source())
    seen = []

    def provider(payload):
        seen.append(payload)
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "activity": "Productora audiovisual", "service": "Vídeo corporativo",
            "target_sectors": [], "ideal_client": "",
        })}]}]}

    result = prepare_short_profile("ONB-12345678", store, Settings(openai_api_key="test"), response_provider=provider)
    assert result["state"] == "provisional"
    assert not seen[0].get("tools")
    assert "web_search" not in json.dumps(seen[0])
    assert store.updates[0]["Sectores"] == "Multisectorial"
    assert "sin web" in store.updates[0]["Origen perfil"]


def test_provider_failure_keeps_generic_profile_and_marks_review():
    store = FakeStore(short_source())

    def provider(_):
        raise RuntimeError("provider unavailable")

    result = prepare_short_profile("ONB-12345678", store, Settings(openai_api_key="test"), response_provider=provider)
    assert result["reason"] == "analysis_failed"
    assert store.updates[0]["Estado perfil"] == "Error análisis"
    assert "Servicio prioritario" not in store.updates[0]


def test_profile_work_refuses_a_full_onboarding_record():
    source = short_source()
    raw = dict(source.raw_form)
    raw["Modalidad alta"] = "Completa"
    store = FakeStore(OnboardingSource.from_sheet_record(raw))
    with pytest.raises(LookupError):
        prepare_short_profile("ONB-12345678", store, Settings(openai_api_key="test"))


@pytest.mark.parametrize("website", ["http://127.0.0.1/internal", "http://10.0.0.1/", "http://localhost/"])
def test_private_web_addresses_are_rejected_before_any_request(website):
    with pytest.raises(ValueError, match="locales|privadas"):
        _public_url(website)


def test_unreachable_website_preserves_declared_profile_and_marks_source():
    store = FakeStore(short_source("https://inaccesible.example.test"))

    class FailingScraper:
        def scrape(self, _website):
            raise ValueError("No se pudo resolver el dominio")

    def provider(_payload):
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "activity": "Vídeo corporativo", "service": "Vídeo corporativo",
            "target_sectors": [], "ideal_client": "",
        })}]}]}

    prepare_short_profile("ONB-12345678", store, Settings(openai_api_key="test"),
                          scraper=FailingScraper(), response_provider=provider)
    assert "web inaccesible" in store.updates[0]["Origen perfil"]
    assert store.updates[0]["Estado perfil"] == "Provisional"

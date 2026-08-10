import json
from copy import deepcopy

from app.config import Settings
from app.enrichment import OpenAIProspectDiscovery


def mock_response():
    prospect = {
        "company": "Empresa Pública Demo", "website": "https://empresa.example", "title": "Empresa Demo",
        "description": "Información pública de prueba", "sector": "Tecnología", "business_model": "B2B",
        "city": "Madrid", "country": "España", "client_type": "Empresa privada B2B", "employees": 40,
        "employee_trend": "growing", "revenue_eur": None, "revenue_trend": None, "capital_event": False,
        "financial_alert": False, "buying_signals": ["Contratación pública anunciada"],
        "decision_access": "active", "decision_recent": False, "disqualifiers": [],
        "summary": "Encaje verificable", "entry_angle": "Servicio audiovisual corporativo",
        "public_contacts": [{"type": "email", "value": "info@empresa.example", "source_url": "https://empresa.example/contacto"}],
        "public_signals": [{"type": "public_procurement", "value": "Busca agencia audiovisual", "date": "2026-08-01", "source_url": "https://empresa.example/contacto", "evidence_class": "company_declared", "confidence": "high"}],
        "decision_makers": [{"name": "Ana Demo", "role": "Directora de marketing", "public_contact": "", "source_url": "https://empresa.example/contacto"}],
        "social_links": {"linkedin": "https://linkedin.com/company/demo", "instagram": None, "facebook": None, "x": None, "youtube": None, "tiktok": None},
        "evidence_urls": ["https://empresa.example/contacto"], "no_contacts_reason": None,
    }
    return {
        "output": [
            {"type": "web_search_call", "action": {"query": "empresa tecnología Madrid", "sources": [{"url": "https://empresa.example/contacto", "title": "Contacto"}]}},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps({"prospects": [prospect], "no_prospect_reason": None, "research_summary": "Una empresa verificada"}), "annotations": []}]},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 80, "total_tokens": 180},
    }


def test_discovery_clamps_web_search_to_five_and_keeps_traceable_public_data():
    settings = Settings(openai_api_key="server-only", openai_web_search_max_calls=99)
    researcher = OpenAIProspectDiscovery(settings, response_provider=lambda _: mock_response())

    payload = researcher.build_discovery_payload({"targeting": {"sectors": ["Tecnología"]}})
    prospects, trace = researcher.discover({"targeting": {"sectors": ["Tecnología"]}})

    assert payload["max_tool_calls"] == 5
    assert "server-only" not in json.dumps(payload)
    assert trace["web_search_calls"] == 1
    assert trace["web_search_call_limit"] == 5
    assert trace["research_provider"] == "OpenAI Responses API + web_search"
    assert trace["search_trace"][0]["query"] == "empresa tecnología Madrid"
    assert trace["search_trace"][0]["status"] == "Completada"
    assert prospects[0]["public_contacts"][0]["source_url"] == "https://empresa.example/contacto"
    assert prospects[0]["public_signals"][0]["evidence_class"] == "company_declared"


def test_discovery_respects_the_requested_lead_quantity_without_more_search_calls():
    response = mock_response()
    original = json.loads(response["output"][1]["content"][0]["text"])
    prospects = []
    for index in range(3):
        item = deepcopy(original["prospects"][0])
        item["company"] = f"Empresa Demo {index}"
        item["website"] = f"https://empresa-{index}.example"
        prospects.append(item)
    original["prospects"] = prospects
    response["output"][1]["content"][0]["text"] = json.dumps(original)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    result, trace = researcher.discover({"targeting": {"sectors": ["Tecnología"]}}, {"lead_count": 1})

    assert len(result) == 1
    assert trace["web_search_calls"] == 1

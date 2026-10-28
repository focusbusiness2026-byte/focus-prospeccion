import json
from copy import deepcopy

from app.config import Settings
from app.enrichment import OpenAIProspectDiscovery, OpenAIWebResearcher, WebEvidence, enrich_discovered_prospects


def mock_response():
    prospect = {
        "company": "Empresa Pública Demo", "website": "https://empresa.example", "title": "Empresa Demo",
        "description": "Información pública de prueba", "sector": "Tecnología", "business_model": "B2B",
        "city": "Madrid", "country": "España", "client_type": "Empresa privada B2B", "employees": 40,
        "employee_trend": "growing", "revenue_eur": None, "revenue_trend": None, "capital_event": False,
        "financial_alert": False, "legal_status": "active", "legal_status_source_url": "https://empresa.example/contacto",
        "buying_signals": ["Contratación pública anunciada"],
        "decision_access": "active", "decision_recent": False, "disqualifiers": [],
        "summary": "Encaje verificable", "entry_angle": "Servicio audiovisual corporativo",
        "public_contacts": [{"type": "email", "value": "info@empresa.example", "source_url": "https://empresa.example/contacto"}],
        "public_signals": [{"type": "public_procurement", "value": "Busca agencia audiovisual", "date": "2026-08-01", "source_url": "https://empresa.example/contacto", "evidence_class": "company_declared", "confidence": "high"}],
        "decision_makers": [{"name": "Ana Demo", "role": "Directora de marketing", "role_status": "current", "public_contact": "", "source_url": "https://empresa.example/contacto"}],
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
    assert "potencial de\ncontratación recurrente" in payload["input"]
    assert "no se presume" in payload["input"]
    assert trace["web_search_calls"] == 1
    assert trace["web_search_call_limit"] == 5
    assert trace["research_provider"] == "OpenAI Responses API + web_search"
    assert trace["search_trace"][0]["query"] == "empresa tecnología Madrid"
    assert trace["search_trace"][0]["status"] == "Completada"
    assert prospects[0]["public_contacts"][0]["source_url"] == "https://empresa.example/contacto"
    assert prospects[0]["public_signals"][0]["evidence_class"] == "company_declared"


def test_discovery_uses_the_five_lead_goal_without_more_search_calls():
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

    assert len(result) == 3
    assert trace["web_search_calls"] == 1


def test_discovery_keeps_valid_results_when_provider_returns_extra_tool_items():
    response = mock_response()
    response["output"] = [deepcopy(response["output"][0]) for _ in range(6)] + response["output"][1:]
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    prospects, trace = researcher.discover({"targeting": {"sectors": ["Tecnología"]}})

    assert len(prospects) == 1
    assert trace["web_search_calls"] == 6
    assert trace["web_search_call_limit"] == 5


def test_discovery_keeps_cited_data_when_url_only_differs_by_trailing_slash_or_fragment():
    response = mock_response()
    response["output"][0]["action"]["sources"][0]["url"] = "https://empresa.example/contacto/"
    data = json.loads(response["output"][1]["content"][0]["text"])
    data["prospects"][0]["public_contacts"][0]["source_url"] += "#equipo"
    response["output"][1]["content"][0]["text"] = json.dumps(data)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    prospects, _ = researcher.discover({"targeting": {"sectors": ["Tecnología"]}})

    assert len(prospects) == 1
    cited_url = "https://empresa.example/contacto/"
    assert prospects[0]["evidence"] == [cited_url]
    assert prospects[0]["public_contacts"][0]["source_url"] == cited_url
    assert prospects[0]["public_signals"][0]["source_url"] == cited_url
    assert prospects[0]["decision_makers"][0]["source_url"] == cited_url


def test_citation_matching_ignores_tracking_parameters_but_preserves_document_id():
    response = mock_response()
    response["output"][0]["action"]["sources"][0]["url"] = (
        "https://empresa.example/registro?id=123&utm_source=chatgpt.com"
    )
    data = json.loads(response["output"][1]["content"][0]["text"])
    lead = data["prospects"][0]
    lead["evidence_urls"] = ["https://empresa.example/registro?id=123"]
    lead["decision_makers"][0]["source_url"] = "https://empresa.example/registro?id=123"
    lead["public_contacts"][0]["source_url"] = "https://empresa.example/registro?id=124"
    response["output"][1]["content"][0]["text"] = json.dumps(data)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="test"), response_provider=lambda _: response)

    leads, _ = researcher.discover({}, {})

    assert leads[0]["decision_makers"][0]["source_url"] == response["output"][0]["action"]["sources"][0]["url"]
    assert leads[0]["public_contacts"] == []


def test_discovery_still_rejects_uncited_pages():
    response = mock_response()
    response["output"][0]["action"]["sources"][0]["url"] = "https://otra.example/contacto/"
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    prospects, _ = researcher.discover({"targeting": {"sectors": ["Tecnología"]}})

    assert prospects == []


def test_identifiable_decision_maker_filter_rejects_generic_lead_but_keeps_verified_duplicate():
    response = mock_response()
    data = json.loads(response["output"][1]["content"][0]["text"])
    generic = deepcopy(data["prospects"][0])
    generic["decision_makers"] = []
    generic["public_contacts"] = []
    data["prospects"] = [generic, data["prospects"][0]]
    response["output"][1]["content"][0]["text"] = json.dumps(data)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    prospects, _ = researcher.discover({}, {"decision_roles": ["Dirección de marketing"], "require_identifiable_decision_maker": True})

    assert len(prospects) == 1
    assert prospects[0]["decision_makers"][0]["name"] == "Ana Demo"
    assert "La web corporativa general" in researcher.build_discovery_payload({}, {})["input"]


def test_identifiable_decision_maker_filter_explains_empty_result():
    response = mock_response()
    data = json.loads(response["output"][1]["content"][0]["text"])
    data["prospects"][0]["decision_makers"] = []
    response["output"][1]["content"][0]["text"] = json.dumps(data)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    prospects, trace = researcher.discover({}, {"require_identifiable_decision_maker": True})

    assert prospects == []
    assert "decisor identificable" in trace["no_prospect_reason"]


def test_generic_uncited_signals_do_not_inflate_lead_score():
    response = mock_response()
    data = json.loads(response["output"][1]["content"][0]["text"])
    lead = data["prospects"][0]
    lead["public_signals"] = []
    lead["decision_makers"] = []
    lead["buying_signals"] = ["Financiación reciente", "Campaña nueva", "Contratación"]
    lead["decision_access"] = "active"
    lead["decision_recent"] = True
    response["output"][1]["content"][0]["text"] = json.dumps(data)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    prospects, _ = researcher.discover({}, {})

    assert prospects[0]["buying_signals"] == []
    assert prospects[0]["decision_access"] is None
    assert prospects[0]["decision_recent"] is False
    assert prospects[0]["score_detail"]["buying_moment"] == 0.5
    assert prospects[0]["score_detail"]["decision_access"] == 0.5


def test_disqualified_lead_does_not_block_valid_duplicate():
    response = mock_response()
    data = json.loads(response["output"][1]["content"][0]["text"])
    disqualified = deepcopy(data["prospects"][0])
    disqualified["disqualifiers"] = ["Fuera del mercado objetivo"]
    data["prospects"] = [disqualified, data["prospects"][0]]
    response["output"][1]["content"][0]["text"] = json.dumps(data)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="server-only"), response_provider=lambda _: response)

    prospects, _ = researcher.discover({}, {})

    assert len(prospects) == 1
    assert prospects[0]["disqualifiers"] == []


def test_discovery_rejects_extinct_entity_and_historical_decisor():
    response = mock_response()
    data = json.loads(response["output"][1]["content"][0]["text"])
    extinct = deepcopy(data["prospects"][0])
    extinct["legal_status"] = "extinct"
    historical = deepcopy(data["prospects"][0])
    historical["company"] = "Empresa Vigente"
    historical["decision_makers"][0]["role_status"] = "historical"
    data["prospects"] = [extinct, historical]
    response["output"][1]["content"][0]["text"] = json.dumps(data)
    researcher = OpenAIProspectDiscovery(Settings(openai_api_key="test"), response_provider=lambda _: response)

    leads, _ = researcher.discover({}, {})

    assert len(leads) == 1
    assert leads[0]["company"] == "Empresa Vigente"
    assert leads[0]["decision_makers"] == []
    assert leads[0]["decision_access"] is None


def test_detailed_research_discards_extinct_company_and_keeps_current_cited_decisor():
    discovery_response = mock_response()
    discovery_data = json.loads(discovery_response["output"][1]["content"][0]["text"])
    old = deepcopy(discovery_data["prospects"][0])
    old["company"] = "Empresa Histórica"
    old["website"] = "https://historica.example"
    current = deepcopy(discovery_data["prospects"][0])
    current["company"] = "Empresa Vigente"
    current["website"] = "https://vigente.example"
    discovery_data["prospects"] = [old, current]
    discovery_response["output"][1]["content"][0]["text"] = json.dumps(discovery_data)
    discovery = OpenAIProspectDiscovery(Settings(openai_api_key="test"), response_provider=lambda _: discovery_response)
    leads, trace = discovery.discover({"targeting": {"sector": "Tecnología"}})

    def detailed_response(active: bool):
        data = deepcopy(current)
        data.update({
            "prospect_found": True,
            "no_prospect_reason": None if active else "Absorbida por otra sociedad en 2025",
            "disqualifiers": [],
            "legal_status": "active" if active else "extinct",
            "decision_makers": current["decision_makers"] if active else [],
            "buying_signals": ["Afirmación sin fuente"],
            "dossier": {
                "identification": [
                    {"text": "Sociedad operativa", "date": "2026-10-28", "source_url": "https://empresa.example/contacto", "certainty": "confirmed"},
                    {"text": "Afirmación no citada", "date": None, "source_url": "https://otra.example/", "certainty": "confirmed"},
                ],
                "verdict": [{"text": "Proponer una reunión comercial", "date": None, "source_url": None, "certainty": "recommendation"}],
            },
        })
        return {
            "output": [
                {"type": "web_search_call", "action": {"query": "situación registral y dirección", "sources": [
                    {"url": "https://empresa.example/contacto/", "title": "Equipo y contacto"}
                ]}},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(data), "annotations": []}]},
            ],
            "usage": {"input_tokens": 30, "output_tokens": 40, "total_tokens": 70},
        }

    responses = iter([detailed_response(False), detailed_response(True)])
    researcher = OpenAIWebResearcher(
        Settings(openai_api_key="test"), response_provider=lambda _: next(responses), max_search_calls=2
    )

    class StubScraper:
        def scrape(self, _website):
            return WebEvidence("", "", "", [], {}, [])

    verified, result_trace = enrich_discovered_prospects(
        leads, trace, {}, {"require_identifiable_decision_maker": True},
        researcher=researcher, scraper=StubScraper(),
    )

    assert [lead["company"] for lead in verified] == ["Empresa Vigente"]
    assert verified[0]["decision_makers"][0]["source_url"] == "https://empresa.example/contacto/"
    assert [fact["text"] for fact in verified[0]["dossier"]["identification"]] == ["Sociedad operativa"]
    assert verified[0]["dossier"]["verdict"][0]["certainty"] == "recommendation"
    assert verified[0]["buying_signals"] == ["Busca agencia audiovisual"]
    assert verified[0]["web_search_calls"] == 2
    assert result_trace["web_search_calls"] == 3
    assert result_trace["web_search_call_limit"] == 9
    assert "1 empresas aptas, 1 descartadas" in result_trace["research_summary"]


def test_company_research_search_limit_is_bounded_independently():
    settings = Settings(openai_company_research_max_calls=99)
    researcher = OpenAIWebResearcher(settings, max_search_calls=settings.company_research_call_limit)
    payload = researcher.build_payload("Empresa", WebEvidence("", "", "", [], {}, []))
    assert payload["max_tool_calls"] == 3
    assert "sociedad sigue operativa" in payload["input"]

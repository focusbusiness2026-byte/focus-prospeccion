from app.config import Settings
from app.main import _process_onboarding_trigger, _run_onboarding_research, _sync_automations_once
from app.onboarding import OnboardingSource


class PipelineStore:
    def __init__(self):
        self.prospects = []
        self.execution = None
        self.schema_checked = False
        self.dashboard_refreshed = False
        self.reservations = 0

    def ensure_operational_schema(self):
        self.schema_checked = True

    def reserve_execution(self, email):
        assert email == "owner@example.test"
        self.reservations += 1

    def existing_prospect_keys(self, onboarding_id):
        assert onboarding_id == "ONB-PIPELINE"
        return {"domain:duplicado.example"}

    def append_prospect(self, values):
        self.prospects.append(values)

    def append_execution(self, **values):
        self.execution = values

    def refresh_dashboard_summary(self):
        self.dashboard_refreshed = True


class FixtureDiscovery:
    def __init__(self, settings):
        assert settings.openai_api_key == "fixture-only"

    def discover(self, profile, adjustments):
        assert profile["onboarding_id"] == "ONB-PIPELINE"
        return [
            {"company": "Duplicado", "website": "https://duplicado.example", "city": "Madrid"},
            {"company": "Prospecto Nuevo", "website": "https://nuevo.example", "city": "Madrid"},
        ], {
            "prompt_tokens": 10, "output_tokens": 20, "total_tokens": 30,
            "web_search_calls": 2, "web_search_call_limit": 5,
            "search_queries": ["consulta uno", "consulta dos"], "research_sources": [],
            "no_prospect_reason": "", "research_summary": "Fixture local",
            "search_configuration": profile, "adjustments": adjustments or {},
            "research_provider": "OpenAI Responses API + web_search",
            "search_trace": [{"query": "consulta uno", "status": "Completada"}],
        }


def source():
    return OnboardingSource.from_sheet_record({
        "ID registro": "ONB-PIPELINE", "Empresa": "Productora Pipeline",
        "Web": "https://productora.example", "Email responsable": "owner@example.test",
        "Servicio prioritario": "Vídeo corporativo", "Servicios": "Vídeo corporativo",
        "Público": "B2B", "Sectores": "Tecnología", "Mercados": "España",
        "Países objetivo": "España", "Tipos de cliente objetivo": "Empresa privada B2B",
        "Tamaño empresa ideal": "11–50 empleados", "Autorización": "true",
    })


def test_onboarding_pipeline_uses_profile_deduplicates_and_persists_trace(monkeypatch):
    settings = Settings(openai_api_key="fixture-only", google_sheets_enabled=True)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.OpenAIProspectDiscovery", FixtureDiscovery)
    store = PipelineStore()

    result = _run_onboarding_research(source(), store, {"target_city": "Madrid"})

    assert result["ok"] is True
    assert store.schema_checked is True
    assert [item["company"] for item in store.prospects] == ["Prospecto Nuevo"]
    assert store.prospects[0]["onboarding_id"] == "ONB-PIPELINE"
    assert store.execution["duplicates_discarded"] == 1
    assert store.execution["web_search_calls"] == 2
    assert store.execution["search_trace"][0]["status"] == "Completada"
    assert store.reservations == 1
    assert store.dashboard_refreshed is True


def test_administrator_execution_bypasses_client_quota_and_records_actor(monkeypatch):
    settings = Settings(openai_api_key="fixture-only", google_sheets_enabled=True)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.OpenAIProspectDiscovery", FixtureDiscovery)
    store = PipelineStore()

    _run_onboarding_research(
        source(),
        store,
        {"target_city": "Madrid"},
        actor_email="admin@example.test",
        actor_role="Administrador",
        execution_origin="manual",
        bypass_user_limit=True,
    )

    assert store.reservations == 0
    assert store.execution["actor_email"] == "admin@example.test"
    assert store.execution["actor_role"] == "Administrador"
    assert store.execution["execution_origin"] == "manual"


def test_research_reports_real_phases_and_each_persisted_lead(monkeypatch):
    settings = Settings(openai_api_key="fixture-only", google_sheets_enabled=True)
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.OpenAIProspectDiscovery", FixtureDiscovery)
    store = PipelineStore()
    progress = []

    result = _run_onboarding_research(
        source(),
        store,
        {"target_city": "Madrid"},
        progress_callback=progress.append,
    )

    assert result["ok"] is True
    assert [item["phase"] for item in progress] == [
        "preparing",
        "searching",
        "validating",
        "saving",
        "finalizing",
        "completed",
    ]
    saved = next(item for item in progress if item["phase"] == "saving")
    assert saved["leads_found"] == 1
    assert saved["latest_lead"]["company"] == "Prospecto Nuevo"
    assert saved["latest_lead"]["execution_id"] == store.prospects[0]["execution_id"]
    assert progress[-1]["progress"] == 100
    assert progress[-1]["leads_found"] == 1


class PreparationStore:
    def __init__(self):
        self.saved = None

    def ensure_operational_schema(self):
        pass

    def get_onboarding_source(self, record_id):
        assert record_id == "ONB-PIPELINE"
        return source()

    def get_automation_config(self, onboarding_id):
        return None

    def upsert_automation_config(self, onboarding_id, email, **values):
        self.saved = {"onboarding_id": onboarding_id, "email": email, **values}
        return self.saved


def test_onboarding_trigger_prepares_profile_without_starting_search():
    store = PreparationStore()

    result = _process_onboarding_trigger("ONB-PIPELINE", store)

    assert result["state"] == "prepared"
    assert result["external_search_started"] is False
    assert result["credits_consumed"] is False
    assert result["automation"]["enabled"] is False
    assert result["automation"]["adjustments"]["lead_count"] == 5
    assert "No ejecutar búsquedas" in result["prompt_preview"]
    assert result["viral_radar_profile"]["client_key"] == "onb-pipeline"


def test_automation_runs_each_configured_cycle_without_provider_calls(monkeypatch):
    class Access:
        email = "owner@example.test"
        role = "Administrador"
        state = "Activo"

    class AutomationStore:
        def __init__(self, *_):
            self.marked = []
        def ensure_operational_schema(self): pass
        def due_automation_configs(self): return [{"onboarding_id": "ONB-PIPELINE", "email": "owner@example.test", "adjustments": {"runs_per_cycle": 4}}]
        def get_onboarding_source(self, *_): return type("Source", (), {"record_id": "ONB-PIPELINE", "email": "owner@example.test", "ready": True, "blockers": []})()
        def get_access(self, *_): return Access()
        def mark_automation_run(self, *values): self.marked.append(values)

    created = AutomationStore()
    calls = []
    settings = Settings(google_sheets_enabled=True, auto_research_enabled=True, openai_api_key="configured")
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.SheetStore", lambda *_: created)
    monkeypatch.setattr("app.main._run_onboarding_research", lambda *args, **kwargs: calls.append(kwargs) or {"prospects": [], "execution_id": str(len(calls))})
    _sync_automations_once()
    assert len(calls) == 4
    assert created.marked[-1][1].startswith("Completado ciclo: 4/4")

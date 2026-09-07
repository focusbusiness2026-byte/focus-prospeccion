from pathlib import Path
import re
import subprocess

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import Identity, require_identity
from app.config import Settings
from app.config import get_settings
from app.build_info import portal_build_id
from app.main import AutomationRequest, ResearchAdjustments, _client_execution_summary, _require_real_sheets
from app.sheet_store import AccessRecord


def test_automation_request_supports_cycle_runs_without_changing_internal_limit():
    payload = AutomationRequest(name="Prospección", interval_minutes=360, runs_per_cycle=2, adjustments=ResearchAdjustments())
    assert payload.enabled is False
    assert payload.runs_per_cycle == 2
    assert payload.adjustments.lead_count == 5


def test_real_sheet_source_is_required_when_unavailable():
    with pytest.raises(HTTPException) as exc:
        _require_real_sheets(Settings(google_sheets_enabled=False))
    assert exc.value.status_code == 503


def test_portal_has_selected_account_real_schedule_and_kanban_exports():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    assert 'data-schedule-frequency' in html
    assert 'Cada 3 horas' in html
    assert 'Cada 6 horas' in html
    assert 'Cada 12 horas' in html
    assert 'Cada 24 horas' in html
    assert 'data-automation-countdown' in html
    assert 'updatePersistedAutomationCountdown' in html
    assert 'pauseAutomation' in html
    assert 'Detener programación' in html
    assert 'portalReadOnly' not in html
    prospeccion = html[html.index('id="sources"'):html.index('id="results"')]
    assert 'raspado' not in prospeccion
    assert 'Mueve un lead entre Nuevo, En revisión y Aprobado para descarga.' in html
    assert 'Exportar para GoHighLevel' in html
    assert 'Exportar Meta' in html
    assert 'Columnas incluidas en el CSV' in html
    assert 'selectedFields=[]' in html


def test_portal_restores_full_operational_controls_and_styles():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    css = Path('app/static/app.css').read_text(encoding='utf-8')
    assert 'id="dashboard"' in html
    assert 'id="results"' in html
    assert 'id="execution-list"' in html
    assert 'id="refresh-top"' in html
    assert 'id="lead-dialog"' in html
    assert 'id="open-ghl-export"' in html
    assert 'fetchDashboardPayload' in html
    assert '.portal-app' in css
    assert '.top-navigation' in css
    assert '@media (max-width: 600px)' in css


def test_admin_can_switch_to_an_isolated_client_presentation():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    assert 'id="admin-presentation-toggle"' in html
    assert 'id="view-as-admin"' in html
    assert 'id="view-as-client"' in html
    assert "params.set('presentation','client')" in html
    assert 'Tu sesión administrativa no cambia.' in html
    assert 'toggle.hidden=false' in html
    assert 'clientButton.disabled=!adminViewAs' in html
    assert 'Selecciona una cuenta para habilitar la vista de cliente.' in html
    assert "control.hidden=!context.is_admin" in html
    assert "toggle.hidden=false" in html
    assert "adminPresentation='admin';loadDashboard()" in html


def test_client_identity_never_receives_admin_presentation_access(monkeypatch):
    class DashboardStore:
        def __init__(self, settings=None):
            pass

        def get_access(self, email):
            role = "Administrador" if email == "admin@example.com" else "Cliente"
            return AccessRecord(2, email, role, "Activo", 50, 0)

        def ensure_operational_schema(self):
            return None

        def onboarding_sources(self, email=None):
            return []

        def review_events(self, email=None):
            return []

        def recent_prospects(self, email=None):
            return []

        def recent_executions(self, email=None, *, hide_admin=False):
            return []

        def prospect_metrics(self, email=None):
            return {"total": 0, "classifications": {}, "statuses": {}}

        def global_metrics(self):
            return {"remaining": 0, "used": 0, "assigned": 0}

        def access_records(self):
            return [
                AccessRecord(2, "admin@example.com", "Administrador", "Activo", 0, 0),
                AccessRecord(3, "client@example.com", "Cliente", "Activo", 50, 0),
            ]

    monkeypatch.setenv("GOOGLE_SHEETS_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(main_module, "SheetStore", DashboardStore)
    main_module.app.dependency_overrides[require_identity] = lambda: Identity("client@example.com", "Cliente", "client")
    try:
        client = TestClient(main_module.app)
        denied = client.get("/api/portal-dashboard?view_as=other@example.com&presentation=client")
        own = client.get("/api/portal-dashboard")
        assert denied.status_code == 403
        assert own.status_code == 200
        assert own.json()["admin_context"]["is_admin"] is False
        assert own.json()["admin_context"]["presentation_mode"] == "client"
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_admin_identity_keeps_switch_controls_while_presenting_as_client(monkeypatch):
    class DashboardStore:
        def __init__(self, settings=None):
            pass

        def get_access(self, email):
            role = "Administrador" if email == "admin@example.com" else "Cliente"
            return AccessRecord(2, email, role, "Activo", 50, 0)

        def ensure_operational_schema(self):
            return None

        def onboarding_sources(self, email=None):
            return []

        def review_events(self, email=None):
            return []

        def recent_prospects(self, email=None):
            return []

        def recent_executions(self, email=None, *, hide_admin=False):
            return []

        def prospect_metrics(self, email=None):
            return {"total": 0, "classifications": {}, "statuses": {}}

        def global_metrics(self):
            return {"remaining": 0, "used": 0, "assigned": 0}

        def access_records(self):
            return [AccessRecord(3, "client@example.com", "Cliente", "Activo", 50, 0)]

    monkeypatch.setenv("GOOGLE_SHEETS_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(main_module, "SheetStore", DashboardStore)
    main_module.app.dependency_overrides[require_identity] = lambda: Identity("admin@example.com", "Administrador", "admin")
    try:
        response = TestClient(main_module.app).get(
            "/api/portal-dashboard?view_as=client@example.com&presentation=client"
        )
        assert response.status_code == 200
        context = response.json()["admin_context"]
        assert context["is_admin"] is True
        assert context["authenticated_email"] == "admin@example.com"
        assert context["viewing_as"] == "client@example.com"
        assert context["presentation_mode"] == "client"
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_kanban_drag_handle_moves_through_the_persisted_status_endpoint():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')

    assert 'class="crm-board-card ${saving?' in html
    assert 'draggable="${saving?' in html
    assert 'class="crm-drag-handle" aria-hidden="true"' in html
    assert "addEventListener('dragstart'" in html
    assert "addEventListener('dragover'" in html
    assert "addEventListener('drop'" in html
    assert "addEventListener('dragend'" in html
    assert "addEventListener('dragleave'" in html
    assert "isCrmInteractiveTarget" in html
    assert "card.getAttribute('draggable')!=='true'" in html
    assert "crmDragIdFromEvent" in html
    assert "clearCrmDrag();if(!column||!prospectId||!currentColumn)return" in html
    assert "pointer-events: none; transform: none" in Path('app/static/app.css').read_text(encoding='utf-8')
    assert "crmMovesInFlight.has(id)" in html
    assert "moveCrmProspect(prospectId,column.dataset.crmColumn)" in html
    assert "fetch(`/api/prospects/${encodeURIComponent(id)}/status`" in html
    assert 'data-move-prospect="${esc(item.execution_id)}"' in html
    assert "prospect.lead_status=columnId;renderCrmBoard()" in html
    assert "crmMovesInFlight.add(id)" in html
    assert "crmMovesInFlight.has(id)" in html
    assert "void refreshDashboardStateInBackground(id,columnId)" in html
    assert "prospect.lead_status=previousStatus" in html
    assert "Se restauró la columna anterior." in html


def test_kanban_only_offers_the_three_persistable_columns_and_resets_stale_drag_state():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')

    seed = re.search(r"const crmBoardSeed=.*", html).group(0)
    assert "{id:'Nuevo'" in seed
    assert "{id:'En revisión'" in seed
    assert "{id:'Aprobado para descarga'" in seed
    assert "{id:'Descartado'" not in seed
    assert "allowed.has(item.lead_status)?item.lead_status:'Nuevo'" in seed
    assert "function clearCrmDrag(){draggedProspectId='';" in html


def test_kanban_move_is_immediate_single_post_persistent_and_rolls_back_on_error():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    function_source = re.search(r"^\s*(async function moveCrmProspect\(.*)$", html, re.MULTILINE).group(1)
    script = f"""
const assert = require('assert');
let dashboardData={{prospects:[{{execution_id:'LEAD-1',lead_status:'Nuevo'}}]}};
const crmMovesInFlight=new Set();
const readCrmBoard=()=>({{columns:[{{id:'Nuevo'}},{{id:'En revisión'}}]}});
let renders=[];
const renderCrmBoard=()=>renders.push({{status:dashboardData.prospects[0].lead_status,at:Date.now()}});
const message={{textContent:''}};
const headers=()=>({{}});
let fetchCalls=0;
let persisted='Nuevo';
let fail=false;
const fetch=async()=>{{fetchCalls++;await new Promise(resolve=>setTimeout(resolve,250));if(fail)return {{ok:false,json:async()=>({{detail:'fallo controlado'}})}};persisted='En revisión';return {{ok:true,json:async()=>({{prospect:{{lead_status:persisted}}}})}};}};
const refreshDashboardStateInBackground=()=>Promise.resolve();
{function_source}
(async()=>{{
  const started=Date.now();
  const first=moveCrmProspect('LEAD-1','En revisión');
  const duplicate=moveCrmProspect('LEAD-1','En revisión');
  assert.equal(dashboardData.prospects[0].lead_status,'En revisión');
  assert.ok(Date.now()-started<200,'el cambio visual no fue inmediato');
  await Promise.all([first,duplicate]);
  assert.equal(fetchCalls,1,'se envió más de un POST');
  dashboardData.prospects[0].lead_status=persisted;
  assert.equal(dashboardData.prospects[0].lead_status,'En revisión');
  fail=true;
  await moveCrmProspect('LEAD-1','Nuevo');
  assert.equal(dashboardData.prospects[0].lead_status,'En revisión');
  assert.ok(message.textContent.includes('Se restauró la columna anterior.'));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_admin_client_selection_has_a_four_second_loading_state_and_clear_empty_copy():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    css = Path('app/static/app.css').read_text(encoding='utf-8')

    assert 'id="client-selection-loading"' in html
    assert 'Cargando…' in html
    assert 'clientSelectionDelay(4000)' in html
    assert 'Debes seleccionar un cliente' in html
    assert 'Configuración bloqueada' not in html
    assert '.client-selection-loading[hidden]' in css
    assert 'pointer-events: none' in css


def test_home_has_real_saved_schedule_controls_and_intro_video_placeholder():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    assert 'id="favorite-automation-select"' in html
    assert 'id="quick-toggle-automation"' in html
    assert 'id="quick-run-automation"' in html
    assert 'id="dashboard-preview-countdown"' in html
    assert 'Video pendiente de configurar' in html


def test_client_execution_summary_hides_technical_provider_errors():
    execution = {
        "execution_id": "RUN-1",
        "created_at": "2026-09-07T10:00:00Z",
        "productora": "Productora Norte",
        "status": "Falló",
        "error": "OpenAI API devolvió HTTP 429",
        "no_prospect_reason": "OpenAI API devolvió HTTP 429",
        "search_queries": ["empresas industriales Madrid"],
        "adjustments": {"lead_count": 5},
        "duplicates_discarded": 1,
    }
    prospects = [{"execution_id": "RUN-1-001"}, {"execution_id": "RUN-1-002"}]

    summary = _client_execution_summary(execution, prospects)

    assert summary["status"] == "Esperando turno"
    assert summary["found"] == 2
    assert summary["deficit"] == 3
    assert summary["duplicates_excluded"] == 1
    assert "OpenAI" not in summary["reason"]
    assert "429" not in summary["reason"]
    assert "error" not in summary


def test_portal_assets_use_the_build_fingerprint_instead_of_a_manual_cache_key():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    login = Path("app/templates/login.html").read_text(encoding="utf-8")

    assert 'app.css?v={{ portal_asset_version }}' in portal
    assert 'app.css?v={{ portal_asset_version }}' in login
    assert len(portal_build_id()) == 12


def test_render_runs_the_same_fastapi_application_used_locally():
    render = Path("render.yaml").read_text(encoding="utf-8")

    assert "uvicorn app.main:app" in render
    assert "GOOGLE_SHEETS_ENABLED" in render
    assert "CENTRAL_AUTH_ENABLED" in render
    assert "DEMO_AUTH_BYPASS" not in render or 'value: "false"' in render

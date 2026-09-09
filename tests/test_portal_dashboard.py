from pathlib import Path
import re
import subprocess

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_module
from app.auth import CSRF_COOKIE, Identity, require_identity
from app.config import Settings
from app.config import get_settings
from app.build_info import portal_build_id
from app.main import AutomationRequest, ResearchAdjustments, _client_execution_summary, _require_real_sheets
from app.sheet_store import AccessRecord, ScrapeQuotaExceeded


def test_automation_request_supports_cycle_runs_without_changing_internal_limit():
    payload = AutomationRequest(name="Prospección", interval_minutes=360, runs_per_cycle=2, adjustments=ResearchAdjustments())
    assert payload.enabled is False
    assert payload.runs_per_cycle == 2
    assert payload.adjustments.lead_count == 5


def test_automation_request_supports_the_weekly_quick_option():
    payload = AutomationRequest(name="Semanal", interval_minutes=10080, runs_per_cycle=1)
    assert payload.interval_minutes == 10080


def test_real_sheet_source_is_required_when_unavailable():
    with pytest.raises(HTTPException) as exc:
        _require_real_sheets(Settings(google_sheets_enabled=False))
    assert exc.value.status_code == 503


def test_research_start_rejects_an_exhausted_client_before_scheduling_provider_work(monkeypatch):
    class ExhaustedStore:
        def __init__(self, settings=None):
            pass

        def get_access(self, email):
            return AccessRecord(2, email, "Cliente", "Activo", 50, 50)

        def get_onboarding_source(self, record_id, email=None):
            return type("Source", (), {"record_id": record_id, "email": email, "ready": True, "blockers": []})()

        def check_scrape_limit(self, email):
            raise ScrapeQuotaExceeded("Límite de raspados alcanzado. Contacta con soporte.")

    monkeypatch.setenv("GOOGLE_SHEETS_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    get_settings.cache_clear()
    monkeypatch.setattr(main_module, "SheetStore", ExhaustedStore)
    main_module.app.dependency_overrides[require_identity] = lambda: Identity("client@example.com", "Cliente", "client")
    try:
        client = TestClient(main_module.app)
        client.cookies.set(CSRF_COOKIE, "csrf-test")
        response = client.post(
            "/api/onboarding-sources/ONB-LIMIT/research-jobs",
            headers={"X-CSRF-Token": "csrf-test"},
            json={"lead_count": 5},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Límite de raspados alcanzado. Contacta con soporte."
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_portal_has_selected_account_real_schedule_and_kanban_exports():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    assert 'Tipo de venta' not in html
    assert 'salesModels:' not in html
    assert "sales_models:['Proyecto puntual','Recurrente / retainer']" not in html
    assert '<link rel="icon" href="/static/favicon.svg"' in html
    assert 'data-schedule-frequency' in html
    assert "label:'3 horas'" in html
    assert "label:'6 horas'" in html
    assert "label:'12 horas'" in html
    assert "label:'24 horas'" in html
    assert 'data-automation-countdown' in html
    assert 'updatePersistedAutomationCountdown' in html
    assert 'setAutomationState' in html
    assert 'data-action="start-automation"' in html
    assert 'data-action="stop-automation"' in html
    assert "label:'3 días'" in html
    assert "label:'1 semana'" in html
    assert 'portalReadOnly' not in html
    prospeccion = html[html.index('id="sources"'):html.index('id="results"')]
    assert 'raspado' not in prospeccion
    assert 'Arrastra una tarjeta o cambia su estado.' in html
    assert 'Exportar para GoHighLevel' in html
    assert 'Exportar para Meta' in html
    assert 'Raspado Personalizado' in html
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


def test_mobile_header_keeps_the_account_control_next_to_the_menu_toggle():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    css = Path('app/static/app.css').read_text(encoding='utf-8')

    mobile_header = css[css.index('@media (max-width: 1180px)'):css.index('@media (max-width: 900px)')]
    assert 'grid-template-columns: minmax(0, 1fr) auto auto' in mobile_header
    assert '.menu-toggle { display: grid; grid-column: 2; grid-row: 1;' in mobile_header
    assert '.site-header .header-actions { grid-column: 3; grid-row: 1;' in mobile_header
    assert '.site-header .account-menu { position: relative; margin: 0;' in mobile_header
    assert 'class="account-avatar"' in html
    assert '<details class="account-menu"><summary aria-label=' in html
    assert 'content: "CU"' not in css
    assert '.account-menu > summary { min-width: 40px; width: 40px; min-height: 40px; height: 40px;' in css
    assert '.account-avatar { display: block;' in css
    navigation = html[html.index('id="top-navigation"'):html.index('</nav>')]
    header_actions = html[html.index('<div class="header-actions">'):html.index('</header>')]
    assert 'id="admin-client-view"' not in navigation
    assert 'id="admin-client-view"' in header_actions
    assert '.site-header .header-actions .admin-control-menu:not([hidden]) { display: block;' in mobile_header
    assert '@media (max-width: 768px)' in css


def test_desktop_header_uses_compact_admin_and_account_menus():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    css = Path('app/static/app.css').read_text(encoding='utf-8')

    assert 'id="admin-control-menu"' in html
    assert 'class="admin-control-popover"' in html
    assert 'id="account-current-view"' in html
    assert 'id="account-selected-client"' in html
    account_summary = html[html.index('<details class="account-menu">'):html.index('</details></div>', html.index('<details class="account-menu">'))]
    assert '{{ identity.email }}' not in account_summary.split('</summary>', 1)[0]
    assert '.admin-control-menu > summary { display: flex; min-height: 32px;' in css
    assert '.account-menu > summary { display: grid; width: 40px; height: 40px;' in css
    assert "controlMenu.hidden=!context.is_admin" in html


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
    assert "switchAdminPresentation('admin')" in html
    assert "switchAdminPresentation('client')" in html
    assert "clientSelectionDelay(minimumMs)" in html
    assert "document.querySelector('#global-card').hidden=!isAdmin" in html
    assert "document.querySelector('#ai-search-card').hidden=!isAdmin" in html


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
        assert response.json()["user"] == {
            "email": "client@example.com",
            "role": "Cliente",
            "assigned": 50,
            "used": 0,
            "available": 50,
            "unlimited": False,
        }
    finally:
        main_module.app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_kanban_drag_handle_moves_through_the_persisted_status_endpoint():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')

    assert 'class="crm-board-card ${persisting?' in html
    assert 'draggable="${persisting?\'false\':\'true\'}"' in html
    assert 'class="crm-drag-handle" aria-hidden="true"' in html
    assert "addEventListener('dragstart'" in html
    assert "column.addEventListener('dragover',handleCrmColumnDragOver)" in html
    assert "addEventListener('drop'" in html
    assert "addEventListener('dragend'" in html
    assert "addEventListener('dragleave'" in html
    assert "isCrmInteractiveTarget" in html
    assert "card.getAttribute('draggable')!=='true'" in html
    assert "crmDragIdFromEvent" in html
    assert "function bindCrmColumnDropTargets(container)" in html
    assert "bindCrmColumnDropTargets(container)" in html
    assert ".crm-board-card.moved { animation: kanban-card-arrive 160ms ease-out both; }" in Path('app/static/app.css').read_text(encoding='utf-8')
    assert "crmMovesInFlight.has(id)" in html
    assert "moveCrmProspect(prospectId,column.dataset.crmColumn)" in html
    assert "fetch(`/api/prospects/${encodeURIComponent(id)}/status`" in html
    assert 'data-move-prospect="${esc(item.execution_id)}"' in html
    assert "prospect.lead_status=columnId;renderCrmBoard()" in html
    assert "crmMovesInFlight.add(id)" in html
    assert "crmMovesInFlight.has(id)" in html
    assert "enqueueCrmStatusWrite" in html
    assert "crmStatusWriteQueue" in html
    assert "scheduleDashboardRefresh" not in html
    assert "refreshDashboardStateInBackground" not in html
    assert "prospect.lead_status=previousStatus" in html
    assert "Se restauró la columna anterior." in html
    assert "Tarjeta movida. Esperando turno de guardado…" in html


def test_kanban_native_drop_listener_prevents_default_and_moves_the_transferred_card():
    """Exercise the actual column handlers with an empty-column-style DOM target."""
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    handlers = re.search(
        r"\s{4}const allowedCrmColumn=.*?function bindCrmColumnDropTargets\(container\)\{.*?\}\n",
        html,
        re.DOTALL,
    ).group(0)
    script = f"""
const assert = require('assert');
const moved=[];
const dataTransfer={{value:'LEAD-1',dropEffect:'',getData(){{return this.value;}}}};
const destination={{dataset:{{crmColumn:'Aprobado para descarga'}},listeners:{{}},contains(){{return false;}},closest(){{return this;}},addEventListener(name,handler){{this.listeners[name]=handler;}}}};
const cards={{closest(){{return destination;}}}};
const container={{querySelectorAll(){{return [destination];}}}};
destination.querySelector=selector=>selector==='.crm-column-cards'?cards:null;
const card={{dataset:{{currentColumn:'Nuevo'}}}};
const document={{querySelector(){{return card;}}}};
const CSS={{escape:value=>value}};
const readCrmBoard=()=>({{columns:[{{id:'Nuevo'}},{{id:'En revisión'}},{{id:'Aprobado para descarga'}}]}});
const crmDragIdFromEvent=e=>e.dataTransfer.getData('text/plain');
const setCrmDropTarget=column=>{{destination.highlighted=column===destination;}};
const clearCrmDrag=()=>{{destination.cleared=true;}};
const moveCrmProspect=(id,status)=>moved.push([id,status]);
{handlers}
bindCrmColumnDropTargets(container);
const over={{currentTarget:destination,target:cards,dataTransfer,prevented:false,preventDefault(){{this.prevented=true;}}}};
destination.listeners.dragover(over);
assert.equal(over.prevented,true,'dragover debe habilitar el drop');
assert.equal(dataTransfer.dropEffect,'move');
const drop={{currentTarget:destination,target:cards,dataTransfer,prevented:false,preventDefault(){{this.prevented=true;}}}};
destination.listeners.drop(drop);
assert.equal(drop.prevented,true,'drop debe impedir el comportamiento nativo');
assert.deepEqual(moved,[['LEAD-1','Aprobado para descarga']]);
assert.equal(destination.cleared,true);
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_kanban_starts_with_three_columns_and_discovers_safe_custom_columns():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')

    seed = re.search(r"const crmBoardSeed=.*", html).group(0)
    assert "{id:'Nuevo'" in seed
    assert "{id:'En revisión'" in seed
    assert "{id:'Aprobado para descarga'" in seed
    assert "{id:'Descartado'" not in seed
    assert "readCustomKanbanColumns()" in seed
    assert "discovered.push({id:status,name:status})" in seed
    assert "known.has(item.lead_status)?item.lead_status:'Nuevo'" in seed
    assert "function clearCrmDrag(){draggedProspectId='';" in html


def test_kanban_move_is_immediate_single_post_persistent_and_rolls_back_on_error():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    function_source = re.search(r"^\s*(async function moveCrmProspect\(.*)$", html, re.MULTILINE).group(1)
    script = f"""
const assert = require('assert');
let dashboardData={{prospects:[{{execution_id:'LEAD-1',lead_status:'Nuevo'}}]}};
const crmMovesInFlight=new Set();
let crmStatusWriteQueue=Promise.resolve();
function enqueueCrmStatusWrite(operation){{const queued=crmStatusWriteQueue.then(operation,operation);crmStatusWriteQueue=queued.catch(()=>{{}});return queued;}}
const readCrmBoard=()=>({{columns:[{{id:'Nuevo'}},{{id:'En revisión'}}]}});
let renders=[];
const renderCrmBoard=()=>renders.push({{status:dashboardData.prospects[0].lead_status,at:Date.now()}});
const message={{textContent:''}};
const headers=()=>({{'X-CSRF-Token':'csrf-test','X-Request-Source':'portal'}});
let fetchCalls=0;
let lastRequest;
let persisted='Nuevo';
let fail=false;
const fetch=async(url,request)=>{{fetchCalls++;lastRequest={{url,request}};await new Promise(resolve=>setTimeout(resolve,250));if(fail)return {{ok:false,json:async()=>({{detail:'fallo controlado'}})}};persisted='En revisión';return {{ok:true,json:async()=>({{prospect:{{lead_status:persisted}}}})}};}};
{function_source}
(async()=>{{
  const started=Date.now();
  const first=moveCrmProspect('LEAD-1','En revisión');
  const duplicate=moveCrmProspect('LEAD-1','En revisión');
  assert.equal(dashboardData.prospects[0].lead_status,'En revisión');
  assert.ok(Date.now()-started<200,'el cambio visual no fue inmediato');
  await Promise.all([first,duplicate]);
  assert.equal(fetchCalls,1,'se envió más de un POST');
  assert.equal(lastRequest.url,'/api/prospects/LEAD-1/status');
  assert.equal(lastRequest.request.credentials,'same-origin');
  assert.equal(lastRequest.request.headers['Content-Type'],'application/json');
  assert.equal(lastRequest.request.headers['X-CSRF-Token'],'csrf-test');
  assert.equal(lastRequest.request.headers['X-Request-Source'],'portal');
  assert.deepEqual(JSON.parse(lastRequest.request.body),{{status:'En revisión'}});
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


def test_kanban_rapid_moves_are_optimistic_fifo_and_never_refetch_dashboard():
    """Rapid moves paint immediately, serialize POSTs, and do not trigger a GET."""
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    move = re.search(r"^\s*(async function moveCrmProspect\(.*)$", html, re.MULTILINE).group(1)
    script = f"""
const assert=require('assert');
let dashboardData={{prospects:[{{execution_id:'LEAD-1',lead_status:'Nuevo'}},{{execution_id:'LEAD-2',lead_status:'Nuevo'}},{{execution_id:'LEAD-3',lead_status:'Nuevo'}},{{execution_id:'LEAD-4',lead_status:'Nuevo'}}]}};
const crmMovesInFlight=new Set();let crmStatusWriteQueue=Promise.resolve();
function enqueueCrmStatusWrite(operation){{const queued=crmStatusWriteQueue.then(operation,operation);crmStatusWriteQueue=queued.catch(()=>{{}});return queued;}}
const readCrmBoard=()=>({{columns:[{{id:'Nuevo'}},{{id:'En revisión'}},{{id:'Aprobado para descarga'}}]}});
const renderCrmBoard=()=>{{}};const message={{textContent:''}};const headers=()=>({{}});
let statusPosts=0,inFlight=0,maxInFlight=0,dashboardGets=0;const order=[];let failLead='LEAD-3';
const fetch=async(url,request)=>{{if(request?.method!=='POST'){{dashboardGets++;return {{ok:true,json:async()=>({{}})}};}}statusPosts++;inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);const id=url.split('/')[3];order.push(id);await new Promise(resolve=>setTimeout(resolve,15));inFlight--;if(id===failLead)return {{ok:false,json:async()=>({{detail:'fallo aislado'}})}};return {{ok:true,json:async()=>({{prospect:{{lead_status:JSON.parse(request.body).status}}}})}};}};
{move}
(async()=>{{
  const moves=[moveCrmProspect('LEAD-1','En revisión'),moveCrmProspect('LEAD-2','Aprobado para descarga'),moveCrmProspect('LEAD-3','En revisión'),moveCrmProspect('LEAD-4','Aprobado para descarga')];
  assert.deepEqual(dashboardData.prospects.map(item=>item.lead_status),['En revisión','Aprobado para descarga','En revisión','Aprobado para descarga'],'las cuatro mutaciones deben ser optimistas');
  assert.equal(crmMovesInFlight.size,4,'solo las cuatro tarjetas encoladas quedan bloqueadas');
  await Promise.all(moves);
  assert.equal(statusPosts,4);
  assert.equal(maxInFlight,1,'la cola debe mantener como máximo un POST en vuelo');
  assert.deepEqual(order,['LEAD-1','LEAD-2','LEAD-3','LEAD-4'],'la cola debe respetar FIFO');
  assert.equal(dashboardGets,0,'un drop no debe pedir el dashboard completo');
  assert.equal(dashboardData.prospects[2].lead_status,'Nuevo','el fallo aislado debe restaurar solo esa tarjeta');
  assert.equal(dashboardData.prospects[3].lead_status,'Aprobado para descarga','las tarjetas posteriores deben continuar');
  assert.equal(crmMovesInFlight.size,0);
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_admin_client_selection_has_a_four_second_loading_state_and_clear_empty_copy():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    css = Path('app/static/app.css').read_text(encoding='utf-8')

    assert 'id="client-selection-loading"' in html
    assert 'Cargando…' in html
    assert "switchAdminPresentation('admin',4000)" in html
    assert 'Debes seleccionar un cliente' in html
    assert 'Configuración bloqueada' not in html
    assert '.client-selection-loading[hidden]' in css
    assert 'pointer-events: none' in css


def test_home_has_real_saved_schedule_controls_and_intro_video_placeholder():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')
    assert 'id="favorite-automation-select"' in html
    assert 'id="dashboard-frequency-buttons"' in html
    assert 'id="quick-start-automation"' in html
    assert 'id="quick-stop-automation"' in html
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

    assert summary["status"] == "Fallida"
    assert summary["found"] == 2
    assert summary["deficit"] == 3
    assert summary["duplicates_excluded"] == 1
    assert "OpenAI" not in summary["reason"]
    assert "429" not in summary["reason"]
    assert "error" not in summary
    assert "search_queries" not in summary
    assert "criteria_summary" in summary


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


def test_improvement_ui_uses_server_contract_and_saves_favorite():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    assert 'data-action="create-prospecting-questionnaire"' in portal
    assert 'id="prospecting-questionnaire-dialog"' in portal
    assert 'id="prospecting-question-count"' in portal
    assert "Array.from({length:26}" in portal
    assert "/prospecting-questionnaire`" in portal
    assert "/prospecting-improvements`" in portal
    assert "questionnaire_answers:questionnaireAnswers" in portal
    assert "suggestions.length!==3" in portal
    assert "favorite:true" in portal
    assert "suggestion.adjustments" in portal
    assert "GEMINI_API_KEY" not in portal
    assert "new AbortController()" in portal
    assert "timeoutMs=count>10?75000:45000" in portal
    assert "Cargando preguntas…" in portal
    assert "form.setAttribute('aria-busy','true')" in portal
    assert "error?.name==='AbortError'&&count>10" in portal


def test_improvement_names_sync_with_research_choice_without_reload():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    assert "function updateProspectingImprovementName(workspace,index,value)" in portal
    assert "suggestion.title=value" in portal
    assert "option.textContent=value.trim()||`Propuesta ${index+1}`" in portal
    assert "if(choice?.value===String(index))" in portal
    assert "updateProspectingImprovementName(workspace,Number(input.dataset.improvementName),input.value)" in portal
    assert "if(suggestion&&scheduleName)scheduleName.value=suggestion.title" in portal


def test_visible_suggestion_copy_is_strictly_provider_neutral():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")

    assert "ASISTENCIA CON GEMINI" not in portal
    assert "Búsqueda OpenAI" not in portal
    assert "Gemini no" not in portal
    assert "RECOMENDACIONES INTELIGENTES" in portal
    assert "No se pudieron generar sugerencias en este momento. Inténtalo de nuevo más tarde." in portal


def test_kanban_cards_do_not_show_a_quality_percentage_ring():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    assert "function scoreQualityPercent(score)" not in portal
    assert 'aria-label="Calidad ${quality} por ciento"' not in portal
    assert 'class="quality-ring"' not in portal
    assert ".quality-ring" not in css


def test_contact_export_is_simplified_for_gohighlevel_and_custom_scrape_is_safe():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    dialog = portal[portal.index('id="ghl-export-dialog"'):portal.index('id="briefing-dialog"')]
    assert "Exportar contactos para GoHighLevel" in dialog
    assert "Productora aislada" not in dialog
    assert "Propósito" not in dialog
    assert "Leads seleccionados y deduplicados" not in dialog
    assert 'id="ghl-contact-count"' not in dialog
    assert 'id="ghl-lead-list"' not in dialog
    assert 'id="open-custom-scrape"' in portal
    assert "document.querySelector('#open-custom-scrape').addEventListener('click',()=>openCustomScrape())" in portal
    assert 'id="custom-scrape-dialog"' in portal
    assert "function openCustomScrape(){const dialog=document.querySelector('#custom-scrape-dialog')" in portal
    assert "showView('sources'" not in portal[portal.index("function openCustomScrape()"):portal.index("const customKanbanStorageKey")]


def test_questionnaire_keeps_context_and_renders_proposals_in_dialog():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    assert 'id="prospecting-questionnaire-results"' in portal
    assert "await improveProspecting(workspace,answers,true)" in portal
    assert "prospecting-questionnaire-dialog').close();improveProspecting" not in portal
    assert "function renderQuestionnaireImprovementResults(workspace)" in portal


def test_kanban_adds_columns_only_from_the_end_of_the_horizontal_canvas():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    assert "DESPLAZAR CANVAS" in portal
    assert "data-add-to-column" in portal
    assert "function bindCrmCanvasControls(container,previousScroll=0)" in portal
    assert "function enhanceKanbanColumns(container)" in portal
    assert "querySelectorAll('[data-add-to-column]').forEach(button=>button.remove())" in portal
    assert "data-add-kanban-column" in portal
    assert "createKanbanColumn()" in portal
    assert "focus_custom_kanban_columns_v1:" in portal
    assert 'id="kanban-quick-add-dialog"' not in portal
    assert "createQuickKanbanTask" not in portal
    assert "focus_quick_kanban_tasks_v1:" not in portal
    assert ".crm-canvas-scroll" in css
    assert ".crm-add-column" in css


def test_custom_kanban_columns_can_be_renamed_without_changing_their_status_id():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")

    assert "function saveKanbanColumnName(columnId,value)" in portal
    assert "column.name=name" in portal
    assert "column.id=" not in portal[portal.index("function saveKanbanColumnName"):portal.index("function startKanbanColumnRename")]
    assert "function startKanbanColumnRename(columnId)" in portal
    assert "data-edit-kanban-column" in portal
    assert "data-kanban-column-title" in portal
    assert "addEventListener('dblclick'" in portal
    assert "if(e.key==='Enter')" in portal
    assert "addEventListener('focusout'" in portal
    assert ".crm-column-edit" in css
    assert ".crm-column-title-input" in css


def test_custom_kanban_column_rename_updates_only_the_visible_name():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    function_source = re.search(r"^\s*(function saveKanbanColumnName\(.*)$", portal, re.MULTILINE).group(1)
    script = f"""
const assert=require('assert');
let custom=[{{id:'Revisión',name:'Revisión'}}],written=null,renders=0;
const readCustomKanbanColumns=()=>custom.map(column=>({{...column}}));
const writeCustomKanbanColumns=columns=>{{written=columns;custom=columns;}};
const readCrmBoard=()=>({{columns:[{{id:'Nuevo',name:'Nuevo'}},...custom]}});
const renderCrmBoard=()=>{{renders++;}};
const message={{textContent:''}};
{function_source}
assert.equal(saveKanbanColumnName('Revisión','Seguimiento comercial'),true);
assert.equal(written[0].id,'Revisión');
assert.equal(written[0].name,'Seguimiento comercial');
assert.equal(renders,1);
assert.ok(message.textContent.includes('Seguimiento comercial'));
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_lead_detail_has_individual_and_complete_copy_actions():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    assert "data-copy-value" in portal
    assert "Copiar notas" in portal
    assert "Copiar ficha completa" in portal
    assert 'class="copy-field-button"' in portal
    assert ".copy-field-button { min-width: 56px; min-height: 24px; padding: 3px 8px;" in Path("app/static/app.css").read_text(encoding="utf-8")


def test_client_execution_view_shows_terminal_statuses_metrics_and_safe_traceability():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    assert "['Completada','Fallida'].includes(x.status)" in portal
    assert 'id="client-execution-order"' in portal
    assert "effectiveness-desc" in portal
    assert "Resultado de la ejecución" in portal
    assert "Ver trazabilidad" in portal
    assert "Configuración utilizada" in portal
    client_branch = portal.split("if(!isAdminView){", 1)[1].split("return;", 1)[0]
    assert "x.error" not in client_branch
    assert "research_provider" not in client_branch
    assert "search_trace" not in client_branch
    assert "x.search_queries" not in client_branch
    assert "x.criteria_summary" in client_branch


def test_improvement_panel_collapses_to_one_column_on_small_screens():
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    assert "@media (max-width: 700px)" in css
    assert ".improvement-suggestions { grid-template-columns: 1fr; }" in css


def test_improvements_use_focus_loading_screen_and_named_options():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    assert 'id="portal-operation-loading"' in portal
    assert "showOperationLoading('Mejorando tu prospección'" in portal
    assert "finally{hideOperationLoading();}" in portal
    assert 'data-improvement-name="${index}"' in portal
    assert 'data-recommended-choice' in portal
    assert ".portal-operation-loading[hidden]" in css


def test_compact_header_and_named_export_controls_are_present():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")
    assert "min-height: 54px" in css
    assert ".top-navigation {" in css and "display: flex" in css
    assert "function ensureExportControls" in portal
    assert "Nombre de la descarga" in portal
    assert "safeExportFilename" in portal


def test_optional_legacy_refresh_control_cannot_block_portal_boot():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    assert "document.querySelector('#refresh')?.addEventListener('click',loadDashboard)" in portal
    assert "loadDashboard().finally(()=>setTimeout(finishPortalBoot,280))" in portal


def test_completed_client_execution_can_request_and_save_three_safe_improvements():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    assert "x.status==='Completada'?clientExecutionImprovementMarkup(x):''" in portal
    assert "Mejorar esta búsqueda" in portal
    assert "data-improve-execution" in portal
    assert "JSON.stringify({adjustments,execution_id:execution.execution_id})" in portal
    assert "suggestions.length!==3" in portal
    assert "data-save-execution-improvement" in portal
    assert "data-run-execution-improvement" in portal
    assert "data-execution-improvement-name" in portal
    assert "favorite:true" in portal
    client_function = portal.split("async function improveClientExecution", 1)[1].split("async function saveClientExecutionImprovement", 1)[0]
    assert "data.detail" not in client_function
    assert "error.message" not in client_function
    assert "No pudimos preparar las mejoras ahora" in client_function


def test_both_presentations_use_the_same_simple_lead_search_and_manual_exports():
    portal = Path("app/templates/portal.html").read_text(encoding="utf-8")
    css = Path("app/static/app.css").read_text(encoding="utf-8")

    assert 'id="result-search"' in portal
    assert "const effectiveClientView=!context.is_admin||clientMode" in portal
    assert 'data-admin-results-heading' not in portal
    assert 'data-admin-result-filter' not in portal
    assert "classList.toggle('client-search-only',effectiveClientView)" in portal
    assert ".filters { display: grid; grid-template-columns: minmax(0, 1fr);" in css
    assert "if(!isPortalAdmin())return;renderGhlExport" not in portal

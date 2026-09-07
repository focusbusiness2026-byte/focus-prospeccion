from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.build_info import portal_build_id
from app.main import AutomationRequest, ResearchAdjustments, _client_execution_summary, _require_real_sheets


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
    assert 'Mueve un lead entre Nuevo, En revisión, Aprobado para descarga y Descartado.' in html
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


def test_kanban_drag_handle_moves_through_the_persisted_status_endpoint():
    html = Path('app/templates/portal.html').read_text(encoding='utf-8')

    assert 'class="crm-board-card" draggable="true" data-drag-prospect="${esc(item.execution_id)}"' in html
    assert 'class="crm-drag-handle" aria-hidden="true"' in html
    assert "addEventListener('dragstart'" in html
    assert "addEventListener('dragover'" in html
    assert "addEventListener('drop'" in html
    assert "addEventListener('dragend'" in html
    assert "isCrmInteractiveTarget" in html
    assert "crmMovesInFlight.has(id)" in html
    assert "moveCrmProspect(prospectId,column.dataset.crmColumn)" in html
    assert "fetch(`/api/prospects/${encodeURIComponent(id)}/status`" in html
    assert 'data-move-prospect="${esc(item.execution_id)}"' in html


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

    assert summary["status"] == "Revisión necesaria"
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

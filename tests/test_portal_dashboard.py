from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.main import AutomationRequest, ResearchAdjustments, _require_real_sheets


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
    assert 'automation-source-select' in html
    assert 'enabled:true' in html
    assert 'next_run_at' in html
    assert 'data-frequency-runs="8"' in html
    assert "['Nuevo','En revisión','Aprobado para descarga','Descartado']" in html
    assert 'Exportar para GoHighLevel' in html
    assert 'Exportar para Meta' in html
    assert 'dashboardData.sources?.[0]' not in html


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

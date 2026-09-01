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


def test_application_code_has_no_removed_local_experience_terms():
    app_files = list(Path('app').rglob('*'))
    content = '\n'.join(path.read_text(encoding='utf-8') for path in app_files if path.is_file() and path.suffix in {'.py', '.html', '.css', '.js'})
    forbidden = ('demo', 'paquete del cliente', 'calentamiento', 'vista previa')
    assert all(term not in content.lower() for term in forbidden)

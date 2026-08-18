from fastapi.testclient import TestClient
from pathlib import Path
import io
import json
import zipfile

from app.auth import SESSION_COOKIE, Identity, create_session
from app.config import get_settings
import pytest
from pydantic import ValidationError

from app.main import AutomationRequest, ResearchAdjustments, _admin_available_users, _build_client_package, _csv_cell, _demo_payload, app
from app.onboarding import OnboardingSource


def test_demo_dashboard_exercises_complete_portal_shape():
    payload = _demo_payload(Identity("demo@focus.local", "Administrador", "demo"), 500)

    assert payload["demo"] is True
    assert payload["metrics"]["total"] == 2
    assert len(payload["prospects"]) == 2
    assert len(payload["executions"]) == 2
    assert payload["source_metrics"] == {"total": 1, "ready": 1, "blocked": 0}
    assert payload["sources"][0]["onboarding_id"] == "ONB-DEMO0001"
    assert payload["prospects"][0]["evidence"]
    assert payload["global"]["openai_requests_remaining"] == 498
    assert payload["prospects"][0]["web_search_call_limit"] == 5
    assert payload["prospects"][0]["public_contacts"]


def test_demo_csv_export_is_downloadable_and_formula_safe():
    identity = Identity("demo@focus.local", "Administrador", "demo")
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, create_session(identity))

    response = client.get("/api/prospects/export.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == "attachment; filename=focus-prospeccion-leads.csv"
    assert "Estudio Horizonte" in response.text
    assert _csv_cell("=HYPERLINK(\"https://bad.example\")").startswith("'=")


def test_portal_uses_onboarding_sources_instead_of_manual_company_fields():
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, create_session(Identity("demo@focus.local", "Administrador", "demo")))

    response = client.get("/portal")

    assert response.status_code == 200
    assert 'id="source-list"' in response.text
    assert "La configuración recomendada se construye automáticamente con el Onboarding" in response.text
    assert "máximo 5 búsquedas" in response.text
    assert 'name="openai_api_key"' not in response.text
    assert 'id="scrape-form"' not in response.text
    assert 'id="recommended-config"' in response.text
    assert 'data-number="lead_count"' in response.text
    assert "Firmografía avanzada" in response.text
    assert "Madurez comercial y digital" in response.text
    assert "Decisores y empresas similares" in response.text
    assert 'id="warmup"' in response.text
    assert "11-4-7" in response.text
    assert 'class="site-header"' in response.text
    assert 'class="sidebar"' not in response.text
    assert 'data-view="sources"' in response.text
    assert '<a class="top-link" href="#method" data-view="method">' in response.text
    assert '<a class="top-link" href="#warmup" data-view="warmup">' in response.text
    assert 'data-schedule-enabled' in response.text
    assert 'data-builder-tab="profile"' in response.text
    assert 'id="automation-preview"' in response.text
    assert "PROGRAMACIÓN OPERATIVA" in response.text
    assert "Acciones reales" in response.text
    assert 'id="preview-countdown"' in response.text
    assert "focus-prospeccion:automation-preview:v1" not in response.text
    assert 'id="dashboard-automation-quick"' in response.text
    assert 'class="dashboard-automation-dock"' in response.text
    assert 'id="favorite-automation-select"' in response.text
    assert 'id="dashboard-automation-filters"' in response.text
    assert 'id="quick-toggle-automation"' in response.text
    assert 'id="module-info-dialog"' in response.text
    assert response.text.count('class="info-button') >= 7
    assert 'data-schedule-name' in response.text
    assert "Nombre del guardado" in response.text
    assert "Automatizaciones reales" in response.text
    assert "Hasta 5 resultados" in response.text
    assert 'max="5" data-number="lead_count"' in response.text
    assert 'id="crm-presentation"' in response.text
    assert "Tablero · columnas Kanban" in response.text
    assert "Tarjetas · estilo Trello" in response.text
    assert "Tabla · hoja de cálculo" in response.text
    assert 'id="crm-preview-board"' in response.text
    assert 'id="crm-preview-table"' in response.text
    assert "data-copy-value" in response.text
    assert "draggable=\"true\"" in response.text
    assert "crm-drag-handle" in response.text
    assert "drop-target" in response.text
    assert "draggedProspectId" in response.text
    assert 'id="warmup-action-form"' in response.text
    assert "Email con consentimiento" in response.text
    assert "Tarea manual · LinkedIn" in response.text
    assert "Cómo se conectará GoHighLevel" in response.text
    assert "flujo oficial de autorización de GoHighLevel" in response.text
    assert "PhantomBuster" in response.text
    assert "Descargar audiencia para Meta" in response.text
    assert 'id="audience-legal-confirmation"' in response.text
    assert 'id="briefing-dialog"' in response.text
    assert "No llama a servicios externos" in response.text
    assert 'id="codex-delivery"' in response.text
    assert 'id="download-ghl-contacts"' in response.text
    assert "Mapeo para importar contactos a GoHighLevel" in response.text
    assert 'id="preview-ghl-master"' in response.text
    assert 'id="ghl-master-preview"' in response.text
    assert "CARPETA MAESTRA GOHIGHLEVEL" in response.text
    assert "08_INSTRUCCIONES_EQUIPO_TECNICO.txt" in response.text
    assert "NO ejecutes ciegamente" in response.text
    assert 'id="demo-role"' in response.text
    assert 'id="demo-role-email"' in response.text
    assert "const sessionEmail=" in response.text
    assert "const sessionRole=" in response.text
    assert "sessionEmail===authorizedDeliveryEmail&&sessionRole.includes('admin')" in response.text
    assert "servicemanagerbossio@gmail.com" in response.text
    assert "previewEmail()===authorizedDeliveryEmail" in response.text
    assert "Administrador" in response.text
    assert "Cliente" in response.text
    assert 'id="admin-lead-delivery-dialog"' in response.text
    assert "sesión real, correo autorizado y permisos del servidor" in response.text
    assert "lead-focus-mark" in response.text
    assert "Descargar paquete del cliente" in response.text
    assert "Paquete del cliente" in response.text
    assert ">Radar</a>" in response.text

    source_response = client.get("/api/onboarding-sources/ONB-DEMO0001")
    assert source_response.status_code == 200
    assert source_response.json()["source"]["productora"]["email"] == "demo@focus.local"


def test_internal_onboarding_trigger_is_closed_without_server_secret():
    client = TestClient(app)

    response = client.post("/api/internal/onboarding-trigger", json={"onboarding_id": "ONB-DEMO0001"})

    assert response.status_code == 503
    assert "no está configurado" in response.json()["detail"]


def test_production_portal_redirects_to_shared_email_password_access(monkeypatch):
    monkeypatch.setenv("CENTRAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("CENTRAL_AUTH_URL", "https://onboarding.focusbusinesslab.es")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://prospeccion.focusbusinesslab.es")
    get_settings.cache_clear()
    try:
        client = TestClient(app, follow_redirects=False)
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"].startswith("https://onboarding.focusbusinesslab.es/access?return_to=")
        assert "prospeccion.focusbusinesslab.es" in response.headers["location"]
    finally:
        get_settings.cache_clear()


def test_professional_research_configuration_limits_requested_leads():
    assert ResearchAdjustments(lead_count=5).lead_count == 5
    with pytest.raises(ValidationError):
        ResearchAdjustments(lead_count=6)


def test_automation_interval_is_limited_between_five_minutes_and_three_days():
    assert AutomationRequest(name="Diaria", enabled=True, interval_minutes=5).interval_minutes == 5
    assert AutomationRequest(name="Semanal", enabled=True, interval_minutes=4320).interval_minutes == 4320
    with pytest.raises(ValidationError):
        AutomationRequest(name="Inválida", enabled=True, interval_minutes=4)
    with pytest.raises(ValidationError):
        AutomationRequest(name="Inválida", enabled=True, interval_minutes=4321)
    with pytest.raises(ValidationError):
        AutomationRequest(name="", enabled=False)


def test_hidden_demo_badge_cannot_be_overridden_by_badge_display_rule():
    css = (Path(__file__).parents[1] / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert "[hidden] { display: none !important; }" in css


def test_google_sign_in_keeps_official_flow_inside_dark_responsive_frame():
    root = Path(__file__).parents[1]
    html = (root / "app" / "templates" / "login.html").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert "google.accounts.id.initialize" in html
    assert "callback:response => postLogin('/auth/google'" in html
    assert "theme:'filled_black'" in html
    assert "type:'icon'" in html
    assert "size:'large'" in html
    assert "shape:'circle'" in html
    assert 'class="google-access-control"' in html
    assert "Continuar con Google" in html
    assert "#google-button" in css
    assert "overflow: hidden" in css
    assert "border-radius: 50%" in css
    assert "justify-content: center" in css
    assert ".google-access-control" in css
    assert "padding: 0" in css
    assert "Continuar con correo" in html
    assert "No se ha enviado ningún email" in html
    assert "postLogin('/auth/demo')" in html
    assert "#google-button iframe" in css


def test_desktop_navigation_uses_two_rows_without_single_item_more_menu():
    root = Path(__file__).parents[1]
    html = (root / "app" / "templates" / "portal.html").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert html.count('class="top-link') == 9
    assert 'grid-template-columns: repeat(5, minmax(92px, 1fr))' in css
    assert 'grid-auto-rows: 42px' in css
    assert '<summary>MÃ¡s</summary>' not in html
    assert '>Paquete del cliente</a>' in html


def test_admin_client_selector_combines_access_and_registered_onboarding_accounts():
    class Store:
        def access_records(self):
            return [
                type("Access", (), {"email": "active@example.com", "role": "Cliente", "state": "Activo"})(),
                type("Access", (), {"email": "admin@example.com", "role": "Administrador", "state": "Activo"})(),
            ]

    sources = [
        type("Source", (), {"email": "registered@example.com"})(),
        type("Source", (), {"email": "registered@example.com"})(),
        type("Source", (), {"email": "active@example.com"})(),
    ]

    users = _admin_available_users(Store(), sources)

    assert users == [
        {"email": "active@example.com", "role": "Cliente", "onboarding_count": 1},
        {"email": "registered@example.com", "role": "Cliente registrado", "onboarding_count": 2},
    ]


def test_portal_identifies_client_and_administrator_views_visually():
    root = Path(__file__).parents[1]
    portal = (root / "app" / "templates" / "portal.html").read_text(encoding="utf-8")
    css = (root / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert 'id="portal-role-banner"' in portal
    assert 'id="portal-role-title">Vista de cliente' in portal
    assert "Vista administrativa · revisión como cliente" in portal
    assert "Cuenta revisada:" in portal
    assert "ADMIN + CLIENTE" in portal
    assert "Vista administrativa · demostración" in portal
    assert "Acceso limitado a sus propios datos" in portal
    assert ".portal-role-banner.admin" in css
    assert ".portal-role-banner.client" in css


def test_prospecting_filter_grid_can_shrink_without_overlapping_summary_panel():
    css = (Path(__file__).parents[1] / "app" / "static" / "app.css").read_text(encoding="utf-8")
    portal = (Path(__file__).parents[1] / "app" / "templates" / "portal.html").read_text(encoding="utf-8")

    assert ".research-builder { display: grid; min-width: 0; grid-template-columns: minmax(0, 1fr);" in css
    assert ".research-builder > * { min-width: 0; }" in css
    assert ".builder-card { min-width: 0;" in css
    assert ".professional-grid { display: grid; min-width: 0;" in css
    assert ".professional-grid .input input," in css
    assert ".professional-grid .input textarea { min-width: 0; }" in css
    assert "@media (max-width: 1380px)" in css
    assert '/static/app.css?v=20260818-1' in portal


def test_real_automation_controls_and_admin_execution_visibility_are_present():
    portal = (Path(__file__).parents[1] / "app" / "templates" / "portal.html").read_text(encoding="utf-8")
    css = (Path(__file__).parents[1] / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert "persistAutomation" in portal
    assert "runSavedAutomation" in portal
    assert 'id="quick-run-automation"' in portal
    assert 'class="admin-execution-column" hidden' in portal
    assert "Tu historial excluye las ejecuciones internas realizadas por administración" in portal
    assert "Sin límite" in portal
    assert "@media (max-width: 1180px)" in css
    assert ".studio-toolbar-actions { width: 100%; min-width: 0; grid-template-columns: 1fr; }" in css


def test_client_package_contains_confirmed_form_data_and_excludes_secrets():
    source = OnboardingSource.from_sheet_record({
        "ID registro": "ONB-CLIENTE-1",
        "Empresa": "Cliente Ejemplo",
        "Email responsable": "cliente@example.com",
        "Web": "https://example.com",
        "Actividad": "Servicios B2B",
        "Servicio prioritario": "ConsultorÃ­a",
        "Sectores": "TecnologÃ­a",
        "PaÃ­ses objetivo": "EspaÃ±a",
        "Tipos de cliente objetivo": "Empresa B2B",
        "Perfil ideal detallado": "Equipos de 10 a 50 personas",
        "AutorizaciÃ³n": "SÃ­",
        "Campo pendiente": "",
        "Token API": "no-debe-salir",
        "ContraseÃ±a": "no-debe-salir",
    })

    payload = _build_client_package(source, {"execution_id": "LEAD-1", "company": "Lead Ejemplo"})

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert names == {
            "README.txt",
            "datos_formulario.json",
            "perfil_normalizado.json",
            "datos_lead.json",
            "INSTRUCCIONES_EQUIPO_TECNICO.txt",
            "CAMPOS_FALTANTES.txt",
        }
        form = json.loads(archive.read("datos_formulario.json"))
        assert form["Empresa"] == "Cliente Ejemplo"
        assert "Token API" not in form
        assert "ContraseÃ±a" not in form
        assert "Campo pendiente" in archive.read("CAMPOS_FALTANTES.txt").decode("utf-8")
        all_text = "\n".join(
            archive.read(name).decode("utf-8")
            for name in names
            if name.endswith((".txt", ".json"))
        )
        assert "Codex" not in all_text
        assert "equipo" in all_text.lower()

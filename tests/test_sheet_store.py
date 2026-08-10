import pytest

from app.config import Settings
from app.sheet_store import AUTOMATION_HEADERS, DASHBOARD_HEADERS, EXECUTION_HEADERS, PROSPECT_HEADERS, SheetStore


class FakeStore(SheetStore):
    def __init__(self):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.rows = [[" USER@example.com\n", "Administrador", "Activo", "", 2, 1, 1, 0.5, "Atención"]]
        self.updates = []

    def _get(self, a1_range):
        if "Accesos" in a1_range:
            return self.rows
        return []

    def _update(self, a1_range, values):
        self.updates.append((a1_range, values))
        self.rows[0][5] = values[0][0]


def test_access_is_normalized_and_quota_is_reserved_once():
    store = FakeStore()

    record = store.reserve_execution("user@example.com")

    assert record.available == 0
    assert store.updates == [("'Accesos'!F2", [[2]])]
    with pytest.raises(RuntimeError, match="No quedan"):
        store.reserve_execution("user@example.com")


def test_inactive_or_missing_email_is_rejected():
    store = FakeStore()

    with pytest.raises(PermissionError):
        store.reserve_execution("missing@example.com")


class ProspectStore(SheetStore):
    def __init__(self):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.prospects = [
            ["exec-1", "owner@example.com", "2026-08-10T10:00:00Z", "Empresa Uno", "https://uno.example", "Titulo", "Descripcion", "Tecnologia", "B2B", "Madrid", 25, 8.2, "green", "Buen encaje", "Hablar de crecimiento", "https://linkedin.com/company/uno", "", "", "", "", "", "https://uno.example\nhttps://uno.example/contacto", "Nuevo", "2026-08-10T10:00:00Z"],
            ["exec-2", "other@example.com", "2026-08-09T10:00:00Z", "Empresa Dos", "https://dos.example", "", "", "Retail", "B2C", "Valencia", 8, 3.4, "red", "Encaje bajo", "", "", "", "", "", "", "", "https://dos.example", "Descartado", "2026-08-09T10:00:00Z"],
        ]
        self.executions = [["exec-1", "2026-08-10T10:00:00Z", "owner@example.com", "Empresa Uno", "https://uno.example", "Completado", "gemini", 10, 4, 14, ""]]
        self.updates = []

    def _get(self, a1_range):
        if "Prospeccion" in a1_range:
            return self.prospects
        if "Ejecuciones" in a1_range:
            return self.executions
        return []

    def _update(self, a1_range, values):
        self.updates.append((a1_range, values))


def test_recent_prospects_are_scoped_and_include_full_detail():
    store = ProspectStore()

    result = store.recent_prospects("owner@example.com")

    assert len(result) == 1
    assert result[0]["company"] == "Empresa Uno"
    assert result[0]["social_links"]["linkedin"] == "https://linkedin.com/company/uno"
    assert result[0]["evidence"] == ["https://uno.example", "https://uno.example/contacto"]
    assert result[0]["lead_status"] == "Nuevo"


def test_admin_can_change_status_and_metrics_are_calculated():
    store = ProspectStore()

    updated = store.update_prospect_status("exec-2", "admin@example.com", "Aprobado", is_admin=True)
    metrics = store.prospect_metrics(None)

    assert updated["lead_status"] == "Aprobado"
    assert store.updates[0][0] == "'Prospeccion'!W3:X3"
    assert metrics["total"] == 2
    assert metrics["classifications"] == {"green": 1, "yellow": 0, "red": 1}


def test_client_cannot_change_another_accounts_lead():
    store = ProspectStore()

    with pytest.raises(PermissionError, match="otra cuenta"):
        store.update_prospect_status("exec-2", "owner@example.com", "Aprobado")


class AppendStore(SheetStore):
    def __init__(self):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.appends = []

    def _append(self, a1_range, values):
        self.appends.append((a1_range, values))


def test_results_keep_the_onboarding_productora_link():
    store = AppendStore()

    store.append_prospect(
        {
            "execution_id": "exec-onb",
            "email": "owner@example.com",
            "company": "Prospecto Uno",
            "website": "https://prospecto.example",
            "onboarding_id": "ONB-UNO",
            "productora": "Productora Norte",
        }
    )
    store.append_execution(
        execution_id="exec-onb",
        email="owner@example.com",
        company="Prospecto Uno",
        website="https://prospecto.example",
        status="Completado",
        model="fixture",
        prompt_tokens=0,
        output_tokens=0,
        total_tokens=0,
        onboarding_id="ONB-UNO",
        productora="Productora Norte",
    )

    assert store.appends[0][0] == "'Prospeccion'!A:AS"
    assert store.appends[0][1][0][24:26] == ["ONB-UNO", "Productora Norte"]
    assert store.appends[1][0] == "'Ejecuciones'!A:X"
    assert store.appends[1][1][0][11:13] == ["ONB-UNO", "Productora Norte"]
    assert store.appends[1][1][0][21] == "OpenAI Responses API + web_search"


def test_crm_fields_are_persisted_in_the_extended_columns():
    store = ProspectStore()

    updated = store.update_prospect_crm(
        "exec-1", "owner@example.com", status="Aprobado", owner="Alberto",
        notes="Revisado", next_action="Llamar", follow_up_date="2026-08-20",
        warmup_preparation="Preparada", warmup_approval="Pendiente",
    )

    assert store.updates[0][0] == "'Prospeccion'!W2:X2"
    assert store.updates[1] == ("'Prospeccion'!AI2:AL2", [["Alberto", "Revisado", "Llamar", "2026-08-20"]])
    assert store.updates[2] == ("'Prospeccion'!AR2:AS2", [["Preparada", "Pendiente"]])
    assert updated["crm_owner"] == "Alberto"
    assert updated["warmup_preparation"] == "Preparada"


class HeaderStore(SheetStore):
    def __init__(self, headers_by_tab):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.headers_by_tab = headers_by_tab
        self.updates = []

    def _sheet_properties(self):
        return [
            {"sheetId": index, "title": tab, "gridProperties": {"columnCount": max(len(headers), 100)}}
            for index, (tab, headers) in enumerate(self.headers_by_tab.items(), start=1)
        ]

    def _get(self, a1_range):
        tab = a1_range.split("'!")[0].strip("'")
        return [self.headers_by_tab.get(tab, [])]

    def _update(self, a1_range, values):
        self.updates.append((a1_range, values))


def test_schema_migration_only_appends_missing_trailing_headers():
    store = HeaderStore({
        "Prospeccion": PROSPECT_HEADERS,
        "Ejecuciones": EXECUTION_HEADERS[:21],
        "Automatizaciones": AUTOMATION_HEADERS,
        "Dashboard Prospeccion": [],
    })

    store.ensure_operational_schema()

    assert store.updates[0] == ("'Ejecuciones'!V1:X1", [EXECUTION_HEADERS[21:]])
    assert len(store.updates) == 1


def test_schema_migration_renames_legacy_gemini_header_without_touching_rows():
    legacy_headers = EXECUTION_HEADERS[:]
    legacy_headers[6] = "gemini_model"
    store = HeaderStore({"Prospeccion": PROSPECT_HEADERS, "Ejecuciones": legacy_headers, "Automatizaciones": AUTOMATION_HEADERS})

    store.ensure_operational_schema()

    assert store.updates == [("'Ejecuciones'!G1", [["model"]])]


class CapacityStore(SheetStore):
    def __init__(self):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.properties = [
            {"sheetId": 10, "title": "Prospeccion", "gridProperties": {"columnCount": 24}},
            {"sheetId": 11, "title": "Ejecuciones", "gridProperties": {"columnCount": 11}},
        ]
        self.requests = []

    def _sheet_properties(self):
        return self.properties

    def _batch_update(self, requests):
        self.requests.extend(requests)


def test_sheet_capacity_expands_existing_tabs_without_recreating_them():
    store = CapacityStore()

    store._ensure_sheet_capacity("Prospeccion", len(PROSPECT_HEADERS))
    store._ensure_sheet_capacity("Ejecuciones", len(EXECUTION_HEADERS))

    assert store.requests == [
        {"appendDimension": {"sheetId": 10, "dimension": "COLUMNS", "length": 21}},
        {"appendDimension": {"sheetId": 11, "dimension": "COLUMNS", "length": 13}},
    ]


class AutomationStore(SheetStore):
    def __init__(self, rows=None):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.rows = rows or []
        self.appends = []
        self.updates = []

    def _get(self, a1_range):
        return self.rows if "Automatizaciones" in a1_range else []

    def _append(self, a1_range, values):
        self.appends.append((a1_range, values))

    def _update(self, a1_range, values):
        self.updates.append((a1_range, values))


def test_automation_schedule_persists_filters_and_clamps_interval():
    store = AutomationStore()

    result = store.upsert_automation_config(
        "ONB-001",
        "Owner@Example.com",
        enabled=True,
        interval_minutes=4,
        adjustments={"lead_count": 12, "sectors": ["Tecnología"]},
    )

    assert result["enabled"] is True
    assert result["interval_minutes"] == 5
    assert result["adjustments"]["lead_count"] == 12
    assert store.appends[0][0] == "'Automatizaciones'!A:J"
    assert store.appends[0][1][0][1] == "owner@example.com"


def test_schema_migration_rejects_reordered_existing_columns():
    store = HeaderStore({"Prospeccion": ["email", "execution_id"]})

    with pytest.raises(RuntimeError, match="no coinciden"):
        store._ensure_header_row("Prospeccion", PROSPECT_HEADERS)

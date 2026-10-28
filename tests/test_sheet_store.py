from datetime import datetime, timedelta, timezone
import re

import pytest

from app.config import Settings
from app.sheet_store import (
    AUTOMATION_HEADERS,
    DASHBOARD_HEADERS,
    EXECUTION_HEADERS,
    PROSPECT_HEADERS,
    SAVED_SEARCH_HEADERS,
    SheetStore,
    TransientSheetWriteError,
)


class FakeStore(SheetStore):
    def __init__(self):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        current_cycle = datetime.now(timezone.utc).date().isoformat()
        self.rows = [[" USER@example.com\n", "Cliente", "Activo", "", 50, 49, 1, 0.02, "Crítico", current_cycle]]
        self.updates = []

    def _get(self, a1_range):
        if "Accesos" in a1_range:
            return self.rows
        return []

    def _update(self, a1_range, values):
        self.updates.append((a1_range, values))
        if ":J" in a1_range:
            self.rows[0][4:10] = values[0]
        else:
            self.rows[0][5] = values[0][0]


def test_dashboard_read_cache_retries_quota_response_and_reuses_same_range(monkeypatch):
    store = SheetStore(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
    statuses = iter([429, 200])
    calls = []
    delays = []

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code != 200:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return {"values": [["lead-1"]]}

    class Session:
        def get(self, url, timeout):
            calls.append(url)
            return Response(next(statuses))

    monkeypatch.setattr(store, "_session", lambda: Session())
    monkeypatch.setattr("app.sheet_store.time.sleep", delays.append)
    store.enable_read_cache()
    assert store._get("'Prospeccion'!A2:AT1000") == [["lead-1"]]
    assert store._get("'Prospeccion'!A2:AT1000") == [["lead-1"]]
    assert len(calls) == 2
    assert delays == [1.5]


def test_access_is_normalized_and_quota_is_consumed_after_one_successful_scrape():
    store = FakeStore()
    store.rows[0][5] = 0

    record = store.consume_successful_scrape("user@example.com")

    assert record.available == 49
    assert store.updates == [("'Accesos'!F2", [[1]])]
    store.rows[0][5] = 50
    with pytest.raises(PermissionError, match="Límite de raspados alcanzado"):
        store.check_scrape_limit("user@example.com")


def test_limit_check_does_not_consume_quota_before_scrape():
    store = FakeStore()
    store.rows[0][5] = 0

    record = store.check_scrape_limit("user@example.com")

    assert record.used == 0
    assert store.updates == []


def test_linked_client_uses_existing_account_quota_without_a_second_allowance():
    store = FakeStore()
    store.rows[0][5] = 0
    store.rows.append([
        "teammate@example.com", "Cliente", "Activo", "", 0, 0, "", "", "", "",
        "Permitido", "Bloqueado", "user@example.com",
    ])

    teammate = store.get_access("teammate@example.com")
    assert teammate is not None
    assert teammate.email == "teammate@example.com"
    assert teammate.account_email == "user@example.com"
    assert store.check_scrape_limit(teammate.email).row == 2
    assert store.consume_successful_scrape(teammate.email).used == 1
    assert store.updates == [("'Accesos'!F2", [[1]])]
    assert store.rows[1][5] == 0


def test_linked_client_fails_closed_if_owner_is_inactive_or_admin():
    store = FakeStore()
    store.rows.append([
        "teammate@example.com", "Cliente", "Activo", "", 0, 0, "", "", "", "",
        "Permitido", "Bloqueado", "user@example.com",
    ])
    store.rows[0][2] = "Inactivo"
    assert store.get_access("teammate@example.com") is None
    store.rows[0][2] = "Activo"
    store.rows[0][1] = "Administrador"
    assert store.get_access("teammate@example.com") is None


def test_quota_reconciles_successful_executions_from_the_current_renewal_cycle():
    class ReconciledStore(FakeStore):
        def _get(self, a1_range):
            if "Accesos" in a1_range:
                return self.rows
            if "Ejecuciones" in a1_range:
                admin_run = ["admin-run", "2026-09-08T11:00:00Z", "user@example.com", "Cliente", "", "Completado"] + [""] * 19 + ["Administrador", "manual"]
                return [
                    ["same-month-before-renewal", "2026-09-06T23:59:59Z", "user@example.com", "Cliente", "", "Completado"],
                    ["ok-1", "2026-09-07T00:00:00Z", "user@example.com", "Cliente", "", "Completado"],
                    ["ok-1", "2026-09-07T00:00:00Z", "user@example.com", "Cliente", "", "Completado"],
                    ["failed", "2026-09-07T10:00:00Z", "user@example.com", "Cliente", "", "Fallido"],
                    ["ok-2", "2026-09-08T09:00:00Z", "user@example.com", "Cliente", "", "Completado sin prospectos"],
                    admin_run,
                ]
            return []

    store = ReconciledStore()
    store.rows[0][5] = 0
    store.rows[0][9] = "2026-09-07"

    record = store.check_scrape_limit("user@example.com")

    assert record.used == 3
    assert record.available == 47
    assert store.updates == []


def test_quota_reconciliation_accepts_spanish_formatted_renewal_date():
    class ReconciledStore(FakeStore):
        def _get(self, a1_range):
            if "Accesos" in a1_range:
                return self.rows
            if "Ejecuciones" in a1_range:
                return [["ok-1", "2026-09-08T09:00:00Z", "user@example.com", "Cliente", "", "Completado"]]
            return []

    store = ReconciledStore()
    store.rows[0][5] = 0
    store.rows[0][9] = "7/09/2026"

    record = store.check_scrape_limit("user@example.com")

    assert record.used == 1
    assert record.available == 49


def test_recent_executions_deduplicates_execution_ids_and_keeps_latest_row():
    class ExecutionStore(FakeStore):
        def _get(self, a1_range):
            if "Ejecuciones" in a1_range:
                return [
                    ["same-id", "2026-09-08T08:00:00Z", "user@example.com", "", "", "Pendiente"],
                    ["same-id", "2026-09-08T09:00:00Z", "user@example.com", "", "", "Completado"],
                    ["other-id", "2026-09-08T10:00:00Z", "user@example.com", "", "", "Fallido"],
                ]
            return super()._get(a1_range)

    executions = ExecutionStore().recent_executions("user@example.com")

    assert [item["execution_id"] for item in executions] == ["other-id", "same-id"]
    assert executions[1]["status"] == "Completado"


def test_next_successful_scrape_persists_reconciled_usage_plus_one():
    class ReconciledStore(FakeStore):
        def _get(self, a1_range):
            if "Accesos" in a1_range:
                return self.rows
            if "Ejecuciones" in a1_range:
                return [
                    ["ok-1", "2026-09-07T09:00:00Z", "user@example.com", "Cliente", "", "Completado"],
                    ["ok-2", "2026-09-08T09:00:00Z", "user@example.com", "Cliente", "", "Completado"],
                ]
            return []

    store = ReconciledStore()
    store.rows[0][5] = 0
    store.rows[0][9] = "2026-09-07"

    record = store.consume_successful_scrape("user@example.com")

    assert record.used == 3
    assert store.updates == [("'Accesos'!F2", [[3]])]


def test_access_read_preserves_the_current_sheet_quota_without_rewriting_it():
    store = FakeStore()
    store.rows[0][5] = 50
    store.rows[0][9] = "2025-01-01"

    record = store.get_access("user@example.com")

    assert record is not None
    assert record.assigned == 50
    assert record.used == 50
    assert record.available == 0
    assert store.updates == []


def test_unlimited_assignment_records_successful_scrapes_without_enforcing_a_limit():
    store = FakeStore()
    store.rows[0][1] = "Administrador"
    store.rows[0][4] = "Ilimitado"
    store.rows[0][5] = 999
    store.updates.clear()

    record = store.consume_successful_scrape("user@example.com")

    assert record.role == "Administrador"
    assert record.used == 1000
    assert store.updates == [("'Accesos'!F2", [[1000]])]


def test_administrator_with_a_numeric_assignment_consumes_one_scrape():
    store = FakeStore()
    store.rows[0][1] = "Administrador"
    store.rows[0][4] = 50
    store.rows[0][5] = 0

    record = store.consume_successful_scrape("user@example.com")

    assert record.used == 1
    assert store.updates == [("'Accesos'!F2", [[1]])]


def test_inactive_or_missing_email_is_rejected():
    store = FakeStore()

    with pytest.raises(PermissionError):
        store.check_scrape_limit("missing@example.com")


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

    updated = store.update_prospect_status("exec-2", "admin@example.com", "Aprobado para descarga", is_admin=True)
    metrics = store.prospect_metrics(None)

    assert updated["lead_status"] == "Aprobado para descarga"
    assert store.updates[0][0] == "'Prospeccion'!W3:X3"
    assert metrics["total"] == 2
    assert metrics["classifications"] == {"green": 1, "yellow": 0, "red": 1}


def test_admin_can_persist_a_safe_custom_kanban_column():
    store = ProspectStore()

    updated = store.update_prospect_status("exec-2", "admin@example.com", "Revisión comercial", is_admin=True)

    assert updated["lead_status"] == "Revisión comercial"
    assert store.updates[0][0] == "'Prospeccion'!W3:X3"


def test_client_cannot_change_another_accounts_lead():
    store = ProspectStore()

    with pytest.raises(PermissionError, match="otra cuenta"):
        store.update_prospect_status("exec-2", "owner@example.com", "Aprobado para descarga")


def test_kanban_status_write_retries_a_transient_database_lock(monkeypatch):
    store = ProspectStore()
    attempts = 0
    original_update = store._update

    def locked_once(a1_range, values):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database is locked")
        original_update(a1_range, values)

    monkeypatch.setattr(store, "_update", locked_once)
    monkeypatch.setattr("app.sheet_store.time.sleep", lambda _: None)

    updated = store.update_prospect_status("exec-2", "admin@example.com", "Aprobado para descarga", is_admin=True)

    assert updated["lead_status"] == "Aprobado para descarga"
    assert attempts == 2
    assert len(store.updates) == 1


def test_kanban_status_write_returns_controlled_error_after_retries_exhaust(monkeypatch):
    store = ProspectStore()
    attempts = 0

    def always_locked(*_):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "_update", always_locked)
    monkeypatch.setattr("app.sheet_store.time.sleep", lambda _: None)

    with pytest.raises(TransientSheetWriteError, match="Google Sheets está ocupado"):
        store.update_prospect_status("exec-2", "admin@example.com", "Aprobado para descarga", is_admin=True)
    assert attempts == 3


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
            "dossier": {"identification": [{"text": "Empresa activa", "date": "2026-10-28", "source_url": "https://prospecto.example", "certainty": "confirmed"}]},
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

    assert store.appends[0][0] == "'Prospeccion'!A:AT"
    assert store.appends[0][1][0][24:26] == ["ONB-UNO", "Productora Norte"]
    assert SheetStore._prospect_from_row(store.appends[0][1][0])["dossier"]["identification"][0]["text"] == "Empresa activa"
    assert store.appends[1][0] == "'Ejecuciones'!A:AA"
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

    assert store.updates[0] == ("'Ejecuciones'!V1:AA1", [EXECUTION_HEADERS[21:]])
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
        {"appendDimension": {"sheetId": 10, "dimension": "COLUMNS", "length": 22}},
        {"appendDimension": {"sheetId": 11, "dimension": "COLUMNS", "length": 16}},
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
        name="Tecnología · Madrid",
        favorite=True,
        created_by_email="admin@example.com",
        created_by_role="Administrador",
    )

    assert result["enabled"] is True
    assert result["interval_minutes"] == 5
    assert result["adjustments"]["lead_count"] == 5
    assert result["name"] == "Tecnología · Madrid"
    assert result["favorite"] is True
    assert store.appends[0][0] == "'Automatizaciones'!A:N"
    assert store.appends[0][1][0][1] == "owner@example.com"


def test_automation_schedule_accepts_one_week_interval():
    store = AutomationStore()
    result = store.upsert_automation_config(
        "ONB-001",
        "owner@example.com",
        enabled=False,
        interval_minutes=10080,
        adjustments={"lead_count": 5},
        name="Semanal",
        favorite=False,
        created_by_email="admin@example.com",
        created_by_role="Administrador",
    )
    assert result["interval_minutes"] == 10080


class StatefulAutomationStore(AutomationStore):
    def _append(self, a1_range, values):
        super()._append(a1_range, values)
        self.rows.append(list(values[0]))

    def _update(self, a1_range, values):
        super()._update(a1_range, values)
        start = a1_range.split("!")[1].split(":")[0][0]
        offset = ord(start) - ord("A")
        self.rows[0][offset:offset + len(values[0])] = values[0]


def test_bounded_batch_claim_five_successes_and_no_overlap():
    store = StatefulAutomationStore()
    saved = store.upsert_automation_config(
        "ONB-001", "owner@example.com", enabled=True, interval_minutes=5,
        runs_per_cycle=1, max_total_runs=5,
    )
    assert saved["batch"]["completed_runs"] == 0
    assert store.due_automation_configs() == []
    for number in range(1, 6):
        store.rows[0][4] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        due = store.due_automation_configs()[0]
        claim = store.claim_bounded_automation_run(due)
        assert claim["batch"]["in_flight"] is True
        assert store.rows[0][2] is False
        assert store.rows[0][4] == ""
        assert store.due_automation_configs() == []
        assert store.claim_bounded_automation_run(due) is None
        # A restarted scheduler sees the same paused, in-flight state.
        restarted = StatefulAutomationStore(store.rows)
        assert restarted.due_automation_configs() == []
        store.finish_bounded_automation_run(
            claim, result={"execution_id": f"run-{number}", "prospects": []},
        )
        current = store.get_automation_config("ONB-001")
        assert current["batch"]["completed_runs"] == number
        assert current["batch"]["execution_ids"][-1] == f"run-{number}"
        assert current["enabled"] is (number < 5)
        assert bool(current["next_run_at"]) is (number < 5)
    assert store.due_automation_configs() == []
    assert "5/5" in store.get_automation_config("ONB-001")["last_status"]


def test_bounded_batch_failure_stays_paused_and_preserves_progress():
    store = StatefulAutomationStore()
    store.upsert_automation_config("ONB-001", "owner@example.com", enabled=True,
                                   interval_minutes=5, max_total_runs=5)
    store.rows[0][4] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    claim = store.claim_bounded_automation_run(store.due_automation_configs()[0])
    store.finish_bounded_automation_run(claim, error="Fallo de prueba")
    current = store.get_automation_config("ONB-001")
    assert not current["enabled"]
    assert current["next_run_at"] == ""
    assert current["batch"]["completed_runs"] == 0
    assert current["batch"]["error"] == "Fallo de prueba"
    assert store.due_automation_configs() == []


def test_bounded_batch_manual_pause_preserves_batch_metadata():
    store = StatefulAutomationStore()
    store.upsert_automation_config("ONB-001", "owner@example.com", enabled=True,
                                   interval_minutes=5, max_total_runs=5)
    before = store.get_automation_config("ONB-001")
    after = store.pause_bounded_automation(before)
    assert not after["enabled"]
    assert after["batch"] == before["batch"]
    assert store.get_automation_config("ONB-001")["batch"]["id"] == before["batch"]["id"]
    assert store.due_automation_configs() == []


def test_bounded_batch_requires_single_run_per_cycle():
    store = StatefulAutomationStore()
    with pytest.raises(ValueError, match="una ejecución por ciclo"):
        store.upsert_automation_config("ONB-001", "owner@example.com", enabled=True,
                                       interval_minutes=5, runs_per_cycle=2, max_total_runs=5)
    assert store.rows == []


class SavedSearchStore(SheetStore):
    def __init__(self, rows=None):
        super().__init__(Settings(google_sheet_id="sheet", google_service_account_json="{}"))
        self.rows = rows or []
        self.appends = []
        self.updates = []

    def _sheet_properties(self):
        return [{"sheetId": 14, "title": "Busquedas Guardadas", "gridProperties": {"columnCount": 10}}]

    def _get(self, a1_range):
        if "A1:J1" in a1_range:
            return [SAVED_SEARCH_HEADERS]
        return self.rows if "Busquedas Guardadas" in a1_range else []

    def _append(self, a1_range, values):
        self.appends.append((a1_range, values))
        self.rows.extend(values)

    def _update(self, a1_range, values):
        self.updates.append((a1_range, values))
        match = re.search(r"(?:[A-Z]+)(\d+)(?::[A-Z]+\d+)?$", a1_range)
        if not match:
            return
        row_index = int(match.group(1)) - 2
        if row_index < 0 or row_index >= len(self.rows):
            return
        if "!E" in a1_range and ":" not in a1_range:
            self.rows[row_index][4] = values[0][0]
        else:
            self.rows[row_index] = values[0]


def test_saved_searches_preserve_multiple_named_options_and_one_favorite():
    store = SavedSearchStore()

    first = store.upsert_saved_search(
        "ONB-001", "Owner@Example.com", name="Madrid B2B", adjustments={"sectors": ["Tecnología"]},
        favorite=True, created_by_email="owner@example.com", created_by_role="Cliente",
    )
    second = store.upsert_saved_search(
        "ONB-001", "owner@example.com", name="Portugal B2C", adjustments={"target_countries": ["Portugal"]},
        favorite=True, created_by_email="owner@example.com", created_by_role="Cliente",
    )

    saved = store.saved_searches("owner@example.com", "ONB-001")
    assert {item["name"] for item in saved} == {"Madrid B2B", "Portugal B2C"}
    assert [item["name"] for item in saved if item["favorite"]] == ["Portugal B2C"]
    assert first["saved_search_id"] != second["saved_search_id"]


def test_saved_search_can_be_deleted_without_touching_other_options():
    store = SavedSearchStore()
    first = store.upsert_saved_search(
        "ONB-001", "owner@example.com", name="Opción uno", adjustments={}, favorite=False,
        created_by_email="owner@example.com", created_by_role="Cliente",
    )
    second = store.upsert_saved_search(
        "ONB-001", "owner@example.com", name="Opción dos", adjustments={}, favorite=True,
        created_by_email="owner@example.com", created_by_role="Cliente",
    )

    assert store.delete_saved_search(first["saved_search_id"], "ONB-001", "owner@example.com") is True
    assert [item["saved_search_id"] for item in store.saved_searches("owner@example.com", "ONB-001")] == [second["saved_search_id"]]


def test_client_execution_history_hides_explicit_admin_runs():
    store = ProspectStore()
    store.executions = [
        ["client-run", "2026-08-10T10:00:00Z", "owner@example.com", "Empresa", "", "Completado", "model", 0, 0, 0, "", "ONB", "Productora", 1, 5, "[]", "[]", "", "", "{}", "{}", "provider", "[]", 0, "owner@example.com", "Cliente", "manual"],
        ["admin-run", "2026-08-10T11:00:00Z", "owner@example.com", "Empresa", "", "Completado", "model", 0, 0, 0, "", "ONB", "Productora", 1, 5, "[]", "[]", "", "", "{}", "{}", "provider", "[]", 0, "admin@example.com", "Administrador", "manual"],
    ]

    client_items = store.recent_executions("owner@example.com", hide_admin=True)
    admin_items = store.recent_executions("owner@example.com")

    assert [item["execution_id"] for item in client_items] == ["client-run"]
    assert {item["execution_id"] for item in admin_items} == {"client-run", "admin-run"}


def test_schema_migration_rejects_reordered_existing_columns():
    store = HeaderStore({"Prospeccion": ["email", "execution_id"]})

    with pytest.raises(RuntimeError, match="no coinciden"):
        store._ensure_header_row("Prospeccion", PROSPECT_HEADERS)

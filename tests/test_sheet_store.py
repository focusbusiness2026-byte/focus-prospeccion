import pytest

from app.config import Settings
from app.sheet_store import SheetStore


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

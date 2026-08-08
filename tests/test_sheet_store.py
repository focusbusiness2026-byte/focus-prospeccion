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

from app.auth import Identity, create_session, verify_session
from app.config import Settings
from app.main import _is_authorized_admin
from app.sheet_store import AccessRecord, is_active_access_state, normalized_kanban_status


def test_signed_session_round_trip_and_tamper_detection():
    settings = Settings(app_secret="test-secret")
    token = create_session(Identity("USER@Example.com ", "Administrador", "google-123"), settings)

    identity = verify_session(token, settings)

    assert identity == Identity("user@example.com", "Administrador", "google-123")
    assert verify_session(token + "x", settings) is None


def test_every_active_administrator_row_has_admin_access_without_a_static_allowlist():
    for email, role, state in [
        ("first.admin@example.com", " Administrador ", "ACTIVO"),
        ("second.admin@example.com", "administrador", "Áctivo"),
    ]:
        identity = Identity(email, role, "subject")
        access = AccessRecord(2, email, role, state, 0, 0)
        assert _is_authorized_admin(identity, access, Settings(focus_admin_emails="other@example.com"))

    client_access = AccessRecord(3, "client@example.com", "Cliente", "Activo", 10, 0)
    assert not _is_authorized_admin(Identity("client@example.com", "Cliente", "subject"), client_access, Settings())
    assert is_active_access_state(" Áctivo ")


def test_legacy_statuses_map_conservatively_to_the_four_kanban_columns():
    assert normalized_kanban_status("Aprobado") == "Aprobado para descarga"
    assert normalized_kanban_status("Cerrado") == "Descartado"

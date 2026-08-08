from app.auth import Identity, create_session, verify_session
from app.config import Settings


def test_signed_session_round_trip_and_tamper_detection():
    settings = Settings(app_secret="test-secret")
    token = create_session(Identity("USER@Example.com ", "Administrador", "google-123"), settings)

    identity = verify_session(token, settings)

    assert identity == Identity("user@example.com", "Administrador", "google-123")
    assert verify_session(token + "x", settings) is None

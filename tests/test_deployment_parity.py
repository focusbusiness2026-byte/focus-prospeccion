from fastapi.testclient import TestClient
from starlette.requests import Request

from app.auth import Identity
from app.build_info import _normalized_source_bytes, portal_build_id
from app.main import _template, app


def test_health_exposes_the_non_secret_build_identifier():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["portal_build_id"] == portal_build_id()


def test_build_fingerprint_normalizes_windows_and_linux_line_endings(tmp_path):
    windows = tmp_path / "windows.py"
    linux = tmp_path / "linux.py"
    windows.write_bytes(b"first\r\nsecond\r\n")
    linux.write_bytes(b"first\nsecond\n")

    assert _normalized_source_bytes(windows) == _normalized_source_bytes(linux)


def test_rendered_portal_uses_current_css_fingerprint_and_disables_html_cache():
    request = Request({"type": "http", "method": "GET", "path": "/portal", "headers": []})
    response = _template(
        request,
        "portal.html",
        identity=Identity("admin@example.com", "Administrador", "test"),
        radar_portal_url="",
    )
    body = response.body.decode("utf-8")

    assert f"/static/app.css?v={portal_build_id()}" in body
    assert response.headers["cache-control"] == "no-store"

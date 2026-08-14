import asyncio

import httpx

from app.config import Settings
from app.keepalive import (
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    ping_render_health_once,
    render_keepalive_interval,
    render_keepalive_ready,
)


def _production_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "render_keepalive_enabled": True,
        "render_keepalive_url": "https://focus-prospeccion-fb.onrender.com/health",
    }
    values.update(overrides)
    return Settings(**values)


def test_keepalive_requires_explicit_production_https_health_url():
    assert render_keepalive_ready(_production_settings()) is True
    assert render_keepalive_ready(_production_settings(app_env="development")) is False
    assert render_keepalive_ready(_production_settings(render_keepalive_enabled=False)) is False
    assert render_keepalive_ready(_production_settings(render_keepalive_url="http://example.com/health")) is False
    assert render_keepalive_ready(_production_settings(render_keepalive_url="https://example.com/private")) is False
    assert render_keepalive_ready(_production_settings(render_keepalive_url="https://user:pass@example.com/health")) is False


def test_keepalive_interval_stays_below_render_idle_window():
    assert render_keepalive_interval(_production_settings(render_keepalive_interval_seconds=1)) == MIN_INTERVAL_SECONDS
    assert render_keepalive_interval(_production_settings(render_keepalive_interval_seconds=600)) == 600
    assert render_keepalive_interval(_production_settings(render_keepalive_interval_seconds=9999)) == MAX_INTERVAL_SECONDS


def test_keepalive_ping_calls_only_public_health_endpoint():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    result = asyncio.run(
        ping_render_health_once(
            _production_settings(),
            transport=httpx.MockTransport(handler),
        )
    )

    assert result is True
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://focus-prospeccion-fb.onrender.com/health"
    assert requests[0].headers["user-agent"] == "focus-business-render-keepalive/2.0"


def test_keepalive_ping_handles_http_failure_without_crashing():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    result = asyncio.run(
        ping_render_health_once(
            _production_settings(),
            transport=httpx.MockTransport(handler),
        )
    )

    assert result is False

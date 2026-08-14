from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx

from app.config import Settings


LOGGER = logging.getLogger(__name__)
MIN_INTERVAL_SECONDS = 300
MAX_INTERVAL_SECONDS = 840


def render_keepalive_interval(settings: Settings) -> int:
    return min(
        MAX_INTERVAL_SECONDS,
        max(MIN_INTERVAL_SECONDS, settings.render_keepalive_interval_seconds),
    )


def render_keepalive_ready(settings: Settings) -> bool:
    if settings.app_env != "production" or not settings.render_keepalive_enabled:
        return False
    parsed = urlparse(settings.render_keepalive_url)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.path == "/health"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


async def ping_render_health_once(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    if not render_keepalive_ready(settings):
        return False
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=20,
            follow_redirects=False,
            headers={"User-Agent": "focus-business-render-keepalive/2.0"},
        ) as client:
            response = await client.get(settings.render_keepalive_url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        LOGGER.warning("Render keep-alive ping failed: %s", type(exc).__name__)
        return False
    return True


async def render_keepalive_loop(settings: Settings) -> None:
    interval = render_keepalive_interval(settings)
    while True:
        await asyncio.sleep(interval)
        await ping_render_health_once(settings)

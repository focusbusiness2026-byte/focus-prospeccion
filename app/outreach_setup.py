"""Explicit hosting gates. This module neither reads production stores nor sends."""
import base64
import json
import logging
import os

from app.outreach import OutreachService
from app.outreach_api import outreach_router
from app.outreach_providers import GHLOAuth, GHLGateway, OAuthVault, OpenAISequenceGenerator


class HideOAuthQuery(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3 and "/integrations/ghl/callback" in str(record.args[2]):
            args = list(record.args)
            args[2] = "/integrations/ghl/callback?[redacted]"
            record.args = tuple(args)
        return True


def outreach_ai_authorized(engine):
    """Enable paid drafts only with explicit server-side credential authorization."""
    credential_authorized = (
        os.getenv("OUTREACH_KEY_ROTATION_CONFIRMED") == "1"
        or os.getenv("OUTREACH_EXISTING_KEY_REUSE_AUTHORIZED") == "1"
    )
    return bool(engine.real and os.getenv("OUTREACH_AI_ENABLED") == "1" and credential_authorized)


def mount_outreach(app, engine, identity, template, settings):
    generator = None
    if outreach_ai_authorized(engine):
        generator = OpenAISequenceGenerator(settings)
    service = OutreachService(engine, generator=generator)
    oauth = None
    enabled = os.getenv("OUTREACH_LIVE_EMAIL_ENABLED") == "1"
    if enabled or os.getenv("GHL_OAUTH_ENABLED") == "1":
        key = base64.b64decode(os.environ["OUTREACH_ENCRYPTION_KEY"], validate=True)
        vault = OAuthVault(engine, key)
        oauth = GHLOAuth(vault, os.environ["GHL_CLIENT_ID"], os.environ["GHL_CLIENT_SECRET"], settings.public_base_url + "/integrations/ghl/callback",
            scopes=None if enabled else "contacts.readonly contacts.write")
        if enabled:
            fields = json.loads(os.environ["GHL_OUTREACH_FIELD_IDS"])
            service.provider = GHLGateway(vault, fields=fields, checkpoint=service.checkpoint, token_provider=oauth.access_token, enabled=True)
    logging.getLogger("uvicorn.access").addFilter(HideOAuthQuery())
    app.include_router(outreach_router(service, identity, template, oauth=oauth, webhook_key=os.getenv("GHL_WEBHOOK_PUBLIC_KEY"), live_enabled=enabled))
    return service

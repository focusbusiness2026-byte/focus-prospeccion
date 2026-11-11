"""Validated, scoped outreach API. Opt-in providers are supplied by the server."""
from __future__ import annotations

import json
import re
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import validate_csrf
from app.intelligence import CampaignError
from app.outreach_providers import GHL_ASSET_READ_SCOPES, GHL_SETUP_WRITE_SCOPES, GHL_TEMPLATE_SETUP_SCOPES, verify_webhook


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Recipient(Payload):
    lead_id: str = Field(min_length=1, max_length=200)
    email: str = Field(default="", max_length=254)


class Create(Payload):
    name: str = Field(default="", max_length=120)
    channels: list[Literal["email", "linkedin", "social"]] = Field(min_length=1, max_length=2)
    steps: Literal[5, 15] = 15
    recipients: list[Recipient] = Field(min_length=1, max_length=100)


class Approve(Payload):
    digest: str = Field(min_length=64, max_length=64)


class Action(Payload):
    action: Literal["activate", "pause", "resume", "duplicate", "archive"]


class Edit(Payload):
    recipient: str = Field(max_length=500)
    emails: list[dict] = Field(min_length=5, max_length=15)


class LibraryEdit(Payload):
    emails: list[dict] = Field(min_length=5, max_length=15)


class LocationLink(Payload):
    url: str = Field(max_length=700)


class LinkedInEdit(Payload):
    recipient: str = Field(max_length=500)
    texts: dict[str, str]


class Manual(Payload):
    recipient: str = Field(max_length=500)
    event: Literal["invitation_sent", "connection_accepted", "message_sent", "followup_sent", "response", "positive_response", "opt_out", "bounce", "meeting", "not_interested"]


def authorize(engine, tenant, identity):
    with engine.db() as db:
        try:
            profile = engine._profile(db, tenant)
        except CampaignError:
            raise HTTPException(403, "Cuenta no autorizada") from None
    allowed = {str(x).lower() for x in profile.get("access_emails", [])}
    if identity.email.lower() in allowed:
        return tenant
    if not engine.real and ("admin" in identity.role.lower() or identity.email == f"{tenant}@example.test"):
        return tenant
    raise HTTPException(403, "Esta sesión no pertenece a esta cuenta")


def outreach_router(service, identity_dependency, template, *, oauth=None, asset_reader=None, webhook_key=None, live_enabled=False):
    router, engine = APIRouter(), service.engine

    def run(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except CampaignError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError:
            raise HTTPException(503, "No se confirmó el guardado; revisa el estado antes de repetir") from None

    def scope(t, identity):
        return run(authorize, engine, t, identity)

    def admin(identity):
        if "admin" not in identity.role.lower():
            raise HTTPException(403, "Configuración reservada a administración")

    @router.get("/campaigns/outreach")
    def page(request: Request, identity=Depends(identity_dependency)):
        return template(request, "outreach.html", identity=identity)

    @router.get("/integrations/ghl")
    def integration_page(request: Request, identity=Depends(identity_dependency)):
        return template(request, "ghl-connection.html", identity=identity)

    @router.get("/api/intelligence/{tenant}/outreach/ghl/status")
    def connection_status(tenant: str, identity=Depends(identity_dependency)):
        scope(tenant, identity)
        brand = service.brand(tenant)
        state = 'setup_pending' if oauth is None else 'not_connected'
        linked = False
        granted_scopes = set()
        if oauth is not None:
            try:
                token = oauth.vault.get(tenant)
                granted_scopes = set(str(token.get('scope') or '').split())
                linked = bool(brand.get('location_id') and token.get('locationId') == brand['location_id'])
                state = 'connected' if linked else 'wrong_location'
                if linked and token.get('refresh_pending'):
                    state, linked = 'reconnect_required', False
                import time
                if linked and token.get('expires_at', 0) < time.time() and not token.get('refresh_token'):
                    state, linked = 'reconnect_required', False
            except CampaignError:
                pass
        required_setup = set((GHL_ASSET_READ_SCOPES + ' ' + GHL_SETUP_WRITE_SCOPES).split())
        setup_permissions = required_setup <= granted_scopes
        return {'company': brand.get('company', ''), 'state': state, 'connected': linked,
            'oauth_ready': oauth is not None, 'location_configured': bool(brand.get('location_id')),
            'can_connect': oauth is not None and bool(brand.get('location_id')) and 'admin' in identity.role.lower(),
            'admin': 'admin' in identity.role.lower(), 'sending_enabled': live_enabled,
            'workflow_verified': bool(brand.get('workflow_verified')), 'unsubscribe_verified': bool(brand.get('unsubscribe_verified')),
            'sender_email': brand.get('sender_email', ''), 'workflow_linked': bool(brand.get('workflow_id')),
            'template_count': len(brand.get('template_ids') or []),
            'asset_check_enabled': asset_reader is not None,
            'can_prepare_templates': bool(linked and GHL_TEMPLATE_SETUP_SCOPES <= granted_scopes and service.template_provider is not None and 'admin' in identity.role.lower()),
            'setup_permissions_missing': bool(linked and not setup_permissions and service.template_provider is not None),
            'domain_verified': bool(brand.get('domain_verified')), 'automatic_import': False}

    @router.put("/api/intelligence/{tenant}/outreach/ghl/location")
    def set_ghl_location(tenant: str, payload: LocationLink, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        admin(identity)
        scope(tenant, identity)
        try:
            parsed = urlsplit(payload.url.strip())
        except ValueError:
            raise HTTPException(422, "Pega el enlace de la subcuenta de GoHighLevel") from None
        match = re.match(r"^/v2/location/([A-Za-z0-9_-]{5,100})(?:/|$)", parsed.path)
        if parsed.scheme != "https" or parsed.netloc.lower() != "app.gohighlevel.com" or not match:
            raise HTTPException(422, "Pega el enlace de la subcuenta de GoHighLevel")
        run(service.configure_brand, tenant, {"location_id": match[1]}, initial_location_only=True)
        return {"location_configured": True}

    @router.get("/api/intelligence/{tenant}/outreach/ghl/workflow-capability")
    def workflow_capability(tenant: str, identity=Depends(identity_dependency)):
        admin(identity)
        scope(tenant, identity)
        if asset_reader is None:
            raise HTTPException(409, "Autoriza la conexión con GoHighLevel para comprobar la API de workflows")
        try:
            return run(asset_reader.workflow_capability, tenant, service.brand(tenant))
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise HTTPException(503, "GoHighLevel no confirmó la disponibilidad de la API de workflows") from None

    @router.get("/api/intelligence/{tenant}/outreach/ghl/assets")
    def inspect_assets(tenant: str, identity=Depends(identity_dependency)):
        admin(identity)
        scope(tenant, identity)
        if asset_reader is None:
            raise HTTPException(409, "La comprobación de recursos GHL requiere autorización administrativa de lectura")
        try:
            return run(asset_reader.inspect_five_step_assets, tenant, service.brand(tenant))
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise HTTPException(503, "No se pudieron comprobar los recursos de esta subcuenta") from None

    @router.get("/api/intelligence/accounts")
    def accounts(identity=Depends(identity_dependency)):
        with engine.db() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM tenants")]
        allowed = []
        for p in rows:
            try:
                authorize(engine, p["id"], identity)
                allowed.append({"id": p["id"], "name": p["name"], "source_id": p.get("source_id")})
            except HTTPException:
                pass
        return {"accounts": allowed, "admin": "admin" in identity.role.lower()}

    @router.get("/api/intelligence/{tenant}/outreach")
    def catalog(tenant: str, identity=Depends(identity_dependency)):
        return run(service.catalog, scope(tenant, identity))

    @router.put("/api/intelligence/{tenant}/outreach/brand")
    def brand(tenant: str, payload: dict, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        admin(identity)
        return run(service.configure_brand, scope(tenant, identity), payload)

    @router.post("/api/intelligence/{tenant}/outreach/library")
    def library(tenant: str, request: Request, steps: Literal["5", "15"] = "15", identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.library, scope(tenant, identity), int(steps))

    @router.post("/api/intelligence/{tenant}/outreach/campaigns")
    def create(tenant: str, payload: Create, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.create, scope(tenant, identity), payload.model_dump())

    @router.put("/api/intelligence/{tenant}/outreach/library/{version}")
    def edit_library(tenant: str, version: int, payload: LibraryEdit, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.edit_library, scope(tenant, identity), version, payload.emails)

    @router.get("/api/intelligence/{tenant}/outreach/library/{version}/preview")
    def library_preview(tenant: str, version: int, step: int, identity=Depends(identity_dependency)):
        value = run(service.preview_library, scope(tenant, identity), version, step)
        return {**value, "preview_only": True}

    @router.post("/api/intelligence/{tenant}/outreach/campaigns/{cid}/reschedule")
    def reschedule(tenant: str, cid: str, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.reschedule, scope(tenant, identity), cid)

    @router.get("/api/intelligence/{tenant}/outreach/campaigns/{cid}")
    def detail(tenant: str, cid: str, identity=Depends(identity_dependency)):
        return run(service.detail, scope(tenant, identity), cid)

    @router.post("/api/intelligence/{tenant}/outreach/campaigns/{cid}/prepare")
    def prepare(tenant: str, cid: str, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.prepare, scope(tenant, identity), cid)

    @router.put("/api/intelligence/{tenant}/outreach/campaigns/{cid}/messages")
    def edit(tenant: str, cid: str, payload: Edit, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.edit, scope(tenant, identity), cid, payload.recipient, payload.emails)

    @router.put("/api/intelligence/{tenant}/outreach/campaigns/{cid}/linkedin")
    def edit_linkedin(tenant: str, cid: str, payload: LinkedInEdit, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.edit_linkedin, scope(tenant, identity), cid, payload.recipient, payload.texts)

    @router.get("/api/intelligence/{tenant}/outreach/campaigns/{cid}/preview")
    def preview(tenant: str, cid: str, recipient: str, step: int, identity=Depends(identity_dependency)):
        value = run(service.preview, scope(tenant, identity), cid, recipient, step)
        return {**value, "preview_only": True}

    @router.post("/api/intelligence/{tenant}/outreach/campaigns/{cid}/approve")
    def approve(tenant: str, cid: str, payload: Approve, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.approve, scope(tenant, identity), cid, payload.digest, identity.email)

    @router.post("/api/intelligence/{tenant}/outreach/campaigns/{cid}/actions")
    def action(tenant: str, cid: str, payload: Action, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.action, scope(tenant, identity), cid, payload.action, live=live_enabled)

    @router.post("/api/intelligence/{tenant}/outreach/campaigns/{cid}/manual")
    def manual(tenant: str, cid: str, payload: Manual, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        return run(service.linkedin_event, scope(tenant, identity), cid, payload.recipient, payload.event, identity.email)

    @router.get("/api/intelligence/{tenant}/outreach/ghl/connect")
    def connect(tenant: str, identity=Depends(identity_dependency)):
        admin(identity)
        scope(tenant, identity)
        if oauth is None:
            raise HTTPException(409, "Conexión OAuth pendiente de configuración administrativa")
        if not service.brand(tenant).get('location_id'):
            raise HTTPException(409, "Falta vincular la subcuenta propia de esta empresa")
        return RedirectResponse(run(oauth.authorize_url, tenant, identity.email), status_code=303)

    @router.post("/api/intelligence/{tenant}/outreach/ghl/templates/{version}")
    def templates(tenant: str, version: int, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        admin(identity)
        scope(tenant, identity)
        if not live_enabled and service.template_provider is None:
            raise HTTPException(409, "Los cambios en GHL no están habilitados")
        if service.template_provider is not None:
            token = run(oauth.vault.get, tenant)
            if not GHL_TEMPLATE_SETUP_SCOPES <= set(str(token.get('scope') or '').split()):
                raise HTTPException(409, "Renueva la autorización de GoHighLevel con los permisos de preparación")
        return run(service.provision_templates, tenant, version)

    @router.get("/integrations/ghl/callback")
    def callback(request: Request, state: str, code: str, identity=Depends(identity_dependency)):
        admin(identity)
        if oauth is None:
            raise HTTPException(409, "Conexión OAuth no habilitada")
        # State is session-bound; location is configured server-side before auth.
        with engine.db() as db:
            from app.outreach_content import fingerprint
            row = db.execute("SELECT tenant FROM outreach_oauth_states WHERE id=?", (fingerprint(state),)).fetchone()
        if not row:
            raise HTTPException(403, "Conexión no reconocida")
        scope(row[0], identity)
        run(oauth.callback, state, code, identity.email, service.brand(row[0]).get("location_id"))
        return RedirectResponse("/integrations/ghl?" + urlencode({"account":row[0], "connected":"1"}), status_code=303)

    @router.post("/webhooks/ghl")
    async def webhook(request: Request):
        if not webhook_key:
            raise HTTPException(503, "Receptor de eventos no configurado")
        if not request.headers.get("content-length", "0").isdigit():
            raise HTTPException(400, "Longitud inválida")
        if int(request.headers.get("content-length", "0")) > 256000:
            raise HTTPException(413, "Evento demasiado grande")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 256000:
                raise HTTPException(413, "Evento demasiado grande")
        raw = bytes(raw)
        try:
            event = verify_webhook(raw, request.headers.get("x-ghl-signature"), webhook_key)
        except (CampaignError, ValueError):
            raise HTTPException(401, "Evento no autenticado") from None
        location = event.get("locationId")
        with engine.db() as db:
            tenants = [r[0] for r in db.execute("SELECT tenant FROM outreach_brand WHERE json_extract(payload,'$.location_id')=?", (location,))]
        if len(tenants) != 1:
            raise HTTPException(403, "Subcuenta no vinculada de forma única")
        # Explicit channel/type semantics; never count an open as a response.
        kind = {"InboundMessage": "response", "AppointmentCreate": "meeting"}.get(event.get("type"))
        if event.get("type") == "ContactDndUpdate" and (event.get("dnd") is True or event.get("dndSettings", {}).get("Email", {}).get("status") == "active"):
            kind = "opt_out"
        if event.get("type") == "OutboundMessage" and event.get("status") in {"sent", "delivered"}:
            kind = {"sent": "accepted", "delivered": "delivered"}[event["status"]]
        if event.get("type") == "OutboundMessage" and event.get("status") == "failed" and event.get("bounceType") == "hard":
            kind = "bounce"
        if event.get("type") in {"InboundMessage", "OutboundMessage"} and event.get("messageType") not in {"Email", "TYPE_EMAIL"}:
            return {"ok": True, "ignored": True}
        if event.get("type") == "OutboundMessage" and not event.get("workflowId"):
            # A manual email or another workflow in this CRM must never be
            # reported as a delivery from our campaign merely by contact ID.
            return {"ok": True, "ignored": True, "reason": "Sin atribución de workflow"}
        if not kind:
            return {"ok": True, "ignored": True}
        from app.outreach_content import fingerprint
        return run(service.provider_event, tenants[0], str(event.get("webhookId") or fingerprint(event)), location,
            event.get("contactId"), kind, message_id=str(event.get("messageId") or ""), workflow_id=event.get("workflowId"))

    return router

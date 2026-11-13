"""Isolated staging entry point. Never imported by the production entry point."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

if os.environ.get("FOCUS_SANDBOX") != "1":
    raise RuntimeError("Sandbox entry point requires FOCUS_SANDBOX=1")
if any(os.environ.get(key) for key in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SHEET_ID", "OPENAI_API_KEY", "GEMINI_API_KEY")):
    raise RuntimeError("Sandbox refuses external credentials and production storage")

PUBLIC_DEMO = os.getenv("SANDBOX_PUBLIC_DEMO") == "1"
if PUBLIC_DEMO and any(os.getenv(key) == "1" for key in (
    "SANDBOX_REAL_MODE", "OUTREACH_AI_ENABLED", "OUTREACH_LIVE_EMAIL_ENABLED",
    "GHL_OAUTH_ENABLED", "GHL_CAMPAIGN_SETUP_ENABLED", "GHL_ASSET_CHECK_ENABLED",
)):
    raise RuntimeError("The public demo cannot enable real integrations")

from app.config import Settings
from app.sheet_store import (SheetStore as OriginalStore, PROSPECT_HEADERS, EXECUTION_HEADERS,
                             AUTOMATION_HEADERS, LEAD_REVIEW_HEADERS, SAVED_SEARCH_HEADERS)
from app.auth import Identity
from app.intelligence import CampaignError
from app.onboarding import OnboardingSource
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from app import main, auth

ROOT = Path(os.environ.get("SANDBOX_DATA_DIR", "/tmp/focus-prospeccion-sandbox"))
REAL_MODE = os.environ.get("SANDBOX_REAL_MODE") == "1"
LOCK = threading.RLock()
SETTINGS = Settings(_env_file=None, app_env="sandbox", google_sheets_enabled=True,
    auto_research_enabled=False, central_auth_enabled=False, allow_external_sources=False,
    app_secret=secrets.token_urlsafe(32), public_base_url=os.environ.get("RENDER_EXTERNAL_URL", "http://127.0.0.1:8018"),
    radar_portal_url=os.environ.get("SANDBOX_RADAR_URL", "http://127.0.0.1:8019"),
    central_auth_url="" if PUBLIC_DEMO else os.environ.get("SANDBOX_PORTAL_URL", "http://127.0.0.1:8020"))
IDENTITY = Identity("admin@sandbox.example.test", "Administrador", "sandbox", True, not PUBLIC_DEMO)
GUIDED_SCHEDULE_PREFIX = "Prospección guiada · "


def is_guided_schedule(config):
    return bool(config and config.get("batch") and re.fullmatch(
        r"Prospección guiada · [0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", config.get("name", "")))


def seed() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    records = []
    for number in (1, 2):
        records.append({"ID registro": f"SANDBOX-{number}", "Empresa": f"Estudio ficticio {number}",
            "Web": f"https://estudio{number}.example.test", "Email responsable": f"cliente{number}@example.test",
            "Servicio prioritario": "Vídeo corporativo", "Servicios": "Vídeo corporativo",
            "Público": "B2B", "Sectores": "Tecnología", "Mercados": "España", "Países objetivo": "España",
            "Tipos de cliente objetivo": "Empresa privada B2B", "Tamaño empresa ideal": "11–50 empleados",
            "Autorización": "true", "Fecha envío": now, "Estado": "Pruebas"})
    headers = list(records[0])
    data = {"Onboarding": [headers] + [[r[h] for h in headers] for r in records],
        "Accesos": [["email", "rol", "estado", "fecha", "asignados", "usados", "disponibles", "%", "cuota", "renovacion", "prospeccion", "radar", "principal"],
            [IDENTITY.email, "Administrador", "Activo", now, "Ilimitado", 0, "", "", "", now, "Permitido", "Permitido", ""]],
        "Prospeccion": [PROSPECT_HEADERS], "Ejecuciones": [EXECUTION_HEADERS],
        "Automatizaciones": [AUTOMATION_HEADERS], "Revisiones Leads": [LEAD_REVIEW_HEADERS],
        "Busquedas Guardadas": [SAVED_SEARCH_HEADERS]}
    for n, record in enumerate(records, 1):
        data["Accesos"].append([record["Email responsable"], "Cliente", "Activo", now, 50, 0, 50, 1, "Disponible", now, "Permitido", "Permitido", ""])
        for index, status in enumerate(("Nuevo", "En revisión", "Aprobado para descarga"), 1):
            lead = dict(execution_id=f"sandbox-{n}-{index}", email=record["Email responsable"], created_at=now,
                company=f"Empresa ficticia {n}.{index}", website=f"https://empresa{n}{index}.example.test",
                sector="Tecnología", city="Madrid", country="España", score=75, classification="Interesante",
                summary="Registro sintético para comprobar el portal. No procede de un raspado real.",
                entry_angle="Ejemplo de propuesta audiovisual", lead_status=status, onboarding_id=record["ID registro"],
                productora=record["Empresa"], public_contacts_json=json.dumps([{"name":"Contacto ficticio", "email":f"contacto{index}@example.test", "role":"Marketing", "source_url":"https://example.test"}]),
                prospect_found="true", client_type="Empresa privada B2B")
            data["Prospeccion"].append([lead.get(h, "") for h in PROSPECT_HEADERS])
    return data


class SandboxStore(OriginalStore):
    """Reuse the real domain logic over a separate SQLite-backed cell store."""
    def __init__(self, settings=None):
        super().__init__(SETTINGS)
        ROOT.mkdir(parents=True, exist_ok=True)
        self.path = ROOT / "cells.sqlite3"
        with LOCK, sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS sandbox (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO sandbox VALUES (1, ?)", (json.dumps(seed()),))

    def _session(self):
        raise RuntimeError("External Sheets transport is disabled in sandbox")

    def _load(self):
        with sqlite3.connect(self.path) as db:
            return json.loads(db.execute("SELECT payload FROM sandbox WHERE id=1").fetchone()[0])

    def _save(self, data):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE sandbox SET payload=? WHERE id=1", (json.dumps(data),))

    @staticmethod
    def _range(value):
        tab, cells = value.split("!", 1)
        parts = re.fullmatch(r"([A-Z]+)(\d*)?(?::([A-Z]+)(\d*)?)?", cells)
        if not parts:
            raise ValueError("Unsupported sandbox range")
        c1, r1, c2, r2 = parts.groups()
        def column(s):
            result = 0
            for ch in s:
                result = result * 26 + ord(ch) - 64
            return result - 1
        return tab.strip("'"), column(c1), int(r1 or 1)-1, column(c2 or c1)+1, int(r2 or r1 or 100000)

    def _get(self, a1_range):
        tab, c1, r1, c2, r2 = self._range(a1_range)
        with LOCK:
            return [row[c1:c2] for row in self._load().get(tab, [])[r1:r2]]

    def _update(self, a1_range, values):
        tab, c1, r1, _, _ = self._range(a1_range)
        with LOCK:
            data = self._load()
            rows = data.setdefault(tab, [])
            for offset, value in enumerate(values):
                while len(rows) <= r1+offset:
                    rows.append([])
                row = rows[r1+offset]
                row.extend([""] * max(0, c1+len(value)-len(row)))
                row[c1:c1+len(value)] = value
            self._save(data)

    def _append(self, a1_range, values):
        tab = a1_range.split("!")[0].strip("'")
        with LOCK:
            data = self._load()
            data.setdefault(tab, []).extend(values)
            self._save(data)

    def update_guide_field(self, source, field, before, value):
        with LOCK:
            data = self._load()
            rows = data.get(self.settings.google_onboarding_tab, [])
            if not rows:
                raise CampaignError('La ficha de onboarding ya no está disponible.')
            headers = rows[0]
            matches = [row for row in rows[1:] if str(dict(zip(headers, row)).get('ID registro', '')).strip() == source.record_id]
            if len(matches) != 1:
                raise CampaignError('No se encontró una ficha única para esta empresa.')
            if headers.count(field) > 1:
                raise CampaignError('El campo está duplicado en la ficha. Actualiza los encabezados antes de editarlo.')
            row = matches[0]
            current = OnboardingSource.from_sheet_record(dict(zip(headers, row)))
            if current.email != source.email:
                raise CampaignError('La cuenta de la ficha cambió. Actualiza antes de guardar.')
            existing = current.raw_form.get(field, '')
            if existing == value:
                return current
            if existing != before:
                raise CampaignError('Este campo cambió en otra sesión. Actualiza la ficha antes de editarlo.')
            if field not in headers:
                headers.append(field)
            index = headers.index(field)
            row.extend([''] * max(0, index + 1 - len(row)))
            row[index] = value
            self._save(data)
            return OnboardingSource.from_sheet_record(dict(zip(headers, row)))

    def _sheet_properties(self):
        return [{"sheetId": i, "title": title, "gridProperties": {"columnCount": 200, "rowCount": 10000}}
                for i, title in enumerate(self._load())]

    def _batch_update(self, requests):
        with LOCK:
            data = self._load()
            for req in requests:
                if "addSheet" in req:
                    data.setdefault(req["addSheet"]["properties"]["title"], [])
                elif "deleteDimension" in req:
                    item = req["deleteDimension"]["range"]
                    title = list(data)[item["sheetId"]]
                    del data[title][item["startIndex"]:item["endIndex"]]
                elif "appendDimension" not in req:
                    raise ValueError("Unsupported sandbox metadata write")
            self._save(data)


main.get_settings = lambda: SETTINGS
auth.get_settings = lambda: SETTINGS
main.SheetStore = SandboxStore

async def identity(*args, **kwargs):
    return IDENTITY

auth.verify_central_session = identity
main.app.dependency_overrides[auth.require_identity] = lambda: IDENTITY

@asynccontextmanager
async def sandbox_lifespan(_):
    async def run_campaigns():
        while True:
            await asyncio.sleep(15)
            try:await asyncio.to_thread(campaign_engine.tick)
            except Exception:
                # Durable errors stop the loop rather than replaying side effects.
                break
    tasks = [asyncio.create_task(run_campaigns())]
    if REAL_MODE and SETTINGS.auto_research_enabled:
        tasks.append(asyncio.create_task(main._automation_loop(60)))
    try:yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

app = main.app
app.router.lifespan_context = sandbox_lifespan

@app.post("/sandbox/onboarding")
def sandbox_onboarding(payload: dict):
    if REAL_MODE:return JSONResponse({"detail":"Usa la importación privada de la cuenta autorizada"},status_code=403)
    email = str(payload.get("contactEmail") or payload.get("email") or "").lower().strip()
    if not email.endswith(".test") or len(email)>200 or not payload.get("companyName"):
        return JSONResponse({"detail":"Utiliza una empresa ficticia y correo terminado en .test"},status_code=422)
    store = SandboxStore()
    with LOCK:
        data = store._load()
        headers = data["Onboarding"][0]
        existing = next((r for r in data["Onboarding"][1:] if r[headers.index("Email responsable")]==email),None)
        if existing:
            return {"ok":True,"id":existing[0]}
        record = dict(zip(headers,data["Onboarding"][1]))
        record.update({"ID registro":str(payload.get("id") or "SANDBOX-"+secrets.token_hex(4)), "Empresa":str(payload["companyName"])[:120],
            "Web":str(payload.get("website") or "https://ficticio.example.test")[:300], "Email responsable":email})
        data["Onboarding"].append([record.get(h,"") for h in headers])
        data["Accesos"].append([email,"Cliente","Activo","",50,0,50,1,"Disponible",datetime.now(timezone.utc).isoformat(),"Permitido","Permitido",""])
        store._save(data)
    return {"ok":True,"id":record["ID registro"]}

@app.middleware("http")
async def sandbox_boundary(request, call_next):
    path = request.url.path
    if PUBLIC_DEMO:
        if path == "/campaigns" and request.method in {"GET", "HEAD"}:
            return RedirectResponse("/campaigns/outreach", status_code=303)
        if path in {"/static/three-dots-logo.svg", "/static/three-dots-logo-email.png"}:
            return JSONResponse({"detail": "No disponible en la demo"}, status_code=404)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse({"detail": "La demo pública es de solo lectura."}, status_code=403)
    if REAL_MODE and path not in {"/health","/sandbox/session","/webhooks/ghl"} and not path.startswith("/static/"):
        identity=await auth.verify_central_session(request.cookies.get(auth.SESSION_COOKIE))
        if not identity:
            if "text/html" in request.headers.get("accept",""):
                return RedirectResponse(SETTINGS.central_auth_url+"/sandbox-access",status_code=303)
            return JSONResponse({"detail":"Inicia sesión en el portal privado de pruebas"},status_code=401,headers={"Cache-Control":"no-store"})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        blocked=("/automation", "/prospecting-improvement", "/prospecting-questionnaire", "/internal/", "/auth/google")
        if not REAL_MODE:blocked+=("/research",)
        if any(part in path for part in blocked):
            return JSONResponse({"detail":"Entorno de pruebas: búsquedas, IA y automatizaciones reales desactivadas."}, status_code=403)
        allowed_origin=SETTINGS.central_auth_url if REAL_MODE and path=="/sandbox/session" else str(request.base_url).rstrip("/")
        signed_handoff=REAL_MODE and path=="/sandbox/session" and request.headers.get("origin")=="null"
        if request.headers.get("origin") and request.headers["origin"] != allowed_origin and not signed_handoff:
            return JSONResponse({"detail":"Origen no autorizado"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Focus-Environment"] = "sandbox"
    if REAL_MODE:response.headers["Cache-Control"]="no-store"
    if path == "/health":
        return JSONResponse({"status":"ok", "environment":"sandbox", "storage":"isolated-durable-private-portal" if REAL_MODE else "isolated-sqlite", "external_services":REAL_MODE, "schedulers":REAL_MODE and SETTINGS.auto_research_enabled})
    if "text/html" in response.headers.get("content-type", ""):
        body = b"".join([chunk async for chunk in response.body_iterator]).decode()
        label="COPIA PRIVADA · Datos y OpenAI reales · Producción separada · Sin envíos" if REAL_MODE else "COPIA DE PRUEBAS · Datos ficticios · Sin raspados ni envíos reales"
        if PUBLIC_DEMO:
            label = "DEMO PÚBLICA · Datos ficticios · Solo lectura"
            body = body.replace("</head>", '<link rel="stylesheet" href="/static/public-demo.css"></head>', 1)
            if path in {"/prospecting-guide", "/campaigns", "/campaigns/outreach", "/integrations/ghl"}:
                notice = f'<p class="public-demo-notice">{label}</p>'
                body = re.sub(r"(<body\b[^>]*>)", lambda match: match[0] + notice, body, count=1)
        banner = '<div style="position:sticky;top:0;z-index:5000;padding:8px;background:#d9af3c;color:#07111e;text-align:center;font:600 13px sans-serif">'+label+' · <a href="'+SETTINGS.central_auth_url+'/portal">Portal</a> · <a href="/campaigns">Focus Intelligence</a></div>'
        response = HTMLResponse(body.replace('<div class="portal-app">', banner+'<div class="portal-app">', 1),status_code=response.status_code, headers={"X-Focus-Environment":"sandbox","Cache-Control":"no-store"})
        if path == "/portal":
            body = body.replace('</head>', '<link rel="stylesheet" href="/static/outreach-board.css"></head>', 1)
            scripts = '<script src="/static/outreach-board.js" defer></script>'
            if request.query_params.get("crm_embed") == "1":
                body = body.replace('<body class="portal-booting">', '<body class="portal-booting guide-crm-embedded">', 1)
                body = body.replace('</head>', '<link rel="stylesheet" href="/static/guide-crm.css"></head>', 1)
                scripts += '<script src="/static/guide-crm.js" defer></script>'
            else:
                body = body.replace('<div class="portal-app">', banner+'<div class="portal-app">', 1)
                body = body.replace('href="#sources" data-view="sources">Prospección', 'href="/prospecting-guide">Prospección', 1)
                body = body.replace('<a class="top-link" href="#results" data-view="results">Leads y CRM</a>', '', 1)
                body = body.replace('</nav>', '<a href="/campaigns/outreach">Campañas</a><a href="/integrations/ghl">GoHighLevel</a></nav>', 1)
                scripts += '<script src="/static/guide-portal-entry.js" defer></script>'
            response = HTMLResponse(body.replace('</body>', scripts + '</body>'), status_code=response.status_code,
                                    headers={"X-Focus-Environment":"sandbox","Cache-Control":"no-store"})
    if not request.cookies.get(auth.CSRF_COOKIE):
        response.set_cookie(auth.CSRF_COOKIE, secrets.token_urlsafe(24), samesite="lax", secure=request.url.scheme=="https")
    return response


if REAL_MODE:
    from app.sandbox_real import configure
    campaign_engine=configure(app)
else:
    from app.intelligence import CampaignEngine
    from app.intelligence_api import campaign_router
    seed_name = "public_demo_seed.json" if PUBLIC_DEMO else "intelligence_seed.json"
    campaign_engine=CampaignEngine(ROOT/"campaigns.sqlite3",Path(__file__).with_name(seed_name))
    app.include_router(campaign_router(campaign_engine,auth.require_identity,main._template))

from app.outreach_setup import mount_outreach
outreach_service = mount_outreach(app, campaign_engine, auth.require_identity, main._template, getattr(campaign_engine, "outreach_settings", SETTINGS))
if PUBLIC_DEMO and not outreach_service.catalog("demo")["campaigns"]:
    outreach_service.configure_brand("demo", {
        "company": "Estudio ficticio 1", "sender_name": "Equipo del estudio",
        "sender_email": "equipo@estudio1.example.test", "color": "#d9af3c",
        "signature": "Equipo de Estudio ficticio 1",
    })
    example = outreach_service.create("demo", {
        "name": "Demo · Campaña de cinco correos", "channels": ["email"], "steps": 5,
        "recipients": [{"lead_id": "demo-1", "email": "contacto@empresa13.example.test"}],
    })
    outreach_service.prepare("demo", example["id"])

# New guided flow is mounted only in this isolated entry point.
from app.prospecting_guide import BUSINESS_CONTEXT_FIELDS, TargetingGuide, OpenAITargetingGuide, guide_intake, review_feedback
from app.prospecting_guide_api import guide_router
from app.outreach_setup import outreach_ai_authorized
from app.outreach_api import authorize

def guide_source(tenant, identity):
    authorize(campaign_engine, tenant, identity)
    with campaign_engine.db() as db:
        profile = campaign_engine._profile(db, tenant)
    source_id = profile.get('source_id') or (None if REAL_MODE else {'three-dots':'SANDBOX-1','control':'SANDBOX-2'}.get(tenant))
    if not source_id:
        raise HTTPException(409, 'Falta vincular el onboarding propio de esta empresa')
    store = main.SheetStore(SETTINGS)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(403, 'Acceso retirado para esta cuenta')
    admin = main._is_authorized_admin(identity, access, SETTINGS)
    source = store.get_onboarding_source(source_id, None if admin else main._account_scope_email(identity, access))
    if not source:
        raise HTTPException(403, 'No se encontró el onboarding de esta cuenta')
    return source

def profile_from_source(source):
    profile = main._questionnaire_source_profile(source)
    profile['business_context'] = {key: source.raw_form[key] for key in BUSINESS_CONTEXT_FIELDS if source.raw_form.get(key)}
    return profile

def guide_profile(tenant, identity):
    return profile_from_source(guide_source(tenant, identity))

def guide_intake_view(tenant, identity):
    source = guide_source(tenant, identity)
    return profile_from_source(source), guide_intake(source)

def guide_update_intake(tenant, identity, field, before, value):
    source = guide_source(tenant, identity)
    updated = main.SheetStore(SETTINGS).update_guide_field(source, field, before, value)
    return profile_from_source(updated), guide_intake(updated)

def guide_feedback(tenant, identity):
    source = guide_source(tenant, identity)
    prospects = main.SheetStore(SETTINGS).recent_prospects(source.email, limit=1000)
    return review_feedback(prospects, source.record_id, source.email)

def guide_save(tenant, identity, name, criteria):
    source = guide_source(tenant, identity)
    adjustments = main.ResearchAdjustments.model_validate(criteria).model_dump()
    return main.SheetStore(SETTINGS).upsert_saved_search(source.record_id, source.email, name=name,
        adjustments=adjustments, favorite=False, created_by_email=identity.email, created_by_role=identity.role)


def guide_run(record_id, criteria, request, background_tasks, identity):
    return main.start_research_job(record_id, main.ResearchAdjustments.model_validate(criteria),
        request, background_tasks, identity)


def guide_job(tenant, identity):
    source = guide_source(tenant, identity)
    live = main.latest_manual_research_job(source.record_id, identity.email)
    if not REAL_MODE or (live and live["status"] in {"running", "queued"}):
        return live
    store = main.SheetStore(SETTINGS)
    restored = store.restore_research_job(source)
    job = live
    if restored and (not live or (restored["job_id"] != live["job_id"] and
                                 (restored.get("updated_at") or "") > (live.get("updated_at") or ""))):
        job = restored
    if job and job["status"] == "completed":
        job["timing"] = store.research_timing(source)
        saved = [item["execution_id"] for item in job.get("saved_leads") or []]
        leads = guide_result_rows(store, source, saved) if saved else []
        by_id = {item["execution_id"]: item for item in leads}
        job["highlights"] = [guide_result_highlight(by_id[lead_id], source) for lead_id in saved if lead_id in by_id]
    return job


def guide_result_highlight(lead, source):
    missing = re.compile(
        r"^(?:no|falta|descartar)\b|\b(?:no (?:se|hay|consta|disponible|encontrad[oa]|identificad[oa]|calificad[oa])|"
        r"sin (?:evidencia|datos|contactos|decisores|informaci[oó]n|confirmar)|"
        r"pendiente|descartad[oa]|(?:encaje|potencial|prioridad|oportunidad) baj[oa])\b", re.IGNORECASE,
    )
    def excerpt(field, limit):
        text = " ".join(str(lead.get(field) or "").split())
        sentence = next((part for part in re.split(r"(?<=[.!?])\s+", text) if part and not missing.search(part)), "")
        return sentence if len(sentence) <= limit else sentence[:limit].rsplit(" ", 1)[0] + "…"

    sector = str(lead.get("sector") or "").strip()
    place = str(lead.get("city") or lead.get("country") or "").strip()
    facts = list(dict.fromkeys(value for value in (sector, place) if value and not missing.search(value)))
    for field, single, plural in (
        ("decision_makers", "decisor identificado", "decisores identificados"),
        ("public_contacts", "contacto público", "contactos públicos"),
        ("public_signals", "señal pública", "señales públicas"),
    ):
        count = len(lead.get(field) or [])
        if count:
            facts.append(f"{count} {single if count == 1 else plural}")
    potential = excerpt("entry_angle", 320)
    if not potential:
        offer = source.main_service
        potential = f"Explorar una propuesta de {offer}." if offer else "Explorar un primer contacto para presentar tu oferta."
    return {"company": lead["company"], "facts": facts, "finding": excerpt("summary", 220), "potential": potential}


def guide_timing(tenant, identity):
    source = guide_source(tenant, identity)
    return main.SheetStore(SETTINGS).research_timing(source) if REAL_MODE else None


def guide_result_rows(store, source, ids):
    selected = set(ids)
    return [item for item in store.recent_prospects(source.email, limit=1000)
            if item["execution_id"] in selected and item["onboarding_id"] == source.record_id]


def guide_leads(tenant, identity, ids):
    source = guide_source(tenant, identity)
    store = main.SheetStore(SETTINGS)
    matches = guide_result_rows(store, source, ids)
    return main.decorate_prospects(matches, store.review_events(source.email),
                                   require_admin_review=SETTINGS.lead_admin_review_required)


def public_guide_schedule(config):
    if not is_guided_schedule(config):
        return None
    batch = config['batch']
    return {**{key: config.get(key) for key in ('enabled', 'interval_minutes', 'next_run_at', 'last_status')},
        'max_total_runs': batch['max_total_runs'], 'completed_runs': batch['completed_runs'],
        'in_flight': batch['in_flight']}


def guide_schedule_status(tenant, identity):
    source = guide_source(tenant, identity)
    return public_guide_schedule(main.SheetStore(SETTINGS).get_automation_config(source.record_id))


def guide_schedule(record_id, sid, criteria, interval, total, request, identity):
    with LOCK:
        current = main.SheetStore(SETTINGS).get_automation_config(record_id)
        if current and (current['enabled'] or (current.get('batch') or {}).get('in_flight')):
            raise HTTPException(409, 'Pausa la programación en curso antes de crear otra.')
        payload = main.AutomationRequest(name=GUIDED_SCHEDULE_PREFIX + sid,
            enabled=REAL_MODE and SETTINGS.auto_research_enabled,
            interval_minutes=interval, max_total_runs=total,
            adjustments=main.ResearchAdjustments.model_validate(criteria))
        result = main.save_onboarding_automation(record_id, payload, request, identity)
        return public_guide_schedule(result['automation'])


def guide_pause_schedule(tenant, identity):
    source = guide_source(tenant, identity)
    with LOCK:
        store = main.SheetStore(SETTINGS)
        current = store.get_automation_config(source.record_id)
        if not is_guided_schedule(current):
            raise CampaignError('No hay una programación de la guía para esta empresa.')
        if current['batch']['in_flight']:
            raise CampaignError('El raspado está en curso. Podrás pausar los siguientes cuando termine.')
        return public_guide_schedule(store.pause_bounded_automation(current))


guide_generator = OpenAITargetingGuide(SETTINGS) if outreach_ai_authorized(campaign_engine) and SETTINGS.openai_api_key else None
targeting_guide = TargetingGuide(campaign_engine, guide_generator)
app.include_router(guide_router(targeting_guide, auth.require_identity, main._template, guide_profile,
    guide_save, guide_feedback, guide_intake_view, guide_update_intake, guide_run, guide_schedule,
    guide_schedule_status, guide_pause_schedule, guide_job, guide_leads, guide_timing))

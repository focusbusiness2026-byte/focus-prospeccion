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

from app.config import Settings
from app.sheet_store import (SheetStore as OriginalStore, PROSPECT_HEADERS, EXECUTION_HEADERS,
                             AUTOMATION_HEADERS, LEAD_REVIEW_HEADERS, SAVED_SEARCH_HEADERS)
from app.auth import Identity
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from app import main, auth

ROOT = Path(os.environ.get("SANDBOX_DATA_DIR", "/tmp/focus-prospeccion-sandbox"))
REAL_MODE = os.environ.get("SANDBOX_REAL_MODE") == "1"
LOCK = threading.RLock()
SETTINGS = Settings(_env_file=None, app_env="sandbox", google_sheets_enabled=True,
    auto_research_enabled=False, central_auth_enabled=False, allow_external_sources=False,
    app_secret=secrets.token_urlsafe(32), public_base_url=os.environ.get("RENDER_EXTERNAL_URL", "http://127.0.0.1:8018"),
    radar_portal_url=os.environ.get("SANDBOX_RADAR_URL", "http://127.0.0.1:8019"),
    central_auth_url=os.environ.get("SANDBOX_PORTAL_URL", "http://127.0.0.1:8020"))
IDENTITY = Identity("admin@sandbox.example.test", "Administrador", "sandbox", True, True)


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
async def no_workers(_):
    async def run_campaigns():
        while True:
            await asyncio.sleep(15)
            try:await asyncio.to_thread(campaign_engine.tick)
            except Exception:
                # Durable errors stop the loop rather than replaying side effects.
                break
    task=asyncio.create_task(run_campaigns())
    try:yield
    finally:
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)

app = main.app
app.router.lifespan_context = no_workers

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
    if REAL_MODE and path not in {"/health","/sandbox/session"} and not path.startswith("/static/"):
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
        return JSONResponse({"status":"ok", "environment":"sandbox", "storage":"isolated-durable-private-portal" if REAL_MODE else "isolated-sqlite", "external_services":REAL_MODE, "schedulers":False})
    if "text/html" in response.headers.get("content-type", ""):
        body = b"".join([chunk async for chunk in response.body_iterator]).decode()
        label="COPIA PRIVADA · Datos y OpenAI reales · Producción separada · Sin envíos" if REAL_MODE else "COPIA DE PRUEBAS · Datos ficticios · Sin raspados ni envíos reales"
        banner = '<div style="position:sticky;top:0;z-index:5000;padding:8px;background:#d9af3c;color:#07111e;text-align:center;font:600 13px sans-serif">'+label+' · <a href="'+SETTINGS.central_auth_url+'/portal">Portal</a> · <a href="/campaigns">Focus Intelligence</a></div>'
        response = HTMLResponse(body.replace('<div class="portal-app">', banner+'<div class="portal-app">', 1),status_code=response.status_code, headers={"X-Focus-Environment":"sandbox","Cache-Control":"no-store"})
    if not request.cookies.get(auth.CSRF_COOKIE):
        response.set_cookie(auth.CSRF_COOKIE, secrets.token_urlsafe(24), samesite="lax", secure=request.url.scheme=="https")
    return response


if REAL_MODE:
    from app.sandbox_real import configure
    campaign_engine=configure(app)
else:
    from app.intelligence import CampaignEngine
    from app.intelligence_api import campaign_router
    campaign_engine=CampaignEngine(ROOT/"campaigns.sqlite3",Path(__file__).with_name("intelligence_seed.json"))
    app.include_router(campaign_router(campaign_engine,auth.require_identity,main._template))

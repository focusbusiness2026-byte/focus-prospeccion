from __future__ import annotations

import csv
import io
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, HttpUrl

from app.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Identity,
    create_session,
    new_csrf_token,
    require_identity,
    validate_csrf,
    verify_google_credential,
)
from app.config import get_settings
from app.db import create_schema, session_scope
from app.enrichment import GeminiAnalyzer, PublicWebScraper
from app.services import ensure_demo_client
from app.sheet_store import SheetStore


class GoogleCredential(BaseModel):
    credential: str


class ScrapeRequest(BaseModel):
    company: str
    website: HttpUrl


class LeadStatusRequest(BaseModel):
    status: Literal["Nuevo", "Aprobado", "Descartado"]


def _demo_payload(identity: Identity, gemini_budget: int) -> dict:
    prospects = [
        {
            "execution_id": "DEMO-VERDE-001",
            "email": identity.email,
            "created_at": "2026-08-10T09:30:00+00:00",
            "company": "Estudio Horizonte",
            "website": "https://example.com",
            "title": "Estudio Horizonte",
            "description": "Productora audiovisual B2B con proyectos corporativos.",
            "sector": "Produccion audiovisual",
            "business_model": "B2B",
            "city": "Madrid",
            "employees": 42,
            "score": 8.4,
            "classification": "green",
            "summary": "Empresa con web activa, equipo consolidado y senales de crecimiento.",
            "entry_angle": "Proponer una estrategia de captacion para sus servicios corporativos.",
            "social_links": {"linkedin": "https://www.linkedin.com", "instagram": "https://www.instagram.com"},
            "evidence": ["https://example.com", "https://example.com/contacto"],
            "lead_status": "Aprobado",
            "updated_at": "2026-08-10T09:35:00+00:00",
        },
        {
            "execution_id": "DEMO-AMARILLO-002",
            "email": identity.email,
            "created_at": "2026-08-09T16:10:00+00:00",
            "company": "Norte Visual",
            "website": "https://example.org",
            "title": "Norte Visual",
            "description": "Agencia creativa con servicios de video y contenido.",
            "sector": "Agencia creativa",
            "business_model": "B2B",
            "city": "Barcelona",
            "employees": 18,
            "score": 5.8,
            "classification": "yellow",
            "summary": "Encaje posible, aunque faltan senales recientes de compra.",
            "entry_angle": "Validar volumen de proyectos y necesidad de apoyo comercial.",
            "social_links": {"linkedin": "https://www.linkedin.com"},
            "evidence": ["https://example.org"],
            "lead_status": "Nuevo",
            "updated_at": "2026-08-09T16:10:00+00:00",
        },
    ]
    return {
        "user": {"email": identity.email, "role": identity.role, "assigned": 10, "used": 2, "available": 8},
        "global": {
            "active_users": 1,
            "assigned": 10,
            "used": 2,
            "remaining": 8,
            "remaining_ratio": 0.8,
            "state": "green",
            "gemini_internal_budget": gemini_budget,
            "gemini_requests_used": 2,
            "gemini_requests_remaining": max(0, gemini_budget - 2),
            "failed_requests": 0,
        },
        "metrics": {"total": 2, "classifications": {"green": 1, "yellow": 1, "red": 0}, "statuses": {"Nuevo": 1, "Aprobado": 1, "Descartado": 0}},
        "prospects": prospects,
        "executions": [
            {"execution_id": item["execution_id"], "created_at": item["created_at"], "email": identity.email, "company": item["company"], "website": item["website"], "status": "Completado", "gemini_model": "demo", "prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0, "error": ""}
            for item in prospects
        ],
        "demo": True,
    }


def _csv_cell(value) -> str:
    text = str(value if value is not None else "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    if settings.app_env != "production":
        create_schema()
        with session_scope() as session:
            ensure_demo_client(session)
    yield


app = FastAPI(title="Focus Prospeccion", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _template(request: Request, name: str, **context):
    response = templates.TemplateResponse(request=request, name=name, context=context)
    if not request.cookies.get(CSRF_COOKIE):
        response.set_cookie(CSRF_COOKIE, new_csrf_token(), httponly=False, secure=get_settings().app_env == "production", samesite="lax")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    from app.auth import verify_session

    if verify_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/portal", status_code=303)
    settings = get_settings()
    return _template(
        request,
        "login.html",
        google_oauth_client_id=settings.google_oauth_client_id,
        demo_enabled=settings.demo_auth_bypass and settings.app_env != "production",
    )


@app.get("/portal", response_class=HTMLResponse)
def portal(request: Request):
    from app.auth import verify_session

    identity = verify_session(request.cookies.get(SESSION_COOKIE))
    if not identity:
        return RedirectResponse("/", status_code=303)
    return _template(request, "portal.html", identity=identity)


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "sheets_configured": bool(settings.google_sheets_enabled and settings.google_service_account_json),
        "google_login_configured": bool(settings.google_oauth_client_id),
        "gemini_configured": bool(settings.gemini_api_key),
    }


@app.post("/auth/google")
def google_login(payload: GoogleCredential, request: Request):
    validate_csrf(request)
    try:
        google_info = verify_google_credential(payload.credential)
        access = SheetStore().get_access(google_info["email"])
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="No se pudo verificar la cuenta de Google") from exc
    if not access:
        raise HTTPException(status_code=403, detail="Este correo no esta activo en la hoja Accesos")
    identity = Identity(email=access.email, role=access.role, google_sub=google_info["sub"])
    response = JSONResponse({"ok": True, "redirect": "/portal"})
    response.set_cookie(
        SESSION_COOKIE,
        create_session(identity),
        httponly=True,
        secure=get_settings().app_env == "production",
        samesite="lax",
        max_age=8 * 60 * 60,
    )
    return response


@app.post("/auth/demo")
def demo_login(request: Request):
    validate_csrf(request)
    settings = get_settings()
    if settings.app_env == "production" or not settings.demo_auth_bypass:
        raise HTTPException(status_code=404)
    response = JSONResponse({"ok": True, "redirect": "/portal"})
    response.set_cookie(
        SESSION_COOKIE,
        create_session(Identity("demo@focus.local", "Administrador", "demo")),
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=8 * 60 * 60,
    )
    return response


@app.post("/auth/logout")
def logout(request: Request):
    validate_csrf(request)
    response = JSONResponse({"ok": True, "redirect": "/"})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/portal-dashboard")
def portal_dashboard(identity: Identity = Depends(require_identity)):
    settings = get_settings()
    if not settings.google_sheets_enabled:
        return _demo_payload(identity, settings.gemini_request_budget)
    store = SheetStore()
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = "admin" in access.role.lower()
    scope_email = None if is_admin else identity.email
    global_metrics = store.global_metrics() if is_admin else None
    return {
        "user": {
            "email": access.email,
            "role": access.role,
            "assigned": access.assigned,
            "used": access.used,
            "available": access.available,
        },
        "global": global_metrics,
        "metrics": store.prospect_metrics(scope_email),
        "prospects": store.recent_prospects(scope_email),
        "executions": store.recent_executions(scope_email),
        "demo": False,
    }


@app.post("/api/prospects/{execution_id}/status")
def update_prospect_status(
    execution_id: str,
    payload: LeadStatusRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Los cambios no se guardan en el modo demo")
    store = SheetStore()
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    try:
        prospect = store.update_prospect_status(
            execution_id,
            identity.email,
            payload.status,
            is_admin="admin" in access.role.lower(),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "prospect": prospect}


@app.get("/api/prospects/export.csv")
def export_prospects(identity: Identity = Depends(require_identity)):
    settings = get_settings()
    if not settings.google_sheets_enabled:
        prospects = _demo_payload(identity, settings.gemini_request_budget)["prospects"]
    else:
        store = SheetStore()
        access = store.get_access(identity.email)
        if not access:
            raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
        prospects = store.recent_prospects(None if "admin" in access.role.lower() else identity.email, limit=1000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Empresa", "Web", "Sector", "Ciudad", "Empleados", "Score", "Clasificacion", "Estado", "Resumen", "Angulo de entrada", "Correo de cuenta", "Fecha", "ID ejecucion"])
    for item in prospects:
        writer.writerow([_csv_cell(value) for value in [
            item["company"], item["website"], item["sector"], item["city"], item["employees"],
            item["score"], item["classification"], item["lead_status"], item["summary"], item["entry_angle"],
            item["email"], item["created_at"], item["execution_id"],
        ]])
    content = "\ufeff" + buffer.getvalue()
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=focus-prospeccion-leads.csv"},
    )


@app.post("/api/scrape")
def scrape_company(payload: ScrapeRequest, request: Request, identity: Identity = Depends(require_identity)):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Google Sheets no esta activado")
    store = SheetStore()
    execution_id = str(uuid.uuid4())
    website = str(payload.website)
    reserved = False
    try:
        store.reserve_execution(identity.email)
        reserved = True
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY_REQUIRED")
        evidence = PublicWebScraper().scrape(website)
        analysis, usage = GeminiAnalyzer().analyze(payload.company.strip(), evidence)
        result = {
            **analysis,
            "execution_id": execution_id,
            "email": identity.email,
            "company": payload.company.strip(),
            "website": website,
            "title": evidence.title,
            "description": evidence.description,
            "social_links": evidence.social_links,
            "evidence": evidence.pages,
        }
        store.append_prospect(result)
        store.append_execution(
            execution_id=execution_id,
            email=identity.email,
            company=payload.company.strip(),
            website=website,
            status="Completado",
            gemini_model=settings.gemini_model,
            prompt_tokens=usage["prompt_tokens"],
            output_tokens=usage["output_tokens"],
            total_tokens=usage["total_tokens"],
        )
        return {"ok": True, "execution_id": execution_id, "result": result}
    except Exception as exc:
        if reserved:
            try:
                store.refund_execution(identity.email)
            except Exception:
                pass
        try:
            store.append_execution(
                execution_id=execution_id,
                email=identity.email,
                company=payload.company.strip(),
                website=website,
                status="Fallido",
                gemini_model=settings.gemini_model,
                prompt_tokens=0,
                output_tokens=0,
                total_tokens=0,
                error=str(exc)[:500],
            )
        except Exception:
            pass
        if str(exc) == "GEMINI_API_KEY_REQUIRED":
            raise HTTPException(status_code=503, detail="Falta configurar la clave de Gemini en Render") from exc
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        raise HTTPException(status_code=502, detail=f"No se pudo completar el raspado: {str(exc)[:300]}") from exc

"""Private Prospección review using an independent native Sheets workbook."""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings


PRODUCTION_WORKBOOK = "1FTWbZ1gDpA4RezEz89w9PmbRjwx_2bX2mhzF67V8wiE"
SCHEDULE_PREFIX = "Revisión guiada · "
ACCOUNTS_PATH = "/api/prospecting-review/accounts"


def validate_configuration(settings, expected_id: str, directory: str) -> Path:
    if (not re.fullmatch(r"[A-Za-z0-9_-]{20,}", expected_id)
            or expected_id == PRODUCTION_WORKBOOK or settings.google_sheet_id != expected_id):
        raise RuntimeError("La revisión exige un Google Sheet separado y con ID confirmado.")
    if not settings.google_sheets_enabled or not settings.google_service_account_json:
        raise RuntimeError("Falta configurar el acceso de esta revisión a Google Sheets.")
    if not settings.central_auth_enabled or settings.app_env != "production":
        raise RuntimeError("La revisión conectada exige el acceso central y cookies seguras.")
    public = urlsplit(settings.public_base_url)
    if (public.scheme != "https" or not (public.hostname or "").endswith(".focusbusinesslab.es")
            or public.hostname in {"prospeccion.focusbusinesslab.es", "focus-prospeccion-fb.onrender.com"}):
        raise RuntimeError("La revisión necesita un subdominio HTTPS propio de focusbusinesslab.es.")
    path = Path(directory)
    if not directory or not path.is_absolute():
        raise RuntimeError("Indica un directorio de datos propio y persistente para la revisión.")
    if not settings.database_url.startswith("sqlite:"):
        raise RuntimeError("La revisión no admite la base de datos de otro servicio.")
    return path


settings = get_settings()
storage = validate_configuration(
    settings, os.environ.get("REVIEW_GOOGLE_SHEET_ID", "").strip(), os.environ.get("REVIEW_DATA_DIR", ""),
)

from app import main
from app.auth import require_identity
from app.intelligence import CampaignEngine
from app.intelligence_adapters import SheetsSnapshot
from app.onboarding import OnboardingSource
from app.prospecting_guide import (
    BUSINESS_CONTEXT_FIELDS, INTAKE_EDITABLE, OpenAITargetingGuide, TargetingGuide, guide_intake, review_feedback,
)
from app.prospecting_guide_api import guide_router
from app.review_sheet_store import ReviewSheetStore as SheetStore


app = main.app
main.SheetStore = SheetStore
snapshot = SheetsSnapshot(settings, "Prospeccion Revision Estado", create_hidden=True)
guide_store = CampaignEngine(storage / "targeting.sqlite3", persistence=snapshot, real=True)
guide = TargetingGuide(guide_store, OpenAITargetingGuide(settings) if settings.openai_api_key else None)
schedule_enabled = (
    settings.auto_research_enabled and bool(settings.openai_api_key)
    and os.environ.get("REVIEW_ENABLE_SCHEDULER") == "1"
)


@asynccontextmanager
async def review_lifespan(_):
    task = None
    if schedule_enabled:
        task = asyncio.create_task(main._automation_loop(settings.auto_research_poll_seconds, SCHEDULE_PREFIX))
    try:
        yield
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


app.router.lifespan_context = review_lifespan


@app.middleware("http")
async def review_layout(request: Request, call_next):
    if request.url.path.startswith("/api/internal/"):
        return JSONResponse({"detail": "No disponible en la copia de revisión."}, status_code=404)
    if request.url.path == "/portal":
        embedded = request.query_params.get("crm_embed") == "1"
        request.state.integrated_guide = not embedded
        request.state.review_crm_embed = embedded
        request.state.guide_accounts_url = ACCOUNTS_PATH
    if request.url.path == "/prospecting-guide" and request.method in {"GET", "HEAD"}:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/portal{query}#sources", status_code=303)
    response = await call_next(request)
    # Onboarding only redirects to its existing destinations. The shared
    # domain cookie works here after sign-in in the separate tab.
    location = response.headers.get("location", "")
    access_page = settings.central_auth_url.rstrip("/") + "/access"
    if request.url.path in {"/", "/portal"} and location.startswith(access_page + "?"):
        return main._template(request, "connected-review-login.html", access_page=access_page)
    return response


def account_scope(store, identity):
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(403, "Acceso retirado en la hoja de revisión.")
    if main._is_authorized_admin(identity, access, settings):
        return None
    return main._account_scope_email(identity, access)


@app.get(ACCOUNTS_PATH)
def accounts(identity=Depends(require_identity)):
    store = SheetStore(settings)
    scope = account_scope(store, identity)
    sources = main._logical_onboarding_sources(store.onboarding_sources(scope))
    return {"accounts": [
        {"id": source.record_id, "source_id": source.record_id, "name": source.company}
        for source, _aliases in sources
    ]}


def source_for(tenant, identity, store=None):
    store = store or SheetStore(settings)
    source = store.get_onboarding_source(tenant, account_scope(store, identity))
    if not source:
        raise HTTPException(404, "No se encontró una ficha autorizada para esta empresa.")
    return source


def profile(source):
    result = main._questionnaire_source_profile(source)
    result["business_context"] = {
        key: source.raw_form[key] for key in BUSINESS_CONTEXT_FIELDS if source.raw_form.get(key)
    }
    return result


def guide_profile(tenant, identity):
    return profile(source_for(tenant, identity))


def intake(tenant, identity):
    source = source_for(tenant, identity)
    return profile(source), guide_intake(source)


@main._serialize_portal_sheets
def update_intake(tenant, identity, field, before, value):
    store = SheetStore(settings)
    source = source_for(tenant, identity, store)
    tab = f"'{settings.google_onboarding_tab}'"
    rows = store._get(f"{tab}!A1:ZZ1000")
    headers = [str(cell).strip() for cell in rows[0]] if rows else []
    if field not in INTAKE_EDITABLE or headers.count(field) != 1 or headers.count("ID registro") != 1:
        raise HTTPException(409, "Este campo requiere un encabezado único en Onboarding.")
    id_column = headers.index("ID registro")
    matches = [(index, row) for index, row in enumerate(rows[1:], 2)
               if len(row) > id_column and str(row[id_column]).strip() == source.record_id]
    if len(matches) != 1:
        raise HTTPException(409, "No se encontró una ficha única para editar.")
    row_number, row = matches[0]
    record = dict(zip(headers, row))
    if OnboardingSource.from_sheet_record(record).email != source.email:
        raise HTTPException(409, "La cuenta de la ficha cambió. Actualiza antes de guardar.")
    current = str(record.get(field) or "").strip()
    if current != value:
        if current != before:
            raise HTTPException(409, "El campo cambió en otra sesión. Actualiza la ficha.")
        column = store._column_name(headers.index(field) + 1)
        store._update(f"{tab}!{column}{row_number}", [[value]], raw=True)
        record[field] = value
    updated = OnboardingSource.from_sheet_record(record)
    return profile(updated), guide_intake(updated)


def feedback(tenant, identity):
    store = SheetStore(settings)
    source = source_for(tenant, identity, store)
    return review_feedback(store.recent_prospects(source.email, limit=1000), source.record_id, source.email)


def save(tenant, identity, name, criteria):
    store = SheetStore(settings)
    source = source_for(tenant, identity, store)
    return store.upsert_saved_search(source.record_id, source.email, name=name,
        adjustments=main.ResearchAdjustments.model_validate(criteria).model_dump(),
        favorite=False, created_by_email=identity.email, created_by_role=identity.role)


def research(record_id, criteria, request, background_tasks, identity):
    return main.start_research_job(record_id, main.ResearchAdjustments.model_validate(criteria),
                                   request, background_tasks, identity)


def rows_for(store, source, ids):
    if not ids:
        return []
    return [row for row in store.recent_prospects(source.email, limit=1000)
            if row["onboarding_id"] == source.record_id and row["execution_id"] in ids]


def leads(tenant, identity, ids):
    store = SheetStore(settings)
    source = source_for(tenant, identity, store)
    return main.decorate_prospects(rows_for(store, source, set(ids)), store.review_events(source.email),
                                   require_admin_review=settings.lead_admin_review_required)


def job(tenant, identity):
    store = SheetStore(settings)
    source = source_for(tenant, identity, store)
    result = main.latest_manual_research_job(source.record_id, identity.email)
    if result and result["status"] == "completed":
        ids = {lead["execution_id"] for lead in result.get("saved_leads", [])}
        result["highlights"] = [
            {"company": lead["company"], "facts": [lead[key] for key in ("sector", "city", "country") if lead.get(key)],
             "finding": lead.get("summary") or "", "potential": lead.get("entry_angle") or ""}
            for lead in rows_for(store, source, ids)
        ]
    return result


def schedule_view(config):
    batch = (config or {}).get("batch")
    if not batch or not str(config.get("name", "")).startswith(SCHEDULE_PREFIX):
        return None
    return {**{key: config.get(key) for key in ("enabled", "interval_minutes", "next_run_at", "last_status")},
            **{key: batch.get(key) for key in ("max_total_runs", "completed_runs", "in_flight")}}


def schedule_status(tenant, identity):
    store = SheetStore(settings)
    return schedule_view(store.get_automation_config(source_for(tenant, identity, store).record_id))


def schedule(record_id, sid, criteria, interval, total, request, identity):
    payload = main.AutomationRequest(
        name=SCHEDULE_PREFIX + sid, enabled=True, interval_minutes=interval, max_total_runs=total,
        adjustments=main.ResearchAdjustments.model_validate(criteria),
    )
    return schedule_view(main.save_onboarding_automation(record_id, payload, request, identity)["automation"])


def pause(tenant, identity):
    store = SheetStore(settings)
    current = store.get_automation_config(source_for(tenant, identity, store).record_id)
    if not schedule_view(current):
        raise HTTPException(409, "No hay una programación de la revisión para esta empresa.")
    if current["batch"].get("in_flight"):
        raise HTTPException(409, "El ciclo está en curso. Podrás pausar al terminar.")
    return schedule_view(store.pause_bounded_automation(current))


app.include_router(guide_router(
    guide, require_identity, main._template, guide_profile, save, feedback, intake, update_intake,
    research if settings.openai_api_key else None, schedule if schedule_enabled else None,
    schedule_status, pause if schedule_enabled else None, job, leads, authorize_for=source_for,
))

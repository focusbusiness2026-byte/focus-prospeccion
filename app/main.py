from __future__ import annotations

import csv
import asyncio
import io
import json
import re
import secrets
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from threading import Lock
from typing import Callable, Literal
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

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
from app.dedupe import company_dedupe_key
from app.enrichment import OpenAIProspectDiscovery
from app.keepalive import render_keepalive_loop, render_keepalive_ready
from app.lead_reviews import (
    ADMIN_DECISIONS,
    CLIENT_DECISIONS,
    decorate_prospects,
    summary_request_preview,
)
from app.services import ensure_demo_client
from app.sheet_store import SheetStore


class GoogleCredential(BaseModel):
    credential: str


class LeadStatusRequest(BaseModel):
    status: Literal["Nuevo", "Aprobado", "Descartado", "En revisión", "Cerrado"]


class LeadDecisionRequest(BaseModel):
    decision: Literal["Aprobado", "Descartado", "En revisión", "Confirmada", "Rechazada"]
    reason: str = Field(default="", max_length=500)


class LeadSummaryRequest(BaseModel):
    confirmed: Literal[True]
    note: str = Field(default="", max_length=500)


class DeleteLeadRequest(BaseModel):
    confirmation: Literal["ELIMINAR"]


class CRMRequest(BaseModel):
    status: Literal["Nuevo", "Aprobado", "Descartado", "En revisión", "Cerrado"]
    owner: str = ""
    notes: str = ""
    next_action: str = ""
    follow_up_date: str = ""
    warmup_preparation: Literal["No iniciada", "Preparada", "En revisión"] = "No iniciada"
    warmup_approval: Literal["Pendiente", "Aprobada", "Rechazada"] = "Pendiente"


class ResearchAdjustments(BaseModel):
    lead_count: int = Field(default=5, ge=1, le=5)
    target_city: str = ""
    target_region: str = ""
    target_countries: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    excluded_sectors: list[str] = Field(default_factory=list)
    client_types: list[str] = Field(default_factory=list)
    organization_types: list[str] = Field(default_factory=list)
    business_models: list[str] = Field(default_factory=list)
    sales_models: list[str] = Field(default_factory=list)
    employee_ranges: list[str] = Field(default_factory=list)
    revenue_ranges: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    opportunity_signals: list[str] = Field(default_factory=list)
    decision_roles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    lookalike_companies: list[str] = Field(default_factory=list)
    ideal_company_size: str = ""
    minimum_budget: str = ""
    exclusions: str = ""
    preferences: str = ""
    hiring_recency: str = ""
    funding_recency: str = ""
    require_marketing_department: bool = False
    require_sales_team: bool = False
    require_ad_investment: bool = False
    require_active_linkedin: bool = False
    require_updated_website: bool = False
    require_identifiable_decision_maker: bool = False
    exclude_current_clients: bool = True
    exclude_contacted_companies: bool = True
    exclude_competitors: bool = True


class OnboardingTrigger(BaseModel):
    onboarding_id: str
    prepare_only: bool = True


class AutomationRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    enabled: bool = False
    favorite: bool = False
    interval_minutes: int = Field(default=1440, ge=5, le=4320)
    adjustments: ResearchAdjustments = Field(default_factory=ResearchAdjustments)


_RESEARCH_JOBS: dict[str, dict] = {}
_ACTIVE_RESEARCH_JOBS: dict[tuple[str, str], str] = {}
_RESEARCH_JOBS_LOCK = Lock()
_RESEARCH_JOB_LIMIT = 100


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_research_job(job: dict) -> dict:
    return {
        key: job.get(key)
        for key in (
            "job_id",
            "source_id",
            "status",
            "phase",
            "progress",
            "message",
            "leads_found",
            "saved_leads",
            "execution_id",
            "created_at",
            "updated_at",
        )
    }


def _cleanup_research_jobs_locked() -> None:
    if len(_RESEARCH_JOBS) <= _RESEARCH_JOB_LIMIT:
        return
    removable = sorted(
        (
            job for job in _RESEARCH_JOBS.values()
            if job.get("status") in {"completed", "failed"}
        ),
        key=lambda item: item.get("_updated_epoch", 0),
    )
    for job in removable[: max(0, len(_RESEARCH_JOBS) - _RESEARCH_JOB_LIMIT)]:
        _RESEARCH_JOBS.pop(job["job_id"], None)


def _update_research_job(job_id: str, **changes) -> dict | None:
    with _RESEARCH_JOBS_LOCK:
        job = _RESEARCH_JOBS.get(job_id)
        if not job:
            return None
        latest_lead = changes.pop("latest_lead", None)
        if latest_lead:
            saved = list(job.get("saved_leads") or [])
            if not any(item.get("execution_id") == latest_lead.get("execution_id") for item in saved):
                saved.append(latest_lead)
            changes["saved_leads"] = saved[-5:]
        job.update(changes)
        job["updated_at"] = _utc_now()
        job["_updated_epoch"] = time.time()
        return dict(job)


def _safe_research_failure(exc: Exception) -> str:
    if str(exc) == "OPENAI_API_KEY_REQUIRED":
        return "La investigación no pudo iniciarse porque el proveedor no está configurado."
    if isinstance(exc, PermissionError):
        return "La investigación fue bloqueada por los límites o permisos de la cuenta."
    if isinstance(exc, ValueError):
        return str(exc)[:240]
    return "La investigación no pudo completarse. Revisa Ejecuciones o inténtalo de nuevo."


def _is_authorized_admin(identity: Identity, access, settings) -> bool:
    allowed = settings.admin_emails or {"servicemanagerbossio@gmail.com"}
    return "admin" in access.role.lower() and identity.email.strip().lower() in allowed


def _admin_available_users(store: SheetStore, sources: list) -> list[dict]:
    users: dict[str, dict] = {}
    for item in store.access_records():
        if item.state.lower() != "activo" or "admin" in item.role.lower():
            continue
        users[item.email] = {"email": item.email, "role": item.role, "onboarding_count": 0}
    for source in sources:
        email = source.email.strip().lower()
        if not email:
            continue
        user = users.setdefault(email, {"email": email, "role": "Cliente registrado", "onboarding_count": 0})
        user["onboarding_count"] += 1
    return sorted(users.values(), key=lambda item: item["email"])


def _safe_package_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.")
    return normalized[:80] or "cliente"


def _build_client_package(source, prospect: dict) -> bytes:
    form = {key: value for key, value in source.raw_form.items() if str(value).strip()}
    missing = [key for key, value in source.raw_form.items() if not str(value).strip()]
    profile = source.as_dict()
    instructions = f"""INSTRUCCIONES PARA EL EQUIPO DE IMPLEMENTACION

Cliente: {source.company or 'Pendiente'}
Registro de onboarding: {source.record_id or 'Pendiente'}
Lead relacionado: {prospect.get('company') or 'Pendiente'} ({prospect.get('execution_id') or 'sin ID'})

OBJETIVO
Preparar y validar la configuracion solicitada por el cliente en su subcuenta de GoHighLevel usando exclusivamente la informacion confirmada de este paquete.

PROCEDIMIENTO OBLIGATORIO
1. Inspeccionar primero la subcuenta autorizada en modo lectura: campos, contactos, pipeline, calendarios, usuarios, integraciones y workflows existentes.
2. Comparar esa evidencia con datos_formulario.json, perfil_normalizado.json y datos_lead.json.
3. Presentar el mapeo de campos estandar y personalizados, los flujos propuestos, conexiones necesarias, conflictos, duplicados, costes y campos faltantes.
4. No crear, actualizar, conectar, activar, enviar ni consumir nada sin aprobacion final explicita.
5. Usar OAuth o el mecanismo oficial solo en el momento autorizado. No pedir, copiar ni guardar contraseñas, tokens, claves API o secretos dentro del paquete.

CONFIGURACION A PREPARAR
- Contactos y campos: nombre, email, telefono, empresa, cargo, web, LinkedIn, ciudad, pais, fuente, busqueda, etapa, score y campos confirmados del formulario.
- Pipeline: proponer etapas y reglas basadas en las solicitudes confirmadas; no inventar estados ausentes.
- Calendarios y reuniones: configurar solo si el formulario contiene requisitos suficientes.
- Workflows: documentar disparador, condiciones, esperas, responsables, mensajes y salida; cualquier mensajeria o accion con coste requiere aprobacion final.
- Conexiones: enumerar solo las confirmadas. Marcar las demas como pendientes de autorizacion o credenciales en la plataforma correspondiente.

CAMPOS FALTANTES
{chr(10).join('- ' + item for item in missing) if missing else '- No se detectaron campos vacios en el registro disponible.'}

RESTRICCIONES
No contiene secretos. No asumir conexiones, permisos, presupuestos, remitentes, dominios, numeros o credenciales. Detener la ejecucion ante cualquier dato material no confirmado y pedir la decision exacta.
"""
    readme = f"""PAQUETE DEL CLIENTE - FOCUS BUSINESS

Cliente: {source.company or 'Pendiente'}
Generado para revision e implementacion controlada.

Contenido:
- datos_formulario.json: respuestas disponibles del onboarding, sin secretos.
- perfil_normalizado.json: resumen estructurado y campos pendientes.
- datos_lead.json: datos publicos y CRM del lead seleccionado.
- INSTRUCCIONES_EQUIPO_TECNICO.txt: procedimiento para preparar GoHighLevel.
- CAMPOS_FALTANTES.txt: valores que deben confirmarse antes de ejecutar.

Este paquete no conecta servicios ni ejecuta cambios. Toda accion posterior requiere cuentas autorizadas y aprobacion final.
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        archive.writestr("datos_formulario.json", json.dumps(form, ensure_ascii=False, indent=2))
        archive.writestr("perfil_normalizado.json", json.dumps(profile, ensure_ascii=False, indent=2))
        archive.writestr("datos_lead.json", json.dumps(prospect, ensure_ascii=False, indent=2))
        archive.writestr("INSTRUCCIONES_EQUIPO_TECNICO.txt", instructions)
        archive.writestr("CAMPOS_FALTANTES.txt", "\n".join(missing) if missing else "Sin campos vacios detectados.")
    return output.getvalue()


def _demo_payload(identity: Identity, openai_budget: int) -> dict:
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
            "research_sources": [{"url": "https://example.com", "title": "Fuente de demostración", "type": "company_website"}],
            "search_queries": ["empresa audiovisual corporativa Madrid"],
            "web_search_calls": 1,
            "web_search_call_limit": 5,
            "public_contacts": [{"type": "email", "value": "info@example.com", "source_url": "https://example.com/contacto"}],
            "decision_makers": [{"name": "Responsable de marketing", "role": "Dirección de marketing", "public_contact": "", "source_url": "https://example.com/contacto"}],
            "public_signals": [],
            "public_signals_status": "No encontrado públicamente",
            "prospect_found": True,
            "no_prospect_reason": "",
            "no_contacts_reason": "",
            "country": "España",
            "client_type": "Empresa privada B2B",
            "onboarding_id": "ONB-DEMO0001",
            "productora": "Productora Demo Focus",
            "crm_owner": "Alberto",
            "crm_notes": "Validar encaje en la próxima revisión.",
            "crm_next_action": "Revisar decisor",
            "crm_follow_up_date": "2026-08-20",
            "lead_status": "Aprobado",
            "warmup_preparation": "No iniciada",
            "warmup_approval": "Pendiente",
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
            "research_sources": [{"url": "https://example.org", "title": "Fuente de demostración", "type": "company_website"}],
            "search_queries": ["agencia creativa Barcelona B2B"],
            "web_search_calls": 1,
            "web_search_call_limit": 5,
            "public_contacts": [],
            "decision_makers": [],
            "public_signals": [],
            "public_signals_status": "No encontrado públicamente",
            "prospect_found": True,
            "no_prospect_reason": "",
            "no_contacts_reason": "No se encontraron contactos públicos verificables.",
            "country": "España",
            "client_type": "Agencia",
            "onboarding_id": "ONB-DEMO0001",
            "productora": "Productora Demo Focus",
            "crm_owner": "",
            "crm_notes": "",
            "crm_next_action": "",
            "crm_follow_up_date": "",
            "lead_status": "Nuevo",
            "warmup_preparation": "No iniciada",
            "warmup_approval": "Pendiente",
            "updated_at": "2026-08-09T16:10:00+00:00",
        },
    ]
    prospects = decorate_prospects(
        prospects,
        [{
            "event_id": "DEMO-DECISION-001",
            "event_type": "client_decision",
            "onboarding_id": "ONB-DEMO0001",
            "owner_email": identity.email,
            "execution_id": "DEMO-VERDE-001",
            "actor_email": identity.email,
            "actor_role": "Cliente",
            "decision": "Aprobado",
            "reason": "Ejemplo local para revisar la trazabilidad.",
            "created_at": "2026-08-10T10:00:00+00:00",
        }],
    )
    return {
        "user": {"email": identity.email, "role": identity.role, "assigned": 10, "used": 2, "available": 8},
        "global": {
            "active_users": 1,
            "assigned": 10,
            "used": 2,
            "remaining": 8,
            "remaining_ratio": 0.8,
            "state": "green",
            "openai_internal_budget": openai_budget,
            "openai_requests_used": 2,
            "openai_requests_remaining": max(0, openai_budget - 2),
            "openai_web_search_calls_used": 2,
            "failed_requests": 0,
        },
        "metrics": {"total": 2, "classifications": {"green": 1, "yellow": 1, "red": 0}, "statuses": {"Nuevo": 1, "Aprobado": 1, "Descartado": 0}},
        "prospects": prospects,
        "executions": [
            {"execution_id": item["execution_id"], "created_at": item["created_at"], "email": identity.email, "company": item["company"], "website": item["website"], "status": "Completado", "model": "demo", "prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0, "error": "", "onboarding_id": "ONB-DEMO0001", "productora": "Productora Demo Focus", "web_search_calls": 1, "web_search_call_limit": 5, "search_queries": item["search_queries"], "research_sources": item["research_sources"], "no_prospect_reason": "", "research_summary": item["summary"], "research_provider": "OpenAI Responses API + web_search (demo)"}
            for item in prospects
        ],
        "sources": [
            {
                "onboarding_id": "ONB-DEMO0001",
                "productora": {
                    "name": "Productora Demo Focus",
                    "website": "https://example.com",
                    "email": identity.email,
                    "activity": "Productora audiovisual",
                    "location": "Madrid, España",
                    "description": "Fuente ficticia para validar la interfaz local.",
                },
                "targeting": {
                    "main_service": "Producción audiovisual",
                    "services": ["Vídeo corporativo"],
                    "audience": ["B2B"],
                    "sectors": ["Tecnología"],
                    "markets": ["España"],
                    "target_city": "Madrid",
                    "target_region": "Comunidad de Madrid",
                    "target_countries": ["España"],
                    "target_client_types": ["Empresa privada B2B"],
                    "ideal_company_size": "11–50 empleados",
                    "decision_maker": "Dirección de marketing",
                    "minimum_budget": "3.000 €",
                    "monthly_capacity": "2–3 proyectos",
                    "prospect_exclusions": "Clientes actuales y competidores directos",
                    "prospect_preferences": "Empresas con marketing activo",
                    "objectives": ["Captar clientes B2B"],
                },
                "submitted_at": "2026-08-10T09:00:00+00:00",
                "status": "Nuevo",
                "ready": True,
                "blockers": [],
                "automation_state": "Pendiente de configurar OpenAI",
                "automation": {
                    "enabled": False,
                    "interval_minutes": 1440,
                    "next_run_at": "",
                    "last_run_at": "",
                    "last_status": "Desactivada",
                    "adjustments": {},
                },
                "openai_configured": False,
                "lead_summary_request": {
                    "decision": "No solicitada",
                    "created_at": "",
                    "actor_email": "",
                    "result_status": "No iniciado",
                    "result_ref": "",
                },
            }
        ],
        "source_metrics": {"total": 1, "ready": 1, "blocked": 0},
        "automation_engine_enabled": False,
        "approval_policy": {"admin_review_required": False},
        "demo": True,
    }


def _csv_cell(value) -> str:
    text = str(value if value is not None else "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _source_view(source, settings, latest: dict | None = None, automation: dict | None = None) -> dict:
    data = source.as_dict()
    configured = bool(settings.openai_api_key)
    if latest:
        automation_state = latest["status"]
    elif not source.ready:
        automation_state = "Bloqueado por datos incompletos"
    elif not configured:
        automation_state = "Pendiente de configurar OpenAI"
    else:
        automation_state = "Listo para investigación automática"
    schedule = automation or {
        "name": f"Prospección · {source.company}",
        "favorite": False,
        "enabled": False,
        "interval_minutes": 1440,
        "next_run_at": "",
        "last_run_at": "",
        "last_status": "Desactivada",
        "adjustments": {},
    }
    return {
        **data,
        "automation_state": automation_state,
        "automation": schedule,
        "openai_configured": configured,
        "latest_execution": latest,
    }


def _run_onboarding_research(
    source,
    store: SheetStore,
    adjustments: dict | None = None,
    *,
    actor_email: str = "",
    actor_role: str = "Cliente",
    execution_origin: str = "manual",
    bypass_user_limit: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    def report(**update) -> None:
        if progress_callback:
            progress_callback(update)

    settings = get_settings()
    if not source.ready:
        raise ValueError("; ".join(source.blockers))
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY_REQUIRED")
    execution_id = str(uuid.uuid4())
    reserved = False
    try:
        report(
            phase="preparing",
            progress=5,
            message="Validando la configuración y los límites de la cuenta…",
        )
        store.ensure_operational_schema()
        if not bypass_user_limit:
            store.reserve_execution(source.email)
            reserved = True
        report(
            phase="searching",
            progress=20,
            message="Buscando empresas reales en fuentes públicas verificables…",
        )
        prospects, trace = OpenAIProspectDiscovery(settings).discover(source.prospecting_profile(), adjustments)
        report(
            phase="validating",
            progress=65,
            message="Validando evidencia pública y descartando resultados duplicados…",
        )
        existing_keys = store.existing_prospect_keys(source.record_id)
        discovered_count = len(prospects)
        prospects = [
            prospect for prospect in prospects
            if company_dedupe_key({"commercial_name": prospect.get("company", ""), "website": prospect.get("website", ""), "city": prospect.get("city", "")}) not in existing_keys
        ]
        duplicates_discarded = discovered_count - len(prospects)
        target_leads = 5
        missing_leads = max(0, target_leads - len(prospects))
        if not prospects and duplicates_discarded:
            trace["no_prospect_reason"] = "Los resultados encontrados ya existían para esta productora y se descartaron como duplicados."
        if missing_leads:
            deficit_reason = str(trace.get("no_prospect_reason") or "No hubo más empresas que cumplieran todos los criterios con evidencia suficiente.")
            trace["research_summary"] = (
                f"Objetivo: {target_leads}. Encontrados: {discovered_count}. "
                f"Válidos nuevos: {len(prospects)}. Duplicados/excluidos: {duplicates_discarded}. "
                f"Faltantes: {missing_leads}. Motivo: {deficit_reason} "
                "Puedes ampliar o ajustar los criterios únicamente mediante una acción explícita en el portal."
            )
        total_to_save = len(prospects)
        for index, prospect in enumerate(prospects, start=1):
            prospect_execution_id = f"{execution_id}-{index:03d}"
            store.append_prospect(
                {
                    **prospect,
                    "execution_id": prospect_execution_id,
                    "email": source.email,
                    "onboarding_id": source.record_id,
                    "productora": source.company,
                }
            )
            report(
                phase="saving",
                progress=65 + round((index / max(total_to_save, 1)) * 25),
                message=f"Lead {index} de {total_to_save} guardado en Leads y CRM.",
                leads_found=index,
                latest_lead={
                    "company": str(prospect.get("company") or "Empresa verificada")[:120],
                    "execution_id": prospect_execution_id,
                },
            )
        report(
            phase="finalizing",
            progress=95,
            message="Registrando la ejecución y actualizando el panel…",
            leads_found=total_to_save,
        )
        store.append_execution(
            execution_id=execution_id,
            email=source.email,
            company=source.company,
            website=source.website,
            status="Completado" if prospects else "Completado sin prospectos",
            model=settings.openai_model,
            prompt_tokens=trace["prompt_tokens"],
            output_tokens=trace["output_tokens"],
            total_tokens=trace["total_tokens"],
            onboarding_id=source.record_id,
            productora=source.company,
            web_search_calls=trace["web_search_calls"],
            web_search_call_limit=trace["web_search_call_limit"],
            search_queries=trace["search_queries"],
            research_sources=trace["research_sources"],
            no_prospect_reason=trace["no_prospect_reason"],
            research_summary=trace["research_summary"],
            search_configuration=trace["search_configuration"],
            adjustments=trace["adjustments"],
            research_provider=trace["research_provider"],
            search_trace=trace["search_trace"],
            duplicates_discarded=duplicates_discarded,
            actor_email=actor_email or source.email,
            actor_role=actor_role,
            execution_origin=execution_origin,
        )
        try:
            store.refresh_dashboard_summary()
        except Exception:
            pass
        report(
            phase="completed",
            progress=100,
            message=(
                f"Investigación completada: objetivo 5 · encontrados {discovered_count} · válidos {len(prospects)} · duplicados/excluidos {duplicates_discarded} · faltantes {missing_leads}."
                if prospects
                else "Investigación completada sin nuevos leads verificables."
            ),
            leads_found=len(prospects),
            execution_id=execution_id,
        )
        return {"ok": True, "execution_id": execution_id, "prospects": prospects, "trace": trace}
    except Exception as exc:
        if reserved and settings.refund_failed_searches:
            try:
                store.refund_execution(source.email)
            except Exception:
                pass
        try:
            store.append_execution(
                execution_id=execution_id,
                email=source.email,
                company=source.company,
                website=source.website,
                status="Fallido",
                model=settings.openai_model,
                prompt_tokens=0,
                output_tokens=0,
                total_tokens=0,
                error=str(exc)[:500],
                onboarding_id=source.record_id,
                productora=source.company,
                web_search_call_limit=settings.web_search_call_limit,
                actor_email=actor_email or source.email,
                actor_role=actor_role,
                execution_origin=execution_origin,
            )
        except Exception:
            pass
        raise


def _execute_research_job(
    job_id: str,
    source,
    adjustments: dict,
    actor_email: str,
    actor_role: str,
    bypass_user_limit: bool,
) -> None:
    def on_progress(update: dict) -> None:
        _update_research_job(job_id, **update)

    try:
        result = _run_onboarding_research(
            source,
            SheetStore(get_settings()),
            adjustments,
            actor_email=actor_email,
            actor_role=actor_role,
            execution_origin="manual",
            bypass_user_limit=bypass_user_limit,
            progress_callback=on_progress,
        )
        _update_research_job(
            job_id,
            status="completed",
            phase="completed",
            progress=100,
            leads_found=len(result["prospects"]),
            execution_id=result["execution_id"],
        )
    except Exception as exc:
        _update_research_job(
            job_id,
            status="failed",
            phase="failed",
            progress=100,
            message=_safe_research_failure(exc),
        )
    finally:
        key = (source.record_id, actor_email.strip().lower())
        with _RESEARCH_JOBS_LOCK:
            if _ACTIVE_RESEARCH_JOBS.get(key) == job_id:
                _ACTIVE_RESEARCH_JOBS.pop(key, None)
            _cleanup_research_jobs_locked()


def _sync_automations_once() -> None:
    settings = get_settings()
    if not settings.auto_research_enabled or not settings.google_sheets_enabled:
        return
    store = SheetStore(settings)
    store.ensure_operational_schema()
    for config in store.due_automation_configs():
        source = store.get_onboarding_source(config["onboarding_id"], config["email"])
        if not source:
            store.mark_automation_run(config, "Bloqueada: no se encontró el Onboarding")
            continue
        if not source.ready:
            store.mark_automation_run(config, f"Bloqueada: {'; '.join(source.blockers)}")
            continue
        if not settings.openai_api_key:
            store.mark_automation_run(config, "Pendiente: falta configurar OpenAI")
            continue
        try:
            creator_email = str(config.get("created_by_email") or config.get("email") or "").strip().lower()
            creator_access = store.get_access(creator_email) if creator_email else None
            creator_is_admin = bool(
                creator_access
                and "admin" in creator_access.role.lower()
                and creator_email in (settings.admin_emails or {"servicemanagerbossio@gmail.com"})
            )
            result = _run_onboarding_research(
                source,
                store,
                config.get("adjustments") or None,
                actor_email=creator_email or source.email,
                actor_role=creator_access.role if creator_access else "Cliente",
                execution_origin="automation",
                bypass_user_limit=creator_is_admin,
            )
            store.mark_automation_run(
                config,
                f"Completada: {len(result['prospects'])} prospectos",
                result["execution_id"],
            )
        except Exception as exc:
            store.mark_automation_run(config, f"Falló: {str(exc)[:240]}")


def _process_onboarding_trigger(record_id: str, store: SheetStore) -> dict:
    """Prepare a connected search draft without running any provider."""
    store.ensure_operational_schema()
    source = store.get_onboarding_source(record_id)
    if not source:
        raise LookupError("El registro de Onboarding todavía no está disponible en Google Sheets")
    automation = store.get_automation_config(source.record_id)
    if automation is None:
        automation = store.upsert_automation_config(
            source.record_id,
            source.email,
            enabled=False,
            interval_minutes=1440,
            adjustments=source.recommended_adjustments(),
        )
    return {
        "ok": True,
        "state": "prepared" if source.ready else "prepared_with_missing_fields",
        "onboarding_id": source.record_id,
        "ready": source.ready,
        "blockers": source.blockers,
        "prompt_preview": source.prompt_preview(),
        "profile": source.prospecting_profile(),
        "viral_radar_profile": source.viral_radar_profile(),
        "automation": automation,
        "external_search_started": False,
        "credits_consumed": False,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    if settings.app_env != "production":
        create_schema()
        with session_scope() as session:
            ensure_demo_client(session)
    automation_task = None
    keepalive_task = None
    if settings.google_sheets_enabled and settings.auto_research_enabled:
        async def automation_loop():
            while True:
                await asyncio.to_thread(_sync_automations_once)
                await asyncio.sleep(max(60, settings.auto_research_poll_seconds))

        automation_task = asyncio.create_task(automation_loop())
    if render_keepalive_ready(settings):
        keepalive_task = asyncio.create_task(render_keepalive_loop(settings))
    try:
        yield
    finally:
        tasks = [task for task in (automation_task, keepalive_task) if task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Focus Prospeccion", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _template(request: Request, name: str, **context):
    response = templates.TemplateResponse(request=request, name=name, context=context)
    if not request.cookies.get(CSRF_COOKIE):
        response.set_cookie(CSRF_COOKIE, new_csrf_token(), httponly=False, secure=get_settings().app_env == "production", samesite="lax")
    return response


def _demo_data_allowed(settings: Settings) -> bool:
    """Allow fixtures only in an explicitly local, non-production setup."""
    sheets_ready = bool(
        settings.google_sheets_enabled
        and settings.google_sheet_id
        and settings.google_service_account_json
    )
    if sheets_ready:
        return False
    if settings.app_env == "production" or settings.google_sheets_enabled:
        raise HTTPException(
            status_code=503,
            detail="La fuente real de productoras no está disponible; no se mostrará información de demostración.",
        )
    return True


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from app.auth import verify_central_session

    settings = get_settings()
    if await verify_central_session(request.cookies.get(SESSION_COOKIE), settings):
        return RedirectResponse("/portal", status_code=303)
    if settings.central_auth_enabled:
        destination = f"{settings.public_base_url.rstrip('/')}/portal"
        return RedirectResponse(f"{settings.central_auth_url.rstrip('/')}/access?return_to={quote(destination, safe='')}", status_code=303)
    return _template(
        request,
        "login.html",
        google_oauth_client_id=settings.google_oauth_client_id,
        demo_enabled=settings.demo_auth_bypass and settings.app_env != "production",
    )


@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request):
    from app.auth import verify_central_session

    settings = get_settings()
    identity = await verify_central_session(request.cookies.get(SESSION_COOKIE), settings)
    if not identity:
        destination = f"{settings.public_base_url.rstrip('/')}/portal"
        return RedirectResponse(f"{settings.central_auth_url.rstrip('/')}/access?return_to={quote(destination, safe='')}", status_code=303)
    if not identity.prospection_access:
        return HTMLResponse(
            "<main><h1>Acceso no habilitado</h1><p>La administración puede activar Prospección desde la hoja de accesos.</p></main>",
            status_code=403,
        )
    return _template(
        request,
        "portal.html",
        identity=identity,
        radar_portal_url=settings.radar_portal_url if identity.radar_access else "",
    )


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "sheets_configured": bool(settings.google_sheets_enabled and settings.google_service_account_json),
        "google_login_configured": bool(settings.google_oauth_client_id),
        "central_auth_configured": bool(settings.central_auth_enabled and settings.central_auth_url),
        "openai_configured": bool(settings.openai_api_key),
        "openai_web_search_call_limit": settings.web_search_call_limit,
        "auto_research_enabled": settings.auto_research_enabled,
        "render_keepalive_enabled": render_keepalive_ready(settings),
    }


@app.post("/api/internal/onboarding-trigger")
def onboarding_trigger(payload: OnboardingTrigger, request: Request):
    settings = get_settings()
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {settings.prospection_trigger_token}" if settings.prospection_trigger_token else ""
    if not expected:
        raise HTTPException(status_code=503, detail="El disparador automático no está configurado")
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Disparador no autorizado")
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Google Sheets no está activado")
    try:
        return _process_onboarding_trigger(payload.onboarding_id.strip(), SheetStore(settings))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo procesar el registro: {str(exc)[:300]}") from exc


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
    settings = get_settings()
    if settings.central_auth_enabled:
        return JSONResponse({
            "ok": True,
            "redirect": f"{settings.central_auth_url.rstrip('/')}/logout?return_to={quote(settings.public_base_url, safe='')}",
        })
    response = JSONResponse({"ok": True, "redirect": "/"})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/portal-dashboard")
def portal_dashboard(
    view_as: str = Query(default="", max_length=320),
    identity: Identity = Depends(require_identity),
):
    settings = get_settings()
    if _demo_data_allowed(settings):
        return _demo_payload(identity, settings.openai_request_budget)
    store = SheetStore()
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    store.ensure_operational_schema()
    is_admin = _is_authorized_admin(identity, access, settings)
    requested_scope = view_as.strip().lower()
    if requested_scope and not is_admin:
        raise HTTPException(status_code=403, detail="Solo la administración puede usar la vista como cliente")
    all_sources = store.onboarding_sources(None) if is_admin else []
    scoped_access = store.get_access(requested_scope) if requested_scope else None
    scoped_sources = [source for source in all_sources if source.email == requested_scope] if requested_scope else []
    if requested_scope and not scoped_access and not scoped_sources:
        raise HTTPException(status_code=404, detail="El cliente solicitado no está registrado")
    scope_email = requested_scope or (None if is_admin else identity.email)
    visible_access = scoped_access or access
    source_records = scoped_sources if requested_scope and scoped_sources else store.onboarding_sources(scope_email)
    sources = [
        _source_view(
            source,
            settings,
            store.latest_execution_for_onboarding(source.record_id),
            store.get_automation_config(source.record_id),
        )
        for source in source_records
    ]
    review_events = store.review_events(scope_email)
    prospects = decorate_prospects(
        store.recent_prospects(scope_email),
        review_events,
        require_admin_review=settings.lead_admin_review_required,
    )
    summary_events = [event for event in review_events if event.get("event_type") == "summary_request"]
    for source in sources:
        latest_request = max(
            (event for event in summary_events if event.get("onboarding_id") == source["onboarding_id"]),
            key=lambda event: str(event.get("created_at") or ""),
            default=None,
        )
        source["lead_summary_request"] = latest_request or {
            "decision": "No solicitada",
            "created_at": "",
            "actor_email": "",
            "result_status": "No iniciado",
            "result_ref": "",
        }
    global_metrics = store.global_metrics() if is_admin and not requested_scope else None
    return {
        "user": {
            "email": requested_scope or visible_access.email,
            "role": scoped_access.role if scoped_access else ("Cliente registrado" if requested_scope else visible_access.role),
            "assigned": scoped_access.assigned if scoped_access else (0 if requested_scope else visible_access.assigned),
            "used": scoped_access.used if scoped_access else (0 if requested_scope else visible_access.used),
            "available": scoped_access.available if scoped_access else (0 if requested_scope else visible_access.available),
        },
        "global": global_metrics,
        "metrics": store.prospect_metrics(scope_email),
        "prospects": prospects,
        "executions": store.recent_executions(scope_email, hide_admin=not is_admin),
        "sources": sources,
        "source_metrics": {
            "total": len(sources),
            "ready": sum(1 for source in sources if source["ready"]),
            "blocked": sum(1 for source in sources if not source["ready"]),
        },
        "admin_context": {
            "is_admin": is_admin,
            "authenticated_email": identity.email,
            "viewing_as": requested_scope,
            "available_users": _admin_available_users(store, all_sources) if is_admin else [],
        },
        "automation_engine_enabled": settings.auto_research_enabled,
        "approval_policy": {"admin_review_required": settings.lead_admin_review_required},
        "demo": False,
    }


@app.get("/api/onboarding-sources/{record_id}")
def onboarding_source(record_id: str, identity: Identity = Depends(require_identity)):
    settings = get_settings()
    if _demo_data_allowed(settings):
        source = next(
            (item for item in _demo_payload(identity, settings.openai_request_budget)["sources"] if item["onboarding_id"] == record_id),
            None,
        )
        if not source:
            raise HTTPException(status_code=404, detail="No se encontró la productora")
        return {"source": source}
    store = SheetStore()
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    source = store.get_onboarding_source(record_id, None if is_admin else identity.email)
    if not source:
        raise HTTPException(status_code=404, detail="No se encontró la productora")
    return {
        "source": _source_view(
            source,
            settings,
            store.latest_execution_for_onboarding(source.record_id),
            store.get_automation_config(source.record_id),
        )
    }


@app.post("/api/onboarding-sources/{record_id}/research-jobs", status_code=202)
def start_research_job(
    record_id: str,
    payload: ResearchAdjustments,
    request: Request,
    background_tasks: BackgroundTasks,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Google Sheets no está activado")
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=503,
            detail="Investigación pendiente: configura OPENAI_API_KEY como secreto del servidor.",
        )
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    source = store.get_onboarding_source(record_id, None if is_admin else identity.email)
    if not source:
        raise HTTPException(status_code=404, detail="No se encontró la productora")
    if not source.ready:
        raise HTTPException(status_code=422, detail="; ".join(source.blockers))

    actor_email = identity.email.strip().lower()
    active_key = (source.record_id, actor_email)
    with _RESEARCH_JOBS_LOCK:
        active_id = _ACTIVE_RESEARCH_JOBS.get(active_key)
        active_job = _RESEARCH_JOBS.get(active_id) if active_id else None
        if active_job and active_job.get("status") in {"queued", "running"}:
            return JSONResponse(_public_research_job(active_job), status_code=200)
        job_id = str(uuid.uuid4())
        now = _utc_now()
        job = {
            "job_id": job_id,
            "source_id": source.record_id,
            "owner_email": actor_email,
            "status": "queued",
            "phase": "queued",
            "progress": 0,
            "message": "Investigación en cola. Preparando la búsqueda real…",
            "leads_found": 0,
            "saved_leads": [],
            "execution_id": "",
            "created_at": now,
            "updated_at": now,
            "_updated_epoch": time.time(),
        }
        _RESEARCH_JOBS[job_id] = job
        _ACTIVE_RESEARCH_JOBS[active_key] = job_id
        _cleanup_research_jobs_locked()

    _update_research_job(
        job_id,
        status="running",
        phase="preparing",
        progress=2,
        message="Preparando la investigación real…",
    )
    background_tasks.add_task(
        _execute_research_job,
        job_id,
        source,
        payload.model_dump(),
        actor_email,
        access.role,
        is_admin,
    )
    return _public_research_job(_RESEARCH_JOBS[job_id])


@app.get("/api/research-jobs/{job_id}")
def research_job_status(job_id: str, identity: Identity = Depends(require_identity)):
    settings = get_settings()
    with _RESEARCH_JOBS_LOCK:
        job = dict(_RESEARCH_JOBS.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="No se encontró la investigación")
    store = SheetStore(settings)
    access = store.get_access(identity.email) if settings.google_sheets_enabled else None
    is_admin = bool(access and _is_authorized_admin(identity, access, settings))
    if identity.email.strip().lower() != job.get("owner_email") and not is_admin:
        raise HTTPException(status_code=404, detail="No se encontró la investigación")
    return _public_research_job(job)


@app.post("/api/onboarding-sources/{record_id}/research")
def research_onboarding_source(
    record_id: str,
    payload: ResearchAdjustments,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Google Sheets no está activado")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    source = store.get_onboarding_source(record_id, None if is_admin else identity.email)
    if not source:
        raise HTTPException(status_code=404, detail="No se encontró la productora")
    try:
        return _run_onboarding_research(
            source,
            store,
            payload.model_dump(),
            actor_email=identity.email,
            actor_role=access.role,
            execution_origin="manual",
            bypass_user_limit=is_admin,
        )
    except RuntimeError as exc:
        if str(exc) == "OPENAI_API_KEY_REQUIRED":
            raise HTTPException(
                status_code=503,
                detail="Investigación pendiente: configura OPENAI_API_KEY como secreto del servidor.",
            ) from exc
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo completar la investigación: {str(exc)[:300]}") from exc


@app.post("/api/onboarding-sources/{record_id}/automation")
def save_onboarding_automation(
    record_id: str,
    payload: AutomationRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="La automatización requiere Google Sheets")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    source = store.get_onboarding_source(record_id, None if is_admin else identity.email)
    if not source:
        raise HTTPException(status_code=404, detail="No se encontró la productora")
    if payload.enabled and not source.ready:
        raise HTTPException(status_code=422, detail="No se puede automatizar: " + "; ".join(source.blockers))
    store.ensure_operational_schema()
    schedule = store.upsert_automation_config(
        source.record_id,
        source.email,
        enabled=payload.enabled,
        interval_minutes=payload.interval_minutes,
        adjustments=payload.adjustments.model_dump(),
        name=payload.name,
        favorite=payload.favorite,
        created_by_email=identity.email,
        created_by_role=access.role,
    )
    return {"ok": True, "automation": schedule}


@app.post("/api/prospects/{execution_id}/status")
def update_prospect_status(
    execution_id: str,
    payload: LeadStatusRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    raise HTTPException(
        status_code=409,
        detail="Usa el flujo de decisión auditada para aprobar, descartar o enviar a revisión",
    )


@app.post("/api/prospects/{execution_id}/decision")
def record_prospect_decision(
    execution_id: str,
    payload: LeadDecisionRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Las decisiones no se guardan en el modo demo")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    allowed = ADMIN_DECISIONS if is_admin else CLIENT_DECISIONS
    if payload.decision not in allowed:
        raise HTTPException(status_code=422, detail="La decisión no corresponde al rol autenticado")
    prospect = store.get_prospect(execution_id, None if is_admin else identity.email)
    if not prospect:
        raise HTTPException(status_code=404, detail="No se encontró el lead solicitado")
    event_type = "admin_review" if is_admin else "client_decision"
    event = store.append_review_event(
        event_type=event_type,
        onboarding_id=str(prospect.get("onboarding_id") or ""),
        owner_email=str(prospect.get("email") or ""),
        execution_id=execution_id,
        actor_email=identity.email,
        actor_role=access.role,
        decision=payload.decision,
        reason=payload.reason,
        result_status="Registrada",
    )
    if not is_admin:
        prospect = store.update_prospect_status(
            execution_id,
            identity.email,
            payload.decision,
            is_admin=False,
        )
    events = store.review_events(str(prospect.get("email") or ""), execution_id=execution_id)
    decorated = decorate_prospects(
        [prospect],
        events,
        require_admin_review=settings.lead_admin_review_required,
    )[0]
    return {"ok": True, "event": event, "prospect": decorated, "external_action_started": False}


@app.get("/api/onboarding-sources/{record_id}/lead-summary-request-preview")
def lead_summary_request_preview(record_id: str, identity: Identity = Depends(require_identity)):
    settings = get_settings()
    if _demo_data_allowed(settings):
        demo = _demo_payload(identity, settings.openai_request_budget)
        source = next((item for item in demo["sources"] if item["onboarding_id"] == record_id), None)
        if not source:
            raise HTTPException(status_code=404, detail="No se encontró la productora")
        return {
            "preview": summary_request_preview(record_id, demo["prospects"]),
            "current_request": source["lead_summary_request"],
        }
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    source = store.get_onboarding_source(record_id, None if is_admin else identity.email)
    if not source:
        raise HTTPException(status_code=404, detail="No se encontró la productora")
    events = store.review_events(source.email, onboarding_id=record_id)
    prospects = decorate_prospects(
        [item for item in store.recent_prospects(source.email, limit=1000) if item.get("onboarding_id") == record_id],
        events,
        require_admin_review=settings.lead_admin_review_required,
    )
    latest = max(
        (event for event in events if event.get("event_type") == "summary_request"),
        key=lambda event: str(event.get("created_at") or ""),
        default=None,
    )
    return {
        "preview": summary_request_preview(record_id, prospects),
        "current_request": latest or {"decision": "No solicitada", "result_status": "No iniciado"},
    }


@app.post("/api/onboarding-sources/{record_id}/lead-summary-requests", status_code=202)
def request_lead_summary(
    record_id: str,
    payload: LeadSummaryRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="La solicitud no se guarda en el modo demo")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    if _is_authorized_admin(identity, access, settings):
        raise HTTPException(status_code=403, detail="La solicitud debe confirmarla el cliente de esa cuenta")
    source = store.get_onboarding_source(record_id, identity.email)
    if not source:
        raise HTTPException(status_code=404, detail="No se encontró la productora")
    events = store.review_events(identity.email, onboarding_id=record_id)
    prospects = decorate_prospects(
        [item for item in store.recent_prospects(identity.email, limit=1000) if item.get("onboarding_id") == record_id],
        events,
        require_admin_review=settings.lead_admin_review_required,
    )
    scope = summary_request_preview(record_id, prospects)
    event = store.append_review_event(
        event_type="summary_request",
        onboarding_id=record_id,
        owner_email=identity.email,
        actor_email=identity.email,
        actor_role=access.role,
        decision="Solicitada",
        reason=payload.note,
        scope=scope,
        result_status="Pendiente de preparación",
    )
    return {
        "ok": True,
        "request": event,
        "preview": scope,
        "summary_generated": False,
        "external_calls": False,
    }


@app.post("/api/prospects/{execution_id}/crm")
def update_prospect_crm(
    execution_id: str,
    payload: CRMRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Los cambios no se guardan en el modo demo")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    current = store.get_prospect(execution_id, None if is_admin else identity.email)
    if not current:
        raise HTTPException(status_code=404, detail="No se encontró el lead solicitado")
    if payload.status != str(current.get("lead_status") or "Nuevo"):
        raise HTTPException(
            status_code=409,
            detail="El estado comercial solo cambia mediante una decisión auditada",
        )
    try:
        prospect = store.update_prospect_crm(
            execution_id,
            identity.email,
            status=payload.status,
            owner=payload.owner[:160],
            notes=payload.notes[:4000],
            next_action=payload.next_action[:500],
            follow_up_date=payload.follow_up_date[:40],
            warmup_preparation=payload.warmup_preparation,
            warmup_approval=payload.warmup_approval,
            is_admin=is_admin,
        )
        store.append_review_event(
            event_type="crm_update",
            onboarding_id=str(prospect.get("onboarding_id") or ""),
            owner_email=str(prospect.get("email") or ""),
            execution_id=execution_id,
            actor_email=identity.email,
            actor_role=access.role,
            decision="Seguimiento CRM actualizado",
            reason=payload.notes,
            result_status="Registrada",
        )
        try:
            store.refresh_dashboard_summary()
        except Exception:
            pass
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "prospect": prospect}


@app.get("/api/prospects/{execution_id}/client-package.zip")
def download_client_package(execution_id: str, identity: Identity = Depends(require_identity)):
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="La descarga real requiere Google Sheets")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access:
        raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
    is_admin = _is_authorized_admin(identity, access, settings)
    prospect = store.get_prospect(execution_id, None if is_admin else identity.email)
    if not prospect:
        raise HTTPException(status_code=404, detail="No se encontro el lead solicitado")
    source = store.get_onboarding_source(prospect.get("onboarding_id", ""), None if is_admin else identity.email)
    if not source:
        raise HTTPException(status_code=404, detail="No se encontro el formulario asociado al lead")
    package = _build_client_package(source, prospect)
    filename = f"paquete-cliente-{_safe_package_name(source.company)}-{_safe_package_name(source.record_id)}.zip"
    return StreamingResponse(
        io.BytesIO(package),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/prospects/{execution_id}/close")
def close_prospect(execution_id: str, request: Request, identity: Identity = Depends(require_identity)):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Los cambios no se guardan en el modo demo")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access or not _is_authorized_admin(identity, access, settings):
        raise HTTPException(status_code=403, detail="Solo la administracion autorizada puede cerrar leads")
    try:
        prospect = store.update_prospect_status(execution_id, identity.email, "Cerrado", is_admin=True)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "prospect": prospect}


@app.delete("/api/prospects/{execution_id}")
def delete_prospect(
    execution_id: str,
    payload: DeleteLeadRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
):
    validate_csrf(request)
    settings = get_settings()
    if not settings.google_sheets_enabled:
        raise HTTPException(status_code=503, detail="Los cambios no se guardan en el modo demo")
    store = SheetStore(settings)
    access = store.get_access(identity.email)
    if not access or not _is_authorized_admin(identity, access, settings):
        raise HTTPException(status_code=403, detail="Solo la administracion autorizada puede eliminar leads")
    try:
        prospect = store.delete_prospect(execution_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "deleted": prospect["execution_id"]}


@app.get("/api/prospects/export.csv")
def export_prospects(
    q: str = "",
    classification: str = "",
    status: str = "",
    productora: str = "",
    country: str = "",
    sector: str = "",
    client_type: str = "",
    identity: Identity = Depends(require_identity),
):
    settings = get_settings()
    if _demo_data_allowed(settings):
        prospects = _demo_payload(identity, settings.openai_request_budget)["prospects"]
    else:
        store = SheetStore()
        access = store.get_access(identity.email)
        if not access:
            raise HTTPException(status_code=403, detail="Acceso retirado en Google Sheets")
        prospects = store.recent_prospects(None if "admin" in access.role.lower() else identity.email, limit=1000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    filters = {
        "classification": classification.strip().lower(), "lead_status": status.strip().lower(),
        "productora": productora.strip().lower(), "country": country.strip().lower(),
        "sector": sector.strip().lower(), "client_type": client_type.strip().lower(),
    }
    query = q.strip().lower()
    prospects = [
        item for item in prospects
        if (not query or query in " ".join(str(item.get(key, "")) for key in ("company", "website", "sector", "city", "summary")).lower())
        and all(not value or value == str(item.get(key, "")).strip().lower() for key, value in filters.items())
    ]
    writer.writerow([
        "Productora", "ID onboarding", "Empresa", "Web", "Sector", "Tipo de cliente", "Ciudad", "País",
        "Empleados", "Score", "Clasificación", "Estado CRM", "Propietario CRM", "Notas CRM", "Próxima acción",
        "Fecha seguimiento", "Preparación calentamiento", "Aprobación calentamiento", "Resumen", "Ángulo de entrada", "Contactos públicos", "Redes sociales",
        "Decisores públicos", "Señales financieras/comerciales", "Estado de señales", "Consultas", "Llamadas de búsqueda", "Límite",
        "Fuentes", "Motivo sin contactos", "Motivo sin prospecto", "Correo de cuenta", "Fecha", "ID lead",
    ])
    for item in prospects:
        writer.writerow([_csv_cell(value) for value in [
            item.get("productora", ""), item.get("onboarding_id", ""), item.get("company", ""), item.get("website", ""),
            item.get("sector", ""), item.get("client_type", ""), item.get("city", ""), item.get("country", ""),
            item.get("employees", ""), item.get("score", ""), item.get("classification", ""), item.get("lead_status", ""),
            item.get("crm_owner", ""), item.get("crm_notes", ""), item.get("crm_next_action", ""), item.get("crm_follow_up_date", ""),
            item.get("warmup_preparation", "No iniciada"), item.get("warmup_approval", "Pendiente"),
            item.get("summary", ""), item.get("entry_angle", ""), json.dumps(item.get("public_contacts") or [], ensure_ascii=False),
            json.dumps(item.get("social_links") or {}, ensure_ascii=False), json.dumps(item.get("decision_makers") or [], ensure_ascii=False), json.dumps(item.get("public_signals") or [], ensure_ascii=False),
            item.get("public_signals_status", "No encontrado públicamente"), " | ".join(item.get("search_queries") or []),
            item.get("web_search_calls", 0), item.get("web_search_call_limit", 5),
            " | ".join(source.get("url", "") for source in item.get("research_sources") or []),
            item.get("no_contacts_reason", ""), item.get("no_prospect_reason", ""), item.get("email", ""),
            item.get("created_at", ""), item.get("execution_id", ""),
        ]])
    content = "\ufeff" + buffer.getvalue()
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=focus-prospeccion-leads.csv"},
    )

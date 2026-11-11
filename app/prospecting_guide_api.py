"""Mounted by the private sandbox only; existing research routes are unchanged."""
from uuid import UUID
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import validate_csrf
from app.intelligence import CampaignError
from app.onboarding import _items, _public_website
from app.outreach_api import authorize
from app.outreach_content import fingerprint
from app.prospecting_guide import INTAKE_EDITABLE, uses_profile


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    fresh: bool = False


class Answer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    version: int = Field(ge=1, le=100)
    answer: str = Field(min_length=1, max_length=2000)
    mode: Literal['option', 'custom'] = 'custom'

class Revise(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    version: int = Field(ge=1, le=100)
    index: int = Field(ge=0, lt=20)
    answer: str = Field(min_length=1, max_length=2000)

class UpdateField(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str = Field(max_length=100)
    before: str = Field(max_length=30000)
    value: str = Field(max_length=4000)


class Reanalyse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    version: int = Field(ge=1, le=100)
    additional_context: str | None = Field(default=None, max_length=2000)
    choice: Literal['review', 'more'] | None = None


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, le=100)
    choice: Literal['review', 'more']


class Launch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, le=100)


class Schedule(Launch):
    interval_minutes: int = Field(ge=5, le=10080)
    max_total_runs: int = Field(ge=1, le=50)


class Apply(Launch):
    confirmed: bool


def profile_view(profile, intake):
    legacy = {key: value for key, value in profile.items() if key != 'business_context'}
    return {'profile': profile, 'intake': intake, 'profile_hash': fingerprint(profile),
        'legacy_profile_hash': fingerprint(legacy)}


def intake_value(field, value):
    if field not in INTAKE_EDITABLE:
        raise HTTPException(422, 'Este campo se gestiona fuera de la guía.')
    value = value.strip()
    limits = {'Ciudad objetivo': 200, 'Región objetivo': 200, 'Tamaño empresa ideal': 500,
        'Presupuesto mínimo': 200, 'Exclusiones de prospección': 1500, 'Preferencias de prospección': 2000}
    if len(value) > limits.get(field, 4000):
        raise HTTPException(422, 'Acorta el texto de este campo antes de guardarlo.')
    if field in {'Sectores', 'Países objetivo', 'Mercados', 'Tipos de cliente objetivo', 'Público'}:
        items = _items(value)
        if len(items) > 12 or any(len(item) > 500 for item in items):
            raise HTTPException(422, 'Escribe hasta 12 opciones separadas por comas.')
    if field == 'Empresa' and not value:
        raise HTTPException(422, 'Escribe el nombre de la empresa.')
    if field == 'Web' and value:
        url = _public_website(value)
        if not url or urlsplit(url).username is not None or urlsplit(url).password is not None:
            raise HTTPException(422, 'Introduce la dirección pública de la web, incluyendo https://.')
        value = url
    return value


def guide_router(service, identity_dependency, template, source_for, save_for, feedback_for=None,
    intake_for=None, update_intake_for=None, run_for=None, schedule_for=None, schedule_status=None, pause_schedule=None):
    router = APIRouter()

    def scope(tenant, identity):
        authorize(service.engine, tenant, identity)
        return source_for(tenant, identity)

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CampaignError as exc:
            raise HTTPException(409, str(exc)) from None
        except (RuntimeError, ValueError):
            raise HTTPException(503, 'No se confirmó la operación. Revisa el estado guardado antes de repetir.') from None

    @router.get('/prospecting-guide')
    def page(request: Request, identity=Depends(identity_dependency)):
        return template(request, 'prospecting-guide.html', identity=identity)

    @router.get('/api/intelligence/{tenant}/guide')
    def state(tenant: str, identity=Depends(identity_dependency)):
        if intake_for:
            authorize(service.engine, tenant, identity)
            profile, intake = intake_for(tenant, identity)
        else:
            profile, intake = scope(tenant, identity), []
        return {**profile_view(profile, intake), 'ai_ready': service.generator is not None,
            'session': run(service.latest, tenant, identity.email), 'research_started': False,
            'saved': run(service.saved, tenant, identity.email),
            'research_available': bool(run_for and service.engine.real), 'schedule_available': bool(schedule_for),
            'schedule': run(schedule_status, tenant, identity) if schedule_status else None,
            'mode': 'real_isolated' if service.engine.real else 'local_demo'}

    @router.patch('/api/intelligence/{tenant}/guide/intake')
    def update_intake(tenant: str, payload: UpdateField, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        authorize(service.engine, tenant, identity)
        if not update_intake_for:
            raise HTTPException(503, 'La ficha de esta cuenta no se puede editar desde la guía.')
        value = intake_value(payload.field, payload.value)
        profile, intake = run(update_intake_for, tenant, identity, payload.field, payload.before, value)
        return profile_view(profile, intake)

    @router.post('/api/intelligence/{tenant}/guide/sessions')
    def start(tenant: str, payload: Start, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        feedback = run(feedback_for, tenant, identity) if feedback_for else {}
        return run(service.start, tenant, str(payload.request_id), identity.email, profile, feedback, payload.fresh)

    @router.get('/api/intelligence/{tenant}/guide/sessions/{sid}')
    def get(tenant: str, sid: UUID, identity=Depends(identity_dependency)):
        scope(tenant, identity)
        return run(service.get, tenant, str(sid), identity.email)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/answers')
    def answer(tenant: str, sid: UUID, payload: Answer, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        return run(service.answer, tenant, str(sid), identity.email, str(payload.request_id), payload.version,
            payload.answer, payload.mode, profile)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/revisions')
    def revise(tenant: str, sid: UUID, payload: Revise, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        return run(service.revise, tenant, str(sid), identity.email, str(payload.request_id),
            payload.version, payload.index, payload.answer, profile)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/reanalysis')
    def reanalyse(tenant: str, sid: UUID, payload: Reanalyse, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        feedback = run(feedback_for, tenant, identity) if feedback_for else None
        return run(service.revise, tenant, str(sid), identity.email, str(payload.request_id), payload.version,
            None, '', profile, feedback=feedback, additional_context=payload.additional_context, choice=payload.choice)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/checkpoint')
    def checkpoint(tenant: str, sid: UUID, payload: Checkpoint, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        scope(tenant, identity)
        return run(service.checkpoint, tenant, str(sid), identity.email, payload.version, payload.choice)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/apply')
    def apply(tenant: str, sid: UUID, payload: Apply, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        if payload.confirmed is not True:
            raise HTTPException(422, 'Confirma los criterios antes de guardarlos.')
        return run(service.apply, tenant, str(sid), identity.email, payload.version, profile,
            lambda name, criteria: save_for(tenant, identity, name, criteria))

    def reviewed(tenant, sid, identity, version):
        profile = scope(tenant, identity)
        session = run(service.get, tenant, str(sid), identity.email)
        if session['version'] != version or session['state'] not in {'ready', 'applied'} or not uses_profile(session, profile):
            raise HTTPException(409, 'Actualiza y revisa la propuesta antes de usarla.')
        if not all(any(item.strip() for item in session['criteria'][field]) for field in ('sectors', 'target_countries')):
            raise HTTPException(422, 'Completa el sector y el país antes de investigar.')
        return profile['onboarding_id'], session['criteria']

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/launch', status_code=202)
    def launch(tenant: str, sid: UUID, payload: Launch, request: Request, background_tasks: BackgroundTasks,
        identity=Depends(identity_dependency)):
        validate_csrf(request)
        record_id, criteria = reviewed(tenant, sid, identity, payload.version)
        if not run_for or not service.engine.real:
            raise HTTPException(503, 'El raspado real no está disponible en este entorno.')
        return run_for(record_id, criteria, request, background_tasks, identity)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/schedule')
    def schedule(tenant: str, sid: UUID, payload: Schedule, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        record_id, criteria = reviewed(tenant, sid, identity, payload.version)
        if not schedule_for:
            raise HTTPException(503, 'La programación no está disponible en este entorno.')
        return run(schedule_for, record_id, str(sid), criteria, payload.interval_minutes, payload.max_total_runs, request, identity)

    @router.get('/api/intelligence/{tenant}/guide/schedule')
    def current_schedule(tenant: str, identity=Depends(identity_dependency)):
        scope(tenant, identity)
        return run(schedule_status, tenant, identity) if schedule_status else None

    @router.delete('/api/intelligence/{tenant}/guide/schedule')
    def pause(tenant: str, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        scope(tenant, identity)
        if not pause_schedule:
            raise HTTPException(503, 'La programación no está disponible en este entorno.')
        return run(pause_schedule, tenant, identity)

    return router

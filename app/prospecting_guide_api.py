"""Authenticated API for the guided prospecting workflow."""
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import validate_csrf
from app.prospecting_guide import GuideError, fingerprint, uses_profile


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


class Apply(Launch):
    confirmed: bool


def profile_view(profile, intake):
    legacy = {key: value for key, value in profile.items() if key != 'business_context'}
    return {'profile': profile, 'intake': intake, 'profile_hash': fingerprint(profile),
        'legacy_profile_hash': fingerprint(legacy)}


def guide_router(get_service, identity_dependency, scope, save_for, feedback_for, intake_for, run_for, job_for):
    router = APIRouter()

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GuideError as exc:
            raise HTTPException(409, str(exc)) from None
        except (RuntimeError, ValueError):
            raise HTTPException(503, 'No se confirmó la operación. Revisa el estado guardado antes de repetir.') from None

    @router.get('/api/intelligence/{tenant}/guide')
    def state(tenant: str, identity=Depends(identity_dependency)):
        profile, intake = intake_for(tenant, identity)
        service = run(get_service)
        return {**profile_view(profile, intake), 'ai_ready': service.generator is not None,
            'session': run(service.latest, tenant, identity.email),
            'saved': run(service.saved, tenant, identity.email),
            'research_available': service.generator is not None,
            'schedule_available': False, 'job': job_for(tenant, identity),
            'mode': 'production'}

    @router.get('/api/intelligence/{tenant}/guide/job')
    def current_job(tenant: str, identity=Depends(identity_dependency)):
        return run(job_for, tenant, identity)

    @router.post('/api/intelligence/{tenant}/guide/sessions')
    def start(tenant: str, payload: Start, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        feedback = run(feedback_for, tenant, identity) if feedback_for else {}
        return run(run(get_service).start, tenant, str(payload.request_id), identity.email, profile, feedback, payload.fresh)

    @router.get('/api/intelligence/{tenant}/guide/sessions/{sid}')
    def get(tenant: str, sid: UUID, identity=Depends(identity_dependency)):
        scope(tenant, identity)
        return run(run(get_service).get, tenant, str(sid), identity.email)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/answers')
    def answer(tenant: str, sid: UUID, payload: Answer, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        return run(run(get_service).answer, tenant, str(sid), identity.email, str(payload.request_id), payload.version,
            payload.answer, payload.mode, profile)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/revisions')
    def revise(tenant: str, sid: UUID, payload: Revise, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        return run(run(get_service).revise, tenant, str(sid), identity.email, str(payload.request_id),
            payload.version, payload.index, payload.answer, profile)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/reanalysis')
    def reanalyse(tenant: str, sid: UUID, payload: Reanalyse, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        feedback = run(feedback_for, tenant, identity) if feedback_for else None
        return run(run(get_service).revise, tenant, str(sid), identity.email, str(payload.request_id), payload.version,
            None, '', profile, feedback=feedback, additional_context=payload.additional_context, choice=payload.choice)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/checkpoint')
    def checkpoint(tenant: str, sid: UUID, payload: Checkpoint, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        scope(tenant, identity)
        return run(run(get_service).checkpoint, tenant, str(sid), identity.email, payload.version, payload.choice)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/apply')
    def apply(tenant: str, sid: UUID, payload: Apply, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        if payload.confirmed is not True:
            raise HTTPException(422, 'Confirma los criterios antes de guardarlos.')
        return run(run(get_service).apply, tenant, str(sid), identity.email, payload.version, profile,
            lambda name, criteria: save_for(tenant, identity, name, criteria))

    def reviewed(tenant, sid, identity, version):
        profile = scope(tenant, identity)
        session = run(run(get_service).get, tenant, str(sid), identity.email)
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
        return run_for(record_id, criteria, request, background_tasks, identity)

    return router

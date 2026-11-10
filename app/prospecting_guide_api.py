"""Mounted by the private sandbox only; existing research routes are unchanged."""
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import validate_csrf
from app.intelligence import CampaignError
from app.outreach_api import authorize


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID


class Answer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    version: int = Field(ge=1, le=100)
    answer: str = Field(min_length=1, max_length=2000)
    mode: Literal['option', 'custom'] = 'custom'


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, le=100)
    choice: Literal['review', 'more']


class Apply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1, le=100)
    confirmed: bool


def guide_router(service, identity_dependency, template, source_for, save_for, feedback_for=None, intake_for=None):
    router = APIRouter()

    def scope(tenant, identity):
        authorize(service.engine, tenant, identity)
        return source_for(tenant, identity)

    def run(fn, *args):
        try:
            return fn(*args)
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
        return {'profile': profile, 'intake': intake, 'ai_ready': service.generator is not None,
            'session': run(service.latest, tenant, identity.email), 'research_started': False,
            'mode': 'real_isolated' if service.engine.real else 'local_demo'}

    @router.post('/api/intelligence/{tenant}/guide/sessions')
    def start(tenant: str, payload: Start, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        profile = scope(tenant, identity)
        feedback = run(feedback_for, tenant, identity) if feedback_for else {}
        return run(service.start, tenant, str(payload.request_id), identity.email, profile, feedback)

    @router.get('/api/intelligence/{tenant}/guide/sessions/{sid}')
    def get(tenant: str, sid: UUID, identity=Depends(identity_dependency)):
        scope(tenant, identity)
        return run(service.get, tenant, str(sid), identity.email)

    @router.post('/api/intelligence/{tenant}/guide/sessions/{sid}/answers')
    def answer(tenant: str, sid: UUID, payload: Answer, request: Request, identity=Depends(identity_dependency)):
        validate_csrf(request)
        scope(tenant, identity)
        return run(service.answer, tenant, str(sid), identity.email, str(payload.request_id), payload.version, payload.answer, payload.mode)

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

    return router

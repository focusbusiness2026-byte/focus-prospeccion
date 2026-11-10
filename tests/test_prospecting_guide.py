"""Adaptive conversation acceptance with fake providers; no network or API charges."""
import copy
import json
import threading
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.auth import Identity
from app.intelligence import CampaignEngine, CampaignError
from app.main import _questionnaire_source_profile
from app.onboarding import OnboardingSource
from app.outreach import OutreachService
from app.outreach_api import outreach_router
from app.outreach_setup import mount_outreach
from app.prospecting_guide import OpenAITargetingGuide, TargetingGuide, base_criteria, guide_intake, review_feedback
from app.prospecting_guide_api import guide_router

SEED = Path(__file__).parents[1] / 'app/intelligence_seed.json'
T = 'three-dots'
ACTOR = 'owner@example.test'


def profile():
    return _questionnaire_source_profile(OnboardingSource.from_sheet_record({
        'ID registro': 'ONB-TEST', 'Empresa': 'Estudio de prueba', 'Web': 'https://studio.example.com',
        'Email responsable': ACTOR, 'Servicio prioritario': 'Podcast', 'Servicios': 'Podcast, producción',
        'Sectores': 'Tecnología', 'Países objetivo': 'España', 'Público': 'Empresas privadas',
        'Perfil ideal detallado': 'Empresas con una oferta B2B', 'Exclusiones de prospección': 'Competidores',
        'Autorización': 'Sí', 'API key': 'must-not-leave-onboarding'}))


class FakeReasoner:
    def __init__(self): self.calls = []
    def __call__(self, context):
        self.calls.append(copy.deepcopy(context))
        history = context['history']
        criteria = copy.deepcopy(context['criteria'])
        if history:
            criteria['preferences'] = history[-1]['answer']
        question = '¿Qué oferta de podcast quieres priorizar?' if not history else '¿Qué tipo de equipo necesita ese formato?'
        return {'turn': {'summary': 'Propuesta basada en lo declarado, pendiente de revisión.',
            'question': question if len(history) < 20 else '',
            'options': ['Podcast corporativo', 'Contenido para redes', 'Vídeo corporativo'] if len(history) < 20 else [],
            'criteria': criteria, 'assumptions': ['Confirmar tamaño de empresa.']},
            'sources': [], 'web_status': 'unavailable', 'usage': {'input_tokens': 10, 'output_tokens': 20}, 'model': 'mock'}


@pytest.fixture
def setup(tmp_path):
    engine = CampaignEngine(tmp_path/'guide.db', SEED)
    reasoner = FakeReasoner()
    return engine, TargetingGuide(engine, reasoner), reasoner


def start(service):
    return service.start(T, str(uuid4()), ACTOR, profile())


def answer(service, session, text='Podcast dirigido a empresas de tecnología'):
    return service.answer(T, session['id'], ACTOR, str(uuid4()), session['version'], text)


def reach_checkpoint(service, session):
    while session['state'] == 'draft':
        session = answer(service, session)
    return session


def review_first_round(service):
    session = reach_checkpoint(service, start(service))
    return service.checkpoint(T, session['id'], ACTOR, session['version'], 'review')


def test_adaptive_context_includes_each_answer_and_preserves_business_tables(setup):
    engine, service, reasoner = setup
    with engine.db() as db:
        old = {t: db.execute('SELECT * FROM '+t).fetchall() for t in ('tenants','contacts','campaigns','outbox','jobs')}
    session = start(service)
    assert 'must-not-leave' not in json.dumps(reasoner.calls)
    session = answer(service, session, 'Series de entrevistas para software B2B')
    assert reasoner.calls[-1]['history'][-1]['answer'] == 'Series de entrevistas para software B2B'
    assert reasoner.calls[-1]['profile']['targeting']['main_service'] == 'Podcast'
    session = answer(service, session, 'Equipos de marketing de pymes')
    assert session['state'] == 'draft' and len(reasoner.calls) == 3
    assert session['criteria']['preferences'] == 'Equipos de marketing de pymes'
    assert session['criteria']['exclusions'] == 'Competidores'
    with engine.db() as db:
        assert all(db.execute('SELECT * FROM '+t).fetchall() == rows for t, rows in old.items())


def test_request_idempotency_and_optimistic_version_stop_duplicate_charges(setup):
    _, service, reasoner = setup
    sid = str(uuid4())
    session = service.start(T, sid, ACTOR, profile())
    assert service.start(T, sid, ACTOR, profile()) == session
    rid = str(uuid4())
    result = service.answer(T, sid, ACTOR, rid, 1, 'Quiero podcast')
    answer(service, result)
    assert service.answer(T, sid, ACTOR, rid, 1, 'Quiero podcast') == result
    with pytest.raises(CampaignError): service.answer(T, sid, ACTOR, rid, 1, 'Otra respuesta')
    with pytest.raises(CampaignError): service.answer(T, sid, ACTOR, str(uuid4()), 1, 'Respuesta desfasada')
    assert len(reasoner.calls) == 3


def test_options_and_custom_answers_survive_reload(setup):
    _, service, reasoner = setup
    session = start(service)
    sid, version = session['id'], session['version']
    with pytest.raises(CampaignError, match='Selecciona una opción'):
        service.answer(T, sid, ACTOR, str(uuid4()), version, 'No es una opción', 'option')
    assert len(reasoner.calls) == 1
    option = session['turn']['options'][0]
    session = service.answer(T, sid, ACTOR, str(uuid4()), version, option, 'option')
    session = answer(service, session, 'Queremos organizaciones culturales')
    history = service.get(T, sid, ACTOR)['history']
    assert [(h['answer'], h['mode']) for h in history] == [
        (option, 'option'), ('Queremos organizaciones culturales', 'custom')]
    assert session['analysis_summary'] and reasoner.calls[-1]['analysis_summary'] == session['analysis_summary']


def test_round_checkpoints_allow_review_or_more_with_a_finite_limit(setup):
    _, service, reasoner = setup
    session = reach_checkpoint(service, start(service))
    assert session['state'] == 'checkpoint' and len(session['history']) == 10
    sid, version = session['id'], session['version']
    with pytest.raises(CampaignError):
        answer(service, session)
    next_question = session['turn']['question']
    review = service.checkpoint(T, sid, ACTOR, version, 'review')
    assert review['state'] == 'ready' and len(reasoner.calls) == 11
    with pytest.raises(CampaignError):
        service.checkpoint(T, sid, ACTOR, version, 'more')
    session = service.checkpoint(T, sid, ACTOR, review['version'], 'more')
    assert session['turn']['question'] == next_question and len(reasoner.calls) == 11
    for limit in (15, 20):
        session = reach_checkpoint(service, session)
        assert session['state'] == 'checkpoint' and len(session['history']) == limit
        if limit < 20:
            session = service.checkpoint(T, sid, ACTOR, session['version'], 'more')
    with pytest.raises(CampaignError, match='máximo'):
        service.checkpoint(T, sid, ACTOR, session['version'], 'more')
    assert len(reasoner.calls) == 21 and reasoner.calls[-1]['question_number'] is None


def test_feedback_is_account_scoped_and_current_and_carries_no_lead_identity(setup):
    _, service, reasoner = setup
    lead = {'onboarding_id':'ONB-TEST', 'email':ACTOR.upper(), 'lead_status':'Descartado',
        'execution_id':'private-id', 'company':'private-company', 'crm_notes':'private-notes',
        'country':'España', 'sector':'Turismo', 'client_type':'Pymes'}
    feedback = review_feedback([
        dict(lead, lead_status='Nuevo'),
        lead,  # Old discarded snapshot for the reopened lead.
        dict(lead, execution_id='discarded-now'),
        dict(lead, execution_id='approved-now', lead_status='Aprobado para descarga', sector='Tecnología'),
        dict(lead, execution_id='different-record', onboarding_id='ONB-OTHER'),
        dict(lead, execution_id='different-owner', email='other@example.test'),
        dict(lead, execution_id='empty-result', prospect_found=False),
    ], 'ONB-TEST', ACTOR)
    session = service.start(T, str(uuid4()), ACTOR, profile(), feedback)
    assert session['feedback']['discarded']['count'] == session['feedback']['approved']['count'] == 1
    assert reasoner.calls[-1]['crm_feedback']['approved']['sector'] == [{'label':'Tecnología', 'count':1}]
    assert 'private-' not in json.dumps(reasoner.calls[-1]['crm_feedback'])
    assert 'discarded-now' not in json.dumps(reasoner.calls[-1]['crm_feedback'])


def test_new_guide_learns_only_from_previous_confirmed_criteria(setup):
    _, service, reasoner = setup
    review = review_first_round(service)
    applied = service.apply(T, review['id'], ACTOR, review['version'], profile(),
        lambda *args: {'saved_search_id':'SEARCH-CONFIRMED'})
    draft = answer(service, start(service), 'Un borrador sin aprobar')
    assert draft['state'] == 'draft'
    start(service)
    assert reasoner.calls[-1]['prior_approved'] == applied['criteria']


def test_two_concurrent_answers_claim_only_one_call(setup):
    _, service, reasoner = setup
    session = start(service)
    entered, release = threading.Event(), threading.Event()
    def slow(context):
        entered.set(); assert release.wait(5)
        return reasoner(context)
    service.generator = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(answer, service, session)
        assert entered.wait(5)
        try:
            with pytest.raises(CampaignError): answer(service, session)
        finally: release.set()
        assert future.result()['version'] == 2
    assert len(reasoner.calls) == 2


def test_uncertain_failure_requires_review_and_restart_never_retries(setup):
    engine, service, reasoner = setup
    session = start(service)
    def broken(_): raise httpx.ReadTimeout('secret provider details')
    service.generator = broken
    with pytest.raises(CampaignError, match='no se repetirá'): answer(service, session)
    assert service.get(T, session['id'], ACTOR)['state'] == 'review_required'
    service = TargetingGuide(engine, reasoner)
    with pytest.raises(CampaignError): answer(service, session)
    assert len(reasoner.calls) == 1
    with engine.db() as db:
        data = json.loads(db.execute('SELECT payload FROM targeting_sessions').fetchone()[0])
        data['state'] = 'generating'
        db.execute('UPDATE targeting_sessions SET payload=?', (json.dumps(data),))
    assert TargetingGuide(engine, reasoner).get(T, session['id'], ACTOR)['state'] == 'review_required'


def test_apply_explicit_version_and_profile_fingerprint_idempotent_no_research(setup):
    _, service, _ = setup
    session = review_first_round(service)
    writes = []
    def save(name, criteria):
        writes.append((name, criteria))
        return {'saved_search_id': 'SEARCH-NEW'}
    changed = profile(); changed['description'] = 'Actualizado'
    with pytest.raises(CampaignError, match='onboarding cambió'): service.apply(T, session['id'], ACTOR, session['version'], changed, save)
    result = service.apply(T, session['id'], ACTOR, session['version'], profile(), save)
    assert result['state'] == 'applied'
    assert service.apply(T, session['id'], ACTOR, session['version'], profile(), save) == result
    assert len(writes) == 1 and session['id'] in writes[0][0]


def test_cross_account_actor_and_missing_ai_never_consume(setup):
    engine, service, reasoner = setup
    session = start(service)
    with pytest.raises(CampaignError): service.get('control', session['id'], ACTOR)
    with pytest.raises(CampaignError): service.get(T, session['id'], 'other@example.test')
    with pytest.raises(CampaignError): TargetingGuide(engine).start(T, str(uuid4()), ACTOR, profile())
    assert len(reasoner.calls) == 1


def test_invalid_ai_schema_fails_closed_without_replacing_onboarding(setup):
    _, service, reasoner = setup
    session = start(service)
    previous = copy.deepcopy(session['profile'])
    def invalid(context):
        output = reasoner(context)
        output['turn']['criteria']['api_key'] = 'not-an-allowed-field'
        return output
    service.generator = invalid
    with pytest.raises(CampaignError): answer(service, session)
    state = service.get(T, session['id'], ACTOR)
    assert state['profile'] == previous and state['state'] == 'review_required'
    assert state['criteria'] == session['criteria']


def test_failed_save_is_not_retried_and_new_sessions_are_rate_limited(setup):
    _, service, reasoner = setup
    session = review_first_round(service)
    calls = []
    def uncertain(*args):
        calls.append(args)
        raise RuntimeError('storage timeout')
    with pytest.raises(CampaignError): service.apply(T, session['id'], ACTOR, session['version'], profile(), uncertain)
    with pytest.raises(CampaignError): service.apply(T, session['id'], ACTOR, session['version'], profile(), uncertain)
    assert len(calls) == 1
    start(service); start(service)
    with pytest.raises(CampaignError, match='conversaciones recientes'): start(service)


def test_expired_conversation_does_not_block_starting_a_fresh_one(setup):
    engine, service, _ = setup
    session = start(service)
    with engine.db() as db:
        session['created_at'] = 0
        db.execute('UPDATE targeting_sessions SET payload=?', (json.dumps(session),))
    assert service.latest(T, ACTOR) is None
    with pytest.raises(CampaignError, match='caducado'): service.get(T, session['id'], ACTOR)
    assert start(service)['state'] == 'draft'


def test_provider_structured_web_context_single_call_and_no_private_urls():
    requests = []
    def transport(request):
        data = json.loads(request.content); requests.append(data)
        result = FakeReasoner()({'profile': profile(), 'history': [], 'criteria': base_criteria(profile())})
        return httpx.Response(200, json={'status': 'completed', 'model': 'requested-model', 'id': 'resp-test',
            'output': [{'type':'message','content':[{'type':'output_text','text':json.dumps(result['turn'])}]},
                {'type':'web_search_call','action':{'sources':[{'url':'https://studio.example.com/services','title':'Servicios'},
                    {'url':'https://other-account.example.com/','title':'Otra empresa'}]}}]})
    settings = SimpleNamespace(openai_model='requested-model',openai_api_key='test-only-key')
    adapter = OpenAITargetingGuide(settings, transport=httpx.MockTransport(transport), website_validator=lambda x:x)
    context = {'profile':profile(),'history':[],'criteria':base_criteria(profile())}
    result = adapter(context)
    assert result['web_status'] == 'consulted' and len(result['sources']) == 1
    assert requests[0]['store'] is False and requests[0]['model'] == 'requested-model'
    assert requests[0]['max_tool_calls'] == 2
    assert requests[0]['tools'][0]['filters']['allowed_domains'] == ['studio.example.com']
    def blocked(_): raise ValueError('private network')
    adapter.website_validator = blocked
    assert adapter(context)['web_status'] == 'blocked_or_unavailable'
    assert 'tools' not in requests[1]


def test_api_scoping_csrf_validation_and_live_ghl_disabled(setup):
    engine, service, reasoner = setup
    actor = [Identity(ACTOR,'Administrador','owner',True,False)]
    engine.real = True
    with engine.db() as db:
        row = json.loads(db.execute('SELECT payload FROM tenants WHERE id=?',(T,)).fetchone()[0])
        row['access_emails'] = [ACTOR]
        row['source_id'] = 'ONB-TEST'
        db.execute('UPDATE tenants SET payload=? WHERE id=?',(json.dumps(row),T))
    app = FastAPI()
    template = lambda *a, **k: HTMLResponse('private review')
    source = OnboardingSource.from_sheet_record({'Empresa':'Estudio de prueba', 'Responsable':'Contacto de prueba',
        'Email responsable':'responsable@example.test', 'Ciclo de venta':'Dos semanas', 'Notas internas':'interno',
        'ID fiscal empresarial':'privado', 'Clave API':'secret-value', 'Config. técnica URL':'https://private.example.test'})
    app.include_router(guide_router(service,lambda:actor[0],template,lambda *a:profile(),lambda *a:None,
        intake_for=lambda *a:(profile(), guide_intake(source))))
    app.include_router(outreach_router(OutreachService(engine),lambda:actor[0],template))
    with TestClient(app) as client:
        sid = str(uuid4()); headers={'x-csrf-token':'test-csrf'};client.cookies.set('focus_csrf','test-csrf')
        assert client.get('/api/intelligence/accounts').json()['accounts'][0]['source_id'] == 'ONB-TEST'
        info = client.get(f'/api/intelligence/{T}/guide').json()
        fields = {row['label']:row['value'] for group in info['intake'] for row in group['fields']}
        assert fields == {'Responsable':'Contacto de prueba','Email responsable':'responsable@example.test',
            'Ciclo de venta':'Dos semanas'}
        assert client.post(f'/api/intelligence/{T}/guide/sessions',json={'request_id':sid}).status_code == 403
        assert client.post('/api/intelligence/control/guide/sessions',json={'request_id':sid},headers=headers).status_code == 403
        r = client.post(f'/api/intelligence/{T}/guide/sessions',json={'request_id':sid},headers=headers)
        assert r.status_code == 200
        assert 'responsable@example.test' not in json.dumps(reasoner.calls)
        session = r.json()
        for _ in range(10):
            r = client.post(f'/api/intelligence/{T}/guide/sessions/{sid}/answers',json={
                'version':session['version'],'request_id':str(uuid4()),
                'answer':session['turn']['options'][0],'mode':'option'},headers=headers)
            assert r.status_code == 200
            session = r.json()
        assert session['state'] == 'checkpoint'
        decision = {'version':session['version'],'choice':'review'}
        url = f'/api/intelligence/{T}/guide/sessions/{sid}/checkpoint'
        assert client.post(url, json=decision).status_code == 403
        assert client.post(url, json=decision, headers=headers).json()['state'] == 'ready'
        assert client.post(f'/api/intelligence/{T}/guide/sessions/{sid}/apply',json={'version':1,'confirmed':False},headers=headers).status_code == 422
        r = client.get(f'/api/intelligence/{T}/outreach/ghl/status')
        assert r.json()['state'] == 'setup_pending'
        assert not r.json()['sending_enabled'] and not r.json()['automatic_import']
        assert not r.json()['can_connect']
        assert 'test-only-key' not in r.text
        actor[0] = Identity('outside@example.test','Cliente','outside',True,False)
        assert client.get(f'/api/intelligence/{T}/guide').status_code == 403
        assert client.get(f'/api/intelligence/{T}/outreach/ghl/status').status_code == 403


def test_oauth_can_be_prepared_without_enabling_emails_or_provider_writes(setup, monkeypatch):
    engine, _, _ = setup
    for key, value in {'GHL_OAUTH_ENABLED':'1','OUTREACH_LIVE_EMAIL_ENABLED':'0',
        'OUTREACH_ENCRYPTION_KEY':base64.b64encode(b'x'*32).decode(),
        'GHL_CLIENT_ID':'test-client','GHL_CLIENT_SECRET':'test-secret'}.items():
        monkeypatch.setenv(key,value)
    app=FastAPI(); actor=Identity(ACTOR,'Administrador','owner',True,False)
    service=mount_outreach(app,engine,lambda:actor,lambda *a,**k:HTMLResponse('local'),SimpleNamespace(public_base_url='https://private.example.test'))
    assert service.provider is None
    service.configure_brand(T,{'location_id':'owned-location'})
    with TestClient(app) as client:
        status=client.get(f'/api/intelligence/{T}/outreach/ghl/status').json()
        assert status['can_connect'] and not status['sending_enabled'] and not status['connected']
        response=client.get(f'/api/intelligence/{T}/outreach/ghl/connect',follow_redirects=False)
        from urllib.parse import parse_qs,urlparse
        query=parse_qs(urlparse(response.headers['location']).query)
        assert query['scope']==['contacts.readonly contacts.write']
        assert query['redirect_uri']==['https://private.example.test/integrations/ghl/callback']
        assert 'test-secret' not in response.text and response.status_code==303

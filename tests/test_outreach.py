"""Local acceptance: no network, real recipients, API consumption or sends."""
import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import Identity
from app.intelligence import CampaignEngine, CampaignError
from app.outreach import OutreachService
from app.outreach_api import outreach_router
from app.outreach_content import DemoSequenceGenerator, dates, evidence, personal_linkedin, public_url, render_email, validate_emails
from app.outreach_providers import GHLGateway, GHLOAuth, OAuthVault, OpenAISequenceGenerator, verify_webhook

SEED = Path(__file__).parents[1] / 'app/intelligence_seed.json'
T = 'three-dots'
NOW = datetime.fromisoformat('2026-11-08T08:00:00+00:00').timestamp()


@pytest.fixture
def service(tmp_path):
    clock = [NOW]
    engine = CampaignEngine(tmp_path/'campaigns.db', SEED, clock=lambda: clock[0])
    s = OutreachService(engine)
    s.clock = clock
    s.configure_brand(T, {'company':'Three Dots Studio', 'logo_url':'https://example.test/logo.png', 'color':'#d9af3c',
        'sender_name':'Equipo', 'sender_email':'team@example.test', 'signature':'Three Dots Studio',
        'calendar_url':'https://example.test/agenda', 'authorization_record':'Solo prueba controlada', 'domain_verified':True})
    return s


def create(s, channels=None, ids=('td-01',)):
    contacts = {p['id']:p for p in s.eligibility(T)}
    return s.create(T, {'name':'Ensayo', 'channels':channels or ['email'], 'recipients':[{'lead_id':id,'email':contacts[id]['email']} for id in ids]})


def ready(s, **kw):
    c = create(s, **kw)
    d = s.prepare(T,c['id'])
    return s.approve(T,c['id'],d['digest'],'reviewer@example.test')['campaign']


def live(s):
    s.configure_brand(T, {'location_id':'loc-test'})
    s.configure_brand(T, {'workflow_id':'wf-owned', 'workflow_verified':True,
        'unsubscribe_verified':True,'template_ids':[f'template-{n}' for n in range(15)]})
    s.engine.real=True
    s.generator=DemoSequenceGenerator()  # Test-only injected provider, never runtime fallback.
    with s.engine.db() as db:
        for row in db.execute('SELECT id,payload FROM contacts WHERE tenant=?',(T,)).fetchall():
            p=json.loads(row['payload']);p['email_options']=[{'email':p['email'],'source_url':'https://example.test/contact'}]
            db.execute('UPDATE contacts SET payload=? WHERE tenant=? AND id=?',(json.dumps(p),T,row['id']))


def test_fifteen_library_and_recipient_calls_cached_no_quota_or_contacts_changed(service):
    s=service; calls=[]
    def generator(*args):
        calls.append(args)
        return DemoSequenceGenerator()(*args)
    s.generator=generator
    with s.engine.db() as db:
        before={table:[tuple(r) for r in db.execute('SELECT * FROM '+table)] for table in ('tenants','contacts','campaigns','members','jobs','crm','outbox')}
    c=create(s,ids=('td-01','td-02'))
    first=s.prepare(T,c['id']);s.prepare(T,c['id'])
    assert len(calls)==3 and len(first['campaign']['library']['emails'])==15
    assert all(len(m['emails'])==15 for m in first['campaign']['messages'].values())
    assert first['metrics']['sent_confirmed']==0
    with s.engine.db() as db:
        after={table:[tuple(r) for r in db.execute('SELECT * FROM '+table)] for table in before}
    assert after==before


def test_five_email_draft_names_and_renders_each_step(service):
    lead = next(lead for lead in service.eligibility(T) if lead['id'] == 'td-01')
    campaign = service.create(T, {'channels':['email'], 'steps':5,
        'recipients':[{'lead_id':lead['id'], 'email':lead['email']}]})
    result = service.prepare(T, campaign['id'])
    draft = result['campaign']
    assert draft['name'] != campaign['name'] and lead['company'] in draft['name']
    assert len(draft['schedule']) == result['metrics']['prepared'] == 5
    for step in range(1, 6):
        assert service.preview(T, draft['id'], lead['email'], step)['html']
    with pytest.raises(CampaignError):
        service.preview(T, draft['id'], lead['email'], 6)
    assert result['metrics']['sent_confirmed'] == 0


def test_social_prepares_only_verified_profiles_without_email_or_activation(service):
    with service.engine.db() as db:
        row = db.execute('SELECT payload FROM contacts WHERE tenant=? AND id=?', (T, 'td-01')).fetchone()
        lead = json.loads(row[0])
        lead.update(email='', email_options=[], linkedin='', social_links={
            'instagram':'https://www.instagram.com/focus_fixture/',
            'facebook':'https://www.facebook.com/focus-fixture/',
            'linkedin':'https://linkedin.com.example.test/fake'})
        db.execute('UPDATE contacts SET payload=? WHERE tenant=? AND id=?', (json.dumps(lead), T, 'td-01'))
    campaign = service.create(T, {'channels':['social'], 'steps':5, 'recipients':[{'lead_id':'td-01'}]})
    result = service.prepare(T, campaign['id'])
    messages = result['campaign']['messages']['lead:td-01']
    assert not messages['emails'] and set(messages['social']) == {'instagram', 'facebook'}
    assert result['campaign']['name'] != campaign['name']
    with pytest.raises(CampaignError, match='desde la red'):
        service.action(T, campaign['id'], 'activate')


def test_approval_required_stale_digest_and_frozen_active_content(service):
    s=service;c=create(s);cid=c['id']
    with pytest.raises(CampaignError):s.action(T,cid,'activate')
    d=s.prepare(T,cid);old=d['digest'];key=c['recipients'][0]['key']
    emails=copy.deepcopy(d['campaign']['messages'][key]['emails']);emails[0]['body']='Texto revisado sin promesas.'
    d=s.edit(T,cid,key,emails)
    with pytest.raises(CampaignError):s.approve(T,cid,old,'reviewer')
    s.approve(T,cid,d['digest'],'reviewer');s.action(T,cid,'activate')
    with pytest.raises(CampaignError):s.edit(T,cid,key,emails)
    s.configure_brand(T,{'calendar_url':'https://example.test/new-agenda'})
    assert s.detail(T,cid)['campaign']['brand']['calendar_url']=='https://example.test/agenda'
    duplicate=s.action(T,cid,'duplicate')
    assert duplicate['state']=='draft' and not duplicate['approval']
    s.prepare(T,duplicate['id'])
    with pytest.raises(CampaignError,match='campaña activa'):s.approve(T,duplicate['id'],s.detail(T,duplicate['id'])['digest'],'reviewer')


def test_library_edit_invalidates_draft_not_active(service):
    s=service;active=ready(s);s.action(T,active['id'],'activate')
    draft=create(s,ids=('td-02',));s.prepare(T,draft['id'])
    lib=s.library(T);emails=copy.deepcopy(lib['emails']);emails[0]['subject']='Una propuesta nueva'
    edited=s.edit_library(T,lib['version'],emails)
    assert edited['version']==2
    assert not s.detail(T,draft['id'])['campaign']['messages']
    assert s.detail(T,active['id'])['campaign']['library']['version']==1
    assert s.library(T)['version']==2


def test_brand_and_dates_changes_invalidate_approval(service):
    s=service;c=ready(s);s.configure_brand(T,{'sender_name':'Otro remitente'})
    assert s.detail(T,c['id'])['campaign']['approval'] is None
    d=s.detail(T,c['id']);s.approve(T,c['id'],d['digest'],'reviewer')
    s.reschedule(T,c['id']);assert s.detail(T,c['id'])['campaign']['approval'] is None


def test_expired_schedule_must_be_reviewed_again(service):
    s=service;c=ready(s);s.clock[0]+=2*86400
    with pytest.raises(CampaignError,match='fechas caducaron'):s.action(T,c['id'],'activate')


@pytest.mark.parametrize('instant',['2026-03-22T08:00:00+00:00','2026-10-18T08:00:00+00:00','2026-11-08T10:00:00+00:00'])
def test_fifteen_calendar_days_at_madrid_ten_including_dst_and_weekends(instant):
    schedule=[datetime.fromisoformat(x) for x in dates(datetime.fromisoformat(instant).timestamp())]
    assert len(schedule)==15 and all(x.hour==10 for x in schedule)
    assert all((b.date()-a.date()).days==1 for a,b in zip(schedule,schedule[1:]))
    assert {x.weekday() for x in schedule}==set(range(7))
    assert schedule[0].timestamp()>datetime.fromisoformat(instant).timestamp()


def test_selected_contact_dedup_and_one_active_campaign_under_concurrency(service):
    s=service;c1=ready(s);c2=ready(s)
    def activate(cid):
        try:return s.action(T,cid,'activate')['campaign']['state']
        except CampaignError:return 'blocked'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(activate,[c1['id'],c2['id']]))
    assert sorted(results)==['active','blocked']
    c=s.eligibility(T)[0];assert not c['email_ready']
    with pytest.raises(CampaignError):create(s)


def test_multiple_emails_require_explicit_choice_and_no_company_to_person_invention(service):
    s=service
    with s.engine.db() as db:
        p=json.loads(db.execute('SELECT payload FROM contacts WHERE tenant=? AND id=?',(T,'td-01')).fetchone()[0])
        p.update(email_options=[{'email':'one@example.test'},{'email':'two@example.test'}],name='Unverified',name_status='not_publicly_verified')
        db.execute('UPDATE contacts SET payload=? WHERE tenant=? AND id=?',(json.dumps(p),T,'td-01'))
    with pytest.raises(CampaignError):s.create(T,{'name':'x','channels':['email'],'recipients':[{'lead_id':'td-01','email':''}]})
    c=s.create(T,{'name':'x','channels':['email'],'recipients':[{'lead_id':'td-01','email':'two@example.test'}]*2})
    d=s.prepare(T,c['id']);assert len(c['recipients'])==1
    assert 'name' not in d['campaign']['messages']['two@example.test']['context']['recipient']


def test_changed_research_requires_new_review(service):
    s=service;c=ready(s)
    with s.engine.db() as db:
        p=json.loads(db.execute('SELECT payload FROM contacts WHERE tenant=? AND id=?',(T,'td-01')).fetchone()[0]);p['company']='Cambio posterior'
        db.execute('UPDATE contacts SET payload=? WHERE tenant=? AND id=?',(json.dumps(p),T,'td-01'))
    with pytest.raises(CampaignError,match='contexto cambió'):s.action(T,c['id'],'activate')


def test_uncertain_generation_never_retries_across_restart(service):
    s=service;calls=[]
    def broken(*args):calls.append(1);raise httpx.ReadTimeout('secret-provider-body')
    s.generator=broken
    with pytest.raises(CampaignError):s.library(T)
    s=OutreachService(s.engine,generator=broken)
    with pytest.raises(CampaignError):s.library(T)
    assert calls==[1]


def test_real_mode_no_fixture_fallback(service):
    s=OutreachService(service.engine);s.engine.real=True
    s=OutreachService(s.engine)
    with pytest.raises(CampaignError,match='no se ha consumido'):s.library(T)


def test_timeout_activation_and_pause_pending_no_blind_repeat(service):
    s=service;live(s);calls=[]
    class Provider:
        def transition(self,*args):calls.append(args[-1]);raise httpx.ReadTimeout('untrusted secret')
    s.provider=Provider();c=ready(s)
    with pytest.raises(CampaignError,match='sin repetir'):s.action(T,c['id'],'activate',live=True)
    assert s.detail(T,c['id'])['campaign']['state']=='activation_uncertain'
    with pytest.raises(CampaignError):s.action(T,c['id'],'activate',live=True)
    with pytest.raises(CampaignError):s.action(T,c['id'],'pause')
    assert s.detail(T,c['id'])['campaign']['state']=='pause_pending'
    assert calls==['activate','pause']


def test_linkedin_requires_personal_profile_manual_confirmations_followup_and_stop(service):
    s=service
    with pytest.raises(CampaignError):create(s,channels=['linkedin'])
    with s.engine.db() as db:
        p=json.loads(db.execute('SELECT payload FROM contacts WHERE tenant=? AND id=?',(T,'td-01')).fetchone()[0]);p['linkedin']='https://www.linkedin.com/in/test-person/'
        db.execute('UPDATE contacts SET payload=? WHERE tenant=? AND id=?',(json.dumps(p),T,'td-01'))
    c=ready(s,channels=['linkedin']);cid=c['id'];key=c['recipients'][0]['key']
    texts=c['messages'][key]['linkedin'];assert len(texts['invitation'])<=300
    d=s.action(T,cid,'activate');assert d['tasks'][0]['kind']=='invitation'
    with pytest.raises(CampaignError):s.linkedin_event(T,cid,key,'connection_accepted','actor')
    s.linkedin_event(T,cid,key,'invitation_sent','actor');s.linkedin_event(T,cid,key,'connection_accepted','actor')
    s.linkedin_event(T,cid,key,'message_sent','actor')
    with pytest.raises(CampaignError):s.linkedin_event(T,cid,key,'followup_sent','actor')
    s.clock[0]+=3*86400;s.linkedin_event(T,cid,key,'followup_sent','actor')
    s.linkedin_event(T,cid,key,'response','actor')
    assert s.detail(T,cid)['metrics']['sent_confirmed']==0
    with pytest.raises(CampaignError):create(s,channels=['email'])


def test_provider_events_dedup_per_message_not_webhook_and_stop_only_one_recipient(service):
    s=service;live(s)
    class Provider:
        def transition(self,t,c,a):return {r['key']:{'contact_id':r['lead_id'],'status':'scheduled'} for r in c['recipients']}
    s.provider=Provider();c=ready(s,ids=('td-01','td-02'));s.action(T,c['id'],'activate',live=True)
    assert s.provider_event(T,'1','loc-test','td-01','delivered',message_id='message-1',workflow_id='wf-owned')['ok']
    assert s.provider_event(T,'2','loc-test','td-01','delivered',message_id='message-1',workflow_id='wf-owned')['deduplicated']
    s.provider_event(T,'3','loc-test','td-01','response',message_id='reply-1')
    eligible={r['id']:r for r in s.eligibility(T)}
    assert 'Baja o condición de parada registrada' in eligible['td-01']['reasons']
    assert 'Baja o condición de parada registrada' not in eligible['td-02']['reasons']
    with pytest.raises(CampaignError):s.provider_event(T,'4','other-loc','td-01','accepted',message_id='x')


@pytest.mark.parametrize('url',['http://example.com','https://127.0.0.1','https://169.254.169.254','https://localhost','https://example.com:8080','https://user:pass@example.com','javascript:alert(1)'])
def test_brand_urls_no_private_or_unsafe_targets(url):
    with pytest.raises(CampaignError):public_url(url)


@pytest.mark.parametrize('url',['https://linkedin.com/company/example','https://evil.example/in/person','https://linkedin.com/in/person?tracking=x'])
def test_linkedin_company_and_unverified_links_rejected(url):assert not personal_linkedin(url)


def test_html_escape_plaintext_and_only_own_calendar(service):
    s=service;c=ready(s);key=c['recipients'][0]['key'];email=copy.deepcopy(c['messages'][key]['emails'][0]);email['body']='<script>untrusted</script> & text'
    rendered=render_email(email,c['brand'])
    assert '<script>' not in rendered['html'] and '&lt;script&gt;' in rendered['html']
    assert 'https://example.test/agenda' in rendered['html'] and '{{unsubscribe_link}}' in rendered['html']
    assert '<script>' in rendered['text']


def test_vault_encryption_state_single_use_expiry_and_refresh_timeout(service):
    s=service;vault=OAuthVault(s.engine,b'k'*32)
    vault.put(T,{'access_token':'private-access','refresh_token':'private-refresh','locationId':'loc-test','expires_at':1})
    with s.engine.db() as db:
        assert 'private-access' not in db.execute('SELECT encrypted FROM outreach_oauth').fetchone()[0]
    state=vault.begin(T,'actor');assert vault.consume(state,'actor')==T
    with pytest.raises(CampaignError):vault.consume(state,'actor')
    state=vault.begin(T,'actor');s.clock[0]+=601
    with pytest.raises(CampaignError):vault.consume(state,'actor')
    calls=[]
    def timeout(request):calls.append(request);raise httpx.ReadTimeout('unknown')
    oauth=GHLOAuth(vault,'client','secret','https://example.test/callback',transport=httpx.MockTransport(timeout))
    for _ in range(2):
        with pytest.raises(CampaignError):oauth.access_token(T)
    assert len(calls)==1 and vault.get(T)['refresh_pending']


def test_signed_webhook_raw_body_and_tamper_rejected():
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    body=b'{"locationId":"loc-test"}';signature=base64.b64encode(key.sign(body)).decode()
    assert verify_webhook(body,signature,public)['locationId']=='loc-test'
    with pytest.raises(CampaignError):verify_webhook(body+b' ',signature,public)


@pytest.mark.parametrize('ambiguous',[True,False])
def test_ghl_exact_selected_contact_fields_no_unrelated_fields_or_ambiguous_writes(service,ambiguous):
    s=service;live(s);c=ready(s);vault=OAuthVault(s.engine,b'k'*32);vault.put(T,{'access_token':'mock-only','locationId':'loc-test'})
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.path=='/contacts/lookup':
            assert request.url.params['email']==c['recipients'][0]['email']
            return httpx.Response(200,json={'contacts':[{'id':'remote','locationId':'loc-test'}]*(2 if ambiguous else 1)})
        if request.method=='PUT':
            payload=json.loads(request.content);assert set(payload)=={'customFields'} and len(payload['customFields'])==46
            return httpx.Response(200,json={})
        return httpx.Response(200,json={'succeeded':True})
    fields={f'fi_email_{n:02}_{part}':f'owned-{n}-{part}' for n in range(1,16) for part in ('subject','preheader','body')};fields['fi_next_step']='owned-next'
    gateway=GHLGateway(vault,fields={T:fields},checkpoint=s.checkpoint,transport=httpx.MockTransport(handler),enabled=True)
    if ambiguous:
        with pytest.raises(CampaignError,match='ambiguos'):gateway.transition(T,c,'activate')
        assert all(r.method=='GET' for r in calls)
    else:
        states=gateway.transition(T,c,'activate');assert states[c['recipients'][0]['key']]['status']=='scheduled'
        assert [r.method for r in calls]==['GET','PUT','POST']
        assert json.loads(calls[-1].content)['eventStartTime']==c['schedule'][0]


def test_api_tenant_isolation_csrf_and_signed_event_rejection(service):
    s=service;app=FastAPI();identity=Identity('control@example.test','Cliente','test',True,True)
    app.include_router(outreach_router(s,lambda:identity,lambda *a,**k:None,webhook_key='invalid-key'))
    client=TestClient(app)
    assert client.get('/api/intelligence/three-dots/outreach').status_code==403
    assert client.get('/api/intelligence/accounts').json()['accounts'][0]['id']=='control'
    assert client.post('/api/intelligence/control/outreach/library').status_code==403
    assert client.post('/webhooks/ghl',json={}).status_code==401


def test_five_base_templates_and_one_contact_draft_through_http(service):
    app = FastAPI()
    identity = Identity('three-dots@example.test', 'Cliente', 'test', True, True)
    app.include_router(outreach_router(service, lambda: identity, lambda *args, **kwargs: None))
    client = TestClient(app, cookies={'focus_csrf': 'test-csrf'}, headers={'X-CSRF-Token': 'test-csrf'})
    url = '/api/intelligence/three-dots/outreach'
    lead = next(contact for contact in service.eligibility(T) if contact['email_ready'])

    library = client.post(url + '/library?steps=5', json={})
    assert library.status_code == 200, library.json()
    assert len(library.json()['emails']) == 5
    version = library.json()['version']
    assert client.post(url + '/library?steps=4', json={}).status_code == 422

    draft = client.post(url + '/campaigns', json={
        'channels': ['email'], 'steps': 5, 'recipients': [{'lead_id': lead['id']}],
    })
    assert draft.status_code == 200, draft.json()
    cid = draft.json()['id']
    prepared = client.post(f'{url}/campaigns/{cid}/prepare', json={})
    assert prepared.status_code == 200, prepared.json()
    result = prepared.json()
    assert result['campaign']['name'] != draft.json()['name']
    assert result['metrics']['prepared'] == 5 and result['metrics']['sent_confirmed'] == 0
    assert result['campaign']['state'] == 'draft' and not result['campaign']['approval']
    recipient = result['campaign']['recipients'][0]['email']
    assert recipient == lead['email'].lower()
    for step in range(1, 6):
        base = client.get(f'{url}/library/{version}/preview', params={'step': step})
        email = client.get(f'{url}/campaigns/{cid}/preview', params={'step': step, 'recipient': recipient})
        for response in (base, email):
            assert response.status_code == 200
            assert response.json()['preview_only'] is True
            assert 'https://example.test/agenda' in response.json()['html']
    assert client.get(f'{url}/library/{version}/preview?step=6').status_code == 422
    with service.engine.db() as db:
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0


def test_one_public_address_is_inferred_but_two_require_a_choice(service):
    with service.engine.db() as db:
        row = db.execute('SELECT payload FROM contacts WHERE tenant=? AND id=?', (T, 'td-01')).fetchone()
        contact = json.loads(row['payload'])
        contact['email_options'] = [{'email': contact['email'], 'source_url': 'https://example.test/contact'},
                                    {'email': contact['email'].upper(), 'source_url': 'https://example.test/contact'}]
        contact['summary'] = 'La empresa publica una serie de entrevistas corporativas.'
        db.execute('UPDATE contacts SET payload=? WHERE tenant=? AND id=?', (json.dumps(contact), T, 'td-01'))
    payload = {'channels': ['email'], 'steps': 5, 'recipients': [{'lead_id': 'td-01'}]}
    campaign = service.create(T, payload)
    draft = service.prepare(T, campaign['id'])['campaign']
    context = draft['messages'][contact['email'].lower()]['context']
    assert context['sources'][1]['summary'] == contact['summary']
    assert contact['email'] not in json.dumps(context)
    contact['email_options'].append({'email': 'team@example.test', 'source_url': 'https://example.test/contact'})
    with service.engine.db() as db:
        db.execute('UPDATE contacts SET payload=? WHERE tenant=? AND id=?', (json.dumps(contact), T, 'td-01'))
    with pytest.raises(CampaignError, match='Elige un correo público'):
        service.create(T, payload)


def test_openai_strict_fifteen_no_tools_store_or_search_quota(service):
    ctx=evidence(service.engine.catalog(T)['profile'])
    def handler(request):
        payload=json.loads(request.content)
        assert payload['store'] is False and 'tools' not in payload
        instruction=payload['input'][0]['content']
        assert 'No incluyas notas internas' in instruction and 'exclusivamente el correo dirigido al destinatario' in instruction
        assert payload['text']['format']['schema']['properties']['emails']['minItems']==15
        return httpx.Response(200,json={'status':'completed','model':'mock-model','id':'mock-response','usage':{'input_tokens':12,'output_tokens':34},'output':[{'content':[{'type':'output_text','text':json.dumps(DemoSequenceGenerator()(ctx))}]}]})
    gen=OpenAISequenceGenerator(SimpleNamespace(openai_model='mock-model',openai_api_key='mock-only'),transport=httpx.MockTransport(handler))
    result=gen(ctx);assert len(validate_emails(result,{'sender:declared'}))==15 and result['actual_cost_usd'] is None


def test_restore_full_snapshot_preserves_approval_and_no_browser_scheduler(service,tmp_path):
    s=service;c=ready(s);s.action(T,c['id'],'activate')
    with s.engine.db() as db:snapshot='\n'.join(db.iterdump())
    class Persistence:
        def load(self):return snapshot
        def save(self,value):pass
    restored=CampaignEngine(tmp_path/'restored.db',persistence=Persistence(),clock=lambda:NOW)
    recovered=OutreachService(restored)
    before=recovered.detail(T,c['id']);restored.tick(advance=20*86400);after=recovered.detail(T,c['id'])
    assert before['campaign']==after['campaign'] and after['metrics']['sent_confirmed']==0
    assert len(after['campaign']['messages'][c['recipients'][0]['key']]['emails'])==15


def test_stop_provider_confirmation_and_no_repeated_cancel(service):
    s=service;live(s);calls=[]
    class Provider:
        def transition(self,t,c,a):return {r['key']:{'contact_id':r['lead_id'],'status':'scheduled'} for r in c['recipients']}
        def stop(self,t,c,remote):calls.append(remote['contact_id'])
    s.provider=Provider();c=ready(s,ids=('td-01','td-02'));s.action(T,c['id'],'activate',live=True)
    for event in ('reply-a','reply-b'):s.provider_event(T,event,'loc-test','td-01','response',message_id=event)
    states=s.detail(T,c['id'])['campaign']['provider_states']
    assert states[c['recipients'][0]['key']]['status']=='stopped'
    assert states[c['recipients'][1]['key']]['status']=='scheduled'
    assert calls==['td-01']


def test_fifteen_provider_steps_deduplicated_and_unrelated_workflow_rejected(service):
    s=service;live(s)
    class Provider:
        def transition(self,t,c,a):return {r['key']:{'contact_id':'remote','status':'scheduled'} for r in c['recipients']}
    s.provider=Provider();c=ready(s);s.action(T,c['id'],'activate',live=True)
    assert s.provider_event(T,'other','loc-test','remote','accepted',message_id='unrelated',workflow_id='other')['ignored']
    for n in range(1,16):
        s.clock[0]=datetime.fromisoformat(c['schedule'][n-1]).timestamp()
        s.provider_event(T,str(n),'loc-test','remote','accepted',message_id=f'msg-{n}',step=n,workflow_id='wf-owned')
        s.provider_event(T,'duplicate-'+str(n),'loc-test','remote','accepted',message_id=f'msg-{n}',step=n,workflow_id='wf-owned')
    detail=s.detail(T,c['id'])
    assert detail['metrics']['sent_confirmed']==15
    assert detail['campaign']['state']=='completed'
    assert detail['campaign']['provider_states'][c['recipients'][0]['key']]['status']=='completed'
    s.provider_event(T,'late-delivery','loc-test','remote','delivered',message_id='msg-15',workflow_id='wf-owned')
    assert s.detail(T,c['id'])['metrics']['delivered']==1


def test_ghl_pause_and_resume_uses_confirmed_next_step_without_repeating_previous(service):
    s=service;live(s);c=ready(s);key=c['recipients'][0]['key'];c['provider_states']={key:{'contact_id':'remote','status':'paused'}}
    vault=OAuthVault(s.engine,b'k'*32);vault.put(T,{'access_token':'mock','locationId':'loc-test'});calls=[]
    def handler(request):
        calls.append(request)
        if request.method=='GET' and request.url.path=='/contacts/remote':return httpx.Response(200,json={'contact':{'locationId':'loc-test','customFields':[{'id':'owned-next','value':'8'}]}})
        if request.url.path=='/contacts/lookup':return httpx.Response(200,json={'contacts':[{'id':'remote','locationId':'loc-test'}]})
        if request.method=='DELETE':return httpx.Response(200,json={})
        if request.method=='PUT':
            assert next(x['field_value'] for x in json.loads(request.content)['customFields'] if x['id']=='owned-next')=='8'
            return httpx.Response(200,json={})
        return httpx.Response(200,json={'succeeded':True})
    fields={f'fi_email_{n:02}_{part}':f'owned-{n}-{part}' for n in range(1,16) for part in ('subject','preheader','body')};fields['fi_next_step']='owned-next'
    g=GHLGateway(vault,fields={T:fields},checkpoint=s.checkpoint,transport=httpx.MockTransport(handler),enabled=True)
    assert g.transition(T,c,'pause')[key]['status']=='paused'
    assert g.transition(T,c,'resume')[key]['next_step']==8
    assert len([r for r in calls if r.method=='POST'])==1


def test_admin_template_preparation_is_claimed_once(service):
    s=service;live(s);calls=[]
    class Provider:
        def provision_templates(self,t,b,lib):calls.append(1);return [f'template-{n}' for n in range(15)]
    s.provider=Provider();lib=s.library(T)
    assert len(s.provision_templates(T,lib['version'])['template_ids'])==15
    with pytest.raises(CampaignError,match='ya iniciada'):s.provision_templates(T,lib['version'])
    assert calls==[1]

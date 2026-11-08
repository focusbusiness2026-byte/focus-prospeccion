import json
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.auth import Identity
from app.intelligence import CampaignEngine,CampaignError,DEFAULT_STEPS,validate_steps
from app.intelligence_api import campaign_router
from app.intelligence_adapters import OpenAIMessageGenerator,SheetsSnapshot,PrivatePortalSnapshot,PRODUCTION_BOOK
from app.config import Settings
from app.sandbox_security import verify_handoff
from app.intelligence_contacts import prospect_contact

SEED=Path(__file__).parents[1]/'app/intelligence_seed.json'

def test_real_research_contact_schema_and_historical_roles():
    p={'execution_id':'lead-1','company':'Empresa','lead_status':'Aprobado para descarga',
       'public_contacts_json':json.dumps([{'type':'email','value':'office@example.test','source_url':'https://example.test/contact'},{'type':'phone','value':'+34000000000','source_url':'https://example.test/contact'}]),
       'decision_makers_json':json.dumps([{'name':'Historical','role_status':'historical','source_url':'https://example.test/old'},{'name':'Actual','role':'CEO','role_status':'current','source_url':'https://example.test/team'}])}
    c=prospect_contact(p)
    assert c['email']=='office@example.test' and c['phone']=='+34000000000'
    assert c['name']=='Actual' and c['role']=='CEO'
    p['decision_makers_json']=json.dumps([{'name':'Historical','role_status':'historical','source_url':'https://example.test/old'}])
    assert prospect_contact(p)['name']==''

def engine(tmp_path,**kw):return CampaignEngine(tmp_path/'campaigns.db',SEED,clock=lambda:1000,**kw)
def create(e,**kw):return e.create('three-dots',{'name':'Prueba','objective':'Reunión comercial','contact_ids':[f'td-{n:02}' for n in range(1,21)],**kw})

def test_twenty_approved_and_isolation(tmp_path):
    e=engine(tmp_path);c=create(e);assert len(c['contact_ids'])==20
    with pytest.raises(CampaignError):e.detail('control',c['id'])
    with pytest.raises(CampaignError):e.create('control',{'name':'wrong','objective':'x','contact_ids':['td-01']})
    assert len(e.catalog('control')['contacts'])==1

def test_sequence_rejects_cycles_disconnected_and_invalid_wait():
    validate_steps(DEFAULT_STEPS)
    for steps in ([{'kind':'start','next':0},{'kind':'stop'}],[{'kind':'start','next':2},{'kind':'wait'},{'kind':'stop'}],[{'kind':'start'},{'kind':'wait','days':-1},{'kind':'stop'}]):
        with pytest.raises(CampaignError):validate_steps(steps)

def test_queue_restart_pause_and_manual_task_idempotence(tmp_path):
    e=engine(tmp_path);c=create(e);e.action('three-dots',c['id'],'activate');e.tick()
    d=e.detail('three-dots',c['id']);assert len(d['tasks'])==20 and len(d['crm_mock'])==20
    assert len(d['messages'])==20
    e.action('three-dots',c['id'],'pause');task=d['tasks'][0]
    with pytest.raises(CampaignError):e.complete_task('three-dots',c['id'],task['id'])
    e=engine(tmp_path);e.action('three-dots',c['id'],'resume')
    e.event('three-dots',c['id'],task['contact'],'connection_accepted','test-1')
    assert e.event('three-dots',c['id'],task['contact'],'connection_accepted','test-1')['deduplicated']
    e.complete_task('three-dots',c['id'],task['id']);assert e.complete_task('three-dots',c['id'],task['id'])['deduplicated']
    e.tick();assert len(e.detail('three-dots',c['id'])['tasks'])==20
    e.tick(advance=2*86400);assert len(e.detail('three-dots',c['id'])['tasks'])==21
    e.tick(advance=2*86400);assert len(e.detail('three-dots',c['id'])['tasks'])==21

def test_response_and_global_suppression(tmp_path):
    e=engine(tmp_path);c=create(e);c2=create(e,name='Segunda')
    for x in (c,c2):e.action('three-dots',x['id'],'activate')
    e.tick();e.event('three-dots',c['id'],'td-01','response','reply');e.event('three-dots',c['id'],'td-01','positive_response','positive')
    assert e.detail('three-dots',c['id'])['metrics']['responses_registered']==1
    for x in (c,c2):
        d=e.detail('three-dots',x['id']);assert all(t['state']=='cancelled' for t in d['tasks'] if t['contact']=='td-01')
    e.event('three-dots',c['id'],'td-02','opt_out','stop')
    with pytest.raises(CampaignError):create(e,name='No contactar',contact_ids=['td-02'])

def test_email_approval_and_retry_never_sends(tmp_path):
    e=engine(tmp_path);steps=[{'kind':'start'},{'kind':'message'},{'kind':'email','simulate_failures':2},{'kind':'stop'}]
    c=create(e,steps=steps,contact_ids=['td-01'])
    with pytest.raises(CampaignError):e.action('three-dots',c['id'],'activate')
    e.update('three-dots',c['id'],{'email_authorization':True,'email_origin':'Prueba sin envío'})
    e.action('three-dots',c['id'],'activate');e.tick();e.tick(advance=6);e.tick(advance=11)
    assert not e.detail('three-dots',c['id'])['outbox']
    e.messages('three-dots',c['id'],'td-01',{'email':'Texto revisado','approved':True});e.tick()
    d=e.detail('three-dots',c['id']);assert len(d['outbox'])==1 and d['metrics']['real_messages_sent']==0
    e.tick();assert len(e.detail('three-dots',c['id'])['outbox'])==1

def test_concurrent_ticks_have_single_task_and_crm(tmp_path):
    e=engine(tmp_path);c=create(e);e.action('three-dots',c['id'],'activate')
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:e.tick(),range(4)))
    d=e.detail('three-dots',c['id']);assert len(d['tasks'])==20 and len(d['crm_mock'])==20

class Snapshot:
    def __init__(self):self.value=None
    def load(self):return self.value
    def save(self,text):self.value=text

def test_durable_restore_without_runtime_files(tmp_path):
    s=Snapshot();e=engine(tmp_path,persistence=s);c=create(e);e.action('three-dots',c['id'],'activate');e.tick()
    recovered=CampaignEngine(tmp_path/'new-instance.db',persistence=s,clock=lambda:1000)
    assert len(recovered.detail('three-dots',c['id'])['tasks'])==20
    recovered.tick();assert len(recovered.detail('three-dots',c['id'])['tasks'])==20

def test_real_generation_one_call_and_fail_closed(tmp_path):
    calls=[]
    def provider(*args):calls.append(args);return {k:'Texto contrastado' for k in ('invitation','first','followup','email','rationale')}|{'usage':{'input_tokens':10,'output_tokens':20}}
    e=engine(tmp_path,real=True,generator=provider);c=create(e,contact_ids=['td-01']);e.action('three-dots',c['id'],'activate');e.tick()
    assert not calls and e.detail('three-dots',c['id'])['members'][0]['state']=='awaiting_generation'
    e.generate('three-dots',c['id'],'td-01');e.generate('three-dots',c['id'],'td-01');assert len(calls)==1
    e.tick();d=e.detail('three-dots',c['id']);assert d['metrics']['billable_calls']==1 and d['metrics']['input_tokens']==10
    assert d['metrics']['actual_cost_usd'] is None
    with pytest.raises(CampaignError):e.tick(advance=1)

def test_uncertain_provider_response_no_retry_after_restart(tmp_path):
    calls=[]
    def fail(*args):calls.append(1);raise TimeoutError('must not leak provider errors')
    e=engine(tmp_path,real=True,generator=fail);c=create(e,contact_ids=['td-01'])
    with pytest.raises(CampaignError,match='pausada'):e.generate('three-dots',c['id'],'td-01')
    e=engine(tmp_path,real=True,generator=fail)
    with pytest.raises(CampaignError,match='no se repite'):e.generate('three-dots',c['id'],'td-01')
    assert calls==[1] and e.detail('three-dots',c['id'])['campaign']['state']=='paused'

def test_message_versions_and_plan_limit(tmp_path):
    e=engine(tmp_path);c=create(e,contact_ids=['td-01']);m=e.messages('three-dots',c['id'],'td-01');changed=e.messages('three-dots',c['id'],'td-01',{'first':'Revisión','approved':True})
    assert changed['version']==2 and changed['history'][0]['first']==m['first']
    create(e,name='2');create(e,name='3')
    with pytest.raises(CampaignError,match='Límite'):create(e,name='4')

def test_api_unauthenticated_and_cross_company_denied(tmp_path):
    e=engine(tmp_path);app=FastAPI()
    def denied():raise HTTPException(401,'Inicia sesión')
    app.include_router(campaign_router(e,denied,lambda *_:None));client=TestClient(app)
    assert client.get('/api/intelligence/three-dots/catalog').status_code==401
    app.dependency_overrides[denied]=lambda:Identity('control@example.test','Cliente','1')
    assert client.get('/api/intelligence/three-dots/catalog').status_code==403
    assert client.get('/api/intelligence/control/catalog').status_code==200
    assert client.post('/api/intelligence/control/campaigns',json={}).status_code==403

def test_openai_transport_usage_and_no_retry():
    calls=[]
    def handle(req):
        body=json.loads(req.content);calls.append(body)
        assert body['store'] is False and 'tools' not in body and body['text']['format']['strict'] is True
        return httpx.Response(200,json={'id':'resp_test','status':'completed','usage':{'input_tokens':18,'output_tokens':30,'total_tokens':48},'output':[{'content':[{'type':'output_text','text':json.dumps({k:'Verificado' for k in ('invitation','first','followup','email','rationale')})}]}]})
    p=OpenAIMessageGenerator(Settings(_env_file=None,openai_api_key='test-only'),httpx.MockTransport(handle))
    result=p({'name':'Test'},{'offer':'Podcast','objective':'Reunión','language':'es','tone':'formal','length':'breve'},{'company':'Test'})
    assert len(calls)==1 and result['usage']['total_tokens']==48 and result['provider_response_id']=='resp_test'

def test_production_book_is_rejected():
    with pytest.raises(RuntimeError,match='producción'):SheetsSnapshot(Settings(_env_file=None,google_sheet_id=PRODUCTION_BOOK),'FICampaigns')

def test_private_snapshot_cas_integrity_and_ambiguous_write():
    import hashlib
    state={'revision':0,'found':False};calls=[]
    def handle(req):
        assert req.url.host=='focus-business-sandbox-20261108.moisses.chatgpt.site'
        assert req.headers['OAI-Sites-Authorization']=='Bearer test-only'
        calls.append(req.method)
        if req.method=='GET':return httpx.Response(200,json=state)
        body=json.loads(req.content)
        if body['revision']!=state['revision']:return httpx.Response(409)
        state.update(value=body['value'],revision=state['revision']+1,found=True,sha256=hashlib.sha256(body['value'].encode()).hexdigest())
        return httpx.Response(200,json={'ok':True,**state})
    transport=httpx.MockTransport(handle)
    s=PrivatePortalSnapshot('test-only','FICampaigns',transport);assert s.load() is None
    s.save('persisted');assert s.load()=='persisted'
    stale=PrivatePortalSnapshot('test-only','FICampaigns',transport);stale.load()
    s.save('latest')
    with pytest.raises(RuntimeError):stale.save('must not overwrite')
    count=len(calls)
    with pytest.raises(RuntimeError):stale.save('no blind retry')
    assert len(calls)==count and s.load()=='latest'

def test_invalid_handoff_rejected():
    with pytest.raises(ValueError):verify_handoff('bad','x'*32,'https://sandbox.test')

def test_real_context_regeneration_keeps_paid_history_and_usage(tmp_path):
    def provider(*_):return {k:'Contrastado' for k in ('invitation','first','followup','email','rationale')}|{'usage':{'input_tokens':10,'output_tokens':20}}
    e=engine(tmp_path,real=True,generator=provider);c=create(e,contact_ids=['td-01'])
    e.generate('three-dots',c['id'],'td-01');e.update('three-dots',c['id'],{'objective':'Nuevo objetivo'})
    with pytest.raises(CampaignError,match='contexto'):e.messages('three-dots',c['id'],'td-01')
    m=e.generate('three-dots',c['id'],'td-01');d=e.detail('three-dots',c['id'])
    assert len(m['history'])==1 and m['context']['campaign_version']==2
    assert d['metrics']['billable_calls']==2 and d['metrics']['input_tokens']==20

def test_snapshot_failure_rolls_back_local_mutation(tmp_path):
    class Failing(Snapshot):
        fail=False
        def save(self,text):
            if self.fail:raise RuntimeError('durability unavailable')
            super().save(text)
    s=Failing();e=engine(tmp_path,persistence=s);s.fail=True
    with pytest.raises(RuntimeError):create(e)
    assert e.catalog('three-dots')['campaigns']==[]

def test_real_sandbox_gate_import_and_cross_tenant(tmp_path):
    import subprocess,sys,os
    script=r'''
import json,time,base64,hmac,hashlib
from fastapi.testclient import TestClient
from app import sandbox_real as real
from app.sheet_store import PROSPECT_HEADERS
snapshots={}
class Snapshot:
    def __init__(self,settings,tab):self.tab=tab;self.uncertain=False
    def load(self):return snapshots.get(self.tab)
    def save(self,value):snapshots[self.tab]=value
class Provider:
    def __init__(self,*_):pass
    def __call__(self,*_):raise AssertionError('No paid calls during import')
real.SheetsSnapshot=Snapshot;real.OpenAIMessageGenerator=Provider
onb={'ID registro':real.SOURCE_ID,'Empresa':'Three Dots Studio','Web':'https://threedots.studio/','Email responsable':'client@example.test','Servicio prioritario':'Podcast','Público':'B2B','Sectores':'Salud','Mercados':'España','Tamaño empresa ideal':'1–10','Autorización':'true','Clave API privada':'MUST_NOT_COPY','NIF':'MUST_NOT_COPY'}
lead={'execution_id':'real-id-preserved','email':'client@example.test','company':'Empresa real de prueba','lead_status':'Aprobado para descarga','onboarding_id':real.SOURCE_ID,'productora':'Three Dots Studio','public_contacts_json':'[]'}
tables={'Onboarding':[list(onb),list(onb.values())],'Prospeccion':[PROSPECT_HEADERS,[lead.get(h,'') for h in PROSPECT_HEADERS]]}
class Result:
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def json(self):return self.data
class Reader:
    def __init__(self,*_):self._base='https://sheets.googleapis.com/v4/spreadsheets/source/values'
    def _sheet_properties(self):return [{'title':t,'sheetId':i,'gridProperties':{'rowCount':2,'columnCount':len(r[0])}} for i,(t,r) in enumerate(tables.items())]
    def _get(self,r):
        if 'Accesos' in r:return [['email','role','state','date','assigned','used','available','percent','quota','renew','p','r','principal','company'],['client@example.test','Cliente','Activo','',50,2,48,0.96,'Disponible','','Permitido','Permitido','','Three Dots Studio']]
        title=r.split('!')[0].strip("'")
        if r.split('!')[1].startswith('A1:'):return [tables[title][0]]
        return [[real.SOURCE_ID]]
    def _session(self):return self
    def get(self,url,params,**kw):return Result({'valueRanges':[{'values':[tables[r.split('!')[0].strip("'")][1]]} for r in params['ranges']]})
real.ReadOnlySource=Reader
captured=[]
def fake_research(source,store,*args,**kwargs):
    captured.append(args[0] if args else kwargs['adjustments'])
    return {'execution_id':'controlled-test','prospects':[],'trace':{}}
real.main._run_onboarding_research=fake_research
from app.sandbox import app
with TestClient(app) as c:
    assert c.get('/api/portal-dashboard').status_code==401
    assert c.get('/api/intelligence/three-dots/catalog').status_code==401
    assert c.post('/sandbox/import-client').status_code==401
    claims={'aud':'http://testserver','sub':'sandbox-owner','tenant':'three-dots','exp':time.time()+60,'nonce':'a'*32}
    raw=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    signature=base64.urlsafe_b64encode(hmac.new(b'test-secret-value-of-32-characters',raw.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    token=raw+'.'+signature
    origin={'Origin':real.PORTAL}
    assert c.post('/sandbox/session',data={'token':token},headers={'Origin':'https://evil.example'},follow_redirects=False).status_code==403
    assert c.post('/sandbox/session',data={'token':'forged'},headers={'Origin':'null'},follow_redirects=False).status_code==401
    r=c.post('/sandbox/session',data={'token':token},headers={'Origin':'null'},follow_redirects=False)
    assert r.status_code==303
    assert 'HttpOnly' in r.headers['set-cookie']
    assert c.post('/sandbox/session',data={'token':token},headers=origin,follow_redirects=False).status_code==401
    c.get('/sandbox/setup');csrf={'X-CSRF-Token':c.cookies['focus_csrf']}
    r=c.post('/sandbox/import-client',headers=csrf);assert r.status_code==200,r.text
    assert r.json()['prospects_copied']==1
    assert c.post('/sandbox/import-client',headers=csrf).json()['deduplicated']
    cat=c.get('/api/intelligence/three-dots/catalog');assert cat.status_code==200,cat.text
    assert cat.json()['contacts'][0]['id']=='real-id-preserved'
    assert c.get('/api/intelligence/control/catalog').status_code==403
    assert all('MUST_NOT_COPY' not in value for value in snapshots.values())
    assert c.get('/campaigns').status_code==200
    assert c.post('/api/onboarding-sources/OTHER/research-jobs',json={},headers=csrf).status_code==404
    store=real.main.SheetStore()
    source=store.get_onboarding_source(real.SOURCE_ID)
    real.main._run_onboarding_research(source,store,adjustments={'exclusions':'Conservar exclusión del cliente'})
    assert captured[0]['already_in_crm']==[{'company':'Empresa real de prueba','website':''}]
    assert captured[0]['exclusions'].startswith('Conservar exclusión del cliente')
    assert c.post('/api/intelligence/three-dots/sync-approved',headers=csrf).status_code==200
'''
    env={k:v for k,v in os.environ.items() if k not in {'GOOGLE_SERVICE_ACCOUNT_JSON','GOOGLE_SHEET_ID','OPENAI_API_KEY','GEMINI_API_KEY'}}
    env.update(FOCUS_SANDBOX='1',SANDBOX_REAL_MODE='1',SANDBOX_STORAGE_SHEET_ID='isolated-test-only',SANDBOX_GOOGLE_SERVICE_ACCOUNT_JSON='{}',SANDBOX_OPENAI_API_KEY='test-only',SANDBOX_AUTH_SECRET='test-secret-value-of-32-characters',RENDER_EXTERNAL_URL='http://testserver',SANDBOX_DATA_DIR=str(tmp_path))
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr

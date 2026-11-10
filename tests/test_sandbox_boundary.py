import os
import subprocess
import sys


def test_sandbox_runs_real_portal_routes_with_isolated_persistence(tmp_path):
    script = '''
from fastapi.testclient import TestClient
from app.sandbox import app, SandboxStore
with TestClient(app) as c:
    assert c.get('/portal').status_code == 200
    data = c.get('/api/portal-dashboard').json()
    assert len(data['sources']) == 2
    assert len(data['prospects']) == 6
    assert all('fictici' in p['company'] for p in data['prospects'])
    headers={'x-csrf-token':c.cookies['focus_csrf']}
    r=c.post('/api/prospects/sandbox-1-1/status',json={'status':'Aprobado para descarga'},headers=headers)
    assert r.status_code == 200, r.text
    assert SandboxStore().get_prospect('sandbox-1-1')['lead_status']=='Aprobado para descarga'
    assert c.post('/api/onboarding-sources/SANDBOX-1/research-jobs',json={}).status_code==403
    assert c.post('/api/onboarding-sources/SANDBOX-1/automation',json={}).status_code==403
    assert c.post('/sandbox/onboarding',json={'companyName':'Unsafe','email':'real@example.com'}).status_code==422
    assert c.get('/health').json()['external_services'] is False
    assert c.get('/api/intelligence/three-dots/guide').json()['profile']['onboarding_id']=='SANDBOX-1'
    assert c.get('/api/intelligence/control/guide').json()['profile']['onboarding_id']=='SANDBOX-2'
    assert c.get('/api/intelligence/three-dots/guide').json()['ai_ready'] is False
    portal=c.get('/portal').text
    assert 'href="/prospecting-guide"' in portal and 'href="/integrations/ghl"' in portal
    try: SandboxStore()._session()
    except RuntimeError: pass
    else: raise AssertionError('Sheets transport must be unavailable')
'''
    env = {k:v for k,v in os.environ.items() if k not in {'GOOGLE_SERVICE_ACCOUNT_JSON','GOOGLE_SHEET_ID','OPENAI_API_KEY','GEMINI_API_KEY'}}
    env.update(FOCUS_SANDBOX='1',SANDBOX_DATA_DIR=str(tmp_path))
    result = subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr+result.stdout

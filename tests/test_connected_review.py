import os
import subprocess
import sys


def test_connected_review_rejects_production_and_uses_existing_account_scope(tmp_path):
    alternate = "isolated-review-book-00001"
    env = {
        **os.environ,
        "APP_ENV": "production",
        "CENTRAL_AUTH_ENABLED": "true",
        "PUBLIC_BASE_URL": "https://review.focusbusinesslab.es",
        "GOOGLE_SHEETS_ENABLED": "true",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
        "GOOGLE_SHEET_ID": alternate,
        "REVIEW_GOOGLE_SHEET_ID": alternate,
        "REVIEW_DATA_DIR": str(tmp_path),
        "DATABASE_URL": "sqlite://",
        "OPENAI_API_KEY": "",
        "REVIEW_ENABLE_SCHEDULER": "0",
    }
    production = "1FTWbZ1gDpA4RezEz89w9PmbRjwx_2bX2mhzF67V8wiE"
    rejected = subprocess.run([sys.executable, "-c", "import app.connected_review"],
        env={**env, "GOOGLE_SHEET_ID": production, "REVIEW_GOOGLE_SHEET_ID": production},
        capture_output=True, text=True, timeout=20)
    assert rejected.returncode != 0
    assert "Google Sheet separado" in rejected.stderr

    scenario = """
from types import SimpleNamespace
from fastapi.testclient import TestClient
from app import auth
from app import intelligence_adapters

class TestSnapshot:
    def __init__(self, settings, tab, *, create_hidden):
        assert settings.google_sheet_id == 'isolated-review-book-00001'
        assert tab == 'Prospeccion Revision Estado' and create_hidden
    def load(self):
        return None
    def save(self, text):
        pass

intelligence_adapters.SheetsSnapshot = TestSnapshot
from app import connected_review as review
from app.auth import Identity
from app.onboarding import OnboardingSource

def company(rid, email):
    return OnboardingSource.from_sheet_record({
        'ID registro': rid, 'Email responsable': email, 'Empresa': rid, 'Sectores': 'Tecnología',
        'Mercados': 'España', 'Web': f'https://{rid.lower()}.example.test'
    })

class Store:
    data = [company('COMPANY-A', 'a@example.test'), company('COMPANY-B', 'b@example.test')]
    updates = []
    def __init__(self, settings):
        assert settings.google_sheet_id == 'isolated-review-book-00001'
    def get_access(self, email):
        if email == 'revoked@example.test':
            return None
        role = 'Administrador' if email == 'admin@example.test' else 'Cliente'
        return SimpleNamespace(email=email, state='Activo', role=role,
            account_email='a@example.test' if email == 'alias@example.test' else '')
    def onboarding_sources(self, email=None):
        return [source for source in self.data if not email or source.email == email]
    def get_onboarding_source(self, rid, email=None):
        return next((source for source in self.onboarding_sources(email) if source.record_id == rid), None)
    def get_automation_config(self, record_id):
        return None
    def _get(self, range_name):
        assert range_name == "'Onboarding'!A1:ZZ1000"
        return [['ID registro', 'Email responsable', 'Empresa', 'Objetivos'],
                ['COMPANY-A', 'a@example.test', 'COMPANY-A', ''], ['COMPANY-B', 'b@example.test', 'COMPANY-B', '']]
    _column_name = staticmethod(review.SheetStore._column_name)
    def _update(self, a1_range, values, *, raw=False):
        self.updates.append((a1_range, values, raw))

review.SheetStore = Store
actor = None
async def login(*args, **kwargs):
    return actor
auth.verify_central_session = login

with TestClient(review.app, base_url='https://review.focusbusinesslab.es') as client:
    accounts = review.ACCOUNTS_PATH
    assert client.get(accounts).status_code == 401
    assert 'Entrar a la copia' in client.get('/portal').text
    actor = Identity('revoked@example.test', 'Cliente', 'test')
    assert client.get(accounts).status_code == 403
    actor = Identity('alias@example.test', 'Cliente', 'test')
    assert [item['id'] for item in client.get(accounts).json()['accounts']] == ['COMPANY-A']
    assert client.get('/api/intelligence/COMPANY-B/guide').status_code == 404
    assert client.get('/api/intelligence/COMPANY-A/guide').json()['research_available'] is False
    client.get('/portal')
    csrf = {'X-CSRF-Token': client.cookies['focus_csrf']}
    edit = {'field':'Objetivos', 'before':'', 'value':'=Texto literal'}
    assert client.patch('/api/intelligence/COMPANY-B/guide/intake', json=edit, headers=csrf).status_code == 404
    assert client.patch('/api/intelligence/COMPANY-A/guide/intake', json=edit, headers=csrf).status_code == 200
    assert Store.updates == [("'Onboarding'!D2", [['=Texto literal']], True)]
    actor = Identity('admin@example.test', 'Administrador', 'test')
    assert len(client.get(accounts).json()['accounts']) == 2
    portal = client.get('/portal').text
    assert 'data-accounts-url="/api/prospecting-review/accounts"' in portal
    assert 'href="#sources" data-view="sources">Prospección' in portal
    assert 'guide-crm-embedded' in client.get('/portal?crm_embed=1&source=COMPANY-A').text
    assert client.post('/api/internal/onboarding-trigger', json={}).status_code == 404
    assert not review.schedule_enabled
"""
    result = subprocess.run([sys.executable, "-c", scenario], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr

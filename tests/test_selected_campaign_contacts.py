import json
from pathlib import Path

import pytest

from app.intelligence import CampaignEngine, CampaignError
from app.intelligence_contacts import import_approved_selection


@pytest.fixture
def selected_store(tmp_path):
    engine=CampaignEngine(tmp_path/'campaigns.sqlite',Path(__file__).parents[1]/'app/intelligence_seed.json')
    rows=[['execution_id','onboarding_id','company','lead_status','public_contacts_json'],
          ['new-test','ONB-TEST','Empresa verificada','Aprobado para descarga',json.dumps([{'type':'email','value':'contacto@example.test','source_url':'https://example.test/contact'}])],
          ['pending-test','ONB-TEST','Pendiente','Nuevo','[]'],
          ['other-test','ONB-OTHER','Otra cuenta','Aprobado para descarga','[]']]
    return engine,rows


def test_selected_import_is_additive_idempotent_and_preserves_prior_payloads(selected_store):
    engine,rows=selected_store
    with engine.db() as db:
        prior=db.execute('SELECT tenant,id,payload FROM contacts ORDER BY tenant,id').fetchall()
    assert import_approved_selection(engine,rows,'ONB-TEST',['new-test'])==1
    assert import_approved_selection(engine,rows,'ONB-TEST',['new-test','new-test'])==0
    rows[1][2]='Nueva descripción que no debe sobrescribir'
    assert import_approved_selection(engine,rows,'ONB-TEST',['new-test'])==0
    with engine.db() as db:
        new=json.loads(db.execute("SELECT payload FROM contacts WHERE tenant='three-dots' AND id='new-test'").fetchone()[0])
        assert new['company']=='Empresa verificada' and new['approved']
        assert new['email_options'][0]['source_url']=='https://example.test/contact'
        assert {tuple(r) for r in prior}<={tuple(r) for r in db.execute('SELECT tenant,id,payload FROM contacts')}
        assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0


@pytest.mark.parametrize('bad',['pending-test','other-test','missing-test'])
def test_invalid_selection_fails_atomically_without_importing_any_contact(selected_store,bad):
    engine,rows=selected_store
    with pytest.raises(CampaignError):import_approved_selection(engine,rows,'ONB-TEST',['new-test',bad])
    with engine.db() as db:
        assert db.execute("SELECT count(*) FROM contacts WHERE id='new-test'").fetchone()[0]==0

"""Read-only preservation audit; prints counters and hashes, never contacts."""
import getpass,hashlib,json,sqlite3
from collections import Counter
from pathlib import Path
import httpx

token=getpass.getpass('Private Site service credential (hidden): ')
def state(bucket):
    r=httpx.get('https://focus-business-sandbox-20261108.moisses.chatgpt.site/_sandbox_state/'+bucket,
        headers={'OAI-Sites-Authorization':'Bearer '+token},timeout=120,follow_redirects=False)
    r.raise_for_status();data=r.json()
    if hashlib.sha256(data['value'].encode()).hexdigest()!=data['sha256']:raise RuntimeError('Snapshot integrity mismatch')
    return data['value']
baseline=json.loads(state('SourceCopy'))
db=sqlite3.connect(':memory:');db.executescript(state('SandboxCells'))
cells=json.loads(db.execute('SELECT payload FROM sandbox WHERE id=1').fetchone()[0])
preservation={}
for table in ('Prospeccion','Ejecuciones','Revisiones Leads','Busquedas Guardadas'):
    headers=baseline[table][0]
    def rows(data):
        current=data[table];indices=[current[0].index(h) for h in headers]
        return Counter(json.dumps([r[i] if i<len(r) else '' for i in indices],ensure_ascii=False,separators=(',',':')) for r in current[1:])
    expected=rows(baseline);actual=rows(cells)
    missing=sum((expected-actual).values())
    digest=hashlib.sha256('\n'.join(sorted(expected.elements())).encode()).hexdigest()
    preservation[table]={'baseline':len(baseline[table])-1,'current':len(cells[table])-1,'original_rows_missing_or_changed':missing,'baseline_sha256':digest}
email=baseline['Onboarding'][1][baseline['Onboarding'][0].index('Email responsable')].lower()
access=next(r for r in cells['Accesos'][1:] if str(r[0]).lower()==email)
fi=sqlite3.connect(':memory:');fi.executescript(state('FICampaigns'))
campaigns=[]
for cid,payload in fi.execute('SELECT id,payload FROM campaigns'):
    c=json.loads(payload)
    messages=[json.loads(r[0]) for r in fi.execute('SELECT payload FROM messages WHERE campaign=?',(cid,))]
    campaigns.append({'id':cid,'state':c['state'],'members':len(c['contact_ids']),'messages':len(messages),'approved_messages':sum(m.get('approved',False) for m in messages),'versions':[m['version'] for m in messages],
        'tasks_pending':fi.execute("SELECT count(*) FROM tasks WHERE campaign=? AND state='pending'",(cid,)).fetchone()[0],
        'openai_calls_confirmed':fi.execute("SELECT count(*) FROM events WHERE campaign=? AND kind='openai_completed'",(cid,)).fetchone()[0],
        'outbound_emails':0,'input_tokens':sum(m.get('usage',{}).get('input_tokens',0) for m in messages),'output_tokens':sum(m.get('usage',{}).get('output_tokens',0) for m in messages)})
executions=[]
for r in cells['Ejecuciones'][1:]:
    entry=dict(zip(cells['Ejecuciones'][0],r))
    if str(entry.get('created_at','')).startswith('2026-11-08') and entry.get('actor_email')=='sandbox-owner@example.test':
        executions.append({k:entry.get(k) for k in ('execution_id','created_at','status','model','prompt_tokens','output_tokens','total_tokens','web_search_calls','duplicates_discarded')})
result={'preservation':preservation,'sandbox_quota':{'assigned':access[4],'used':access[5],'available':access[6]},'campaigns':campaigns,'research_executions':executions}
print(json.dumps(result,ensure_ascii=False))

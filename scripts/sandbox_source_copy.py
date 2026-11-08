"""Provision the authorized private source copy. Secret read with hidden stdin."""
import getpass
import hashlib
import json
from pathlib import Path
import httpx

data=json.loads(Path('data/source-copy.json').read_text(encoding='utf-8'))
if data['Onboarding'][1][data['Onboarding'][0].index('ID registro')]!='ONB-A23D5DB5':
    raise SystemExit('Wrong account')
value=json.dumps(data,ensure_ascii=False,separators=(',',':'))
digest=hashlib.sha256(value.encode()).hexdigest()
token=getpass.getpass('Private Site service credential (hidden): ')
url='https://focus-business-sandbox-20261108.moisses.chatgpt.site/_sandbox_state/SourceCopy'
headers={'OAI-Sites-Authorization':'Bearer '+token}
with httpx.Client(timeout=120,follow_redirects=False) as client:
    prior=client.get(url,headers=headers)
    if prior.status_code!=200:raise SystemExit('Private storage unavailable: '+str(prior.status_code))
    state=prior.json()
    if state.get('found'):
        if state.get('sha256')!=digest:raise SystemExit('A different source copy exists; no overwrite performed')
        print(json.dumps({'ok':True,'deduplicated':True,'sha256':digest}))
    else:
        response=client.put(url,headers=headers,json={'revision':state['revision'],'value':value})
        if response.status_code!=200:raise SystemExit('Write not confirmed: '+str(response.status_code))
        result=response.json()
        if result.get('sha256')!=digest:raise SystemExit('Integrity not confirmed')
        print(json.dumps({'ok':True,'revision':result['revision'],'sha256':digest,'records':{k:len(v)-1 for k,v in data.items()}}))

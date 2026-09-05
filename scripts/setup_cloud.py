from __future__ import annotations
import json,os,re
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]

def need(k):
    v=os.getenv(k,'').strip()
    if not v: raise SystemExit(f'Brak {k}')
    return v

account=need('CF_ACCOUNT_ID'); dbid=need('CF_D1_DATABASE_ID'); token=need('CF_D1_API_TOKEN')
panel=need('PANEL_URL').rstrip('/'); bot=need('TELEGRAM_BOT_TOKEN'); webhook_secret=need('TELEGRAM_WEBHOOK_SECRET')
if not panel.startswith('https://'): raise SystemExit('PANEL_URL musi zaczynać się od https://')

schema=(ROOT/'schema.sql').read_text(encoding='utf-8')
# Schema contains no semicolons inside strings; simple split is safe here.
stmts=[x.strip() for x in schema.split(';') if x.strip()]
url=f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{dbid}/query'
r=requests.post(url,headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},json={'batch':[{'sql':s,'params':[]} for s in stmts]},timeout=60)
r.raise_for_status(); data=r.json()
if not data.get('success'): raise SystemExit('D1 init failed: '+json.dumps(data,ensure_ascii=False)[:2000])
print(f'[OK] D1 schema: {len(stmts)} statements')

base=f'https://api.telegram.org/bot{bot}/'
def tg(method,payload):
    rr=requests.post(base+method,json=payload,timeout=30); rr.raise_for_status(); d=rr.json()
    if not d.get('ok'): raise SystemExit(f'Telegram {method}: '+str(d))
    return d.get('result')

tg('setWebhook',{
    'url':panel+'/telegram/webhook',
    'secret_token':webhook_secret,
    'allowed_updates':['message','callback_query'],
    'drop_pending_updates':True,
})
tg('setMyCommands',{'commands':[
    {'command':'start','description':'Otwórz Property Radar'},
    {'command':'status','description':'Stan bazy i ostatni skan'},
    {'command':'skanuj','description':'Uruchom skan na GitHub Actions'},
    {'command':'id','description':'Pokaż chat_id / user_id'},
]})
tg('setChatMenuButton',{'menu_button':{'type':'web_app','text':'🏡 Oferty','web_app':{'url':panel}}})
print('[OK] Telegram webhook + komendy + Mini App')
print('Webhook:',panel+'/telegram/webhook')

from __future__ import annotations
import json,os
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
url=f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{dbid}/query'
headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'}

def d1(sql,params=None):
    r=requests.post(url,headers=headers,json={'sql':sql,'params':params or []},timeout=60);r.raise_for_status();d=r.json()
    if not d.get('success'):raise SystemExit('D1: '+json.dumps(d,ensure_ascii=False)[:2000])
    res=d.get('result') or []
    return (res[0].get('results') or []) if res else []

schema=(ROOT/'schema.sql').read_text(encoding='utf-8')
stmts=[x.strip() for x in schema.split(';') if x.strip()]
# On an existing D1 database an index referencing a new v1.2 column would fail before ALTER TABLE.
# Therefore create tables first, migrate the listings table, then create indexes.
table_stmts=[s for s in stmts if not s.lstrip().upper().startswith('CREATE INDEX')]
index_stmts=[s for s in stmts if s.lstrip().upper().startswith('CREATE INDEX')]
if table_stmts:
    r=requests.post(url,headers=headers,json={'batch':[{'sql':s,'params':[]} for s in table_stmts]},timeout=90);r.raise_for_status();data=r.json()
    if not data.get('success'):raise SystemExit('D1 table init failed: '+json.dumps(data,ensure_ascii=False)[:2000])
print(f'[OK] D1 tables/base schema: {len(table_stmts)} statements')

# SQLite/D1 CREATE TABLE IF NOT EXISTS does not add new columns to an old table.
required={
 'updated_text':'TEXT','updated_at':'TEXT','source_status':"TEXT DEFAULT 'active'",'archive_reason':'TEXT',
 'market_mean_comparable':'REAL','rcn_median_ppm':'REAL','rcn_mean_ppm':'REAL','rcn_count':'INTEGER DEFAULT 0',
 'rcn_radius_km':'REAL','rcn_months':'INTEGER','rcn_last_date':'TEXT','rcn_last_ppm':'REAL','rcn_quality':'TEXT',
 'price_alert_reference':'REAL','last_meaningful_price_change_at':'TEXT','last_price_old':'REAL','last_price_new':'REAL','last_price_change_pct':'REAL'
}
cols={r['name'] for r in d1('PRAGMA table_info(listings)')}
for name,typ in required.items():
    if name not in cols:
        d1(f'ALTER TABLE listings ADD COLUMN {name} {typ}')
        print('[MIGRATE] listings +',name)

if index_stmts:
    r=requests.post(url,headers=headers,json={'batch':[{'sql':s,'params':[]} for s in index_stmts]},timeout=90);r.raise_for_status();data=r.json()
    if not data.get('success'):raise SystemExit('D1 index init failed: '+json.dumps(data,ensure_ascii=False)[:2000])
print(f'[OK] D1 indexes: {len(index_stmts)} statements')

base=f'https://api.telegram.org/bot{bot}/'
def tg(method,payload):
    rr=requests.post(base+method,json=payload,timeout=30);rr.raise_for_status();d=rr.json()
    if not d.get('ok'):raise SystemExit(f'Telegram {method}: '+str(d))
    return d.get('result')

tg('setWebhook',{'url':panel+'/telegram/webhook','secret_token':webhook_secret,'allowed_updates':['message','callback_query'],'drop_pending_updates':True})
tg('setMyCommands',{'commands':[
    {'command':'start','description':'Menu Property Radar'},
    {'command':'nowe','description':'Faktycznie nowe ogłoszenia'},
    {'command':'ceny','description':'Ostatnie istotne zmiany cen'},
    {'command':'status','description':'Krótki status bota'},
    {'command':'statuspelny','description':'Pełny status bota i źródeł'},
    {'command':'baza','description':'Status i statystyki bazy'},
    {'command':'id','description':'Pokaż Telegram user_id'},
    {'command':'diag','description':'Diagnostyka (admin)'},
]})
tg('setChatMenuButton',{'menu_button':{'type':'web_app','text':'🏡 Oferty','web_app':{'url':panel}}})
print('[OK] Telegram webhook + uproszczone komendy + Mini App')
print('Webhook:',panel+'/telegram/webhook')

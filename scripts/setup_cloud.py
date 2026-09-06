from __future__ import annotations
import json,os,sys
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]
def need(k):
    v=os.getenv(k,'').strip()
    if not v: raise SystemExit(f'Brak {k}')
    return v

account=need('CF_ACCOUNT_ID'); dbid=need('CF_D1_DATABASE_ID'); token=need('CF_D1_API_TOKEN')
DEFAULT_PANEL_URL='https://property-radar.hoolz.workers.dev'
panel=(os.getenv('PANEL_URL','').strip() or DEFAULT_PANEL_URL).rstrip('/')
# Old quick-tunnel URLs expire and were the reason the Telegram bottom "Oferty" button returned 502.
# This project has a stable Worker URL, so never configure Telegram back to trycloudflare.
if 'trycloudflare.com' in panel.lower():
    print('[WARN] Stary PANEL_URL trycloudflare wykryty — używam stałego Worker URL')
    panel=DEFAULT_PANEL_URL
if not panel.startswith('https://'): raise SystemExit('PANEL_URL musi zaczynać się od https://')
bot=need('TELEGRAM_BOT_TOKEN'); webhook_secret=need('TELEGRAM_WEBHOOK_SECRET')
chat_ids=[int(x) for x in __import__('re').findall(r'-?\d+', os.getenv('TELEGRAM_CHAT_IDS',''))]
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
 'parcel_id':'TEXT','parcel_id_confidence':'TEXT','updated_text':'TEXT','updated_at':'TEXT','source_status':"TEXT DEFAULT 'active'",'archive_reason':'TEXT',
 'market_mean_comparable':'REAL','rcn_median_ppm':'REAL','rcn_mean_ppm':'REAL','rcn_count':'INTEGER DEFAULT 0',
 'rcn_radius_km':'REAL','rcn_months':'INTEGER','rcn_last_date':'TEXT','rcn_last_ppm':'REAL','rcn_quality':'TEXT',
 'rcn_history_count':'INTEGER DEFAULT 0','rcn_history_last_date':'TEXT','rcn_history_last_price':'REAL','rcn_history_last_ppm':'REAL','rcn_history_match':'TEXT',
 'price_alert_reference':'REAL','last_meaningful_price_change_at':'TEXT','last_price_old':'REAL','last_price_new':'REAL','last_price_change_pct':'REAL'
}
cols={r['name'] for r in d1('PRAGMA table_info(listings)')}
for name,typ in required.items():
    if name not in cols:
        d1(f'ALTER TABLE listings ADD COLUMN {name} {typ}')
        print('[MIGRATE] listings +',name)

rcn_required={
 'parcel_id':'TEXT','transaction_id':'TEXT','price_basis':'TEXT'
}
rcn_cols={r['name'] for r in d1('PRAGMA table_info(rcn_transactions)')}
for name,typ in rcn_required.items():
    if name not in rcn_cols:
        d1(f'ALTER TABLE rcn_transactions ADD COLUMN {name} {typ}')
        print('[MIGRATE] rcn_transactions +',name)

if index_stmts:
    r=requests.post(url,headers=headers,json={'batch':[{'sql':s,'params':[]} for s in index_stmts]},timeout=90);r.raise_for_status();data=r.json()
    if not data.get('success'):raise SystemExit('D1 index init failed: '+json.dumps(data,ensure_ascii=False)[:2000])
print(f'[OK] D1 indexes: {len(index_stmts)} statements')

# Automatic one-time cleanup of legacy false positives. This is intentionally
# conservative: only rows with hard textual/admin evidence of a foreign location
# are physically removed. Users never need to paste SQL into D1 manually.
try:
    sys.path.insert(0,str(ROOT/'scraper'))
    from app.area import area_accepts
    from app.classify import classify_category, olx_url_cid, is_rental_offer
    cfg=json.loads((ROOT/'scraper'/'config.json').read_text(encoding='utf-8'))
    area_cfg=cfg.get('area') or {}
    rows=d1('SELECT id,canonical_url,source,title,location,description FROM listings')
    hard_prefixes=('explicit-outside','known-gmina-outside-target','conflicting-locality','unknown-locality-in-target-gmina')
    bad=[]; reasons={}
    for row in rows:
        why=None
        if row.get('source')=='OLX':
            why='olx-rebuild-v147'
        elif is_rental_offer(row.get('title') or '',row.get('description') or '','',row.get('canonical_url') or ''):
            why='rental-offer'
        if why is None:
            ok,_,area_why=area_accepts(row,area_cfg,None)
            if (not ok) and str(area_why or '').startswith(hard_prefixes):
                why=area_why
        if why:
            bad.append(int(row['id']))
            reasons[why]=reasons.get(why,0)+1
    for i in range(0,len(bad),40):
        chunk=bad[i:i+40]; qs=','.join('?' for _ in chunk)
        d1(f'DELETE FROM price_history WHERE listing_id IN ({qs})',chunk)
        d1(f'DELETE FROM listings WHERE id IN ({qs})',chunk)
    maintenance='1.4.7-purge-olx-rent-v1'
    now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    d1("INSERT INTO system_state(key,value,updated_at) VALUES('db_maintenance_version',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",[maintenance,now])
    d1("INSERT INTO system_state(key,value,updated_at) VALUES('db_maintenance_last',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",[json.dumps({'version':maintenance,'checked_rows':len(rows),'deleted_rows':len(bad),'reasons':reasons,'finished_at':now},ensure_ascii=False),now])
    print(f'[OK] D1 auto-clean: checked={len(rows)} deleted={len(bad)} reasons={reasons}')
except Exception as e:
    # Setup should not become unavailable because cleanup diagnostics failed; the
    # scanner repeats the same maintenance automatically on its first run.
    print(f'[WARN] D1 auto-clean deferred to first scan: {type(e).__name__}: {e}')

base=f'https://api.telegram.org/bot{bot}/'
def tg(method,payload,*,fatal=True):
    try:
        rr=requests.post(base+method,json=payload,timeout=30)
    except requests.RequestException as e:
        if fatal:
            raise SystemExit(f'Telegram {method} request failed: {e}')
        print(f'[WARN] Telegram {method} request failed: {e}')
        return None
    try:
        d=rr.json()
    except ValueError:
        d={'ok':False,'description':rr.text[:1000]}
    if rr.status_code >= 400 or not d.get('ok'):
        msg=f'Telegram {method} HTTP {rr.status_code}: {d.get("description") or d}'
        if fatal:
            raise SystemExit(msg)
        print('[WARN]',msg)
        return None
    return d.get('result')

# Hard reset the webhook and pending queue so Telegram cannot keep retrying stale updates.
tg('deleteWebhook',{'drop_pending_updates':True})
tg('setWebhook',{'url':panel+'/telegram/webhook','secret_token':webhook_secret,'allowed_updates':['message','callback_query'],'drop_pending_updates':True})
wh=tg('getWebhookInfo',{})
if str((wh or {}).get('url','')).rstrip('/') != (panel+'/telegram/webhook').rstrip('/'):
    raise SystemExit('Telegram webhook URL mismatch: '+str(wh))
print('[OK] Webhook verified:', wh.get('url'), 'pending=', wh.get('pending_update_count',0))

tg('setMyCommands',{'commands':[
    {'command':'start','description':'Menu Property Radar'},
    {'command':'nowe','description':'Faktycznie nowe ogłoszenia'},
    {'command':'ceny','description':'Ostatnie istotne zmiany cen'},
    {'command':'status','description':'Krótki status bota'},
    {'command':'statuspelny','description':'Pełny status bota i źródeł'},
    {'command':'baza','description':'Status i statystyki bazy'},
    {'command':'skanuj','description':'Uruchom skan teraz (admin)'},
    {'command':'id','description':'Pokaż Telegram user_id'},
    {'command':'diag','description':'Diagnostyka (admin)'},
]})
# Set the global Mini App button directly. A separate MenuButtonDefault reset is
# unnecessary and can be rejected by Telegram in some chat/menu states.
tg('setChatMenuButton',{'menu_button':{'type':'web_app','text':'🏡 Oferty','web_app':{'url':panel}}})

# A per-chat override has priority over the global button. Refresh private notification
# chats directly to web_app; no intermediate `default` reset is required. Invalid/non-private
# chat IDs are warned about instead of aborting the entire cloud setup.
for cid in sorted(set(chat_ids)):
    changed=tg('setChatMenuButton',{'chat_id':cid,'menu_button':{'type':'web_app','text':'🏡 Oferty','web_app':{'url':panel}}},fatal=False)
    if changed is None:
        print(f'[WARN] menu chat {cid}: pomijam per-chat override; global Mini App menu pozostaje aktywne')
        continue
    actual=tg('getChatMenuButton',{'chat_id':cid},fatal=False) or {}
    actual_url=((actual.get('web_app') or {}).get('url') or '')
    print(f'[OK] menu chat {cid}: type={actual.get("type")} url={actual_url}')
    if actual.get('type')!='web_app' or actual_url.rstrip('/')!=panel.rstrip('/'):
        print(f'[WARN] Telegram menu mismatch for chat {cid}: {actual}; global menu nadal ustawione')

print('[OK] Telegram webhook + komendy + Mini App menu')
print('Webhook:',panel+'/telegram/webhook')

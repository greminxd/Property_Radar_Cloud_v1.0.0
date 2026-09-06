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
 'market_mean_comparable':'REAL','price_alert_reference':'REAL','last_meaningful_price_change_at':'TEXT','last_price_old':'REAL','last_price_new':'REAL','last_price_change_pct':'REAL'
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

# RCN was removed from Property Radar v1.4.8. Remove its cache/state automatically.
try:
    d1('DROP TABLE IF EXISTS rcn_transactions')
    d1("DELETE FROM system_state WHERE key IN ('rcn_status','rcn_parser_version')")
    print('[OK] Legacy RCN cache removed')
except Exception as e:
    print(f'[WARN] Legacy RCN cleanup: {type(e).__name__}: {e}')

# Old databases can still carry now-unused RCN columns on listings. Remove them
# best-effort so the existing D1 schema is also market-only after this setup.
def d1_soft(sql):
    try:
        rr=requests.post(url,headers=headers,json={'sql':sql,'params':[]},timeout=60)
        data=rr.json() if rr.content else {}
        return rr.ok and bool(data.get('success'))
    except Exception:
        return False

legacy_cols=['rcn_median_ppm','rcn_mean_ppm','rcn_count','rcn_radius_km','rcn_months','rcn_last_date','rcn_last_ppm','rcn_quality','rcn_history_count','rcn_history_last_date','rcn_history_last_price','rcn_history_last_ppm','rcn_history_match']
removed_cols=0
for col in legacy_cols:
    if col in {x['name'] for x in d1('PRAGMA table_info(listings)')}:
        if d1_soft(f'ALTER TABLE listings DROP COLUMN {col}'):
            removed_cols+=1
print(f'[OK] Legacy RCN listing fields removed: {removed_cols}/{len(legacy_cols)}')

KNOWN_BAD_URLS={
    'https://www.olx.pl/d/oferta/dzialka-budowlana-20km-od-krakowa-CID3-ID1c8sfW.html':'wrong-zakliczyn-myslenice',
    'https://www.olx.pl/d/oferta/powierzchnia-300m2-CID3-ID1c2K6x.html':'rental-wrong-zakliczyn',
    'https://www.olx.pl/d/oferta/nowy-kolowrotek-samolla-ksn-8000-12-1-bb-karpiowy-surfcasting-1-sztuki-CID767-ID1ccuyw.html':'not-property',
    'https://www.olx.pl/d/oferta/nowy-kolowrotek-samolla-ksn-8000-12-1-bb-karpiowy-surfcasting-3-sztuki-CID767-ID1ccupp.html':'not-property',
}
now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
for bad_url,bad_reason in KNOWN_BAD_URLS.items():
    d1('INSERT INTO listing_blacklist(canonical_url,reason,source,title,created_at) VALUES(?,?,?,?,?) ON CONFLICT(canonical_url) DO UPDATE SET reason=excluded.reason',[bad_url,bad_reason,'OLX',None,now])
    ids=[int(x['id']) for x in d1('SELECT id FROM listings WHERE canonical_url=?',[bad_url])]
    if ids:
        qs=','.join('?' for _ in ids); d1(f'DELETE FROM price_history WHERE listing_id IN ({qs})',ids)
    d1('DELETE FROM listings WHERE canonical_url=?',[bad_url])

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
        if row.get('canonical_url') in KNOWN_BAD_URLS:
            why=KNOWN_BAD_URLS[row.get('canonical_url')]
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
    maintenance='1.5.0-sale-only-cleanup-v4'
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

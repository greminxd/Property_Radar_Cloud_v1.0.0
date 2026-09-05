from __future__ import annotations
import asyncio,json,os,sys
from datetime import datetime,timezone
from pathlib import Path
from statistics import median,mean
from playwright.async_api import async_playwright

from app.cloud_db import CloudDB
from app.scraper import Scraper
from app.geocode import Geocoder
from app.area import area_accepts
from app.utils import haversine_km,fingerprint
from app.classify import privacy_score
from app.dates import normalize_published
from app.scoring import enrich_scores
from app.telegram_notify import TelegramNotify,listing_message

ROOT=Path(__file__).resolve().parent
LOGS=ROOT.parent/'logs'; LOGS.mkdir(exist_ok=True)

def need(name):
    v=os.getenv(name,'').strip()
    if not v: raise RuntimeError(f'Brak sekretu/zmiennej {name}')
    return v

def load_cfg(): return json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
def market_unique(rows):
    out={}
    for x in rows:
        key=x.get('fingerprint') or x['canonical_url']
        old=out.get(key)
        score=lambda r:(bool(r.get('phone')),bool(r.get('parcel_number')),bool(r.get('area_locality') or r.get('location')),len(r.get('description') or ''))
        if old is None or score(x)>score(old): out[key]=x
    return list(out.values())

def compute_privacy(rec,body):
    if rec.get('category')!='plot': return None,''
    score,reasons=privacy_score((rec.get('title') or '')+' '+(rec.get('description') or '')+' '+(body or '')[:4500],rec.get('area_m2'))
    return score,'|'.join(reasons)

async def run():
    cfg=load_cfg(); started=datetime.now(timezone.utc).isoformat()
    db=CloudDB(need('CF_ACCOUNT_ID'),need('CF_D1_DATABASE_ID'),need('CF_D1_API_TOKEN')); db.begin_scan()
    database_was_new=db.count()==0
    tg=TelegramNotify(os.getenv('TELEGRAM_BOT_TOKEN'),os.getenv('TELEGRAM_CHAT_ID'),os.getenv('PANEL_URL',''))
    scraper=Scraper(cfg); geocoder=Geocoder(db,cfg['center'])
    all_recs=[]; errs=[]; diagnostics=[]; healthy_sources=[]

    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
        sem=asyncio.Semaphore(max(1,int(cfg['browser'].get('parallel_sources',3))))
        async def scan_one(source):
            async with sem:
                print(f"[SCAN] {source['name']}...")
                try:
                    recs,source_errors,diag=await scraper.collect_source(browser,source)
                    return source,recs,source_errors,diag
                except Exception as e:
                    return source,[],[f"{source['name']}: {type(e).__name__}: {e}"],{'source':source['name'],'healthy':False,'fatal':f'{type(e).__name__}: {e}'}
        enabled=[x for x in cfg['sources'] if x.get('enabled',True)]
        results=await asyncio.gather(*(scan_one(x) for x in enabled))
        for source,recs,source_errors,diag in results:
            diagnostics.append(diag); all_recs.extend(recs); errs.extend(source_errors)
            if diag.get('healthy'): healthy_sources.append(source['name'])
            print(f"       {source['name']}: {len(recs)} rekordów | linki {diag.get('discovered_links',0)} | detail {diag.get('detail_pages_ok',0)} | {'OK' if diag.get('healthy') else 'NIEPEWNY'}")
        await browser.close()

    accepted=[]; rejected=[]; area_cfg=cfg['area']
    for r in all_recs:
        jsonld=r.pop('_jsonld',[]); body=r.pop('_body','')
        fold=body.lower().replace('ł','l')
        if 'gmina siepraw' in fold or 'powiat myslenicki' in fold:
            rejected.append({'url':r.get('canonical_url'),'reason':'wrong Zakliczyn (Siepraw/Myślenice)'})
            continue
        coords=geocoder.from_jsonld(jsonld)
        if not coords and r.get('location'):
            loc=r['location'].strip(); query=(loc+', gmina Zakliczyn, powiat tarnowski') if loc.lower()=='zakliczyn' else (loc+', powiat tarnowski')
            coords=geocoder.geocode(query)
        if coords:
            r['lat'],r['lon']=coords; r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],coords[0],coords[1])
        else: r['lat']=r['lon']=r['distance_km']=None
        ok,locality,confidence=area_accepts(r,area_cfg,r.get('distance_km'))
        if not ok:
            rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':r.get('distance_km'),'reason':confidence}); continue
        r['area_locality']=locality or r.get('location') or None; r['area_confidence']=confidence
        if locality and (not coords or (r.get('location') or '').strip().lower() in {'zakliczyn','gmina zakliczyn'}):
            c2=geocoder.geocode(locality+', gmina Zakliczyn, powiat tarnowski')
            if c2:
                r['lat'],r['lon']=c2; r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],c2[0],c2[1])
        r['published_at']=normalize_published(r.get('published_text'))
        r['fingerprint']=fingerprint(r.get('title',''),r.get('area_locality') or r.get('location',''),r.get('area_m2'),r.get('price'),r.get('parcel_number'))
        r['privacy_score'],r['privacy_reasons']=compute_privacy(r,body)
        r['deal_label']='liczę po skanie';r['median_comparable']=None;r['comparable_count']=0;r['comparison_quality']=''
        accepted.append(r)

    accepted=list({r['canonical_url']:r for r in accepted}.values())
    changes=db.upsert_many(accepted)
    threshold=int(cfg.get('retention',{}).get('missing_scans_before_inactive',3)); deactivated=db.age_missing_for_healthy_sources(healthy_sources,threshold)
    active=db.all_active(); unique=market_unique(active)
    for r in active: enrich_scores(r,unique,cfg)
    db.update_scores(active)
    active=db.all_active(); unique=market_unique(active); byurl={r['canonical_url']:r for r in active}

    new_count=sum(1 for _,n,_ in changes if n); price_count=sum(1 for _,_,p in changes if p)
    notify=[]
    for rec,is_new,price_changed in changes:
        if not (is_new or price_changed): continue
        r=byurl.get(rec['canonical_url'],rec); notify.append((r,is_new,price_changed))
    if database_was_new:
        notify=sorted(notify,key=lambda t:((t[0].get('privacy_score') or 0),-(t[0].get('price_m2') or 99999)),reverse=True)[:cfg['telegram']['first_run_top_n']]
    for r,is_new,price_changed in notify:
        try: tg.send(listing_message(r,'new' if is_new else 'price'),r.get('canonical_url'))
        except Exception as e: errs.append('Telegram: '+str(e))

    plots=sum(1 for x in unique if x.get('category')=='plot'); garages=sum(1 for x in unique if x.get('category')=='garage')
    deals=sum(1 for x in unique if 'OKAZJA' in (x.get('deal_label') or '')); phones=sum(1 for x in unique if x.get('phone'))
    ppms=[float(x['price_m2']) for x in unique if x.get('category')=='plot' and x.get('price_m2') and 1<=float(x['price_m2'])<=2000]
    market_line=f"mediana {median(ppms):.1f} zł/m² • średnia {mean(ppms):.1f} zł/m²" if ppms else 'brak danych cenowych'
    healthy_count=sum(1 for d in diagnostics if d.get('healthy'))
    summary=(f"🏡 <b>PROPERTY RADAR — SKAN GOTOWY</b>\n"
             f"🟢 Aktywne: {plots} działek • {garages} garaży\n"
             f"🔎 Pobrano: {len(all_recs)} • przyjęto do strefy: {len(accepted)} • źródła OK: {healthy_count}/{len(diagnostics)}\n"
             f"🆕 Nowe: {new_count} • 💸 zmiany ceny: {price_count} • ☎️ telefon: {phones}/{len(unique)}\n"
             f"🔥 Okazje: {deals} • 📈 {market_line}\n"
             f"🚫 Odrzucone: {len(rejected)} • wygaszone po {threshold} brakach: {deactivated}")
    if cfg['telegram'].get('send_summary_even_if_nothing_new',True) or new_count or price_count:
        try: tg.send(summary)
        except Exception as e: errs.append('Telegram summary: '+str(e))

    finished=datetime.now(timezone.utc).isoformat(); status='ok' if healthy_count else 'warning'
    db.record_scan(started_at=started,finished_at=finished,downloaded_records=len(all_recs),accepted_records=len(accepted),active_after_scan=len(unique),new_count=new_count,price_change_count=price_count,rejected_count=len(rejected),deactivated_count=deactivated,healthy_sources=healthy_count,total_sources=len(diagnostics),diagnostics_json=json.dumps(diagnostics,ensure_ascii=False),status=status)
    (LOGS/'scan_diagnostics.json').write_text(json.dumps({'downloaded':len(all_recs),'accepted':len(accepted),'active':len(unique),'sources':diagnostics},ensure_ascii=False,indent=2),encoding='utf-8')
    (LOGS/'rejected_area.json').write_text(json.dumps(rejected,ensure_ascii=False,indent=2),encoding='utf-8')
    (LOGS/'last_errors.txt').write_text('\n'.join(errs),encoding='utf-8')
    print(summary.replace('<b>','').replace('</b>','')); print(f'Błędy/ostrzeżenia: {len(errs)}')
    db.close()

if __name__=='__main__':
    try: asyncio.run(run())
    except KeyboardInterrupt: sys.exit(130)

from __future__ import annotations
import asyncio,json,os,sys
from datetime import datetime,timezone,timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from statistics import median,mean
from playwright.async_api import async_playwright

from app.cloud_db import CloudDB
from app.scraper import Scraper
from app.geocode import Geocoder
from app.area import area_accepts
from app.utils import haversine_km,fingerprint
from app.dates import normalize_published
from app.scoring import enrich_scores
from app.telegram_notify import TelegramNotify,listing_message
from app.rcn import RCNClient

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

def fresh_publication(rec,now,cfg):
    if rec.get('source_status')=='archived' or not rec.get('published_at'): return False
    try:
        d=datetime.fromisoformat(str(rec['published_at']).replace('Z','+00:00'))
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        age=(now-d.astimezone(timezone.utc)).total_seconds()/86400
        return -1 <= age <= float(cfg['telegram'].get('new_alert_max_age_days',3))
    except Exception:return False

def meaningful_price_change(old,new,cfg):
    if old is None or new is None or old<=0:return False
    delta=abs(float(new)-float(old)); pct=delta/float(old)*100
    pcfg=cfg['telegram'].get('price_change_alert',{})
    min_any=float(pcfg.get('minimum_any_pln',1000)); min_abs=float(pcfg.get('min_absolute_pln',5000)); min_pct=float(pcfg.get('min_percent',3.0))
    return delta>=min_any and (delta>=min_abs or pct>=min_pct)

async def run():
    print('[BOOT] Property Radar scan start', flush=True)
    cfg=load_cfg(); started=datetime.now(timezone.utc).isoformat(); now_utc=datetime.now(timezone.utc)
    print('[BOOT] config OK', flush=True)
    print('[BOOT] D1 preflight...', flush=True)
    db=CloudDB(need('CF_ACCOUNT_ID'),need('CF_D1_DATABASE_ID'),need('CF_D1_API_TOKEN')); db.begin_scan()
    db.set_state('scan_status','running'); db.set_state('scan_started_at',started); db.set_state('scan_phase','D1 preflight')
    try:
        database_was_new=db.count()==0
        warsaw=ZoneInfo('Europe/Warsaw'); now_local=datetime.now(warsaw)
        day_start_local=now_local.replace(hour=0,minute=0,second=0,microsecond=0); day_end_local=day_start_local+timedelta(days=1)
        scans_today=db.count_scans_between(day_start_local.astimezone(timezone.utc).isoformat(),day_end_local.astimezone(timezone.utc).isoformat())
        print(f'[BOOT] D1 OK | database_new={database_was_new} | scans_today={scans_today}/2', flush=True)
        if scans_today >= 2:
            print('[LIMIT] Dzienny limit 2 skanów osiągnięty — kończę bez Chromium.', flush=True)
            db.set_state('scan_status','idle'); db.set_state('scan_phase','limit 2/day')
            db.close(); return
    except Exception as e:
        try: db.set_state('scan_status','error');db.set_state('last_error',f'D1 preflight: {type(e).__name__}: {e}')
        except:pass
        print(f'[FATAL] D1 preflight failed: {type(e).__name__}: {e}', flush=True); raise

    tg=TelegramNotify(os.getenv('TELEGRAM_BOT_TOKEN'),os.getenv('TELEGRAM_CHAT_IDS') or os.getenv('TELEGRAM_CHAT_ID'),os.getenv('PANEL_URL',''))
    scraper=Scraper(cfg); geocoder=Geocoder(db,cfg['center'])
    all_recs=[]; errs=[]; diagnostics=[]; healthy_sources=[]

    db.set_state('scan_phase','Chromium + portale')
    print('[BOOT] starting Playwright...', flush=True)
    async with async_playwright() as p:
        print('[BOOT] launching Chromium...', flush=True)
        try: browser=await asyncio.wait_for(p.chromium.launch(headless=True,args=['--disable-dev-shm-usage']), timeout=30)
        except Exception as e:
            db.set_state('scan_status','error');db.set_state('last_error',f'Chromium: {type(e).__name__}: {e}'); raise
        print('[BOOT] Chromium OK', flush=True)
        sem=asyncio.Semaphore(max(1,int(cfg['browser'].get('parallel_sources',3))))
        async def scan_one(source):
            async with sem:
                print(f"[SCAN] {source['name']}...", flush=True)
                try:
                    recs,source_errors,diag=await asyncio.wait_for(scraper.collect_source(browser,source), timeout=float(cfg['browser'].get('source_timeout_s',300)))
                    return source,recs,source_errors,diag
                except asyncio.TimeoutError:
                    return source,[],[f"{source['name']}: timeout całego źródła"],{'source':source['name'],'healthy':False,'fatal':'source timeout'}
                except Exception as e:
                    return source,[],[f"{source['name']}: {type(e).__name__}: {e}"],{'source':source['name'],'healthy':False,'fatal':f'{type(e).__name__}: {e}'}
        enabled=[x for x in cfg['sources'] if x.get('enabled',True)]
        results=await asyncio.gather(*(scan_one(x) for x in enabled))
        for source,recs,source_errors,diag in results:
            diagnostics.append(diag); all_recs.extend(recs); errs.extend(source_errors)
            if diag.get('healthy'): healthy_sources.append(source['name'])
            print(f"       {source['name']}: {len(recs)} rekordów | linki {diag.get('discovered_links',0)} | detail {diag.get('detail_pages_ok',0)} | {'OK' if diag.get('healthy') else 'NIEPEWNY'}", flush=True)
        await browser.close()

    db.set_state('scan_phase','normalizacja + lokalizacja')
    accepted=[]; rejected=[]; area_cfg=cfg['area']
    for r in all_recs:
        jsonld=r.pop('_jsonld',[]); body=r.pop('_body','')
        fold=body.lower().replace('ł','l')
        if 'gmina siepraw' in fold or 'powiat myslenicki' in fold:
            rejected.append({'url':r.get('canonical_url'),'reason':'wrong Zakliczyn (Siepraw/Myślenice)'}); continue
        coords=geocoder.from_jsonld(jsonld)
        if not coords and r.get('location'):
            loc=r['location'].strip(); query=(loc+', gmina Zakliczyn, powiat tarnowski') if loc.lower()=='zakliczyn' else (loc+', powiat tarnowski')
            coords=geocoder.geocode(query)
        if coords:
            r['lat'],r['lon']=coords; r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],coords[0],coords[1])
        else:r['lat']=r['lon']=r['distance_km']=None
        ok,locality,confidence=area_accepts(r,area_cfg,r.get('distance_km'))
        if not ok:
            rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':r.get('distance_km'),'reason':confidence}); continue
        r['area_locality']=locality or r.get('location') or None; r['area_confidence']=confidence
        if locality and (not coords or (r.get('location') or '').strip().lower() in {'zakliczyn','gmina zakliczyn'}):
            c2=geocoder.geocode(locality+', gmina Zakliczyn, powiat tarnowski')
            if c2:r['lat'],r['lon']=c2;r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],c2[0],c2[1])
        r['published_at']=normalize_published(r.get('published_text'))
        r['updated_at']=normalize_published(r.get('updated_text'))
        r['fingerprint']=fingerprint(r.get('title',''),r.get('area_locality') or r.get('location',''),r.get('area_m2'),r.get('price'),r.get('parcel_number'))
        # Text-only privacy scoring was misleading. It stays disabled until parcel/building geometry is available.
        r['privacy_score']=None;r['privacy_reasons']=''
        r['deal_label']='liczę po skanie';r['median_comparable']=None;r['market_mean_comparable']=None;r['comparable_count']=0;r['comparison_quality']=''
        for k in ['rcn_median_ppm','rcn_mean_ppm','rcn_radius_km','rcn_last_date','rcn_last_ppm']:r[k]=None
        r['rcn_count']=0;r['rcn_months']=int(cfg.get('rcn',{}).get('months',24));r['rcn_quality']='brak danych'
        accepted.append(r)

    accepted=list({r['canonical_url']:r for r in accepted}.values())
    changes=db.upsert_many(accepted)
    threshold=int(cfg.get('retention',{}).get('missing_scans_before_inactive',3)); deactivated=db.age_missing_for_healthy_sources(healthy_sources,threshold)

    # Real transaction prices from GUGiK RCN WFS; failures never block listing alerts.
    rcn_rows=[]
    if cfg.get('rcn',{}).get('enabled',True):
        db.set_state('scan_phase','RCN transakcje')
        rcfg=cfg['rcn']; months=int(rcfg.get('months',24))
        cutoff=(datetime.now(timezone.utc)-timedelta(days=months*31+60)).isoformat()
        # RCN is historical data and does not need downloading twice per day. Refresh at most once per local day;
        # otherwise use the D1 cache. This saves requests/writes while keeping the 24-month window current.
        last_rcn=db.query("SELECT MAX(fetched_at) last_fetch FROM rcn_transactions")
        last_fetch=(last_rcn[0].get('last_fetch') if last_rcn else None)
        refresh=True
        if last_fetch:
            try:
                lf=datetime.fromisoformat(str(last_fetch).replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Warsaw'))
                refresh=lf.date()!=datetime.now(ZoneInfo('Europe/Warsaw')).date()
            except Exception: pass
        if refresh:
            try:
                rc=RCNClient(cfg['center']['lat'],cfg['center']['lon'],rcfg)
                print(f"[RCN] odświeżam realne transakcje z ostatnich {months} mies...",flush=True)
                fresh_rows=await asyncio.to_thread(rc.fetch_recent,months,float(rcfg.get('fetch_radius_km',12)),int(rcfg.get('max_features',2500)))
                print(f'[RCN] pobrane transakcje po filtrze: {len(fresh_rows)}',flush=True)
                db.upsert_rcn_transactions(fresh_rows); db.purge_old_rcn(cutoff)
            except Exception as e:
                errs.append('RCN refresh: '+str(e));print(f'[RCN] warning: {type(e).__name__}: {e}; używam cache D1',flush=True)
        else:
            print('[RCN] cache z dzisiaj — bez ponownego pobierania WFS',flush=True)
        try:
            rcn_rows=db.get_rcn_recent(cutoff)
            print(f'[RCN] transakcje użyte z D1: {len(rcn_rows)}',flush=True)
        except Exception as e:
            errs.append('RCN cache: '+str(e));print(f'[RCN] cache error: {e}',flush=True);rcn_rows=[]

    db.set_state('scan_phase','analiza cen')
    active=db.all_active(); unique=market_unique(active)
    rc_an=RCNClient(cfg['center']['lat'],cfg['center']['lon'],cfg.get('rcn',{})) if cfg.get('rcn',{}).get('enabled',True) else None
    for r in active:
        enrich_scores(r,unique,cfg)
        if rc_an and rcn_rows:
            r.update(rc_an.analyze(r,rcn_rows,int(cfg['rcn'].get('months',24)),int(cfg['rcn'].get('minimum_comparables',3))))
    db.update_scores(active)
    active=db.all_active(); unique=market_unique(active); byurl={r['canonical_url']:r for r in active}

    discovered_new=sum(1 for _,n,_,_,_ in changes if n)
    fresh_new=0; meaningful_changes=0; notify=[]
    for rec,is_new,price_changed,old_price,reference_price in changes:
        r=byurl.get(rec['canonical_url'],rec)
        if is_new and fresh_publication(r,now_utc,cfg):
            fresh_new+=1; notify.append((r,'new',None))
        # Price alert uses a persistent reference price, not only the immediately previous tiny edit.
        # Example: 300000 -> 299000 -> 295000 still alerts at 295000 because the cumulative change is 5000.
        ref=reference_price if reference_price is not None else old_price
        if price_changed and r.get('active',1) and meaningful_price_change(ref,r.get('price'),cfg):
            meaningful_changes+=1;db.mark_meaningful_price_change(r['canonical_url'],ref,float(r['price']))
            notify.append((r,'price',ref))
    # First bootstrap is allowed to notify only genuinely fresh publications, never old discoveries.
    for r,kind,old_price in notify:
        try: tg.send(listing_message(r,kind,old_price),r.get('canonical_url'),r.get('image_url'))
        except Exception as e:errs.append('Telegram: '+str(e))

    plots=sum(1 for x in unique if x.get('category')=='plot'); phones=sum(1 for x in unique if x.get('phone'))
    ppms=[float(x['price_m2']) for x in unique if x.get('category')=='plot' and x.get('price_m2') and 1<=float(x['price_m2'])<=5000]
    market_line=f"mediana {median(ppms):.1f} zł/m² • średnia {mean(ppms):.1f} zł/m²" if ppms else 'brak danych cenowych'
    rvals=[float(x['price_m2']) for x in rcn_rows if x.get('price_m2')]
    rcn_line=f"RCN mediana {median(rvals):.1f} • średnia {mean(rvals):.1f} zł/m² ({len(rvals)} trans.)" if rvals else 'RCN: brak/awaria'
    healthy_count=sum(1 for d in diagnostics if d.get('healthy'))
    summary=(f"🏡 <b>PROPERTY RADAR — SKAN GOTOWY</b>\n"
             f"🟢 Aktywne działki: {plots}\n"
             f"🔎 Pobrano: {len(all_recs)} • przyjęto: {len(accepted)} • źródła OK: {healthy_count}/{len(diagnostics)}\n"
             f"🆕 Faktycznie nowe: {fresh_new} • odkryte pierwszy raz: {discovered_new} • 📉 istotne zmiany cen: {meaningful_changes}\n"
             f"📢 {market_line}\n🏛 {rcn_line}\n"
             f"🚫 Odrzucone: {len(rejected)} • wygaszone: {deactivated}")
    if cfg['telegram'].get('send_scan_summary',False):
        try:tg.send(summary)
        except Exception as e:errs.append('Telegram summary: '+str(e))

    finished=datetime.now(timezone.utc).isoformat(); status='ok' if healthy_count>=max(1,len(diagnostics)//2) else 'warning'
    db.record_scan(started_at=started,finished_at=finished,downloaded_records=len(all_recs),accepted_records=len(accepted),active_after_scan=len(unique),new_count=fresh_new,price_change_count=meaningful_changes,rejected_count=len(rejected),deactivated_count=deactivated,healthy_sources=healthy_count,total_sources=len(diagnostics),diagnostics_json=json.dumps(diagnostics,ensure_ascii=False),status=status)
    db.set_state('scan_status','idle');db.set_state('scan_phase','gotowe');db.set_state('last_scan_finished_at',finished);db.set_state('last_error','\n'.join(errs[-8:]) if errs else '')
    (LOGS/'scan_diagnostics.json').write_text(json.dumps({'downloaded':len(all_recs),'accepted':len(accepted),'active':len(unique),'fresh_new':fresh_new,'meaningful_price_changes':meaningful_changes,'rcn_transactions':len(rcn_rows),'sources':diagnostics},ensure_ascii=False,indent=2),encoding='utf-8')
    (LOGS/'rejected_area.json').write_text(json.dumps(rejected,ensure_ascii=False,indent=2),encoding='utf-8')
    (LOGS/'last_errors.txt').write_text('\n'.join(errs),encoding='utf-8')
    print(summary.replace('<b>','').replace('</b>',''));print(f'Błędy/ostrzeżenia: {len(errs)}')
    db.close()

if __name__=='__main__':
    try:asyncio.run(run())
    except KeyboardInterrupt:sys.exit(130)

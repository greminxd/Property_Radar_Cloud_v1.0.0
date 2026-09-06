from __future__ import annotations
import asyncio,copy,json,os,sys,time,traceback
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
from app.rcn import RCNClient, RCN_PARSER_VERSION

ROOT=Path(__file__).resolve().parent
LOGS=ROOT.parent/'logs'; LOGS.mkdir(exist_ok=True)
LISTING_PARSER_VERSION='1.4.2-location-olx-v2'

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
        purged_bad_sprzedajemy=db.purge_invalid_sprzedajemy()
        lpv=db.query("SELECT value FROM system_state WHERE key='listing_parser_version'")
        previous_listing_parser=(lpv[0].get('value') if lpv else None)
        parser_migration=previous_listing_parser!=LISTING_PARSER_VERSION
        print(f'[BOOT] D1 OK | database_new={database_was_new} | scans_today={scans_today} | limit=OFF | purged_bad_sprzedajemy={purged_bad_sprzedajemy} | parser_migration={parser_migration}', flush=True)
    except Exception as e:
        try: db.set_state('scan_status','error');db.set_state('last_error',f'D1 preflight: {type(e).__name__}: {e}')
        except:pass
        print(f'[FATAL] D1 preflight failed: {type(e).__name__}: {e}', flush=True); raise

    tg=TelegramNotify(os.getenv('TELEGRAM_BOT_TOKEN'),os.getenv('TELEGRAM_CHAT_IDS') or os.getenv('TELEGRAM_CHAT_ID'),os.getenv('PANEL_URL',''))
    scraper=Scraper(cfg); geocoder=Geocoder(db,cfg['center'])
    all_recs=[]; accepted=[]; rejected=[]; changes=[]; errs=[]; diagnostics=[]; healthy_sources=[]
    area_cfg=cfg['area']
    enabled=[x for x in cfg['sources'] if x.get('enabled',True)]

    # Parser migration cleanup: immediately deactivate legacy rows that carry explicit
    # evidence of a foreign county/municipality. This removes previously accepted
    # false positives without waiting for the normal three-scan expiry window.
    purged_outside_area=0
    if parser_migration:
        try:
            existing=db.query("SELECT canonical_url,title,location,description FROM listings WHERE active=1")
            bad=[]
            for row in existing:
                ok,_,why=area_accepts(row,area_cfg,None)
                if (not ok) and why.startswith('explicit-outside'):
                    bad.append(row.get('canonical_url'))
            purged_outside_area=db.deactivate_urls(bad,'outside-area')
            if purged_outside_area:
                print(f'[BOOT] location migration: deactivated {purged_outside_area} explicit outside-area rows',flush=True)
        except Exception as e:
            print(f'[BOOT] location migration warning: {type(e).__name__}: {e}',flush=True)

    # Live state consumed by Mini App + Telegram. The scan still runs on GitHub Actions,
    # but the UI receives source-level progress from D1 every few seconds.
    progress={
        'version':1,
        'run_id':os.getenv('GITHUB_RUN_ID') or None,
        'run_url':os.getenv('GITHUB_RUN_URL') or None,
        'trigger':os.getenv('GITHUB_EVENT_NAME') or 'unknown',
        'started_at':started,
        'total_sources':len(enabled),
        'done_sources':0,
        'downloaded_records':0,
        'accepted_records':0,
        'rejected_records':0,
        'sources':[{'name':x['name'],'status':'queued','records':0,'accepted':0,'healthy':None} for x in enabled]
    }
    if progress['run_id']: db.set_state('scan_github_run_id',str(progress['run_id']))
    db.set_state('scan_trigger',str(progress['trigger']))
    db.set_state('scan_progress',progress)
    try: tg.start_progress(progress)
    except Exception as e: errs.append('Telegram live start: '+str(e))

    def progress_source(name):
        return next((x for x in progress['sources'] if x['name']==name),None)

    async def live_state_write(values,label='LIVE state'):
        # Telemetry must never freeze/cancel Playwright. It is best-effort and uses
        # a shorter retry policy than critical listing writes.
        try:
            await asyncio.to_thread(db.set_states,values,2,(5,12))
            return True
        except Exception as e:
            msg=f'{label}: {type(e).__name__}: {e}'
            errs.append(msg); print(f'[D1] warning: {msg}',flush=True)
            return False

    async def publish_progress(phase=None):
        values={'scan_progress':copy.deepcopy(progress)}
        if phase:values['scan_phase']=phase
        await live_state_write(values,'LIVE progress')

    def normalize_batch(records):
        batch_ok=[]; batch_rejected=[]
        for rec0 in records:
            r=dict(rec0)
            jsonld=r.pop('_jsonld',[]); body=r.pop('_body','')
            fold=body.lower().replace('ł','l')
            if 'gmina siepraw' in fold or 'powiat myslenicki' in fold:
                batch_rejected.append({'url':r.get('canonical_url'),'reason':'wrong Zakliczyn (Siepraw/Myślenice)'}); continue
            # Resolve the target area from listing text FIRST. In whitelist mode we
            # deliberately do not let an arbitrary JSON-LD coordinate rescue an unknown
            # locality: portals can embed geo for recommended offers and that caused
            # far-away listings to appear 2 km from Bieśnik.
            text_ok,text_locality,text_confidence=area_accepts(r,area_cfg,None)
            if not text_ok and text_confidence.startswith('explicit-outside'):
                batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':None,'reason':text_confidence}); continue

            coords=None
            locality=text_locality
            confidence=text_confidence
            if locality:
                query=(locality+', gmina Zakliczyn, powiat tarnowski') if locality.lower()=='zakliczyn' else (locality+', gmina Zakliczyn, powiat tarnowski')
                coords=geocoder.geocode(query)
            elif (area_cfg or {}).get('mode')!='locality_whitelist':
                coords=geocoder.from_jsonld(jsonld)
                if not coords and r.get('location'):
                    coords=geocoder.geocode(r['location'])

            if coords:
                r['lat'],r['lon']=coords; r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],coords[0],coords[1])
            else:
                r['lat']=r['lon']=r['distance_km']=None

            if not locality:
                ok,locality,confidence=area_accepts(r,area_cfg,r.get('distance_km'))
                if not ok:
                    batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':r.get('distance_km'),'reason':confidence}); continue
            r['area_locality']=locality or r.get('location') or None; r['area_confidence']=confidence
            r['published_at']=normalize_published(r.get('published_text'))
            r['updated_at']=normalize_published(r.get('updated_text'))
            r['fingerprint']=fingerprint(r.get('title',''),r.get('area_locality') or r.get('location',''),r.get('area_m2'),r.get('price'),r.get('parcel_number'))
            r['privacy_score']=None;r['privacy_reasons']=''
            r['deal_label']='liczę po skanie';r['median_comparable']=None;r['market_mean_comparable']=None;r['comparable_count']=0;r['comparison_quality']=''
            for k in ['rcn_median_ppm','rcn_mean_ppm','rcn_radius_km','rcn_last_date','rcn_last_ppm']:r[k]=None
            r['rcn_count']=0;r['rcn_months']=int(cfg.get('rcn',{}).get('months',24));r['rcn_quality']='brak danych'
            batch_ok.append(r)
        # A source can occasionally expose the same canonical URL more than once.
        return list({r['canonical_url']:r for r in batch_ok}.values()),batch_rejected

    db.set_state('scan_phase','uruchamianie Chromium')
    print('[BOOT] starting Playwright...', flush=True)
    async with async_playwright() as p:
        print('[BOOT] launching Chromium...', flush=True)
        try: browser=await asyncio.wait_for(p.chromium.launch(headless=True,args=['--disable-dev-shm-usage']), timeout=30)
        except Exception as e:
            db.set_state('scan_status','error');db.set_state('last_error',f'Chromium: {type(e).__name__}: {e}'); raise
        print('[BOOT] Chromium OK', flush=True)
        sem=asyncio.Semaphore(max(1,int(cfg['browser'].get('parallel_sources',3))))
        state_lock=asyncio.Lock()

        async def mark_source(name,**fields):
            async with state_lock:
                item=progress_source(name)
                if item:item.update(fields)
                running=[x['name'] for x in progress['sources'] if x.get('status')=='running']
                phase='portale: '+(', '.join(running[:3]) if running else f"{progress['done_sources']}/{progress['total_sources']}")
                snapshot=copy.deepcopy(progress)
            # requests is blocking, so run it off the asyncio/Playwright event loop.
            await live_state_write({'scan_progress':snapshot,'scan_phase':phase},f"LIVE start {name}")

        async def scan_one(source):
            async with sem:
                t0=time.monotonic()
                await mark_source(source['name'],status='running',started_at=datetime.now(timezone.utc).isoformat())
                print(f"[SCAN] {source['name']}...", flush=True)
                try:
                    source_timeout=float(source.get('source_timeout_s',cfg['browser'].get('source_timeout_s',300)))
                    recs,source_errors,diag=await asyncio.wait_for(scraper.collect_source(browser,source), timeout=source_timeout)
                except asyncio.TimeoutError:
                    recs=[];source_errors=[f"{source['name']}: timeout całego źródła"];diag={'source':source['name'],'healthy':False,'fatal':'source timeout'}
                except Exception as e:
                    recs=[];source_errors=[f"{source['name']}: {type(e).__name__}: {e}"];diag={'source':source['name'],'healthy':False,'fatal':f'{type(e).__name__}: {e}'}
                return source,recs,source_errors,diag,time.monotonic()-t0

        tasks=[asyncio.create_task(scan_one(x),name=f"source:{x['name']}") for x in enabled]
        try:
            for fut in asyncio.as_completed(tasks):
                source,recs,source_errors,diag,elapsed=await fut
                all_recs.extend(recs); errs.extend(source_errors)

                # LIVE DATA: geocoding and D1 both use blocking requests. Run the whole
                # normalization/write path in worker threads so other portals keep moving.
                batch_ok,batch_rejected=await asyncio.to_thread(normalize_batch,recs)
                rejected.extend(batch_rejected)
                persisted=batch_ok
                if batch_ok:
                    try:
                        portal_changes=await asyncio.to_thread(db.upsert_many,batch_ok)
                        changes.extend(portal_changes); accepted.extend(batch_ok)
                    except Exception as e:
                        persisted=[]
                        msg=f"{source['name']}: D1 persist {type(e).__name__}: {e}"
                        errs.append(msg); diag['persistence_error']=msg; diag['healthy']=False
                        print(f'[D1] warning: {msg}; skan pozostałych źródeł trwa dalej',flush=True)
                if diag.get('healthy'): healthy_sources.append(source['name'])
                diagnostics.append(diag)

                async with state_lock:
                    item=progress_source(source['name'])
                    if item:item.update({
                        'status':'done','finished_at':datetime.now(timezone.utc).isoformat(),
                        'records':len(recs),'accepted':len(persisted),'healthy':bool(diag.get('healthy')),
                        'elapsed_s':round(elapsed,1),'error':diag.get('fatal') or diag.get('persistence_error')
                    })
                    progress['done_sources']+=1
                    progress['downloaded_records']=len(all_recs)
                    progress['accepted_records']=len(accepted)
                    progress['rejected_records']=len(rejected)
                    snapshot=copy.deepcopy(progress)
                    phase=f"portale {progress['done_sources']}/{progress['total_sources']} • {source['name']}"
                await live_state_write({'scan_progress':snapshot,'scan_phase':phase},f"LIVE done {source['name']}")
                try: await asyncio.to_thread(tg.update_progress,snapshot)
                except Exception as e: errs.append('Telegram live update: '+str(e))
                print(f"       {source['name']}: {len(recs)} rekordów | przyjęto {len(persisted)} | linki {diag.get('discovered_links',0)} | detail {diag.get('detail_pages_ok',0)} | {'OK' if diag.get('healthy') else 'NIEPEWNY'}", flush=True)
        finally:
            # If anything outside a portal fails, do not close Chromium under still-running
            # Playwright tasks. Cancel and retrieve them first to avoid TargetClosedError noise.
            pending=[t for t in tasks if not t.done()]
            for t in pending:t.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)
            try:await browser.close()
            except Exception:pass

    # All portal records are already in D1. From here we only finalize expiry,
    # RCN and market scoring.
    accepted=list({r['canonical_url']:r for r in accepted}.values())
    db.set_state('scan_phase','finalizacja ofert')
    threshold=int(cfg.get('retention',{}).get('missing_scans_before_inactive',3)); deactivated=db.age_missing_for_healthy_sources(healthy_sources,threshold)

    # Real transaction prices from GUGiK RCN WFS; failures never block listing alerts.
    rcn_rows=[]
    if cfg.get('rcn',{}).get('enabled',True):
        db.set_state('scan_phase','RCN transakcje')
        rcfg=cfg['rcn']; months=int(rcfg.get('months',24))
        cutoff=(datetime.now(timezone.utc)-timedelta(days=months*31+60)).isoformat()
        # Parser v3 fixes RCN units/date/price pairing. Invalidate any cache produced by
        # an older parser; otherwise bad historical rows would keep poisoning medians.
        state=db.query("SELECT value FROM system_state WHERE key='rcn_parser_version'")
        current_version=(state[0].get('value') if state else None)
        force_rcn_refresh=current_version!=RCN_PARSER_VERSION
        if force_rcn_refresh:
            print(f'[RCN] parser cache migration {current_version!r} -> {RCN_PARSER_VERSION}; czyszczę stare transakcje',flush=True)
            db.execute('DELETE FROM rcn_transactions')
            db.set_state('rcn_parser_version',RCN_PARSER_VERSION)
        # RCN is historical data; normal refresh remains once per local day, but a
        # parser-version migration always forces a fresh download.
        last_rcn=db.query("SELECT MAX(fetched_at) last_fetch FROM rcn_transactions")
        last_fetch=(last_rcn[0].get('last_fetch') if last_rcn else None)
        refresh=True
        if last_fetch and not force_rcn_refresh:
            try:
                lf=datetime.fromisoformat(str(last_fetch).replace('Z','+00:00')).astimezone(ZoneInfo('Europe/Warsaw'))
                refresh=lf.date()!=datetime.now(ZoneInfo('Europe/Warsaw')).date()
            except Exception: pass
        if force_rcn_refresh: refresh=True
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
        # Never retain a stale/broken RCN score if the current scan cannot build a
        # trustworthy comparable set.
        for k in ['rcn_median_ppm','rcn_mean_ppm','rcn_radius_km','rcn_last_date','rcn_last_ppm']: r[k]=None
        r['rcn_count']=0; r['rcn_months']=int(cfg.get('rcn',{}).get('months',24)); r['rcn_quality']='brak wiarygodnych porównań'
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
        if price_changed and r.get('active',1):
            if parser_migration:
                # First scan after a parser upgrade establishes a clean reference price.
                # Parser corrections are not real market price changes.
                if r.get('price') is not None: db.reset_price_alert_reference(r['canonical_url'],float(r['price']))
            elif meaningful_price_change(ref,r.get('price'),cfg):
                meaningful_changes+=1;db.mark_meaningful_price_change(r['canonical_url'],ref,float(r['price']))
                notify.append((r,'price',ref))
    # First bootstrap is allowed to notify only genuinely fresh publications, never old discoveries.
    for r,kind,old_price in notify:
        try: tg.send(listing_message(r,kind,old_price),r.get('canonical_url'),r.get('image_url'))
        except Exception as e:errs.append('Telegram: '+str(e))

    plots=sum(1 for x in unique if x.get('category')=='plot'); phones=sum(1 for x in unique if x.get('phone'))
    ppms=[float(x['price_m2']) for x in unique if x.get('category')=='plot' and x.get('price_m2') and 1<=float(x['price_m2'])<=5000]
    market_line=f"mediana {median(ppms):.1f} zł/m² • średnia {mean(ppms):.1f} zł/m²" if ppms else 'brak danych cenowych'
    rvals=[float(x['price_m2']) for x in rcn_rows if x.get('price_m2') and 0.5<=float(x['price_m2'])<=3000]
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
    db.set_state('listing_parser_version',LISTING_PARSER_VERSION)
    progress['finished_at']=finished;progress['status']='done';progress['done_sources']=progress['total_sources']
    progress['downloaded_records']=len(all_recs);progress['accepted_records']=len(accepted);progress['rejected_records']=len(rejected)
    db.set_state('scan_progress',progress)
    try: tg.update_progress(progress,final=True)
    except Exception as e: errs.append('Telegram live final: '+str(e))
    db.set_state('scan_github_run_id','')
    db.set_state('scan_status','idle');db.set_state('scan_phase','gotowe');db.set_state('last_scan_finished_at',finished);db.set_state('last_error','\n'.join(errs[-8:]) if errs else '')
    (LOGS/'scan_diagnostics.json').write_text(json.dumps({'downloaded':len(all_recs),'accepted':len(accepted),'active':len(unique),'fresh_new':fresh_new,'meaningful_price_changes':meaningful_changes,'rcn_transactions':len(rcn_rows),'sources':diagnostics},ensure_ascii=False,indent=2),encoding='utf-8')
    (LOGS/'rejected_area.json').write_text(json.dumps(rejected,ensure_ascii=False,indent=2),encoding='utf-8')
    (LOGS/'last_errors.txt').write_text('\n'.join(errs),encoding='utf-8')
    print(summary.replace('<b>','').replace('</b>',''));print(f'Błędy/ostrzeżenia: {len(errs)}')
    db.close()

if __name__=='__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        # Always leave an artifact for GitHub Actions, even when failure happens before
        # normal scan_diagnostics.json is produced.
        try:
            LOGS.mkdir(exist_ok=True)
            text=f"{datetime.now(timezone.utc).isoformat()} | {type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            (LOGS/'fatal_error.txt').write_text(text,encoding='utf-8')
        except Exception:
            pass
        print(f'[FATAL] {type(e).__name__}: {e}',flush=True)
        raise

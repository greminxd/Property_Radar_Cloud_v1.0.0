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
from app.area import area_accepts, target_region_accepts
from app.classify import classify_category, olx_url_cid, is_rental_offer
from app.utils import haversine_km,fingerprint,asciifold
from app.dates import normalize_published
from app.scoring import enrich_scores
from app.telegram_notify import TelegramNotify,listing_message
from app.egib import EGIBResolver

ROOT=Path(__file__).resolve().parent
LOGS=ROOT.parent/'logs'; LOGS.mkdir(exist_ok=True)
LISTING_PARSER_VERSION='1.5.3-region-category-validation-v1'
DB_MAINTENANCE_VERSION='1.5.3-region-category-cleanup-v1'
KNOWN_BAD_URLS={
    'https://www.olx.pl/d/oferta/dzialka-budowlana-20km-od-krakowa-CID3-ID1c8sfW.html':'wrong-zakliczyn-myslenice',
    'https://www.olx.pl/d/oferta/powierzchnia-300m2-CID3-ID1c2K6x.html':'rental-wrong-zakliczyn',
    'https://www.olx.pl/d/oferta/nowy-kolowrotek-samolla-ksn-8000-12-1-bb-karpiowy-surfcasting-1-sztuki-CID767-ID1ccuyw.html':'not-property',
    'https://www.olx.pl/d/oferta/nowy-kolowrotek-samolla-ksn-8000-12-1-bb-karpiowy-surfcasting-3-sztuki-CID767-ID1ccupp.html':'not-property',
    'https://www.olx.pl/d/oferta/3-pokoje-50-79-m-balkon-6-16-m2-przetronne-CID3-ID1caVYu.html':'not-plot-wroblowice-dolnoslaskie',
    'https://www.olx.pl/d/oferta/41-29-m-czystej-funkcjonalnosci-2-pok-41-29-m-balkon-6-16m-CID3-ID1caVYm.html':'not-plot-wroblowice-dolnoslaskie',
    'https://www.olx.pl/d/oferta/sprzedam-dzialke-budowlana-olszyny-k-szczytna-12-100-CID3-ID1c86Zt.html':'outside-area-olszyny-warminsko-mazurskie',
    'https://www.olx.pl/d/oferta/2-pokoje-41-29-m-balkon-6-16-m2-deweloperskie-blisko-wro-CID3-ID1caVYn.html':'not-plot-wroblowice-dolnoslaskie',
}

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
        mv=db.query("SELECT value FROM system_state WHERE key='db_maintenance_version'")
        previous_maintenance=(mv[0].get('value') if mv else None)
        maintenance_needed=previous_maintenance!=DB_MAINTENANCE_VERSION
        print(f'[BOOT] D1 OK | database_new={database_was_new} | scans_today={scans_today} | limit=OFF | purged_bad_sprzedajemy={purged_bad_sprzedajemy} | parser_migration={parser_migration} | db_maintenance={maintenance_needed}', flush=True)
    except Exception as e:
        try: db.set_state('scan_status','error');db.set_state('last_error',f'D1 preflight: {type(e).__name__}: {e}')
        except:pass
        print(f'[FATAL] D1 preflight failed: {type(e).__name__}: {e}', flush=True); raise

    # Make the deny-list self-healing even if a scan is run before Setup Cloud.
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS listing_blacklist (canonical_url TEXT PRIMARY KEY, reason TEXT, source TEXT, title TEXT, created_at TEXT NOT NULL)""")
    except Exception as e:
        print(f'[WARN] blacklist table init: {type(e).__name__}: {e}',flush=True)

    # Seed confirmed production false positives. They are deleted now and cannot return.
    try:
        for bad_url,reason in KNOWN_BAD_URLS.items():
            db.blacklist_url(bad_url,reason,'OLX',None)
    except Exception as e:
        print(f'[WARN] blacklist seed: {type(e).__name__}: {e}',flush=True)

    tg=TelegramNotify(os.getenv('TELEGRAM_BOT_TOKEN'),os.getenv('TELEGRAM_CHAT_IDS') or os.getenv('TELEGRAM_CHAT_ID'),os.getenv('PANEL_URL',''))
    scraper=Scraper(cfg); geocoder=Geocoder(db,cfg['center']); egib=EGIBResolver(db,cfg['center'])
    all_recs=[]; accepted=[]; rejected=[]; changes=[]; errs=[]; diagnostics=[]; healthy_sources=[]
    area_cfg=cfg['area']
    enabled=[x for x in cfg['sources'] if x.get('enabled',True)]

    # Automatic DB maintenance. No manual SQL is required after an upgrade.
    # We only physically delete legacy rows with HARD textual/admin evidence that they
    # are outside the configured area. Unresolved rows are left alone because some
    # legitimate listings are accepted later using coordinates/distance fallback.
    purged_outside_area=0
    if maintenance_needed or parser_migration:
        try:
            for bad_url,bad_reason in KNOWN_BAD_URLS.items():
                try: db.block_url(bad_url,bad_reason)
                except Exception as e: print(f'[BOOT] blocklist seed warning {bad_url}: {e}',flush=True)
            existing=db.query("SELECT canonical_url,source,title,location,description,distance_km FROM listings")
            bad=[]; reasons={}
            hard_prefixes=('explicit-outside','known-gmina-outside-target','conflicting-locality','unknown-locality-in-target-gmina','outside-radius')
            for row in existing:
                why=None
                if row.get('canonical_url') in KNOWN_BAD_URLS:
                    why=KNOWN_BAD_URLS[row.get('canonical_url')]
                elif is_rental_offer(row.get('title') or '',row.get('description') or '','',row.get('canonical_url') or ''):
                    why='rental-offer'
                elif classify_category(row.get('title') or '',row.get('canonical_url') or '',row.get('description') or '',category_hint=None)!='plot':
                    why='not-plot-reclassified'
                if why is None:
                    region_ok,region_name,region_why=target_region_accepts(row.get('location'),None,row.get('description') or '')
                    if not region_ok:
                        why=f'{region_why}:{region_name}'
                if why is None:
                    ok,_,area_why=area_accepts(row,area_cfg,row.get('distance_km'))
                    if (not ok) and str(area_why or '').startswith(hard_prefixes):
                        why=area_why
                if why:
                    bad.append(row.get('canonical_url'))
                    reasons[why]=reasons.get(why,0)+1
            purged_outside_area=db.delete_urls(bad)
            db.set_states({
                'db_maintenance_version':DB_MAINTENANCE_VERSION,
                'db_maintenance_last':{
                    'version':DB_MAINTENANCE_VERSION,
                    'checked_rows':len(existing),
                    'deleted_rows':purged_outside_area,
                    'reasons':reasons,
                    'finished_at':datetime.now(timezone.utc).isoformat(),
                }
            })
            print(f'[BOOT] DB maintenance: checked={len(existing)} deleted={purged_outside_area} reasons={reasons}',flush=True)
        except Exception as e:
            print(f'[BOOT] DB maintenance warning: {type(e).__name__}: {e}',flush=True)

    blocked_urls=db.blocked_urls()

    # Live state consumed by Mini App + Telegram. The scan still runs on GitHub Actions,
    # but the UI receives source-level progress from D1 every few seconds.
    progress={
        'version':2,
        'run_id':os.getenv('GITHUB_RUN_ID') or None,
        'run_url':os.getenv('GITHUB_RUN_URL') or None,
        'trigger':os.getenv('GITHUB_EVENT_NAME') or 'unknown',
        'started_at':started,
        'heartbeat_at':started,
        'elapsed_s':0,
        'running_sources':[],
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
        blocked=set(db.blacklist_map().keys())
        for rec0 in records:
            r=dict(rec0)
            if r.get('canonical_url') in blocked:
                batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'reason':'blacklisted'}); continue
            jsonld=r.pop('_jsonld',[]); body=r.pop('_body','')
            fold=asciifold(body)
            if is_rental_offer(r.get('title') or '',r.get('description') or body,'',r.get('canonical_url') or ''):
                batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'reason':'rental-offer'}); continue
            if 'gmina siepraw' in fold or 'powiat myslenicki' in fold:
                batch_rejected.append({'url':r.get('canonical_url'),'reason':'wrong Zakliczyn (Siepraw/Myślenice)'}); continue
            area_mode=(area_cfg or {}).get('mode','locality_whitelist')
            radius_mode=area_mode=='radius_verified'
            scope_fold=asciifold(' '.join([str(r.get('title') or ''),str(r.get('location') or ''),str(r.get('description') or ''),body[:4000]]))
            if 'zakliczyn' in scope_fold and any(x in scope_fold for x in ('powiat myslenick','gmina siepraw','kolo myslenic','okolice myslenic')):
                batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'reason':'wrong Zakliczyn (Siepraw/Myślenice)'}); continue

            # v1.5.3: validate province BEFORE any same-name geocoding. An explicit
            # Dolnośląskie/Warmińsko-mazurskie/etc. is decisive and cannot be rescued
            # by finding a Wróblowice/Olszyny namesake near Zakliczyn.
            structured_region=r.get('_olx_region') or r.get('_structured_region') or ''
            region_ok,region_name,region_reason=target_region_accepts(r.get('location'),structured_region,body)
            if not region_ok:
                batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'region':region_name,'reason':region_reason}); continue
            if region_name:
                r['area_confidence_region']=region_name

            # OLX Zakliczyn is ambiguous. Structured API coordinates are the strongest
            # evidence and are reused by both legacy whitelist and radius modes.
            olx_coords=None
            if r.get('source')=='OLX':
                olx_lat=r.get('_olx_structured_lat');olx_lon=r.get('_olx_structured_lon')
                if olx_lat is not None and olx_lon is not None:
                    olx_coords=(float(olx_lat),float(olx_lon))
                    olx_d=haversine_km(cfg['center']['lat'],cfg['center']['lon'],olx_coords[0],olx_coords[1])
                    r['lat']=olx_coords[0];r['lon']=olx_coords[1];r['distance_km']=olx_d
                    if not radius_mode and olx_d>max(12.0,float(cfg.get('center',{}).get('radius_km',10.0))):
                        batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':olx_d,'reason':'olx-structured-geo-outside-target'}); continue
                elif asciifold((r.get('location') or '').strip())=='zakliczyn':
                    target_text=asciifold(str(r.get('title') or '')+' '+str(r.get('description') or '')+' '+body)
                    if not any(x in target_text for x in ('powiat tarnowsk','gmina zakliczyn','nad dunajcem','tarnow')):
                        batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'reason':'olx-ambiguous-zakliczyn-no-geo'}); continue
                # A structured OLX city without either province or coordinates is
                # still ambiguous (many Polish villages share names). Only explicit
                # target administrative evidence may rescue it.
                if r.get('_olx_structured_city') and not structured_region and olx_coords is None:
                    target_text=asciifold(str(r.get('title') or '')+' '+str(r.get('description') or '')+' '+body)
                    if not any(x in target_text for x in ('powiat tarnowsk','gmina zakliczyn','malopolsk')):
                        batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'reason':'olx-city-without-region-or-geo'}); continue

            if radius_mode:
                # v1.5.2: acceptance is no longer an arbitrary village whitelist. We
                # discover broadly around Zakliczyn/Gromnik/Czchów, resolve the listing
                # locality, and keep it only if it is <= configured radius from Bieśnik.
                coords=olx_coords
                coord_source='olx-api' if coords else None
                if coords is None and (r.get('location') or '').strip():
                    coords=geocoder.geocode(str(r.get('location')),structured_region)
                    if coords: coord_source='location-geocode'
                if coords:
                    r['lat'],r['lon']=coords
                    r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],coords[0],coords[1])
                else:
                    r['lat']=r['lon']=r['distance_km']=None
                ok,_,confidence=area_accepts(r,area_cfg,r.get('distance_km'))
                if not ok:
                    batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':r.get('distance_km'),'reason':confidence}); continue
                r['area_locality']=r.get('location') or None
                r['area_confidence']=f"{confidence}:{coord_source or 'none'}"
            else:
                # Legacy locality-whitelist mode retained for backwards compatibility.
                # Resolve the target area from listing text FIRST. In whitelist mode we
                # deliberately do not let an arbitrary JSON-LD coordinate rescue an unknown
                # locality: portals can embed geo for recommended offers.
                text_ok,text_locality,text_confidence=area_accepts(r,area_cfg,None)
                if not text_ok and text_confidence.startswith('explicit-outside'):
                    batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':None,'reason':text_confidence}); continue

                coords=None
                locality=text_locality
                confidence=text_confidence
                if locality:
                    if olx_coords:
                        coords=olx_coords
                    else:
                        query=locality+', gmina Zakliczyn, powiat tarnowski'
                        coords=geocoder.geocode(query)
                elif (area_cfg or {}).get('mode')!='locality_whitelist':
                    coords=geocoder.from_jsonld(jsonld)
                    if not coords and r.get('location'):
                        coords=geocoder.geocode(r['location'],structured_region)

                if coords:
                    r['lat'],r['lon']=coords; r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],coords[0],coords[1])
                else:
                    r['lat']=r['lon']=r['distance_km']=None

                if not locality:
                    ok,locality,confidence=area_accepts(r,area_cfg,r.get('distance_km'))
                    if not ok:
                        batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':r.get('distance_km'),'reason':confidence}); continue
                r['area_locality']=locality or r.get('location') or None; r['area_confidence']=confidence

            # If the advert exposes a parcel number, resolve it against the official
            # GUGiK EGiB WFS. This gives a stable cadastral id and a real parcel centroid.
            # The village-centre geocode remains only a fallback.
            r['parcel_id']=None; r['parcel_id_confidence']=None
            if r.get('parcel_number') and r.get('area_locality'):
                try:
                    parcel=egib.lookup(r['area_locality'],r['parcel_number'])
                    if parcel and parcel.get('parcel_id'):
                        r['parcel_id']=parcel.get('parcel_id');r['parcel_id_confidence']=parcel.get('confidence') or 'egib-exact'
                        if parcel.get('lat') is not None and parcel.get('lon') is not None:
                            r['lat']=float(parcel['lat']);r['lon']=float(parcel['lon'])
                            r['distance_km']=haversine_km(cfg['center']['lat'],cfg['center']['lon'],r['lat'],r['lon'])
                except Exception as e:
                    print(f"[EGIB] lookup warning {r.get('area_locality')} dz. {r.get('parcel_number')}: {type(e).__name__}: {e}",flush=True)
            # Exact cadastral coordinates, when available, are stronger than a village
            # centre. Re-apply the radius after EGiB so edge localities cannot rescue a
            # parcel that is actually outside the 10 km target.
            if radius_mode:
                ok,_,radius_conf=area_accepts(r,area_cfg,r.get('distance_km'))
                if not ok:
                    batch_rejected.append({'url':r.get('canonical_url'),'title':r.get('title'),'location':r.get('location'),'distance_km':r.get('distance_km'),'reason':radius_conf+'-after-egib'}); continue
            r['published_at']=normalize_published(r.get('published_text'))
            r['updated_at']=normalize_published(r.get('updated_text'))
            r['fingerprint']=fingerprint(r.get('title',''),r.get('area_locality') or r.get('location',''),r.get('area_m2'),r.get('price'),r.get('parcel_number'))
            r['privacy_score']=None;r['privacy_reasons']=''
            r['deal_label']='liczę po skanie';r['median_comparable']=None;r['market_mean_comparable']=None;r['comparable_count']=0;r['comparison_quality']=''
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
        heartbeat_stop=asyncio.Event()

        async def heartbeat_loop():
            # A portal can legitimately take tens of seconds before it finishes. Write a
            # small heartbeat even when counters do not change so Mini App can prove that
            # the GitHub runner is alive instead of looking frozen.
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(),timeout=4.0)
                    break
                except asyncio.TimeoutError:
                    pass
                async with state_lock:
                    progress['heartbeat_at']=datetime.now(timezone.utc).isoformat()
                    progress['elapsed_s']=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(started)).total_seconds()))
                    progress['running_sources']=[x['name'] for x in progress['sources'] if x.get('status')=='running']
                    snapshot=copy.deepcopy(progress)
                await live_state_write({'scan_progress':snapshot},'LIVE heartbeat')

        heartbeat_task=asyncio.create_task(heartbeat_loop(),name='scan-heartbeat')

        async def mark_source(name,**fields):
            async with state_lock:
                item=progress_source(name)
                if item:item.update(fields)
                running=[x['name'] for x in progress['sources'] if x.get('status')=='running']
                progress['running_sources']=running
                progress['heartbeat_at']=datetime.now(timezone.utc).isoformat()
                progress['elapsed_s']=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(started)).total_seconds()))
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
                        changes.extend(portal_changes)
                        persisted_urls={x[0].get('canonical_url') for x in portal_changes}
                        persisted=[x for x in batch_ok if x.get('canonical_url') in persisted_urls]
                        accepted.extend(persisted)
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
                    progress['running_sources']=[x['name'] for x in progress['sources'] if x.get('status')=='running']
                    progress['heartbeat_at']=datetime.now(timezone.utc).isoformat()
                    progress['elapsed_s']=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(started)).total_seconds()))
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
            heartbeat_stop.set()
            heartbeat_task.cancel()
            try:await heartbeat_task
            except asyncio.CancelledError:pass

    # All portal records are already in D1. From here we finalize expiry and market scoring.
    accepted=list({r['canonical_url']:r for r in accepted}.values())
    db.set_state('scan_phase','finalizacja ofert')
    threshold=int(cfg.get('retention',{}).get('missing_scans_before_inactive',3)); deactivated=db.age_missing_for_healthy_sources(healthy_sources,threshold)

    # Market scoring is based only on current asking prices per m².
    db.set_state('scan_phase','analiza cen')
    active=db.all_active(); unique=market_unique(active)
    for r in active:
        enrich_scores(r,unique,cfg)
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
                # Price changes stay in the database/history and can be opened manually,
                # but production Telegram alerts are new-listing-only by default.
                if cfg['telegram'].get('notify_price_changes',False):
                    notify.append((r,'price',ref))
    # First bootstrap is allowed to notify only genuinely fresh publications, never old discoveries.
    for r,kind,old_price in notify:
        try: tg.send(listing_message(r,kind,old_price),r.get('canonical_url'),r.get('image_url'))
        except Exception as e:errs.append('Telegram: '+str(e))

    plots=sum(1 for x in unique if x.get('category')=='plot'); phones=sum(1 for x in unique if x.get('phone'))
    ppms=[float(x['price_m2']) for x in unique if x.get('category')=='plot' and x.get('price_m2') and 1<=float(x['price_m2'])<=5000]
    market_line=f"mediana {median(ppms):.1f} zł/m² • średnia {mean(ppms):.1f} zł/m²" if ppms else 'brak danych cenowych'
    healthy_count=sum(1 for d in diagnostics if d.get('healthy'))
    summary=(f"🏡 <b>PROPERTY RADAR — SKAN GOTOWY</b>\n"
             f"🟢 Aktywne działki: {plots}\n"
             f"🔎 Pobrano: {len(all_recs)} • przyjęto: {len(accepted)} • źródła OK: {healthy_count}/{len(diagnostics)}\n"
             f"🆕 Faktycznie nowe: {fresh_new} • odkryte pierwszy raz: {discovered_new} • 📉 istotne zmiany cen: {meaningful_changes}\n"
             f"📢 {market_line}\n"
             f"🚫 Odrzucone: {len(rejected)} • wygaszone: {deactivated}")
    if cfg['telegram'].get('send_scan_summary',False):
        try:tg.send(summary)
        except Exception as e:errs.append('Telegram summary: '+str(e))

    # Compact location-validation telemetry for Telegram/Mini App diagnostics.
    location_reason_counts={}
    for item in rejected:
        reason=str(item.get('reason') or 'unknown')
        location_reason_counts[reason]=location_reason_counts.get(reason,0)+1
    location_validation={
        'registry_version':'zakliczyn-teryt-2026-09',
        'area_mode':area_cfg.get('mode'),'radius_km':area_cfg.get('fallback_radius_km'),'discovery_localities':list(area_cfg.get('discovery_localities') or []),
        'rejected_total':len(rejected),
        'reasons':location_reason_counts,
    }
    try: db.set_state('location_validation',location_validation)
    except Exception as e: errs.append('location validation state: '+str(e))

    finished=datetime.now(timezone.utc).isoformat(); status='ok' if healthy_count>=max(1,len(diagnostics)//2) else 'warning'
    db.record_scan(started_at=started,finished_at=finished,downloaded_records=len(all_recs),accepted_records=len(accepted),active_after_scan=len(unique),new_count=fresh_new,price_change_count=meaningful_changes,rejected_count=len(rejected),deactivated_count=deactivated,healthy_sources=healthy_count,total_sources=len(diagnostics),diagnostics_json=json.dumps(diagnostics,ensure_ascii=False),status=status)
    db.set_state('listing_parser_version',LISTING_PARSER_VERSION)
    progress['finished_at']=finished;progress['heartbeat_at']=finished;progress['status']='done';progress['done_sources']=progress['total_sources'];progress['running_sources']=[]
    progress['downloaded_records']=len(all_recs);progress['accepted_records']=len(accepted);progress['rejected_records']=len(rejected)
    db.set_state('scan_progress',progress)
    try: tg.update_progress(progress,final=True)
    except Exception as e: errs.append('Telegram live final: '+str(e))
    db.set_state('scan_github_run_id','')
    db.set_state('scan_status','idle');db.set_state('scan_phase','gotowe');db.set_state('last_scan_finished_at',finished);db.set_state('last_error','\n'.join(errs[-8:]) if errs else '')
    (LOGS/'scan_diagnostics.json').write_text(json.dumps({'downloaded':len(all_recs),'accepted':len(accepted),'active':len(unique),'fresh_new':fresh_new,'meaningful_price_changes':meaningful_changes,'location_validation':location_validation,'sources':diagnostics},ensure_ascii=False,indent=2),encoding='utf-8')
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

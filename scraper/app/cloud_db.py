from __future__ import annotations
import json
import random
import threading
import time
from datetime import datetime, timezone
import requests

LISTING_COLUMNS = [
    'canonical_url','source','fingerprint','category','title','price','area_m2','price_m2',
    'plot_type','planning_status','location','lat','lon','distance_km','phone','parcel_number',
    'published_text','published_at','updated_text','updated_at','source_status','archive_reason',
    'area_warning','image_url','location_confidence','area_locality','area_confidence',
    'privacy_score','privacy_reasons','deal_label','median_comparable','market_mean_comparable','comparable_count',
    'comparison_quality','rcn_median_ppm','rcn_mean_ppm','rcn_count','rcn_radius_km','rcn_months',
    'rcn_last_date','rcn_last_ppm','rcn_quality','description'
]

class CloudDB:
    def __init__(self, account_id:str, database_id:str, api_token:str):
        self.url=f'https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query'
        self._headers={'Authorization':f'Bearer {api_token}','Content-Type':'application/json'}
        self._session_lock=threading.RLock()
        self.session=self._new_session()
        self.scan_started=None

    def _new_session(self):
        s=requests.Session(); s.headers.update(self._headers); return s

    def now(self): return datetime.now(timezone.utc).isoformat()
    def begin_scan(self): self.scan_started=self.now()

    @staticmethod
    def _retry_delay(attempt,response=None):
        if response is not None:
            try:
                raw=response.headers.get('Retry-After')
                if raw is not None:return min(15.0,max(0.5,float(raw)))
            except Exception:pass
        base=(1.0,2.5,5.5,9.0)[min(max(0,attempt-1),3)]
        return base + random.uniform(0.0,0.35)

    def _post(self,payload,attempts=4,timeout=None):
        """Call Cloudflare D1 with bounded retries for transient transport/API failures.

        The LIVE scanner writes status frequently. A single Cloudflare read timeout must
        not destroy an otherwise healthy scan. Requests are serialized because v1.4
        moves D1 work to worker threads and requests.Session is not shared concurrently.
        """
        attempts=max(1,int(attempts or 1)); timeout=timeout or (8,35)
        transient_status={408,425,429,500,502,503,504,520,521,522,523,524}
        last_error=None
        for attempt in range(1,attempts+1):
            response=None
            try:
                with self._session_lock:
                    response=self.session.post(self.url,json=payload,timeout=timeout)
                if response.status_code in transient_status:
                    if attempt>=attempts:
                        response.raise_for_status()
                    delay=self._retry_delay(attempt,response)
                    print(f'[D1] transient HTTP {response.status_code}; retry {attempt}/{attempts} in {delay:.1f}s',flush=True)
                    time.sleep(delay); continue
                response.raise_for_status(); data=response.json()
                if not data.get('success',False):
                    raise RuntimeError(json.dumps(data.get('errors') or data,ensure_ascii=False)[:1200])
                return data
            except (requests.Timeout,requests.ConnectionError) as e:
                last_error=e
                with self._session_lock:
                    try:self.session.close()
                    except Exception:pass
                    self.session=self._new_session()
                if attempt>=attempts:raise
                delay=self._retry_delay(attempt,response)
                print(f'[D1] {type(e).__name__}; retry {attempt}/{attempts} in {delay:.1f}s',flush=True)
                time.sleep(delay)
            except requests.HTTPError as e:
                last_error=e
                status=getattr(getattr(e,'response',None),'status_code',None)
                if status not in transient_status or attempt>=attempts:raise
                delay=self._retry_delay(attempt,getattr(e,'response',None))
                print(f'[D1] HTTP {status}; retry {attempt}/{attempts} in {delay:.1f}s',flush=True)
                time.sleep(delay)
        if last_error:raise last_error
        raise RuntimeError('D1 request failed without a response')

    @staticmethod
    def _result_items(data):
        x=data.get('result') or []
        return x if isinstance(x,list) else [x]

    def query(self,sql,params=None):
        data=self._post({'sql':sql,'params':params or []})
        items=self._result_items(data)
        if not items: return []
        return items[0].get('results') or []

    def execute(self,sql,params=None,attempts=4,timeout=None): self._post({'sql':sql,'params':params or []},attempts=attempts,timeout=timeout)

    def batch(self,statements,chunk=28,attempts=4,timeout=None):
        stmts=[{'sql':s,'params':p} for s,p in statements]
        for i in range(0,len(stmts),chunk):
            if stmts[i:i+chunk]: self._post({'batch':stmts[i:i+chunk]},attempts=attempts,timeout=timeout)

    def count(self):
        rows=self.query('SELECT COUNT(*) c FROM listings')
        return int(rows[0]['c']) if rows else 0


    def purge_invalid_sprzedajemy(self):
        """Delete legacy parser artifacts that are category/search pages, not offers."""
        rows=self.query("SELECT id FROM listings WHERE source='Sprzedajemy' AND canonical_url NOT GLOB '*-nr[0-9]*'")
        ids=[int(r['id']) for r in rows if r.get('id') is not None]
        if not ids:return 0
        for i in range(0,len(ids),50):
            chunk=ids[i:i+50]; qs=','.join('?' for _ in chunk)
            self.execute(f'DELETE FROM price_history WHERE listing_id IN ({qs})',chunk)
            self.execute(f'DELETE FROM listings WHERE id IN ({qs})',chunk)
        return len(ids)

    def deactivate_urls(self,urls,reason='outside-area'):
        urls=[u for u in dict.fromkeys(urls or []) if u]
        if not urls:return 0
        changed=0
        for i in range(0,len(urls),50):
            chunk=urls[i:i+50]; qs=','.join('?' for _ in chunk)
            before=self.query(f'SELECT COUNT(*) c FROM listings WHERE active=1 AND canonical_url IN ({qs})',chunk)
            n=int(before[0]['c']) if before else 0
            self.execute(f"UPDATE listings SET active=0,source_status=?,archive_reason=? WHERE canonical_url IN ({qs})",[reason,reason]+chunk)
            changed+=n
        return changed

    def count_scans_between(self,start_iso,end_iso):
        rows=self.query('SELECT COUNT(*) c FROM scan_runs WHERE finished_at>=? AND finished_at<?',[start_iso,end_iso])
        return int(rows[0]['c']) if rows else 0

    def set_states(self,values,attempts=4,timeout=None):
        if not values:return
        now=self.now(); stmts=[]
        sql='INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at'
        for key,value in values.items():
            if not isinstance(value,str):value=json.dumps(value,ensure_ascii=False)
            stmts.append((sql,[key,value,now]))
        self.batch(stmts,attempts=attempts,timeout=timeout)

    def set_state(self,key,value,attempts=4,timeout=None):
        self.set_states({key:value},attempts=attempts,timeout=timeout)

    def existing_map(self):
        return {r['canonical_url']:r for r in self.query('SELECT id,canonical_url,price,price_alert_reference,active,source_status,published_at FROM listings')}

    def upsert_many(self,records):
        now=self.now(); old=self.existing_map(); changes=[]; stmts=[]
        update_cols=[c for c in LISTING_COLUMNS if c!='canonical_url']
        placeholders=','.join('?' for _ in LISTING_COLUMNS)
        update_sql=','.join(f'{c}=excluded.{c}' for c in update_cols)
        sql=f'''INSERT INTO listings ({','.join(LISTING_COLUMNS)},price_alert_reference,first_seen,last_seen,active,missing_scans)
                VALUES ({placeholders},?,?,?,?,0)
                ON CONFLICT(canonical_url) DO UPDATE SET {update_sql},price_alert_reference=COALESCE(listings.price_alert_reference,listings.price),last_seen=excluded.last_seen,active=excluded.active,missing_scans=0'''
        for rec in records:
            prev=old.get(rec['canonical_url'])
            is_new=prev is None
            old_price=float(prev['price']) if prev and prev.get('price') is not None else None
            new_price=float(rec['price']) if rec.get('price') is not None else None
            price_changed=bool(prev and old_price is not None and new_price is not None and abs(old_price-new_price) >= 0.01)
            first_seen=now
            row_active=0 if rec.get('source_status')=='archived' else 1
            reference_price=float(prev.get('price_alert_reference')) if prev and prev.get('price_alert_reference') is not None else old_price
            params=[rec.get(c) for c in LISTING_COLUMNS]+[new_price,first_seen,now,row_active]
            stmts.append((sql,params))
            if new_price is not None and (is_new or price_changed):
                # Idempotent insert: a transport timeout may happen after D1 committed
                # the request, so a retry must not duplicate the same history point.
                stmts.append(("""INSERT INTO price_history(listing_id,seen_at,price)
                    SELECT l.id,?,? FROM listings l WHERE l.canonical_url=?
                    AND NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.listing_id=l.id AND ph.seen_at=? AND ph.price=?)""",
                    [now,new_price,rec['canonical_url'],now,new_price]))
            changes.append((rec,is_new,price_changed,old_price,reference_price))
        self.batch(stmts)
        return changes

    def reset_price_alert_reference(self,url,new_price):
        self.execute('UPDATE listings SET price_alert_reference=? WHERE canonical_url=?',[new_price,url])

    def mark_meaningful_price_change(self,url,old_price,new_price,at=None):
        at=at or self.now()
        pct=((new_price-old_price)/old_price*100.0) if old_price else None
        self.execute('UPDATE listings SET price_alert_reference=?,last_meaningful_price_change_at=?,last_price_old=?,last_price_new=?,last_price_change_pct=? WHERE canonical_url=?',[new_price,at,old_price,new_price,pct,url])

    def age_missing_for_healthy_sources(self,sources,threshold=3):
        if not sources: return 0
        threshold=max(2,int(threshold or 3)); before=self.query('SELECT COUNT(*) c FROM listings WHERE active=0')[0]['c']
        stmts=[]
        for src in dict.fromkeys(sources):
            stmts.append(("UPDATE listings SET missing_scans=COALESCE(missing_scans,0)+1 WHERE source=? AND last_seen < ? AND active=1",[src,self.scan_started]))
            stmts.append(("UPDATE listings SET active=0, source_status=CASE WHEN source_status='active' THEN 'missing' ELSE source_status END WHERE source=? AND active=1 AND COALESCE(missing_scans,0)>=?",[src,threshold]))
        self.batch(stmts)
        after=self.query('SELECT COUNT(*) c FROM listings WHERE active=0')[0]['c']
        return max(0,int(after)-int(before))

    def all_active(self): return self.query("SELECT * FROM listings WHERE active=1 AND COALESCE(source_status,'active')<>'archived'")

    def update_scores(self,rows):
        stmts=[]
        for r in rows:
            stmts.append(("""UPDATE listings SET privacy_score=NULL,privacy_reasons='',deal_label=?,median_comparable=?,market_mean_comparable=?,comparable_count=?,comparison_quality=?,
                rcn_median_ppm=?,rcn_mean_ppm=?,rcn_count=?,rcn_radius_km=?,rcn_months=?,rcn_last_date=?,rcn_last_ppm=?,rcn_quality=? WHERE id=?""",[
                r.get('deal_label'),r.get('median_comparable'),r.get('market_mean_comparable'),r.get('comparable_count',0),r.get('comparison_quality'),
                r.get('rcn_median_ppm'),r.get('rcn_mean_ppm'),r.get('rcn_count',0),r.get('rcn_radius_km'),r.get('rcn_months'),
                r.get('rcn_last_date'),r.get('rcn_last_ppm'),r.get('rcn_quality'),r['id']]))
        self.batch(stmts)

    def upsert_rcn_transactions(self,rows):
        if not rows: return
        now=self.now(); stmts=[]
        sql='''INSERT INTO rcn_transactions(tx_key,transaction_date,price,area_m2,price_m2,parcel_number,mpzp,use_type,address,lat,lon,fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(tx_key) DO UPDATE SET transaction_date=excluded.transaction_date,price=excluded.price,area_m2=excluded.area_m2,price_m2=excluded.price_m2,parcel_number=excluded.parcel_number,mpzp=excluded.mpzp,use_type=excluded.use_type,address=excluded.address,lat=excluded.lat,lon=excluded.lon,fetched_at=excluded.fetched_at'''
        for r in rows:
            stmts.append((sql,[r.get('tx_key'),r.get('transaction_date'),r.get('price'),r.get('area_m2'),r.get('price_m2'),r.get('parcel_number'),r.get('mpzp'),r.get('use_type'),r.get('address'),r.get('lat'),r.get('lon'),now]))
        self.batch(stmts)

    def purge_old_rcn(self,cutoff_iso): self.execute('DELETE FROM rcn_transactions WHERE transaction_date IS NOT NULL AND transaction_date < ?',[cutoff_iso])

    def get_rcn_recent(self,cutoff_iso=None):
        if cutoff_iso:
            return self.query('SELECT * FROM rcn_transactions WHERE transaction_date IS NOT NULL AND transaction_date>=? ORDER BY transaction_date DESC',[cutoff_iso])
        return self.query('SELECT * FROM rcn_transactions ORDER BY transaction_date DESC')


    def geocode_get(self,q):
        rows=self.query('SELECT * FROM geocode_cache WHERE query=?',[q]); return rows[0] if rows else None
    def geocode_put(self,q,lat,lon,display):
        self.execute('INSERT INTO geocode_cache(query,lat,lon,display_name,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(query) DO UPDATE SET lat=excluded.lat,lon=excluded.lon,display_name=excluded.display_name,updated_at=excluded.updated_at',[q,lat,lon,display,self.now()])

    def record_scan(self,**kw):
        cols=['started_at','finished_at','downloaded_records','accepted_records','active_after_scan','new_count','price_change_count','rejected_count','deactivated_count','healthy_sources','total_sources','diagnostics_json','status']
        vals=[kw.get(c) for c in cols]
        # Idempotent on started_at so retry after an ambiguous network timeout cannot
        # create a duplicate scan-run row.
        self.execute(f"INSERT INTO scan_runs({','.join(cols)}) SELECT {','.join('?' for _ in cols)} WHERE NOT EXISTS (SELECT 1 FROM scan_runs WHERE started_at=?)",vals+[kw.get('started_at')])

    def close(self):
        with self._session_lock:self.session.close()

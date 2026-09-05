from __future__ import annotations
import json
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
        self.session=requests.Session()
        self.session.headers.update({'Authorization':f'Bearer {api_token}','Content-Type':'application/json'})
        self.scan_started=None

    def now(self): return datetime.now(timezone.utc).isoformat()
    def begin_scan(self): self.scan_started=self.now()

    def _post(self,payload):
        r=self.session.post(self.url,json=payload,timeout=25)
        r.raise_for_status(); data=r.json()
        if not data.get('success',False):
            raise RuntimeError(json.dumps(data.get('errors') or data,ensure_ascii=False)[:1200])
        return data

    @staticmethod
    def _result_items(data):
        x=data.get('result') or []
        return x if isinstance(x,list) else [x]

    def query(self,sql,params=None):
        data=self._post({'sql':sql,'params':params or []})
        items=self._result_items(data)
        if not items: return []
        return items[0].get('results') or []

    def execute(self,sql,params=None): self._post({'sql':sql,'params':params or []})

    def batch(self,statements,chunk=28):
        stmts=[{'sql':s,'params':p} for s,p in statements]
        for i in range(0,len(stmts),chunk):
            if stmts[i:i+chunk]: self._post({'batch':stmts[i:i+chunk]})

    def count(self):
        rows=self.query('SELECT COUNT(*) c FROM listings')
        return int(rows[0]['c']) if rows else 0

    def count_scans_between(self,start_iso,end_iso):
        rows=self.query('SELECT COUNT(*) c FROM scan_runs WHERE finished_at>=? AND finished_at<?',[start_iso,end_iso])
        return int(rows[0]['c']) if rows else 0

    def set_state(self,key,value):
        now=self.now()
        if not isinstance(value,str): value=json.dumps(value,ensure_ascii=False)
        self.execute('INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',[key,value,now])

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
                stmts.append(("INSERT INTO price_history(listing_id,seen_at,price) SELECT id,?,? FROM listings WHERE canonical_url=?",[now,new_price,rec['canonical_url']]))
            changes.append((rec,is_new,price_changed,old_price,reference_price))
        self.batch(stmts)
        return changes

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
        self.execute(f"INSERT INTO scan_runs({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",vals)

    def close(self): self.session.close()

from __future__ import annotations
import json
from datetime import datetime, timezone
import requests

LISTING_COLUMNS = [
    'canonical_url','source','fingerprint','category','title','price','area_m2','price_m2',
    'plot_type','planning_status','location','lat','lon','distance_km','phone','parcel_number',
    'published_text','published_at','area_warning','image_url','location_confidence','area_locality','area_confidence',
    'privacy_score','privacy_reasons','deal_label','median_comparable','comparable_count',
    'comparison_quality','description'
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
        r=self.session.post(self.url,json=payload,timeout=60)
        r.raise_for_status(); data=r.json()
        if not data.get('success',False):
            raise RuntimeError(json.dumps(data.get('errors') or data,ensure_ascii=False)[:1000])
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

    def execute(self,sql,params=None):
        self._post({'sql':sql,'params':params or []})

    def batch(self,statements,chunk=35):
        stmts=[{'sql':s,'params':p} for s,p in statements]
        for i in range(0,len(stmts),chunk):
            if stmts[i:i+chunk]: self._post({'batch':stmts[i:i+chunk]})

    def count(self):
        rows=self.query('SELECT COUNT(*) c FROM listings')
        return int(rows[0]['c']) if rows else 0

    def existing_map(self):
        return {r['canonical_url']:r for r in self.query('SELECT id,canonical_url,price,active FROM listings')}

    def upsert_many(self,records):
        now=self.now(); old=self.existing_map(); changes=[]; stmts=[]
        update_cols=[c for c in LISTING_COLUMNS if c!='canonical_url']
        placeholders=','.join('?' for _ in LISTING_COLUMNS)
        update_sql=','.join(f'{c}=excluded.{c}' for c in update_cols)
        sql=f'''INSERT INTO listings ({','.join(LISTING_COLUMNS)},first_seen,last_seen,active,missing_scans)
                VALUES ({placeholders},?,?,1,0)
                ON CONFLICT(canonical_url) DO UPDATE SET {update_sql},last_seen=excluded.last_seen,active=1,missing_scans=0'''
        for rec in records:
            prev=old.get(rec['canonical_url'])
            is_new=prev is None
            price_changed=bool(prev and rec.get('price') is not None and prev.get('price') != rec.get('price'))
            first_seen=now if is_new else now  # ignored on conflict by SQL
            params=[rec.get(c) for c in LISTING_COLUMNS]+[first_seen,now]
            stmts.append((sql,params))
            if rec.get('price') is not None and (is_new or price_changed):
                stmts.append(("INSERT INTO price_history(listing_id,seen_at,price) SELECT id,?,? FROM listings WHERE canonical_url=?",[now,rec.get('price'),rec['canonical_url']]))
            changes.append((rec,is_new,price_changed))
        self.batch(stmts)
        return changes

    def age_missing_for_healthy_sources(self,sources,threshold=3):
        if not sources: return 0
        threshold=max(2,int(threshold or 3)); before=self.query('SELECT COUNT(*) c FROM listings WHERE active=0')[0]['c']
        stmts=[]
        for src in dict.fromkeys(sources):
            stmts.append(("UPDATE listings SET missing_scans=COALESCE(missing_scans,0)+1 WHERE source=? AND last_seen < ? AND active=1",[src,self.scan_started]))
            stmts.append(("UPDATE listings SET active=0 WHERE source=? AND active=1 AND COALESCE(missing_scans,0)>=?",[src,threshold]))
        self.batch(stmts)
        after=self.query('SELECT COUNT(*) c FROM listings WHERE active=0')[0]['c']
        return max(0,int(after)-int(before))

    def all_active(self):
        return self.query('SELECT * FROM listings WHERE active=1')

    def update_scores(self,rows):
        stmts=[]
        for r in rows:
            stmts.append(("UPDATE listings SET privacy_score=?,privacy_reasons=?,deal_label=?,median_comparable=?,comparable_count=?,comparison_quality=? WHERE id=?",[
                r.get('privacy_score'),r.get('privacy_reasons'),r.get('deal_label'),r.get('median_comparable'),r.get('comparable_count',0),r.get('comparison_quality'),r['id']]))
        self.batch(stmts)

    def geocode_get(self,q):
        rows=self.query('SELECT * FROM geocode_cache WHERE query=?',[q]); return rows[0] if rows else None

    def geocode_put(self,q,lat,lon,display):
        self.execute('INSERT INTO geocode_cache(query,lat,lon,display_name,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(query) DO UPDATE SET lat=excluded.lat,lon=excluded.lon,display_name=excluded.display_name,updated_at=excluded.updated_at',[q,lat,lon,display,self.now()])

    def record_scan(self,**kw):
        cols=['started_at','finished_at','downloaded_records','accepted_records','active_after_scan','new_count','price_change_count','rejected_count','deactivated_count','healthy_sources','total_sources','diagnostics_json','status']
        vals=[kw.get(c) for c in cols]
        self.execute(f"INSERT INTO scan_runs({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",vals)

    def close(self): self.session.close()

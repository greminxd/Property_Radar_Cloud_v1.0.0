from __future__ import annotations
import math, re, statistics
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
import requests
from dateutil import parser as dtparser
from dateutil.relativedelta import relativedelta
from pyproj import Transformer
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:
    Retry=None

RCN_URL='https://mapy.geoportal.gov.pl/wss/service/rcn'


def _local(tag:str)->str:
    return tag.rsplit('}',1)[-1]


def _num(v):
    if v is None:return None
    s=str(v).strip().replace('\xa0',' ').replace(' ','').replace(',','.')
    m=re.search(r'-?\d+(?:\.\d+)?',s)
    if not m:return None
    try:return float(m.group(0))
    except:return None


def _date(v):
    if not v:return None
    try:
        d=dtparser.parse(str(v),dayfirst=True,fuzzy=True)
        if d.tzinfo is None:d=d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except Exception:return None


def _coords(feature):
    for el in feature.iter():
        if _local(el.tag) in {'posList','pos'} and (el.text or '').strip():
            vals=[]
            for x in re.findall(r'-?\d+(?:\.\d+)?',el.text or ''):
                try:vals.append(float(x))
                except:pass
            if len(vals)>=2:
                pairs=list(zip(vals[0::2],vals[1::2]))
                if pairs:return sum(x for x,_ in pairs)/len(pairs),sum(y for _,y in pairs)/len(pairs)
    return None


def _field_map(feature):
    out={}
    for el in feature.iter():
        name=_local(el.tag)
        txt=(el.text or '').strip()
        if txt and name not in {'pos','posList','Polygon','surfaceMember','exterior','LinearRing'}:
            out[name]=txt
    return out


def _normalize_area_price(area,price):
    if not area or not price or area<=0 or price<=0:return area,None
    ppm=price/area
    # Some source services historically expose hectares; detect obviously impossible p/m².
    if area<100 and ppm>5000:
        area2=area*10000
        ppm2=price/area2
        if 0.1<=ppm2<=5000:return area2,ppm2
    return area,ppm


class RCNClient:
    def __init__(self,center_lat,center_lon,cfg=None):
        self.cfg=cfg or {}
        self.center_lat=float(center_lat);self.center_lon=float(center_lon)
        self.to2180=Transformer.from_crs('EPSG:4326','EPSG:2180',always_xy=True)
        self.to4326=Transformer.from_crs('EPSG:2180','EPSG:4326',always_xy=True)
        self.cx,self.cy=self.to2180.transform(self.center_lon,self.center_lat)
        self.s=requests.Session()
        if Retry:
            retry=Retry(total=2,backoff_factor=.8,status_forcelist=[429,500,502,503,504],allowed_methods=['GET'])
            self.s.mount('https://',HTTPAdapter(max_retries=retry,pool_connections=4,pool_maxsize=4))

    def fetch_recent(self,months=24,radius_km=12,max_features=2500):
        half=float(radius_km)*1000
        # WFS/MapServer accepts BBOX and is much more reliable than CQL filters for this service.
        bbox=f'{self.cx-half},{self.cy-half},{self.cx+half},{self.cy+half},EPSG:2180'
        cutoff=datetime.now(timezone.utc)-relativedelta(months=int(months))
        rows=[];start=0;page=500
        while start<max_features:
            params={'service':'WFS','version':'2.0.0','request':'GetFeature','typenames':'ms:dzialki','bbox':bbox,'startIndex':start,'count':min(page,max_features-start)}
            r=self.s.get(RCN_URL,params=params,timeout=90)
            r.raise_for_status()
            root=ET.fromstring(r.content)
            members=[x for x in root.iter() if _local(x.tag)=='member']
            if not members:break
            added=0
            for mem in members:
                feature=next(iter(mem),None)
                if feature is None:continue
                f=_field_map(feature)
                tx_date=_date(f.get('dok_data') or f.get('DATA'))
                if not tx_date:continue
                try:d=datetime.fromisoformat(tx_date)
                except:continue
                if d<cutoff:continue
                price=_num(f.get('dzi_cena_brutto')) or _num(f.get('nier_cena_brutto'))
                area=_num(f.get('dzi_pow_ewid')) or _num(f.get('nier_pow_gruntu'))
                area,ppm=_normalize_area_price(area,price)
                if not ppm or not (0.1<=ppm<=5000):continue
                xy=_coords(feature)
                if xy:
                    # Axis order varies between WFS implementations. Pick the interpretation nearest our search center.
                    a,b=xy
                    d1=(a-self.cx)**2+(b-self.cy)**2;d2=(b-self.cx)**2+(a-self.cy)**2
                    x,y=(a,b) if d1<=d2 else (b,a)
                    lon,lat=self.to4326.transform(x,y)
                else:lat=lon=None
                key='|'.join([f.get('tran_oznaczenie_trans') or f.get('tran_lokalny_id_iip') or '',f.get('dzi_id_dzialki') or f.get('dzi_nr_dzialki') or '',tx_date,str(price)])
                rows.append({'tx_key':key,'transaction_date':tx_date,'price':price,'area_m2':area,'price_m2':ppm,
                    'parcel_number':f.get('dzi_nr_dzialki'),'mpzp':f.get('dzi_przezn_wmpzp'),'use_type':f.get('dzi_sposob_uzyt'),
                    'address':f.get('dzi_adres'),'lat':lat,'lon':lon})
                added+=1
            if len(members)<page:break
            start+=page
        # dedupe exact transaction+parcel records
        return list({r['tx_key']:r for r in rows}.values())

    @staticmethod
    def _haversine(lat1,lon1,lat2,lon2):
        R=6371.0088
        p1=math.radians(lat1);p2=math.radians(lat2);dp=math.radians(lat2-lat1);dl=math.radians(lon2-lon1)
        a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return 2*R*math.asin(math.sqrt(a))

    def analyze(self,listing,transactions,months=24,min_count=3):
        if listing.get('category')!='plot' or not listing.get('lat') or not listing.get('lon'):
            return {'rcn_count':0,'rcn_quality':'brak dokładnej lokalizacji','rcn_months':months}
        la=float(listing['lat']);lo=float(listing['lon']);ta=listing.get('area_m2')
        candidates=[]
        for t in transactions:
            if t.get('lat') is None or t.get('lon') is None:continue
            if ta and t.get('area_m2'):
                ratio=float(t['area_m2'])/float(ta)
                if ratio<0.5 or ratio>2.0:continue
            dist=self._haversine(la,lo,float(t['lat']),float(t['lon']))
            candidates.append((dist,t))
        chosen=[];used_radius=None
        for rad in [3,5,10]:
            x=[(d,t) for d,t in candidates if d<=rad]
            if len(x)>=min_count:
                chosen=x;used_radius=rad;break
            if len(x)>len(chosen):chosen=x;used_radius=rad
        if not chosen:
            return {'rcn_count':0,'rcn_quality':'brak transakcji w pobliżu','rcn_months':months}
        vals=[float(t['price_m2']) for _,t in chosen if t.get('price_m2')]
        if not vals:return {'rcn_count':0,'rcn_quality':'brak cen transakcyjnych','rcn_months':months}
        newest=max((t for _,t in chosen),key=lambda t:t.get('transaction_date') or '')
        quality='wysoka' if len(vals)>=8 and used_radius<=5 else 'dobra' if len(vals)>=4 and used_radius<=5 else 'orientacyjna'
        return {'rcn_median_ppm':statistics.median(vals),'rcn_mean_ppm':statistics.mean(vals),'rcn_count':len(vals),
            'rcn_radius_km':used_radius,'rcn_months':months,'rcn_last_date':newest.get('transaction_date'),'rcn_last_ppm':newest.get('price_m2'),'rcn_quality':quality}

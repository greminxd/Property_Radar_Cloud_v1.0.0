from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import requests
from pyproj import Transformer
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

EGIB_URL = 'https://mapy.geoportal.gov.pl/wss/service/PZGIK/EGIB/WFS/UslugaZbiorcza'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _fold(v: str) -> str:
    return str(v or '').lower().translate(str.maketrans('ąćęłńóśźż', 'acelnoszz')).strip()


def _norm_parcel(v: str) -> str:
    s = re.sub(r'\s+', '', str(v or '')).lower()
    s = re.sub(r'^(?:dzialka|dz\.?|nr|numer)+', '', _fold(s))
    m = re.search(r'(\d{1,7}(?:/\d{1,7})?)', s)
    return m.group(1) if m else s


def _fields(feature):
    out = {}
    for el in feature.iter():
        name = _local(el.tag)
        txt = (el.text or '').strip()
        if txt and name not in {'pos', 'posList', 'Polygon', 'surfaceMember', 'exterior', 'LinearRing'}:
            out[name] = txt
    return out


def _coords(feature, center_xy):
    vals = []
    for el in feature.iter():
        if _local(el.tag) in {'posList', 'pos'} and (el.text or '').strip():
            vals = [float(x) for x in re.findall(r'-?\d+(?:\.\d+)?', el.text or '')]
            if len(vals) >= 2:
                break
    if len(vals) < 2:
        return None
    pairs = list(zip(vals[0::2], vals[1::2]))
    if not pairs:
        return None
    a = sum(x for x, _ in pairs) / len(pairs)
    b = sum(y for _, y in pairs) / len(pairs)
    cx, cy = center_xy
    d1 = (a - cx) ** 2 + (b - cy) ** 2
    d2 = (b - cx) ** 2 + (a - cy) ** 2
    return (a, b) if d1 <= d2 else (b, a)


class EGIBResolver:
    """Resolve a listing's (locality, parcel number) to an official EGiB parcel id.

    GUGiK's aggregate EGiB WFS supports parcel lookup by cadastral precinct/name and
    parcel number. We query number + municipality, then verify the precinct locally.
    The result gives us a stable ``id_dzialki`` and a real parcel centroid, which is
    much safer for RCN history matching than a village-centre geocode.
    """

    def __init__(self, db, center):
        self.db = db
        self.center_lat = float(center['lat'])
        self.center_lon = float(center['lon'])
        self.to2180 = Transformer.from_crs('EPSG:4326', 'EPSG:2180', always_xy=True)
        self.to4326 = Transformer.from_crs('EPSG:2180', 'EPSG:4326', always_xy=True)
        self.cx, self.cy = self.to2180.transform(self.center_lon, self.center_lat)
        self.s = requests.Session()
        self.s.headers.update({
            'User-Agent': 'PropertyRadar/1.4.4 (+GUGiK EGiB WFS)',
            'Accept': 'application/xml,text/xml,*/*',
            'Connection': 'close',
        })
        if Retry:
            retry = Retry(total=2, backoff_factor=.8, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])
            self.s.mount('https://', HTTPAdapter(max_retries=retry, pool_connections=3, pool_maxsize=3))

    @staticmethod
    def _filter(parcel_number: str):
        # Query exact number only. ``nazwa_gminy`` differs between some county WFS
        # implementations, so gmina/precinct are verified client-side instead of
        # making the WFS filter brittle.
        n = escape(parcel_number)
        return (
            '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">'
            '<fes:PropertyIsEqualTo>'
            '<fes:ValueReference>numer_dzialki</fes:ValueReference>'
            f'<fes:Literal>{n}</fes:Literal>'
            '</fes:PropertyIsEqualTo>'
            '</fes:Filter>'
        )

    def lookup(self, locality: str, parcel_number: str):
        locality = str(locality or '').strip()
        parcel = _norm_parcel(parcel_number)
        if not locality or not parcel:
            return None
        key = f'{_fold(locality)}|{parcel}'
        try:
            cached = self.db.parcel_cache_get(key)
        except Exception:
            cached = None
        if cached:
            if cached.get('status') == 'ok' and cached.get('parcel_id'):
                return {
                    'parcel_id': cached.get('parcel_id'), 'lat': cached.get('lat'), 'lon': cached.get('lon'),
                    'area_m2': cached.get('area_m2'), 'locality': cached.get('locality'),
                    'parcel_number': cached.get('parcel_number'), 'confidence': 'egib-cache',
                }
            if cached.get('status') in {'not-found', 'ambiguous'}:
                # Negative WFS answers are useful for throttling, but upstream county
                # services can temporarily be incomplete. Retry them after 24 h rather
                # than turning one empty response into a permanent cache entry.
                try:
                    updated=datetime.fromisoformat(str(cached.get('updated_at') or '').replace('Z','+00:00'))
                    if updated.tzinfo is None:updated=updated.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc)-updated.astimezone(timezone.utc) < timedelta(hours=24):
                        return None
                except Exception:
                    pass

        params = {
            'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature', 'typenames': 'ms:dzialki',
            'count': 100, 'filter': self._filter(parcel),
        }
        try:
            r = self.s.get(EGIB_URL, params=params, timeout=50)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            # Do not negative-cache transport errors: a later scan should retry.
            return None

        candidates = []
        for mem in [x for x in root.iter() if _local(x.tag) == 'member']:
            feature = next(iter(mem), None)
            if feature is None:
                continue
            f = _fields(feature)
            num = _norm_parcel(f.get('numer_dzialki') or f.get('NUMER_DZIALKI'))
            if num != parcel:
                continue
            gmina = f.get('nazwa_gminy') or f.get('NAZWA_GMINY') or ''
            obreb = f.get('nazwa_obrebu') or f.get('NAZWA_OBREBU') or ''
            # Restrict to the intended municipality and cadastral precinct. County WFS
            # naming varies, hence folded substring checks rather than exact casing.
            if gmina and 'zakliczyn' not in _fold(gmina):
                continue
            if obreb and _fold(locality) not in _fold(obreb) and _fold(obreb) not in _fold(locality):
                continue
            parcel_id = f.get('id_dzialki') or f.get('ID_DZIALKI') or ''
            xy = _coords(feature, (self.cx, self.cy))
            lat = lon = None
            if xy:
                lon, lat = self.to4326.transform(*xy)
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    lat = lon = None
            area = f.get('pole_powierzchni') or f.get('POLE_POWIERZCHNI') or f.get('pole_ewidencyjne')
            try:
                area = float(str(area).replace(',', '.')) if area is not None else None
            except Exception:
                area = None
            candidates.append({'parcel_id': parcel_id, 'lat': lat, 'lon': lon, 'area_m2': area, 'locality': obreb or locality, 'parcel_number': parcel})

        # One official parcel id = trustworthy. Multiple different ids means we refuse
        # to invent a history match.
        by_id = {c['parcel_id']: c for c in candidates if c.get('parcel_id')}
        if len(by_id) == 1:
            out = next(iter(by_id.values()))
            out['confidence'] = 'egib-exact'
            try:
                self.db.parcel_cache_put(key, out['locality'], parcel, out['parcel_id'], out['lat'], out['lon'], out['area_m2'], 'ok')
            except Exception:
                pass
            return out
        status = 'ambiguous' if len(by_id) > 1 else 'not-found'
        try:
            self.db.parcel_cache_put(key, locality, parcel, None, None, None, None, status)
        except Exception:
            pass
        return None

from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests
from dateutil import parser as dtparser
from dateutil.relativedelta import relativedelta
from pyproj import Transformer
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:
    Retry = None

RCN_URL = 'https://mapy.geoportal.gov.pl/wss/service/rcn'
# Bump this whenever interpretation of raw RCN fields changes. main.py uses it to
# invalidate the D1 cache so old, wrongly-normalised transactions can never leak
# into price comparisons after an upgrade.
RCN_PARSER_VERSION = 'v4.0-official-wfs-parcel-history'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _fold(v: str) -> str:
    return str(v or '').lower().translate(str.maketrans('ąćęłńóśźż', 'acelnoszz')).strip()


def _norm_parcel(v: str) -> str:
    s=re.sub(r'\s+','',_fold(v))
    m=re.search(r'(\d{1,7}(?:/\d{1,7})?)',s)
    return m.group(1) if m else s


def _num(v):
    """Parse Polish/English formatted numeric values without losing thousands.

    Examples:
      1 000 000,00 -> 1000000.00
      1.000.000,00 -> 1000000.00
      1,000,000.00 -> 1000000.00
      0,5500       -> 0.55
    """
    if v is None:
        return None
    s = str(v).strip().replace('\xa0', '').replace(' ', '')
    s = re.sub(r'[^0-9,\.\-+]', '', s)
    if not s or not re.search(r'\d', s):
        return None

    # Both separators present: whichever occurs last is the decimal separator.
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif s.count('.') > 1:
        # 1.234.567 or 1.234.567, already stripped comma branch above.
        s = s.replace('.', '')
    elif s.count(',') > 1:
        s = s.replace(',', '')
    elif ',' in s:
        left, right = s.rsplit(',', 1)
        # RCN area/price decimals normally have 1-4 fractional digits. A 3-digit
        # tail on a large integer is overwhelmingly a thousands group.
        if len(right) == 3 and len(left.lstrip('+-')) >= 1:
            s = left + right
        else:
            s = left + '.' + right
    elif '.' in s:
        left, right = s.rsplit('.', 1)
        if len(right) == 3 and len(left.lstrip('+-')) >= 1 and len(left.lstrip('+-')) <= 3:
            # "1.000" is usually a thousands separator in Polish source data,
            # while "0.550" is a decimal. Preserve the latter.
            try:
                if abs(float(left)) >= 1:
                    s = left + right
            except Exception:
                pass
    try:
        return float(s)
    except Exception:
        return None


def _date(v):
    """Parse RCN dates, preferring ISO semantics and rejecting future records.

    The old implementation forced dayfirst=True on ISO strings, turning e.g.
    2026-03-10 into 2026-10-03 on some dateutil paths.
    """
    if not v:
        return None
    raw = str(v).strip()
    d = None
    try:
        if re.match(r'^\d{4}-\d{2}-\d{2}(?:[T\s].*)?$', raw):
            d = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        else:
            d = dtparser.parse(raw, dayfirst=True, fuzzy=True)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(timezone.utc)
    if d > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return d.isoformat()


def _coords(feature):
    for el in feature.iter():
        if _local(el.tag) in {'posList', 'pos'} and (el.text or '').strip():
            vals = []
            for x in re.findall(r'-?\d+(?:\.\d+)?', el.text or ''):
                try:
                    vals.append(float(x))
                except Exception:
                    pass
            if len(vals) >= 2:
                pairs = list(zip(vals[0::2], vals[1::2]))
                if pairs:
                    return sum(x for x, _ in pairs) / len(pairs), sum(y for _, y in pairs) / len(pairs)
    return None


def _field_map(feature):
    out = {}
    for el in feature.iter():
        name = _local(el.tag)
        txt = (el.text or '').strip()
        if txt and name not in {'pos', 'posList', 'Polygon', 'surfaceMember', 'exterior', 'LinearRing'}:
            out[name] = txt
    return out


def _ha_to_m2(v):
    """RCN/EGiB ground areas are expressed in hectares -> square metres.

    Area values are decimals in hectares, so a string like "1.000" means 1 ha,
    not one thousand. Parse area separately from price/thousands formatting.
    """
    if v is None:return None
    raw=str(v).strip().replace('\xa0','').replace(' ','')
    raw=re.sub(r'[^0-9,\.\-+]','',raw)
    if not raw:return None
    if ',' in raw and '.' in raw:
        if raw.rfind(',')>raw.rfind('.'):
            raw=raw.replace('.','').replace(',','.')
        else:
            raw=raw.replace(',','')
    elif ',' in raw:
        raw=raw.replace(',','.')
    try:n=float(raw)
    except Exception:return None
    if n<=0:return None
    return n*10000.0


def _undeveloped_flag(value):
    """Return True/False when the RCN property kind is recognisable, else None."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    folded = raw.translate(str.maketrans('ąćęłńóśźż', 'acelnoszz'))
    # Some exports use enumerated codes; old/current RCN models consistently put
    # undeveloped ground types in 1/2/3/5 and built/non-ground types elsewhere.
    m = re.fullmatch(r'\D*(\d+)\D*', folded)
    if m:
        code = int(m.group(1))
        if code in {1, 2, 3, 5}:
            return True
        if code in {4, 6, 7, 8, 9, 10}:
            return False
    if 'niezabud' in folded:
        return True
    if 'zabud' in folded or 'lokal' in folded or 'budyn' in folded:
        return False
    return None


def _robust_rows(rows):
    """Remove obvious unit/parser/outlier contamination from a comparable set."""
    clean = [r for r in rows if r.get('price_m2') is not None and 0.5 <= float(r['price_m2']) <= 3000]
    if len(clean) < 5:
        return clean
    vals = [float(r['price_m2']) for r in clean]
    med = statistics.median(vals)
    if med <= 0:
        return []
    absdev = [abs(v - med) for v in vals]
    mad = statistics.median(absdev)
    out = []
    for r in clean:
        v = float(r['price_m2'])
        ratio_ok = med / 5 <= v <= med * 5
        mad_ok = True if mad <= 0 else abs(v - med) <= 3.5 * 1.4826 * mad
        if ratio_ok and mad_ok:
            out.append(r)
    return out if len(out) >= 3 else clean


class RCNClient:
    def __init__(self, center_lat, center_lon, cfg=None):
        self.cfg = cfg or {}
        self.center_lat = float(center_lat)
        self.center_lon = float(center_lon)
        self.to2180 = Transformer.from_crs('EPSG:4326', 'EPSG:2180', always_xy=True)
        self.to4326 = Transformer.from_crs('EPSG:2180', 'EPSG:4326', always_xy=True)
        self.cx, self.cy = self.to2180.transform(self.center_lon, self.center_lat)
        self.s = requests.Session()
        self.s.headers.update({
            'User-Agent':'PropertyRadar/1.4.4 (+GUGiK RCN WFS)',
            'Accept':'application/xml,text/xml,*/*',
            'Connection':'close',
        })
        if Retry:
            retry = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET'])
            self.s.mount('https://', HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4))

    def fetch_recent(self, months=24, radius_km=12, max_features=2500):
        half = float(radius_km) * 1000
        bbox = f'{self.cx-half},{self.cy-half},{self.cx+half},{self.cy+half},EPSG:2180'
        cutoff = datetime.now(timezone.utc) - relativedelta(months=int(months))
        rows = []
        start = 0
        page = 500
        while start < max_features:
            params = {
                'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature', 'typenames': 'ms:dzialki',
                'bbox': bbox, 'startIndex': start, 'count': min(page, max_features-start),
            }
            r = self.s.get(RCN_URL, params=params, timeout=180)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            members = [x for x in root.iter() if _local(x.tag) == 'member']
            if not members:
                break
            for mem in members:
                feature = next(iter(mem), None)
                if feature is None:
                    continue
                f = _field_map(feature)
                tx_date = _date(f.get('dok_data') or f.get('DATA'))
                if not tx_date:
                    continue
                try:
                    d = datetime.fromisoformat(tx_date)
                except Exception:
                    continue
                if d < cutoff:
                    continue

                # For plot benchmarking keep undeveloped ground properties. Unknown
                # kind is allowed because not every integrated source exposes the field;
                # explicit built/local/building records are rejected.
                kind = f.get('nier_rodzaj') or f.get('rodzajNieruchomosci')
                if _undeveloped_flag(kind) is False:
                    continue

                # NEVER mix a parcel price with a whole-property area (or vice versa).
                parcel_price = _num(f.get('dzi_cena_brutto'))
                parcel_area = _ha_to_m2(f.get('dzi_pow_ewid'))
                property_price = _num(f.get('nier_cena_brutto'))
                property_area = _ha_to_m2(f.get('nier_pow_gruntu'))
                if parcel_price and parcel_area:
                    price, area, basis = parcel_price, parcel_area, 'parcel'
                elif property_price and property_area:
                    price, area, basis = property_price, property_area, 'property'
                else:
                    continue
                if not (100 <= price <= 1_000_000_000 and 50 <= area <= 20_000_000):
                    continue
                ppm = price / area
                if not (0.5 <= ppm <= 3000):
                    continue

                xy = _coords(feature)
                if xy:
                    a, b = xy
                    d1 = (a-self.cx)**2 + (b-self.cy)**2
                    d2 = (b-self.cx)**2 + (a-self.cy)**2
                    x, y = (a, b) if d1 <= d2 else (b, a)
                    lon, lat = self.to4326.transform(x, y)
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        lat = lon = None
                else:
                    lat = lon = None

                txid = f.get('tran_oznaczenie_trans') or f.get('tran_lokalny_id_iip') or f.get('IdRCN') or ''
                parcel = f.get('dzi_id_dzialki') or f.get('dzi_nr_dzialki') or ''
                # Keep one row PER PARCEL even when the source exposes only a whole-
                # property price. Exact parcel history needs this membership relation.
                # Benchmark de-duplication of property-level prices happens later in
                # ``analyze`` so a multi-parcel transaction is counted only once there.
                if basis == 'property':
                    key = '|'.join([txid, 'PROPERTY', parcel, tx_date, f'{price:.2f}', f'{area:.2f}'])
                else:
                    key = '|'.join([txid, parcel, tx_date, f'{price:.2f}'])
                rows.append({
                    'tx_key': key,
                    'transaction_date': tx_date,
                    'price': price,
                    'area_m2': area,
                    'price_m2': ppm,
                    'parcel_number': f.get('dzi_nr_dzialki'),
                    'parcel_id': f.get('dzi_id_dzialki'),
                    'transaction_id': txid or None,
                    'price_basis': basis,
                    'mpzp': f.get('dzi_przezn_wmpzp'),
                    'use_type': f.get('dzi_sposob_uzyt') or kind,
                    'address': f.get('dzi_adres'),
                    'lat': lat,
                    'lon': lon,
                })
            if len(members) < page:
                break
            start += page
        return list({r['tx_key']: r for r in rows}.values())

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        R = 6371.0088
        p1 = math.radians(lat1); p2 = math.radians(lat2)
        dp = math.radians(lat2-lat1); dl = math.radians(lon2-lon1)
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return 2 * R * math.asin(math.sqrt(a))

    def find_parcel_history(self, listing, transactions, max_fallback_distance_km=2.5):
        """Return transactions that can be attributed to this exact advertised parcel.

        Preferred match is the official EGiB/RCN ``id_dzialki``. If an ad does not
        resolve to EGiB, a conservative fallback requires the same parcel number plus
        supporting locality/geometry/area evidence. A bare parcel number is never enough
        because numbers repeat between cadastral precincts.
        """
        parcel_id=str(listing.get('parcel_id') or '').strip()
        parcel_no=_norm_parcel(listing.get('parcel_number'))
        if not parcel_id and not parcel_no:
            return []
        locality=_fold(listing.get('area_locality') or listing.get('location'))
        target_area=float(listing.get('area_m2') or 0)
        la=listing.get('lat'); lo=listing.get('lon')
        try:
            la=float(la) if la is not None else None; lo=float(lo) if lo is not None else None
        except Exception:
            la=lo=None
        found=[]
        for t in transactions:
            quality=None; dist=None
            tx_pid=str(t.get('parcel_id') or '').strip()
            if parcel_id and tx_pid and tx_pid==parcel_id:
                quality='egib-id-exact' if str(t.get('price_basis') or '')=='parcel' else 'egib-id-property-level'
            else:
                if not parcel_no or _norm_parcel(t.get('parcel_number'))!=parcel_no:
                    continue
                addr=_fold(t.get('address'))
                locality_ok=bool(locality and addr and locality in addr)
                area_ok=False
                if target_area and t.get('area_m2'):
                    try:
                        ratio=float(t['area_m2'])/target_area
                        area_ok=0.70<=ratio<=1.35
                    except Exception:
                        pass
                if la is not None and lo is not None and t.get('lat') is not None and t.get('lon') is not None:
                    try:dist=self._haversine(la,lo,float(t['lat']),float(t['lon']))
                    except Exception:dist=None
                # Exact EGiB centroid on the listing makes distance strong evidence.
                exact_geo=str(listing.get('parcel_id_confidence') or '').startswith('egib')
                if exact_geo and dist is not None and dist<=0.35:
                    quality='parcel-number+egib-geometry'
                elif locality_ok and area_ok and (dist is None or dist<=max_fallback_distance_km):
                    quality='parcel-number+locality+area'
                else:
                    continue
            row=dict(t); row['history_match']=quality
            if dist is not None:row['distance_km']=dist
            found.append(row)
        # One RCN transaction can appear in several rows after integration. Prefer a
        # stable transaction id / tx key to avoid showing duplicates.
        dedup={}
        for t in found:
            k=t.get('transaction_id') or t.get('tx_key') or '|'.join([str(t.get('transaction_date')),str(t.get('price')),str(t.get('parcel_id') or t.get('parcel_number'))])
            old=dedup.get(k)
            if old is None or (str(t.get('history_match',''))=='egib-id-exact' and str(old.get('history_match',''))!='egib-id-exact'):
                dedup[k]=t
        return sorted(dedup.values(),key=lambda x:x.get('transaction_date') or '',reverse=True)

    def analyze(self, listing, transactions, months=24, min_count=3):
        empty = {
            'rcn_median_ppm': None, 'rcn_mean_ppm': None, 'rcn_count': 0,
            'rcn_radius_km': None, 'rcn_months': months, 'rcn_last_date': None,
            'rcn_last_ppm': None, 'rcn_quality': 'brak wiarygodnych porównań',
        }
        if listing.get('category') != 'plot' or not listing.get('lat') or not listing.get('lon'):
            empty['rcn_quality'] = 'brak dokładnej lokalizacji'
            return empty

        la = float(listing['lat']); lo = float(listing['lon'])
        target_area = float(listing.get('area_m2') or 0)
        candidates = []
        now = datetime.now(timezone.utc) + timedelta(days=1)
        cutoff = datetime.now(timezone.utc) - relativedelta(months=int(months))
        for t in transactions:
            try:
                ppm = float(t.get('price_m2'))
                if not (0.5 <= ppm <= 3000):
                    continue
                td = datetime.fromisoformat(str(t.get('transaction_date')).replace('Z', '+00:00'))
                if td.tzinfo is None:
                    td = td.replace(tzinfo=timezone.utc)
                td = td.astimezone(timezone.utc)
                if td > now or td < cutoff:
                    continue
                if t.get('lat') is None or t.get('lon') is None:
                    continue
                if target_area and t.get('area_m2'):
                    ratio = float(t['area_m2']) / target_area
                    if ratio < 0.5 or ratio > 2.0:
                        continue
                dist = self._haversine(la, lo, float(t['lat']), float(t['lon']))
                candidates.append((dist, t))
            except Exception:
                continue

        chosen = []
        used_radius = None
        for rad in [3, 5, 10]:
            raw = [t for d, t in candidates if d <= rad]
            # RCN can expose one whole-property transaction once for every parcel
            # belonging to that property. Keep those rows for parcel history, but for
            # the market benchmark count the property transaction exactly once.
            unique=[]; seen_property=set()
            for t in raw:
                if str(t.get('price_basis') or '')=='property':
                    pk=t.get('transaction_id') or '|'.join([
                        str(t.get('transaction_date') or ''),
                        str(t.get('price') or ''),str(t.get('area_m2') or ''),
                    ])
                    if pk in seen_property:
                        continue
                    seen_property.add(pk)
                unique.append(t)
            trimmed = _robust_rows(unique)
            if len(trimmed) >= int(min_count):
                chosen = trimmed
                used_radius = rad
                break
            if len(trimmed) > len(chosen):
                chosen = trimmed
                used_radius = rad

        if len(chosen) < int(min_count):
            empty['rcn_count'] = len(chosen)
            empty['rcn_radius_km'] = used_radius
            empty['rcn_quality'] = f'za mało danych ({len(chosen)}/{int(min_count)})'
            return empty

        vals = [float(t['price_m2']) for t in chosen]
        newest = max(chosen, key=lambda t: t.get('transaction_date') or '')
        quality = 'wysoka' if len(vals) >= 8 and used_radius <= 5 else 'dobra' if len(vals) >= 4 and used_radius <= 5 else 'orientacyjna'
        return {
            'rcn_median_ppm': statistics.median(vals),
            'rcn_mean_ppm': statistics.mean(vals),
            'rcn_count': len(vals),
            'rcn_radius_km': used_radius,
            'rcn_months': months,
            'rcn_last_date': newest.get('transaction_date'),
            'rcn_last_ppm': newest.get('price_m2'),
            'rcn_quality': quality,
        }

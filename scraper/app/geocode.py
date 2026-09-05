from __future__ import annotations
import re, time, requests
from .utils import clean_text, haversine_km

KNOWN = {
    "biesnik": (49.82625, 20.80990),
}

class Geocoder:
    def __init__(self, db, center):
        self.db=db; self.center=center
        self.session=requests.Session()
        self.session.headers["User-Agent"]="BiesnikNieruchomosciBot/1.0 (private local property monitor)"
        self.last=0.0

    def from_jsonld(self, objs):
        for o in objs:
            if not isinstance(o,dict): continue
            geo=o.get("geo")
            if not geo and isinstance(o.get("location"),dict):
                geo=(o.get("location") or {}).get("geo")
            if isinstance(geo,dict):
                try:
                    lat=float(geo.get("latitude")); lon=float(geo.get("longitude"))
                    if 40 < lat < 60 and 10 < lon < 30: return lat,lon
                except: pass
        return None

    def geocode(self, location: str):
        q=clean_text(location)
        if not q: return None
        cached=self.db.geocode_get(q)
        if cached: return cached["lat"],cached["lon"]
        # Force the correct Zakliczyn area whenever locality is ambiguous.
        query = q
        low=q.lower()
        if "małopol" not in low and "malopol" not in low:
            query += ", małopolskie"
        if "polska" not in low:
            query += ", Polska"
        wait=max(0,1.05-(time.time()-self.last))
        if wait: time.sleep(wait)
        try:
            r=self.session.get("https://nominatim.openstreetmap.org/search",params={"q":query,"format":"jsonv2","limit":5,"countrycodes":"pl"},timeout=12)
            self.last=time.time(); r.raise_for_status()
            for item in r.json():
                lat=float(item["lat"]); lon=float(item["lon"])
                d=haversine_km(self.center["lat"],self.center["lon"],lat,lon)
                # choose local hit, rejecting Zakliczyn near Myślenice etc.
                if d <= 35:
                    self.db.geocode_put(q,lat,lon,item.get("display_name",""))
                    return lat,lon
        except Exception:
            self.last=time.time()
        return None

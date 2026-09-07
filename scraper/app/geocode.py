from __future__ import annotations
import time, requests
from .utils import clean_text, haversine_km
from .area import normalize_voivodeship

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
                    # Whole Poland + small margin. Do not throw away a foreign point:
                    # the radius validator needs the real coordinate to reject it.
                    if 48.0 < lat < 55.5 and 13.0 < lon < 24.8: return lat,lon
                except: pass
        return None

    def geocode(self, location: str, region: str | None=None):
        q=clean_text(location)
        region=clean_text(region)
        if not q: return None

        # Never overwrite explicit source geography. v1.5.2 always appended
        # `małopolskie`, so "Wróblowice, Dolnośląskie" reduced to Wróblowice and
        # could be resolved to the same-named village near Zakliczyn.
        query=q
        explicit_region=normalize_voivodeship(region) or normalize_voivodeship(q)
        if region and normalize_voivodeship(q) is None:
            query += ", " + region
        if explicit_region is None:
            query += ", małopolskie"
        if "polska" not in query.lower():
            query += ", Polska"

        # New cache namespace includes the actual query/context, so poisoned v1.5.2
        # cache entries keyed only by a bare same-named locality are ignored.
        cache_key="v2|"+clean_text(query)
        cached=self.db.geocode_get(cache_key)
        if cached: return cached["lat"],cached["lon"]

        wait=max(0,1.05-(time.time()-self.last))
        if wait: time.sleep(wait)
        try:
            r=self.session.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q":query,"format":"jsonv2","limit":5,"countrycodes":"pl"},
                timeout=12,
            )
            self.last=time.time(); r.raise_for_status()
            for item in r.json():
                lat=float(item["lat"]); lon=float(item["lon"])
                display=clean_text(item.get("display_name", ""))
                result_region=normalize_voivodeship(display)
                # If the source explicitly supplied a province, Nominatim must agree.
                if explicit_region and result_region and normalize_voivodeship(explicit_region)!=normalize_voivodeship(result_region):
                    continue
                d=haversine_km(self.center["lat"],self.center["lon"],lat,lon)
                # Geocoder is only a candidate resolver for this local radar. Far-away
                # same-name hits are never cached as a usable local coordinate.
                if d <= 35:
                    self.db.geocode_put(cache_key,lat,lon,display)
                    return lat,lon
        except Exception:
            self.last=time.time()
        return None

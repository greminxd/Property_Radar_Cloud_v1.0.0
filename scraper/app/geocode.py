from __future__ import annotations
import re
import time
import requests
from .utils import clean_text, haversine_km
from .area import normalize_voivodeship, fold
from .listing_data import entity_coordinates, coordinate_pair


def geocode_query(record):
    location = clean_text(record.get('_structured_location') or record.get('location'))
    if not location:
        return ''
    evidence = fold(' '.join([record.get('title') or '', record.get('description') or '']))
    for pattern, prefix in ((r'\bpowiat\s+([a-z-]+)', 'powiat '), (r'\b(?:gmina|gm\.)\s+([a-z-]+)', 'gmina ')):
        matches = set(re.findall(pattern, evidence))
        if len(matches) == 1:
            location += ', ' + prefix + next(iter(matches))
    return location


class Geocoder:
    def __init__(self, db, center):
        self.db = db
        self.center = center
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'PropertyRadar/1.6.2 (private property monitor)'
        self.last = 0.0
        self.last_reason = ''

    def from_jsonld(self, objs):
        return entity_coordinates(objs)

    def geocode(self, location: str, region: str | None = None):
        query = clean_text(location)
        self.last_reason = 'location-missing'
        if not query:
            return None
        if re.search(r'\bzakliczyn(?:ie|a)?\b',fold(query)) and not re.search(r'\b(?:powiat|gmina|gm\.|tarnowsk\w*|myslenick\w*|siepraw)\b',fold(query)):
            self.last_reason = 'geocode-ambiguous'
            return None
        explicit_region = normalize_voivodeship(region) or normalize_voivodeship(query)
        if explicit_region and not normalize_voivodeship(query):
            query += ', ' + explicit_region
        if 'polska' not in fold(query):
            query += ', Polska'
        cache_key = 'v3-unambiguous|' + fold(query)
        cached = self.db.geocode_get(cache_key)
        if cached:
            coords = coordinate_pair(cached)
            if coords:
                self.last_reason = 'unique-locality-cache'
                return coords
        wait = max(0, 1.05 - (time.monotonic() - self.last))
        if wait:
            time.sleep(wait)
        try:
            self.last = time.monotonic()
            response = self.session.get(
                'https://nominatim.openstreetmap.org/search',
                params={'q': query, 'format': 'jsonv2', 'limit': 20, 'countrycodes': 'pl', 'addressdetails': 1, 'featuretype': 'settlement'},
                timeout=12,
            )
            response.raise_for_status()
            items = response.json()
            if not isinstance(items, list) or len(items) >= 20:
                self.last_reason = 'geocode-too-many-candidates'
                return None
            candidates = []
            for item in items:
                coords = coordinate_pair(item)
                if not coords:
                    continue
                display = clean_text(item.get('display_name', ''))
                result_region = normalize_voivodeship(display)
                if explicit_region and result_region != explicit_region:
                    continue
                address = item.get('address') or {}
                admin_text = fold(display + ' ' + ' '.join(str(value) for value in address.values()))
                constraints = re.findall(r'\b(?:powiat|gmina)\s+([a-z-]+)', fold(query))
                if any(not re.search(r'\b' + re.escape(value) + r'\b', admin_text) for value in constraints):
                    continue
                if not any(haversine_km(*coords, *candidate[0]) < 1 for candidate in candidates):
                    candidates.append((coords, display))
            if len(candidates) != 1:
                self.last_reason = 'geocode-ambiguous' if candidates else 'geocode-no-match'
                return None
            coords, display = candidates[0]
            self.db.geocode_put(cache_key, *coords, display)
            self.last_reason = 'unique-locality'
            return coords
        except (requests.RequestException, ValueError, TypeError, KeyError):
            self.last_reason = 'geocode-unavailable'
            return None

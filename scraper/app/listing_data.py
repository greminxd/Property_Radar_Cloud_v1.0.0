from __future__ import annotations
import math
from urllib.parse import urlsplit


EXCLUDED = {'recommendations', 'recommended', 'recommendedads', 'similarads', 'relatedoffers', 'searchads', 'itemlistelement', 'seller', 'agency', 'user', 'breadcrumbs', 'breadcrumb'}


def scoped(value):
    if isinstance(value, dict):
        return {key: scoped(child) for key, child in value.items() if key.lower() not in EXCLUDED}
    if isinstance(value, list):
        return [scoped(child) for child in value]
    return value


def same_url(value, url):
    if isinstance(value, dict):
        value = value.get('@id') or value.get('url')
    if not isinstance(value, str):
        return False
    actual, expected = urlsplit(value), urlsplit(url)
    return actual.path.rstrip('/') == expected.path.rstrip('/') and (not actual.netloc or actual.netloc.removeprefix('www.') == expected.netloc.removeprefix('www.'))


def primary_entities(objects, url):
    candidates = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        kind = str(obj.get('@type', '')).lower()
        if any(label in kind for label in ('itemlist', 'breadcrumblist', 'organization', 'website')):
            continue
        nested = obj.get('mainEntity')
        if isinstance(nested, dict):
            obj = nested
        if not any(key in obj for key in ('offers', 'price', 'address', 'itemOffered', 'geo', 'lotSize')):
            continue
        identity = obj.get('url') or obj.get('mainEntityOfPage')
        if identity and not same_url(identity, url):
            continue
        candidates.append(scoped(obj))
    matched = [obj for obj in candidates if same_url(obj.get('url') or obj.get('mainEntityOfPage'), url)]
    return matched or (candidates if len(candidates) == 1 else [])


def primary_payload(payloads, url):
    for payload in payloads:
        if not isinstance(payload, dict) or '@type' in payload:
            continue
        props = (payload.get('props') or {}).get('pageProps') or {}
        candidates = [props.get('ad'), props.get('advert'), props.get('listing'), payload.get('ad'), payload.get('offer')]
        if payload.get('title') and payload.get('url'):
            candidates.append(payload)
        for candidate in candidates:
            if not isinstance(candidate, dict) or not (candidate.get('title') or candidate.get('name')):
                continue
            identity = candidate.get('url') or candidate.get('canonicalUrl')
            if identity and not same_url(identity, url):
                continue
            return scoped(candidate)
    return {}


def coordinate_pair(value):
    if not isinstance(value, dict):
        return None
    try:
        lat = float(value.get('latitude', value.get('lat')))
        lon = float(value.get('longitude', value.get('lon', value.get('lng'))))
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
            return lat, lon
    except (TypeError, ValueError):
        pass
    return None


def entity_coordinates(entities):
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        for key in ('geo', 'coordinates', 'map'):
            result = coordinate_pair(entity.get(key))
            if result:
                return result
        for key in ('location', 'itemOffered', 'mainEntity'):
            result = entity_coordinates([entity.get(key)])
            if result:
                return result
    return None


def payload_entity(payload):
    if not payload:
        return {}
    location = payload.get('location') or {}
    address = location.get('address') or location if isinstance(location, dict) else {}
    def label(value):
        return value.get('name', value.get('label', '')) if isinstance(value, dict) else value or ''
    entity = {
        'name': payload.get('title'), 'description': payload.get('description'),
        'address': {
            'addressLocality': label(address.get('city') or address.get('addressLocality')),
            'addressRegion': label(address.get('province') or address.get('region') or address.get('addressRegion')),
        },
        'datePublished': payload.get('createdAt') or payload.get('created_time'),
        'dateModified': payload.get('modifiedAt') or payload.get('last_refresh_time'),
    }
    for key, prefix in (('county', 'powiat '), ('municipality', 'gmina ')):
        value = label(address.get(key))
        if value:
            entity['address']['addressLocality'] += ', ' + prefix + str(value)
    price = payload.get('totalPrice') or payload.get('price')
    if isinstance(price, dict):
        currency = price.get('currency') or price.get('currencyCode') or 'PLN'
        price = price.get('value', price.get('amount')) if currency == 'PLN' else None
    if price is not None:
        entity['offers'] = {'price': price, 'priceCurrency': 'PLN'}
    area = payload.get('terrainArea') or payload.get('lotArea') or payload.get('plotArea') or payload.get('areaInSquareMeters')
    if area:
        entity['lotSize'] = area
    for item in payload.get('characteristics') or []:
        if not isinstance(item, dict):
            continue
        key = item.get('key')
        if key in ('terrain_area', 'plot_area', 'lot_area'):
            entity['lotSize'] = item.get('value')
        if key == 'price' and 'offers' not in entity:
            entity['offers'] = {'price': item.get('value'), 'priceCurrency': 'PLN'}
    coords = entity_coordinates([payload])
    if coords:
        entity['geo'] = {'latitude': coords[0], 'longitude': coords[1]}
    return entity

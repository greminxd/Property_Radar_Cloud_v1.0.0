from __future__ import annotations
import math


def alert_location_eligible(record, cfg):
    try:
        distance = float(record.get('distance_km'))
        radius = float(cfg['center']['radius_km'])
        if not math.isfinite(distance) or not 0 <= distance <= radius:
            return False
    except (TypeError, ValueError, KeyError):
        return False
    confidence = str(record.get('area_confidence') or '')
    if 'egib-exact' in confidence:
        return True
    if 'olx-api' in confidence or 'listing-geo' in confidence:
        margin = max(0, float(cfg.get('telegram', {}).get('coordinate_margin_km', 1)))
        return distance + margin <= radius
    return bool(cfg.get('telegram', {}).get('allow_locality_center_alerts', False)) and 'location-geocode' in confidence


def safe_to_age_source(diagnostics):
    return bool(
        diagnostics.get('healthy')
        and diagnostics.get('coverage_complete') is True
        and not diagnostics.get('errors')
        and not diagnostics.get('blocked')
        and not diagnostics.get('detail_timeouts_or_cancelled')
        and not diagnostics.get('persistence_error')
    )

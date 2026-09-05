from __future__ import annotations
import re
import unicodedata


def fold(s: str | None) -> str:
    s = (s or '').strip().lower().replace('ł', 'l')
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))


def locality_aliases(name: str) -> list[str]:
    n = fold(name)
    aliases = {n}
    custom = {
        'slona': {'slona', 'slonej'},
        'zdonia': {'zdonia', 'zdoni', 'zdonii'},
        'zakliczyn': {'zakliczyn', 'zakliczyna', 'zakliczynie'},
        'biesnik': {'biesnik', 'biesnika', 'biesniku'},
        'konczyska': {'konczyska', 'konczyskach'},
        'olszowa': {'olszowa', 'olszowej'},
        'palesnica': {'palesnica', 'palesnicy'},
        'luslawice': {'luslawice', 'luslawicach'},
        'wesolow': {'wesolow', 'wesolowie'},
        'milowka': {'milowka', 'milowce', 'milowki'},
        'zlota': {'zlota', 'zlotej'},
    }
    aliases |= custom.get(n, set())
    return sorted(aliases, key=len, reverse=True)


def _contains_alias(text: str, alias: str) -> bool:
    return re.search(rf'(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])', text) is not None


def _find_names(text: str, names: list[str]) -> list[str]:
    out=[]
    t=fold(text)
    for name in names:
        if any(_contains_alias(t,a) for a in locality_aliases(name)):
            out.append(name)
    return out


def _first_hit(text: str, names: list[str]):
    """Return (name, position) for the earliest known locality mention."""
    t=fold(text)
    best=None
    for name in names:
        for alias in locality_aliases(name):
            m=re.search(rf'(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])', t)
            if m and (best is None or m.start() < best[1]):
                best=(name,m.start())
    return best


def detect_allowed_locality(location: str | None, title: str | None, description: str | None, area_cfg: dict) -> tuple[str | None, str]:
    """Resolve locality conservatively.

    v0.3.1 rule: an unrecognised location string is *not* an automatic rejection. Portals
    often return strings such as "powiat tarnowski" or their own region labels. Such rows
    may still be accepted by a trustworthy distance fallback. Explicit known outside towns
    (e.g. Milówka/Złota) still hard-reject the row.
    """
    allowed = list(dict.fromkeys(list(area_cfg.get('primary_localities') or []) + list(area_cfg.get('nearby_localities') or [])))
    known_gmina = list(area_cfg.get('known_gmina_localities') or [])
    known_outside = list(area_cfg.get('known_outside_localities') or [])
    known = list(dict.fromkeys(known_gmina + known_outside + allowed))

    # 1) Parsed location field is strongest, but may be verbose ("Słona, gm. Zakliczyn...").
    loc_hits=_find_names(location or '', known)
    if loc_hits:
        # A specific village is stronger than the municipality name "Zakliczyn".
        specific=[x for x in loc_hits if fold(x)!='zakliczyn']
        chosen=(specific or loc_hits)[0]
        if chosen in allowed:
            return chosen, 'location'
        return None, 'explicit-outside-location'

    # 2) Title is strong and usually clean.
    title_hit=_first_hit(title or '', known)
    if title_hit:
        chosen=title_hit[0]
        if chosen in allowed:
            return chosen, 'title'
        return None, 'explicit-outside-title'

    # 3) Description: only trust the beginning. Recommendation widgets/footer text farther
    # down the DOM frequently contain unrelated towns.
    early=(description or '')[:3500]
    desc_hit=_first_hit(early, known)
    if desc_hit:
        chosen,pos=desc_hit
        if chosen in allowed:
            return chosen, 'description'
        # hard reject only when an outside locality is mentioned very early, where the
        # actual offer description/location normally lives
        if chosen in known_outside or chosen in known_gmina:
            return None, 'explicit-outside-description'

    # Unresolved is deliberately left for distance fallback instead of rejecting here.
    return None, 'unresolved'


def area_accepts(record: dict, area_cfg: dict, distance_km: float | None) -> tuple[bool, str | None, str]:
    mode = (area_cfg or {}).get('mode', 'radius')
    if mode != 'locality_whitelist':
        max_d=float(area_cfg.get('fallback_radius_km', 0) or 0)
        if max_d and distance_km is not None:
            return distance_km <= max_d, None, 'radius-mode'
        return True, None, 'radius-mode'

    locality, confidence = detect_allowed_locality(
        record.get('location'), record.get('title'), record.get('description'), area_cfg
    )
    if locality:
        return True, locality, confidence

    # Explicitly known outside localities are never rescued by geocoding.
    if confidence.startswith('explicit-outside'):
        return False, None, confidence

    fallback = float(area_cfg.get('fallback_radius_km', 0) or 0)
    if distance_km is not None and fallback > 0 and distance_km <= fallback:
        return True, None, 'distance-fallback'

    if area_cfg.get('reject_unknown_location', True):
        return False, None, 'outside-or-unresolved'
    return True, None, 'unknown-allowed'

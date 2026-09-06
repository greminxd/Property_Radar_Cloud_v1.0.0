from __future__ import annotations
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path


_REGISTRY_PATH = Path(__file__).resolve().parents[1] / 'data' / 'localities_zakliczyn.json'


def fold(s: str | None) -> str:
    s = (s or '').strip().lower().replace('ł', 'l')
    return ''.join(c for c in unicodedata.normalize('NFKD', s) if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def locality_registry() -> dict:
    try:
        data=json.loads(_REGISTRY_PATH.read_text(encoding='utf-8'))
        if isinstance(data,dict) and isinstance(data.get('localities'),list):
            return data
    except Exception:
        pass
    return {'meta':{},'localities':[],'outside_guards':[]}


def registry_localities() -> list[dict]:
    return [x for x in locality_registry().get('localities',[]) if isinstance(x,dict) and x.get('name')]


def registry_names(scopes: set[str] | None=None) -> list[str]:
    out=[]
    for item in registry_localities():
        if scopes is None or item.get('target_scope') in scopes:
            out.append(str(item['name']))
    return out


def registry_outside_names() -> list[str]:
    return [str(x['name']) for x in locality_registry().get('outside_guards',[]) if isinstance(x,dict) and x.get('name')]


def locality_aliases(name: str) -> list[str]:
    n = fold(name)
    aliases = {n}
    for item in registry_localities() + [x for x in locality_registry().get('outside_guards',[]) if isinstance(x,dict)]:
        if fold(item.get('name'))==n:
            aliases |= {fold(x) for x in (item.get('aliases') or []) if x}
            break
    return sorted((x for x in aliases if x), key=len, reverse=True)


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


def _configured_area(area_cfg: dict) -> tuple[list[str],list[str],list[str]]:
    # Registry is the canonical catalogue; config controls which entries are accepted.
    # Keeping config overrides means the target can be widened without code changes.
    reg_all=registry_names()
    primary=list(area_cfg.get('primary_localities') or registry_names({'primary'}))
    nearby=list(area_cfg.get('nearby_localities') or registry_names({'nearby'}))
    allowed=list(dict.fromkeys(primary+nearby))
    configured_known=list(area_cfg.get('known_gmina_localities') or [])
    known_gmina=list(dict.fromkeys(reg_all+configured_known+allowed))
    known_outside=list(dict.fromkeys(registry_outside_names()+list(area_cfg.get('known_outside_localities') or [])))
    return allowed,known_gmina,known_outside


def locality_record(name: str | None) -> dict | None:
    n=fold(name)
    if not n:return None
    for item in registry_localities():
        if any(n==a for a in locality_aliases(str(item.get('name') or ''))):
            return item
    return None


def _admin_guard(location: str | None,title: str | None,description: str | None) -> str | None:
    """Return a hard-reject reason when explicit administrative evidence is foreign."""
    strong_text=' '.join([location or '', title or '', (description or '')[:1200]])
    ft=fold(strong_text)
    # Stop at common separators so regexes do not swallow the next sentence/field.
    county_hits=re.findall(r'powiat\s+([a-ząćęłńóśźż -]{3,32}?)(?=\s*(?:[,;|•\n]|$|gmina\b|gm\.\b))', ft, re.I)
    if not county_hits:
        county_hits=re.findall(r'powiat\s+([a-ząćęłńóśźż-]{3,24})', ft, re.I)
    for county in county_hits:
        c=fold(county).strip(' ,.-')
        if c and not c.startswith('tarnowsk'):
            return 'explicit-outside-county'
    municipality_hits=re.findall(r'(?:gmina|gm\.)\s+([a-ząćęłńóśźż -]{3,28}?)(?=\s*(?:[,;|•\n]|$|powiat\b))', ft, re.I)
    if not municipality_hits:
        municipality_hits=re.findall(r'(?:gmina|gm\.)\s+([a-ząćęłńóśźż-]{3,22})', ft, re.I)
    for municipality in municipality_hits:
        g=fold(municipality).strip(' ,.-')
        if g and not g.startswith('zakliczyn'):
            return 'explicit-outside-gmina'
    return None


def _same_name_zakliczyn_guard(location: str | None, title: str | None, description: str | None) -> str | None:
    """Reject the other Małopolskie Zakliczyn (near Myślenice/Siepraw).

    Portals frequently expose only the city label ``Zakliczyn, Małopolskie``. That
    label is ambiguous: there is also a Zakliczyn near Myślenice. We therefore
    look for contextual phrases that disambiguate the advert without treating a
    casual mention of Myślenice as enough evidence on its own.
    """
    loc=fold(location)
    title_f=fold(title)
    desc_f=fold((description or '')[:1800])
    combined=' '.join([title_f,desc_f])
    # The guard matters only when the portal/location evidence is generic Zakliczyn.
    if 'zakliczyn' not in loc and 'zakliczyn' not in title_f:
        return None
    patterns=(
        r'zakliczyn(?:ie)?\s*[/,;()\-]*\s*(?:kolo|okolice|k\.?)\s+myslenic',
        r'(?:kolo|okolice|k\.?)\s+myslenic',
        r'powiat\s+myslenick',
        r'(?:gmina|gm\.)\s+siepraw',
    )
    if any(re.search(p,combined,re.I) for p in patterns):
        return 'explicit-outside-same-name-zakliczyn-myslenice'
    return None


def detect_allowed_locality(location: str | None, title: str | None, description: str | None, area_cfg: dict) -> tuple[str | None, str]:
    """Resolve locality against one canonical registry and explicit admin evidence.

    Order of trust:
    1. explicit foreign powiat/gmina => hard reject,
    2. specific known village in title/structured location,
    3. early description,
    4. optional distance fallback handled by ``area_accepts``.

    Known villages in gmina Zakliczyn that are not part of the configured target scope are
    recognized and rejected deliberately rather than being treated as an unknown geocode.
    """
    allowed,known_gmina,known_outside=_configured_area(area_cfg)
    known=list(dict.fromkeys(known_gmina+known_outside+allowed))

    admin_reject=_admin_guard(location,title,description)
    if admin_reject:
        return None,admin_reject
    same_name_reject=_same_name_zakliczyn_guard(location,title,description)
    if same_name_reject:
        return None,same_name_reject

    # Gather evidence from each strong field. A specific village beats generic Zakliczyn.
    loc_hits=_find_names(location or '',known)
    title_hits=_find_names(title or '',known)
    early=(description or '')[:1200]
    desc_hits=_find_names(early,known)

    def specific(hits):
        return [x for x in hits if fold(x)!='zakliczyn']

    # If structured location says only Zakliczyn but title names Lusławice/Słona/etc.,
    # choose the specific title locality. This is common on OLX and portal category pages.
    candidates=[]
    if specific(loc_hits): candidates.append((specific(loc_hits)[0],'location'))
    if specific(title_hits): candidates.append((specific(title_hits)[0],'title'))
    if candidates:
        first=candidates[0]
        # Conflicting strong locality evidence is suspicious; do not let a generic
        # geocode rescue it. Same-name evidence is fine.
        strong_names={fold(x[0]) for x in candidates}
        if len(strong_names)>1:
            return None,'conflicting-locality-evidence'
        chosen,src=first
        if chosen in allowed:return chosen,src
        if chosen in known_gmina:return None,'known-gmina-outside-target'
        return None,'explicit-outside-'+src

    # Generic Zakliczyn is accepted only if there is no contradictory specific evidence.
    if loc_hits:
        chosen=loc_hits[0]
        if chosen in allowed:return chosen,'location-generic'
        if chosen in known_gmina:return None,'known-gmina-outside-target'
        return None,'explicit-outside-location'
    if title_hits:
        chosen=title_hits[0]
        if chosen in allowed:return chosen,'title'
        if chosen in known_gmina:return None,'known-gmina-outside-target'
        return None,'explicit-outside-title'

    desc_hit=_first_hit(early,known)
    if desc_hit:
        chosen,_=desc_hit
        if chosen in allowed:return chosen,'description'
        if chosen in known_gmina:return None,'known-gmina-outside-target'
        if chosen in known_outside:return None,'explicit-outside-description'

    # If an explicit "gmina Zakliczyn" is present but the village is not in the complete
    # registry, reject rather than inventing a locality. This catches parser typos/data drift.
    ft=fold(' '.join([location or '',title or '',early]))
    if re.search(r'(?:gmina|gm\.)\s+zakliczyn\b',ft) and not _find_names(ft,known_gmina):
        return None,'unknown-locality-in-target-gmina'

    return None,'unresolved'


def area_accepts(record: dict, area_cfg: dict, distance_km: float | None) -> tuple[bool, str | None, str]:
    mode = (area_cfg or {}).get('mode', 'radius')
    # OLX API exposes a structured city. When it is present, it is stronger than
    # any word found in description. An unknown foreign city must never be rescued
    # by a target-village word such as "Słona" appearing in normal prose.
    if mode == 'locality_whitelist' and record.get('source') == 'OLX' and str(record.get('location_confidence') or '').startswith('olx-api-structured'):
        allowed,known_gmina,known_outside=_configured_area(area_cfg)
        structured_hits=_find_names(record.get('location') or '', list(dict.fromkeys(allowed+known_gmina+known_outside)))
        if not structured_hits and (record.get('location') or '').strip():
            return False, None, 'olx-structured-location-outside-target'
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

    # Any registry/admin conflict is decisive and can never be rescued by coordinates.
    hard_reject_prefixes=('explicit-outside','known-gmina-outside-target','conflicting-locality','unknown-locality-in-target-gmina')
    if confidence.startswith(hard_reject_prefixes):
        return False, None, confidence

    fallback = float(area_cfg.get('fallback_radius_km', 0) or 0)
    if distance_km is not None and fallback > 0 and distance_km <= fallback:
        return True, None, 'distance-fallback'

    if area_cfg.get('reject_unknown_location', True):
        return False, None, 'outside-or-unresolved'
    return True, None, 'unknown-allowed'

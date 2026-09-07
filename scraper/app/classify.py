from __future__ import annotations
import re
from .utils import asciifold

TYPE_RULES = [
    ("budowlano-rolno-leśna", ["budowlano-rolno-les", "rolno-budowlano-les"]),
    ("rolno-budowlana", ["rolno-budow", "budowlano-rol", "budowlana rolna", "rolna budowlana"]),
    ("budowlana", ["dzialka budowl", "teren budowl", "pod zabudowe"]),
    ("siedliskowa", ["siedlisk"]),
    ("rekreacyjna", ["rekreacyjn"]),
    ("leśna", ["dzialka lesna", "grunt lesny"]),
    ("rolna", ["dzialka rolna", "grunt rolny", "pole rolne"]),
    ("inwestycyjna", ["inwestycyjn", "pod inwestycje"]),
    ("usługowa", ["uslugow", "komercyjn"]),
]



def olx_url_cid(url: str) -> int | None:
    """Return the category id embedded in a public OLX advert URL (``-CID3-``)."""
    m=re.search(r'-CID(\d+)(?:-|$)', url or '', re.I)
    return int(m.group(1)) if m else None


def classify_category(title: str, url: str, text: str, category_hint: str | None = None) -> str:
    """Property Radar monitors LAND ONLY. Strong title/URL signals win.

    Generic words must not be enough. In particular ``grunt`` used to match
    ``gruntowy`` in fishing adverts, which could turn a reel into a land plot.
    """
    t=asciifold(title or '')
    u=asciifold(url or '')
    # OLX exposes the category in public advert URLs. Any explicit non-real-estate
    # category is a hard reject even if a caller supplied a stale plot hint.
    cid=olx_url_cid(url)
    if 'olx.pl' in u and cid is not None and cid != 3:
        return 'other'
    if any(k in t for k in ["garaz", "miejsce postojowe", "parking", "boks garazowy"]):
        return "other"
    if any(k in u for k in ["/garaz", "/garaze", "garaz-na-sprzedaz", "garaze-parkingi"]):
        return "other"

    title_land=bool(
        re.search(r'\bdzialk[a-z]*\b',t)
        or re.search(r'\bparcela(?:\b|[a-z])',t)
        or re.search(r'\bgrunt\b(?:\s+(?:roln|budowl|inwest|lesn|uslug|na\s+sprzedaz))?',t)
        or re.search(r'\bteren\b.{0,28}\bbudowl',t)
        or 'pole na sprzedaz' in t
    )
    if re.search(r"(?:^|[^a-z0-9])(dom|domek|willa|mieszkanie|apartament|lokal|kamienica|pensjonat|hala|magazyn)(?:[^a-z0-9]|$)", t):
        if not title_land:
            return "other"
    if title_land:
        return "plot"
    if any(k in u for k in ["/dzialka", "/dzialki", "dzialka-na-sprzedaz", "dzialki-grunty"]):
        return "plot"

    # Text fallback uses land-specific phrases only. Bare ``grunt`` is deliberately
    # forbidden because it is common in fishing/agricultural-product vocabulary.
    x=asciifold((text or '')[:4500])

    # Strong structured/page evidence for flats, houses or commercial units must win
    # over a broad search/category hint. This fixes OLX results such as "3 pokoje
    # 50,79 m²" that were discovered by a locality query and previously inherited
    # the caller's `plot` hint.
    non_plot_signals=[
        "liczba pokoi", "rodzaj zabudowy", "poziom:", "pietro:",
        "umeblowane:", "powierzchnia uzytkowa", "mieszkanie o powierzchni",
        "lokal mieszkalny", "salon z aneksem", "sypialnia",
    ]
    if any(k in x for k in non_plot_signals):
        # A genuine land advert can mention a future house/rooms in prose, therefore
        # retain it only when the *title* itself clearly identifies land.
        if not title_land:
            return "other"

    strong_text_signals=[
        "powierzchnia dzialki", "powierzchnia gruntu", "numer dzialki",
        "dzialka budowl", "dzialka rol", "dzialka lesn", "dzialki budowl",
        "grunt roln", "grunt budowl", "grunt inwest", "grunt lesn",
        "rodzaj dzialki", "warunki zabudowy", "miejscowy plan zagospodarowania",
    ]
    if any(k in x for k in strong_text_signals):
        return "plot"
    # A category hint is now only a weak fallback and cannot override obvious
    # apartment/building parameters above.
    if category_hint == "plot":
        return "plot"
    return "other"


def classify_plot_type(title: str, text: str) -> str:
    x = asciifold(title + " " + text)
    for label, terms in TYPE_RULES:
        if any(t in x for t in terms):
            return label
    return "nieustalona"


def planning_status(text: str) -> str:
    x = asciifold(text)
    has_no_plan = ("brak mpzp" in x or "bez planu" in x or re.search(r"nie jest objet.{0,30}plan", x))
    if re.search(r"\bmpzp\b|miejscow.{0,30}plan", x) and not has_no_plan:
        return "MPZP"
    if any(k in x for k in ["wydane warunki zabudowy", "wydane wz", "wydana decyzja o wz", "decyzja wz", "prawomocne wz", "wz wydane", "posiada wz", "ma wz"]):
        return "wydane WZ"
    if any(k in x for k in ["w trakcie uzyskiwania wz", "wniosek o wz", "wz w trakcie"]) or re.search(r"zlozon.{0,30}wz", x):
        return "WZ w trakcie"
    if any(k in x for k in ["brak warunkow zabudowy", "bez wz"]):
        return "brak WZ"
    if has_no_plan:
        return "brak MPZP / WZ nieustalone"
    return "nieustalone"


def parcel_number(text: str) -> str | None:
    x = text or ""
    patterns = [r"(?:nr|numer)\s*(?:dzialki|dz\.?|parceli)?\s*[:#]?\s*(\d{1,5}(?:/\d{1,5})?)",
                r"dzialka\s+(\d{1,5}(?:/\d{1,5})?)"]
    xf = asciifold(x)
    for p in patterns:
        m = re.search(p, xf, re.I)
        if m:
            return m.group(1)
    return None



def is_rental_offer(title: str = '', description: str = '', params_text: str = '', url: str = '') -> bool:
    """Hard reject rentals/leases. Property Radar is sale-only."""
    t=asciifold(' '.join([title or '', params_text or '', url or '']))
    d=asciifold((description or '')[:5000])
    hard=[
        r'\bdo wynajecia\b', r'\bna wynajem\b', r'\bwynajem\b', r'\bwynajme\b',
        r'\bnajem\b', r'\bdzierzaw[ayie]\b', r'\bdo dzierzawy\b',
        r'\bczynsz\b', r'\bz[lł]\s*/\s*mies', r'\bz[lł]\s*miesiecznie\b',
        r'\bmiesiecznie\b', r'\bza miesiac\b', r'\bmies\.?\s*/\s*mc\b'
    ]
    if any(re.search(p,t,re.I) for p in hard): return True
    desc_hard=[r'\boferta wynajmu\b',r'\bprzedmiotem wynajmu\b',r'\bdo wynajecia\b',r'\bna wynajem\b',r'\bczynsz.{0,40}(?:zl|pln)',r'(?:zl|pln).{0,15}/\s*mies']
    return any(re.search(p,d,re.I) for p in desc_hard)

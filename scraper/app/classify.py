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

PRIV_POS = {
    "brak sasiadow": 4, "bez sasiadow": 4, "na uboczu": 4, "ostatni dom": 4,
    "przy lesie": 3, "otoczeniu lasu": 3, "graniczy z lasem": 3, "wlasny las": 3,
    "pola": 2, "wsrod pol": 3, "cisza": 1, "spokojna okolica": 1,
    "luzna zabudowa": 3, "z dala od zabudowan": 4, "samotn": 3, "odosobn": 3,
    "droga prywatna": 1, "widokowa": 1
}
PRIV_NEG = {
    "osiedle": -3, "zwarta zabudowa": -4, "miedzy domami": -4,
    "nowe osiedle": -4, "zabudowa szeregowa": -5, "gesta zabudowa": -5,
    "centrum": -1
}


def classify_category(title: str, url: str, text: str, category_hint: str | None = None) -> str:
    """Classify from strong signals first.

    Old version looked for the word 'garaż' in the first page text. Portal navigation
    itself contains 'Garaże', so genuine plot pages were sometimes labelled GARAZ.
    """
    t=asciifold(title or '')
    u=asciifold(url or '')
    if any(k in t for k in ["dzialka", "grunt", "parcela", "pole na sprzedaz"]):
        return "plot"
    if any(k in t for k in ["garaz", "miejsce postojowe", "parking", "boks garazowy"]):
        return "garage"
    # Strong title signal wins over a broad/misclassified search result. The radar
    # is intentionally plots + garages; houses/flats/commercial premises must not
    # leak in just because a portal returned them on a land-results page.
    if re.search(r"(?:^|[^a-z0-9])(dom|domek|willa|mieszkanie|apartament|lokal|kamienica|pensjonat|hala|magazyn)(?:[^a-z0-9]|$)", t):
        return "other"
    if any(k in u for k in ["/dzialka", "/dzialki", "dzialka-na-sprzedaz", "dzialki-grunty"]):
        return "plot"
    if any(k in u for k in ["/garaz", "/garaze", "garaz-na-sprzedaz", "garaze-parkingi"]):
        return "garage"
    if category_hint in {"plot", "garage"}:
        return category_hint
    # Weak body signal is last resort only.
    x=asciifold((text or '')[:2200])
    plot_score=sum(k in x for k in ["powierzchnia dzialki", "dzialka budowl", "grunt", "warunki zabudowy", "mpzp"])
    garage_score=sum(k in x for k in ["garaz", "miejsce postojowe", "parking podziemny", "boks garazowy"])
    return "garage" if garage_score > plot_score + 1 else "plot"


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


def privacy_score(text: str, area_m2: float | None) -> tuple[int, list[str]]:
    x = asciifold(text)
    raw = 4.0
    reasons=[]
    for term, val in PRIV_POS.items():
        if term in x:
            raw += val
            reasons.append("+" + term)
    for term, val in PRIV_NEG.items():
        if term in x:
            raw += val
            reasons.append(term)
    if area_m2:
        if area_m2 >= 10000: raw += 3
        elif area_m2 >= 5000: raw += 2
        elif area_m2 >= 3000: raw += 1
        elif area_m2 < 1000: raw -= 1
    return max(1, min(10, round(raw))), reasons[:6]

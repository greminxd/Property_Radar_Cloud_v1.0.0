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



def classify_category(title: str, url: str, text: str, category_hint: str | None = None) -> str:
    """Property Radar monitors LAND ONLY. Strong title/URL signals win.

    Garage/parking listings and houses/flats/commercial objects are deliberately
    classified as ``other`` so they can never leak into alerts or Mini App.
    """
    t=asciifold(title or '')
    u=asciifold(url or '')
    if any(k in t for k in ["garaz", "miejsce postojowe", "parking", "boks garazowy"]):
        return "other"
    if any(k in u for k in ["/garaz", "/garaze", "garaz-na-sprzedaz", "garaze-parkingi"]):
        return "other"
    if re.search(r"(?:^|[^a-z0-9])(dom|domek|willa|mieszkanie|apartament|lokal|kamienica|pensjonat|hala|magazyn)(?:[^a-z0-9]|$)", t):
        # Exception: a land listing may say "działka z domem"; explicit land words win.
        if not any(k in t for k in ["dzialka", "grunt", "parcela", "pole na sprzedaz"]):
            return "other"
    if any(k in t for k in ["dzialka", "grunt", "parcela", "pole na sprzedaz"]):
        return "plot"
    if any(k in u for k in ["/dzialka", "/dzialki", "dzialka-na-sprzedaz", "dzialki-grunty"]):
        return "plot"
    if category_hint == "plot":
        return "plot"
    x=asciifold((text or '')[:4500])
    plot_score=sum(k in x for k in ["powierzchnia dzialki", "dzialka budowl", "dzialka rol", "grunt", "warunki zabudowy", "mpzp", "numer dzialki"])
    return "plot" if plot_score >= 1 else "other"


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


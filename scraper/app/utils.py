from __future__ import annotations
import hashlib, math, re, unicodedata
from urllib.parse import urljoin, urldefrag


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def asciifold(s: str) -> str:
    # NFKD nie rozkłada polskiego ł/Ł, więc mapujemy je ręcznie.
    s = (s or "").translate(str.maketrans({"ł":"l","Ł":"L"}))
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower()


def canonical_url(base: str, href: str) -> str:
    u = urljoin(base, href)
    u, _ = urldefrag(u)
    return u.split("?")[0].rstrip("/")


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2-lat1)
    dl = math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))


def parse_number(text: str) -> float | None:
    if not text:
        return None
    t = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def parse_price(text: str) -> float | None:
    # picks currency-looking number, tolerates spaces/NBSP
    m = re.search(r"(\d[\d\s\xa0.,]{1,18})\s*(?:zł|PLN)\b", text or "", re.I)
    if not m:
        return None
    raw = m.group(1).replace("\xa0", " ").strip()
    raw = re.sub(r"\s+", "", raw)
    # prices are normally integers; remove decimal punctuation conservatively
    if raw.count(",") == 1 and len(raw.split(",")[-1]) <= 2:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(".", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def area_candidates(text: str) -> list[tuple[float, str, str]]:
    out = []
    t = text or ""
    # hectares
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*ha\b", t, re.I):
        v = float(m.group(1).replace(",", ".")) * 10000
        out.append((v, "ha", m.group(0)))
    # ares/arów
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(?:a|ar|ara|ary|arów|arow)\b", t, re.I):
        v = float(m.group(1).replace(",", ".")) * 100
        if 50 <= v <= 500000:
            out.append((v, "ar", m.group(0)))
    # m2 / m²
    for m in re.finditer(r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*m(?:²|2)\b", t, re.I):
        raw = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
        try:
            v = float(raw)
            if 5 <= v <= 5000000:
                out.append((v, "m2", m.group(0)))
        except ValueError:
            pass
    return out


def best_area(text: str) -> tuple[float | None, str | None]:
    cands = area_candidates(text)
    if not cands:
        return None, None
    values = sorted(set(round(v, 1) for v, _, _ in cands))
    # Typowy błąd: portal pokazuje np. 15 m², a opis mówi 15 ar = 1500 m².
    if values and values[0] < 100 and values[-1] >= 500 and values[-1] / max(values[0], 1) >= 50:
        winner = values[-1]
        raw = next(raw for v, _, raw in cands if round(v, 1) == winner)
        return float(winner), raw
    first = cands[:10]
    scores = {}
    raws = {}
    for v, unit, raw in cands:
        key = round(v, 1)
        scores[key] = scores.get(key, 0) + 1
        raws[key] = raw
    for i, (v, unit, raw) in enumerate(first):
        scores[round(v,1)] = scores.get(round(v,1),0) + max(0, 5-i*0.3)
    winner = max(scores, key=scores.get)
    return float(winner), raws[winner]


def area_warning(text: str) -> str | None:
    vals = sorted(set(round(v, 1) for v, _, _ in area_candidates(text)))
    if len(vals) >= 2 and vals[-1] / max(vals[0], 1) >= 20:
        return "⚠️ Sprzeczne metraże w ogłoszeniu: " + ", ".join(f"{v:g} m²" for v in vals[:5])
    return None


def _normalize_phone_digits(raw: str) -> str | None:
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("48"):
        d = d[2:]
    if len(d) != 9 or d.startswith("000"):
        return None
    return d


def phone_candidates(text: str) -> list[tuple[int, str, str]]:
    """Return scored Polish phone candidates from visible listing text.

    Scores prefer numbers near seller/contact words and heavily penalize portal
    support/footer contexts. This is intentionally conservative: a missing phone is
    better than displaying a portal hotline as the seller's number.
    """
    t = text or ""
    out = []
    rx = re.compile(r"(?<!\d)(?:\+?48[\s().-]?)?(\d{3}[\s().-]?\d{3}[\s().-]?\d{3})(?!\d)")
    for m in rx.finditer(t):
        d = _normalize_phone_digits(m.group(0))
        if not d:
            continue
        lo=max(0,m.start()-140); hi=min(len(t),m.end()+140)
        ctx=asciifold(t[lo:hi])
        score=0
        for kw,w in {
            "telefon":4,"tel.":3,"tel ":3,"tel:":3,"kontakt":4,"zadzwo":5,"sprzedaj":3,
            "wlasciciel":4,"agent":2,"posrednik":2,"opiekun oferty":5,
        }.items():
            if kw in ctx: score+=w
        for kw,w in {
            "infolinia":-10,"pomoc":-5,"obsluga klienta":-8,"biuro obslugi":-8,
            "regulamin":-5,"polityka prywatnosci":-5,"reklam":-4,"newsletter":-4,
        }.items():
            if kw in ctx: score+=w
        out.append((score, d, t[lo:hi]))
    # keep best score per number
    best={}
    for score,d,ctx in out:
        if d not in best or score>best[d][0]: best[d]=(score,d,ctx)
    return sorted(best.values(), key=lambda x:x[0], reverse=True)


def phone_from_text(text: str) -> str | None:
    cands=phone_candidates(text)
    if not cands:
        return None
    score,d,_=cands[0]
    # Generic body text must have at least weak contextual support.
    if score < 1:
        return None
    return f"{d[:3]} {d[3:6]} {d[6:]}"



def location_key(location: str) -> str:
    x=asciifold(location)
    known=["biesnik","palesnica","slona","zdonia","olszowa","wola stroska","dzierzaniny","gwozdziec","stroze","wesolow","zawada lanckoronska","jamna","konczyska","wroblowice","filipowice","fasciszowa","ruda kameralna","jastrzebia","siemiechow","brzozowa","roztoka-brzeziny","borowa","zakliczyn"]
    for n in known:
        if n in x:
            return n
    return x[:100]

def fingerprint(title: str, location: str, area: float | None, price: float | None, parcel: str | None) -> str:
    """Stable cross-portal identity. Price is intentionally NOT part of it.

    A price cut must stay attached to the same property, and two portals may list the
    same plot at slightly different prices. Parcel number wins when available.
    """
    loc = location_key(location)
    if parcel:
        raw = f"parcel|{loc}|{parcel}"
    else:
        t=asciifold(title or "")
        words=[w for w in re.findall(r"[a-z0-9]+",t) if w not in {"sprzedam","sprzedaz","dzialka","dzialke","na","w","i","do","gm","gmina","powiat","tarnowski"}]
        signature=" ".join(words[:12])
        raw = f"geo|{loc}|{round(area or 0, -1)}|{signature}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

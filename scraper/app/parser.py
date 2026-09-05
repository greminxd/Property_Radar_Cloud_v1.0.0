from __future__ import annotations
import json, re
from bs4 import BeautifulSoup
from .utils import clean_text, parse_price, best_area, area_warning, phone_from_text, phone_candidates, canonical_url, asciifold
from .classify import classify_category, classify_plot_type, planning_status, parcel_number

# Main villages in gmina Zakliczyn plus a few nearby names that regularly appear in searches.
KNOWN_LOCALITIES = [
    "Bieśnik","Borowa","Charzewice","Dzierżaniny","Faliszewice","Faściszowa","Filipowice",
    "Gwoździec","Jamna","Kończyska","Lusławice","Melsztyn","Olszowa","Paleśnica","Roztoka",
    "Ruda Kameralna","Słona","Stróże","Wesołów","Wola Stróska","Wróblowice","Zakliczyn",
    "Zawada Lanckorońska","Zdonia",
    # frequent false-positive nearby locations — recognizing them lets the area filter reject them
    "Milówka","Złota","Siemiechów","Jastrzębia","Gromnik","Wojnicz","Czchów","Domosławice",
]

LOCATION_PATTERNS = [
    r"(?:lokalizacja|położona|polozona|położony|polozony)(?:\s+jest)?(?:\s+w|\s*:)?\s+([A-ZŁŚŻŹĆŃÓĘĄ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ -]{2,45})",
    r"([A-ZŁŚŻŹĆŃÓĘĄ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ -]{2,35}),\s*(?:gmina|gm\.)\s*Zakliczyn",
    r"(?:miejscowość|miejscowosc|wieś|wies)\s*[:\-]?\s*([A-ZŁŚŻŹĆŃÓĘĄ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ -]{2,35})",
]


def jsonld_objects(soup):
    objs=[]
    for tag in soup.find_all("script", attrs={"type":"application/ld+json"}):
        try:
            data=json.loads(tag.string or tag.get_text())
            if isinstance(data,list): objs.extend(data)
            elif isinstance(data,dict):
                if isinstance(data.get("@graph"),list): objs.extend(data["@graph"])
                objs.append(data)
        except Exception:
            pass
    return objs


def _find_meta(soup, *names):
    for name in names:
        tag=soup.find("meta", attrs={"property":name}) or soup.find("meta", attrs={"name":name})
        if tag and tag.get("content"): return clean_text(tag.get("content"))
    return ""


def _norm_phone(raw: str) -> str | None:
    d=re.sub(r"\D","",raw or "")
    if len(d)==11 and d.startswith("48"): d=d[2:]
    if len(d)!=9: return None
    return f"{d[:3]} {d[3:6]} {d[6:]}"


def _phone_from_soup(soup: BeautifulSoup, body_text: str) -> str | None:
    # Score tel: links by their surrounding seller/contact context.
    candidates=[]
    for a in soup.select('a[href^="tel:"]'):
        ph=_norm_phone(a.get("href","")[4:])
        if not ph: continue
        parent=a
        for _ in range(3):
            if getattr(parent,"parent",None) is None: break
            parent=parent.parent
        ctx=clean_text(parent.get_text(" ",strip=True) if parent else a.get_text(" ",strip=True))
        f=asciifold(ctx)
        score=2
        if any(k in f for k in ["kontakt","telefon","zadzwo","sprzedaj","agent","posrednik","opiekun"]): score+=5
        if any(k in f for k in ["infolinia","pomoc","obsluga klienta","regulamin","polityka prywatnosci"]): score-=12
        ancestors=list(a.parents)
        if any(getattr(x,"name",None) in {"main","article"} for x in ancestors): score+=3
        if any(getattr(x,"name",None)=="footer" for x in ancestors): score-=10
        candidates.append((score,ph))
    if candidates:
        candidates.sort(reverse=True)
        if candidates[0][0] >= 3:
            return candidates[0][1]
    return phone_from_text(body_text)


def _specific_locality(title: str, desc: str, structured_loc: str) -> tuple[str, str]:
    """Choose the most specific locality with a confidence source label."""
    # Structured locality is good unless it is only the generic municipality.
    sl=asciifold(structured_loc)
    if structured_loc and sl not in {"zakliczyn","gmina zakliczyn","tarnowski","powiat tarnowski","malopolskie"}:
        return clean_text(structured_loc), "structured"

    # Title is strong: portals often title offers "Działka Słona" even when JSON-LD says Zakliczyn.
    tf=asciifold(title)
    for name in KNOWN_LOCALITIES:
        if re.search(rf"(?<![a-z0-9]){re.escape(asciifold(name))}(?![a-z0-9])", tf):
            return name, "title"

    # Look at the first part of the actual description, before footer/recommendation noise.
    df=asciifold((desc or "")[:5000])
    hits=[]
    for name in KNOWN_LOCALITIES:
        if re.search(rf"(?<![a-z0-9]){re.escape(asciifold(name))}(?![a-z0-9])", df):
            hits.append(name)
    # Prefer a specific village over generic Zakliczyn when both occur.
    specific=[h for h in hits if asciifold(h)!="zakliczyn"]
    if len(set(specific))==1:
        return specific[0], "description"
    if not specific and "Zakliczyn" in hits:
        return "Zakliczyn", "description"

    if structured_loc:
        return clean_text(structured_loc), "structured-generic"
    return "", "none"


def parse_detail(html: str, url: str, source: str, category_hint: str | None = None):
    soup=BeautifulSoup(html,"lxml")
    objs=jsonld_objects(soup)
    body=clean_text(soup.get_text(" ", strip=True))
    title=_find_meta(soup,"og:title","twitter:title") or clean_text((soup.title.string if soup.title else ""))
    desc=_find_meta(soup,"og:description","description")
    if not desc:
        # Prefer visible main/article content over the whole footer-heavy page.
        main=soup.find("main") or soup.find("article")
        desc=clean_text(main.get_text(" ",strip=True))[:10000] if main else body[:7000]
    combined=clean_text(title+" "+desc+" "+body[:16000])

    price=None; structured_loc=""; published=""
    for o in objs:
        if not isinstance(o,dict): continue
        offers=o.get("offers")
        if isinstance(offers,dict) and price is None:
            try: price=float(str(offers.get("price")).replace(" ",""))
            except Exception: pass
        if not structured_loc:
            addr=o.get("address")
            if isinstance(addr,dict):
                # addressLocality is intentionally first and should not be polluted by region name.
                structured_loc=clean_text(str(addr.get("addressLocality") or ""))
                if not structured_loc:
                    structured_loc=clean_text(", ".join(str(addr.get(k,"")) for k in ["addressRegion"] if addr.get(k)))
            elif isinstance(addr,str): structured_loc=clean_text(addr)
        if not published: published=clean_text(str(o.get("datePosted") or o.get("datePublished") or ""))

    if price is None: price=parse_price(combined)
    area,_=best_area(combined)
    awarn=area_warning(combined)
    phone=_phone_from_soup(soup, desc+" "+body)
    if not published:
        dm=re.search(r"(?:Dodane|Opublikowano|Data dodania)\s*[:–-]?\s*([^|]{4,45})", combined, re.I)
        if dm: published=clean_text(dm.group(1))

    # Alternate metadata can improve structured location.
    if not structured_loc:
        for meta in ["geo.placename","place:location:locality"]:
            v=_find_meta(soup,meta)
            if v:
                structured_loc=v
                break
    loc,loc_conf=_specific_locality(title,desc,structured_loc)
    if not loc:
        for p in LOCATION_PATTERNS:
            m=re.search(p,combined,re.I)
            if m:
                loc=clean_text(m.group(1)); loc_conf="regex"; break

    cat=classify_category(title,url,combined,category_hint=category_hint)
    ptype=classify_plot_type(title,combined) if cat=="plot" else "garaż"
    plan=planning_status(combined) if cat=="plot" else "n/d"
    parcel=parcel_number(combined) if cat=="plot" else None
    image=_find_meta(soup,"og:image","twitter:image") or None

    return {
        "canonical_url": canonical_url(url,url), "source":source, "category":cat,
        "title": title[:500], "price":price, "area_m2":area,
        "price_m2": (price/area if price and area else None), "plot_type":ptype,
        "planning_status":plan, "location":loc[:250], "location_confidence":loc_conf,
        "phone":phone, "parcel_number":parcel, "published_text":published[:100],
        "area_warning":awarn, "description":desc[:12000], "image_url":image,
        "_jsonld":objs, "_body":combined,
    }


def refine_from_rendered_text(rec: dict, visible_text: str) -> dict:
    """Best-effort second pass over the text the browser actually rendered.

    Modern portals sometimes keep useful data out of the initial HTML/metadata and
    inject it with JavaScript. This pass runs after Playwright has clicked the phone
    reveal button. It is deliberately conservative to avoid picking a locality from
    recommended listings farther down the page.
    """
    text=clean_text(visible_text or "")
    if not text:
        return rec
    early=text[:6500]
    loc_fold=asciifold(rec.get("location") or "")
    if loc_fold in {"", "zakliczyn", "gmina zakliczyn", "powiat tarnowski", "tarnowski", "malopolskie"}:
        ef=asciifold(early)
        hits=[]
        for name in KNOWN_LOCALITIES:
            nf=asciifold(name)
            if re.search(rf"(?<![a-z0-9]){re.escape(nf)}(?![a-z0-9])", ef):
                hits.append(name)
        specific=list(dict.fromkeys(x for x in hits if asciifold(x)!="zakliczyn"))
        if len(specific)==1:
            rec["location"]=specific[0]
            rec["location_confidence"]="rendered-text"
        elif not specific and "Zakliczyn" in hits:
            rec["location"]="Zakliczyn"
            rec["location_confidence"]="rendered-text-generic"

    if not rec.get("phone"):
        rec["phone"]=phone_from_text(text)
    if rec.get("category")=="plot":
        if not rec.get("parcel_number"):
            rec["parcel_number"]=parcel_number(early)
        if rec.get("plot_type") in {None,"","nieustalona"}:
            rec["plot_type"]=classify_plot_type(rec.get("title") or "", early)
        if rec.get("planning_status") in {None,"","nieustalone"}:
            rec["planning_status"]=planning_status(early)
    if not rec.get("published_text"):
        dm=re.search(r"(?:Dodane|Opublikowano|Data dodania)\s*[:–-]?\s*([^|\n]{4,55})", early, re.I)
        if dm: rec["published_text"]=clean_text(dm.group(1))[:100]
    return rec

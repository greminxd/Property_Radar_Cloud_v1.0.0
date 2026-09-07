from __future__ import annotations
import json, re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .utils import clean_text, parse_price, best_area, area_warning, phone_from_text, phone_candidates, canonical_url, asciifold
from .classify import classify_category, classify_plot_type, planning_status, parcel_number
from .area import registry_names, registry_outside_names, normalize_voivodeship
from .listing_data import primary_entities, primary_payload, payload_entity, entity_coordinates
from .utils import PRICE_AMOUNT

# One canonical registry is shared by parser + area validator.  This avoids the old
# drift where config.py/parser.py/area.py each knew a slightly different village list.
KNOWN_LOCALITIES = list(dict.fromkeys(registry_names() + registry_outside_names()))
GENERIC_MUNICIPALITY_LOCALITIES = {'zakliczyn','gromnik','czchow'}

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



def embedded_json_objects(soup):
    """Parse JSON payloads used by JS-heavy portals, not only JSON-LD.

    OLX/Otodom in particular keep useful listing fields in application/json /
    __NEXT_DATA__ payloads. Invalid or huge non-JSON scripts are ignored.
    """
    out=[]
    for tag in soup.find_all("script"):
        typ=(tag.get("type") or "").lower(); ident=(tag.get("id") or "").lower()
        if typ not in {"application/json","application/ld+json"} and ident not in {"__next_data__","__nuxt_data__"}:
            continue
        raw=(tag.string or tag.get_text() or "").strip()
        if not raw or len(raw)>5_000_000: continue
        try:
            data=json.loads(raw)
            out.append(data)
        except Exception:
            pass
    return out

def _walk_dicts(obj):
    stack=[obj]
    while stack:
        x=stack.pop()
        if isinstance(x,dict):
            yield x
            stack.extend(x.values())
        elif isinstance(x,list): stack.extend(x)

def _embedded_first(payloads, keys):
    wanted={asciifold(k).replace('_','').replace('-','') for k in keys}
    for payload in payloads:
        for d in _walk_dicts(payload):
            for k,v in d.items():
                kk=asciifold(str(k)).replace('_','').replace('-','')
                if kk in wanted and v not in (None,"",[],{}):
                    return v
    return None

def _embedded_price(payloads):
    for payload in payloads:
        for key in ('totalPrice','priceValue','price'):
            value=payload.get(key)
            if isinstance(value,dict):
                if value.get('currency','PLN')!='PLN': continue
                value=value.get('value',value.get('amount'))
            try:
                number=float(str(value).replace(' ','').replace('\xa0','').replace(',','.'))
                if 100 <= number <= 1_000_000_000: return number
            except (TypeError,ValueError): pass
    return None

def _find_meta(soup, *names):
    for name in names:
        tag=soup.find("meta", attrs={"property":name}) or soup.find("meta", attrs={"name":name})
        if tag and tag.get("content"): return clean_text(tag.get("content"))
    return ""

def _image_url_from_value(value, base_url: str) -> str | None:
    """Return one usable listing-photo URL from common schema/API shapes."""
    if isinstance(value,str):
        raw=clean_text(value)
        if not raw or raw.startswith(('data:','blob:')): return None
        try: candidate=urljoin(base_url,raw)
        except Exception: return None
        if not re.match(r'^https?://',candidate,re.I): return None
        folded=asciifold(candidate)
        if any(x in folded for x in ('logo','avatar','sprite','icon','favicon','placeholder','tracking','pixel')): return None
        return candidate
    if isinstance(value,list):
        for item in value:
            got=_image_url_from_value(item,base_url)
            if got:return got
        return None
    if isinstance(value,dict):
        for key in ('contentUrl','url','src','link','original','large','medium','imageUrl','photoUrl','thumbnailUrl'):
            if key in value:
                got=_image_url_from_value(value.get(key),base_url)
                if got:return got
        for item in value.values():
            if isinstance(item,(dict,list)):
                got=_image_url_from_value(item,base_url)
                if got:return got
    return None


def _extract_listing_image(soup: BeautifulSoup, payloads, objs, base_url: str) -> str | None:
    # Social metadata on listing pages almost always points at the main offer photo.
    for meta in ('og:image:secure_url','og:image','twitter:image'):
        got=_image_url_from_value(_find_meta(soup,meta),base_url)
        if got:return got
    # JSON-LD ImageObject / image arrays are the next strongest source.
    for obj in objs or []:
        if isinstance(obj,dict):
            for key in ('image','photo','photos'):
                if key in obj:
                    got=_image_url_from_value(obj.get(key),base_url)
                    if got:return got
    # Main embedded payload from modern JS portals.
    for payload in payloads or []:
        for d in _walk_dicts(payload):
            for key in ('imageUrl','photoUrl','photos','images','image','thumbnailUrl'):
                if key in d:
                    got=_image_url_from_value(d.get(key),base_url)
                    if got:return got
    # Last resort: visible listing-area images. Avoid page chrome and recommendation noise.
    selectors='main img, article img, [data-testid*="gallery"] img, [data-cy*="gallery"] img, [class*="gallery"] img, [class*="Gallery"] img'
    for tag in soup.select(selectors):
        context=' '.join([str(tag.get('alt') or ''),str(tag.get('class') or ''),str(tag.get('id') or '')])
        cf=asciifold(context)
        if any(x in cf for x in ('logo','avatar','profil','icon','mapa','map','reklam','banner')): continue
        candidates=[]
        for attr in ('src','data-src','data-lazy-src','data-original'):
            if tag.get(attr): candidates.append(tag.get(attr))
        srcset=tag.get('srcset') or tag.get('data-srcset')
        if srcset:
            candidates.extend(part.strip().split(' ')[0] for part in str(srcset).split(',') if part.strip())
        for candidate in reversed(candidates):
            got=_image_url_from_value(candidate,base_url)
            if got:return got
    return None


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
    if structured_loc and sl not in GENERIC_MUNICIPALITY_LOCALITIES | {"gmina zakliczyn","gmina gromnik","gmina czchow","tarnowski","powiat tarnowski","brzeski","powiat brzeski","malopolskie"}:
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
    specific=[h for h in hits if asciifold(h) not in GENERIC_MUNICIPALITY_LOCALITIES]
    if len(set(specific))==1:
        return specific[0], "description"
    if not specific:
        for municipality in ("Zakliczyn","Gromnik","Czchów"):
            if municipality in hits:
                return municipality, "description"

    if structured_loc:
        return clean_text(structured_loc), "structured-generic"

    # Generic portal titles often expose an otherwise unknown village immediately
    # before an explicit county, e.g. "… – Olcha, powiat żuromiński". Keep that
    # locality instead of returning an empty value; area.py can then reject the
    # foreign county deterministically.
    m=re.search(r'(?:^|[-–—])\s*([A-ZŁŚŻŹĆŃÓĘĄ][A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż .-]{2,45}?)\s*,\s*powiat\b', title or '', re.I)
    if m:
        candidate=clean_text(m.group(1)).strip(' -–—,')
        if candidate:
            return candidate, "title-county"
    return "", "none"




_AREA_TOKEN = r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*(ha|m(?:²|2)|a|ar|ara|ary|arów|arow)\b"

def _area_token_to_m2(raw_number: str, unit: str) -> float | None:
    try:
        n=float((raw_number or '').replace('\xa0','').replace(' ','').replace(',','.'))
    except Exception:
        return None
    u=asciifold(unit or '')
    if u=='ha': n*=10000
    elif u in {'a','ar','ara','ary','arow'}: n*=100
    if 5 <= n <= 5_000_000:
        return float(n)
    return None

def _area_from_json_value(v) -> float | None:
    """Parse a JSON-LD area value without confusing floorSize with lotSize."""
    if isinstance(v,(int,float)):
        n=float(v); return n if 5 <= n <= 5_000_000 else None
    if isinstance(v,str):
        m=re.search(_AREA_TOKEN,v,re.I)
        if m:return _area_token_to_m2(m.group(1),m.group(2))
        try:
            n=float(v.replace(' ','').replace(',','.'));return n if 5<=n<=5_000_000 else None
        except:return None
    if isinstance(v,dict):
        raw=v.get('value') or v.get('maxValue') or v.get('minValue')
        unit=v.get('unitText') or v.get('unitCode') or ''
        if raw is None:return None
        # Schema.org often uses MTK = square metre.
        if str(unit).upper() in {'MTK','M2','M²','SQM'}:unit='m2'
        if str(unit).upper() in {'HAR','HA'}:unit='ha'
        if unit:
            return _area_token_to_m2(str(raw),str(unit))
        try:
            n=float(str(raw).replace(' ','').replace(',','.'));return n if 5<=n<=5_000_000 else None
        except:return None
    return None

def _jsonld_plot_area(objs) -> tuple[float | None,str | None]:
    """Find explicit land/lot area in JSON-LD. Never use floorSize here."""
    strong_keys={'lotsize','landarea','plotarea','parcelarea','lotarea','plotsize'}
    stack=list(objs or [])
    while stack:
        o=stack.pop(0)
        if isinstance(o,list):stack.extend(o);continue
        if not isinstance(o,dict):continue
        # additionalProperty / PropertyValue style: name=Powierzchnia działki, value=37000
        name=asciifold(str(o.get('name') or o.get('propertyID') or o.get('propertyId') or ''))
        if any(k in name for k in ['powierzchnia dzialki','powierzchnia gruntu','powierzchnia parceli','plot area','lot size','land area']):
            a=_area_from_json_value(o.get('value'))
            if a:return a,'jsonld-property'
        for k,v in o.items():
            kf=asciifold(str(k)).replace('_','').replace('-','')
            if kf in strong_keys:
                a=_area_from_json_value(v)
                if a:return a,f'jsonld:{k}'
            if isinstance(v,(dict,list)):stack.append(v)
    return None,None

def _explicit_plot_area(text: str) -> tuple[float | None,str | None,str | None]:
    """Prefer a labelled parcel/land area over generic areas from the title/body.

    Example that used to fail: 'DOM, 350m, 3.74ha' plus the actual field
    'Powierzchnia działki 37000 m²'. 350 m² is floor area and 3.74 ha may be a rounded
    headline value; the labelled parcel field must win.
    """
    t=text or ''
    patterns=[
        rf"(?:powierzchnia|pow\.?)\s+(?:działki|dzialki|gruntu|parceli)\s*[:\-]?\s*{_AREA_TOKEN}",
        rf"(?:działka|dzialka|grunt|parcela)\s+(?:o\s+)?(?:powierzchni|pow\.?)\s*[:\-]?\s*{_AREA_TOKEN}",
        # Portal rows such as: 'Działka: budowlana, 2 900 m²'
        rf"(?:działka|dzialka|grunt|parcela)\s*:\s*[^|;•]{{0,90}}?[,;]\s*{_AREA_TOKEN}",
    ]
    for i,p in enumerate(patterns):
        m=re.search(p,t,re.I)
        if not m:continue
        # _AREA_TOKEN contributes two final capture groups: number and unit
        num,unit=m.group(m.lastindex-1),m.group(m.lastindex)
        a=_area_token_to_m2(num,unit)
        if a:return a,f'label:{i+1}',clean_text(m.group(0))[:180]
    return None,None,None


def _total_price_from_text(text: str) -> float | None:
    """Extract total PLN price while explicitly ignoring price-per-m² labels."""
    vals=[]
    rx=re.compile(rf"(?<![\d.,])({PRICE_AMOUNT})\s*(?:zł|PLN)\b",re.I)
    for m in rx.finditer(text or ''):
        before=asciifold((text or '')[max(0,m.start()-28):m.start()])
        after=asciifold((text or '')[m.end():m.end()+18])
        # Reject only a ppm label immediately attached to this number. A previous
        # ppm field elsewhere in the same line must not make us reject the later
        # total price.
        ppm_label_before=bool(re.search(r'cena\s*(?:za|/)\s*m(?:2)?\s*[:\-]?\s*$',before))
        if ppm_label_before or re.match(r'\s*/\s*m(?:2|²)',after):
            continue
        n=parse_price(m.group(0))
        if n and 100 <= n <= 1_000_000_000:
            vals.append(n)
    if not vals:return None
    # On a focused listing body the total offer price is normally the largest PLN
    # amount; per-m² values were removed above.
    return max(vals)


def _price_m2_from_text(text: str) -> float | None:
    """Extract an explicitly labelled asking price per square metre.

    This value is first-class data. It must never be routed through ``parse_price``
    because e.g. ``97.22 zł/m²`` used to become ``9722 zł`` and poison market
    medians after another division by plot area.
    """
    t=text or ''
    patterns=[
        r"(?:cena\s*(?:za|/)\s*m(?:²|2)|cena\s+metra(?:\s+kwadratowego)?)\s*[:\-]?\s*(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*(?:zł|PLN)(?:\s*/\s*m(?:²|2))?",
        r"(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*(?:zł|PLN)\s*/\s*m(?:²|2)",
    ]
    for pat in patterns:
        m=re.search(pat,t,re.I)
        if not m: continue
        raw=m.group(1).replace('\xa0','').replace(' ','').replace(',','.')
        try:
            v=float(raw)
        except ValueError:
            continue
        if 0.1 <= v <= 100_000:
            return v
    return None


def _reconcile_price_fields(price: float | None, area: float | None, explicit_ppm: float | None):
    """Preserve the asking price; compute comparable ppm from price and land area."""
    ppm=explicit_ppm if explicit_ppm and explicit_ppm > 0 else None
    if area and area > 0 and price and price > 0:
        derived=float(price)/float(area)
    else:
        derived=None
    if derived is not None:
        return price, derived
    if ppm is None:
        return price, derived
    if area and area > 0:
        expected=float(ppm)*float(area)
        if price is None or price <= 0:
            price=expected
    return price, ppm


def _sprzedajemy_plot_area(text: str) -> float | None:
    m=re.search(rf"\bPowierzchnia\s*[:\-]?\s*{_AREA_TOKEN}",text or '',re.I)
    if not m:return None
    return _area_token_to_m2(m.group(m.lastindex-1),m.group(m.lastindex))


def _strong_price_from_soup(soup: BeautifulSoup) -> float | None:
    """Prefer listing-level structured price over arbitrary currency in page chrome."""
    for tag in [
        soup.find("meta", attrs={"property":"product:price:amount"}),
        soup.find("meta", attrs={"itemprop":"price"}),
        soup.find(attrs={"itemprop":"price"}),
    ]:
        if not tag:
            continue
        raw=tag.get("content") or tag.get("value") or tag.get_text(" ",strip=True)
        if not raw:
            continue
        t=clean_text(str(raw))
        # Feed a synthetic PLN suffix to the mature price parser first.
        n=parse_price(t+" zł")
        if n and 100 <= n <= 1_000_000_000:
            return n
        try:
            n=float(t.replace('\xa0','').replace(' ','').replace('.','').replace(',','.'))
            if 100 <= n <= 1_000_000_000:return n
        except Exception:pass
    return None


def _sprzedajemy_date(soup: BeautifulSoup, text: str) -> str:
    """Sprzedajemy often exposes publication as '03 Maj 07:27' without a label/year."""
    # A listing-level <time datetime> is the strongest source.
    for t in soup.find_all('time', limit=4):
        raw=t.get('datetime') or clean_text(t.get_text(' ',strip=True))
        if raw and re.search(r'\d',str(raw)):
            return clean_text(str(raw))[:100]
    m=re.search(r'\b(\d{1,2}\s+(?:Sty|Lut|Mar|Kwi|Maj|Cze|Lip|Sie|Wrz|Pa[zź]|Lis|Gru)[a-ząćęłńóśźż]*\s+\d{1,2}:\d{2})\b',text or '',re.I)
    return clean_text(m.group(1))[:100] if m else ''


def parse_detail(html: str, url: str, source: str, category_hint: str | None = None):
    soup=BeautifulSoup(html,"lxml")
    objs=primary_entities(jsonld_objects(soup),url)
    payload=primary_payload(embedded_json_objects(soup),url)
    payloads=[payload] if payload else []
    entity=payload_entity(payload)
    if entity: objs.insert(0,entity)
    for noise in soup.select('aside, footer, nav, [data-testid*="recommend"], [data-cy*="recommend"], [data-testid*="similar"], [data-cy*="similar"]'):
        noise.decompose()
    body=clean_text(soup.get_text(" ", strip=True))
    meta_title=_find_meta(soup,"og:title","twitter:title") or clean_text((soup.title.string if soup.title else ""))
    h1=clean_text((soup.find("h1").get_text(" ",strip=True) if soup.find("h1") else ""))
    title=h1 if h1 and len(h1)>=8 else meta_title
    if payload.get('title'): title=clean_text(str(payload['title']))
    main=soup.find("main") or soup.find("article")
    main_text=clean_text(main.get_text(" ",strip=True))[:9000] if main else body[:6000]
    desc=_find_meta(soup,"og:description","description")
    description_node=soup.select_one('[data-cy="ad_description"], [data-testid="ad.description"], [data-testid="ad-description"]')
    if payload.get('description'):
        desc=clean_text(BeautifulSoup(str(payload['description']),'lxml').get_text(' ',strip=True))
    elif description_node:
        desc=clean_text(description_node.get_text(' ',strip=True))
    if not desc:
        desc=main_text[:8000] if main_text else body[:5000]
    # Keep core-field parsing tightly scoped to the listing itself. The old parser
    # searched up to 16k of the whole page and could steal a price/area from a
    # recommended listing below the actual offer (e.g. Morizon 3200 -> 14643 m²).
    focused=clean_text(title+" "+desc[:4500]+" "+main_text[:6500])
    combined=focused

    price=None; structured_loc=""; structured_region=""; published=""; updated=""
    for o in objs:
        if not isinstance(o,dict): continue
        offers=o.get("offers") or (o if o.get('@type')=='Offer' else None)
        if isinstance(offers,dict) and price is None:
            try:
                if offers.get('priceCurrency','PLN')=='PLN':
                    price=float(str(offers.get("price")).replace(" ","").replace('\xa0','').replace(',','.'))
            except Exception: pass
        # JSON-LD providers place PostalAddress in several shapes. OLX/Otodom may
        # nest it under itemOffered, while other portals put it directly on the Offer.
        # Read both; otherwise a foreign province can be lost before geo validation.
        address_nodes=[o]
        for key in ("itemOffered","mainEntity","location"):
            node=o.get(key)
            if isinstance(node,dict):
                address_nodes.append(node)
                nested=node.get("location")
                if isinstance(nested,dict): address_nodes.append(nested)
        for node in address_nodes:
            addr=node.get("address") if isinstance(node,dict) else None
            if isinstance(addr,dict):
                if not structured_loc:
                    # addressLocality is intentionally first and should not be polluted by region name.
                    structured_loc=clean_text(str(addr.get("addressLocality") or ""))
                    if not structured_loc:
                        structured_loc=clean_text(", ".join(str(addr.get(k,"")) for k in ["addressRegion"] if addr.get(k)))
                if not structured_region:
                    structured_region=clean_text(str(addr.get("addressRegion") or ""))
            elif isinstance(addr,str) and not structured_loc:
                structured_loc=clean_text(addr)
        if not published: published=clean_text(str(o.get("datePosted") or o.get("datePublished") or ""))
        if not updated: updated=clean_text(str(o.get("dateModified") or ""))

    if price is None: price=_embedded_price(payloads)
    if price is None: price=_strong_price_from_soup(soup)
    # Total asking price and price/m² are different fields. Always exclude labelled
    # ppm values before falling back to the generic price parser.
    if price is None: price=_total_price_from_text(focused)
    if price is None: price=parse_price(focused)

    # Determine object type before choosing an area. A house page can contain both
    # 350 m² floor area and 37 000 m² parcel area; generic first/most-common-number
    # heuristics are not safe for that.
    cat=classify_category(title,url,focused,category_hint=category_hint)
    strong_area,strong_source=_jsonld_plot_area(objs)
    strong_raw=None
    # An explicit land-area label within the focused listing text beats generic values.
    if strong_area is None:
        strong_area,strong_source,strong_raw=_explicit_plot_area(focused)
    if strong_area is None and source=="Sprzedajemy" and cat=="plot":
        strong_area=_sprzedajemy_plot_area(focused)
        if strong_area is not None: strong_source='sprzedajemy:Powierzchnia'
    # Plot titles are especially trustworthy: "Działka na sprzedaż 3 200 m²".
    # This prevents recommendation/footer areas from replacing the advertised area.
    title_area,title_raw=best_area(title) if cat=="plot" else (None,None)
    if strong_area is not None:
        area=strong_area
        awarn=None
        if title_area is not None:
            ratio=max(strong_area,title_area)/max(1,min(strong_area,title_area))
            if ratio>=1.5:
                awarn=f"⚠️ Sprzeczny metraż: pole oferty {strong_area:g} m², tytuł {title_area:g} m². Zachowano pole oferty; sprawdź u sprzedającego."
    elif title_area is not None:
        area=title_area
        awarn=None
    else:
        area,_=best_area(focused)
        awarn=area_warning(focused)

    explicit_ppm=_price_m2_from_text(focused)
    price,price_m2=_reconcile_price_fields(price,area,explicit_ppm)
    if explicit_ppm and price_m2 and abs(price_m2-explicit_ppm)/max(price_m2,explicit_ppm)>0.05:
        warning=f'⚠️ Cena/m² w opisie ({explicit_ppm:g}) różni się od ceny podzielonej przez powierzchnię ({price_m2:.2f}).'
        awarn=' '.join(filter(None,[awarn,warning]))
    phone=_phone_from_soup(soup, desc+" "+body)
    if not published:
        v=_embedded_first(payloads,["datePosted","datePublished","publishedAt","createdAt","createdDate","creationDate"])
        if isinstance(v,str): published=clean_text(v)
    if not updated:
        v=_embedded_first(payloads,["dateModified","updatedAt","lastModified","refreshDate","pushedAt"])
        if isinstance(v,str): updated=clean_text(v)
    if not published and source=="Sprzedajemy":
        published=_sprzedajemy_date(soup, focused)
    if not published:
        dm=re.search(r"(?:Dodano|Dodane|Dodano dnia|Dodane dnia|Opublikowano|Data dodania|Data publikacji)\s*[:–-]?\s*([^|\n]{4,55})", focused, re.I)
        if dm: published=clean_text(dm.group(1))
    if not updated:
        um=re.search(r"(?:Aktualizacja|Zaktualizowano|Zaktualizowane|Zaktualizowana|Odświeżono(?: dnia)?|Odswiezono(?: dnia)?|Odświeżone|Odswiezone|Podbite|Data aktualizacji)\s*[:–-]?\s*([^|\n]{4,55})", focused, re.I)
        if um: updated=clean_text(um.group(1))

    af=asciifold(combined[:12000])
    archive_reason=None
    archive_markers=[
        'ogloszenie archiwalne','oferta archiwalna','ogloszenie nieaktualne','oferta nieaktualna',
        'ta oferta jest nieaktualna','oferta zostala zakonczona','ogloszenie zostalo zakonczone'
    ]
    for marker in archive_markers:
        if marker in af:
            archive_reason=marker; break
    source_status='archived' if archive_reason else 'active' 

    # Alternate metadata can improve structured location / region.
    if not structured_loc:
        for meta in ["geo.placename","place:location:locality"]:
            v=_find_meta(soup,meta)
            if v:
                structured_loc=v
                break
    if not structured_region:
        for meta in ["geo.region","place:location:region"]:
            v=_find_meta(soup,meta)
            if v:
                structured_region=v
                break
    # Detail pages from OLX and similar portals often render the province next to the
    # locality even when JSON-LD omits addressRegion. Keep this as strong location
    # metadata so a foreign Wróblowice/Olszyny cannot be geocoded to a local namesake.
    if not structured_region:
        cf=asciifold(combined[:12000])
        regions=(
            'dolnoslaskie','kujawsko-pomorskie','lubelskie','lubuskie','lodzkie',
            'malopolskie','mazowieckie','opolskie','podkarpackie','podlaskie','pomorskie',
            'slaskie','swietokrzyskie','warminsko-mazurskie','wielkopolskie','zachodniopomorskie'
        )
        rm=re.search(r'lokalizacja\s*[:\-]?\s*.{0,100}?\b('+'|'.join(map(re.escape,regions))+r')\b',cf,re.I)
        if rm:
            structured_region=rm.group(1)
    structured_region=structured_region or normalize_voivodeship(structured_loc) or ''
    loc,loc_conf=_specific_locality(title,desc,structured_loc.split(',')[0].strip())
    if not loc:
        for p in LOCATION_PATTERNS:
            m=re.search(p,combined,re.I)
            if m:
                loc=clean_text(m.group(1)); loc_conf="regex"; break

    ptype=classify_plot_type(title,combined) if cat=="plot" else ("garaż" if cat=="garage" else "n/d")
    plan=planning_status(combined) if cat=="plot" else "n/d"
    parcel=parcel_number(combined) if cat=="plot" else None
    image=_extract_listing_image(soup,payloads,objs,url)

    return {
        "canonical_url": canonical_url(url,url), "source":source, "category":cat,
        "title": title[:500], "price":price, "area_m2":area,
        "price_m2": price_m2, "plot_type":ptype,
        "planning_status":plan, "location":loc[:250], "location_confidence":loc_conf,
        "phone":phone, "parcel_number":parcel, "published_text":published[:100], "updated_text":updated[:100],
        "source_status":source_status, "archive_reason":archive_reason,
        "area_warning":awarn, "description":desc[:12000], "image_url":image,
        "_structured_region":structured_region[:120] if structured_region else "",
        "_structured_location":structured_loc,
        "_listing_coords":entity_coordinates(objs),
        "_parser_source":"primary-payload" if payload else "primary-jsonld" if objs else "html",
        "_area_source":strong_source,
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
    early=text[:9000]
    # Dynamic page fallback: recover the core listing fields from rendered text.
    if rec.get("category")=="plot" and not rec.get('_area_source'):
        ta,_=best_area(rec.get("title") or "")
        if ta is not None and rec.get("area_m2"):
            try:
                ratio=max(float(ta),float(rec["area_m2"]))/max(1,min(float(ta),float(rec["area_m2"])))
                if ratio>=1.5:
                    rec["area_warning"]=f"⚠️ Metraż skorygowany wg tytułu: {ta:g} m²"
                    rec["area_m2"]=ta
            except Exception:pass
    if rec.get("price") is None:
        rec["price"]=_total_price_from_text(early) or parse_price(early)
    if rec.get("area_m2") is None and rec.get("category")=="plot":
        a,_,_= _explicit_plot_area(early)
        if a is None: a,_=best_area(early)
        if a is not None: rec["area_m2"]=a
    explicit_ppm=_price_m2_from_text(early)
    rec["price"],rec["price_m2"]=_reconcile_price_fields(
        rec.get("price"),rec.get("area_m2"),explicit_ppm
    )
    loc_fold=asciifold(rec.get("location") or "")
    if loc_fold in {"", "zakliczyn", "gmina zakliczyn", "gromnik", "gmina gromnik", "czchow", "gmina czchow", "powiat tarnowski", "tarnowski", "powiat brzeski", "brzeski", "malopolskie"}:
        ef=asciifold(early)
        hits=[]
        for name in KNOWN_LOCALITIES:
            nf=asciifold(name)
            if re.search(rf"(?<![a-z0-9]){re.escape(nf)}(?![a-z0-9])", ef):
                hits.append(name)
        specific=list(dict.fromkeys(x for x in hits if asciifold(x) not in GENERIC_MUNICIPALITY_LOCALITIES))
        if len(specific)==1:
            rec["location"]=specific[0]
            rec["location_confidence"]="rendered-text"
        elif not specific:
            for municipality in ("Zakliczyn","Gromnik","Czchów"):
                if municipality in hits:
                    rec["location"]=municipality
                    rec["location_confidence"]="rendered-text-generic"
                    break

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
        dm=re.search(r"(?:Dodano|Dodane|Dodano dnia|Dodane dnia|Opublikowano|Data dodania|Data publikacji)\s*[:–-]?\s*([^|\n]{4,55})", early, re.I)
        if dm: rec["published_text"]=clean_text(dm.group(1))[:100]
    if not rec.get("published_text") and rec.get("source")=="Sprzedajemy":
        dm=re.search(r"\b(\d{1,2}\s+(?:Sty|Lut|Mar|Kwi|Maj|Cze|Lip|Sie|Wrz|Pa[zź]|Lis|Gru)[a-ząćęłńóśźż]*\s+\d{1,2}:\d{2})\b",early,re.I)
        if dm:rec["published_text"]=clean_text(dm.group(1))[:100]
    if not rec.get("updated_text"):
        um=re.search(r"(?:Aktualizacja|Zaktualizowano|Zaktualizowane|Zaktualizowana|Odświeżono(?: dnia)?|Odswiezono(?: dnia)?|Odświeżone|Odswiezone|Podbite|Data aktualizacji)\s*[:–-]?\s*([^|\n]{4,55})", early, re.I)
        if um: rec["updated_text"]=clean_text(um.group(1))[:100]
    af=asciifold(early)
    if any(x in af for x in ['ogloszenie archiwalne','oferta archiwalna','ogloszenie nieaktualne','oferta nieaktualna','ta oferta jest nieaktualna']):
        rec['source_status']='archived'; rec['archive_reason']='archiwalne/nieaktualne wg portalu'
    return rec

from __future__ import annotations
import re
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from .utils import asciifold

MONTHS={
' sty':1,'styczen':1,'stycznia':1,'lut':2,'luty':2,'lutego':2,'mar':3,'marzec':3,'marca':3,
'kwi':4,'kwiecien':4,'kwietnia':4,'maj':5,'maja':5,'cze':6,'czerwiec':6,'czerwca':6,
'lip':7,'lipiec':7,'lipca':7,'sie':8,'sierpien':8,'sierpnia':8,'wrz':9,'wrzesien':9,'wrzesnia':9,
'paz':10,'pazdziernik':10,'pazdziernika':10,'lis':11,'listopad':11,'listopada':11,'gru':12,'grudzien':12,'grudnia':12}

def normalize_published(text:str|None, now:datetime|None=None)->str|None:
    s=(text or '').strip()
    if not s:return None
    now=now or datetime.now(ZoneInfo('Europe/Warsaw'))
    # ISO/JSON-LD dates
    try:
        x=s.replace('Z','+00:00')
        d=datetime.fromisoformat(x)
        if d.tzinfo is None:d=d.replace(tzinfo=ZoneInfo('Europe/Warsaw'))
        return d.astimezone(ZoneInfo('UTC')).isoformat()
    except Exception:pass
    f=asciifold(s)
    tm=re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b',f)
    hh=int(tm.group(1)) if tm else 12; mm=int(tm.group(2)) if tm else 0
    if 'dzisiaj' in f:
        d=now.replace(hour=hh,minute=mm,second=0,microsecond=0);return d.astimezone(ZoneInfo('UTC')).isoformat()
    if 'wczoraj' in f:
        d=(now-timedelta(days=1)).replace(hour=hh,minute=mm,second=0,microsecond=0);return d.astimezone(ZoneInfo('UTC')).isoformat()
    m=re.search(r'\b(\d{1,2})[.\-/](\d{1,2})[.\-/](20\d{2})\b',f)
    if m:
        try:return datetime(int(m.group(3)),int(m.group(2)),int(m.group(1)),hh,mm,tzinfo=ZoneInfo('Europe/Warsaw')).astimezone(ZoneInfo('UTC')).isoformat()
        except:pass
    m=re.search(r'\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b',f)
    if m:
        mon=MONTHS.get(m.group(2))
        if mon:
            try:return datetime(int(m.group(3)),mon,int(m.group(1)),hh,mm,tzinfo=ZoneInfo('Europe/Warsaw')).astimezone(ZoneInfo('UTC')).isoformat()
            except:pass
    # Portals such as Sprzedajemy often show current-year publication as
    # "03 Maj 07:27" without a year. Infer the year conservatively: if the
    # resulting date would be more than one day in the future, it belongs to
    # the previous year.
    m=re.search(r'\b(\d{1,2})\s+([a-z]+)(?:\s+o)?(?:\s+\d{1,2}:\d{2})?\b',f)
    if m:
        mon=MONTHS.get(m.group(2))
        if mon:
            try:
                d=datetime(now.year,mon,int(m.group(1)),hh,mm,tzinfo=ZoneInfo('Europe/Warsaw'))
                if d > now + timedelta(days=1): d=d.replace(year=d.year-1)
                return d.astimezone(ZoneInfo('UTC')).isoformat()
            except:pass
    return None

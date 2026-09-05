from __future__ import annotations
from statistics import median
from .classify import privacy_score


def size_bucket(a):
    if not a: return "unknown"
    if a < 1000: return "<1000"
    if a < 2000: return "1000-1999"
    if a < 5000: return "2000-4999"
    if a < 10000: return "5000-9999"
    return "10000+"


def planning_bucket(v: str | None) -> str:
    v=v or ""
    if v in {"MPZP","wydane WZ"}: return "confirmed-build"
    if v in {"brak WZ","brak MPZP / WZ nieustalone"}: return "no-confirmed-build"
    if v=="WZ w trakcie": return "pending"
    return "unknown"


def _valid_plot(x):
    return x.get("category")=="plot" and x.get("price_m2") and 1 <= float(x["price_m2"]) <= 2000


def comparable_stats(target, active, minimum=3):
    """Return median, count and comparison quality.

    We first try truly similar plots. If the local market is sparse, we widen the
    comparison in controlled steps instead of pretending there is no market signal.
    """
    if not target.get("price_m2") or target.get("category") != "plot": return None,0,"brak"
    pool=[x for x in active if _valid_plot(x)]
    tb=size_bucket(target.get("area_m2")); tp=target.get("plot_type") or "nieustalona"; tplan=planning_bucket(target.get("planning_status"))
    levels=[
        ("wysoka", [x for x in pool if (x.get("plot_type") or "nieustalona")==tp and size_bucket(x.get("area_m2"))==tb and planning_bucket(x.get("planning_status"))==tplan]),
        ("dobra", [x for x in pool if (x.get("plot_type") or "nieustalona")==tp and size_bucket(x.get("area_m2"))==tb]),
        ("średnia", [x for x in pool if size_bucket(x.get("area_m2"))==tb and planning_bucket(x.get("planning_status"))==tplan]),
        ("orientacyjna", [x for x in pool if size_bucket(x.get("area_m2"))==tb]),
        ("słaba", [x for x in pool if (x.get("plot_type") or "nieustalona")==tp]),
    ]
    for quality, rows in levels:
        vals=[float(x["price_m2"]) for x in rows]
        if len(vals) >= minimum:
            return median(vals),len(vals),quality
    return None,0,"za mało danych"


def deal_label(ppm, med, n, minimum=3, thresholds=None):
    if not ppm or not med or n < minimum: return "za mało porównań"
    t=thresholds or {"mega":.65,"deal":.8,"good":.95,"market":1.10,"expensive":1.40}
    r=float(ppm)/float(med)
    if r < t["mega"]: return "🔥🔥 MEGA OKAZJA"
    if r < t["deal"]: return "🔥 OKAZJA"
    if r < t["good"]: return "🟢 DOBRA CENA"
    if r <= t["market"]: return "⚪ RYNKOWA"
    if r <= t["expensive"]: return "🟠 DROGO"
    return "🔴 BARDZO DROGO"


def enrich_scores(rec, active, cfg):
    if rec.get("category")=="plot":
        det,reasons=privacy_score((rec.get("title") or "")+" "+(rec.get("description") or ""),rec.get("area_m2"))
        rec["privacy_score"]=rec.get("privacy_score") or det
        if not rec.get("privacy_reasons"): rec["privacy_reasons"]="|".join(reasons)
        minimum=int(cfg["scoring"].get("minimum_comparables_for_deal_score",3))
        med,n,quality=comparable_stats(rec,active,minimum)
        rec["median_comparable"]=med
        rec["comparable_count"]=n
        rec["comparison_quality"]=quality
        rec["deal_label"]=deal_label(rec.get("price_m2"),med,n,minimum,cfg["scoring"]["deal_thresholds"])
    else:
        rec["privacy_score"]=None; rec["privacy_reasons"]=""; rec["median_comparable"]=None; rec["comparable_count"]=0; rec["comparison_quality"]="n/d"; rec["deal_label"]="n/d"
    return rec

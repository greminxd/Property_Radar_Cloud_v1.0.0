from __future__ import annotations
from statistics import median, mean


def size_bucket(a):
    if not a: return 'unknown'
    if a < 1000: return '<1000'
    if a < 2000: return '1000-1999'
    if a < 5000: return '2000-4999'
    if a < 10000: return '5000-9999'
    return '10000+'


def planning_bucket(v: str | None) -> str:
    v=v or ''
    if v in {'MPZP','wydane WZ'}: return 'confirmed-build'
    if v in {'brak WZ','brak MPZP / WZ nieustalone'}: return 'no-confirmed-build'
    if v=='WZ w trakcie': return 'pending'
    return 'unknown'


def _valid_plot(x):
    return x.get('category')=='plot' and x.get('price_m2') and 1 <= float(x['price_m2']) <= 5000


def comparable_stats(target, active, minimum=3):
    if not target.get('price_m2') or target.get('category') != 'plot': return None,None,0,'brak'
    pool=[x for x in active if _valid_plot(x) and x.get('canonical_url')!=target.get('canonical_url')]
    tb=size_bucket(target.get('area_m2')); tp=target.get('plot_type') or 'nieustalona'; tplan=planning_bucket(target.get('planning_status'))

    # Do not call an agricultural/unknown plot a comparable for a service/building
    # plot merely to reach n=3. That produced impressive-looking but meaningless
    # percentages. For a known plot type we stay within that type; if there are too
    # few records, the honest answer is "za mało danych".
    if tp not in {'nieustalona','n/d',''}:
        same=[x for x in pool if (x.get('plot_type') or 'nieustalona')==tp]
        levels=[
            ('wysoka',[x for x in same if size_bucket(x.get('area_m2'))==tb and planning_bucket(x.get('planning_status'))==tplan]),
            ('dobra',[x for x in same if size_bucket(x.get('area_m2'))==tb]),
            ('orientacyjna',same),
        ]
    else:
        levels=[
            ('dobra',[x for x in pool if size_bucket(x.get('area_m2'))==tb and planning_bucket(x.get('planning_status'))==tplan]),
            ('orientacyjna',[x for x in pool if size_bucket(x.get('area_m2'))==tb]),
        ]
    for quality,rows in levels:
        vals=[float(x['price_m2']) for x in rows]
        if len(vals)>=minimum: return median(vals),mean(vals),len(vals),quality
    return None,None,0,'za mało danych'


def deal_label(ppm, med, n, minimum=3, thresholds=None):
    if not ppm or not med or n < minimum: return 'za mało porównań'
    t=thresholds or {'mega':.65,'deal':.8,'good':.95,'market':1.10,'expensive':1.40}
    r=float(ppm)/float(med)
    if r < t['mega']: return 'bardzo tanio vs ogłoszenia'
    if r < t['deal']: return 'tanio vs ogłoszenia'
    if r < t['good']: return 'lekko poniżej rynku ofertowego'
    if r <= t['market']: return 'rynkowa cena ofertowa'
    if r <= t['expensive']: return 'drogo vs ogłoszenia'
    return 'bardzo drogo vs ogłoszenia'


def enrich_scores(rec, active, cfg):
    if rec.get('category')=='plot':
        rec['privacy_score']=None; rec['privacy_reasons']=''
        minimum=int(cfg['scoring'].get('minimum_comparables_for_deal_score',3))
        med,avg,n,quality=comparable_stats(rec,active,minimum)
        rec['median_comparable']=med; rec['market_mean_comparable']=avg
        rec['comparable_count']=n; rec['comparison_quality']=quality
        rec['deal_label']=deal_label(rec.get('price_m2'),med,n,minimum,cfg['scoring']['deal_thresholds'])
    else:
        rec['privacy_score']=None; rec['privacy_reasons']=''; rec['median_comparable']=None; rec['market_mean_comparable']=None
        rec['comparable_count']=0; rec['comparison_quality']='n/d'; rec['deal_label']='n/d'
    return rec

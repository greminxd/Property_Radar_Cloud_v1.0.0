from app.parser import parse_detail
from app.area import area_accepts
from app.utils import fingerprint
from app.scoring import enrich_scores
from app.dates import normalize_published

html='''<html><head><meta property="og:title" content="Działka rolno-budowlana Zdonia 15 ar"><meta property="og:description" content="Zdonia, gm. Zakliczyn. Wydane warunki zabudowy. Nr działki 187/22. Telefon 600 123 456. Na uboczu, przy lesie."></head><body><main>Powierzchnia 15 m2, faktycznie 15 ar. Cena 99 000 zł. Zdonia gm. Zakliczyn. Wydane WZ. Kontakt telefon 600 123 456.</main></body></html>'''
r=parse_detail(html,'https://example.com/dzialka-test','TEST','plot')
assert r['category']=='plot',r
assert r['plot_type']=='rolno-budowlana',r
assert r['planning_status']=='wydane WZ',r
assert r['area_m2']==1500,r
assert r['parcel_number']=='187/22',r
assert r['phone']=='600 123 456',r

cfg={'mode':'locality_whitelist','primary_localities':['Zdonia','Zakliczyn','Słona'],'nearby_localities':['Bieśnik','Paleśnica'],'known_gmina_localities':['Zdonia','Zakliczyn','Słona','Bieśnik','Paleśnica','Milówka'],'known_outside_localities':['Milówka','Złota'],'fallback_radius_km':5,'reject_unknown_location':True}
ok,loc,_=area_accepts(r,cfg,2.0); assert ok and loc=='Zdonia'
bad=dict(r,location='Milówka',title='Działka Milówka'); ok,_,_=area_accepts(bad,cfg,2.0); assert not ok

f1=fingerprint('Działka Zdonia 15 ar','Zdonia',1500,100000,None)
f2=fingerprint('Działka Zdonia 15 ar','Zdonia',1500,85000,None)
assert f1==f2,'fingerprint must survive price changes'

rows=[]
for i,p in enumerate([35,40,42,46]): rows.append({'id':i+1,'category':'plot','plot_type':'rolno-budowlana','planning_status':'wydane WZ','area_m2':1500,'price_m2':p,'title':'x','description':''})
t=dict(rows[0],price_m2=28,privacy_score=7)
config={'scoring':{'minimum_comparables_for_deal_score':3,'deal_thresholds':{'mega':.65,'deal':.8,'good':.95,'market':1.1,'expensive':1.4}}}
enrich_scores(t,rows,config); assert 'OKAZJA' in t['deal_label'],t
print('SELFTEST OK')

assert normalize_published('28 sierpnia 2026') is not None
print('DATE TEST OK')

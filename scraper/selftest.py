from datetime import datetime, timezone, timedelta
from app.parser import parse_detail, refine_from_rendered_text
from app.area import area_accepts
from app.utils import fingerprint
from app.scoring import enrich_scores
from app.dates import normalize_published
from app.rcn import RCNClient
from app.classify import classify_category

html='''<html><head><meta property="og:title" content="Działka rolno-budowlana Zdonia 15 ar"><meta property="og:description" content="Zdonia, gm. Zakliczyn. Wydane warunki zabudowy. Nr działki 187/22. Telefon 600 123 456."><script type="application/ld+json">{"@type":"Offer","datePublished":"2026-09-05","dateModified":"2026-09-06"}</script></head><body><main>Powierzchnia 15 m2, faktycznie 15 ar. Cena 99 000 zł. Zdonia gm. Zakliczyn. Wydane WZ. Kontakt telefon 600 123 456.</main></body></html>'''
r=parse_detail(html,'https://example.com/dzialka-test','TEST','plot')
assert r['category']=='plot',r
assert r['plot_type']=='rolno-budowlana',r
assert r['planning_status']=='wydane WZ',r
assert r['area_m2']==1500,r
assert r['parcel_number']=='187/22',r
assert r['phone']=='600 123 456',r
assert r['published_text']=='2026-09-05',r
assert r['updated_text']=='2026-09-06',r


# Regression: never confuse a house floor area / rounded title hectare value with the
# explicit parcel area. This exact shape appeared in a real portal result.
house_html = """<html><head><meta property='og:title' content='DOM, 350m, 3.74ha, ZAKLICZYN n/d Dunajcem'></head><body><main>
Cena 1 250 000 zł. Powierzchnia domu 350 m². Powierzchnia działki <b>37000 m²</b>.
</main></body></html>"""
h = parse_detail(house_html,'https://example.com/dom-350m-374ha','TEST','plot')
assert h['area_m2'] == 37000, h
assert h['category'] == 'other', h  # radar is plots only

# Labelled plot field also wins over unrelated dimensions in a genuine plot listing.
plot_html = """<html><head><meta property='og:title' content='Działka Zakliczyn 3.74 ha'></head><body><main>
Szerokość 35 m. Powierzchnia działki: 37 000 m². Cena 500 000 zł.
</main></body></html>"""
p2 = parse_detail(plot_html,'https://example.com/dzialka-374ha','TEST','plot')
assert p2['category'] == 'plot', p2
assert p2['area_m2'] == 37000, p2

arch=parse_detail('<html><head><title>Działka Zakliczyn</title></head><body>Ogłoszenie archiwalne. Zaktualizowane: 13 lipca 2024. Cena 45 000 zł, powierzchnia 2900 m².</body></html>','https://example.com/old','TEST','plot')
assert arch['source_status']=='archived',arch
assert not arch.get('published_text'),arch
assert '13 lipca 2024' in arch.get('updated_text',''),arch

cfg={'mode':'locality_whitelist','primary_localities':['Zdonia','Zakliczyn','Słona'],'nearby_localities':['Bieśnik','Paleśnica'],'known_gmina_localities':['Zdonia','Zakliczyn','Słona','Bieśnik','Paleśnica','Milówka'],'known_outside_localities':['Milówka','Złota'],'fallback_radius_km':5,'reject_unknown_location':True}
ok,loc,_=area_accepts(r,cfg,2.0); assert ok and loc=='Zdonia'
bad=dict(r,location='Milówka',title='Działka Milówka'); ok,_,_=area_accepts(bad,cfg,2.0); assert not ok

f1=fingerprint('Działka Zdonia 15 ar','Zdonia',1500,100000,None)
f2=fingerprint('Działka Zdonia 15 ar','Zdonia',1500,85000,None)
assert f1==f2,'fingerprint must survive price changes'

rows=[]
for i,p in enumerate([35,40,42,46]): rows.append({'id':i+1,'canonical_url':f'https://x/{i}','category':'plot','plot_type':'rolno-budowlana','planning_status':'wydane WZ','area_m2':1500,'price_m2':p,'title':'x','description':''})
t=dict(rows[0],canonical_url='https://x/target',price_m2=28)
config={'scoring':{'minimum_comparables_for_deal_score':3,'deal_thresholds':{'mega':.65,'deal':.8,'good':.95,'market':1.1,'expensive':1.4}}}
enrich_scores(t,rows,config); assert 'tanio' in t['deal_label'],t
assert t['privacy_score'] is None

# RCN comparison: use nearby real transactions and a similar plot size.
rc=RCNClient(49.82625,20.8099,{})
tx=[]
for i,ppm in enumerate([30,32,34,36]):
    tx.append({'transaction_date':(datetime.now(timezone.utc)-timedelta(days=i*30)).isoformat(),'price_m2':ppm,'area_m2':1600,'lat':49.82625+i*0.001,'lon':20.8099})
res=rc.analyze({'category':'plot','area_m2':1500,'lat':49.82625,'lon':20.8099},tx,24,3)
assert res['rcn_count']>=3 and res['rcn_median_ppm']>0,res

assert normalize_published('28 sierpnia 2026') is not None

# v1.2.2: garage must never enter radar
assert classify_category("Garaż murowany 18 m2", "https://x/garaz/123", "", "plot") == "other"

# v1.2.2: JS-heavy/detail fallback must recover price + explicit plot area
r={"category":"plot","title":"Działka Zakliczyn","price":None,"area_m2":None,"price_m2":None,"location":"Zakliczyn","plot_type":"nieustalona","planning_status":"nieustalone","published_text":"","updated_text":"","phone":None,"parcel_number":None}
r=refine_from_rendered_text(r,"Powierzchnia działki: 5 400 m² Cena 199 000 zł Dodane 1 września 2026 Zakliczyn")
assert r["area_m2"] == 5400, r
assert r["price"] == 199000, r

print('SELFTEST OK v1.2.2')

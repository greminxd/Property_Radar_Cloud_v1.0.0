from app.parser import parse_detail, refine_from_rendered_text
from app.area import area_accepts
from app.classify import classify_category, is_rental_offer, olx_url_cid
from app.scoring import enrich_scores
from app.utils import fingerprint

html='''<html><head><meta property="og:title" content="Działka rolno-budowlana Zdonia 15 ar"><meta property="og:description" content="Zdonia, gm. Zakliczyn. Wydane warunki zabudowy. Nr działki 187/22. Telefon 600 123 456."></head><body><main>Powierzchnia 15 ar. Cena 99 000 zł.</main></body></html>'''
r=parse_detail(html,'https://example.com/dzialka-test','TEST','plot')
assert r['category']=='plot' and r['area_m2']==1500 and r['parcel_number']=='187/22',r

assert classify_category('Nowy kołowrotek gruntowy','https://www.olx.pl/d/oferta/x-CID767-IDabc.html','sprzęt do połowu gruntowego','plot')=='other'
assert olx_url_cid('https://www.olx.pl/d/oferta/x-CID767-IDabc.html')==767
assert olx_url_cid('https://www.olx.pl/d/oferta/dzialka-CID3-IDabc.html')==3
assert is_rental_offer('Powierzchnia 300m2 do wynajęcia','', '', 'https://x')
assert is_rental_offer('Powierzchnia 300m2','Oferuję halę magazynową do wynajęcia. Cena wynajmu wynosi 7000 PLN miesięcznie.', '', 'https://x')
assert not is_rental_offer('Działka budowlana na sprzedaż','', '', 'https://x')

cfg={'mode':'locality_whitelist','primary_localities':['Zdonia','Zakliczyn','Słona'],'nearby_localities':['Bieśnik','Paleśnica','Lusławice'],'known_gmina_localities':['Zdonia','Zakliczyn','Słona','Bieśnik','Paleśnica','Lusławice'],'known_outside_localities':['Milówka','Złota'],'fallback_radius_km':5,'reject_unknown_location':True}
ok,loc,_=area_accepts({'source':'TEST','title':'Działka Zdonia','location':'Zdonia','description':'gm. Zakliczyn'},cfg,2);assert ok and loc=='Zdonia'
ok,_,why=area_accepts({'source':'TEST','title':'Działka Olcha, powiat żuromiński','location':'Olcha','description':''},cfg,None);assert not ok and why.startswith('explicit-outside'),why
ok,_,why=area_accepts({'source':'TEST','title':'Działka Zakliczyn','location':'Zakliczyn','description':'gmina Siepraw, powiat myślenicki'},cfg,None);assert not ok,why
ok,_,why=area_accepts({'source':'OLX','title':'Działka budowlana 20km od Krakowa','location':'Zakliczyn, Małopolskie','description':'Sprzedam działkę położoną w Zakliczynie/ koło Myślenic ul. Cichy Kącik nr 369/7'},cfg,None);assert not ok and 'same-name' in why,why
ok,loc,why=area_accepts({'source':'OLX','title':'Działka budowlana Zakliczyn','location':'Zakliczyn, Małopolskie','description':'Zakliczyn, powiat tarnowski. Działka na sprzedaż.'},cfg,None);assert ok and loc=='Zakliczyn',(ok,loc,why)

f1=fingerprint('Działka Zdonia','Zdonia',1500,100000,'187/22');f2=fingerprint('Działka Zdonia','Zdonia',1500,90000,'187/22');assert f1==f2
rows=[{'id':i,'canonical_url':f'https://x/{i}','category':'plot','plot_type':'budowlana','planning_status':'wydane WZ','area_m2':1500,'price_m2':p} for i,p in enumerate([40,45,50,55],1)]
t={'id':99,'canonical_url':'https://x/t','category':'plot','plot_type':'budowlana','planning_status':'wydane WZ','area_m2':1500,'price_m2':35}
conf={'scoring':{'minimum_comparables_for_deal_score':3,'deal_thresholds':{'mega':.65,'deal':.8,'good':.95,'market':1.1,'expensive':1.4}}}
enrich_scores(t,rows,conf);assert t['median_comparable'] and t['comparable_count']>=3
print('SELFTEST OK v1.5.1')

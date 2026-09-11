from app.parser import parse_detail, refine_from_rendered_text
from app.area import area_accepts, target_region_accepts
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


# v1.5.2 production scope: broad discovery + strict 10 km distance verification.
radius_cfg={'mode':'radius_verified','fallback_radius_km':10,'reject_unknown_location':True}
ok,_,why=area_accepts({'source':'TEST','title':'Działka Borowa','location':'Borowa','description':'gmina Zakliczyn'},radius_cfg,3.14);assert ok and why=='radius-verified',(ok,why)
ok,_,why=area_accepts({'source':'TEST','title':'Działka','location':'Siemiechów','description':'gmina Gromnik'},radius_cfg,7.53);assert ok and why=='radius-verified',(ok,why)
ok,_,why=area_accepts({'source':'TEST','title':'Działka','location':'Gromnik','description':''},radius_cfg,12.1);assert not ok and why=='outside-radius',(ok,why)
ok,_,why=area_accepts({'source':'TEST','title':'Działka','location':'','description':''},radius_cfg,None);assert not ok and why=='radius-unresolved',(ok,why)
ok,_,why=area_accepts({'source':'TEST','title':'Działka Zakliczyn','location':'Zakliczyn','description':'Zakliczyn koło Myślenic, gmina Siepraw'},radius_cfg,2.0);assert not ok and 'same-name' in why,why

html_gromnik='''<html><head><meta property="og:title" content="Działka budowlano-rolna 1,23 ha | 30,35 ar UM"><meta property="og:description" content="SIEMIECHÓW | GMINA GROMNIK | POWIAT TARNOWSKI. Powierzchnia działki 12 300 m². Cena 150 000 zł."><meta name="geo.placename" content="Gromnik"></head><body><main>SIEMIECHÓW | GMINA GROMNIK | POWIAT TARNOWSKI</main></body></html>'''
rg=parse_detail(html_gromnik,'https://www.otodom.pl/pl/oferta/test-IDx','Otodom','plot')
assert rg['location']=='Siemiechów',rg.get('location')

# v1.5.3: explicit voivodeship is a hard validation boundary. Same-named
# villages in another region must never be re-geocoded into Małopolskie.
ok,reg,why=target_region_accepts('Wróblowice','Dolnośląskie','')
assert not ok and reg=='dolnośląskie' and why=='explicit-outside-region',(ok,reg,why)
ok,reg,why=target_region_accepts('Olszyny','Warmińsko-Mazurskie','')
assert not ok and reg=='warmińsko-mazurskie',(ok,reg,why)
ok,reg,why=target_region_accepts('Lusławice','Małopolskie','')
assert ok and reg=='małopolskie',(ok,reg,why)

# OLX uses the generic area field `m` for flats too. Even with a broad plot hint,
# unmistakable apartment attributes must win and classify the offer as non-plot.
apartment_desc='Powierzchnia: 50,79 m². Liczba pokoi: 3 pokoje. Rodzaj zabudowy: Blok. Umeblowane: Nie. Mieszkanie o powierzchni 50,79 m².'
assert classify_category('3 pokoje | 50,79 m² | balkon 6,16 m2','https://www.olx.pl/d/oferta/x-CID3-IDabc.html',apartment_desc,'plot')=='other'

html_region="""<html><head><script type='application/ld+json'>{"@type":"Offer","itemOffered":{"@type":"Residence","address":{"@type":"PostalAddress","addressLocality":"Wróblowice","addressRegion":"Dolnośląskie"}}}</script><meta property='og:title' content='3 pokoje 50,79 m2'></head><body>Powierzchnia 50,79 m² Liczba pokoi: 3</body></html>"""
rr=parse_detail(html_region,'https://www.olx.pl/d/oferta/x-CID3-IDabc.html','OLX','plot')
assert rr.get('_structured_region')=='Dolnośląskie',rr


# 1.6.2: listing photos are preserved from the real advert metadata / payload.
html_image='<html><head><meta property="og:image" content="https://cdn.example.com/listing/plot-123.jpg"><meta property="og:title" content="Działka Zdonia"><meta property="og:description" content="Powierzchnia działki 1200 m2. Cena 120 000 zł."></head><body><main><h1>Działka Zdonia</h1></main></body></html>'
ri=parse_detail(html_image,'https://example.com/oferta/123','TEST','plot')
assert ri.get('image_url')=='https://cdn.example.com/listing/plot-123.jpg',ri.get('image_url')
html_json_image='<html><head><meta property="og:title" content="Działka Słona"><script type="application/ld+json">{"@type":"Offer","offers":{"price":150000,"priceCurrency":"PLN"},"image":[{"@type":"ImageObject","contentUrl":"https://img.example.com/slona-large.webp"}]}</script></head><body><main>Działka Słona 1500 m2 150000 zł</main></body></html>'
rj=parse_detail(html_json_image,'https://example.com/oferta/124','TEST','plot')
assert rj.get('image_url')=='https://img.example.com/slona-large.webp',rj.get('image_url')

f1=fingerprint('Działka Zdonia','Zdonia',1500,100000,'187/22');f2=fingerprint('Działka Zdonia','Zdonia',1500,90000,'187/22');assert f1==f2
rows=[{'id':i,'canonical_url':f'https://x/{i}','category':'plot','plot_type':'budowlana','planning_status':'wydane WZ','area_m2':1500,'price_m2':p} for i,p in enumerate([40,45,50,55],1)]
t={'id':99,'canonical_url':'https://x/t','category':'plot','plot_type':'budowlana','planning_status':'wydane WZ','area_m2':1500,'price_m2':35}
conf={'scoring':{'minimum_comparables_for_deal_score':3,'deal_thresholds':{'mega':.65,'deal':.8,'good':.95,'market':1.1,'expensive':1.4}}}
enrich_scores(t,rows,conf);assert t['median_comparable'] and t['comparable_count']>=3
print('SELFTEST OK v1.6.2')

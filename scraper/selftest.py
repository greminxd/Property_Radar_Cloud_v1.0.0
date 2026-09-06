import json
from datetime import datetime, timezone, timedelta
from app.parser import parse_detail, refine_from_rendered_text
from app.area import area_accepts
from app.utils import fingerprint
from app.scoring import enrich_scores
from app.dates import normalize_published
from app.rcn import RCNClient
from app.classify import classify_category, is_rental_offer

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

# v1.2.5: search-link extraction must work on server-rendered OLX/Otodom style HTML.
import re as _re
from app.scraper import Scraper
_pat_olx=_re.compile(r"https?://(?:www\.)?olx\.pl/d/oferta/[^?#]+",_re.I)
links,nxt=Scraper._extract_links_from_html(
    'https://www.olx.pl/nieruchomosci/dzialki/sprzedaz/q-zakliczyn/',
    '<a href="/d/oferta/dzialka-zdonia-CID3-IDabc.html">x</a><a rel="next" href="?page=2">Następna</a>',
    _pat_olx,
)
assert len(links)==1 and 'olx.pl/d/oferta/' in links[0], links
assert nxt and 'page=2' in nxt, nxt

_pat_oto=_re.compile(r"https?://(?:www\.)?otodom\.pl/pl/oferta/[^?#]+",_re.I)
links,_=Scraper._extract_links_from_html(
    'https://www.olx.pl/nieruchomosci/dzialki/sprzedaz/q-zakliczyn/',
    '<a href="https://www.otodom.pl/pl/oferta/dzialka-zakliczyn-ID4xyz">oto</a>',
    _pat_oto,
)
assert len(links)==1 and 'otodom.pl/pl/oferta/' in links[0], links
print('SELFTEST OK v1.2.5 discovery')

# v1.2.6: RCN raw values use Polish formatting and hectare areas.
from app.rcn import _num as rcn_num, _ha_to_m2, _date as rcn_date
assert rcn_num('1.000.000,00') == 1000000.0
assert rcn_num('75.000') == 75000.0
assert _ha_to_m2('0,5500') == 5500.0
assert _ha_to_m2('1.000') == 10000.0
assert rcn_date('2026-03-10').startswith('2026-03-10'), rcn_date('2026-03-10')
# Future RCN rows must never be accepted into the benchmark.
assert rcn_date('2099-01-01') is None

# v1.2.6: title area wins when portal page contains unrelated/recommended areas.
mor_html="""<html><head><meta property='og:title' content='Działka na sprzedaż 3 200 m² | Paleśnica | Morizon.pl'></head>
<body><main>Cena 170 000 zł. Paleśnica. Polecane: Powierzchnia działki 14 643 m².</main></body></html>"""
mor=parse_detail(mor_html,'https://www.morizon.pl/oferta/test','Morizon','plot')
assert mor['area_m2']==3200,mor

# v1.2.6: actual Sprzedajemy listing shape, no category/search-page artifact.
spr_html="""<html><head><meta property='og:title' content='Działka budowlana Wesołów 10 ar'><meta property='product:price:amount' content='75000'></head>
<body><main>03 Maj 07:27 Cena za m² 75 zł/m² Powierzchnia 1000 m² Wesołów. 75 000 zł 75 zł/m²</main></body></html>"""
spr=parse_detail(spr_html,'https://sprzedajemy.pl/dzialka-wesolow-4-1b8e55-nr67779073','Sprzedajemy','plot')
assert spr['price']==75000,spr
assert spr['area_m2']==1000,spr
assert spr['published_text'],spr
assert normalize_published(spr['published_text']) is not None,spr
print('SELFTEST OK v1.2.6 RCN/parser')

# v1.3.0: OLX may render price/m² before total price. Never interpret ppm as total.
olx_html="""<html><head><title>Działka Lusławice 36 ar z WZ</title></head><body><main>
Rodzaj: Działki rolno-budowlane Powierzchnia: 3 600 m² Cena za m²: 97.22 zł/m²
Opis oferty. 350 000 zł do negocjacji. Lokalizacja Zakliczyn.
</main></body></html>"""
olx=parse_detail(olx_html,'https://www.olx.pl/d/oferta/test-CID3-IDabc.html','OLX','plot')
assert olx['price']==350000,olx
assert olx['area_m2']==3600,olx
assert abs(olx['price_m2']-97.22)<0.01,olx

# Tabelaofert server-rendered shape: explicit ppm is authoritative for comparisons,
# but total price stays the actual asking price (small rounding difference is normal).
tab_html="""<html><head><title>Działka na sprzedaż, 5 400,00 m², Zakliczyn</title></head><body><main>
<h1>Działka na sprzedaż, 5 400,00 m², oferta nr BEST-GS-15365</h1>
199 000 zł 37 zł /m² Zakliczyn Cena za m²: 37 zł Powierzchnia: 5 400,00 m²
</main></body></html>"""
tab=parse_detail(tab_html,'https://tabelaofert.pl/oferta/dzialka-budowlana-zakliczyn,10401479','Tabelaofert','plot')
assert tab['price']==199000,tab
assert tab['area_m2']==5400,tab
assert tab['price_m2']==37,tab

# Scoring invariant: only ppm participates. Total plot price must not change the result.
a={'canonical_url':'a','category':'plot','plot_type':'budowlana','planning_status':'MPZP','area_m2':1000,'price':100000,'price_m2':100}
b={'canonical_url':'b','category':'plot','plot_type':'budowlana','planning_status':'MPZP','area_m2':2000,'price':200000,'price_m2':100}
c={'canonical_url':'c','category':'plot','plot_type':'budowlana','planning_status':'MPZP','area_m2':1500,'price':150000,'price_m2':100}
target={'canonical_url':'t','category':'plot','plot_type':'budowlana','planning_status':'MPZP','area_m2':1200,'price':240000,'price_m2':200}
enrich_scores(target,[a,b,c],config)
assert target['median_comparable']==100,target
print('SELFTEST OK v1.3.0 ppm-only pricing')

# v1.3.0: Tabelaofert search page must ignore recommendation links beyond its result count.
_tab_html = "Znaleziono 3 oferty " + " ".join(
    f'<a href="https://tabelaofert.pl/oferta/dzialka-x,{i}">Działka {i}</a>' for i in range(1,6)
)
_tab_pat=_re.compile(r"https?://(?:www\.)?tabelaofert\.pl/oferta/[^?#,]+(?:,|%2C)\d+",_re.I)
_tab_links,_=Scraper._extract_links_from_html('https://tabelaofert.pl/sprzedaz/dzialki/zakliczyn',_tab_html,_tab_pat)
_tab_links=Scraper._source_filter_discovery('Tabelaofert',_tab_html,_tab_links)
assert len(_tab_links)==3,_tab_links
print('SELFTEST OK v1.3.0 Tabelaofert discovery cap')


# v1.3.1: dedicated Otodom discovery reads Next.js searchAds.items, not CSS card links.
_oto_payload={
    "props":{"pageProps":{"data":{"searchAds":{"items":[
        {"slug":"dzialka-zakliczyn-ID4abc"},
        {"detailUrl":"https://www.otodom.pl/pl/oferta/druga-dzialka-ID4def"},
    ]}}}}
}
_items=Scraper._otodom_search_items(_oto_payload)
assert len(_items)==2,_items
assert Scraper._otodom_item_url(_items[0])=='https://www.otodom.pl/pl/oferta/dzialka-zakliczyn-ID4abc'
assert Scraper._otodom_item_url(_items[1])=='https://www.otodom.pl/pl/oferta/druga-dzialka-ID4def'
_html='<html><script id="__NEXT_DATA__" type="application/json">'+json.dumps(_oto_payload)+'</script></html>'
assert Scraper._otodom_search_items(Scraper._next_data(_html))==_items
assert 'page=3' in Scraper._page_url('https://www.otodom.pl/pl/wyniki/sprzedaz/dzialka/x?limit=72',3)

print("SELFTEST OK v1.3.1 Otodom NextData")


# v1.3.3: dedicated OLX collector consumes /api/v1/offers JSON directly.
_olx_offer={
    "id":859896227,
    "url":"https://www.olx.pl/d/oferta/dzialka-luslawice-36-ar-CID3-IDVc2hZ.html",
    "title":"Działka Lusławice 36 ar z WZ",
    "description":"Działka nr 142/1 w Lusławicach. Wydane warunki zabudowy.",
    "created_time":"2026-08-28T12:00:00+02:00",
    "last_refresh_time":"2026-09-05T12:00:00+02:00",
    "status":"active",
    "price":{"value":350000,"label":"350 000 zł","currency":"PLN"},
    "params":[
        {"key":"m","name":"Powierzchnia","value":{"key":"3600","label":"3 600 m²"}},
        {"key":"type","name":"Rodzaj","value":{"key":"rolno-budowlana","label":"Działki rolno-budowlane"}},
    ],
    "location":{"city":{"id":1,"name":"Zakliczyn"},"region":{"id":2,"name":"Małopolskie"}},
    "photos":[{"link":"https://example.com/{width}x{height}.jpg"}],
}
assert Scraper._olx_offer_is_plot(_olx_offer), _olx_offer
_olx_rec=Scraper._olx_record_from_api(_olx_offer)
assert _olx_rec and _olx_rec['category']=='plot',_olx_rec
assert _olx_rec['price']==350000,_olx_rec
assert _olx_rec['area_m2']==3600,_olx_rec
assert abs(_olx_rec['price_m2']-(350000/3600))<0.02,_olx_rec
assert _olx_rec['location']=='Lusławice',_olx_rec
assert _olx_rec['parcel_number']=='142/1',_olx_rec
assert '1200x900' in (_olx_rec.get('image_url') or ''),_olx_rec
assert Scraper._olx_api_query_from_search_url('https://www.olx.pl/nieruchomosci/dzialki/sprzedaz/q-zakliczyn/')=='zakliczyn'
assert Scraper._olx_api_query_from_search_url('https://www.olx.pl/nieruchomosci/dzialki/zakliczyn/')=='zakliczyn'
_nonplot=dict(_olx_offer,url='https://www.olx.pl/d/oferta/volkswagen-golf-zakliczyn-CID5-IDcar123.html',title='Volkswagen Golf Zakliczyn',description='Samochód osobowy, benzyna, 2018',params=[])
assert not Scraper._olx_offer_is_plot(_nonplot),_nonplot
print('SELFTEST OK v1.3.3 OLX public API')

# v1.4.2: explicit foreign county/gmina must beat any later distance fallback.
area_cfg=json.load(open('config.json',encoding='utf-8'))['area']
outside={'location':'','title':'Na sprzedaż las o powierzchni 5,23 ha – Olcha, powiat żuromiński','description':''}
ok,loc,why=area_accepts(outside,area_cfg,None)
assert not ok and loc is None and why=='explicit-outside-county',(ok,loc,why)
wrong_zak={'location':'Zakliczyn','title':'Działka Zakliczyn','description':'Zakliczyn, gmina Siepraw, powiat myślenicki'}
ok,loc,why=area_accepts(wrong_zak,area_cfg,None)
assert not ok and why.startswith('explicit-outside'),(ok,loc,why)
right={'location':'','title':'Działka Lusławice 36 ar z WZ','description':''}
ok,loc,why=area_accepts(right,area_cfg,None)
assert ok and loc=='Lusławice',(ok,loc,why)
foreign_html='<html><head><meta property="og:title" content="Na sprzedaż las o powierzchni 5,23 ha – Olcha, powiat żuromiński"></head><body><main><h1>Na sprzedaż las o powierzchni 5,23 ha – Olcha, powiat żuromiński</h1><p>Oferta</p></main></body></html>'
foreign=parse_detail(foreign_html,'https://sprzedajemy.pl/test-nr123','Sprzedajemy','plot')
assert foreign['location']=='Olcha' and foreign['location_confidence']=='title-county',foreign
print('SELFTEST OK v1.4.2 strict location guard')

# v1.4.3: canonical locality registry shared by parser, area validation and OLX discovery.
from app.area import registry_names, locality_record
_registry=registry_names()
assert len(_registry)==27, _registry
for _name in ['Granice','Kanada','Podlesie','Bieśnik','Zdonia','Zakliczyn']:
    assert _name in _registry, (_name,_registry)
assert locality_record('Biesnik')['name']=='Bieśnik'
assert locality_record('Palesnicy')['name']=='Paleśnica'

area_cfg=json.load(open('config.json',encoding='utf-8'))['area']
# A portal may report generic Zakliczyn while the title contains the real village.
_specific={'location':'Zakliczyn','title':'Działka Lusławice 3600 m²','description':''}
ok,loc,why=area_accepts(_specific,area_cfg,None)
assert ok and loc=='Lusławice' and why=='title',(ok,loc,why)
# Complete gmina registry knows this place, but it is outside the configured radar scope.
_gmina_only={'location':'Granice','title':'Działka Granice, gmina Zakliczyn','description':''}
ok,loc,why=area_accepts(_gmina_only,area_cfg,None)
assert not ok and loc is None and why=='known-gmina-outside-target',(ok,loc,why)
# Foreign administrative evidence remains a hard reject even if a target word appears elsewhere.
_conflict={'location':'Zakliczyn','title':'Działka Zakliczyn','description':'Olcha, powiat żuromiński'}
ok,loc,why=area_accepts(_conflict,area_cfg,1.0)
assert not ok and why=='explicit-outside-county',(ok,loc,why)
# OLX query coverage must mirror the accepted area list.
_cfg=json.load(open('config.json',encoding='utf-8'))
_olx=next(x for x in _cfg['sources'] if x['name']=='OLX')
assert set(_olx['api_queries'])==set(area_cfg['primary_localities']+area_cfg['nearby_localities'])
print('SELFTEST OK v1.4.3 canonical locality registry')

# v1.4.4: RCN parcel history must prefer the full official EGiB id. Parcel numbers
# alone repeat between cadastral precincts and can never be treated as globally unique.
_rc_hist=RCNClient(49.82625,20.8099,{})
_listing={
    'category':'plot','parcel_number':'142/1','parcel_id':'120000_2.0001.142/1',
    'parcel_id_confidence':'egib-exact','area_locality':'Lusławice','location':'Lusławice',
    'area_m2':3600,'lat':49.82625,'lon':20.8099,
}
_hist_rows=[
    {'tx_key':'a','transaction_id':'T-1','transaction_date':'2025-05-10T00:00:00+00:00','price':300000,'area_m2':3600,'price_m2':83.33,
     'parcel_number':'142/1','parcel_id':'120000_2.0001.142/1','price_basis':'parcel','address':'Lusławice','lat':49.82625,'lon':20.8099},
    {'tx_key':'b','transaction_id':'T-2','transaction_date':'2023-02-01T00:00:00+00:00','price':600000,'area_m2':8000,'price_m2':75,
     'parcel_number':'142/1','parcel_id':'120000_2.0001.142/1','price_basis':'property','address':'Lusławice','lat':49.82625,'lon':20.8099},
    # Same visible number but another official cadastral id: MUST NOT match.
    {'tx_key':'foreign','transaction_id':'T-X','transaction_date':'2024-01-01T00:00:00+00:00','price':999999,'area_m2':3600,'price_m2':277.77,
     'parcel_number':'142/1','parcel_id':'999999_9.9999.142/1','price_basis':'parcel','address':'inna miejscowość','lat':50.2,'lon':21.2},
]
_hist=_rc_hist.find_parcel_history(_listing,_hist_rows)
assert [x['transaction_id'] for x in _hist]==['T-1','T-2'],_hist
assert _hist[0]['history_match']=='egib-id-exact',_hist
assert _hist[1]['history_match']=='egib-id-property-level',_hist

# Without official id, a conservative fallback needs number + locality + compatible area.
_fallback=dict(_listing,parcel_id=None,parcel_id_confidence=None)
_fallback_hist=_rc_hist.find_parcel_history(_fallback,[_hist_rows[0],_hist_rows[2]])
assert len(_fallback_hist)==1 and _fallback_hist[0]['transaction_id']=='T-1',_fallback_hist

# Whole-property RCN entries may occur once per member parcel. They remain available
# for exact history, but must count only once in the market benchmark.
_now=datetime.now(timezone.utc)
_dup_prop=[]
for i in range(3):
    for parcel_id in ['P-A','P-B']:
        _dup_prop.append({
            'transaction_id':f'PROP-{i}','price_basis':'property','parcel_id':parcel_id,
            'transaction_date':(_now-timedelta(days=30*i)).isoformat(),
            'price':400000+i*10000,'area_m2':4000,'price_m2':100+i*2,
            'lat':49.82625+i*0.0002,'lon':20.8099,
        })
_bench=_rc_hist.analyze({'category':'plot','area_m2':3800,'lat':49.82625,'lon':20.8099},_dup_prop,24,3)
assert _bench['rcn_count']==3,_bench
print('SELFTEST OK v1.4.4 exact EGiB / RCN parcel history')

# v1.4.6: regression for the exact OLX false-positive class seen in production.
_reel={
    "url":"https://www.olx.pl/d/oferta/nowy-kolowrotek-samolla-ksn-8000-12-1-bb-karpiowy-surfcasting-1-sztuki-CID767-ID1ccuyw.html",
    "title":"Nowy Kołowrotek Samolla KSN 8000 | 12+1 BB | Karpiowy Surfcasting | 1 sztuki",
    "description":"Kołowrotek do połowu gruntowego i surfcastingu, odporny na słoną wodę.",
    "params":[],
    "location":{"city":{"name":"Bielsko-Biała"},"region":{"name":"Śląskie"}},
}
assert classify_category(_reel['title'],_reel['url'],_reel['description'],None)=='other',_reel
assert not Scraper._olx_offer_is_plot(_reel),_reel
_fake_plot=dict(_olx_offer,location={"city":{"name":"Bielsko-Biała"},"region":{"name":"Śląskie"}},description='Działka testowa; słona woda w pobliżu')
_fake_rec=Scraper._olx_record_from_api(_fake_plot)
assert _fake_rec['location']=='Bielsko-Biała' and _fake_rec['location_confidence']=='olx-api-structured',_fake_rec
ok,loc,why=area_accepts(_fake_rec,area_cfg,1.9)
assert not ok and loc is None and why=='olx-structured-location-outside-target',(ok,loc,why)
assert next(x for x in _cfg['sources'] if x['name']=='OLX').get('api_category_id')==3
print('SELFTEST OK v1.4.6 OLX strict category/location')


# v1.4.7 regressions: sale-only + OLX false positives
assert is_rental_offer('Powierzchnia 300m2','Do wynajęcia plac. Czynsz 3000 zł / mies.','','https://www.olx.pl/d/oferta/powierzchnia-300m2-CID3-ID1c2K6x.html')
assert not is_rental_offer('Działka budowlana 30 ar','Na sprzedaż działka budowlana','','https://www.olx.pl/d/oferta/dzialka-CID3-IDxxx.html')

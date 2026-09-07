import asyncio
import json
import unittest
from unittest.mock import Mock
from app.parser import parse_detail
from app.geocode import Geocoder, geocode_query
from app.area import area_accepts
from app.listing_data import coordinate_pair
from app.reliability import alert_location_eligible, safe_to_age_source
from app.scraper import Scraper
from app.egib import EGIBResolver


URL = 'https://www.otodom.pl/pl/oferta/dzialka-IDmain'
CENTER = {'lat':49.82625, 'lon':20.8099, 'radius_km':10}


def page(payload=None, entities=None, main='Działka na sprzedaż', extra=''):
    embedded = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>' if payload else ''
    linked = f'<script type="application/ld+json">{json.dumps(entities)}</script>' if entities else ''
    return f'<html><head>{embedded}{linked}</head><body><main><h1>Działka na sprzedaż</h1>{main}{extra}</main></body></html>'


class PrimaryEntityTests(unittest.TestCase):
    def test_otodom_payload_ignores_recommendations(self):
        advert = {'title':'Działka Słona 1500 m²', 'totalPrice':{'value':150000,'currency':'PLN'}, 'terrainArea':1500,
                  'createdAt':'2026-09-01T12:00:00Z', 'location':{'coordinates':{'latitude':49.84,'longitude':20.82}, 'address':{'city':{'name':'Słona'},'province':{'name':'małopolskie'},'county':{'name':'tarnowski'}}},
                  'recommendations':[{'price':990000,'createdAt':'2099-01-01','geo':{'latitude':54,'longitude':18}}]}
        record = parse_detail(page({'props':{'pageProps':{'ad':advert,'searchAds':{'items':[{'price':500}]}}}}), URL, 'Otodom','plot')
        self.assertEqual(record['price'],150000)
        self.assertEqual(record['area_m2'],1500)
        self.assertEqual(record['_listing_coords'],(49.84,20.82))
        self.assertEqual(record['published_text'],'2026-09-01T12:00:00Z')
        self.assertIn('tarnowski',record['_structured_location'])

    def test_jsonld_matches_requested_url_not_first_offer(self):
        entities = [{'@type':'Product','url':URL+'other','offers':{'price':999999},'geo':{'latitude':54,'longitude':18}},
                    {'@type':'Product','url':URL,'offers':{'price':120000},'geo':{'latitude':49.84,'longitude':20.82}}]
        record = parse_detail(page(entities=entities),URL,'Otodom','plot')
        self.assertEqual(record['price'],120000)
        self.assertEqual(record['_listing_coords'],(49.84,20.82))

    def test_unidentified_multiple_entities_are_not_merged(self):
        entities = [{'@type':'Offer','price':120000,'geo':{'latitude':49.84,'longitude':20.82}},
                    {'@type':'Offer','price':999999,'geo':{'latitude':54,'longitude':18}}]
        record = parse_detail(page(entities=entities),URL,'Otodom','plot')
        self.assertIsNone(record['price'])
        self.assertIsNone(record['_listing_coords'])

    def test_recommended_dom_does_not_fill_missing_fields(self):
        record = parse_detail(page(extra='<aside>Powierzchnia działki 5000 m². Cena 900 000 zł. Dodano 7 września 2026.</aside>'),URL,'Otodom','plot')
        self.assertIsNone(record['price'])
        self.assertIsNone(record['area_m2'])
        self.assertEqual(record['published_text'],'')

    def test_arbitrary_json_values_are_not_prices(self):
        record = parse_detail(page({'analytics':{'value':200000},'props':{'pageProps':{'searchAds':{'items':[{'price':250000}]}}}}),URL,'Otodom','plot')
        self.assertIsNone(record['price'])

    def test_no_refresh_date_as_publication(self):
        advert = {'title':'Działka Słona', 'modifiedAt':'2026-09-07', 'price':150000}
        record = parse_detail(page({'props':{'pageProps':{'ad':advert}}}),URL,'Otodom','plot')
        self.assertEqual(record['published_text'],'')
        self.assertEqual(record['updated_text'],'2026-09-07')

    def test_decimal_and_hectare_units(self):
        record = parse_detail(page(main='Powierzchnia działki 0,15 ha. Cena 150 000,50 zł.'),URL,'Otodom','plot')
        self.assertEqual(record['area_m2'],1500)
        self.assertAlmostEqual(record['price'],150000.5)

    def test_coordinates_are_finite_and_can_be_outside_poland(self):
        self.assertIsNone(coordinate_pair({'lat':'nan','lon':20}))
        self.assertIsNone(coordinate_pair({'lat':0,'lon':0}))
        self.assertEqual(coordinate_pair({'lat':40,'lon':-3}),(40,-3))

    def test_conflicting_title_does_not_overwrite_listing_area(self):
        advert = {'title':'Działka 3000 m²','terrainArea':1500,'price':150000}
        record = parse_detail(page({'props':{'pageProps':{'ad':advert}}}),URL,'Otodom','plot')
        self.assertEqual(record['area_m2'],1500)
        self.assertIn('Sprzeczny metraż',record['area_warning'])

    def test_conflicting_ppm_does_not_rewrite_total(self):
        record = parse_detail(page(main='Powierzchnia działki 1500 m². Cena 150 000 zł. Cena za m² 500 zł/m².'),URL,'Otodom','plot')
        self.assertEqual(record['price'],150000)
        self.assertEqual(record['price_m2'],100)
        self.assertIn('Cena/m²',record['area_warning'])


class GeographyTests(unittest.TestCase):
    def geocoder(self, items):
        database = Mock()
        database.geocode_get.return_value = None
        geocoder = Geocoder(database,CENTER)
        geocoder.session = Mock()
        geocoder.session.get.return_value.json.return_value = items
        return geocoder

    def test_two_zakliczyns_in_same_province_are_ambiguous(self):
        geocoder = self.geocoder([
            {'lat':49.856,'lon':20.809,'display_name':'Zakliczyn, powiat tarnowski, małopolskie'},
            {'lat':49.879,'lon':20.012,'display_name':'Zakliczyn, gmina Siepraw, powiat myślenicki, małopolskie'}])
        self.assertIsNone(geocoder.geocode('Zakliczyn','małopolskie'))
        self.assertEqual(geocoder.last_reason,'geocode-ambiguous')
        geocoder.db.geocode_put.assert_not_called()

    def test_explicit_county_disambiguates(self):
        geocoder = self.geocoder([
            {'lat':49.856,'lon':20.809,'display_name':'Zakliczyn, powiat tarnowski, małopolskie'},
            {'lat':49.879,'lon':20.012,'display_name':'Zakliczyn, powiat myślenicki, małopolskie'}])
        self.assertEqual(geocoder.geocode('Zakliczyn, powiat tarnowski','małopolskie'),(49.856,20.809))

    def test_far_unique_result_is_not_relocated(self):
        geocoder = self.geocoder([{'lat':49.879,'lon':20.012,'display_name':'Zakliczyn, powiat myślenicki, małopolskie'}])
        self.assertEqual(geocoder.geocode('Zakliczyn, powiat myślenicki','małopolskie'),(49.879,20.012))

    def test_missing_region_not_invented(self):
        geocoder = self.geocoder([])
        geocoder.geocode('Słona')
        self.assertEqual(geocoder.session.get.call_args.kwargs['params']['q'],'Słona, Polska')

    def test_query_preserves_structured_county(self):
        query = geocode_query({'location':'Zakliczyn','_structured_location':'Zakliczyn, powiat myślenicki'})
        self.assertIn('myślenicki',query)

    def test_old_cache_namespace_is_not_used(self):
        geocoder = self.geocoder([])
        geocoder.geocode('Słona')
        self.assertTrue(geocoder.db.geocode_get.call_args.args[0].startswith('v3-unambiguous|'))

    def test_radius_boundary_and_neighbouring_municipality(self):
        config = {'mode':'radius_verified','fallback_radius_km':10,'reject_unknown_location':True}
        record = {'location':'Siemiechów','description':'gmina Gromnik'}
        self.assertTrue(area_accepts(record,config,10)[0])
        self.assertFalse(area_accepts(record,config,10.001)[0])
        self.assertFalse(area_accepts(record,config,None)[0])
        self.assertFalse(area_accepts(record,config,float('nan'))[0])

    def test_wrong_zakliczyn_in_structured_location(self):
        record = {'location':'Zakliczyn, gmina Siepraw, powiat myślenicki'}
        self.assertFalse(area_accepts(record,{'mode':'radius_verified','fallback_radius_km':10},2)[0])


class ReliabilityTests(unittest.TestCase):
    def test_egib_requires_target_administrative_id_and_precinct(self):
        for parcel_id, precinct, accepted in [('121614_5.0001.17','Słona',True),('121908_2.0001.17','Słona',False),('121614_5.0001.17','',False)]:
            database = Mock()
            database.parcel_cache_get.return_value = None
            resolver = EGIBResolver(database,CENTER)
            resolver.s = Mock()
            resolver.s.get.return_value.content = f'<collection><member><parcel><numer_dzialki>17</numer_dzialki><nazwa_gminy>Zakliczyn</nazwa_gminy><nazwa_obrebu>{precinct}</nazwa_obrebu><id_dzialki>{parcel_id}</id_dzialki></parcel></member></collection>'.encode('utf-8')
            self.assertEqual(resolver.lookup('Słona','17') is not None,accepted)
            self.assertTrue(database.parcel_cache_get.call_args.args[0].startswith('v2-admin-verified|'))

    def test_locality_centroid_is_not_alert_quality(self):
        self.assertFalse(alert_location_eligible({'distance_km':2,'area_confidence':'radius-verified:location-geocode'},{'center':CENTER}))

    def test_portal_coordinate_uses_safety_margin(self):
        config = {'center':CENTER,'telegram':{'coordinate_margin_km':1}}
        self.assertTrue(alert_location_eligible({'distance_km':9,'area_confidence':'radius-verified:olx-api'},config))
        self.assertFalse(alert_location_eligible({'distance_km':9.5,'area_confidence':'radius-verified:olx-api'},config))
        self.assertTrue(alert_location_eligible({'distance_km':9.5,'area_confidence':'radius-verified:egib-exact'},config))

    def test_partial_success_does_not_age_listings(self):
        self.assertFalse(safe_to_age_source({'healthy':True,'records':1}))
        self.assertFalse(safe_to_age_source({'healthy':True,'coverage_complete':True,'blocked':1}))
        self.assertTrue(safe_to_age_source({'healthy':True,'coverage_complete':True}))

    def test_403_and_429_are_not_retried(self):
        for status in (403,429):
            scraper = Scraper({'browser':{}})
            scraper._http = Mock()
            scraper._http.get.return_value = Mock(status_code=status,url=URL,text='Access denied')
            result = asyncio.run(scraper._http_get_retry(URL,attempts=3,delays=()))
            self.assertEqual(result[-1],[status])
            asyncio.run(scraper._http_get_retry(URL+'/other',attempts=3,delays=()))
            self.assertEqual(scraper._http.get.call_count,1)

    def test_olx_structured_api_preserves_coordinates(self):
        offer = {'title':'Działka budowlana Słona','url':'https://www.olx.pl/d/oferta/dzialka-CID3-IDabc.html',
                 'price':{'value':150000,'label':'150 000 zł'},'description':'Powierzchnia działki 1500 m².',
                 'location':{'city':{'name':'Słona'},'region':{'name':'małopolskie'}},'map':{'lat':49.84,'lon':20.82},
                 'created_time':'2026-09-01T12:00:00Z','params':[]}
        record = Scraper._olx_record_from_api(offer)
        self.assertIsNotNone(record)
        self.assertEqual(record['_olx_structured_lat'],49.84)
        self.assertEqual(record['price'],150000)
        self.assertEqual(record['area_m2'],1500)


if __name__ == '__main__':
    unittest.main()

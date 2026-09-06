# Property Radar v1.3.1 — Otodom Next.js collector

- Otodom ma dedykowany collector zamiast wspólnego scrapera kart HTML.
- Discovery czyta `script#__NEXT_DATA__` i `searchAds.items`.
- Linki ofert są budowane z `detailUrl/url/href/canonicalUrl/slug`, bez zależności od klas CSS.
- 3 strony wyników, do 80 detali, równolegle 6 requestów.
- Retry dla błędów transportowych/403/429/5xx z twardym budżetem czasu.
- Playwright jest używany najwyżej jako pojedynczy fallback wyszukiwarki; detale są HTTP-first bez otwierania wielu kart.
- Diagnostyka pokazuje `next_data_pages`, `next_data_items`, statusy prób HTTP i detail HTTP, fallback przeglądarki oraz timeouty.
- Usunięto awaryjne odkrywanie ofert Otodom poprzez wyniki OLX.

Pozostałe portale nie zostały zmienione.

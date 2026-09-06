# Property Radar — poprawki v1.3.0

## 1. Porównania cen tylko w zł/m²
- `price_m2` jest osobnym polem i ma pierwszeństwo w benchmarkach.
- Cena całkowita nie jest używana do procentu `vs ogłoszenia` ani `vs RCN`.
- Parser rozdziela cenę całkowitą od `Cena za m²` / `zł/m²`.
- Naprawiony przypadek OLX: `97.22 zł/m²` nie może zostać odczytane jako `9722 zł` ceny całkowitej.
- Naprawiony tokenizer kwot: numer oferty obok ceny nie może zostać sklejony z ceną.
- UI pokazuje teraz jawnie: `cena/m² oferty vs mediana/m²`.

## 2. OLX / Otodom — timeout źródła
- Limity czasu są konfigurowalne per portal.
- Detale są przetwarzane partiami; timeout jednego detalu nie blokuje kolejnych.
- Jest budżet czasu dla detali i częściowe wyniki nie są tracone przez jeden wiszący request.
- OLX używa krótszej, bardziej lokalnej strony wyników oraz ograniczonej liczby detali.

## 3. Tabelaofert
- Dedykowane czyszczenie discovery: jeśli strona mówi `Znaleziono N ofert`, scraper bierze pierwsze N właściwych linków zamiast linków z rekomendacji.
- Poprawione parsowanie ceny całkowitej / ceny za m² / powierzchni.
- `detail_pages_ok` oznacza teraz faktycznie rozpoznane strony ofert; osobno raportowane są pobrane detale.

## 4. Testy
`cd scraper && python selftest.py`

Dodane regresje dla OLX, Tabelaofert i invariant scoringu tylko po `price_m2`.

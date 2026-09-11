# Property Radar 1.6.2 — Bieśnik + 10 km

Prywatny radar działek: Cloudflare Worker + D1, skaner Python/Playwright w GitHub Actions, panel WWW / Telegram Mini App.

## 1.6.2 — panel nastawiony na oferty

- Usunięto marketingowe/powitalne teksty z głównego widoku. Panel zaczyna się od skanu, statystyk i listy ofert.
- Przywrócono informacyjne, szerokie karty: duże zdjęcie z oryginalnego ogłoszenia, bezpośredni link do portalu, cena, cena/m², metraż, data, odległość, typ, WZ/MPZP i numer działki.
- Zdjęcie, tytuł i przycisk źródła są bezpośrednio klikalne. Szczegóły pokazują również zdjęcie, pełny URL, daty z portalu, pierwszy/ostatni odczyt oraz opis ogłoszenia.
- Worker ma uwierzytelniony proxy obrazów z fallbackiem do bezpośredniego CDN portalu; ogranicza to problemy z hotlinkowaniem. Parser ma dodatkowe źródła zdjęcia (`og:image`, JSON-LD/payload, galeria w DOM).
- Zmiana parsera zdjęć ma nową wersję, więc następny pełny skan odświeży `image_url` także dla istniejących ofert, jeśli portal nadal udostępnia zdjęcie.

## Co zmieniono

- Parser wybiera dane aktualnej oferty, zamiast przeszukiwać cały JSON strony wraz z rekomendacjami. Obsługuje główny rekord `__NEXT_DATA__`, wybrane encje JSON-LD i istniejący adapter publicznych danych OLX.
- Cena i metraż mają pierwszeństwo z pól oferty. Tytuł nie nadpisuje sprzecznego metrażu; rozbieżność jest ostrzeżeniem. Cena/m² wynika z ceny całkowitej i powierzchni. Poprawiono kwoty z groszami, np. `150 000,50 zł`.
- Dane geograficzne głównej oferty mają pierwszeństwo przed geokodowaniem. Odległość jest liczona od punktu z konfiguracji: **49.82625, 20.80990**, promień **10 km w linii prostej**. Przyjęto punkt Bieśnika z dostarczonego projektu, nie ustalano go ponownie.
- Samo „Zakliczyn, małopolskie” nie wystarcza do geokodowania. Wymagany jest kontekst administracyjny; geokoder nie wybiera najbliższego spośród kilku miejscowości. Jawna lokalizacja poza obszarem nie jest przenoszona do lokalnego imiennika. Stary cache geokodera nie jest używany.
- EGiB wymaga zgodności obrębu i prefiksu jednostki ewidencyjnej Zakliczyna `121614`; nie wystarcza sama nazwa gminy lub sam numer działki. Jego cache również ma nową przestrzeń kluczy. Ten adapter nadal nie rozwiązuje działek z sąsiednich gmin — ich ogłoszenia mogą być przyjmowane na podstawie pozostałych danych lokalizacji.
- Po HTTP 403/429 skaner nie ponawia żądań HTTP do tego hosta w bieżącym skanie; nie próbuje obchodzić odmowy przez sesję przeglądarki. Częściowy wynik nie oznacza zdrowego źródła.
- Brak oferty w limitowanym skanie **nie wygasza jej automatycznie**. Wygaszanie przez nieobecność wymaga jawnego `coverage_complete=true` oraz braku błędów. Obecne kolektory nie dowodzą kompletności, więc ta ścieżka jest celowo wyłączona. Jawny status archiwalny z portalu nadal działa. To chroni poprawne oferty przy blokadzie lub ograniczeniu liczby stron, ale może pozostawiać stare oferty wymagające sprawdzenia.

## Alerty: ostrożność zamiast zgadywania

Domyślnie alert nowej oferty wymaga daty publikacji (nie odświeżenia), odpowiedniego wieku i lokalizacji kwalifikującej się do powiadomień:

- punkt geometrii działki z dopasowania EGiB w promieniu 10 km; lub
- współrzędne aktualnej oferty z portalu, z **1 km marginesem** od granicy zasięgu.

Środek miejscowości ustalony geokoderem może dopuścić ofertę do panelu, ale **nie wysyła domyślnie alertu**. Niejednoznaczne lokalizacje są odrzucane i zapisywane w diagnostyce. Skutek jest zamierzony: mniej fałszywych alertów kosztem części prawdziwych ofert. Odrzucone rekordy nie mają osobnej skrzynki w panelu; powody są w artefakcie GitHub Actions `logs/rejected_area.json`. Istniejące stare rekordy nie są masowo kasowane przez tę aktualizację.

Punkt portalu może być przybliżony, a margines nie stanowi gwarancji dokładności. Dane EGiB są reprezentowane punktem geometrii, nie analizą całej granicy działki. Przed zakupem należy sprawdzić dokumenty i rzeczywiste położenie.

Ustawienia w `scraper/config.json`:

```json
"telegram": {
  "coordinate_margin_km": 1.0,
  "allow_locality_center_alerts": false
}
```

To fragment istniejącej sekcji — nie należy usuwać jej pozostałych pól. Pierwszy skan po aktualizacji resetuje odniesienie zmian cen, żeby korekta parsera nie udawała okazji rynkowej. Oferta początkowo zapisana bez jakości lokalizacji wymaganej do alertu nie otrzyma później automatycznego alertu „nowa”, jeśli dopiero w kolejnym skanie zostanie doprecyzowana.

## Panel po swojemu

- Jasny, stonowany wygląd oraz tryb ciemny; układ kafelkowy lub lista.
- „Dostosuj”: chowanie powitania, statystyk, wskazówek i centrum skanowania. Centrum można też zwyczajnie zwinąć.
- Zapisane oferty, odwracalne ukrywanie, porównanie maksymalnie 3 działek, prywatne notatki w szczegółach.
- Informacja o źródle i przybliżeniu lokalizacji. Wskazówki zmieniają się na życzenie, bez automatycznej karuzeli.
- Preferencje zapisują się w przeglądarce. Notatki, zapisane i ukryte oferty są oddzielone według użytkownika panelu; nie synchronizują się między urządzeniami. Wyczyszczenie pamięci przeglądarki je usuwa.
- „Ukryj” działa tylko lokalnie. Dotychczasowe „Usuń i zablokuj” administratora nadal usuwa rekord z D1 i blokuje URL dla wszystkich użytkowników.

## Bezpieczny podgląd bez kont i sekretów

W katalogu projektu, z Node.js 22 lub nowszym:

```sh
npm run preview
```

Otwórz `http://127.0.0.1:4173`. Serwer nasłuchuje wyłącznie lokalnie. Ogłoszenia i ilustracje są **przykładowe**, widok ma oznaczenie podglądu. Nie łączy się z D1, nie uruchamia skanów ani nie wysyła alertów. Serwer podglądu nie jest kodem produkcyjnego Workera i nie jest wdrażany przez Wrangler.

## Wdrożenie do istniejącej instalacji

1. Zachowaj kopię repozytorium i eksport D1. Materiał wejściowy znaleziono w rozpakowanym folderze `Property_Radar_Cloud_v1.0.0`, ale jego `package.json` wskazywał wersję **1.5.3**. Wskazanego ZIP-a nie było na dysku.
2. Przenieś zmienione pliki do własnego repozytorium. Zachowaj własne `wrangler.jsonc`, sekrety, identyfikator bazy i adres panelu; w tej kopii dotychczasowa konfiguracja została zachowana.
3. Uruchom testy niżej, potem `npm install` i `npm run deploy` po zwykłym uwierzytelnieniu Cloudflare. Nie potrzeba nowego schematu D1 ani resetowania tabel.
4. Zaktualizowany kod skanera musi być również w gałęzi uruchamianej przez workflow GitHub Actions. Sam deploy Workera aktualizuje panel, nie skaner na GitHubie.
5. Zleć jeden kontrolny skan i sprawdź diagnostykę OLX/Otodom, odrzucenia lokalizacji oraz ręcznie kilka konkretnych ofert. Nie traktuj zielonego HTTP 200 jako dowodu kompletności danych.

W ramach przygotowania tej wersji **nie wykonano wdrożenia, migracji produkcyjnej, skanu produkcyjnego ani wysyłki Telegram**. Oryginalny katalog projektu pozostaje bez zmian.

## Testy

Python 3.12+:

```sh
python -m pip install -r scraper/requirements.txt
python scraper/selftest.py
python -m unittest discover -s scraper -p "test_*.py" -v
node --check src/index.js
node --check public/app.js
node --check public/workspace.js
```

Test UI przy uruchomionym podglądzie i zainstalowanym Chrome: `python scripts/test_ui.py`. Sprawdza zapis/ukrywanie/przywracanie, porównania, notatki, trwałość ustawień, filtrowanie brakujących cen, sortowanie oraz szerokości 320/390/768/1440 px. Zrzuty zapisuje w `test-artifacts/`.

Sprawdzenie dostępności dwóch portali bez skanu i powiadomień: `python scripts/smoke_sources.py`. W środowisku przygotowania testy na żywo zatrzymały się na weryfikacji certyfikatów TLS, zarówno w Pythonie, jak i systemowym kliencie HTTP. Nie wyłączano weryfikacji certyfikatów. **Nie potwierdzono aktualnej dostępności OLX/Otodom ani parsowania ich dzisiejszych stron produkcyjnych.** Testy parsera używają syntetycznych, kontrolowanych danych regresyjnych. Żaden parser nie gwarantuje dostępu przy blokadzie portalu lub zmianie jego schematu.

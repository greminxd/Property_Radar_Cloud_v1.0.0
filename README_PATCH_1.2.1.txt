PROPERTY RADAR CLOUD v1.2.1 — PARSER FIX + v1.2.0 FEATURES
===========================================================

To jest skonsolidowany patch: zawiera wszystko z v1.2.0 plus poprawkę parsera metrażu.

NOWA POPRAWKA 1.2.1
------------------
- metraż działki jest brany najpierw z jawnie opisanych pól typu:
  "Powierzchnia działki", "Powierzchnia gruntu", "Powierzchnia parceli", "Działka: ... 37000 m²",
- obsługa JSON-LD lotSize / landArea / plotArea / parcelArea,
- powierzchnia domu/lokalu nie może już wygrać z polem "Powierzchnia działki",
- przykład regresyjny: tytuł "DOM, 350m, 3.74ha..." + pole "Powierzchnia działki 37000 m²"
  daje area_m2=37000, a nie 350 ani 37400,
- mocny tytuł DOM/MIESZKANIE/LOKAL/HALA jest klasyfikowany jako "other" i odrzucany,
  bo radar ma zbierać tylko DZIAŁKI + GARAŻE. Jeśli portal błędnie wrzuci dom do wyników działek,
  nie dostaniesz z niego alertu.

FUNKCJE Z 1.2.0
---------------
- prawdziwa data publikacji osobno od aktualizacji i first_seen,
- stare/archiwalne oferty zapisane w bazie, ale bez alertu "NOWA",
- Mini App domyślnie: aktywne oferty opublikowane w ostatnich 30 dniach,
- Telegram: alert tylko dla faktycznie świeżej publikacji,
- istotne zmiany ceny: 300000 -> 299999 i 300000 -> 299000 nie alarmują,
- zdjęcie z ogłoszenia w alercie przez sendPhoto + fallback do tekstu,
- ceny ofertowe i RCN są pokazane osobno,
- RCN: ostatnie 24 miesiące, cache w D1 maks. 1 odświeżenie dziennie,
- brak pseudo-rankingu prywatności bez precyzyjnej geometrii,
- Telegram = monitoring/status; rozbudowane filtry są w Mini App,
- status bota i status bazy są osobne,
- automatyczny skan 09:00 i 20:00 Europe/Warsaw + limit 2 skanów/dobę,
- cleanup Playwright / watchdog / diagnostyka bootowania.

INSTALACJA
----------
1. Nadpisz zawartością ZIP-a pliki w root repo.
2. Commit + push.
3. Poczekaj na deploy Cloudflare.
4. GitHub Actions -> Setup D1 + Telegram -> Run workflow JEDEN RAZ.
5. Potem jeden testowy Scan nieruchomosci, o ile dzienny limit nie został osiągnięty.

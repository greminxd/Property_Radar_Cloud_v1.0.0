# Property Radar 1.6.1

Zmiany w tej wersji są skupione na Mini App i zdjęciach ofert.

- Usunięto marketingowy hero, slogany i sekcję porad z głównego widoku.
- Zachowano nową, stonowaną stylistykę, ale przywrócono szerokie, informacyjne karty ofert.
- Karty pokazują: prawdziwe zdjęcie z ogłoszenia, cenę, cenę/m², powierzchnię, datę publikacji, odległość, typ działki, MPZP/WZ, numer działki, krótki opis i źródło.
- Zdjęcie, tytuł i przycisk portalu prowadzą bezpośrednio do oryginalnego ogłoszenia.
- Widok szczegółów pokazuje duże zdjęcie, pełny URL, daty publikacji/odświeżenia, first_seen/last_seen, opis i historię ceny.
- Dodano endpoint `/api/listing/:id/image`, który pobiera zdjęcie przez Worker; frontend ma fallback do bezpośredniego URL CDN.
- Parser zdjęć korzysta kolejno z `og:image`, JSON-LD, głównego payloadu strony i obrazów galerii w DOM.
- Podbito `LISTING_PARSER_VERSION`, więc kolejny pełny skan ponownie zapisze zdjęcia dla ofert, dla których portal je udostępnia.
- Nie zmieniano logiki źródeł, harmonogramu ani zasad wysyłania alertów Telegram w ramach tej poprawki UI.

Testy: selftest parsera + 24 testy jednostkowe + `node --check` dla Workera i frontendu przechodzą. Test przeglądarkowy nie mógł zostać uruchomiony w środowisku roboczym z powodu lokalnej polityki przeglądarki blokującej `127.0.0.1`; lokalny preview i endpoint obrazu zostały sprawdzone przez HTTP.

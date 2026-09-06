# Property Radar Cloud v1.4.9 — Telegram/D1 fix + dark compact UI

## Naprawione

- **Telegram „Status skanu” i „Baza”** nie zależą już od tabeli `listing_blacklist` dodanej w v1.4.8. Na starszej istniejącej bazie D1 brak tej tabeli mógł wywalić cały `databaseStats()`, a przez to również status bota.
- **Mini App `/api/listings`** ma bezpieczny fallback dla D1 sprzed migracji blacklisty. Brak opcjonalnej tabeli nie może już powodować pustej aplikacji / HTTP 500.
- Akcja **„Błędna oferta → usuń i zablokuj”** sama tworzy tabelę blacklisty, jeżeli migracja nie była jeszcze wykonana.
- Telegramowe **„Nowe ogłoszenia”** bierze `first_seen`, gdy portal nie podał `published_at`.
- Mini App domyślnie pokazuje **wszystkie aktywne działki**, a nie tylko wpisy z rozpoznaną datą z ostatnich 30 dni. Dzięki temu poprawna oferta bez `published_at` nie znika z ekranu.

## UI

- usunięty jasny „Apple feed” z v1.4.8,
- powrót do zwartego, **ciemnego stylu inspirowanego pierwotną wersją**, z turkusowym akcentem,
- brak wielkiego graficznego radaru / orbity,
- kompaktowy status skanu + zwykły pasek postępu,
- małe kafle statystyk,
- wyszukiwarka i filtry na pierwszym planie,
- zwarte karty ofert z mocno widoczną ceną i **zł/m²**,
- dark bottom-sheet filtrów i dark ekran szczegółów.

## Ważne

Parser i wersja migracji rekordów pozostają z v1.4.8 — zmiana UI/Worker compatibility nie udaje nowej migracji parsera i nie powinna niepotrzebnie przeliczać/dezaktywować istniejącej bazy.

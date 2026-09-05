# Property Radar Cloud v1.2.0

Prywatny monitoring działek i garaży dla strefy **Zdonia / Zakliczyn / Słona + bliskie okolice**.

## Architektura

- **Cloudflare Worker** — prywatny panel WWW/Mini App, API, logowanie i webhook Telegrama.
- **Cloudflare D1** — trwała baza ofert, historia cen, status skanów i cache realnych transakcji RCN.
- **GitHub Actions** — Python + Playwright + Chromium; automatyczny skan maks. 2 razy dziennie.
- **Telegram** — alerty o faktycznie nowych publikacjach oraz istotnych zmianach cen, status bota i bazy.

## Skanowanie

Automatycznie o **09:00 i 20:00 czasu Europe/Warsaw**. Dodatkowo scraper ma twardy limit maksymalnie dwóch zakończonych skanów dziennie, więc przypadkowe kolejne uruchomienie kończy się przed startem Chromium.

## Daty ofert

System rozdziela:

- `published_at` — pierwotna data publikacji na portalu,
- `updated_at` — data odświeżenia / aktualizacji na portalu,
- `first_seen` — kiedy Property Radar pierwszy raz zobaczył URL.

Stara oferta wykryta dzisiaj nie staje się przez to "nowa". Mini App domyślnie pokazuje **aktywne oferty opublikowane w ostatnich 30 dniach**. Oferta bez pewnej daty publikacji nie dostaje alertu "NOWA".

Oferty z oznaczeniami typu „Ogłoszenie archiwalne”, „Oferta nieaktualna” są przechowywane historycznie, ale mają `active=0`, nie wysyłają alertów i nie są widoczne w domyślnym widoku.

## Ceny i alerty

Każda rzeczywista zmiana ceny jest zachowana w `price_history`, ale Telegram nie alarmuje o kosmetycznych zmianach. Domyślnie alert jest istotny, jeśli zmiana wynosi co najmniej **1000 zł** i jednocześnie co najmniej **5000 zł lub 3%**.

Przykłady:

- 300 000 → 299 999: brak alertu,
- 300 000 → 299 000: brak alertu,
- 300 000 → 295 000: alert,
- 50 000 → 48 000: alert (4%).

Próg jest liczony kumulacyjnie od ostatniej ceny referencyjnej, więc seria małych obniżek nie omija alarmu.

## Dwa osobne benchmarki cenowe

1. **Ceny z ogłoszeń** — mediana i średnia porównywalnych aktywnych ofert.
2. **RCN** — rzeczywiste ceny transakcyjne działek z Rejestru Cen Nieruchomości, domyślnie ostatnie 24 miesiące.

Dla pojedynczej oferty RCN dobiera transakcje o podobnym metrażu w promieniu 3 km, potem 5 km i 10 km, zależnie od liczby danych. Mini App pokazuje także ostatnie porównywalne transakcje w pobliżu.

RCN jest odświeżany najwyżej raz dziennie; drugi skan używa cache D1.

## Prywatność

Stary tekstowy score prywatności został wyłączony. Bez dokładnej geometrii działki / numeru działki i danych o budynkach nie ma sensu udawać precyzyjnej oceny odosobnienia.

## Telegram

Główne menu:

- 🏡 Otwórz Mini App
- 🆕 Nowe ogłoszenia
- 📉 Zmiany cen
- 🤖 Status bota
- 🗃 Status bazy
- 🧪 Diagnostyka — admin

Komendy:

- `/start`
- `/nowe`
- `/ceny`
- `/status`
- `/statuspelny`
- `/baza`
- `/id`
- `/diag`

Alert próbuje wysłać pierwsze zdjęcie z ogłoszenia przez `sendPhoto`; gdy portal/Telegram odrzuci obraz, automatycznie przechodzi na wiadomość tekstową.

## Mini App

Domyślnie: aktywne + publikacja do 30 dni.

Filtry obejmują status, wiek publikacji, typ działki, WZ/MPZP, miejscowość, portal, powierzchnię, cenę, zł/m², telefon, numer działki, istotne zmiany ceny i maksymalną różnicę ceny względem mediany RCN.

## Aktualizacja istniejącej instalacji do 1.2.0

1. Wgraj pliki patcha do root repo i push.
2. Poczekaj na zielony deploy Cloudflare.
3. GitHub Actions → **Setup D1 + Telegram** → Run workflow — jeden raz. Migrator zachowuje istniejące dane i dodaje nowe kolumny/tabele.
4. Opcjonalnie uruchom jeden ręczny **Scan nieruchomosci**, o ile limit 2 skanów danego dnia nie został osiągnięty.
5. Potem zostaw harmonogram 09:00 / 20:00.

Nie są potrzebne nowe sekrety dla RCN.

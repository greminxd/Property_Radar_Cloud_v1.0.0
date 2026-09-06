Property Radar Cloud v1.2.2

- Radar monitoruje WYŁĄCZNIE działki; garaże/parkingi usunięte z URL-i, filtrów, statusów i klasyfikacji.
- Gratka: linki szczegółów zawężone do /ob/<id>, zamiast łapania stron kategorii.
- Tabelaofert: linki szczegółów zawężone do ofert z numerycznym ID.
- Oferty.net: tylko działki.
- Otodom/OLX/Gratka/Tabelaofert: parser czeka na dynamiczny H1, ponownie pobiera DOM i używa tekstu renderowanego jako fallback dla ceny/metrażu.
- Parser odczytuje też application/json / __NEXT_DATA__ (cena, daty, zdjęcie) na portalach JS-heavy.
- Zachowane: 09:00 i 20:00 Europe/Warsaw, max 2 skany/dzień, świeżość ofert, historia cen, istotne alerty cenowe, RCN, zdjęcia Telegram, status bota/bazy.

Po wgraniu: commit+push. Setup D1 + Telegram uruchomić raz. Scan nieruchomosci nie trzeba ręcznie uruchamiać — schedule odpali go automatycznie.

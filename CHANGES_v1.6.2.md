# Property Radar 1.6.2

## OLX fallback fix

- Naprawiono błąd w `_collect_olx`: odpowiedź HTTP `403/429` z publicznego `/api/v1/offers` zatrzymywała dalsze zapytania, ale jednocześnie błędnie wyłączała fallback przez prawdziwą sesję Chromium.
- `api_fail_fast` nadal zatrzymuje bezsensowne powtarzanie tych samych requestów HTTP dla kolejnych miejscowości, ale nie blokuje `browser_session_fallback`.
- Dzięki temu skan może odzyskać oferty z requestów wykonywanych przez stronę OLX albo z wyrenderowanych linków, gdy GitHub Runner jest blokowany na zwykłym HTTP.
- Nie zmieniano parsera ofert, filtrów lokalizacji ani `LISTING_PARSER_VERSION`.

# v1.3.2 — Telegram menu setup fix

- usunięto zbędny `setChatMenuButton` z `type=default` przed ustawieniem `web_app`;
- per-chat menu jest ustawiane bezpośrednio na Mini App;
- błąd pojedynczego per-chat override nie przerywa całego setupu Cloudflare/D1;
- błędy Telegram API pokazują teraz HTTP status i `description` z odpowiedzi API;
- analogiczny reset `default` usunięto z naprawy menu wykonywanej przy `/start`;
- scraper Otodom NextData z v1.3.1 pozostaje bez zmian.

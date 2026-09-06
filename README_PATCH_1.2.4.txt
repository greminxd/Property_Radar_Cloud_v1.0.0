Property Radar Cloud v1.2.4 — Telegram menu + latency + desktop UI

NAPRAWIONE:
1) Telegram dolny przycisk „🏡 Oferty”:
   - poprzedni Worker przekazywał chat_id do setChatMenuButton jako string;
   - teraz używa integer;
   - Setup robi reset globalnego i per-chat menu button;
   - Setup wykorzystuje TELEGRAM_CHAT_IDS i po ustawieniu sprawdza getChatMenuButton;
   - webhook jest kasowany i ustawiany od nowa z drop_pending_updates=True, a URL jest weryfikowany przez getWebhookInfo.

2) Telegram latency:
   - webhook czyta mały JSON update przed odpowiedzią i natychmiast zwraca HTTP 200;
   - w tle przetwarzany jest zwykły obiekt, nie otwarty Request;
   - status pobiera DB + system_state równolegle;
   - callback ACK leci równolegle z pobieraniem statusu.

3) Panel/Mini App:
   - szerszy desktop (do 1520 px);
   - 2 kolumny ofert na dużych ekranach;
   - mniej przeładowane karty: szczegółowa analityka rynku/RCN jest w „Szczegóły”, karta pokazuje skrót;
   - dodany szybki filtr „Wszystkie aktywne”;
   - poprawiona czytelność toolbaru, kart i statystyk.

PO WDROŻENIU:
- commit + push;
- poczekaj na Cloudflare deploy;
- Actions -> Setup D1 + Telegram -> Run workflow JEDEN RAZ;
- w logu setup musi być m.in.:
  [OK] Webhook verified: https://property-radar.hoolz.workers.dev/telegram/webhook
  [OK] menu chat 371510211: type=web_app url=https://property-radar.hoolz.workers.dev
- potem /start.

UWAGA: TELEGRAM_CHAT_IDS musi pozostać jako Environment secret w production.

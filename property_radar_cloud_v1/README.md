# Property Radar Cloud v1.0

Prywatny agregator działek i garaży dla strefy **Zdonia / Zakliczyn / Słona + bliskie okolice**.

## Co jest gdzie

- **Cloudflare Worker** — panel WWW, API, logowanie, Telegram webhook i Mini App.
- **Cloudflare D1** — trwała baza ofert i historia cen. Działa nawet gdy PC jest wyłączony.
- **GitHub Actions** — Python + Playwright + Chromium headless, czyli ciężkie skanowanie portali.
- **Telegram** — alerty, `/start`, `/status`, `/skanuj`, skróty ofert i Mini App.
- **HypeHoolz / stary Worker `hype`** — NIE JEST DOTYKANY.

## Co bot zbiera

11 źródeł: Otodom, OLX, Morizon, Gratka, Nieruchomosci-online, Adresowo, Domiporta, Sprzedajemy, Lento, Oferty.net, Tabelaofert.

Dla każdej oferty: cena, m², zł/m², lokalizacja, odległość, telefon (gdy publicznie dostępny), nr działki, data, źródło, zdjęcie, opis, typ działki, MPZP/WZ, historia ceny, scoring prywatności i ocena ceny względem lokalnej mediany porównawczej.

Nie ma filtra minimalnego metrażu. Podejrzane wartości (np. 15 m² vs 15 ar w opisie) są oznaczane, nie odrzucane.

## Strefa

Główne: **Zdonia, Zakliczyn, Słona**.

Bliskie: **Bieśnik, Kończyska, Olszowa, Paleśnica, Lusławice, Wesołów**.

Milówka, Złota i jawnie wskazane dalsze miejscowości są odrzucane. Dla niejasnej lokalizacji działa ostrożny fallback odległości do 5 km od Bieśnika.

---

# Instalacja — po kolei

## 1. GitHub

Utwórz **nowe prywatne repo**:

`property-radar`

Wgraj do niego CAŁĄ zawartość tego ZIP-a. Nie wrzucaj tego do repo HypeHoolz.

## 2. Cloudflare D1

Cloudflare Dashboard → **Storage & databases → D1 → Create database**.

Nazwa:

`property-radar-db`

Po utworzeniu skopiuj **Database ID**.

W repo otwórz `wrangler.jsonc` i zmień:

`PUT_D1_DATABASE_ID_HERE`

na prawdziwe Database ID.

Zmień też:

`PUT_GITHUB_USER/property-radar`

na np.:

`greminxd/property-radar`

Zapisz/commit.

## 3. Nowy Worker — NIE `hype`

Cloudflare → **Compute → Workers & Pages → Create application → Import a repository**.

Wybierz nowe repo `property-radar`.

Nazwa Workera:

`property-radar`

Build command: zostaw pusty.

Deploy command:

`npx wrangler deploy`

Cloudflare powinien użyć `wrangler.jsonc` i wdrożyć Worker + katalog `public/`.

Docelowy adres będzie podobny do:

`https://property-radar.hoolz.workers.dev`

Nie potrzebujesz na początku własnej domeny ani subdomeny HypeHoolz.

## 4. Sekrety runtime w Cloudflare Worker

Worker `property-radar` → **Settings → Variables & Secrets**.

Dodaj jako **Secrets**:

- `PANEL_PASSWORD` — Twoje mocne hasło do panelu.
- `SESSION_SECRET` — losowy sekret minimum ~40 znaków.
- `TELEGRAM_BOT_TOKEN` — token z BotFather.
- `TELEGRAM_ALLOWED_USER_ID` — Twój Telegram user_id.
- `TELEGRAM_WEBHOOK_SECRET` — drugi losowy sekret.

Opcjonalnie, żeby działał przycisk **Skanuj teraz** z panelu/Telegrama:

- `GITHUB_DISPATCH_TOKEN` — fine-grained GitHub PAT z dostępem do tego repo i Actions: Read and write.

Losowe sekrety możesz wygenerować lokalnie:

`python scripts/generate_secrets.py`

`SESSION_DAYS=30` jest już ustawione w `wrangler.jsonc`.

## 5. GitHub Actions Secrets

Repo GitHub → **Settings → Secrets and variables → Actions → New repository secret**.

Dodaj:

- `CF_ACCOUNT_ID` — ID konta Cloudflare.
- `CF_D1_DATABASE_ID` — Database ID z kroku 2.
- `CF_D1_API_TOKEN` — token Cloudflare z uprawnieniami **D1 Read + D1 Write** dla tego konta.
- `TELEGRAM_BOT_TOKEN` — ten sam token bota.
- `TELEGRAM_CHAT_ID` — prywatny chat_id, na który mają iść alerty.
- `TELEGRAM_WEBHOOK_SECRET` — identyczny jak w Workerze.
- `PANEL_URL` — np. `https://property-radar.hoolz.workers.dev`.

### CF_ACCOUNT_ID

Cloudflare → Account home. Account ID możesz skopiować z danych konta / API section.

### CF_D1_API_TOKEN

Cloudflare → My Profile → API Tokens → Create Custom Token.

Nadaj tylko potrzebne uprawnienia do D1. Nie używaj Global API Key.

## 6. Zainicjalizuj bazę i Telegram

GitHub → repo → **Actions → Setup D1 + Telegram → Run workflow**.

Workflow:

1. tworzy tabele w D1 z `schema.sql`,
2. ustawia Telegram webhook na Workerze,
3. ustawia komendy bota,
4. ustawia przycisk menu **🏡 Oferty** jako Telegram Mini App.

Po zielonym zakończeniu napisz botowi `/start`.

## 7. Pierwszy skan

GitHub → **Actions → Scan nieruchomosci → Run workflow**.

Pierwszy skan może trwać kilka–kilkadziesiąt minut, bo bot odwiedza konkretne strony ofert w Chromium.

Po zakończeniu:

- D1 zawiera oferty,
- Telegram dostaje podsumowanie i maksymalnie 10 najciekawszych ofert pierwszego uruchomienia,
- panel pokazuje pełną bazę.

Jeżeli coś nie działa, w wyniku workflow jest artifact `scan-diagnostics-*` z:

- `scan_diagnostics.json`
- `rejected_area.json`
- `last_errors.txt`

## 8. Automatyka

`scan.yml` ma domyślnie jeden skan dziennie:

`15 6 * * *` (06:15 UTC)

oraz ręczne `Run workflow`.

Nie ma skanowania co 15 minut — to celowe, żeby ograniczyć blokady portali i zużycie GitHub Actions.

---

# Telegram

Po konfiguracji działają:

- `/start`
- `/status`
- `/id`
- `/skanuj` — po dodaniu `GITHUB_DISPATCH_TOKEN`
- przyciski Najnowsze / Okazje / Prywatne / Duże / Garaże
- `🏡 Oferty` — Mini App

Mini App uwierzytelnia Twoje konto Telegram kryptograficznie. Użytkownik o innym `user_id` nie dostanie sesji do API.

W zwykłej przeglądarce pojawia się ekran hasła `PANEL_PASSWORD`.

## Dlaczego bez Cloudflare Access na całym Workerze?

Bo ten sam Worker przyjmuje publiczny webhook Telegrama. Worker-level Access zablokowałby Telegram przed dotarciem do `/telegram/webhook`, chyba że robilibyśmy dodatkowe reguły bypass. Tu jest prościej:

- dane API są chronione sesją,
- zwykła przeglądarka wymaga hasła,
- Telegram używa podpisanego `initData`,
- webhook wymaga osobnego `TELEGRAM_WEBHOOK_SECRET`,
- statyczny HTML nie zawiera kluczy ani ofert,
- `noindex,nofollow` ukrywa panel przed indeksowaniem.

Jeśli później chcesz, można dodatkowo dołożyć Cloudflare Access tylko na wybrany hostname/path.

---

# Ocena „okazji”

Bot nie porównuje rolnej do budowlanej bez kontroli.

Kolejność porównań:

1. ten sam typ + podobny metraż + podobny status zabudowy,
2. ten sam typ + podobny metraż,
3. podobny metraż + podobny status zabudowy,
4. podobny metraż,
5. ten sam typ.

Panel pokazuje również liczbę porównań i jakość benchmarku: wysoka / dobra / średnia / orientacyjna / słaba.

---

# Ważne ograniczenia

Portale mogą zmieniać HTML albo blokować ruch z adresów centrów danych GitHuba. Dlatego każdy portal ma diagnostykę i jeden uszkodzony skan nie kasuje starej bazy. Oferta jest wygaszana dopiero po 3 poprawnych skanach danego źródła, w których jej nie było.

Jeżeli konkretnie Otodom/OLX zacznie blokować GitHub Actions, można później dodać hybrydowy fallback z Twojego PC dla tylko tego jednego źródła, zapisujący do tej samej D1.

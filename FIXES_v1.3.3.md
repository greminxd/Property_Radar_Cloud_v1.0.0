# Property Radar Cloud v1.3.3 — OLX API-first

## OLX

- Added a dedicated OLX collector using the public web JSON endpoint `https://www.olx.pl/api/v1/offers/`.
- OLX no longer uses Playwright for the normal collection path.
- API search is performed for every locality in the configured primary + nearby whitelist, then the existing geographic filter remains authoritative.
- Structured OLX fields (description, price, params, location, timestamps and photos) are converted into the normal Property Radar record and passed through the existing parser.
- Plot classification happens before the global `plot` hint, so unrelated OLX results returned by a text query are rejected.
- Ordinary OLX search HTML is only a bounded supplement. If OLX returns 403/challenge to GitHub runners, API results survive and no browser-tab storm occurs.
- Added retries and diagnostics for API HTTP statuses, attempts, pages, raw items, plot items, parse failures and fallback status.
- No CAPTCHA bypass, proxy rotation or anti-bot circumvention is included.

## Parser / alerts

- Existing price-per-square-metre logic remains authoritative for market comparisons.
- Parser version bumped to `1.3.3-olx-public-api-v1` so a parser migration cannot masquerade as a real price change.

## Tests

- Added synthetic OLX API regression covering total price, 3,600 m² area, derived PLN/m², locality, parcel number, image URL and non-plot rejection.

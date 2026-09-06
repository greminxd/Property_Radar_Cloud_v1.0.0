Property Radar Cloud v1.2.3 — Telegram latency / stale menu / D1 visibility fix

- Telegram webhook ACK 200 immediately; processing in ctx.waitUntil -> no multi-minute queue.
- Telegram/GitHub outbound calls have 12s timeout.
- D1 status queries are batched instead of many serial queries.
- /start repairs Telegram bottom menu button to current Worker URL.
- Setup and scanner use stable https://property-radar.hoolz.workers.dev instead of stale trycloudflare PANEL_URL.
- GitHub manual dispatch uses repo branch master by default (was incorrectly hardcoded main).
- Mini App API returns only plots. Legacy houses/garages remain hidden.
- Empty 30-day view now shows D1 counts and a button to show all active plots from the database.
- Database status now distinguishes all rows, active rows, active plots and excluded legacy rows.
- scan diagnostics upload action updated to v6.

After deploy run Setup D1 + Telegram once. This resets the webhook backlog and the persistent Telegram menu button.

from __future__ import annotations

import asyncio
import re
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urldefrag
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .utils import canonical_url, clean_text, phone_candidates
from .parser import parse_detail, refine_from_rendered_text
from .classify import classify_category


_BLOCK_MARKERS = (
    "just a moment", "access denied", "verify you are human", "captcha",
    "robot or human", "are you a robot", "sprawdź, czy jesteś człowiekiem",
)


class Scraper:
    def __init__(self, cfg):
        self.cfg = cfg
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        })

    async def _click_first_text(self, page, texts, timeout=900):
        for txt in texts:
            try:
                loc = page.get_by_text(txt, exact=False).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    await page.wait_for_timeout(200)
                    return True
            except Exception:
                pass
        return False

    async def _goto(self, page, url, scroll=False, reveal_phone=False):
        """Navigate without treating DOMContentLoaded timeout as a hard failure.

        OLX/Otodom can keep background requests open or delay DOMContentLoaded on
        datacenter IPs.  The old code discarded the whole page on TimeoutError,
        even when the useful HTML/links had already arrived.  We wait for the
        response commit first and then best-effort for DOM readiness.
        """
        timeout_ms = int(self.cfg["browser"].get("timeout_ms", 20000))
        nav_timeout = min(timeout_ms, int(self.cfg["browser"].get("navigation_commit_timeout_ms", 12000)))
        timed_out = False
        try:
            await page.goto(url, wait_until="commit", timeout=nav_timeout)
        except PlaywrightTimeoutError:
            timed_out = True
        # If commit happened, DOMContentLoaded is only a best-effort extra.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=4500)
        except Exception:
            pass
        await page.wait_for_timeout(350)
        try:
            html = await page.content()
        except Exception:
            html = ""
        if timed_out and len(html) < 350:
            raise PlaywrightTimeoutError(f"navigation timed out before useful HTML: {url}")

        await self._click_first_text(
            page,
            ["Akceptuję wszystkie", "Akceptuj wszystkie", "Zaakceptuj wszystkie", "Zgadzam się", "Akceptuję", "Rozumiem", "Accept all"],
            550,
        )
        try:
            if scroll:
                for _ in range(self.cfg["browser"].get("scroll_rounds", 2)):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(280)
                await page.evaluate("window.scrollTo(0, 0)")
            if reveal_phone:
                clicked = await self._click_first_text(
                    page,
                    ["Pokaż numer telefonu", "Pokaż numer", "Pokaż telefon", "Pokaż nr", "Wyświetl numer", "Zadzwoń"],
                    950,
                )
                if not clicked:
                    for sel in [
                        'button:has-text("telefon")', 'button:has-text("numer")', 'a:has-text("telefon")',
                        '[data-testid*="phone"]', '[data-cy*="phone"]', '[class*="phone"] button', '[class*="Phone"] button',
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() and await loc.is_visible():
                                await loc.scroll_into_view_if_needed()
                                await loc.click(timeout=850)
                                clicked = True
                                break
                        except Exception:
                            pass
                if clicked:
                    await page.wait_for_timeout(600)
        except Exception:
            pass
        try:
            return await page.content()
        except Exception:
            return html

    @staticmethod
    def _hint_from_search_url(url: str) -> str:
        return "plot"

    def _http_get_sync(self, url: str):
        timeout_s = float(self.cfg["browser"].get("http_timeout_s", 12))
        try:
            r = self._http.get(url, timeout=(5, timeout_s), allow_redirects=True)
            return r.status_code, r.url, r.text or ""
        except Exception as e:
            return 0, url, f"__HTTP_ERROR__ {type(e).__name__}: {e}"

    async def _http_get(self, url: str):
        return await asyncio.to_thread(self._http_get_sync, url)

    @staticmethod
    def _navigation_url(base_url: str, href: str) -> str:
        """URL for pagination/navigation: preserve query string, drop only fragment."""
        u = urljoin(base_url, href or "")
        u, _ = urldefrag(u)
        return u

    @staticmethod
    def _extract_links_from_html(base_url: str, html: str, pattern: re.Pattern) -> tuple[list[str], str | None]:
        if not html or html.startswith("__HTTP_ERROR__"):
            return [], None
        soup = BeautifulSoup(html, "lxml")
        out = []
        for a in soup.select("a[href]"):
            href = canonical_url(base_url, a.get("href") or "")
            if href and pattern.match(href):
                out.append(href)
        # de-dupe, keep DOM order
        out = list(dict.fromkeys(out))
        nxt = None
        for a in soup.select('a[rel="next"], a[href]'):
            txt = clean_text(a.get_text(" ", strip=True)).lower()
            rel = " ".join(a.get("rel") or []).lower()
            if "next" in rel or txt in {"następna", "dalej", "next", ">"}:
                href = Scraper._navigation_url(base_url, a.get("href") or "")
                if href:
                    nxt = href
                    break
        return out, nxt

    async def _discover_http(self, start_url: str, pattern: re.Pattern, max_pages: int):
        current = start_url
        seen = set()
        found = []
        statuses = []
        for _ in range(max_pages):
            if not current or current in seen:
                break
            seen.add(current)
            status, final_url, html = await self._http_get(current)
            statuses.append(status)
            if status != 200:
                break
            links, nxt = self._extract_links_from_html(final_url, html, pattern)
            found.extend(links)
            if not nxt:
                break
            current = nxt
        return list(dict.fromkeys(found)), statuses

    async def _browser_hrefs(self, page) -> list[str]:
        try:
            return await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
        except Exception:
            return []

    @staticmethod
    def _listing_like(rec: dict, visible: str = "") -> tuple[bool, bool]:
        block_text = ((rec.get("title") or "") + " " + (visible or rec.get("_body") or "")[:2500]).lower()
        blocked = any(x in block_text for x in _BLOCK_MARKERS)
        ok = bool(rec.get("title")) and (rec.get("price") is not None or rec.get("area_m2") is not None) and not blocked
        return ok, blocked

    async def _parse_http_detail(self, u: str, source_name: str, hint: str):
        status, final_url, html = await self._http_get(u)
        if status != 200 or len(html) < 500:
            return None, "", status
        rec = parse_detail(html, final_url or u, source_name, category_hint=hint)
        body = rec.get("_body") or ""
        rec["category"] = classify_category(rec.get("title") or "", u, body, category_hint=hint)
        if body:
            rec = refine_from_rendered_text(rec, body)
        ok, blocked = self._listing_like(rec, body)
        if ok and not blocked:
            return rec, body, status
        return None, body, status

    async def collect_source(self, browser, source):
        """Return (records, errors, diagnostics) with HTTP-first discovery + browser fallback."""
        context = await browser.new_context(
            locale="pl-PL",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"},
        )

        # Heavy assets are irrelevant for scraping and are a major source of navigation
        # stalls on GitHub-hosted runners. Scripts/XHR remain enabled.
        async def _route(route):
            try:
                if route.request.resource_type in {"image", "media", "font"}:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass
        try:
            await context.route("**/*", _route)
        except Exception:
            pass

        page = await context.new_page()
        pattern = re.compile(source["detail_regex"], re.I)
        link_hints = {}
        errors = []
        search_pages_ok = 0
        discovery_methods = []
        http_statuses = []

        try:
            max_search_pages = int(self.cfg["browser"].get("max_search_pages_per_url", 3))
            prefer_http = source.get("prefer_http_discovery", source["name"] in {"OLX", "Otodom", "Gratka", "Tabelaofert"})

            for search_url in source["search_urls"]:
                hint = self._hint_from_search_url(search_url)
                got_for_url = 0

                if prefer_http:
                    links_http, statuses = await self._discover_http(search_url, pattern, max_search_pages)
                    http_statuses.extend(statuses)
                    if links_http:
                        for h in links_http:
                            link_hints.setdefault(h, hint)
                        got_for_url += len(links_http)
                        search_pages_ok += 1
                        discovery_methods.append("http")

                # Browser is a fallback, not the only discovery path.
                if got_for_url == 0:
                    current = search_url
                    seen_pages = set()
                    for _ in range(max_search_pages):
                        if not current or current in seen_pages:
                            break
                        seen_pages.add(current)
                        try:
                            await self._goto(page, current, scroll=True)
                            # Do not wait for the whole app. Wait only for links to exist.
                            try:
                                await page.locator("a[href]").first.wait_for(state="attached", timeout=2500)
                            except Exception:
                                pass
                            hrefs = await self._browser_hrefs(page)
                            added = 0
                            for h in hrefs:
                                h = canonical_url(current, h)
                                if pattern.match(h):
                                    if h not in link_hints:
                                        added += 1
                                    link_hints.setdefault(h, hint)
                            if hrefs:
                                search_pages_ok += 1
                            if added:
                                discovery_methods.append("browser")
                            nxt = None
                            for sel in ['a[rel="next"]', 'a:has-text("Następna")', 'a:has-text("Dalej")']:
                                try:
                                    loc = page.locator(sel).first
                                    if await loc.count():
                                        href = await loc.get_attribute("href")
                                        if href:
                                            nxt = self._navigation_url(current, href)
                                            break
                                except Exception:
                                    pass
                            current = nxt
                        except Exception as e:
                            errors.append(f"{current}: {type(e).__name__}: {e}")
                            break

            # Otodom often blocks datacenter search pages. OLX currently exposes many
            # Otodom offers in its results, so use those links only as a *discovery fallback*.
            # Details are still fetched from Otodom itself and normal locality filters apply.
            if not link_hints and source.get("fallback_discovery_urls"):
                for fallback_url in source["fallback_discovery_urls"]:
                    links_http, statuses = await self._discover_http(fallback_url, pattern, 1)
                    http_statuses.extend(statuses)
                    for h in links_http:
                        link_hints.setdefault(h, "plot")
                    if links_http:
                        discovery_methods.append("fallback-http")
                        search_pages_ok += 1

            links = list(link_hints.keys())[: self.cfg["browser"].get("max_detail_pages_per_source", 80)]
            results = []
            detail_ok = 0
            listing_like_count = 0
            blocked_count = 0
            failed_samples = []
            detail_parallel = max(1, int(self.cfg["browser"].get("parallel_details_per_source", 4)))
            sem = asyncio.Semaphore(detail_parallel)
            counter = 0
            lock = asyncio.Lock()
            prefer_http_details = source.get("prefer_http_details", source["name"] in {"OLX", "Otodom", "Gratka", "Tabelaofert"})
            detail_methods = {"http": 0, "browser": 0}

            async def one_detail(u):
                nonlocal detail_ok, listing_like_count, counter, blocked_count
                async with sem:
                    rec = None
                    visible = ""
                    try:
                        if prefer_http_details:
                            try:
                                rec, visible, status = await self._parse_http_detail(u, source["name"], link_hints.get(u))
                                if rec is not None:
                                    detail_methods["http"] += 1
                            except Exception as e:
                                async with lock:
                                    errors.append(f"{u}: HTTP detail {type(e).__name__}: {e}")

                        if rec is None:
                            p = await context.new_page()
                            try:
                                await self._goto(p, u, reveal_phone=True)
                                try:
                                    await p.locator("h1").first.wait_for(state="visible", timeout=2500)
                                except Exception:
                                    pass
                                try:
                                    html = await p.content()
                                except Exception:
                                    html = ""
                                actual_url=p.url or u
                                rec = parse_detail(html, actual_url, source["name"], category_hint=link_hints.get(u))
                                try:
                                    visible = await p.locator("body").inner_text(timeout=1800)
                                except Exception:
                                    visible = ""
                                try:
                                    h1 = clean_text(await p.locator("h1").first.inner_text(timeout=900))
                                except Exception:
                                    h1 = ""
                                if h1 and (
                                    not rec.get("title")
                                    or len(rec.get("title") or "") < 8
                                    or rec.get("title", " ").lower().startswith(("otodom", "olx", "gratka", "tabelaofert"))
                                ):
                                    rec["title"] = h1[:500]
                                rec["category"] = classify_category(rec.get("title") or "", actual_url, visible, category_hint=link_hints.get(u))
                                try:
                                    tel_hrefs = await p.locator('a[href^="tel:"]').evaluate_all("els => els.map(e => e.href)")
                                except Exception:
                                    tel_hrefs = []
                                if not rec.get("phone"):
                                    direct = []
                                    for href in tel_hrefs:
                                        d = re.sub(r"\D", "", str(href))
                                        if len(d) == 11 and d.startswith("48"):
                                            d = d[2:]
                                        if len(d) == 9:
                                            direct.append(f"{d[:3]} {d[3:6]} {d[6:]}")
                                    if direct:
                                        rec["phone"] = direct[0]
                                    else:
                                        cands = phone_candidates(visible)
                                        if cands and cands[0][0] >= 2:
                                            d = cands[0][1]
                                            rec["phone"] = f"{d[:3]} {d[3:6]} {d[6:]}"
                                if visible:
                                    rec = refine_from_rendered_text(rec, visible)
                                    rec["_body"] = (rec.get("_body") or "") + " " + visible[:12000]
                                detail_methods["browser"] += 1
                            finally:
                                try:
                                    if not p.is_closed():
                                        await p.close()
                                except Exception:
                                    pass

                        listing_like, blocked = self._listing_like(rec or {}, visible)
                        if rec and not pattern.match(rec.get("canonical_url") or ""):
                            listing_like=False
                            if len(failed_samples)<3:
                                failed_samples.append({"url":u,"title":(rec.get("title") or "")[:160],"blocked":False,"text":"redirect/non-detail URL: "+str(rec.get("canonical_url") or "")[:180]})
                        async with lock:
                            detail_ok += 1
                            if blocked:
                                blocked_count += 1
                            if listing_like:
                                listing_like_count += 1
                            elif len(failed_samples) < 3:
                                failed_samples.append({
                                    "url": u,
                                    "title": ((rec or {}).get("title") or "")[:160],
                                    "blocked": blocked,
                                    "text": (visible or ((rec or {}).get("_body") or ""))[:220].replace("\n", " "),
                                })
                            if listing_like and rec and rec["category"] in self.cfg["filters"]["categories"]:
                                results.append(rec)
                    except Exception as e:
                        async with lock:
                            errors.append(f"{u}: {type(e).__name__}: {e}")
                    finally:
                        async with lock:
                            counter += 1
                            if counter % 10 == 0 or counter == len(links):
                                print(f"       {source['name']}: szczegóły {counter}/{len(links)}", flush=True)
                    await asyncio.sleep(self.cfg["browser"].get("delay_between_pages_s", 0.15))

            tasks = [asyncio.create_task(one_detail(u)) for u in links]
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

            diagnostics = {
                "source": source["name"],
                "search_pages_ok": search_pages_ok,
                "discovered_links": len(links),
                "detail_pages_ok": detail_ok,
                "listing_like": listing_like_count,
                "records": len(results),
                "errors": len(errors),
                "blocked": blocked_count,
                "failed_samples": failed_samples,
                "healthy": bool(search_pages_ok > 0 and links and detail_ok > 0 and listing_like_count > 0),
                "discovery_methods": sorted(set(discovery_methods)),
                "detail_methods": detail_methods,
                "http_statuses": http_statuses[-12:],
            }
            return results, errors, diagnostics
        finally:
            try:
                await context.close()
            except Exception:
                pass

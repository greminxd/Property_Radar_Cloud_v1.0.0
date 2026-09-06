from __future__ import annotations

import asyncio
import json
import re
import time
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

    async def _http_get_retry(self, url: str, attempts: int = 3, delays=(0.0, 1.5, 4.0)):
        """HTTP GET with a small bounded retry policy for transient portal errors."""
        history=[]
        last=(0,url,"")
        attempts=max(1,int(attempts))
        for i in range(attempts):
            if i:
                delay=delays[min(i,len(delays)-1)] if delays else 0
                if delay:
                    await asyncio.sleep(float(delay))
            last=await self._http_get(url)
            status,final_url,html=last
            history.append(status)
            # Retry only transport errors, throttling and server-side failures.
            if status==200:
                break
            if status not in {0,403,408,425,429,500,502,503,504}:
                break
        return (*last,history)

    @staticmethod
    def _next_data(html: str):
        if not html or html.startswith("__HTTP_ERROR__"):
            return None
        try:
            soup=BeautifulSoup(html,"lxml")
            tag=soup.find("script",id="__NEXT_DATA__")
            raw=(tag.string or tag.get_text() or "").strip() if tag else ""
            return json.loads(raw) if raw else None
        except Exception:
            return None

    @staticmethod
    def _walk_json(obj):
        stack=[obj]
        while stack:
            x=stack.pop()
            if isinstance(x,dict):
                yield x
                stack.extend(x.values())
            elif isinstance(x,list):
                stack.extend(x)

    @classmethod
    def _otodom_search_items(cls, payload) -> list[dict]:
        """Return Otodom searchAds.items from Next.js payload without hardcoding pageProps depth."""
        if not payload:
            return []
        for d in cls._walk_json(payload):
            search_ads=d.get("searchAds")
            if isinstance(search_ads,dict) and isinstance(search_ads.get("items"),list):
                return [x for x in search_ads["items"] if isinstance(x,dict)]
        return []

    @staticmethod
    def _otodom_item_url(item: dict) -> str | None:
        # Field names changed a few times; accept only canonical Otodom detail URLs.
        for key in ("detailUrl","url","href","canonicalUrl"):
            v=item.get(key)
            if isinstance(v,str) and v:
                if v.startswith("/"):
                    v=urljoin("https://www.otodom.pl",v)
                if re.match(r"https?://(?:www\.)?otodom\.pl/pl/oferta/[^?#]+",v,re.I):
                    return canonical_url(v,v)
        slug=item.get("slug")
        if isinstance(slug,str) and slug.strip():
            slug=slug.strip().strip("/")
            if slug.startswith("pl/oferta/"):
                return canonical_url("https://www.otodom.pl/"+slug,"https://www.otodom.pl/"+slug)
            if "/pl/oferta/" in slug and slug.startswith("http"):
                return canonical_url(slug,slug)
            return canonical_url("https://www.otodom.pl/pl/oferta/"+slug,"https://www.otodom.pl/pl/oferta/"+slug)
        return None

    @staticmethod
    def _page_url(url: str, page_no: int) -> str:
        from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
        if page_no <= 1:
            return url
        parts=urlsplit(url)
        q=dict(parse_qsl(parts.query,keep_blank_values=True))
        q["page"]=str(page_no)
        return urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(q,doseq=True),parts.fragment))

    async def _collect_otodom(self, browser, source):
        """Dedicated Otodom collector based on Next.js __NEXT_DATA__.

        Search discovery never depends on CSS classes. Detail pages stay HTTP-first and
        reuse the mature generic detail parser, which already understands embedded JSON.
        A single browser search fallback is retained for datacenter responses where the
        server HTML is incomplete, but detail scraping does not fan out Playwright tabs.
        """
        pattern=re.compile(source["detail_regex"],re.I)
        errors=[]
        links=[]
        statuses=[]
        retry_statuses=[]
        next_data_pages=0
        next_data_items=0
        browser_search_fallbacks=0
        max_pages=max(1,int(source.get("max_search_pages",1)))
        attempts=max(1,int(source.get("http_retries",3)))

        for base in source.get("search_urls",[]):
            for page_no in range(1,max_pages+1):
                u=self._page_url(base,page_no)
                status,final_url,html,hist=await self._http_get_retry(u,attempts=attempts)
                statuses.append(status);retry_statuses.extend(hist)
                payload=self._next_data(html) if status==200 else None
                items=self._otodom_search_items(payload)
                if payload is not None:
                    next_data_pages += 1
                if items:
                    next_data_items += len(items)
                    for item in items:
                        v=self._otodom_item_url(item)
                        if v and pattern.match(v): links.append(v)
                    # Empty next page means pagination is finished.
                    continue
                # The new collector deliberately does not scrape card CSS. One lightweight
                # browser attempt may still expose __NEXT_DATA__ if raw HTTP was challenged.
                if page_no==1 and source.get("browser_search_fallback",True):
                    context=None
                    try:
                        context=await browser.new_context(locale="pl-PL",user_agent=self._http.headers.get("User-Agent"))
                        page=await context.new_page()
                        rendered=await self._goto(page,u,scroll=False)
                        payload=self._next_data(rendered)
                        bitems=self._otodom_search_items(payload)
                        browser_search_fallbacks += 1
                        if payload is not None: next_data_pages += 1
                        if bitems:
                            next_data_items += len(bitems)
                            for item in bitems:
                                v=self._otodom_item_url(item)
                                if v and pattern.match(v): links.append(v)
                    except Exception as e:
                        errors.append(f"{u}: browser search fallback {type(e).__name__}: {e}")
                    finally:
                        if context is not None:
                            try: await context.close()
                            except Exception: pass
                # If first page has no searchAds, more pages will not help.
                if page_no==1 and not links:
                    break

        links=list(dict.fromkeys(links))[:int(source.get("max_detail_pages",80))]
        detail_parallel=max(1,int(source.get("parallel_details",5)))
        detail_timeout=float(source.get("detail_timeout_s",18))
        detail_budget=float(source.get("detail_budget_s",90))
        sem=asyncio.Semaphore(detail_parallel)
        results=[]
        detail_http_statuses=[]
        detail_attempt_statuses=[]
        detail_ok=0
        blocked=0
        failed=[]
        started=asyncio.get_running_loop().time()

        async def one(u):
            nonlocal detail_ok,blocked
            async with sem:
                status,final_url,html,hist=await self._http_get_retry(u,attempts=attempts)
                detail_http_statuses.append(status);detail_attempt_statuses.extend(hist)
                if status!=200 or len(html)<500:
                    if len(failed)<4: failed.append({"url":u,"status":status,"reason":"http detail failed"})
                    return
                rec=parse_detail(html,final_url or u,"Otodom",category_hint="plot")
                body=rec.get("_body") or ""
                rec["category"]=classify_category(rec.get("title") or "",final_url or u,body,category_hint="plot")
                if body: rec=refine_from_rendered_text(rec,body)
                ok,is_blocked=self._listing_like(rec,body)
                detail_ok += 1
                if is_blocked: blocked += 1
                if ok and rec.get("category") in self.cfg["filters"]["categories"]:
                    results.append(rec)
                elif len(failed)<4:
                    failed.append({"url":u,"status":status,"reason":"not listing-like","title":(rec.get("title") or "")[:140],"text":body[:180]})

        timeout_count=0
        for i in range(0,len(links),detail_parallel):
            elapsed=asyncio.get_running_loop().time()-started
            if elapsed>=detail_budget:
                errors.append(f"Otodom: detail budget {detail_budget:.0f}s exhausted; skipped {len(links)-i}")
                timeout_count += len(links)-i
                break
            batch=[]
            for u in links[i:i+detail_parallel]:
                async def bounded(x=u):
                    nonlocal timeout_count
                    try: await asyncio.wait_for(one(x),timeout=detail_timeout)
                    except asyncio.TimeoutError:
                        timeout_count += 1; errors.append(f"{x}: detail timeout after {detail_timeout:.0f}s")
                    except Exception as e:
                        errors.append(f"{x}: Otodom detail {type(e).__name__}: {e}")
                batch.append(asyncio.create_task(bounded()))
            remaining=max(1.0,detail_budget-(asyncio.get_running_loop().time()-started))
            done,pending=await asyncio.wait(batch,timeout=remaining)
            if pending:
                timeout_count += len(pending)
                for t in pending:t.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
                errors.append(f"Otodom: detail budget exhausted inside batch; cancelled {len(pending)}")
                break
            print(f"       Otodom: szczegóły {min(i+detail_parallel,len(links))}/{len(links)}",flush=True)

        healthy=bool(next_data_pages and links and detail_ok and results)
        diag={
            "source":"Otodom",
            "search_pages_ok":next_data_pages,
            "discovered_links":len(links),
            "detail_pages_ok":len(results),
            "detail_pages_fetched":detail_ok,
            "listing_like":len(results),
            "records":len(results),
            "errors":len(errors),
            "blocked":blocked,
            "failed_samples":failed,
            "healthy":healthy,
            "discovery_methods":["next-data"] + (["browser-next-data"] if browser_search_fallbacks else []),
            "detail_methods":{"http-next-data":detail_ok,"browser":0},
            "http_statuses":statuses[-12:],
            "http_attempt_statuses":retry_statuses[-24:],
            "detail_http_statuses":detail_http_statuses[-12:],
            "detail_attempt_statuses":detail_attempt_statuses[-24:],
            "next_data_pages":next_data_pages,
            "next_data_items":next_data_items,
            "browser_search_fallbacks":browser_search_fallbacks,
            "detail_timeouts_or_cancelled":timeout_count,
            "collector":"otodom-next-data-v1",
        }
        if not links:
            errors.append("Otodom: __NEXT_DATA__ nie zwrócił linków searchAds.items")
            diag["errors"]=len(errors)
        return results,errors,diag

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

    @staticmethod
    def _source_filter_discovery(source_name: str, html: str, links: list[str]) -> list[str]:
        """Portal-specific cleanup of search-page links.

        Tabelaofert renders extra/recommended ``/oferta/...`` links on the same page.
        Its heading exposes the real result count (e.g. ``Znaleziono 3 oferty``), so
        only the first N canonical detail links belong to the actual result list.
        """
        if source_name == "Tabelaofert" and html:
            try:
                text=clean_text(BeautifulSoup(html,"lxml").get_text(" ",strip=True))
                m=re.search(r"Znaleziono\s+(\d+)\s+ofert",text,re.I)
                if m:
                    n=max(0,int(m.group(1)))
                    if n:
                        return links[:n]
            except Exception:
                pass
        return links

    async def _discover_http(self, start_url: str, pattern: re.Pattern, max_pages: int, source_name: str = ""):
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
            links = self._source_filter_discovery(source_name, html, links)
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
        """Return (records, errors, diagnostics) with portal-specific collectors where needed."""
        if source.get("name")=="Otodom":
            return await self._collect_otodom(browser,source)

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
            max_search_pages = int(source.get("max_search_pages", self.cfg["browser"].get("max_search_pages_per_url", 3)))
            prefer_http = source.get("prefer_http_discovery", source["name"] in {"OLX", "Otodom", "Gratka", "Tabelaofert"})

            for search_url in source["search_urls"]:
                hint = self._hint_from_search_url(search_url)
                got_for_url = 0

                if prefer_http:
                    links_http, statuses = await self._discover_http(search_url, pattern, max_search_pages, source["name"])
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
                    links_http, statuses = await self._discover_http(fallback_url, pattern, 1, source["name"])
                    http_statuses.extend(statuses)
                    for h in links_http:
                        link_hints.setdefault(h, "plot")
                    if links_http:
                        discovery_methods.append("fallback-http")
                        search_pages_ok += 1

            max_details=int(source.get("max_detail_pages", self.cfg["browser"].get("max_detail_pages_per_source", 80)))
            links = list(link_hints.keys())[:max_details]
            results = []
            detail_ok = 0
            listing_like_count = 0
            blocked_count = 0
            failed_samples = []
            detail_parallel = max(1, int(source.get("parallel_details", self.cfg["browser"].get("parallel_details_per_source", 4))))
            detail_timeout_s=float(source.get("detail_timeout_s", self.cfg["browser"].get("detail_timeout_s", 24)))
            detail_budget_s=float(source.get("detail_budget_s", self.cfg["browser"].get("detail_budget_s", 150)))
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

            async def bounded_detail(u):
                try:
                    # Batches are at most ``detail_parallel`` wide, so this timeout
                    # measures actual work, not time spent waiting behind 30 other tasks.
                    await asyncio.wait_for(one_detail(u), timeout=detail_timeout_s)
                except asyncio.TimeoutError:
                    async with lock:
                        errors.append(f"{u}: detail timeout after {detail_timeout_s:.0f}s")
                except asyncio.CancelledError:
                    raise

            timed_out_details=0
            started_details=asyncio.get_running_loop().time()
            for i in range(0,len(links),detail_parallel):
                elapsed=asyncio.get_running_loop().time()-started_details
                if elapsed >= detail_budget_s:
                    skipped=len(links)-i
                    timed_out_details += skipped
                    errors.append(f"{source['name']}: detail budget {detail_budget_s:.0f}s exhausted; skipped {skipped} remaining")
                    break
                batch_links=links[i:i+detail_parallel]
                batch=[asyncio.create_task(bounded_detail(u)) for u in batch_links]
                remaining=max(1.0,detail_budget_s-elapsed)
                done,pending=await asyncio.wait(batch,timeout=remaining)
                if pending:
                    timed_out_details += len(pending)
                    for t in pending: t.cancel()
                    await asyncio.gather(*pending,return_exceptions=True)
                    errors.append(f"{source['name']}: detail budget exhausted inside batch; cancelled {len(pending)}")
                    break

            diagnostics = {
                "source": source["name"],
                "search_pages_ok": search_pages_ok,
                "discovered_links": len(links),
                "detail_pages_ok": listing_like_count,
                "detail_pages_fetched": detail_ok,
                "listing_like": listing_like_count,
                "records": len(results),
                "errors": len(errors),
                "blocked": blocked_count,
                "failed_samples": failed_samples,
                "healthy": bool(search_pages_ok > 0 and links and detail_ok > 0 and listing_like_count > 0),
                "discovery_methods": sorted(set(discovery_methods)),
                "detail_methods": detail_methods,
                "http_statuses": http_statuses[-12:],
                "detail_timeouts_or_cancelled": timed_out_details + sum(1 for e in errors if "detail timeout after" in e),
            }
            return results, errors, diagnostics
        finally:
            try:
                await context.close()
            except Exception:
                pass

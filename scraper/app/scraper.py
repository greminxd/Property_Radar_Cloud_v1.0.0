from __future__ import annotations
import asyncio, re
from .utils import canonical_url
from .parser import parse_detail, refine_from_rendered_text
from .utils import phone_candidates


class Scraper:
    def __init__(self, cfg): self.cfg=cfg

    async def _click_first_text(self,page,texts,timeout=900):
        for txt in texts:
            try:
                loc=page.get_by_text(txt,exact=False).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                pass
        return False

    async def _goto(self,page,url,scroll=False,reveal_phone=False):
        await page.goto(url,wait_until="domcontentloaded",timeout=self.cfg["browser"]["timeout_ms"])
        await page.wait_for_timeout(700)
        await self._click_first_text(page,["Akceptuję wszystkie","Akceptuj wszystkie","Zaakceptuj wszystkie","Zgadzam się","Akceptuję","Rozumiem","Accept all"],700)
        try:
            if scroll:
                for _ in range(self.cfg["browser"].get("scroll_rounds",3)):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(420)
                await page.evaluate("window.scrollTo(0, 0)")
            if reveal_phone:
                clicked=await self._click_first_text(page,["Pokaż numer telefonu","Pokaż numer","Pokaż telefon","Pokaż nr","Wyświetl numer","Zadzwoń"],1300)
                if not clicked:
                    sels=[
                        'button:has-text("telefon")','button:has-text("numer")','a:has-text("telefon")',
                        '[data-testid*="phone"]','[data-cy*="phone"]','[class*="phone"] button','[class*="Phone"] button'
                    ]
                    for sel in sels:
                        try:
                            loc=page.locator(sel).first
                            if await loc.count() and await loc.is_visible():
                                await loc.scroll_into_view_if_needed()
                                await loc.click(timeout=1100)
                                clicked=True; break
                        except Exception:
                            pass
                if clicked:
                    await page.wait_for_timeout(850)
        except Exception:
            pass
        return await page.content()

    @staticmethod
    def _hint_from_search_url(url: str) -> str:
        u=(url or '').lower()
        return 'garage' if any(k in u for k in ['garaz','garaż','parking']) else 'plot'

    async def collect_source(self,browser,source):
        """Return (records, errors, diagnostics) with bounded, parallel detail scraping."""
        context=await browser.new_context(
            locale="pl-PL",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
            viewport={"width":1440,"height":1000},
        )
        page=await context.new_page(); pattern=re.compile(source["detail_regex"],re.I)
        link_hints={}; errors=[]; search_pages_ok=0
        try:
            for search_url in source["search_urls"]:
                hint=self._hint_from_search_url(search_url)
                current=search_url; seen_pages=set()
                for _ in range(self.cfg["browser"].get("max_search_pages_per_url",3)):
                    if not current or current in seen_pages: break
                    seen_pages.add(current)
                    try:
                        await self._goto(page,current,scroll=True)
                        search_pages_ok += 1
                        hrefs=await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
                        for h in hrefs:
                            h=canonical_url(current,h)
                            if pattern.match(h): link_hints.setdefault(h,hint)
                        nxt=None
                        for sel in ['a[rel="next"]','a:has-text("Następna")','a:has-text("Dalej")','button:has-text("Następna")']:
                            try:
                                loc=page.locator(sel).first
                                if await loc.count():
                                    href=await loc.get_attribute("href")
                                    if href: nxt=canonical_url(current,href); break
                            except Exception: pass
                        current=nxt
                    except Exception as e:
                        errors.append(f"{current}: {type(e).__name__}: {e}"); break

            links=list(link_hints.keys())[:self.cfg["browser"].get("max_detail_pages_per_source",80)]
            results=[]; detail_ok=0; listing_like_count=0
            detail_parallel=max(1,int(self.cfg["browser"].get("parallel_details_per_source",4)))
            sem=asyncio.Semaphore(detail_parallel)
            counter=0
            lock=asyncio.Lock()

            async def one_detail(u):
                nonlocal detail_ok, listing_like_count, counter
                async with sem:
                    p=await context.new_page()
                    try:
                        html=await self._goto(p,u,reveal_phone=True)
                        rec=parse_detail(html,u,source["name"],category_hint=link_hints.get(u))
                        try: visible=await p.locator("body").inner_text(timeout=1800)
                        except Exception: visible=""
                        try: tel_hrefs=await p.locator('a[href^="tel:"]').evaluate_all("els => els.map(e => e.href)")
                        except Exception: tel_hrefs=[]
                        if not rec.get("phone"):
                            direct=[]
                            for href in tel_hrefs:
                                d=re.sub(r"\D","",str(href))
                                if len(d)==11 and d.startswith("48"): d=d[2:]
                                if len(d)==9: direct.append(f"{d[:3]} {d[3:6]} {d[6:]}")
                            if direct: rec["phone"]=direct[0]
                            else:
                                cands=phone_candidates(visible)
                                if cands and cands[0][0] >= 2:
                                    d=cands[0][1]; rec["phone"]=f"{d[:3]} {d[3:6]} {d[6:]}"
                        if visible:
                            rec=refine_from_rendered_text(rec,visible)
                            rec["_body"]=(rec.get("_body") or "")+" "+visible[:12000]
                        listing_like=bool(rec.get("title")) and (rec.get("price") is not None or rec.get("area_m2") is not None)
                        async with lock:
                            detail_ok += 1
                            if listing_like: listing_like_count += 1
                            if listing_like and rec["category"] in self.cfg["filters"]["categories"]: results.append(rec)
                    except Exception as e:
                        async with lock: errors.append(f"{u}: {type(e).__name__}: {e}")
                    finally:
                        await p.close()
                        async with lock:
                            counter += 1
                            if counter % 10 == 0 or counter == len(links):
                                print(f"       {source['name']}: szczegóły {counter}/{len(links)}", flush=True)
                    await asyncio.sleep(self.cfg["browser"].get("delay_between_pages_s",0.15))

            if links:
                await asyncio.gather(*(one_detail(u) for u in links))

            diagnostics={
                "source":source["name"],"search_pages_ok":search_pages_ok,"discovered_links":len(links),
                "detail_pages_ok":detail_ok,"listing_like":listing_like_count,"records":len(results),
                "errors":len(errors),"healthy":bool(search_pages_ok>0 and links and detail_ok>0 and listing_like_count>0),
            }
            return results,errors,diagnostics
        finally:
            await context.close()


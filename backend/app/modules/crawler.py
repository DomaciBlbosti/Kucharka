"""Automatický crawler – sám plní databázi receptů.

Pro každý seed dotaz: SearXNG → kandidátní URL → dedup proti DB → async paralelní
fetch HTML → sync extract + ingest (přes asyncio.to_thread). Per-doménový throttle
přes `asyncio.Semaphore`. Žádný LLM v hot pathu — to dělá enrichment worker.

Veřejné API zůstává synchronní:
- `crawl()` se volá z APSchedulera, `crawl_async()` z REST endpointu.
- Uvnitř obě volají `asyncio.run(_crawl_impl(...))`.
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import random
import re
import threading
import time
from collections import defaultdict

import httpx
from sqlalchemy import func, select

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient, Recipe
from . import discovery, scraper
from .ingest import _persist  # používáme přímo, abychom obešli synchronní fetch v ingest_url

log = logging.getLogger("kucharka.crawler")

# Výchozí weby pro procházení sitemap (když není RECIPE_DOMAINS). České přes
# wild_mode (JSON-LD).
DEFAULT_SITES = [
    "recepty.cz", "toprecepty.cz", "apetitonline.cz", "vareni.cz",
    "ireceptar.cz", "klasicke-recepty.cz", "bestrecepty.cz", "kucharky.cz",
]

# URL vzory: co je recept vs. co přeskočit
_RECIPE_HINT = re.compile(r"/(recept|recipe|recepty|recipes)/", re.I)
_EXCLUDE = re.compile(
    r"/(clanky|clanek|magazin|magazine|tag|tags|stitek|kategorie|category|"
    r"categories|menu|user|users|author|autor|blog|temata|tema|hledat|search)/",
    re.I,
)

# Výchozí seed dotazy – běžná česká jídla. Lze přepsat přes CRAWLER_SEEDS.
DEFAULT_SEEDS = [
    "svíčková na smetaně", "guláš", "smažený řízek", "bramboračka",
    "kuřecí kari", "rajská omáčka", "koprová omáčka", "čočka na kyselo",
    "španělský ptáček", "vepřo knedlo zelo", "segedínský guláš",
    "kuřecí prsa na másle", "boloňské špagety", "lasagne", "rizoto",
    "pizza domácí", "palačinky", "lívance", "bramborový salát",
    "kuřecí polévka", "dýňová polévka", "česnečka", "gulášová polévka",
    "bábovka", "jablečný koláč", "tvarohový koláč", "buchty",
    "perník", "cuketa recept", "květákový mozeček", "rizoto s houbami",
    "hovězí pečeně", "kuřecí stehna pečená", "losos pečený",
    "těstovinový salát", "ovocné knedlíky", "bramborové knedlíky",
]

_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "found": 0,
    "added": 0,
    "skipped": 0,
    "errors": 0,
    "current_query": None,
    "recent": [],
}
_lock = threading.Lock()


def status() -> dict:
    with _lock:
        s = dict(_state)
        s["recent"] = list(_state["recent"])[-15:]
    db = SessionLocal()
    try:
        s["recipes_total"] = db.scalar(select(func.count(Recipe.id))) or 0
        s["ingredients_total"] = db.scalar(select(func.count(Ingredient.id))) or 0
    finally:
        db.close()
    s["searxng_enabled"] = settings.searxng_enabled
    return s


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _existing_urls() -> set[str]:
    db = SessionLocal()
    try:
        return set(db.scalars(select(Recipe.source_url)).all())
    finally:
        db.close()


def crawl(
    queries: list[str] | None = None,
    max_recipes: int = 30,
    per_query: int = 8,
) -> dict:
    """Synchronní entry point. Uvnitř async paralelní fetch + sync ingest."""
    if not settings.searxng_enabled:
        log.warning("SearXNG není nastavený – crawler nemá kde hledat.")
        return status()

    queries = queries or settings.crawler_seeds or DEFAULT_SEEDS
    _set(
        running=True, started_at=time.time(), finished_at=None,
        found=0, added=0, skipped=0, errors=0, current_query=None, recent=[],
    )

    try:
        # 1. Posbírej kandidáty SearXNG (sekvenčně — discovery.search je rychlé,
        #    1 req per seed, není to bottleneck).
        seen = _existing_urls()
        candidates: list[str] = []
        for q in queries:
            _set(current_query=q)
            for cand in discovery.search(q, limit=per_query):
                url = cand["url"]
                if url in seen:
                    with _lock:
                        _state["skipped"] += 1
                    continue
                seen.add(url)
                candidates.append(url)
                if len(candidates) >= max_recipes * 3:
                    # buffer pro selhání — víc kandidátů než cíl
                    break
            if len(candidates) >= max_recipes * 3:
                break

        log.info("Crawler: %s kandidátů z %s dotazů", len(candidates), len(queries))

        # 2. Async paralelní fetch + ingest
        if candidates:
            asyncio.run(_crawl_parallel(candidates, max_recipes))
    finally:
        _set(running=False, finished_at=time.time(), current_query=None)

    return status()


# ─── Async paralelní jádro ──────────────────────────────────────────────────

# Konkurence: celkem max 16 souběžných HTTP requestů, max 2 per doména
# (slušnost vůči webům + ochrana před blokem).
_MAX_CONCURRENT = 16
_MAX_PER_DOMAIN = 2


async def _crawl_parallel(urls: list[str], max_recipes: int) -> None:
    sem_overall = asyncio.Semaphore(_MAX_CONCURRENT)
    sem_per_domain: dict[str, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(_MAX_PER_DOMAIN)
    )
    cancel_event = asyncio.Event()

    headers = {"User-Agent": settings.user_agent, "Accept-Language": "cs,en;q=0.8"}
    timeout = httpx.Timeout(settings.http_timeout, connect=10.0)
    limits = httpx.Limits(max_connections=_MAX_CONCURRENT * 2, max_keepalive_connections=_MAX_CONCURRENT)

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=headers,
        verify=settings.scraper_verify,
        timeout=timeout,
        limits=limits,
    ) as client:
        tasks = [
            asyncio.create_task(_process_one(client, url, sem_overall, sem_per_domain, cancel_event, max_recipes))
            for url in urls
        ]
        # Sbírej výsledky postupně; jakmile máme dost, signalizuj cancel.
        for coro in asyncio.as_completed(tasks):
            try:
                await coro
            except Exception as exc:  # noqa: BLE001
                log.warning("Crawler task selhal: %s", exc)
            with _lock:
                if _state["added"] >= max_recipes:
                    cancel_event.set()
                    break
        # Po breaku zruš zbylé tasky (jinak by httpx mohlo držet spojení)
        for t in tasks:
            if not t.done():
                t.cancel()
        # Počkej na úplné dokončení (cancellation propagation)
        await asyncio.gather(*tasks, return_exceptions=True)


async def _process_one(
    client: httpx.AsyncClient,
    url: str,
    sem_overall: asyncio.Semaphore,
    sem_per_domain: dict[str, asyncio.Semaphore],
    cancel_event: asyncio.Event,
    max_recipes: int,
) -> None:
    if cancel_event.is_set():
        return
    domain = scraper.domain_of(url)
    sem_dom = sem_per_domain[domain]

    async with sem_overall, sem_dom:
        if cancel_event.is_set():
            return
        with _lock:
            _state["found"] += 1
        try:
            html = await scraper.fetch_html_async(client, url)
        except Exception as exc:  # noqa: BLE001
            log.debug("fetch selhalo %s: %s", url, exc)
            with _lock:
                _state["errors"] += 1
            return

        # Extract + DB persist v thread pool (SQLAlchemy session není async)
        try:
            result = await asyncio.to_thread(_extract_and_persist, html, url)
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest selhal %s: %s", url, exc)
            with _lock:
                _state["errors"] += 1
            return

        if result is None:
            with _lock:
                _state["skipped"] += 1
            return

        title, source_domain = result
        with _lock:
            if _state["added"] >= max_recipes:
                cancel_event.set()
                return
            _state["added"] += 1
            _state["recent"].append({"title": title, "domain": source_domain})


def _extract_and_persist(html: str, url: str) -> tuple[str, str] | None:
    """Synchronní extrakce + DB write. Vrátí (title, domain) nebo None."""
    data = scraper.extract(html, url)
    if data is None:
        return None
    db = SessionLocal()
    try:
        recipe = _persist(db, data)
        if recipe is None:
            return None
        return (recipe.title, recipe.source_domain or "")
    finally:
        db.close()


def crawl_async(
    queries: list[str] | None = None,
    max_recipes: int = 30,
    per_query: int = 8,
) -> bool:
    """Spustí crawl na pozadí. Vrací False, pokud už běží."""
    with _lock:
        if _state["running"]:
            return False
    t = threading.Thread(
        target=crawl,
        kwargs={"queries": queries, "max_recipes": max_recipes, "per_query": per_query},
        daemon=True,
    )
    t.start()
    return True


# ===================== Procházení webů přes sitemapy =====================
def _fetch_bytes(url: str) -> bytes:
    headers = {"User-Agent": settings.user_agent, "Accept-Language": "cs,en;q=0.8"}
    with httpx.Client(
        follow_redirects=True,
        timeout=settings.http_timeout,
        headers=headers,
        verify=settings.scraper_verify,
    ) as cl:
        r = cl.get(url)
        r.raise_for_status()
        return r.content


def _maybe_gunzip(content: bytes) -> bytes:
    if content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except Exception:  # noqa: BLE001
            return content
    return content


def _sitemaps_for(domain: str) -> list[str]:
    """Najdi sitemapy z robots.txt, jinak zkus /sitemap.xml."""
    out: list[str] = []
    try:
        robots = _fetch_bytes(f"https://{domain}/robots.txt").decode("utf-8", "ignore")
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                out.append(line.split(":", 1)[1].strip())
    except Exception:  # noqa: BLE001
        pass
    return out or [f"https://{domain}/sitemap.xml"]


def _urls_from_sitemap(url: str, depth: int = 0) -> list[str]:
    if depth > 2:
        return []
    try:
        text = _maybe_gunzip(_fetch_bytes(url)).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)", text)
    if "<sitemapindex" in text.lower():  # index → rekurze do dílčích sitemap
        random.shuffle(locs)
        out: list[str] = []
        for sm in locs[:8]:
            out += _urls_from_sitemap(sm, depth + 1)
            if len(out) > 3000:
                break
        return out
    return locs


def discover_site(domain: str, max_urls: int = 120) -> list[str]:
    """Vrať náhodný vzorek receptových URL z webu (přes sitemapy)."""
    all_urls: list[str] = []
    for sm in _sitemaps_for(domain)[:5]:
        all_urls += _urls_from_sitemap(sm)
        if len(all_urls) >= max_urls * 6:
            break
    recipe_urls = [
        u for u in all_urls if _RECIPE_HINT.search(u) and not _EXCLUDE.search(u)
    ]
    if not recipe_urls:  # web nemá 'recept' v URL → ber vše krom vyloučeného
        recipe_urls = [u for u in all_urls if not _EXCLUDE.search(u)]
    random.shuffle(recipe_urls)  # ať pokaždé objevíme jiné recepty
    return recipe_urls[:max_urls]


def crawl_sites(
    domains: list[str] | None = None,
    max_recipes: int = 30,
    per_site: int = 12,
) -> dict:
    """Projdi weby a stáhni nové recepty z jejich sitemap (nepotřebuje SearXNG).
    Sběr URL ze sitemap je synchronní, ingest async paralelní."""
    domains = domains or list(settings.recipe_domains) or DEFAULT_SITES
    seen = _existing_urls()
    _set(
        running=True, started_at=time.time(), finished_at=None,
        found=0, added=0, skipped=0, errors=0, current_query=None, recent=[],
    )
    try:
        candidates: list[str] = []
        for dom in domains:
            _set(current_query=dom)
            urls = discover_site(dom, max_urls=per_site * 4)
            log.info("site %s: %s kandidátů ze sitemapy", dom, len(urls))
            site_count = 0
            for url in urls:
                if url in seen:
                    with _lock:
                        _state["skipped"] += 1
                    continue
                seen.add(url)
                candidates.append(url)
                site_count += 1
                if site_count >= per_site:
                    break
            if len(candidates) >= max_recipes * 3:
                break

        log.info("Sites crawler: %s kandidátů z %s domén", len(candidates), len(domains))
        if candidates:
            asyncio.run(_crawl_parallel(candidates, max_recipes))
    finally:
        _set(running=False, finished_at=time.time(), current_query=None)
    return status()


def crawl_sites_async(
    domains: list[str] | None = None,
    max_recipes: int = 30,
    per_site: int = 12,
) -> bool:
    with _lock:
        if _state["running"]:
            return False
    t = threading.Thread(
        target=crawl_sites,
        kwargs={"domains": domains, "max_recipes": max_recipes, "per_site": per_site},
        daemon=True,
    )
    t.start()
    return True


if __name__ == "__main__":
    import sys

    from ..main import init_db

    init_db()
    domains = sys.argv[1:] or None
    print("Procházím weby přes sitemapy…", domains or "(výchozí sada)")
    result = crawl_sites(domains=domains, max_recipes=20)
    print(
        f"Hotovo: nalezeno {result['found']}, přidáno {result['added']}, "
        f"přeskočeno {result['skipped']}, chyby {result['errors']}. "
        f"Receptů v DB: {result['recipes_total']}, surovin: {result['ingredients_total']}."
    )

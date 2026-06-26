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
from datetime import datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import CrawlSource, Ingredient, Recipe
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


def _fetch_bytes_conditional(
    url: str,
    etag: str | None = None,
    if_modified_since: str | None = None,
) -> tuple[bytes | None, str | None, str | None, int]:
    """Conditional GET. Vrátí (content, new_etag, new_last_modified, status_code).

    Pokud server odpoví 304 Not Modified → content=None (volající ví, že nic nestáhl).
    Pokud server odpoví 200 OK → content=bytes + nové hlavičky pro příště.
    """
    headers = {"User-Agent": settings.user_agent, "Accept-Language": "cs,en;q=0.8"}
    if etag:
        headers["If-None-Match"] = etag
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since
    with httpx.Client(
        follow_redirects=True,
        timeout=settings.http_timeout,
        headers=headers,
        verify=settings.scraper_verify,
    ) as cl:
        r = cl.get(url)
        if r.status_code == 304:
            return None, etag, if_modified_since, 304
        r.raise_for_status()
        new_etag = r.headers.get("ETag")
        new_lm = r.headers.get("Last-Modified")
        return r.content, new_etag, new_lm, r.status_code


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


# Páry <loc>...</loc> a volitelný <lastmod>...</lastmod> v rámci jednoho <url> nebo <sitemap> bloku.
_URL_BLOCK_RE = re.compile(
    r"<(?:url|sitemap)\b[^>]*>(.*?)</(?:url|sitemap)>",
    re.IGNORECASE | re.DOTALL,
)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)", re.IGNORECASE)
_LASTMOD_RE = re.compile(r"<lastmod>\s*([^<\s]+)", re.IGNORECASE)


def _parse_lastmod(s: str) -> datetime | None:
    """Sitemap protokol povoluje pár ISO 8601 variant. Stačí nám rok+den, čas zahodit."""
    if not s:
        return None
    s = s.strip()
    # Zkratky: "2024-03-15", "2024-03-15T12:34:56+00:00", "2024-03-15T12:34:56Z"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s.replace("Z", "+0000") if fmt == "%Y-%m-%dT%H:%M:%S%z" and s.endswith("Z") else s, fmt)
            # Strip tzinfo pro porovnání (DB pracuje s naivními datetime)
            return d.replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            continue
    return None


def _urls_from_sitemap(
    url: str,
    depth: int = 0,
    *,
    lastmod_filter: datetime | None = None,
    crawl_source_rows: dict[str, "CrawlSource"] | None = None,
    domain: str | None = None,
) -> list[tuple[str, datetime | None]]:
    """Vrátí list (url, lastmod) párů. Rekurzivně rozbalí sitemap index.

    `lastmod_filter` (volitelný) — vrať jen URL kde `lastmod > filter`. URL bez
    `<lastmod>` se vrátí vždy (konzervativní, raději projde víc než méně).
    """
    if depth > 2:
        return []
    try:
        text = _maybe_gunzip(_fetch_bytes(url)).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return []

    is_index = "<sitemapindex" in text.lower()
    blocks = _URL_BLOCK_RE.findall(text)
    pairs: list[tuple[str, datetime | None]] = []
    for blk in blocks:
        loc_m = _LOC_RE.search(blk)
        if not loc_m:
            continue
        lm_m = _LASTMOD_RE.search(blk)
        lm = _parse_lastmod(lm_m.group(1)) if lm_m else None
        pairs.append((loc_m.group(1), lm))

    # Fallback (sitemap bez bloků <url>) — vezmi všechny <loc>
    if not pairs:
        for loc in _LOC_RE.findall(text):
            pairs.append((loc, None))

    if is_index:
        random.shuffle(pairs)
        out: list[tuple[str, datetime | None]] = []
        for sub_url, sub_lm in pairs[:8]:
            # Sub-sitemapa: optimalizace — pokud sub_lm < lastmod_filter,
            # všechny URL v ní jsou starší, není třeba ji stahovat.
            if lastmod_filter and sub_lm and sub_lm <= lastmod_filter:
                continue
            out += _urls_from_sitemap(sub_url, depth + 1, lastmod_filter=lastmod_filter)
            if len(out) > 3000:
                break
        return out

    # Listová sitemapa: vrať URL splňující filter
    if lastmod_filter:
        return [(u, lm) for u, lm in pairs if lm is None or lm > lastmod_filter]
    return pairs


def discover_site(domain: str, max_urls: int = 120) -> list[str]:
    """Vrať náhodný vzorek receptových URL z webu (přes sitemapy).
    Nejsnažší legacy varianta bez inkrementality."""
    all_pairs: list[tuple[str, datetime | None]] = []
    for sm in _sitemaps_for(domain)[:5]:
        all_pairs += _urls_from_sitemap(sm)
        if len(all_pairs) >= max_urls * 6:
            break
    urls = [u for u, _ in all_pairs]
    recipe_urls = [
        u for u in urls if _RECIPE_HINT.search(u) and not _EXCLUDE.search(u)
    ]
    if not recipe_urls:
        recipe_urls = [u for u in urls if not _EXCLUDE.search(u)]
    random.shuffle(recipe_urls)
    return recipe_urls[:max_urls]


def discover_site_incremental(
    db: "Session", domain: str, max_urls: int = 5000,
) -> tuple[list[str], dict]:
    """Inkrementální verze přes `crawl_source`. Vrátí (urls, stats).

    Postup:
      1. Najdi/vytvoř `crawl_source` pro doménu.
      2. Conditional GET na kořenovou sitemapu — `ETag`/`Last-Modified`.
         - 304 → vrať [] (a updatuj `last_run_at`).
      3. Parsuj `<loc>` + `<lastmod>` ze všech (sub)sitemap.
      4. Filter podle `crawl_source.last_lastmod`.
      5. Apply `_RECIPE_HINT` + `_EXCLUDE`.
      6. Updatuj `crawl_source` (ETag, last_modified, last_lastmod, total_seen).
    """
    from ..models import CrawlSource

    cs = db.get(CrawlSource, domain)
    if cs is None:
        cs = CrawlSource(domain=domain)
        db.add(cs)
        db.flush()

    sitemap_urls = _sitemaps_for(domain)
    if not sitemap_urls:
        return [], {"status": "no_sitemap"}
    primary_sm = sitemap_urls[0]

    # 1) Conditional GET na kořenovou sitemapu
    try:
        content, new_etag, new_lm, status_code = _fetch_bytes_conditional(
            primary_sm,
            etag=cs.etag,
            if_modified_since=cs.http_last_modified,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sitemap %s fetch selhal: %s", primary_sm, exc)
        cs.last_error = str(exc)[:500]
        cs.last_run_at = datetime.utcnow()
        return [], {"status": "fetch_failed", "error": str(exc)}

    if status_code == 304:
        cs.last_run_at = datetime.utcnow()
        cs.last_error = None
        log.info("sitemap %s: 304 Not Modified — nic nového", domain)
        return [], {"status": "not_modified"}

    # 2) Aktualizuj cache hlavičky
    cs.etag = new_etag
    cs.http_last_modified = new_lm
    cs.sitemap_url = primary_sm

    # 3) Posbírej páry (url, lastmod) ze všech sitemap (s filtrem od minula)
    all_pairs: list[tuple[str, datetime | None]] = []
    filter_dt = cs.last_lastmod  # None na úplně prvním běhu
    for sm in sitemap_urls[:5]:
        try:
            all_pairs += _urls_from_sitemap(sm, lastmod_filter=filter_dt)
        except Exception as exc:  # noqa: BLE001
            log.debug("sub-sitemap %s selhala: %s", sm, exc)
        if len(all_pairs) >= max_urls * 2:
            break

    # 4) Apply _RECIPE_HINT + _EXCLUDE
    recipe_pairs = [
        (u, lm) for u, lm in all_pairs
        if _RECIPE_HINT.search(u) and not _EXCLUDE.search(u)
    ]
    if not recipe_pairs:
        recipe_pairs = [(u, lm) for u, lm in all_pairs if not _EXCLUDE.search(u)]

    # 5) Spočti max lastmod (pro update DB)
    new_max_lm = cs.last_lastmod
    for _, lm in recipe_pairs:
        if lm is not None and (new_max_lm is None or lm > new_max_lm):
            new_max_lm = lm

    # 6) Updatuj crawl_source
    cs.last_run_at = datetime.utcnow()
    cs.last_lastmod = new_max_lm
    cs.total_seen = (cs.total_seen or 0) + len(recipe_pairs)
    cs.last_error = None

    # 7) Náhodné promíchání (pro fairness) a oříznutí
    random.shuffle(recipe_pairs)
    urls = [u for u, _ in recipe_pairs[:max_urls]]
    log.info(
        "site %s: %s nových/změněných URL (po lastmod filtru, raw bloků=%s)",
        domain, len(urls), len(all_pairs),
    )
    return urls, {
        "status": "fetched",
        "candidates": len(urls),
        "raw_blocks": len(all_pairs),
        "filter_dt": filter_dt.isoformat() if filter_dt else None,
        "new_max_lm": new_max_lm.isoformat() if new_max_lm else None,
    }


def crawl_sites(
    domains: list[str] | None = None,
    max_recipes: int = 30,
    per_site: int | None = None,
) -> dict:
    """Projdi weby a stáhni nové recepty z jejich sitemap (nepotřebuje SearXNG).

    Použije `crawl_source` tabulku pro inkrementální crawl:
      - Conditional GET na sitemapu (ETag / If-Modified-Since)
      - `<lastmod>` filter proti `crawl_source.last_lastmod`

    `per_site=None` znamená nelimitovat (round-robin přes všechny domény).
    """
    domains = domains or list(settings.recipe_domains) or DEFAULT_SITES
    seen = _existing_urls()
    _set(
        running=True, started_at=time.time(), finished_at=None,
        found=0, added=0, skipped=0, errors=0, current_query=None, recent=[],
    )
    try:
        # 1) Sběr kandidátů per doména (inkrementálně přes crawl_source)
        per_domain_urls: dict[str, list[str]] = {}
        db = SessionLocal()
        try:
            for dom in domains:
                _set(current_query=dom)
                try:
                    urls, info = discover_site_incremental(db, dom, max_urls=max_recipes * 5)
                except Exception as exc:  # noqa: BLE001
                    log.warning("discover_site_incremental(%s) selhalo: %s", dom, exc)
                    continue
                # Filter URL, které už máme v DB
                fresh = [u for u in urls if u not in seen]
                if not fresh:
                    continue
                per_domain_urls[dom] = fresh
            db.commit()  # ulož aktualizovaný crawl_source pro všechny domény
        finally:
            db.close()

        # 2) Round-robin merge — vyvážený mix přes domény, aby žádná nebyla
        #    "vyhladovaná" jen proto, že je v abecedě dál.
        candidates: list[str] = []
        domain_iters = {dom: iter(urls) for dom, urls in per_domain_urls.items()}
        while domain_iters and len(candidates) < max_recipes * 3:
            empty = []
            for dom, it in domain_iters.items():
                try:
                    u = next(it)
                    if u not in seen:
                        candidates.append(u)
                        seen.add(u)
                        if len(candidates) >= max_recipes * 3:
                            break
                except StopIteration:
                    empty.append(dom)
            for d in empty:
                del domain_iters[d]

        log.info(
            "Sites crawler: %s nových URL z %s/%s aktivních domén "
            "(inkrement: domény bez změny → 0)",
            len(candidates), len(per_domain_urls), len(domains),
        )

        # 3) Async paralelní ingest
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

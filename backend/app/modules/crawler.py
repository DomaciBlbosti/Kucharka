"""Automatický crawler – sám plní databázi receptů.

Pro každý seed dotaz: SearXNG → kandidátní URL → dedup proti DB → scrape →
normalizace (Ollama) → uložení. Běží na pozadí ve vlákně; stav lze číst přes
`status()` a spustit přes `crawl_async()`.
"""
from __future__ import annotations

import gzip
import logging
import random
import re
import threading
import time

import httpx
from sqlalchemy import func, select

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient, Recipe
from . import discovery
from .ingest import ingest_url

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
    """Synchronní crawl. Vrací finální stav."""
    if not settings.searxng_enabled:
        log.warning("SearXNG není nastavený – crawler nemá kde hledat.")
        return status()

    queries = queries or settings.crawler_seeds or DEFAULT_SEEDS
    seen = _existing_urls()
    _set(
        running=True, started_at=time.time(), finished_at=None,
        found=0, added=0, skipped=0, errors=0, current_query=None, recent=[],
    )

    added = 0
    try:
        for q in queries:
            if added >= max_recipes:
                break
            _set(current_query=q)
            for cand in discovery.search(q, limit=per_query):
                if added >= max_recipes:
                    break
                url = cand["url"]
                if url in seen:
                    with _lock:
                        _state["skipped"] += 1
                    continue
                seen.add(url)
                with _lock:
                    _state["found"] += 1

                db = SessionLocal()
                try:
                    recipe = ingest_url(db, url)
                    if recipe:
                        added += 1
                        with _lock:
                            _state["added"] = added
                            _state["recent"].append(
                                {"title": recipe.title, "domain": recipe.source_domain}
                            )
                        log.info("crawl + %s", recipe.title)
                    else:
                        with _lock:
                            _state["skipped"] += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("crawl ingest selhal %s: %s", url, exc)
                    with _lock:
                        _state["errors"] += 1
                finally:
                    db.close()
                time.sleep(0.4)  # mírné tempo i nad rámec per-doménového throttlu
    finally:
        _set(running=False, finished_at=time.time(), current_query=None)

    return status()


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
    """Projdi weby a stáhni nové recepty z jejich sitemap (nepotřebuje SearXNG)."""
    domains = domains or list(settings.recipe_domains) or DEFAULT_SITES
    seen = _existing_urls()
    _set(
        running=True, started_at=time.time(), finished_at=None,
        found=0, added=0, skipped=0, errors=0, current_query=None, recent=[],
    )
    added = 0
    try:
        for dom in domains:
            if added >= max_recipes:
                break
            _set(current_query=dom)
            urls = discover_site(dom, max_urls=per_site * 4)
            log.info("site %s: %s kandidátů ze sitemapy", dom, len(urls))
            site_added = 0
            for url in urls:
                if added >= max_recipes or site_added >= per_site:
                    break
                if url in seen:
                    continue
                seen.add(url)
                with _lock:
                    _state["found"] += 1
                db = SessionLocal()
                try:
                    recipe = ingest_url(db, url)
                    if recipe:
                        added += 1
                        site_added += 1
                        with _lock:
                            _state["added"] = added
                            _state["recent"].append(
                                {"title": recipe.title, "domain": recipe.source_domain}
                            )
                        log.info("crawl + %s", recipe.title)
                    else:
                        with _lock:
                            _state["skipped"] += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("crawl ingest selhal %s: %s", url, exc)
                    with _lock:
                        _state["errors"] += 1
                finally:
                    db.close()
                time.sleep(0.4)
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

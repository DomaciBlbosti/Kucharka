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
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select

from ..config import settings
from ..db import SessionLocal
from ..models import CrawlDomainState, CrawlUrl, Ingredient, Recipe
from . import discovery
from .ingest import ingest_url

log = logging.getLogger("kucharka.crawler")

# Sitemapa domény se znovu nestahuje/neparsuje častěji než tohle – ať se
# nezatěžuje cizí server a hlavně ať to není zbytečně pomalé (u velkých webů
# je sitemapa i tisíce URL). Nové recepty na webu i tak najdeme při dalším
# běhu po uplynutí týhle doby.
_SYNC_MIN_INTERVAL = timedelta(hours=6)

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


def is_running() -> bool:
    """Jen paměťový flag, žádný DB dotaz – bezpečné volat i když crawler
    zrovna drží DB (na rozdíl od status(), který dělá count přes DB)."""
    with _lock:
        return bool(_state["running"])


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
    if depth > 3:
        return []
    try:
        text = _maybe_gunzip(_fetch_bytes(url)).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)", text)
    if "<sitemapindex" in text.lower():  # index → rekurze do VŠECH dílčích sitemap
        # Žádný shuffle ani ořez: pro persistentní frontu chceme projít celou
        # sitemapu deterministicky. Náhodný výběr dílčích sitemap (relikt staré
        # "vzorkovací" logiky) způsoboval, že každý sync objevil jinou
        # podmnožinu URL → fronta pak nekontrolovaně narůstala nad skutečnou
        # velikost webu (klidně 4–6×).
        out: list[str] = []
        for sm in locs:
            out += _urls_from_sitemap(sm, depth + 1)
        return out
    return locs


def discover_site(domain: str, max_urls: int = 120) -> list[str]:
    """Vrať náhodný vzorek receptových URL z webu (přes sitemapy). Zachováno
    kvůli zpětné kompatibilitě (query-based `crawl()` výš to nepoužívá, ale
    může to používat existující kód/testy) – nová fronta jede přes
    `discover_site_all` + `sync_domain` níž."""
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


def discover_site_all(domain: str, cap: int = 100000) -> list[str]:
    """Vrať VŠECHNY receptové URL ze sitemapy domény, deterministicky, bez
    náhodného vzorkování/ořezu – pro naplnění persistentní fronty
    (`sync_domain`). `cap` je jen pojistka proti extrémně obřím sitemapám.

    Determinismus je tu podstatný: kdyby dvě po sobě jdoucí volání vrátila
    různé podmnožiny URL, `sync_domain` by pořád dokola přidával „nové" URL a
    fronta by narostla nad skutečnou velikost webu.
    """
    all_urls: list[str] = []
    for sm in _sitemaps_for(domain):
        all_urls += _urls_from_sitemap(sm)
        if len(all_urls) >= cap:
            break
    recipe_urls = [
        u for u in all_urls if _RECIPE_HINT.search(u) and not _EXCLUDE.search(u)
    ]
    if not recipe_urls:
        recipe_urls = [u for u in all_urls if not _EXCLUDE.search(u)]
    seen: set[str] = set()
    out: list[str] = []
    for u in recipe_urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
        if len(out) >= cap:
            break
    return out


def sync_domain(db, domain: str, force: bool = False) -> int:
    """Stáhni sitemapu domény a přidej nové URL do persistentní fronty
    (`crawl_url`, status='pending'). URL, co už frontu jednou prošly (ať už
    úspěšně, s chybou, nebo jako skip), se NEPŘIDÁVAJÍ znovu – díky tomu
    crawler nezkouší donekonečna to samé. Vrací počet nově přidaných URL.

    Sitemapa se nestahuje častěji než `_SYNC_MIN_INTERVAL`, pokud `force`
    není True – ať se web zbytečně nezatěžuje a hlavně ať to není pomalé
    (velké sitemapy mají klidně tisíce URL)."""
    state = db.get(CrawlDomainState, domain)
    if state and state.last_synced_at and not force:
        if datetime.utcnow() - state.last_synced_at < _SYNC_MIN_INTERVAL:
            return 0

    urls = discover_site_all(domain)
    existing = set(db.scalars(select(CrawlUrl.url).where(CrawlUrl.domain == domain)))
    added = 0
    for u in urls:
        if u in existing:
            continue
        db.add(CrawlUrl(domain=domain, url=u, status="pending"))
        added += 1

    if state is None:
        state = CrawlDomainState(domain=domain)
        db.add(state)
    state.last_synced_at = datetime.utcnow()
    state.last_sync_added = added
    state.sitemap_urls_total = len(urls)
    db.commit()
    log.info(
        "sync %s: %s nových URL do fronty (sitemapa má celkem %s kandidátů)",
        domain, added, len(urls),
    )
    return added


def process_queue(db, max_items: int = 30) -> dict:
    """Zpracuj až `max_items` čekajících (status='pending') URL z fronty,
    v pořadí od nejdřív objevených. Každá URL se zpracuje NEJVÝŠ JEDNOU za
    volání – výsledek (ok/skip/error + důvod) se zapíše zpátky do řádku, ať
    je vidět historie i v přehledové tabulce v adminu."""
    rows = db.scalars(
        select(CrawlUrl)
        .where(CrawlUrl.status == "pending")
        .order_by(CrawlUrl.discovered_at.asc())
        .limit(max_items)
    ).all()
    added = 0
    for row in rows:
        row.attempted_at = datetime.utcnow()
        row.attempts += 1
        with _lock:
            _state["found"] += 1
        try:
            recipe = ingest_url(db, row.url)
            if recipe:
                row.status = "ok"
                row.error = None
                row.recipe_id = recipe.id
                added += 1
                with _lock:
                    _state["added"] += 1
                    _state["recent"].append(
                        {"title": recipe.title, "domain": recipe.source_domain}
                    )
                log.info("crawl + %s", recipe.title)
            else:
                row.status = "skip"
                row.error = "Není recept, nebo se nedal zpracovat (bez chyby)."
                with _lock:
                    _state["skipped"] += 1
        except Exception as exc:  # noqa: BLE001
            # Session je po chybě (např. IntegrityError) v rozbitém stavu –
            # rollback ji vyčistí, jinak by spadly i všechny další recepty
            # ve stejném běhu. Řádek fronty pak označíme v čisté transakci.
            db.rollback()
            with _lock:
                _state["errors"] += 1
            log.warning("crawl ingest selhal %s: %s", row.url, exc)
            fresh = db.get(CrawlUrl, row.id)
            if fresh is not None:
                fresh.attempted_at = datetime.utcnow()
                fresh.status = "error"
                fresh.error = str(exc)[:500]
            db.commit()
            time.sleep(0.4)
            continue
        db.commit()
        time.sleep(0.4)
    return {"processed": len(rows), "added": added}


def crawl_sites(
    domains: list[str] | None = None,
    max_recipes: int = 30,
    per_site: int = 12,  # ponecháno kvůli zpětné kompatibilitě volání, dál nepoužito
) -> dict:
    """Projdi weby přes persistentní frontu: nejdřív dosyncuj sitemapy všech
    domén (jen těch, co nebyly syncované nedávno), pak zpracuj až
    `max_recipes` čekajících URL z fronty (napříč doménami, od nejstarších)."""
    domains = domains or list(settings.recipe_domains) or DEFAULT_SITES
    _set(
        running=True, started_at=time.time(), finished_at=None,
        found=0, added=0, skipped=0, errors=0, current_query=None, recent=[],
    )
    db = SessionLocal()
    try:
        for dom in domains:
            _set(current_query=dom)
            try:
                sync_domain(db, dom)
            except Exception as exc:  # noqa: BLE001
                log.warning("sync %s selhal: %s", dom, exc)
        _set(current_query=None)
        process_queue(db, max_items=max_recipes)
    finally:
        db.close()
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


def queue_stats(domain: str | None = None) -> dict:
    """Souhrn stavu fronty (pro admin dashboard).

    Doménové rozpady se počítají ze SKUTEČNÉHO obsahu fronty (`crawl_url`),
    ne ze `sitemap_urls_total` v `crawl_domain_state`. Ta druhá hodnota je
    jen velikost sitemapy z posledního syncu (přepisuje se) a nemá důvod
    odpovídat počtu řádků ve frontě – kvůli tomu dřív součet přes čipy
    nesouhlasil s „vše". Teď je součet přes domény == „vše".
    """
    db = SessionLocal()
    try:
        q = select(CrawlUrl.status, func.count(CrawlUrl.id)).group_by(CrawlUrl.status)
        if domain:
            q = q.where(CrawlUrl.domain == domain)
        counts = {s: c for s, c in db.execute(q).all()}

        # počet URL ve frontě per doména (skutečnost) – tohle se sečte na „vše"
        per_domain = dict(
            db.execute(
                select(CrawlUrl.domain, func.count(CrawlUrl.id)).group_by(CrawlUrl.domain)
            ).all()
        )
        # čas posledního syncu si necháme z crawl_domain_state (jen doplňková info)
        synced = {
            d: t
            for d, t in db.execute(
                select(CrawlDomainState.domain, CrawlDomainState.last_synced_at)
            ).all()
        }
        doms = [
            {
                "domain": d,
                "queued": n,  # skutečný počet URL téhle domény ve frontě
                "last_synced_at": synced[d].isoformat() if synced.get(d) else None,
            }
            for d, n in sorted(per_domain.items(), key=lambda x: -x[1])
        ]
        return {
            "pending": counts.get("pending", 0),
            "ok": counts.get("ok", 0),
            "skip": counts.get("skip", 0),
            "error": counts.get("error", 0),
            "domains": doms,
        }
    finally:
        db.close()


# --- Pročištění fronty na pozadí -------------------------------------------
# Prune stahuje a parsuje celé sitemapy všech domén, což u velkých webů trvá
# klidně minuty – nesmí běžet v HTTP requestu (Cloudflare zabíjí spojení po
# ~100 s → HTTP 524). Běží tedy ve vlákně a UI se ptá na stav přes prune_status().
_prune_lock = threading.Lock()
_prune_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "current_domain": None,
    "removed": 0,
    "checked_domains": 0,
    "result": {},
    "error": None,
}


def prune_status() -> dict:
    with _prune_lock:
        return dict(_prune_state)


def _prune_run(domains: list[str], dry_run: bool) -> None:
    db = SessionLocal()
    try:
        result: dict[str, dict] = {}
        removed_total = 0
        for i, dom in enumerate(domains, 1):
            with _prune_lock:
                _prune_state["current_domain"] = dom
                _prune_state["checked_domains"] = i
            try:
                valid = set(discover_site_all(dom))
            except Exception as exc:  # noqa: BLE001
                result[dom] = {"error": str(exc)[:200]}
                continue
            pending = db.scalars(
                select(CrawlUrl).where(
                    CrawlUrl.domain == dom, CrawlUrl.status == "pending"
                )
            ).all()
            stale = [r for r in pending if r.url not in valid]
            result[dom] = {
                "pending_total": len(pending),
                "in_sitemap": len(pending) - len(stale),
                "stale_removed": len(stale),
                "dry_run": dry_run,
            }
            if not dry_run and stale:
                for r in stale:
                    db.delete(r)
                db.commit()
                removed_total += len(stale)
            with _prune_lock:
                _prune_state["removed"] = removed_total
                _prune_state["result"] = dict(result)
    except Exception as exc:  # noqa: BLE001
        with _prune_lock:
            _prune_state["error"] = str(exc)[:500]
        log.warning("prune selhal: %s", exc)
    finally:
        db.close()
        with _prune_lock:
            _prune_state["running"] = False
            _prune_state["finished_at"] = time.time()
            _prune_state["current_domain"] = None


def prune_async(domains: list[str], dry_run: bool = True) -> bool:
    """Spusť pročištění fronty na pozadí. Vrací False, když už běží."""
    with _prune_lock:
        if _prune_state["running"]:
            return False
        _prune_state.update(
            running=True, started_at=time.time(), finished_at=None,
            current_domain=None, removed=0, checked_domains=0,
            result={}, error=None,
        )
    t = threading.Thread(target=_prune_run, args=(domains, dry_run), daemon=True)
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

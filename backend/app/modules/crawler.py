"""Automatický crawler – sám plní databázi receptů.

Pro každý seed dotaz: SearXNG → kandidátní URL → dedup proti DB → scrape →
normalizace (Ollama) → uložení. Běží na pozadí ve vlákně; stav lze číst přes
`status()` a spustit přes `crawl_async()`.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import func, select

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient, Recipe
from . import discovery
from .ingest import ingest_url

log = logging.getLogger("kucharka.crawler")

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


if __name__ == "__main__":
    import sys

    from ..main import init_db

    init_db()
    qs = sys.argv[1:] or None
    print("Spouštím crawl…", "(seeds: vlastní)" if qs else "(seeds: výchozí)")
    result = crawl(queries=qs, max_recipes=15)
    print(
        f"Hotovo: nalezeno {result['found']}, přidáno {result['added']}, "
        f"přeskočeno {result['skipped']}, chyby {result['errors']}. "
        f"Receptů v DB: {result['recipes_total']}, surovin: {result['ingredients_total']}."
    )

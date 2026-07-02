"""Úlohy jádra na pozadí: crawl (objevit+zpracovat+odeslat), překlad, kategorizace.

Vše komunikuje s webovou appkou přes webclient (žádná DB v jádru).
"""
from __future__ import annotations

import logging
import random
import threading
import time

from . import config, discovery, llm, scraper, webclient
from .pipeline import Matcher, build_payload, looks_czech

log = logging.getLogger("core.jobs")

_lock = threading.Lock()
_state: dict = {
    "crawl": {"running": False, "done": 0, "found": 0, "pushed": 0},
    "translate": {"running": False, "done": 0, "total": 0},
    "categorize": {"running": False, "done": 0, "total": 0},
    "last_error": None,
}


def status() -> dict:
    with _lock:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in _state.items()}


def _set(job: str, **kw):
    with _lock:
        _state[job].update(kw)


def _running(job: str) -> bool:
    with _lock:
        return _state[job]["running"]


# ---------- crawl ----------

def run_crawl(max_recipes: int | None = None) -> None:
    if _running("crawl"):
        return
    c = config.get()
    max_recipes = max_recipes or c["crawler_max_per_run"]
    _set("crawl", running=True, done=0, found=0, pushed=0)
    try:
        matcher = Matcher(webclient.dictionary())
        doms = discovery.domains()
        random.shuffle(doms)
        pushed = 0
        for dom in doms:
            if pushed >= max_recipes:
                break
            try:
                urls = discovery.discover_site(dom, max_urls=max_recipes * 3)
                new = webclient.filter_new(urls)
            except Exception as exc:  # noqa: BLE001
                log.warning("discover %s: %s", dom, exc)
                continue
            _set("crawl", found=_state["crawl"]["found"] + len(new))
            for url in new:
                if pushed >= max_recipes:
                    break
                try:
                    scraped = scraper.fetch_and_extract(url)
                    if not scraped:
                        continue
                    if c["translate_to_cs"] and not looks_czech(
                        scraped.get("source_domain"),
                        f"{scraped['title']} {scraped.get('instructions') or ''}",
                    ):
                        tr = llm.translate_fields(
                            scraped["title"], scraped["ingredients"], scraped.get("instructions") or ""
                        )
                        if tr:
                            scraped["title"] = tr["title"]
                            scraped["ingredients"] = tr["ingredients"]
                            scraped["instructions"] = tr["instructions"]
                    payload = build_payload(scraped, matcher)
                    webclient.upsert_recipe(payload)
                    pushed += 1
                    _set("crawl", pushed=pushed)
                except Exception as exc:  # noqa: BLE001
                    log.warning("zpracování %s: %s", url, exc)
                finally:
                    _set("crawl", done=_state["crawl"]["done"] + 1)
    except Exception as exc:  # noqa: BLE001
        _set("crawl", )
        with _lock:
            _state["last_error"] = str(exc)
    finally:
        _set("crawl", running=False)


# ---------- překlad existujících ----------

def run_translate(limit: int = 100) -> None:
    if _running("translate"):
        return
    _set("translate", running=True, done=0, total=0)
    try:
        recipes = webclient.recipes_needing("translate", limit=limit)
        _set("translate", total=len(recipes))
        for r in recipes:
            try:
                if looks_czech(r.get("source_domain"), f"{r['title']} {r.get('instructions') or ''}"):
                    continue
                texts = [i["raw_text"] for i in r["ingredients"]]
                tr = llm.translate_fields(r["title"], texts, r.get("instructions") or "")
                if tr:
                    webclient.patch_recipe(r["id"], {
                        "title": tr["title"],
                        "instructions": tr["instructions"],
                        "ingredient_texts": tr["ingredients"],
                    })
            except Exception as exc:  # noqa: BLE001
                log.warning("překlad %s: %s", r.get("id"), exc)
            finally:
                _set("translate", done=_state["translate"]["done"] + 1)
    finally:
        _set("translate", running=False)


# ---------- kategorizace surovin ----------

def run_categorize(limit: int = 500) -> None:
    if _running("categorize"):
        return
    _set("categorize", running=True, done=0, total=0)
    try:
        items = webclient.ingredients_needing("categorize", limit=limit)
        _set("categorize", total=len(items))
        batch = [(it["id"], it["name_cs"]) for it in items]
        for i in range(0, len(batch), 25):
            chunk = batch[i : i + 25]
            paths = llm.categorize_batch(chunk)
            for iid, path in paths.items():
                try:
                    webclient.patch_ingredient(iid, {"category_path": path})
                except Exception as exc:  # noqa: BLE001
                    log.warning("kategorie %s: %s", iid, exc)
            _set("categorize", done=_state["categorize"]["done"] + len(chunk))
    finally:
        _set("categorize", running=False)


def _async(fn, *a) -> bool:
    threading.Thread(target=fn, args=a, daemon=True).start()
    return True


def start_crawl(mx=None):
    return False if _running("crawl") else _async(run_crawl, mx)


def start_translate():
    return False if _running("translate") else _async(run_translate)


def start_categorize():
    return False if _running("categorize") else _async(run_categorize)


# ---------- plánovač ----------

_sched = None


def _get_sched():
    global _sched
    if _sched is None:
        try:
            from apscheduler.executors.pool import ThreadPoolExecutor
            from apscheduler.schedulers.background import BackgroundScheduler

            _sched = BackgroundScheduler(daemon=True, executors={"default": ThreadPoolExecutor(1)})
            _sched.start()
        except Exception as exc:  # noqa: BLE001
            log.warning("plánovač: %s", exc)
            return None
    return _sched


def configure_schedule() -> None:
    sched = _get_sched()
    if sched is None:
        return
    c = config.get()
    plan = [
        ("crawler", c["crawler_enabled"], c["crawler_interval_min"], run_crawl),
        ("translate", c["auto_translate_enabled"], c["auto_translate_interval_min"], run_translate),
        ("categorize", c["auto_categorize_enabled"], c["auto_categorize_interval_min"], run_categorize),
    ]
    for jid, enabled, minutes, fn in plan:
        try:
            sched.remove_job(jid)
        except Exception:  # noqa: BLE001
            pass
        if enabled:
            sched.add_job(fn, "interval", minutes=max(1, minutes), id=jid, max_instances=1, coalesce=True)
            log.info("Úloha '%s' každých %s min.", jid, minutes)

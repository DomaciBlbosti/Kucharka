"""Přeparsování postupů u už uložených receptů podle doménových pravidel.

Když se do `site_rules` přidá pravidlo pro nějaký web, historické recepty
z něj mají pořád postup vyparsovaný generickým schema.org parserem. Tahle
úloha je projde, stáhne stránku znovu a postup přepíše – nic jiného se
nemění (název, suroviny, obrázek ani hodnocení zůstávají).

Běží na pozadí a stahuje přes `scraper.fetch_html`, takže platí stejný
per-doménový throttle jako při crawlu. Recept se přepíše jen tehdy, když
pravidlo vrátí použitelný postup; při chybě stahování se přeskočí a zkusí
se zase příště.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import false, func, or_, select

from ..db import SessionLocal
from ..models import Recipe, RecipeEmbedding
from . import site_rules
from .scraper import fetch_html

log = logging.getLogger("kucharka.reparse")

_lock = threading.Lock()
_stop = threading.Event()
_state: dict = {
    "running": False,
    "domains": [],
    "total": 0,
    "done": 0,
    "changed": 0,
    "unchanged": 0,
    "no_rule_match": 0,
    "failed": 0,
    "last_error": None,
    "started_at": None,
    "finished_at": None,
}


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def status() -> dict:
    with _lock:
        s = dict(_state)
    s["domains_with_rule"] = sorted(site_rules.INSTRUCTION_RULES)
    return s


def is_running() -> bool:
    with _lock:
        return bool(_state["running"])


def pending_count(domains: list[str] | None = None) -> int:
    """Kolik receptů spadá pod doménové pravidlo (bez stahování čehokoli)."""
    doms = domains or sorted(site_rules.INSTRUCTION_RULES)
    db = SessionLocal()
    try:
        return db.scalar(
            select(func.count(Recipe.id)).where(_domain_filter(doms))
        ) or 0
    finally:
        db.close()


def _domain_filter(domains: list[str]):
    """source_domain sedí na doménu s pravidlem (s www. i bez)."""
    if not domains:
        return false()
    return or_(*[
        Recipe.source_domain.in_((d, f"www.{d}")) for d in domains
    ])


def run(domains: list[str] | None = None, limit: int | None = None) -> dict:
    doms = domains or sorted(site_rules.INSTRUCTION_RULES)
    _stop.clear()
    db = SessionLocal()
    try:
        ids = list(db.scalars(
            select(Recipe.id).where(_domain_filter(doms)).order_by(Recipe.id)
        ).all())
        if limit:
            ids = ids[:limit]
        _set(domains=doms, total=len(ids), done=0, changed=0, unchanged=0,
             no_rule_match=0, failed=0, last_error=None)
        log.info("Přeparsování postupů: %s receptů z domén %s", len(ids), doms)

        for rid in ids:
            if _stop.is_set():
                log.info("Přeparsování postupů zastaveno uživatelem.")
                break
            try:
                _one(db, rid)
            except Exception as exc:  # noqa: BLE001 – jeden recept nesmí shodit běh
                with _lock:
                    _state["failed"] += 1
                    _state["last_error"] = f"{rid}: {type(exc).__name__}: {exc}"[:300]
            with _lock:
                _state["done"] += 1
        with _lock:
            out = {k: _state[k] for k in
                   ("total", "done", "changed", "unchanged", "no_rule_match", "failed")}
        log.info("Přeparsování postupů hotové: %s", out)
        return out
    finally:
        db.close()


def _one(db, recipe_id: int) -> None:
    rec = db.get(Recipe, recipe_id)
    if rec is None or not rec.source_url:
        return
    domain = (rec.source_domain or "").replace("www.", "")
    instructions = site_rules.instructions_for(domain, fetch_html(rec.source_url))
    if not instructions:
        with _lock:
            _state["no_rule_match"] += 1
        return
    if instructions.strip() == (rec.instructions or "").strip():
        with _lock:
            _state["unchanged"] += 1
        return
    rec.instructions = instructions
    # Vektor v RAG indexu je počítaný ze starého textu. index_recipes()
    # přeskakuje recepty, které už embedding mají, takže ho musíme zahodit –
    # jinak by se sémantické hledání dál řídilo marketingovým úvodem.
    db.query(RecipeEmbedding).filter(RecipeEmbedding.recipe_id == rec.id).delete()
    db.commit()
    with _lock:
        _state["changed"] += 1


def run_async(domains: list[str] | None = None, limit: int | None = None) -> bool:
    """Spusť na pozadí. Vrací False, když už běží."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, started_at=time.time(), finished_at=None)

    def _worker():
        try:
            run(domains=domains, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("Přeparsování postupů selhalo: %s", exc)
            _set(last_error=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            _set(running=False, finished_at=time.time())

    threading.Thread(target=_worker, daemon=True, name="reparse-instructions").start()
    return True


def stop() -> None:
    _stop.set()

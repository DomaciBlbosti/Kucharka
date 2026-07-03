"""Překlad zahraničních receptů do češtiny přes Ollamu.

České recepty (doména .cz nebo text s českou diakritikou) se nepřekládají.
U cizích se přeloží titul, ingredience a postup jedním dotazem; pokud se
nezachová počet ingrediencí, ponecháme originál (kvůli párování surovin).

Kromě překladu při importu umí modul i ZPĚTNĚ přeložit už uložené recepty
(retranslate_*), což využívá údržba v administraci — typicky pro recepty
stažené v době, kdy Ollama/model nebyl dostupný.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe
from .ollamachat import chat_json

log = logging.getLogger("kucharka.translate")

_CZ_CHARS = set("ěščřžůňďť")


def looks_czech(domain: str | None, text: str) -> bool:
    if domain and domain.endswith(".cz"):
        return True
    sample = (text or "")[:2000].lower()
    return any(c in _CZ_CHARS for c in sample)


def _translate_fields(title: str, ingredients: list[str], instructions: str) -> dict | None:
    """Zavolá Ollamu a vrátí přeložená pole, nebo None (chyba / nesedí počet)."""
    if not settings.ollama_enabled:
        return None
    payload = {
        "title": title or "",
        "ingredients": list(ingredients),
        "instructions": instructions or "",
    }
    prompt = (
        "Přelož tento recept do češtiny. Zachovej přesně počet a pořadí "
        "ingrediencí. Jednotky a množství ponech, jen přelož názvy. Odpověz "
        "POUZE JSON objektem "
        '{"title": string, "ingredients": [string], "instructions": string}.\n'
        f"Recept: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        out = chat_json(
            settings.ollama_url,
            settings.ollama_fast_model,
            prompt,
            keep_alive=settings.ollama_keep_alive,
            timeout=max(settings.http_timeout, 120),
        )
        if out is None:
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("překlad selhal: %s", exc)
        return None

    new_ing = out.get("ingredients")
    if not isinstance(new_ing, list) or len(new_ing) != len(ingredients):
        log.info("překlad zahozen (nesedí počet ingrediencí)")
        return None
    return {
        "title": (out.get("title") or title or "").strip(),
        "ingredients": [str(x) for x in new_ing],
        "instructions": out.get("instructions") or instructions or "",
    }


def translate_recipe(data: dict) -> dict:
    """Přelož recept (dict při importu) do češtiny, je-li cizí.

    Původní (nepřeložený) text se uloží do original_* klíčů, aby ho appka
    mohla později zobrazit / na něj přepnout – recept se v UI ukáže česky,
    s možností podívat se na předlohu.
    """
    if not settings.translate_to_cs or not settings.ollama_enabled:
        return data
    probe = f"{data.get('title', '')} {data.get('instructions') or ''}"
    if looks_czech(data.get("source_domain"), probe):
        return data
    orig_title = data.get("title", "")
    orig_ingredients = list(data.get("ingredients", []))
    orig_instructions = data.get("instructions") or ""
    res = _translate_fields(orig_title, orig_ingredients, orig_instructions)
    if not res:
        return data
    data["original_title"] = orig_title
    data["original_ingredients"] = orig_ingredients
    data["original_instructions"] = orig_instructions or None
    data["title"] = res["title"]
    data["ingredients"] = res["ingredients"]
    if res["instructions"]:
        data["instructions"] = res["instructions"]
    data["translated_from"] = data.get("source_domain")
    return data


def is_foreign(recipe: Recipe) -> bool:
    return not looks_czech(
        recipe.source_domain, f"{recipe.title} {recipe.instructions or ''}"
    )


def retranslate_recipe(db, recipe: Recipe) -> bool:
    """Přelož už uložený recept (titul, postup, raw_text ingrediencí).

    Zachová originál – pokud už je uložený (např. z předchozího běhu), znovu
    ho nepřepisuje, ať se v případě opakovaného spuštění neztratí.
    """
    texts = [ri.raw_text for ri in recipe.ingredients]
    res = _translate_fields(recipe.title, texts, recipe.instructions or "")
    if not res:
        return False
    if recipe.original_title is None:
        recipe.original_title = recipe.title
        recipe.original_instructions = recipe.instructions
    recipe.title = res["title"]
    if res["instructions"]:
        recipe.instructions = res["instructions"]
    for ri, new in zip(recipe.ingredients, res["ingredients"]):
        if ri.original_raw_text is None:
            ri.original_raw_text = ri.raw_text
        ri.raw_text = new
    db.commit()
    return True


# ---- hromadný zpětný překlad (na pozadí, s progresem) ----

_lock = threading.Lock()
_state: dict = {"running": False, "done": 0, "total": 0, "translated": 0, "finished_at": None}


def _set(**kw):
    with _lock:
        _state.update(kw)


def _inc(key: str, by: int = 1):
    with _lock:
        _state[key] = _state.get(key, 0) + by


def status() -> dict:
    with _lock:
        s = dict(_state)
    db = SessionLocal()
    try:
        s["recipes_total"] = db.scalar(select(func.count(Recipe.id))) or 0
        s["foreign_estimate"] = db.scalar(
            select(func.count(Recipe.id)).where(
                (Recipe.source_domain.is_(None)) | (~Recipe.source_domain.like("%.cz"))
            )
        ) or 0
    finally:
        db.close()
    return s


def _retranslate_one(recipe_id: int) -> bool:
    db = SessionLocal()
    try:
        r = db.scalar(
            select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
        )
        if r is None or not is_foreign(r):
            return False
        return retranslate_recipe(db, r)
    except Exception as exc:  # noqa: BLE001
        log.warning("retranslate recipe %s selhal: %s", recipe_id, exc)
        db.rollback()
        return False
    finally:
        db.close()


def retranslate_all() -> None:
    _set(running=True, done=0, total=0, translated=0, finished_at=None)
    db = SessionLocal()
    try:
        ids = [r.id for r in db.scalars(select(Recipe)).all() if is_foreign(r)]
    finally:
        db.close()
    _set(total=len(ids))
    workers = max(1, settings.bg_workers)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for ok in ex.map(_retranslate_one, ids):
                if ok:
                    _inc("translated")
                _inc("done")
    finally:
        _set(running=False, finished_at=time.time())


def retranslate_async() -> bool:
    with _lock:
        if _state["running"]:
            return False
    threading.Thread(target=retranslate_all, daemon=True).start()
    return True


# ---- reset a znovupřeložení (stáhne čerstvý originál ze zdroje) ----
#
# Překlad přepisuje text přímo (žádná kopie originálu se neukládá), takže
# "smazat překlad" samo o sobě nedá nic zpátky. Jediná čistá cesta je znovu
# stáhnout originál ze zdrojové URL a přeložit ho znovu – to už umí ingest
# pipeline, jen ji tady voláme zpětně pro už uložené recepty.

_reset_lock = threading.Lock()
_reset_state: dict = {"running": False, "done": 0, "total": 0, "reset": 0, "finished_at": None}


def _reset_set(**kw):
    with _reset_lock:
        _reset_state.update(kw)


def _reset_inc(key: str, by: int = 1):
    with _reset_lock:
        _reset_state[key] = _reset_state.get(key, 0) + by


def needs_reset(recipe: Recipe) -> bool:
    """Cizí doména, text vypadá česky, ale originál není uložený = starý
    překlad z doby před ukládáním originálu. Nové překlady originál mají
    vždy (viz translate_recipe), takže se sem po chvíli přestanou trefovat."""
    if recipe.original_title:
        return False
    dom = recipe.source_domain
    if not dom or dom.endswith(".cz"):
        return False
    if not recipe.source_url or recipe.source_url.startswith(("photo://", "ai://")):
        return False  # není odkud stáhnout originál
    return looks_czech(None, f"{recipe.title} {recipe.instructions or ''}")


def reset_status() -> dict:
    with _reset_lock:
        s = dict(_reset_state)
    db = SessionLocal()
    try:
        s["candidates"] = sum(1 for r in db.scalars(select(Recipe)).all() if needs_reset(r))
    finally:
        db.close()
    return s


def _reset_one(recipe_id: int) -> bool:
    from . import ingest  # lazy import – translate <-> ingest by se jinak kruhově importovaly

    db = SessionLocal()
    try:
        r = db.get(Recipe, recipe_id)
        if r is None or not needs_reset(r):
            return False
        fresh = ingest.ingest_url(db, r.source_url)
        return fresh is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("reset překladu receptu %s selhal: %s", recipe_id, exc)
        return False
    finally:
        db.close()


def reset_translations_all() -> None:
    _reset_set(running=True, done=0, total=0, reset=0, finished_at=None)
    db = SessionLocal()
    try:
        ids = [r.id for r in db.scalars(select(Recipe)).all() if needs_reset(r)]
    finally:
        db.close()
    _reset_set(total=len(ids))
    workers = max(1, settings.bg_workers)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for ok in ex.map(_reset_one, ids):
                if ok:
                    _reset_inc("reset")
                _reset_inc("done")
    finally:
        _reset_set(running=False, finished_at=time.time())


def reset_translations_async() -> bool:
    with _reset_lock:
        if _reset_state["running"]:
            return False
    threading.Thread(target=reset_translations_all, daemon=True).start()
    return True

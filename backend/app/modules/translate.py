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

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe

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
        r = httpx.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": settings.ollama_fast_model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "think": False,
                "keep_alive": settings.ollama_keep_alive,
                "options": {"temperature": 0},
            },
            timeout=max(settings.http_timeout, 120),
        )
        r.raise_for_status()
        out = json.loads(r.json()["response"])
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
    """Přelož recept (dict při importu) do češtiny, je-li cizí."""
    if not settings.translate_to_cs or not settings.ollama_enabled:
        return data
    probe = f"{data.get('title', '')} {data.get('instructions') or ''}"
    if looks_czech(data.get("source_domain"), probe):
        return data
    res = _translate_fields(
        data.get("title", ""), data.get("ingredients", []), data.get("instructions") or ""
    )
    if not res:
        return data
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
    """Přelož už uložený recept (titul, postup, raw_text ingrediencí)."""
    texts = [ri.raw_text for ri in recipe.ingredients]
    res = _translate_fields(recipe.title, texts, recipe.instructions or "")
    if not res:
        return False
    recipe.title = res["title"]
    if res["instructions"]:
        recipe.instructions = res["instructions"]
    for ri, new in zip(recipe.ingredients, res["ingredients"]):
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

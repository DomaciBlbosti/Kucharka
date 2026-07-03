"""Kategorizace surovin do hierarchie (např. 'maso > drůbeží > kuřecí').

Dávkově (víc surovin v jednom dotazu) a paralelně přes rychlý model.
Cesta se ukládá na surovinu (ingredient.category_path) – běží tedy jen jednou
pro nezkategorizované suroviny. Slouží k snadnějšímu hledání a filtrování.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, or_, select

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient
from .ollamachat import chat_json

log = logging.getLogger("kucharka.categorize")

TOP = [
    "maso", "ryby a mořské plody", "mléčné výrobky", "vejce", "zelenina",
    "ovoce", "obiloviny a pečivo", "luštěniny", "ořechy a semínka",
    "tuky a oleje", "koření a bylinky", "sladidla", "nápoje", "ostatní",
]

_BATCH = 25
_lock = threading.Lock()
_state: dict = {"running": False, "done": 0, "total": 0, "finished_at": None}


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
        s["total_ingredients"] = db.scalar(select(func.count(Ingredient.id))) or 0
        s["uncategorized"] = db.scalar(
            select(func.count(Ingredient.id)).where(
                or_(Ingredient.category_path.is_(None), Ingredient.category_path == "")
            )
        ) or 0
    finally:
        db.close()
    return s


def _categorize_batch(pairs: list[tuple[int, str]]) -> None:
    """pairs = [(id, name)]; přiřadí category_path a uloží."""
    if not settings.ollama_enabled or not pairs:
        return
    listing = "\n".join(f"{i}. {name}" for i, (_id, name) in enumerate(pairs))
    prompt = (
        "Zařaď každou potravinu do hierarchické kategorie. Oddělovač úrovní je "
        "' > ', nejvýš 3 úrovně, první úroveň MUSÍ být jedna z: "
        f"{', '.join(TOP)}. Příklady: 'kuřecí prsa' → 'maso > drůbeží > kuřecí'; "
        "'cibule' → 'zelenina > cibulová'; 'hladká mouka' → 'obiloviny a pečivo > mouka'. "
        "Odpověz POUZE JSON {\"items\":[{\"i\":<index>,\"category_path\":\"...\"}]}.\n"
        f"Potraviny:\n{listing}"
    )
    out = chat_json(
        settings.ollama_url,
        settings.ollama_fast_model,
        prompt,
        keep_alive=settings.ollama_keep_alive,
        timeout=max(settings.http_timeout, 120),
    )
    if out is None:
        log.warning("kategorizace dávky selhala (volání modelu nebo parsování).")
        _inc("done", len(pairs))
        return
    items = out.get("items", [])

    paths: dict[int, str] = {}
    for it in items:
        try:
            idx = int(it.get("i"))
            path = str(it.get("category_path") or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if 0 <= idx < len(pairs) and path:
            first = path.split(">")[0].strip().lower()
            if first in TOP:
                paths[pairs[idx][0]] = path

    db = SessionLocal()
    try:
        for ing_id, path in paths.items():
            ing = db.get(Ingredient, ing_id)
            if ing:
                ing.category_path = path
                if not ing.category:
                    ing.category = path.split(">")[0].strip()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("uložení kategorií selhalo: %s", exc)
        db.rollback()
    finally:
        db.close()
    _inc("done", len(pairs))


def categorize_all(only_missing: bool = True) -> None:
    _set(running=True, done=0, total=0, finished_at=None)
    db = SessionLocal()
    try:
        stmt = select(Ingredient.id, Ingredient.name_cs)
        if only_missing:
            stmt = stmt.where(
                or_(Ingredient.category_path.is_(None), Ingredient.category_path == "")
            )
        rows = [(r[0], r[1]) for r in db.execute(stmt).all()]
    finally:
        db.close()
    _set(total=len(rows))
    batches = [rows[i : i + _BATCH] for i in range(0, len(rows), _BATCH)]
    workers = max(1, settings.bg_workers)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_categorize_batch, batches))
    finally:
        _set(running=False, finished_at=time.time())


def categorize_async(only_missing: bool = True) -> bool:
    with _lock:
        if _state["running"]:
            return False
    threading.Thread(target=categorize_all, args=(only_missing,), daemon=True).start()
    return True

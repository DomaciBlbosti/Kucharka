"""Automatické otagování receptů přes rychlý model.

Uzavřený slovník (viz seed/starter_tags.py) – model smí vybírat JEN z
předaného seznamu, ne vymýšlet nové. Recept může dostat víc tagů z různých
i stejných jmenných prostorů zároveň.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe, RecipeTag, Tag
from ..seed.starter_tags import NAMESPACE_LABELS
from .ollamachat import chat_json

log = logging.getLogger("kucharka.tagging")

_BATCH = 6
_lock = threading.Lock()
_state: dict = {"running": False, "done": 0, "total": 0, "tagged": 0, "finished_at": None}


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
        s["total_recipes"] = db.scalar(select(func.count(Recipe.id))) or 0
        tagged_ids = select(RecipeTag.recipe_id).distinct()
        s["untagged"] = db.scalar(
            select(func.count(Recipe.id)).where(~Recipe.id.in_(tagged_ids))
        ) or 0
    finally:
        db.close()
    return s


def _vocab_text(all_tags: list[Tag]) -> str:
    by_ns: dict[str, list[str]] = {}
    for t in all_tags:
        by_ns.setdefault(t.namespace, []).append(t.slug)
    lines = []
    for ns, slugs in by_ns.items():
        label = NAMESPACE_LABELS.get(ns, ns)
        lines.append(f"{ns} ({label}): {', '.join(slugs)}")
    return "\n".join(lines)


def _tag_batch(recipe_ids: list[int]) -> None:
    db = SessionLocal()
    try:
        recipes = db.scalars(
            select(Recipe)
            .where(Recipe.id.in_(recipe_ids))
            .options(selectinload(Recipe.ingredients))
        ).all()
        if not recipes:
            return
        all_tags = db.scalars(select(Tag)).all()
        valid = {f"{t.namespace}:{t.slug}": t.id for t in all_tags}
        vocab = _vocab_text(all_tags)

        items = []
        for i, r in enumerate(recipes):
            ings = ", ".join(
                (ri.ingredient.name_cs if ri.ingredient else ri.raw_text)
                for ri in r.ingredients[:8]
            )
            items.append(f"{i}. {r.title} – suroviny: {ings}")
        listing = "\n".join(items)

        prompt = (
            "Pro každý recept níže vyber VŠECHNY vhodné tagy VÝHRADNĚ ze "
            "seznamu (formát 'jmenny_prostor:slug'). Recept může mít víc tagů "
            "z různých i stejných prostorů. Nevymýšlej nové tagy, jen z "
            "nabídky. Odpověz POUZE JSON "
            '{"items":[{"i":<index>,"tags":["chod:hlavni-jidlo", ...]}]}.\n\n'
            f"Dostupné tagy:\n{vocab}\n\nRecepty:\n{listing}"
        )
        out = chat_json(
            settings.ollama_url,
            settings.ollama_fast_model,
            prompt,
            keep_alive=settings.ollama_keep_alive,
            timeout=max(settings.http_timeout, 120),
        )
        if out is None:
            log.warning("otagování dávky selhalo (volání modelu nebo parsování).")
            _inc("done", len(recipes))
            return

        for it in out.get("items", []):
            try:
                idx = int(it.get("i"))
            except Exception:  # noqa: BLE001
                continue
            if not (0 <= idx < len(recipes)):
                continue
            recipe = recipes[idx]
            tag_ids = {valid[key] for key in (it.get("tags") or []) if key in valid}
            existing = {
                tid
                for tid in db.scalars(
                    select(RecipeTag.tag_id).where(RecipeTag.recipe_id == recipe.id)
                ).all()
            }
            for tid in tag_ids - existing:
                db.add(RecipeTag(recipe_id=recipe.id, tag_id=tid))
            if tag_ids:
                _inc("tagged")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("otagování dávky selhalo: %s", exc)
        db.rollback()
    finally:
        db.close()
        _inc("done", len(recipe_ids))


def tag_all(only_missing: bool = True) -> None:
    _set(running=True, done=0, total=0, tagged=0, finished_at=None)
    db = SessionLocal()
    try:
        stmt = select(Recipe.id)
        if only_missing:
            tagged_ids = select(RecipeTag.recipe_id).distinct()
            stmt = stmt.where(~Recipe.id.in_(tagged_ids))
        ids = [r for r in db.scalars(stmt).all()]
    finally:
        db.close()
    _set(total=len(ids))
    batches = [ids[i : i + _BATCH] for i in range(0, len(ids), _BATCH)]
    workers = max(1, settings.bg_workers)
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(_tag_batch, batches))
    finally:
        _set(running=False, finished_at=time.time())


def tag_async(only_missing: bool = True) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
    threading.Thread(target=tag_all, args=(only_missing,), daemon=True).start()
    return True

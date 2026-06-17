"""Dopárování nenapárovaných ingrediencí u existujících receptů.

Řádky receptů, kde se původně nepodařilo napárovat surovinu (ingredient_id =
NULL), zkusíme znovu:
  Fáze 1 (bez LLM): regex název → fuzzy/alias match. Databáze surovin mezitím
                    narostla, takže spousta řádků se chytne hned.
  Fáze 2 (LLM):     zbylé řádky dávkově přeparsuje Ollama (čistší název), znovu
                    match, a co se nenajde, vytvoří jako novou surovinu.
Po úpravách se přepočítají kalorie dotčených receptů.
"""
from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Recipe, RecipeIngredient
from .normalizer import (
    create_ingredient_via_llm,
    match_ingredient,
    parse_line_regex,
    parse_lines_ollama,
)
from .nutrition import grams_for, kcal_for, recompute_recipe_kcal

log = logging.getLogger("kucharka.backfill")

_lock = threading.Lock()
_state: dict = {
    "running": False, "phase": None, "done": 0, "total": 0,
    "matched": 0, "created": 0, "finished_at": None,
}


def _set(**kw):
    with _lock:
        _state.update(kw)


def stats() -> dict:
    db = SessionLocal()
    try:
        total_rows = db.scalar(select(func.count(RecipeIngredient.id))) or 0
        unmatched_rows = db.scalar(
            select(func.count(RecipeIngredient.id)).where(
                RecipeIngredient.ingredient_id.is_(None)
            )
        ) or 0
        recipes_total = db.scalar(select(func.count(Recipe.id))) or 0
        recipes_unmatched = db.scalar(
            select(func.count(func.distinct(RecipeIngredient.recipe_id))).where(
                RecipeIngredient.ingredient_id.is_(None)
            )
        ) or 0
        return {
            "rows_total": total_rows,
            "rows_unmatched": unmatched_rows,
            "recipes_total": recipes_total,
            "recipes_unmatched": recipes_unmatched,
        }
    finally:
        db.close()


def status() -> dict:
    with _lock:
        s = dict(_state)
    s.update(stats())
    return s


def _update_row(db: Session, row: RecipeIngredient, ing, amount, unit) -> None:
    row.ingredient_id = ing.id
    if row.amount is None and amount is not None:
        row.amount = amount
    if not row.unit and unit:
        row.unit = unit
    row.grams = grams_for(row.amount, row.unit, ing)
    row.kcal = kcal_for(row.grams, ing)


def backfill(create_missing: bool = True, chunk: int = 20) -> dict:
    db = SessionLocal()
    affected: set[int] = set()
    matched = created = 0
    try:
        rows = db.scalars(
            select(RecipeIngredient).where(RecipeIngredient.ingredient_id.is_(None))
        ).all()
        _set(running=True, phase="fuzzy", done=0, total=len(rows),
             matched=0, created=0, finished_at=None)

        # --- Fáze 1: regex název → match (bez LLM) ---
        for i, row in enumerate(rows, 1):
            amount, unit, name = parse_line_regex(row.raw_text)
            ing = match_ingredient(db, name)
            if ing is not None:
                _update_row(db, row, ing, amount, unit)
                matched += 1
                affected.add(row.recipe_id)
            if i % 100 == 0:
                db.commit()
            _set(done=i, matched=matched)
        db.commit()

        # --- Fáze 2: LLM přeparsuje a doplní zbytek ---
        remaining = db.scalars(
            select(RecipeIngredient).where(RecipeIngredient.ingredient_id.is_(None))
        ).all()
        _set(phase="llm", done=0, total=len(remaining))
        done = 0
        for start in range(0, len(remaining), chunk):
            batch = remaining[start:start + chunk]
            parsed = parse_lines_ollama([r.raw_text for r in batch])
            for j, row in enumerate(batch):
                if parsed is not None and parsed[j][2]:
                    amount, unit, name = parsed[j]
                else:
                    amount, unit, name = parse_line_regex(row.raw_text)
                ing = match_ingredient(db, name)
                if ing is None and create_missing:
                    ing = create_ingredient_via_llm(db, name)
                    if ing is not None:
                        created += 1
                if ing is not None:
                    _update_row(db, row, ing, amount, unit)
                    matched += 1
                    affected.add(row.recipe_id)
                done += 1
                _set(done=done, matched=matched, created=created)
            db.commit()

        # --- přepočet kalorií dotčených receptů ---
        _set(phase="kcal")
        for rid in affected:
            recipe = db.get(Recipe, rid)
            if recipe is not None:
                recompute_recipe_kcal(recipe)
        db.commit()
        log.info("backfill hotovo: napárováno %s, nově vytvořeno %s, receptů %s",
                 matched, created, len(affected))
    finally:
        _set(running=False, phase=None, finished_at=time.time())
        db.close()
    return status()


def backfill_async(create_missing: bool = True) -> bool:
    with _lock:
        if _state["running"]:
            return False
    threading.Thread(
        target=backfill, kwargs={"create_missing": create_missing}, daemon=True
    ).start()
    return True

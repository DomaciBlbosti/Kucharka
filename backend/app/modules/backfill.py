"""Dopárování nenapárovaných ingrediencí u existujících receptů (škálovatelné).

Optimalizováno pro tisíce řádků:
  * seznam surovin + aliasy se načte JEDNOU do paměti (žádný SELECT na řádek),
  * Fáze 1 (bez LLM): regex název → alias/fuzzy match v paměti,
  * Fáze 2 (LLM): zbylé řádky dávkově přeparsuje Ollama, názvy se DEDUPLIKUJÍ a
    každá nová surovina se přes LLM vytvoří jen jednou (ostatní řádky se stejným
    názvem se pak napárují zdarma).
Po úpravách se přepočítají kalorie dotčených receptů.
"""
from __future__ import annotations

import logging
import threading
import time

from rapidfuzz import fuzz, process
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Ingredient, IngredientAlias, Recipe, RecipeIngredient
from .normalizer import (
    _clean_name,
    _norm,
    create_ingredient_via_llm,
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
        return {
            "rows_total": db.scalar(select(func.count(RecipeIngredient.id))) or 0,
            "rows_unmatched": db.scalar(
                select(func.count(RecipeIngredient.id)).where(
                    RecipeIngredient.ingredient_id.is_(None)
                )
            ) or 0,
            "recipes_total": db.scalar(select(func.count(Recipe.id))) or 0,
            "recipes_unmatched": db.scalar(
                select(func.count(func.distinct(RecipeIngredient.recipe_id))).where(
                    RecipeIngredient.ingredient_id.is_(None)
                )
            ) or 0,
            "ingredients_total": db.scalar(select(func.count(Ingredient.id))) or 0,
        }
    finally:
        db.close()


def status() -> dict:
    with _lock:
        s = dict(_state)
    s.update(stats())
    return s


class _Matcher:
    """In-memory matcher: alias dict + fuzzy. Žádné DB dotazy na řádek."""

    def __init__(self, db: Session):
        self.db = db
        self.choices: dict[int, str] = {}
        self.by_id: dict[int, Ingredient] = {}
        self.alias_map: dict[str, int] = {}
        self.pending_aliases: dict[str, int] = {}
        for ing in db.scalars(select(Ingredient)).all():
            self.choices[ing.id] = _norm(ing.name_cs)
            self.by_id[ing.id] = ing
        for alias, iid in db.execute(
            select(IngredientAlias.alias, IngredientAlias.ingredient_id)
        ).all():
            self.alias_map[alias] = iid

    def match(self, name: str) -> Ingredient | None:
        key = _clean_name(name)
        if not key:
            return None
        iid = self.alias_map.get(key)
        if iid is None:
            best = process.extractOne(
                key, self.choices, scorer=fuzz.token_set_ratio, score_cutoff=82
            )
            if not best:
                return None
            iid = best[2]
            self.alias_map[key] = iid
            self.pending_aliases[key] = iid
        return self.by_id.get(iid)

    def create(self, name: str) -> Ingredient | None:
        ing = create_ingredient_via_llm(self.db, name)
        if ing is not None:
            self.choices[ing.id] = _norm(ing.name_cs)
            self.by_id[ing.id] = ing
            self.alias_map[_clean_name(name)] = ing.id
        return ing

    def flush_aliases(self):
        for alias, iid in self.pending_aliases.items():
            self.db.add(IngredientAlias(alias=alias, ingredient_id=iid))
        self.pending_aliases.clear()


def _apply(db: Session, row: RecipeIngredient, ing: Ingredient, amount, unit):
    row.ingredient_id = ing.id
    if row.amount is None and amount is not None:
        row.amount = amount
    if not row.unit and unit:
        row.unit = unit
    row.grams = grams_for(row.amount, row.unit, ing)
    row.kcal = kcal_for(row.grams, ing)


def backfill(create_missing: bool = True, chunk: int = 30) -> dict:
    db = SessionLocal()
    affected: set[int] = set()
    matched = created = 0
    try:
        m = _Matcher(db)
        rows = db.scalars(
            select(RecipeIngredient).where(RecipeIngredient.ingredient_id.is_(None))
        ).all()
        _set(running=True, phase="fuzzy", done=0, total=len(rows),
             matched=0, created=0, finished_at=None)

        # --- Fáze 1: regex → match v paměti (bez LLM) ---
        for i, row in enumerate(rows, 1):
            amount, unit, name = parse_line_regex(row.raw_text)
            ing = m.match(name)
            if ing is not None:
                _apply(db, row, ing, amount, unit)
                matched += 1
                affected.add(row.recipe_id)
            if i % 500 == 0:
                m.flush_aliases()
                db.commit()
                _set(done=i, matched=matched)
        m.flush_aliases()
        db.commit()
        _set(done=len(rows), matched=matched)

        # --- Fáze 2: LLM parse + dedup názvů + create jednou ---
        remaining = db.scalars(
            select(RecipeIngredient).where(RecipeIngredient.ingredient_id.is_(None))
        ).all()
        _set(phase="llm", done=0, total=len(remaining))
        done = 0
        name_cache: dict[str, Ingredient | None] = {}
        for start in range(0, len(remaining), chunk):
            batch = remaining[start:start + chunk]
            parsed = parse_lines_ollama([r.raw_text for r in batch])
            for j, row in enumerate(batch):
                if parsed is not None and parsed[j][2]:
                    amount, unit, name = parsed[j]
                else:
                    amount, unit, name = parse_line_regex(row.raw_text)
                key = _clean_name(name)
                if key in name_cache:
                    ing = name_cache[key]
                else:
                    ing = m.match(name)
                    if ing is None and create_missing:
                        ing = m.create(name)
                        if ing is not None:
                            created += 1
                    name_cache[key] = ing
                if ing is not None:
                    _apply(db, row, ing, amount, unit)
                    matched += 1
                    affected.add(row.recipe_id)
                done += 1
            m.flush_aliases()
            db.commit()
            _set(done=done, matched=matched, created=created)

        # --- přepočet kalorií dotčených receptů ---
        _set(phase="kcal")
        for rid in affected:
            recipe = db.get(Recipe, rid)
            if recipe is not None:
                recompute_recipe_kcal(recipe)
        db.commit()
        log.info("backfill hotovo: napárováno %s, vytvořeno %s surovin, receptů %s",
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

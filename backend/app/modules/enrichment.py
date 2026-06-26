"""Enrichment worker (bez LLM).

Bere recepty se `enrichment_status='pending'` a doplňuje:
  - `ingredient_id` na řádcích `recipe_ingredient`
  - parsing `amount` a `unit` (regex, žádný LLM)
  - `grams` a `kcal` přes nutrition modul
  - `recipe.kcal_per_serving`

Postup matchingu per řádek:
  1. Slovník (`lookup.lookup_alias`) — pokud hit, použij.
  2. Fuzzy match na `ingredient.name_cs` (rapidfuzz) s prahem `min_fuzzy_score`.
     Pokud hit, vytvoř automaticky nový alias (`source='import'`, `verified=False`)
     a použij. Tím se slovník postupně naplňuje, opakované volání téhož raw_textu
     je už O(1) lookup.
  3. Miss → ingredient_id zůstává NULL, recept se po doběhnutí označí
     `enrichment_status='manual_review'`.

LLM batch matching pro `manual_review` recepty přijde v dalším kroku.

Worker volá `configure_enrichment()` v `scheduler.py`. Spouští se cyklicky,
`max_instances=1, coalesce=True`, jeden běh = `batch_size` receptů.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Ingredient, IngredientAlias, Recipe
from .lookup import make_lookup_key, lookup_alias
from .nutrition import (
    PIECE_GRAMS,
    UNIT_TO_G,
    UNIT_TO_ML,
    grams_for,
    kcal_for,
    recompute_recipe_kcal,
)

log = logging.getLogger("kucharka.enrichment")

# Default práh pro fuzzy match (0–100). 90 je dost přísné — false-positivy
# by byly horší než ručně doplnit alias.
DEFAULT_FUZZY_THRESHOLD = 90

_KNOWN_UNITS = set(UNIT_TO_G) | set(UNIT_TO_ML) | set(PIECE_GRAMS)
_NUM_RE = re.compile(r"(\d+[.,]?\d*)")
_FRACTION_VALUES = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1/3, "⅔": 2/3, "⅛": 0.125}


# ─── Pomocné parsování čísla a jednotky bez LLM ──────────────────────────────

def _parse_amount_unit(raw: str) -> tuple[float | None, str | None]:
    """Z volného textu vytáhne (amount, unit). Žádný LLM, jen regex."""
    if not raw:
        return None, None
    t = raw.strip()

    amount: float | None = None
    for sym, val in _FRACTION_VALUES.items():
        if sym in t:
            amount = val
            t = t.replace(sym, " ", 1).strip()
            break
    if amount is None:
        m = _NUM_RE.search(t)
        if m:
            try:
                amount = float(m.group(1).replace(",", "."))
                t = (t[: m.start()] + t[m.end():]).strip()
            except ValueError:
                pass

    unit: str | None = None
    tokens = t.split()
    if tokens:
        first = tokens[0].lower().strip(",.;")
        if first in _KNOWN_UNITS:
            unit = first
    return amount, unit


# ─── Hlavní logika ───────────────────────────────────────────────────────────

def _build_ingredient_index(db: Session) -> tuple[dict, list[str]]:
    """Načte všechny `ingredient` do paměti. Vrátí (name_lower → Ingredient) a list jmen."""
    ings = db.scalars(select(Ingredient)).all()
    by_name = {i.name_cs.lower(): i for i in ings if i.name_cs}
    return by_name, list(by_name.keys())


def enrich_recipe(
    db: Session,
    recipe: Recipe,
    *,
    ing_by_name: dict[str, Ingredient] | None = None,
    ing_names: list[str] | None = None,
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> dict:
    """Enrichmentuj jeden recept. Aktualizuje DB. Vrací statistiky."""
    if ing_by_name is None or ing_names is None:
        ing_by_name, ing_names = _build_ingredient_index(db)

    stats = {"hits_dict": 0, "hits_fuzzy": 0, "nonfood": 0, "missed": 0, "tags": 0}

    for ri in recipe.ingredients:
        if not ri.raw_text:
            continue

        amount, unit = _parse_amount_unit(ri.raw_text)
        ri.amount = amount
        ri.unit = unit

        # 1. Slovník
        alias = lookup_alias(db, ri.raw_text)
        if alias is not None:
            if alias.kind == "food" and alias.ingredient_id:
                ing = alias.ingredient
                ri.ingredient_id = ing.id
                ri.grams = grams_for(amount, unit, ing)
                ri.kcal = kcal_for(ri.grams, ing)
                stats["hits_dict"] += 1
            else:
                # non-food / equipment / packaging / unknown
                ri.ingredient_id = None
                ri.grams = None
                ri.kcal = None
                stats["nonfood"] += 1
            continue

        # 2. Fuzzy match
        key = make_lookup_key(ri.raw_text)
        matched = False
        if key and ing_names:
            best = process.extractOne(key, ing_names, scorer=fuzz.WRatio, score_cutoff=fuzzy_threshold)
            if best:
                matched_name, score, _idx = best
                ing = ing_by_name[matched_name]
                # Zapiš do slovníku pro příští volání
                db.add(IngredientAlias(
                    alias=key,
                    lookup_key=key,
                    ingredient_id=ing.id,
                    kind="food",
                    source="import",
                    confidence=score / 100.0,
                    verified=False,
                    last_seen_at=datetime.utcnow(),
                    hit_count=1,
                ))
                ri.ingredient_id = ing.id
                ri.grams = grams_for(amount, unit, ing)
                ri.kcal = kcal_for(ri.grams, ing)
                stats["hits_fuzzy"] += 1
                matched = True

        if not matched:
            ri.ingredient_id = None
            ri.grams = None
            ri.kcal = None
            stats["missed"] += 1

    recompute_recipe_kcal(recipe)

    # Total weight + kcal/100g — pro UI filtry "do 150 kcal/100g".
    total_g = sum((ri.grams or 0.0) for ri in recipe.ingredients) or None
    recipe.total_weight_g = total_g
    if total_g and recipe.kcal_per_serving and recipe.servings:
        # kcal_per_serving × porce = celkové kcal receptu; děleno gramy × 100 = na 100 g
        total_kcal = recipe.kcal_per_serving * recipe.servings
        recipe.kcal_per_100g = round(total_kcal / total_g * 100, 1)
    else:
        recipe.kcal_per_100g = None

    # Klasifikace do tagů (course/flavor/meal/technique/diet/cuisine).
    # Manuální tagy (source='manual') zůstanou nedotčené.
    try:
        from . import classifier
        tags = classifier.classify(db, recipe)
        classifier.apply_tags(db, recipe, tags)
        stats["tags"] = len(tags)
    except Exception as exc:  # noqa: BLE001
        log.warning("Klasifikace selhala pro recept %s: %s", recipe.id, exc)
        stats["tags"] = 0

    recipe.enrichment_attempts = (recipe.enrichment_attempts or 0) + 1
    recipe.last_enriched_at = datetime.utcnow()
    if stats["missed"] == 0:
        recipe.enrichment_status = "done"
        recipe.enrichment_error = None
    else:
        recipe.enrichment_status = "manual_review"
        recipe.enrichment_error = f"{stats['missed']} nepřiřazených řádek"
    return stats


def process_batch(batch_size: int | None = None) -> dict:
    """Hlavní vstup workeru. Načte N pending receptů, zpracuje, commitne per recept."""
    batch_size = batch_size or getattr(settings, "enrichment_batch_size", 20)
    db = SessionLocal()
    try:
        recipes = db.scalars(
            select(Recipe)
            .where(Recipe.enrichment_status == "pending")
            .order_by(Recipe.id)
            .limit(batch_size)
        ).all()
        if not recipes:
            return {"recipes": 0}

        ing_by_name, ing_names = _build_ingredient_index(db)
        total = {"recipes": 0, "hits_dict": 0, "hits_fuzzy": 0, "nonfood": 0, "missed": 0, "tags": 0, "errors": 0}
        for recipe in recipes:
            try:
                s = enrich_recipe(db, recipe, ing_by_name=ing_by_name, ing_names=ing_names)
                db.commit()
                total["recipes"] += 1
                for k in ("hits_dict", "hits_fuzzy", "nonfood", "missed"):
                    total[k] += s[k]
            except Exception as exc:  # noqa: BLE001
                log.warning("Enrichment recept %s selhal: %s", recipe.id, exc)
                db.rollback()
                # Označ recept jako failed, ne abychom ho zkoušeli pořád dokola
                try:
                    db.refresh(recipe)
                    recipe.enrichment_status = "failed"
                    recipe.enrichment_error = str(exc)[:500]
                    recipe.enrichment_attempts = (recipe.enrichment_attempts or 0) + 1
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
                total["errors"] += 1
        log.info("Enrichment batch: %s", total)
        return total
    finally:
        db.close()


# ─── Stav / status ───────────────────────────────────────────────────────────

def status() -> dict:
    db = SessionLocal()
    try:
        from sqlalchemy import func
        rows = db.execute(
            select(Recipe.enrichment_status, func.count(Recipe.id))
            .group_by(Recipe.enrichment_status)
        ).all()
        return {status: count for status, count in rows}
    finally:
        db.close()

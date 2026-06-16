"""Ingest pipeline: URL → recept v DB.

scrape → normalize každý řádek → dopočet gramů a kcal → upsert receptu.
Idempotentní podle source_url (re-scrape aktualizuje hodnocení).
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Recipe, RecipeIngredient
from . import scraper
from .normalizer import normalize_lines
from .nutrition import grams_for, kcal_for, recompute_recipe_kcal


def ingest_url(db: Session, url: str) -> Recipe | None:
    data = scraper.fetch_and_extract(url)
    if data is None:
        return None
    return _persist(db, data)


def _persist(db: Session, data: dict) -> Recipe:
    recipe = db.scalar(select(Recipe).where(Recipe.source_url == data["source_url"]))
    if recipe is None:
        recipe = Recipe(source_url=data["source_url"])
        db.add(recipe)

    recipe.title = data["title"]
    recipe.source_domain = data.get("source_domain")
    recipe.image_url = data.get("image_url")
    recipe.video_url = data.get("video_url")
    recipe.instructions = data.get("instructions")
    recipe.servings = data.get("servings")
    recipe.total_time = data.get("total_time")
    recipe.rating = data.get("rating")
    recipe.rating_count = data.get("rating_count")
    recipe.category = data.get("category")
    recipe.raw_json = json.dumps(data, ensure_ascii=False)

    # přepiš ingredience
    recipe.ingredients.clear()
    db.flush()

    lines = data.get("ingredients", [])
    for norm in normalize_lines(db, lines):
        ing = norm["ingredient"]
        grams = grams_for(norm["amount"], norm["unit"], ing)
        ri = RecipeIngredient(
            raw_text=norm["raw_text"][:400],
            ingredient_id=ing.id if ing else None,
            amount=norm["amount"],
            unit=norm["unit"],
            grams=grams,
            kcal=kcal_for(grams, ing),
        )
        recipe.ingredients.append(ri)

    recompute_recipe_kcal(recipe)
    db.commit()
    db.refresh(recipe)
    return recipe

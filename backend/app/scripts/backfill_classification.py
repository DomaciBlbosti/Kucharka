"""Jednorázový backfill: doplnit tagy + kcal_per_100g + total_weight_g pro recepty,
které už mají hotový enrichment (matchnuté ingredience), ale ještě nebyly
oklasifikovány.

Spuštění:
    docker exec ix-kucharka-app-1 python -m app.scripts.backfill_classification

Idempotentní — recepty s alespoň jedním `source='auto'` tagem se přeskakují
(už byly zpracované).
"""
from __future__ import annotations

import logging
import sys

from sqlalchemy import func, select, exists

from ..db import SessionLocal
from ..models import Recipe, RecipeTag
from ..modules import classifier

log = logging.getLogger("kucharka.backfill_cls")


def run() -> dict:
    db = SessionLocal()
    try:
        # Vyber recepty s alespoň jednou matchnutou ingrediencí, které ještě nemají auto tagy
        already_classified = (
            select(RecipeTag.recipe_id)
            .where(RecipeTag.source == "auto")
            .distinct()
            .scalar_subquery()
        )
        recipes = db.scalars(
            select(Recipe)
            .where(Recipe.id.not_in(already_classified))
            .order_by(Recipe.id)
        ).all()

        log.info("Backfill klasifikace: %s receptů ke zpracování", len(recipes))
        if not recipes:
            return {"recipes": 0, "tags_total": 0}

        tags_total = 0
        kcal_set = 0
        for r in recipes:
            try:
                tags = classifier.classify(db, r)
                added = classifier.apply_tags(db, r, tags)
                tags_total += added

                # Dopočet kcal/100g + total_weight_g
                total_g = sum((ri.grams or 0.0) for ri in r.ingredients) or None
                r.total_weight_g = total_g
                if total_g and r.kcal_per_serving and r.servings:
                    r.kcal_per_100g = round(
                        r.kcal_per_serving * r.servings / total_g * 100, 1
                    )
                    kcal_set += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("recept %s: %s", r.id, exc)

        db.commit()
        result = {
            "recipes": len(recipes),
            "tags_total": tags_total,
            "kcal_per_100g_set": kcal_set,
            "tags_per_recipe": round(tags_total / max(len(recipes), 1), 1),
        }
        log.info("Backfill hotov: %s", result)
        return result
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s [%(levelname)s] %(message)s")
    from ..main import init_db
    init_db()
    print(run())
    sys.exit(0)

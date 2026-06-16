"""Logika spíže – co mám doma, co recept potřebuje, co chybí."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PantryItem, Recipe


def pantry_ingredient_ids(db: Session) -> set[int]:
    return set(db.scalars(select(PantryItem.ingredient_id)).all())


def recipe_availability(recipe: Recipe, have: set[int]) -> dict:
    """Spočítej dostupnost receptu vůči spíži.

    Počítají se jen ingredience, které se podařilo napárovat na kanon
    (mají ingredient_id). Nenapárované řádky bereme jako 'neznámé'.
    """
    known = [ri for ri in recipe.ingredients if ri.ingredient_id is not None]
    total = len(known)
    have_ids = {ri.ingredient_id for ri in known if ri.ingredient_id in have}
    missing = [ri for ri in known if ri.ingredient_id not in have]
    unmatched = [ri for ri in recipe.ingredients if ri.ingredient_id is None]
    return {
        "total": total,
        "have": len(have_ids),
        "missing_count": len(missing),
        "missing": missing,
        "unmatched": unmatched,
        "ratio": (len(have_ids) / total) if total else 0.0,
    }

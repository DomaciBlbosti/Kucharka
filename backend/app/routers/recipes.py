"""API pro recepty – výpis s filtry vůči spíži + detail."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Recipe
from ..modules.pantry import pantry_ingredient_ids, recipe_availability
from ..schemas import RecipeCard, RecipeDetail

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


@router.get("", response_model=list[RecipeCard])
def list_recipes(
    db: Session = Depends(get_db),
    q: str | None = Query(None, description="hledání v názvu"),
    only_have: bool = Query(False, description="jen co můžu uvařit teď"),
    max_missing: int | None = Query(None, ge=0),
    max_kcal: float | None = Query(None, ge=0),
    max_time: int | None = Query(None, ge=0),
    min_rating: float | None = Query(None, ge=0, le=5),
    sort: str = Query("smart", pattern="^(smart|rating|time|kcal|newest)$"),
):
    stmt = select(Recipe).options(selectinload(Recipe.ingredients))
    if q:
        stmt = stmt.where(Recipe.title.ilike(f"%{q}%"))
    if max_kcal is not None:
        stmt = stmt.where(Recipe.kcal_per_serving <= max_kcal)
    if max_time is not None:
        stmt = stmt.where(Recipe.total_time <= max_time)
    if min_rating is not None:
        stmt = stmt.where(Recipe.rating >= min_rating)

    recipes = db.scalars(stmt).all()
    have = pantry_ingredient_ids(db)

    cards: list[RecipeCard] = []
    for r in recipes:
        av = recipe_availability(r, have)
        if only_have and av["missing_count"] > 0:
            continue
        if max_missing is not None and av["missing_count"] > max_missing:
            continue
        card = RecipeCard.model_validate(r)
        card.have = av["have"]
        card.total = av["total"]
        card.missing_count = av["missing_count"]
        card.ratio = round(av["ratio"], 3)
        cards.append(card)

    if sort == "rating":
        cards.sort(key=lambda c: (c.rating or 0), reverse=True)
    elif sort == "time":
        cards.sort(key=lambda c: (c.total_time or 9999))
    elif sort == "kcal":
        cards.sort(key=lambda c: (c.kcal_per_serving or 9e9))
    elif sort == "newest":
        cards.sort(key=lambda c: c.id, reverse=True)
    else:  # smart: nejmíň chybějících, pak nejlepší hodnocení
        cards.sort(key=lambda c: (c.missing_count, -(c.rating or 0)))
    return cards


@router.get("/{recipe_id}", response_model=RecipeDetail)
def get_recipe(recipe_id: int, db: Session = Depends(get_db)):
    r = db.scalar(
        select(Recipe)
        .where(Recipe.id == recipe_id)
        .options(selectinload(Recipe.ingredients))
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen")
    have = pantry_ingredient_ids(db)
    av = recipe_availability(r, have)
    detail = RecipeDetail.model_validate(r)
    detail.have = av["have"]
    detail.total = av["total"]
    detail.missing_count = av["missing_count"]
    detail.ratio = round(av["ratio"], 3)
    detail.missing_ingredient_ids = [ri.ingredient_id for ri in av["missing"]]
    return detail


@router.delete("/{recipe_id}", status_code=204)
def delete_recipe(recipe_id: int, db: Session = Depends(get_db)):
    r = db.get(Recipe, recipe_id)
    if r:
        db.delete(r)
        db.commit()

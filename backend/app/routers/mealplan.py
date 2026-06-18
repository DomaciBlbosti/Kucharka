"""Jídelníček – plánování receptů na dny, denní kcal a nákup z plánu."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..config import settings
from ..models import MealPlanEntry, Recipe, ShoppingItem
from ..modules import planner
from ..modules.pantry import pantry_ingredient_ids
from ..schemas import (
    ApplyRequest,
    MealPlanAdd,
    MealPlanEntryOut,
    MealPlanUpdate,
    PlanRange,
    SuggestRequest,
)

router = APIRouter(prefix="/api/mealplan", tags=["mealplan"])

_MEALS = {"snídaně", "svačina", "oběd", "večeře"}


def _to_out(e: MealPlanEntry) -> MealPlanEntryOut:
    kps = e.recipe.kcal_per_serving
    return MealPlanEntryOut(
        id=e.id,
        date=e.date,
        meal=e.meal,
        servings=e.servings,
        recipe_id=e.recipe_id,
        title=e.recipe.title,
        image_url=e.recipe.image_url,
        kcal_per_serving=kps,
        kcal=(kps * e.servings) if kps else None,
    )


@router.get("", response_model=list[MealPlanEntryOut])
def list_plan(
    start,
    days: int = Query(7, ge=1, le=31),
    db: Session = Depends(get_db),
):
    from datetime import date as _date

    start_d = _date.fromisoformat(str(start))
    end_d = start_d + timedelta(days=days - 1)
    entries = db.scalars(
        select(MealPlanEntry)
        .where(MealPlanEntry.date >= start_d, MealPlanEntry.date <= end_d)
        .options(selectinload(MealPlanEntry.recipe))
        .order_by(MealPlanEntry.date, MealPlanEntry.id)
    ).all()
    return [_to_out(e) for e in entries]


@router.post("", response_model=MealPlanEntryOut)
def add_entry(req: MealPlanAdd, db: Session = Depends(get_db)):
    recipe = db.scalar(
        select(Recipe).where(Recipe.id == req.recipe_id).options(selectinload(Recipe.ingredients))
    )
    if recipe is None:
        raise HTTPException(404, "Recept nenalezen.")
    meal = req.meal if req.meal in _MEALS else "oběd"
    e = MealPlanEntry(
        date=req.date, meal=meal, recipe_id=req.recipe_id, servings=max(1, req.servings)
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    e.recipe = recipe
    return _to_out(e)


@router.patch("/{entry_id}", response_model=MealPlanEntryOut)
def update_entry(entry_id: int, req: MealPlanUpdate, db: Session = Depends(get_db)):
    e = db.scalar(
        select(MealPlanEntry).where(MealPlanEntry.id == entry_id).options(selectinload(MealPlanEntry.recipe))
    )
    if e is None:
        raise HTTPException(404, "Položka nenalezena.")
    if req.date is not None:
        e.date = req.date
    if req.meal is not None and req.meal in _MEALS:
        e.meal = req.meal
    if req.servings is not None:
        e.servings = max(1, req.servings)
    db.commit()
    db.refresh(e)
    return _to_out(e)


@router.delete("/{entry_id}", status_code=204)
def delete_entry(entry_id: int, db: Session = Depends(get_db)):
    e = db.get(MealPlanEntry, entry_id)
    if e:
        db.delete(e)
        db.commit()


@router.post("/shopping")
def shopping_from_plan(req: PlanRange, db: Session = Depends(get_db)):
    """Z naplánovaných receptů v rozsahu přidá chybějící suroviny do nákupu (dedup)."""
    end_d = req.start + timedelta(days=req.days - 1)
    entries = db.scalars(
        select(MealPlanEntry)
        .where(MealPlanEntry.date >= req.start, MealPlanEntry.date <= end_d)
        .options(selectinload(MealPlanEntry.recipe).selectinload(Recipe.ingredients))
    ).all()
    have = pantry_ingredient_ids(db)
    # už nezaškrtnuté v nákupu
    in_cart = set(
        db.scalars(
            select(ShoppingItem.ingredient_id).where(ShoppingItem.checked == False)  # noqa: E712
        ).all()
    )
    added = 0
    seen: set[int] = set()
    for e in entries:
        for ri in e.recipe.ingredients:
            iid = ri.ingredient_id
            if iid is None or iid in have or iid in in_cart or iid in seen:
                continue
            db.add(ShoppingItem(label=ri.raw_text, ingredient_id=iid))
            seen.add(iid)
            added += 1
    db.commit()
    return {"added": added, "recipes": len(entries)}


@router.post("/suggest")
def suggest_plan(req: SuggestRequest, db: Session = Depends(get_db)):
    if not settings.ollama_enabled:
        return {"started": False, "status": planner.status(), "error": "Ollama není dostupná."}
    meals = [m for m in req.meals if m in _MEALS] or ["oběd"]
    started = planner.suggest_async(req.start, req.days, meals, req.daily_kcal, req.preferences)
    return {"started": started, "status": planner.status()}


@router.get("/suggest-status")
def suggest_status():
    s = planner.status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/apply")
def apply_plan(req: ApplyRequest, db: Session = Depends(get_db)):
    from datetime import timedelta as _td

    if req.replace_range:
        end_d = req.start + _td(days=req.days - 1)
        for e in db.scalars(
            select(MealPlanEntry).where(
                MealPlanEntry.date >= req.start, MealPlanEntry.date <= end_d
            )
        ).all():
            db.delete(e)
    added = 0
    for ent in req.entries:
        meal = ent.meal if ent.meal in _MEALS else "oběd"
        db.add(
            MealPlanEntry(
                date=ent.date, meal=meal, recipe_id=ent.recipe_id,
                servings=max(1, ent.servings),
            )
        )
        added += 1
    db.commit()
    return {"added": added}

"""API pro spíž a nákupní seznam."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..models import Ingredient, PantryItem, ShoppingItem
from ..schemas import (
    PantryAdd,
    PantryItemOut,
    ShoppingAdd,
    ShoppingItemOut,
)

router = APIRouter(prefix="/api", tags=["pantry"])


# ---- Spíž ----------------------------------------------------------------
@router.get("/pantry", response_model=list[PantryItemOut])
def list_pantry(db: Session = Depends(get_db)):
    stmt = (
        select(PantryItem)
        .options(selectinload(PantryItem.ingredient))
        .join(Ingredient)
        .order_by(Ingredient.name_cs)
    )
    return db.scalars(stmt).all()


@router.post("/pantry", response_model=PantryItemOut)
def add_pantry(item: PantryAdd, db: Session = Depends(get_db)):
    if db.get(Ingredient, item.ingredient_id) is None:
        raise HTTPException(404, "Surovina nenalezena")
    existing = db.scalar(
        select(PantryItem).where(PantryItem.ingredient_id == item.ingredient_id)
    )
    if existing:
        existing.amount = item.amount
        existing.unit = item.unit
        pantry = existing
    else:
        pantry = PantryItem(**item.model_dump())
        db.add(pantry)
    db.commit()
    db.refresh(pantry)
    return pantry


@router.delete("/pantry/{ingredient_id}", status_code=204)
def remove_pantry(ingredient_id: int, db: Session = Depends(get_db)):
    item = db.scalar(
        select(PantryItem).where(PantryItem.ingredient_id == ingredient_id)
    )
    if item:
        db.delete(item)
        db.commit()


# ---- Nákupní seznam ------------------------------------------------------
@router.get("/shopping", response_model=list[ShoppingItemOut])
def list_shopping(db: Session = Depends(get_db)):
    return db.scalars(select(ShoppingItem).order_by(ShoppingItem.checked)).all()


@router.post("/shopping", response_model=ShoppingItemOut)
def add_shopping(item: ShoppingAdd, db: Session = Depends(get_db)):
    s = ShoppingItem(label=item.label, ingredient_id=item.ingredient_id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@router.post("/shopping/from-recipe/{recipe_id}")
def add_missing_from_recipe(recipe_id: int, db: Session = Depends(get_db)):
    """Přidej chybějící suroviny receptu do nákupního seznamu."""
    from ..models import Recipe
    from ..modules.pantry import pantry_ingredient_ids, recipe_availability

    recipe = db.scalar(
        select(Recipe)
        .where(Recipe.id == recipe_id)
        .options(selectinload(Recipe.ingredients))
    )
    if recipe is None:
        raise HTTPException(404, "Recept nenalezen")
    have = pantry_ingredient_ids(db)
    av = recipe_availability(recipe, have)
    added = 0
    for ri in av["missing"]:
        label = ri.ingredient.name_cs if ri.ingredient else ri.raw_text
        exists = db.scalar(
            select(ShoppingItem).where(
                ShoppingItem.ingredient_id == ri.ingredient_id,
                ShoppingItem.checked == False,  # noqa: E712
            )
        )
        if exists is None:
            db.add(ShoppingItem(label=label, ingredient_id=ri.ingredient_id))
            added += 1
    db.commit()
    return {"added": added}


@router.patch("/shopping/{item_id}/toggle", response_model=ShoppingItemOut)
def toggle_shopping(item_id: int, db: Session = Depends(get_db)):
    s = db.get(ShoppingItem, item_id)
    if s is None:
        raise HTTPException(404, "Položka nenalezena")
    s.checked = not s.checked
    db.commit()
    db.refresh(s)
    return s


@router.delete("/shopping/{item_id}", status_code=204)
def remove_shopping(item_id: int, db: Session = Depends(get_db)):
    s = db.get(ShoppingItem, item_id)
    if s:
        db.delete(s)
        db.commit()

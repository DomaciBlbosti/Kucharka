"""HMI API pro kuchyňský displej / E-ink rámeček.

Samostatný, jednoduchý token (HMI_TOKEN, volitelný) místo běžného hesla
appky – displej v kuchyni se nepřihlašuje, jen fetchuje URL. Když token není
nastavený, endpointy jsou otevřené v rámci LAN (stejný kompromis jako u
ingest/core tokenu).

„Právě vařím" je jednoduchý singleton stav uložený v app_setting – appka ho
nastaví (POST), displej ho pravidelně čte (GET) a zobrazí velký recept.
"""
from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import get_db
from ..models import AppSetting, MealPlanEntry, Recipe, ShoppingItem

router = APIRouter(prefix="/api/hmi", tags=["hmi"])

_COOKING_KEY = "hmi_cooking_recipe_id"


def require_hmi(token: str | None = Query(default=None)):
    if settings.hmi_token and token != settings.hmi_token:
        raise HTTPException(401, "Neplatný token displeje.")
    return True


@router.get("/today")
def today(_: bool = Depends(require_hmi), db: Session = Depends(get_db)):
    d = _date.today()
    entries = db.scalars(
        select(MealPlanEntry)
        .where(MealPlanEntry.date == d)
        .options(selectinload(MealPlanEntry.recipe))
        .order_by(MealPlanEntry.id)
    ).all()
    order = {"snídaně": 0, "svačina": 1, "oběd": 2, "večeře": 3}
    entries = sorted(entries, key=lambda e: order.get(e.meal, 9))
    items = []
    total_kcal = 0.0
    for e in entries:
        kcal = (e.recipe.kcal_per_serving or 0) * e.servings if e.recipe.kcal_per_serving else None
        if kcal:
            total_kcal += kcal
        items.append({
            "meal": e.meal,
            "recipe_id": e.recipe_id,
            "title": e.recipe.title,
            "servings": e.servings,
            "kcal": kcal,
        })
    return {"date": d.isoformat(), "meals": items, "kcal_total": round(total_kcal) or None}


@router.get("/shopping")
def shopping(_: bool = Depends(require_hmi), db: Session = Depends(get_db)):
    items = db.scalars(
        select(ShoppingItem)
        .where(ShoppingItem.checked == False)  # noqa: E712
        .order_by(ShoppingItem.label)
    ).all()
    return {"items": [{"id": i.id, "label": i.label} for i in items]}


def _cooking_recipe_id(db: Session) -> int | None:
    row = db.get(AppSetting, _COOKING_KEY)
    if not row or not row.value:
        return None
    try:
        return int(row.value)
    except ValueError:
        return None


@router.get("/cooking")
def get_cooking(_: bool = Depends(require_hmi), db: Session = Depends(get_db)):
    rid = _cooking_recipe_id(db)
    if rid is None:
        return {"recipe": None}
    r = db.scalar(
        select(Recipe).where(Recipe.id == rid).options(selectinload(Recipe.ingredients))
    )
    if r is None:
        return {"recipe": None}
    steps = [s.strip() for s in (r.instructions or "").split("\n") if s.strip()]
    return {
        "recipe": {
            "id": r.id,
            "title": r.title,
            "servings": r.servings,
            "ingredients": [ri.raw_text for ri in r.ingredients],
            "steps": steps,
        }
    }


class SetCooking(BaseModel):
    recipe_id: int | None = None


@router.post("/cooking")
def set_cooking(req: SetCooking, db: Session = Depends(get_db), _: bool = Depends(require_hmi)):
    row = db.get(AppSetting, _COOKING_KEY)
    value = str(req.recipe_id) if req.recipe_id else ""
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=_COOKING_KEY, value=value))
    db.commit()
    return {"recipe_id": req.recipe_id}

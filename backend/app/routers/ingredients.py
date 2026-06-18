"""API pro suroviny – vyhledání v kanonické databázi."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Ingredient
from ..schemas import IngredientOut

router = APIRouter(prefix="/api/ingredients", tags=["ingredients"])


@router.get("", response_model=list[IngredientOut])
def list_ingredients(
    db: Session = Depends(get_db),
    q: str | None = Query(None),
    limit: int = Query(30, ge=1, le=200),
):
    stmt = select(Ingredient)
    if q:
        stmt = stmt.where(Ingredient.name_cs.ilike(f"%{q}%"))
    stmt = stmt.order_by(Ingredient.name_cs).limit(limit)
    return db.scalars(stmt).all()


@router.get("/count")
def count(db: Session = Depends(get_db)):
    return {"count": db.scalar(select(func.count(Ingredient.id)))}


@router.get("/categories")
def categories(db: Session = Depends(get_db)):
    """Seznam kategorií (1. a 2. úroveň) s počtem surovin – pro filtrování."""
    rows = db.scalars(
        select(Ingredient.category_path).where(Ingredient.category_path.isnot(None))
    ).all()
    counts: dict[str, int] = {}
    for path in rows:
        if not path:
            continue
        parts = [p.strip() for p in path.split(">") if p.strip()]
        keys = set()
        if parts:
            keys.add(parts[0])
        if len(parts) >= 2:
            keys.add(" > ".join(parts[:2]))
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    out = [{"category": k, "count": v} for k, v in counts.items()]
    out.sort(key=lambda x: (-x["count"], x["category"]))
    return out

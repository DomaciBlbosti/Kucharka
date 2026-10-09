"""API pro suroviny – vyhledání v kanonické databázi + procházení podle kategorií."""
from __future__ import annotations

import time
from threading import Lock

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Ingredient, RecipeIngredient
from ..schemas import IngredientOut

router = APIRouter(prefix="/api/ingredients", tags=["ingredients"])

# Počet receptů na surovinu – GROUP BY nad recipe_ingredient je nad velkým
# korpusem drahý, proto cache v procesu (TTL 15 min).
_USAGE_TTL = 15 * 60
_usage_cache: dict[int, int] = {}
_usage_ts = 0.0
_usage_lock = Lock()


def _usage(db: Session) -> dict[int, int]:
    global _usage_cache, _usage_ts
    now = time.time()
    if _usage_cache and now - _usage_ts < _USAGE_TTL:
        return _usage_cache
    with _usage_lock:
        if _usage_cache and time.time() - _usage_ts < _USAGE_TTL:
            return _usage_cache
        rows = db.execute(
            select(RecipeIngredient.ingredient_id, func.count(func.distinct(RecipeIngredient.recipe_id)))
            .where(RecipeIngredient.ingredient_id.isnot(None))
            .group_by(RecipeIngredient.ingredient_id)
        ).all()
        _usage_cache = {iid: n for iid, n in rows}
        _usage_ts = time.time()
        return _usage_cache


def _split(path: str | None) -> list[str]:
    if not path:
        return []
    return [p.strip() for p in path.split(">") if p.strip()]


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
        parts = _split(path)
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


@router.get("/categories/tree")
def categories_tree(db: Session = Depends(get_db)):
    """Strom kategorií (1. → 2. úroveň) vážený počtem receptů, které suroviny
    z kategorie používají. Pro menu výběru surovin."""
    usage = _usage(db)
    rows = db.execute(
        select(Ingredient.id, Ingredient.category_path).where(Ingredient.category_path.isnot(None))
    ).all()
    tree: dict[str, dict] = {}
    for iid, path in rows:
        parts = _split(path)
        if not parts:
            continue
        u = usage.get(iid, 0)
        node = tree.setdefault(parts[0], {"name": parts[0], "n_ingredients": 0, "n_recipes": 0, "children": {}})
        node["n_ingredients"] += 1
        node["n_recipes"] += u
        if len(parts) >= 2:
            ch = node["children"].setdefault(parts[1], {"name": parts[1], "n_ingredients": 0, "n_recipes": 0})
            ch["n_ingredients"] += 1
            ch["n_recipes"] += u
    out = []
    for node in tree.values():
        children = sorted(node["children"].values(), key=lambda c: (-c["n_recipes"], c["name"]))
        out.append({**node, "children": children})
    out.sort(key=lambda c: (-c["n_recipes"], c["name"]))
    return out


@router.get("/by-category")
def by_category(
    category: str = Query(..., description="prefix category_path, např. 'maso' nebo 'maso > drůbeží'"),
    limit: int = Query(60, ge=1, le=300),
    q: str | None = Query(None, description="volitelné zúžení podle názvu"),
    db: Session = Depends(get_db),
):
    """Nejpoužívanější suroviny v kategorii (podle počtu receptů) – pro menu výběru."""
    usage = _usage(db)
    stmt = select(Ingredient.id, Ingredient.name_cs, Ingredient.kcal_100g, Ingredient.category_path).where(
        Ingredient.category_path.ilike(f"{category}%")
    )
    if q:
        stmt = stmt.where(Ingredient.name_cs.ilike(f"%{q}%"))
    rows = db.execute(stmt).all()
    items = [
        {"id": iid, "name_cs": name, "kcal_100g": kcal, "category_path": path, "use_count": usage.get(iid, 0)}
        for iid, name, kcal, path in rows
    ]
    items.sort(key=lambda x: (-x["use_count"], x["name_cs"]))
    return {"total": len(items), "items": items[:limit]}

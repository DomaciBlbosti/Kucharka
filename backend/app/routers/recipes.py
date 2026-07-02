"""API pro recepty – výpis s filtry vůči spíži + detail."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import get_db
from ..models import Ingredient, PantryItem, Recipe, RecipeIngredient, RecipeTag, Tag
from ..modules.pantry import pantry_ingredient_ids, recipe_availability
from ..modules.nutrition import recompute_recipe_kcal
from ..modules import photo_recipe
from ..modules.ingest import persist as persist_recipe
from ..seed.starter_tags import NAMESPACE_LABELS
from ..schemas import RecipeCard, RecipeDetail, RecipeEdit

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
    category: str | None = Query(None, description="recepty se surovinou z kategorie"),
    tags: list[str] = Query(default=[], description="filtr 'namespace:slug' – víc namespace = AND, víc tagů v jednom = OR"),
    sort: str = Query("smart", pattern="^(smart|rating|time|kcal|newest)$"),
):
    stmt = select(Recipe).options(selectinload(Recipe.ingredients), selectinload(Recipe.tags))
    if q:
        stmt = stmt.where(Recipe.title.ilike(f"%{q}%"))
    if max_kcal is not None:
        stmt = stmt.where(Recipe.kcal_per_serving <= max_kcal)
    if max_time is not None:
        stmt = stmt.where(Recipe.total_time <= max_time)
    if min_rating is not None:
        stmt = stmt.where(Recipe.rating >= min_rating)
    if category:
        sub = (
            select(RecipeIngredient.recipe_id)
            .join(Ingredient, RecipeIngredient.ingredient_id == Ingredient.id)
            .where(Ingredient.category_path.ilike(f"{category}%"))
        )
        stmt = stmt.where(Recipe.id.in_(sub))
    if tags:
        by_ns: dict[str, list[str]] = {}
        for t in tags:
            if ":" not in t:
                continue
            ns, slug = t.split(":", 1)
            by_ns.setdefault(ns, []).append(slug)
        for ns, slugs in by_ns.items():
            sub = (
                select(RecipeTag.recipe_id)
                .join(Tag, RecipeTag.tag_id == Tag.id)
                .where(Tag.namespace == ns, Tag.slug.in_(slugs))
            )
            stmt = stmt.where(Recipe.id.in_(sub))

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


@router.get("/tags")
def list_tags(db: Session = Depends(get_db)):
    """Kanonické tagy seskupené podle jmenného prostoru, s počtem receptů – pro filtr."""
    all_tags = db.scalars(select(Tag)).all()
    counts = dict(
        db.execute(select(RecipeTag.tag_id, func.count()).group_by(RecipeTag.tag_id)).all()
    )
    by_ns: dict[str, list[dict]] = {}
    for t in all_tags:
        by_ns.setdefault(t.namespace, []).append(
            {"slug": t.slug, "label": t.label_cs, "count": counts.get(t.id, 0)}
        )
    return [
        {
            "namespace": ns,
            "label": NAMESPACE_LABELS.get(ns, ns),
            "tags": sorted(items, key=lambda x: x["label"]),
        }
        for ns, items in sorted(by_ns.items())
    ]


class TagsSet(BaseModel):
    tags: list[str]  # "namespace:slug"


@router.put("/{recipe_id}/tags", response_model=RecipeDetail)
def set_recipe_tags(recipe_id: int, req: TagsSet, db: Session = Depends(get_db)):
    r = db.scalar(
        select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.tags))
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    all_tags = {f"{t.namespace}:{t.slug}": t for t in db.scalars(select(Tag)).all()}
    r.tags = [all_tags[key] for key in req.tags if key in all_tags]
    db.commit()
    return get_recipe(recipe_id, db)


@router.get("/cook-from", response_model=list[RecipeCard])
def cook_from(
    ingredient_ids: list[int] = Query(default=[], description="suroviny, ze kterých chci vařit"),
    limit: int = Query(60, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Recepty, které využijí vybrané suroviny – seřazené podle nejmenšího doplnění.

    Skóre se počítá stejně jako dostupnost vůči spíži, jen místo spíže
    bereme vybrané suroviny: have = kolik klíčových surovin receptu pokrývá
    výběr, missing_count = kolik by ještě bylo třeba dokoupit.
    """
    if not ingredient_ids:
        return []
    sel = set(ingredient_ids)
    recipes = db.scalars(
        select(Recipe).options(selectinload(Recipe.ingredients), selectinload(Recipe.tags))
    ).all()
    cards: list[RecipeCard] = []
    for r in recipes:
        av = recipe_availability(r, sel)
        if av["total"] == 0 or av["have"] == 0:
            continue  # recept nevyužívá žádnou z vybraných surovin
        card = RecipeCard.model_validate(r)
        card.have = av["have"]
        card.total = av["total"]
        card.missing_count = av["missing_count"]
        card.ratio = round(av["ratio"], 3)
        cards.append(card)
    cards.sort(key=lambda c: (c.missing_count, -c.have, -(c.rating or 0)))
    return cards[:limit]


class PhotoRecipeSave(BaseModel):
    title: str
    instructions: str | None = None
    servings: int | None = None
    ingredients: list[str]


@router.post("/from-photo")
async def recipe_from_photo(images: list[UploadFile] = File(...)):
    """Náhled receptu vyfoceného po úsecích – jen extrahuje, neukládá."""
    if not settings.ocr_model:
        raise HTTPException(
            400,
            "OCR model není nastaven. Nastav ho v Admin → Nástroje → OCR model "
            "(vision model stažený v Ollamě, např. qwen2.5vl nebo minicpm-v).",
        )
    if not images:
        raise HTTPException(400, "Nahraj alespoň jednu fotku receptu.")
    raw = [await f.read() for f in images]
    try:
        return photo_recipe.extract_draft(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Čtení receptu selhalo: {exc}") from None


@router.post("/from-photo/save", response_model=RecipeDetail)
def save_photo_recipe(req: PhotoRecipeSave, db: Session = Depends(get_db)):
    title = req.title.strip()
    ingredients = [i.strip() for i in req.ingredients if i.strip()]
    if not title or not ingredients:
        raise HTTPException(400, "Recept musí mít název a alespoň jednu surovinu.")
    data = {
        "title": title,
        "source_url": f"photo://{uuid.uuid4()}",
        "source_domain": None,
        "image_url": None,
        "instructions": req.instructions,
        "servings": req.servings,
        "ingredients": ingredients,
    }
    recipe = persist_recipe(db, data)
    return get_recipe(recipe.id, db)


@router.get("/{recipe_id}", response_model=RecipeDetail)
def get_recipe(recipe_id: int, db: Session = Depends(get_db)):
    r = db.scalar(
        select(Recipe)
        .where(Recipe.id == recipe_id)
        .options(selectinload(Recipe.ingredients), selectinload(Recipe.tags))
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


@router.patch("/{recipe_id}", response_model=RecipeDetail)
def edit_recipe(recipe_id: int, req: RecipeEdit, db: Session = Depends(get_db)):
    r = db.scalar(
        select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    if req.title is not None:
        r.title = req.title.strip() or r.title
    if req.instructions is not None:
        r.instructions = req.instructions
    if req.servings is not None:
        r.servings = max(1, req.servings)
    if req.image_url is not None:
        r.image_url = req.image_url.strip() or None
    if req.user_rating is not None:
        r.user_rating = max(0, min(5, req.user_rating)) or None
    if req.user_note is not None:
        r.user_note = req.user_note.strip() or None
    if req.ingredient_texts is not None and len(req.ingredient_texts) == len(r.ingredients):
        for ri, txt in zip(r.ingredients, req.ingredient_texts):
            ri.raw_text = txt.strip()
    if req.servings is not None:
        recompute_recipe_kcal(r)
    db.commit()
    return get_recipe(recipe_id, db)


@router.post("/{recipe_id}/cooked")
def mark_cooked(recipe_id: int, db: Session = Depends(get_db)):
    """Uvařeno – odečte suroviny receptu ze spíže (které tam jsou)."""
    r = db.scalar(
        select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients))
    )
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    used_ids = {ri.ingredient_id for ri in r.ingredients if ri.ingredient_id}
    removed = 0
    for item in db.scalars(
        select(PantryItem).where(PantryItem.ingredient_id.in_(used_ids))
    ).all():
        db.delete(item)
        removed += 1
    db.commit()
    return {"removed": removed}


@router.delete("/{recipe_id}", status_code=204)
def delete_recipe(recipe_id: int, db: Session = Depends(get_db)):
    r = db.get(Recipe, recipe_id)
    if r:
        db.delete(r)
        db.commit()

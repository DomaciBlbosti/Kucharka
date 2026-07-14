"""API pro recepty – výpis s filtry vůči spíži + detail."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import case, func, literal, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import get_db
from ..models import Ingredient, PantryItem, Recipe, RecipeIngredient, RecipeTag, Tag
from ..modules.pantry import pantry_ingredient_ids, recipe_availability
from ..modules.nutrition import recompute_recipe_kcal
from ..modules import photo_recipe
from ..modules.ingest import persist as persist_recipe
from ..seed.starter_tags import NAMESPACE_LABELS
from ..schemas import RecipeCard, RecipeDetail, RecipeEdit, RecipeListOut

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


def _availability_cols(have_ids: set[int]):
    """Spočítej 'total'/'have' dostupnosti PŘÍMO V SQL (agregace přes
    recipe_ingredient), místo abychom kvůli dvěma číslům tahali do Pythonu
    kompletní seznam ingrediencí každého receptu v celé DB. U 150k+ receptů
    byl tohle hlavní důvod, proč se hlavní stránka načítala pomalu.

    Vrací (subquery, total_col, have_col, missing_col) – total_col/have_col/
    missing_col jsou SQL výrazy použitelné ve WHERE i ORDER BY, takže filtrování
    ('jen co můžu uvařit', 'max chybí') i řazení ('smart') jde udělat v DB a
    LIMIT/OFFSET pak opravdu stránkuje, ne až post-hoc v Pythonu.
    """
    total_expr = func.sum(case((RecipeIngredient.ingredient_id.isnot(None), 1), else_=0))
    have_expr = (
        func.sum(case((RecipeIngredient.ingredient_id.in_(have_ids), 1), else_=0))
        if have_ids else literal(0)
    )
    sub = (
        select(
            RecipeIngredient.recipe_id.label("recipe_id"),
            total_expr.label("total"),
            have_expr.label("have"),
        )
        .group_by(RecipeIngredient.recipe_id)
        .subquery()
    )
    total_col = func.coalesce(sub.c.total, 0)
    have_col = func.coalesce(sub.c.have, 0)
    missing_col = total_col - have_col
    return sub, total_col, have_col, missing_col


def _tags_by_recipe(db: Session, recipe_ids: list[int]) -> dict[int, list[Tag]]:
    """Tagy jen pro danou stránku receptů (ne pro celou DB)."""
    if not recipe_ids:
        return {}
    rows = db.execute(
        select(RecipeTag.recipe_id, Tag)
        .join(Tag, RecipeTag.tag_id == Tag.id)
        .where(RecipeTag.recipe_id.in_(recipe_ids))
    ).all()
    out: dict[int, list[Tag]] = {}
    for rid, tag in rows:
        out.setdefault(rid, []).append(tag)
    return out


@router.get("", response_model=RecipeListOut)
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
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    have = pantry_ingredient_ids(db)
    _sub, total_col, have_col, missing_col = _availability_cols(have)

    base = select(Recipe, total_col.label("total"), have_col.label("have"), missing_col.label("missing_count")).outerjoin(
        _sub, _sub.c.recipe_id == Recipe.id
    )
    if q:
        base = base.where(Recipe.title.ilike(f"%{q}%"))
    if max_kcal is not None:
        base = base.where(Recipe.kcal_per_serving <= max_kcal)
    if max_time is not None:
        base = base.where(Recipe.total_time <= max_time)
    if min_rating is not None:
        base = base.where(Recipe.rating >= min_rating)
    if category:
        sub = (
            select(RecipeIngredient.recipe_id)
            .join(Ingredient, RecipeIngredient.ingredient_id == Ingredient.id)
            .where(Ingredient.category_path.ilike(f"{category}%"))
        )
        base = base.where(Recipe.id.in_(sub))
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
            base = base.where(Recipe.id.in_(sub))
    if only_have:
        base = base.where(missing_col == 0)
    if max_missing is not None:
        base = base.where(missing_col <= max_missing)

    # Celkový počet (stejné filtry, bez řazení/limitu) – pro "Načíst další" v UI.
    total_count = db.scalar(select(func.count()).select_from(base.order_by(None).subquery())) or 0

    if sort == "rating":
        base = base.order_by(func.coalesce(Recipe.rating, 0).desc())
    elif sort == "time":
        base = base.order_by(func.coalesce(Recipe.total_time, 9999).asc())
    elif sort == "kcal":
        base = base.order_by(func.coalesce(Recipe.kcal_per_serving, 1_000_000_000).asc())
    elif sort == "newest":
        base = base.order_by(Recipe.id.desc())
    else:  # smart: nejmíň chybějících, pak nejlepší hodnocení
        base = base.order_by(missing_col.asc(), func.coalesce(Recipe.rating, 0).desc())

    rows = db.execute(base.limit(limit).offset(offset)).all()
    recipe_ids = [r.Recipe.id for r in rows]
    tags_map = _tags_by_recipe(db, recipe_ids)

    items = []
    for r in rows:
        recipe = r.Recipe
        items.append(
            RecipeCard(
                id=recipe.id,
                title=recipe.title,
                source_domain=recipe.source_domain,
                image_url=recipe.image_url,
                servings=recipe.servings,
                total_time=recipe.total_time,
                rating=recipe.rating,
                rating_count=recipe.rating_count,
                kcal_per_serving=recipe.kcal_per_serving,
                tags=tags_map.get(recipe.id, []),
                have=r.have,
                total=r.total,
                missing_count=r.missing_count,
                ratio=round(r.have / r.total, 3) if r.total else 0.0,
            )
        )
    return RecipeListOut(items=items, total=total_count, limit=limit, offset=offset)


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

    # Nejdřív v SQL zúžit na recepty, co aspoň JEDNU vybranou surovinu vůbec
    # obsahují – dřív se tahalo úplně všech ~150k receptů se všemi
    # ingrediencemi jen proto, aby se pak 99 % z nich v Pythonu zahodilo.
    candidate_ids = select(RecipeIngredient.recipe_id).where(
        RecipeIngredient.ingredient_id.in_(sel)
    ).distinct()

    _sub, total_col, have_col, missing_col = _availability_cols(sel)
    stmt = (
        select(Recipe, total_col.label("total"), have_col.label("have"), missing_col.label("missing_count"))
        .outerjoin(_sub, _sub.c.recipe_id == Recipe.id)
        .where(Recipe.id.in_(candidate_ids))
        .where(have_col > 0)
        .order_by(missing_col.asc(), have_col.desc(), func.coalesce(Recipe.rating, 0).desc())
        .limit(limit)
    )
    rows = db.execute(stmt).all()
    recipe_ids = [r.Recipe.id for r in rows]
    tags_map = _tags_by_recipe(db, recipe_ids)

    cards: list[RecipeCard] = []
    for r in rows:
        recipe = r.Recipe
        cards.append(
            RecipeCard(
                id=recipe.id,
                title=recipe.title,
                source_domain=recipe.source_domain,
                image_url=recipe.image_url,
                servings=recipe.servings,
                total_time=recipe.total_time,
                rating=recipe.rating,
                rating_count=recipe.rating_count,
                kcal_per_serving=recipe.kcal_per_serving,
                tags=tags_map.get(recipe.id, []),
                have=r.have,
                total=r.total,
                missing_count=r.missing_count,
                ratio=round(r.have / r.total, 3) if r.total else 0.0,
            )
        )
    return cards


class PhotoRecipeSave(BaseModel):
    title: str
    instructions: str | None = None
    servings: int | None = None
    ingredients: list[str]
    image_url: str | None = None


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
        "image_url": req.image_url,
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


@router.post("/{recipe_id}/retranslate", response_model=RecipeDetail)
def retranslate_one(recipe_id: int, db: Session = Depends(get_db)):
    """Znovu stáhni originál ze zdroje a přelož čerstvě (přepíše starý překlad)."""
    from ..modules import ingest

    r = db.get(Recipe, recipe_id)
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    if not r.source_url or r.source_url.startswith(("photo://", "ai://")):
        raise HTTPException(
            400, "Tento recept nemá externí zdroj – originál se nedá znovu stáhnout."
        )
    if not settings.ollama_enabled:
        raise HTTPException(400, "Ollama není dostupná.")
    fresh = ingest.ingest_url(db, r.source_url)
    if fresh is None:
        raise HTTPException(502, "Stažení nebo zpracování zdrojové stránky selhalo.")
    return get_recipe(fresh.id, db)


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

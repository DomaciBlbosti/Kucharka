"""Kontrakt mezi JÁDREM (core) a WEBOVOU APPKOU (library).

Jádro (scraping, překlad, kategorizace, LLM párování, embeddingy) nesahá na DB
přímo – veškerou práci posílá sem přes HTTP. Web je „hloupá knihovna": uloží,
vyřeší napárování surovin na kanon a servíruje. Díky tomu může web časem běžet
i jinde (klidně přepsaný do PHP) a jádro se na něj jen připojí přes WEB_API_URL.

Zabezpečeno hlavičkou X-Core-Token (musí odpovídat settings.core_token; když
token není nastaven, projde i bez něj – vhodné pro běh na stejném serveru).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..db import get_db
from ..models import Ingredient, IngredientAlias, Recipe, RecipeIngredient
from ..modules.normalizer import _norm, match_ingredient
from ..modules.nutrition import recompute_recipe_kcal

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


def require_core(x_core_token: str | None = Header(default=None)):
    if settings.core_token and x_core_token != settings.core_token:
        raise HTTPException(401, "Neplatný core token.")
    return True


# ---------- schémata kontraktu ----------

class IngestIngredient(BaseModel):
    raw_text: str
    ingredient_name: str | None = None  # kanonický název (jádro už napárovalo)
    amount: float | None = None
    unit: str | None = None
    grams: float | None = None
    optional: bool = False


class IngestRecipe(BaseModel):
    title: str
    source_url: str
    source_domain: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    instructions: str | None = None
    servings: int | None = None
    total_time: int | None = None
    rating: float | None = None
    rating_count: int | None = None
    ingredients: list[IngestIngredient] = []


class IngredientPatch(BaseModel):
    category_path: str | None = None
    kcal_100g: float | None = None


class RecipePatch(BaseModel):
    title: str | None = None
    instructions: str | None = None
    ingredient_texts: list[str] | None = None  # v pořadí řádků (překlad raw_text)


# ---------- endpointy ----------

@router.post("/filter-new")
def filter_new(urls: list[str], _: bool = Depends(require_core), db: Session = Depends(get_db)):
    """Z předaných URL vrátí ty, které v DB ještě nejsou (dedup pro crawler jádra)."""
    if not urls:
        return {"new": []}
    known = set(
        db.scalars(select(Recipe.source_url).where(Recipe.source_url.in_(urls))).all()
    )
    return {"new": [u for u in urls if u not in known]}


@router.get("/ping")
def ping(_: bool = Depends(require_core)):
    return {"ok": True, "app": "kucharka-web"}


@router.get("/dictionary")
def dictionary(_: bool = Depends(require_core), db: Session = Depends(get_db)):
    """Kanonické suroviny + aliasy + kategorie – jádro podle nich páruje."""
    aliases: dict[int, list[str]] = {}
    for alias, iid in db.execute(
        select(IngredientAlias.alias, IngredientAlias.ingredient_id)
    ).all():
        aliases.setdefault(iid, []).append(alias)
    out = []
    for ing in db.scalars(select(Ingredient)).all():
        out.append({
            "id": ing.id,
            "name_cs": ing.name_cs,
            "category_path": ing.category_path,
            "aliases": aliases.get(ing.id, []),
        })
    return {"ingredients": out, "count": len(out)}


def _resolve_ingredient(db: Session, name: str | None) -> Ingredient | None:
    if not name or not name.strip():
        return None
    name = name.strip()
    ing = match_ingredient(db, name)  # alias/název/fuzzy
    if ing:
        return ing
    ing = Ingredient(name_cs=name, source="core")
    db.add(ing)
    db.flush()
    key = _norm(name)[:200]
    if key and not db.scalar(select(IngredientAlias).where(IngredientAlias.alias == key)):
        db.add(IngredientAlias(alias=key, ingredient_id=ing.id))
    return ing


@router.post("/recipe")
def upsert_recipe(req: IngestRecipe, _: bool = Depends(require_core), db: Session = Depends(get_db)):
    """Idempotentní vložení/aktualizace zpracovaného receptu (dedup dle source_url)."""
    recipe = db.scalar(select(Recipe).where(Recipe.source_url == req.source_url))
    created = recipe is None
    if recipe is None:
        recipe = Recipe(source_url=req.source_url)
        db.add(recipe)

    recipe.title = req.title
    recipe.source_domain = req.source_domain
    recipe.image_url = req.image_url
    recipe.video_url = req.video_url
    recipe.instructions = req.instructions
    recipe.servings = req.servings
    recipe.total_time = req.total_time
    recipe.rating = req.rating
    recipe.rating_count = req.rating_count

    # přepiš řádky surovin
    recipe.ingredients.clear()
    db.flush()
    for line in req.ingredients:
        ing = _resolve_ingredient(db, line.ingredient_name)
        recipe.ingredients.append(RecipeIngredient(
            raw_text=line.raw_text,
            ingredient_id=ing.id if ing else None,
            amount=line.amount,
            unit=line.unit,
            grams=line.grams,
            optional=line.optional,
        ))
    db.flush()
    recompute_recipe_kcal(recipe)
    db.commit()
    db.refresh(recipe)
    return {"id": recipe.id, "created": created, "title": recipe.title}


@router.get("/recipes")
def recipes_needing(
    need: str = Query("translate", pattern="^(translate|match)$"),
    limit: int = Query(50, ge=1, le=500),
    _: bool = Depends(require_core),
    db: Session = Depends(get_db),
):
    """Recepty, které potřebují zpracování jádrem (překlad / dopárování)."""
    stmt = select(Recipe).options(selectinload(Recipe.ingredients)).limit(limit)
    if need == "translate":
        stmt = stmt.where(
            (Recipe.source_domain.is_(None)) | (~Recipe.source_domain.like("%.cz"))
        )
    else:  # match – recepty s nenapárovanou surovinou
        sub = select(RecipeIngredient.recipe_id).where(RecipeIngredient.ingredient_id.is_(None))
        stmt = stmt.where(Recipe.id.in_(sub))
    out = []
    for r in db.scalars(stmt).all():
        out.append({
            "id": r.id,
            "title": r.title,
            "source_domain": r.source_domain,
            "instructions": r.instructions,
            "ingredients": [
                {"id": ri.id, "raw_text": ri.raw_text, "matched": ri.ingredient_id is not None}
                for ri in r.ingredients
            ],
        })
    return {"items": out}


@router.patch("/recipe/{recipe_id}")
def patch_recipe(recipe_id: int, req: RecipePatch, _: bool = Depends(require_core), db: Session = Depends(get_db)):
    r = db.scalar(select(Recipe).where(Recipe.id == recipe_id).options(selectinload(Recipe.ingredients)))
    if r is None:
        raise HTTPException(404, "Recept nenalezen.")
    if req.title is not None:
        r.title = req.title
    if req.instructions is not None:
        r.instructions = req.instructions
    if req.ingredient_texts and len(req.ingredient_texts) == len(r.ingredients):
        for ri, txt in zip(r.ingredients, req.ingredient_texts):
            ri.raw_text = txt
    db.commit()
    return {"ok": True}


@router.get("/ingredients")
def ingredients_needing(
    need: str = Query("categorize", pattern="^(categorize|nutrition)$"),
    limit: int = Query(200, ge=1, le=1000),
    _: bool = Depends(require_core),
    db: Session = Depends(get_db),
):
    if need == "categorize":
        cond = (Ingredient.category_path.is_(None)) | (Ingredient.category_path == "")
    else:
        cond = Ingredient.kcal_100g.is_(None)
    rows = db.execute(
        select(Ingredient.id, Ingredient.name_cs).where(cond).limit(limit)
    ).all()
    return {"items": [{"id": i, "name_cs": n} for i, n in rows]}


@router.patch("/ingredient/{ingredient_id}")
def patch_ingredient(ingredient_id: int, req: IngredientPatch, _: bool = Depends(require_core), db: Session = Depends(get_db)):
    ing = db.get(Ingredient, ingredient_id)
    if ing is None:
        raise HTTPException(404, "Surovina nenalezena.")
    if req.category_path is not None:
        ing.category_path = req.category_path
        if not ing.category:
            ing.category = req.category_path.split(">")[0].strip()
    if req.kcal_100g is not None:
        ing.kcal_100g = req.kcal_100g
    db.commit()
    return {"ok": True}

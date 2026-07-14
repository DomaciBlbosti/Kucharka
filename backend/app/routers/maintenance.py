"""Údržba dat: dopárování nenapárovaných surovin u receptů."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Ingredient, IngredientAlias, Recipe, RecipeIngredient
from ..modules import backfill, categorize, tagging, translate
from ..modules.normalizer import is_section_header
from ..modules.nutrition import recompute_recipe_kcal

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


class BackfillRequest(BaseModel):
    create_missing: bool = True  # smí LLM vytvářet nové suroviny


@router.get("/match-status")
def match_status():
    s = backfill.status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/backfill")
def run_backfill(req: BackfillRequest):
    create = req.create_missing and settings.ollama_enabled
    started = backfill.backfill_async(create_missing=create)
    return {"started": started, "status": backfill.status()}


def _fast_model_error() -> str | None:
    """None když je rychlý model dostupný, jinak srozumitelná hláška."""
    import httpx

    model = settings.ollama_fast_model
    try:
        r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as exc:  # noqa: BLE001
        return f"Ollama nedostupná: {exc}"
    base = model.split(":")[0]
    if any(n == model or n.split(":")[0] == base for n in names):
        return None
    return (
        f"Rychlý model '{model}' není v Ollamě stažený. Stáhni ho "
        f"(ollama pull {model}) nebo v Nástrojích nastav jiný / nech pole prázdné."
    )


@router.get("/translate-status")
def translate_status():
    s = translate.status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/translate")
def run_translate():
    if not settings.ollama_enabled:
        return {"started": False, "status": translate.status(), "error": "Ollama není dostupná."}
    err = _fast_model_error()
    if err:
        return {"started": False, "status": translate.status(), "error": err}
    started = translate.retranslate_async()
    return {"started": started, "status": translate.status(), "error": None}


@router.get("/categorize-status")
def categorize_status():
    s = categorize.status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/categorize")
def run_categorize():
    if not settings.ollama_enabled:
        return {"started": False, "status": categorize.status(), "error": "Ollama není dostupná."}
    err = _fast_model_error()
    if err:
        return {"started": False, "status": categorize.status(), "error": err}
    started = categorize.categorize_async(only_missing=True)
    return {"started": started, "status": categorize.status(), "error": None}


# ---- ruční párování nenapárovaných řádků ----

@router.post("/purge-headers")
def purge_headers(db: Session = Depends(get_db)):
    """Smaže nenapárované řádky, které nejsou surovina, ale jen nadpis
    skupiny (např. 'Marináda:', 'Na ozdobu:') – weby je vkládaly jako další
    položku seznamu ingrediencí. Napárované řádky (ingredient_id != NULL) se
    nedotýká; nová stažení už tyhle řádky vůbec nevytvoří (viz scraper.py)."""
    rows = db.scalars(
        select(RecipeIngredient).where(RecipeIngredient.ingredient_id.is_(None))
    ).all()
    removed = 0
    for ri in rows:
        if is_section_header(ri.raw_text):
            db.delete(ri)
            removed += 1
    db.commit()
    return {"removed": removed}


@router.get("/unmatched")
def unmatched(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Distinktní nenapárované texty surovin (seřazené podle četnosti)."""
    total = db.scalar(
        select(func.count(func.distinct(RecipeIngredient.raw_text))).where(
            RecipeIngredient.ingredient_id.is_(None)
        )
    ) or 0
    rows = db.execute(
        select(
            RecipeIngredient.raw_text,
            func.count().label("cnt"),
            func.min(RecipeIngredient.recipe_id).label("rid"),
        )
        .where(RecipeIngredient.ingredient_id.is_(None))
        .group_by(RecipeIngredient.raw_text)
        .order_by(func.count().desc(), RecipeIngredient.raw_text)
        .limit(limit)
        .offset(offset)
    ).all()
    items = []
    for raw_text, cnt, rid in rows:
        title = db.scalar(select(Recipe.title).where(Recipe.id == rid))
        items.append(
            {"raw_text": raw_text, "count": cnt, "recipe_id": rid, "recipe_title": title}
        )
    return {"items": items, "total_texts": total}


class MatchOne(BaseModel):
    raw_text: str
    ingredient_id: int | None = None
    new_name: str | None = None


@router.post("/match-one")
def match_one(req: MatchOne, db: Session = Depends(get_db)):
    """Přiřadí surovinu VŠEM nenapárovaným řádkům s daným textem; vytvoří alias a přepočítá kcal."""
    if req.ingredient_id:
        ing = db.get(Ingredient, req.ingredient_id)
        if ing is None:
            raise HTTPException(404, "Surovina nenalezena.")
    elif req.new_name and req.new_name.strip():
        name = req.new_name.strip()
        ing = db.scalar(
            select(Ingredient).where(func.lower(Ingredient.name_cs) == name.lower())
        )
        if ing is None:
            ing = Ingredient(name_cs=name, source="manual")
            db.add(ing)
            db.commit()
            db.refresh(ing)
    else:
        raise HTTPException(400, "Zadej surovinu nebo nový název.")

    rows = db.scalars(
        select(RecipeIngredient).where(
            RecipeIngredient.raw_text == req.raw_text,
            RecipeIngredient.ingredient_id.is_(None),
        )
    ).all()
    affected = set()
    for ri in rows:
        ri.ingredient_id = ing.id
        affected.add(ri.recipe_id)

    # alias, ať se příště stejný text napáruje sám
    alias_key = req.raw_text.strip().lower()[:200]
    if alias_key and not db.scalar(
        select(IngredientAlias).where(IngredientAlias.alias == alias_key)
    ):
        db.add(IngredientAlias(alias=alias_key, ingredient_id=ing.id))
    db.commit()

    for rid in affected:
        recipe = db.get(Recipe, rid)
        if recipe:
            recompute_recipe_kcal(recipe)
    db.commit()

    return {
        "updated_rows": len(rows),
        "recipes": len(affected),
        "ingredient_id": ing.id,
        "ingredient_name": ing.name_cs,
    }


@router.get("/tag-status")
def tag_status():
    s = tagging.status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/tag-recipes")
def run_tagging():
    if not settings.ollama_enabled:
        return {"started": False, "status": tagging.status(), "error": "Ollama není dostupná."}
    err = _fast_model_error()
    if err:
        return {"started": False, "status": tagging.status(), "error": err}
    started = tagging.tag_async(only_missing=True)
    return {"started": started, "status": tagging.status(), "error": None}


@router.get("/retranslate-status")
def retranslate_reset_status():
    s = translate.reset_status()
    s["ollama"] = settings.ollama_enabled
    return s


@router.post("/retranslate-reset")
def run_retranslate_reset():
    """Hromadně: znovu stáhni originál a přelož recepty, co vypadají jako starý strojový překlad."""
    if not settings.ollama_enabled:
        return {"started": False, "status": translate.reset_status(), "error": "Ollama není dostupná."}
    started = translate.reset_translations_async()
    return {"started": started, "status": translate.reset_status(), "error": None}

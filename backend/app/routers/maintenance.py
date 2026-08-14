"""Údržba dat: dopárování nenapárovaných surovin u receptů."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from datetime import datetime

from ..config import settings
from ..db import get_db
from ..models import Ingredient, IngredientAlias, MatchDecision, Recipe, RecipeIngredient
from ..modules import backfill, categorize, llm_match, tagging, translate
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
    """None když je zvolený LLM provider použitelný, jinak srozumitelná hláška."""
    import httpx

    from ..modules import llmclient

    if settings.llm_provider == "api":
        return llmclient.availability_error()

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
    from ..modules import llmclient

    s = categorize.status()
    s["ollama"] = settings.ollama_enabled
    s["llm_ready"] = llmclient.is_available()
    return s


@router.post("/categorize")
def run_categorize():
    err = _fast_model_error()
    if err:
        return {"started": False, "status": categorize.status(), "error": err}
    started = categorize.categorize_async(only_missing=True)
    return {"started": started, "status": categorize.status(), "error": None}


@router.get("/llm-match-status")
def llm_match_status():
    from ..modules import llmclient

    s = llm_match.status()
    s["ollama"] = settings.ollama_enabled
    s["llm_ready"] = llmclient.is_available()
    s["enabled"] = settings.llm_match_enabled
    return s


@router.post("/llm-match")
def run_llm_match():
    if not settings.llm_match_enabled:
        return {"started": False, "status": llm_match.status(),
                "error": "Vypnuto – zapni v Administraci → Nástroje (servery)."}
    err = _fast_model_error()
    if err:
        return {"started": False, "status": llm_match.status(), "error": err}
    started = llm_match.process_batch_async()
    return {"started": started, "status": llm_match.status(), "error": None}


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
    """Distinktní nenapárované texty surovin (seřazené podle četnosti).

    Ukazuje jen SKUTEČNĚ nerozhodnuté: texty, které už systém vyhodnotil
    jako ne-surovinu (alobal, pečicí papír…) nebo je člověk ignoroval,
    zůstávají v řádcích receptů nenapárované záměrně – tady by jen mátly
    ("proč to pořád visí ve frontě, když je to dávno vyřešené?")."""
    from ..modules.lookup import make_lookup_key

    rows = db.execute(
        select(
            RecipeIngredient.raw_text,
            func.count().label("cnt"),
            func.min(RecipeIngredient.recipe_id).label("rid"),
        )
        .where(
            RecipeIngredient.ingredient_id.is_(None),
            RecipeIngredient.nonfood.is_(False),
            RecipeIngredient.raw_text.is_not(None),
        )
        .group_by(RecipeIngredient.raw_text)
        .order_by(func.count().desc(), RecipeIngredient.raw_text)
    ).all()

    # klíče, o kterých už je finálně rozhodnuto (non-food slovník / ignorováno)
    settled = set(db.scalars(
        select(IngredientAlias.lookup_key).where(
            IngredientAlias.lookup_key.is_not(None),
            IngredientAlias.kind != "food",
        )
    ).all())
    settled |= set(db.scalars(
        select(MatchDecision.lookup_key).where(
            MatchDecision.status.in_(("nonfood", "ignored"))
        )
    ).all())

    filtered = [
        (raw_text, cnt, rid) for raw_text, cnt, rid in rows
        if make_lookup_key(raw_text) not in settled
    ]
    total = len(filtered)
    page = filtered[offset:offset + limit]
    items = []
    for raw_text, cnt, rid in page:
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

    # zaznamenej i do katalogu rozhodnutí (ruční = finální)
    from ..modules.lookup import make_lookup_key

    key = make_lookup_key(req.raw_text)
    if key:
        llm_match._upsert_decision(
            db, key, req.raw_text, status="applied", category="food",
            ingredient_id=ing.id, confidence=1.0, model="manual",
            occurrences=len(rows),
        )
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


# ---- katalog rozhodnutí (match_decision) ----

def _decision_out(d: MatchDecision) -> dict:
    return {
        "id": d.id,
        "lookup_key": d.lookup_key,
        "sample_text": d.sample_text,
        "status": d.status,
        "category": d.category,
        "ingredient_id": d.ingredient_id,
        "ingredient_name": d.ingredient.name_cs if d.ingredient else None,
        "suggested_name": d.suggested_name,
        "confidence": d.confidence,
        "model": d.model,
        "occurrences": d.occurrences,
        "attempts": d.attempts,
        "error": d.error,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


@router.get("/decisions")
def list_decisions(
    status: str = Query("", description="filtr stavu; 'review' = suggested+no_match+error"),
    q: str = Query("", description="hledání v textu"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Katalog rozhodnutí párování: co LLM/člověk rozhodl, s jakou jistotou,
    a co čeká na ruční dořešení."""
    stmt = select(MatchDecision)
    if status == "review":
        stmt = stmt.where(MatchDecision.status.in_(("suggested", "no_match", "error")))
    elif status:
        stmt = stmt.where(MatchDecision.status == status)
    if q.strip():
        needle = f"%{q.strip()}%"
        stmt = stmt.where(
            MatchDecision.sample_text.like(needle) | MatchDecision.lookup_key.like(needle)
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(MatchDecision.occurrences.desc(), MatchDecision.id.desc())
        .limit(limit).offset(offset)
    ).all()
    return {
        "items": [_decision_out(d) for d in rows],
        "total": total,
        "summary": llm_match.decisions_summary(db),
    }


@router.post("/decisions/retry-errors")
def retry_error_decisions(db: Session = Depends(get_db)):
    """Hromadně vrátí VŠECHNY chybové položky do hry: vynuluje pokusy dávkové
    fáze i příznak kontextové fáze. Příští běh dopárování je vezme znovu –
    užitečné poté, co se vyřešila příčina chyb (timeouty, spadlá Ollama)."""
    rows = db.scalars(
        select(MatchDecision).where(MatchDecision.status == "error")
    ).all()
    for d in rows:
        d.attempts = 0
        d.ctx_tried = False
        d.updated_at = datetime.utcnow()
    db.commit()
    return {"reset": len(rows)}


class DecisionResolve(BaseModel):
    action: str  # accept | assign | nonfood | ignore | retry
    ingredient_id: int | None = None
    new_name: str | None = None


@router.post("/decisions/{decision_id}/resolve")
def resolve_decision(decision_id: int, req: DecisionResolve, db: Session = Depends(get_db)):
    """Ruční dořešení položky katalogu: přijmout návrh LLM, přiřadit jinou
    surovinu, označit jako ne-surovinu, ignorovat, nebo poslat znovu do LLM."""
    d = db.get(MatchDecision, decision_id)
    if d is None:
        raise HTTPException(404, "Rozhodnutí nenalezeno.")

    if req.action == "retry":
        # smazání záznamu = příští běh LLM se položky zeptá znovu. Musí se
        # smazat i případný NEOVĚŘENÝ alias z LLM se stejným klíčem – jinak
        # by slovníkový sweep položku okamžitě zase "rozhodl" postaru a
        # k žádnému novému dotazu by nedošlo (týkalo se hlavně ne-surovin).
        stale_alias = db.scalar(
            select(IngredientAlias).where(
                IngredientAlias.lookup_key == d.lookup_key,
                IngredientAlias.verified.is_(False),
                IngredientAlias.source == "llm",
            )
        )
        if stale_alias is not None:
            db.delete(stale_alias)
        if d.status in ("nonfood", "ignored"):
            # řádky označené jako vyřešené-bez-suroviny vrať mezi čekající,
            # jinak by je párování dál přeskakovalo a retry by nic nedělal
            llm_match.mark_key_rows(db, d.lookup_key, False)
        db.delete(d)
        db.commit()
        return {"ok": True, "action": "retry"}

    if req.action == "ignore":
        d.status = "ignored"
        d.model = "manual"
        d.error = None
        d.updated_at = datetime.utcnow()
        # řádky označit jako vyřešené-bez-suroviny, ať se nepočítají mezi čekající
        llm_match.mark_key_rows(db, d.lookup_key, True)
        db.commit()
        return {"ok": True, "action": "ignore", "decision": _decision_out(d)}

    if req.action == "nonfood":
        llm_match._upsert_alias(
            db, d.sample_text, lookup_key=d.lookup_key, ingredient_id=None,
            kind="unknown", source="manual", confidence=1.0, verified=True,
        )
        d.status = "nonfood"
        d.category = d.category if d.category and d.category != "food" else "unknown"
        d.ingredient_id = None
        d.model = "manual"
        d.error = None
        d.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "action": "nonfood", "decision": _decision_out(d)}

    if req.action in ("accept", "assign"):
        if req.action == "accept":
            if d.ingredient_id:
                ing = db.get(Ingredient, d.ingredient_id)
                if ing is None:
                    raise HTTPException(404, "Navržená surovina už neexistuje.")
            elif d.suggested_name:
                # LLM navrhlo založit novou surovinu → založ a zkus doplnit výživu
                ing = llm_match.get_or_create_ingredient(db, d.suggested_name)
                if ing.kcal_100g is None:
                    llm_match.estimate_nutrition(db, [ing])
            else:
                raise HTTPException(400, "Položka nemá žádný návrh k přijetí.")
        elif req.ingredient_id:
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
                db.flush()
        else:
            raise HTTPException(400, "Zadej surovinu nebo nový název.")

        result = llm_match.apply_manual_match(db, d, ing)
        return {"ok": True, "action": req.action, "decision": _decision_out(d), **result}

    raise HTTPException(400, f"Neznámá akce '{req.action}'.")


@router.get("/tag-status")
def tag_status():
    from ..modules import llmclient

    s = tagging.status()
    s["ollama"] = settings.ollama_enabled
    s["llm_ready"] = llmclient.is_available()
    return s


@router.post("/tag-recipes")
def run_tagging():
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

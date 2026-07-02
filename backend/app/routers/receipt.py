"""Skenování účtenky – náhled položek a jejich potvrzení do spíže."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Ingredient, IngredientAlias, PantryItem
from ..modules import receipt
from ..modules.normalizer import _norm

router = APIRouter(prefix="/api/receipt", tags=["receipt"])


@router.post("/scan")
async def scan(
    images: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    if not settings.ocr_model:
        raise HTTPException(
            400,
            "OCR model není nastaven. Nastav ho v Admin → Nástroje → OCR model "
            "(vision model stažený v Ollamě, např. qwen2.5vl nebo minicpm-v).",
        )
    if not images:
        raise HTTPException(400, "Nahraj alespoň jednu fotku úseku účtenky.")

    raw = [await f.read() for f in images]
    try:
        result = receipt.scan_segments(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Čtení účtenky selhalo: {exc}") from None

    matched = receipt.match_items(db, result["items"])
    db.rollback()  # match_ingredient může připsat aliasy – u náhledu nic neukládej

    return {
        "items": matched,
        "segments_read": result["segments"],
        "total_found": len(matched),
    }


class ConfirmItem(BaseModel):
    raw_name: str
    ingredient_id: int | None = None
    new_name: str | None = None
    include: bool = True


class ConfirmRequest(BaseModel):
    items: list[ConfirmItem]


@router.post("/confirm")
def confirm(req: ConfirmRequest, db: Session = Depends(get_db)):
    added = 0
    already = 0
    for it in req.items:
        if not it.include:
            continue
        ing: Ingredient | None = None
        if it.ingredient_id:
            ing = db.get(Ingredient, it.ingredient_id)
        elif it.new_name and it.new_name.strip():
            name = it.new_name.strip()
            ing = db.scalar(
                select(Ingredient).where(Ingredient.name_cs.ilike(name))
            )
            if ing is None:
                ing = Ingredient(name_cs=name, source="receipt")
                db.add(ing)
                db.flush()
            key = _norm(it.raw_name)[:200]
            if key and not db.scalar(
                select(IngredientAlias).where(IngredientAlias.alias == key)
            ):
                db.add(IngredientAlias(alias=key, ingredient_id=ing.id))
        if ing is None:
            continue

        existing = db.scalar(select(PantryItem).where(PantryItem.ingredient_id == ing.id))
        if existing:
            already += 1
        else:
            db.add(PantryItem(ingredient_id=ing.id))
            added += 1
    db.commit()
    return {"added": added, "already_had": already}

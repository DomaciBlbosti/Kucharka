"""Skenování čárových kódů při vybalování nákupu → přidání do spíže.

Naskenovaný kód se poprvé ověří přes Open Food Facts a fuzzy napáruje na
kanonickou surovinu; jakmile uživatel párování potvrdí, uloží se natrvalo do
`barcode_map` – při dalším skenování stejného produktu se přidá do spíže
okamžitě, bez dotazu.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import BarcodeMap, Ingredient, PantryItem
from ..modules import openfoodfacts
from ..modules.normalizer import match_ingredient

router = APIRouter(prefix="/api/pantry/barcode", tags=["pantry"])


def _add_to_pantry(db: Session, ingredient_id: int) -> None:
    if not db.scalar(select(PantryItem).where(PantryItem.ingredient_id == ingredient_id)):
        db.add(PantryItem(ingredient_id=ingredient_id))


class BarcodeScan(BaseModel):
    code: str


@router.post("/scan")
def scan(req: BarcodeScan, db: Session = Depends(get_db)):
    code = req.code.strip()
    if not code:
        raise HTTPException(400, "Prázdný kód.")

    mapped = db.get(BarcodeMap, code)
    if mapped and db.get(Ingredient, mapped.ingredient_id):
        _add_to_pantry(db, mapped.ingredient_id)
        db.commit()
        ing = db.get(Ingredient, mapped.ingredient_id)
        return {"added": True, "known": True, "ingredient_name": ing.name_cs}

    off = openfoodfacts.lookup(code)
    off_name = off["name"] if off else None
    matched = match_ingredient(db, off_name) if off_name else None
    db.rollback()  # match_ingredient může připsat alias – u náhledu nic neukládej

    return {
        "added": False,
        "known": False,
        "off_name": off_name,
        "brand": off.get("brand") if off else None,
        "matched": {"id": matched.id, "name": matched.name_cs} if matched else None,
    }


class BarcodeConfirm(BaseModel):
    code: str
    ingredient_id: int | None = None
    new_name: str | None = None
    off_name: str | None = None


@router.post("/confirm")
def confirm(req: BarcodeConfirm, db: Session = Depends(get_db)):
    code = req.code.strip()
    if not code:
        raise HTTPException(400, "Prázdný kód.")

    ing: Ingredient | None = None
    if req.ingredient_id:
        ing = db.get(Ingredient, req.ingredient_id)
    elif req.new_name and req.new_name.strip():
        name = req.new_name.strip()
        ing = db.scalar(select(Ingredient).where(Ingredient.name_cs.ilike(name)))
        if ing is None:
            ing = Ingredient(name_cs=name, source="barcode")
            db.add(ing)
            db.flush()
    if ing is None:
        raise HTTPException(400, "Vyber existující surovinu, nebo zadej název nové.")

    existing = db.get(BarcodeMap, code)
    if existing:
        existing.ingredient_id = ing.id
        existing.off_name = req.off_name
    else:
        db.add(BarcodeMap(barcode=code, ingredient_id=ing.id, off_name=req.off_name))

    _add_to_pantry(db, ing.id)
    db.commit()
    return {"added": True, "ingredient_name": ing.name_cs}

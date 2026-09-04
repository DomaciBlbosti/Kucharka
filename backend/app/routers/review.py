"""Ruční kontrola receptů – stránkovaný výpis a ukládání štítků.

Obsluhuje záložku Kontrola: člověk si prochází recepty po stránkách, u každého
vidí, co přišlo ze zdroje vedle toho, co appka ukazuje, a rozhodne se
(zkontrolováno / není recept / špatný překlad…). Logika je v modules/review.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..modules import recipe_export, review

router = APIRouter(prefix="/api/review", tags=["review"])


class ReviewIn(BaseModel):
    labels: list[str] = Field(default_factory=list)
    note: str | None = None


@router.get("/labels")
def labels():
    """Nabídka štítků a výběrových režimů – UI si ji nedrží natvrdo, ať se
    nemůže rozejít se serverem."""
    return {"labels": review.LABELS, "picks": recipe_export.PICKS}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    """Kolik receptů je zkontrolovaných, kolik zbývá a čeho se štítky týkají."""
    return review.stats(db)


@router.get("/recipes")
def recipes(
    pick: str = Query("random"),
    domain: str | None = Query(None),
    only_unreviewed: bool = Query(False),
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=50),
    seed: int = Query(42),
    include_raw: bool = Query(False, description="přibalit i syrová data ze scraperu"),
    db: Session = Depends(get_db),
):
    """Jedna stránka receptů ke kontrole se vším, co je k posouzení potřeba."""
    if pick not in recipe_export.PICKS:
        raise HTTPException(
            400, f"Neznámý výběr {pick!r}. Možnosti: "
                 f"{', '.join(sorted(recipe_export.PICKS))}.")
    return review.page(
        db, pick=pick, domain=domain, only_unreviewed=only_unreviewed,
        page_no=page, per_page=per_page, seed=seed, include_raw=include_raw,
    )


@router.put("/{recipe_id}")
def save(recipe_id: int, body: ReviewIn, db: Session = Depends(get_db)):
    """Ulož rozhodnutí. Prázdné štítky i poznámka = kontrola se zruší."""
    try:
        return review.save(db, recipe_id, body.labels, body.note)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
